# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Checkpoint Manager — Transparent filesystem snapshots via a single shared
shadow git store.

Creates automatic snapshots of working directories before file-mutating
operations (``write_file``, ``patch``, ``terminal`` with destructive flags),
triggered once per conversation turn.  Provides rollback to any previous
checkpoint.

This is NOT a tool — the LLM never sees it.  It's transparent infrastructure
controlled by the ``checkpoints`` config flag or ``--checkpoints`` CLI flag.

Storage layout (single shared store, git objects deduplicated across projects)
-----------------------------------------------------------------------------

    M-Claw home checkpoints/
        store/                          — single bare-ish git repo
            HEAD, config, objects/      — standard git internals (shared)
            refs/mclaw/<hash16>         — per-project branch tip
            indexes/<hash16>            — per-project git index
            projects/<hash16>.json      — {workdir, created_at, last_touch}
            meta/<commit>.json          — optional session/tool metadata
            info/exclude                — default excludes (shared)
        .last_prune                     — auto-prune idempotency marker

Why a single store?
-------------------

The single shared store lets git's content-addressable object DB deduplicate
across projects and across turns, so adding a new worktree costs near-zero.

The shadow store uses ``GIT_DIR`` + ``GIT_WORK_TREE`` + ``GIT_INDEX_FILE``
so no git state leaks into the user's project directory.

Auto-maintenance
----------------

Shadow state accumulates over time.  ``prune_checkpoints`` deletes refs whose
recorded working directory no longer exists (orphan) or whose last touch is
older than ``retention_days`` (stale), then runs ``git gc --prune=now`` to
reclaim object storage.  A size-cap pass drops the oldest checkpoints per
project until total store size is under ``max_total_size_mb``.
"""

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from mclaw.constants import get_mclaw_home
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CHECKPOINT_BASE = get_mclaw_home() / "checkpoints"

# Single shared store directory under CHECKPOINT_BASE.
_STORE_DIRNAME = "store"
_REFS_PREFIX = "refs/mclaw"
_INDEXES_DIRNAME = "indexes"
_PROJECTS_DIRNAME = "projects"
_META_DIRNAME = "meta"
DEFAULT_EXCLUDES = [
    # Dependency / build output
    "node_modules/",
    "dist/",
    "build/",
    "target/",
    "out/",
    ".next/",
    ".nuxt/",
    # Caches
    "__pycache__/",
    "*.pyc",
    "*.pyo",
    ".cache/",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
    "coverage/",
    ".coverage",
    # Virtualenvs
    ".venv/",
    "venv/",
    "env/",
    # VCS
    ".git/",
    ".hg/",
    ".svn/",
    # Worktrees (M-Claw convention — don't recursively snapshot siblings)
    ".worktrees/",
    # Native / compiled binaries
    "*.so",
    "*.dylib",
    "*.dll",
    "*.o",
    "*.a",
    "*.jar",
    "*.class",
    "*.exe",
    "*.obj",
    # Media / large binaries
    "*.mp4",
    "*.mov",
    "*.mkv",
    "*.webm",
    "*.zip",
    "*.tar",
    "*.tar.gz",
    "*.tgz",
    "*.7z",
    "*.rar",
    "*.iso",
    # Secrets
    ".env",
    ".env.*",
    ".env.local",
    ".env.*.local",
    # OS junk
    ".DS_Store",
    "Thumbs.db",
    # Logs
    "*.log",
]

_MCLAW_RUNTIME_CHECKPOINT_SKIP_DIRS = (
    "temp",
    "tmp",
    "snapshots",
    "logs",
)

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


# Git subprocess timeout (seconds).
_GIT_TIMEOUT: int = max(10, min(60, _env_int("MCLAW_CHECKPOINT_TIMEOUT", 30)))

# Max files to snapshot — skip huge directories to avoid slowdowns.
_MAX_FILES = 50_000

# Valid git commit hash pattern: 4–40 hex chars (short or full SHA-1/SHA-256).
_COMMIT_HASH_RE = re.compile(r'^[0-9a-fA-F]{4,64}$')


# ---------------------------------------------------------------------------
# Input validation helpers
# ---------------------------------------------------------------------------

def _validate_commit_hash(commit_hash: str) -> Optional[str]:
    """Validate a commit hash to prevent git argument injection.

    Returns an error string if invalid, None if valid.
    Values starting with '-' would be interpreted as git flags
    (e.g., '--patch', '-p') instead of revision specifiers.
    """
    if not commit_hash or not commit_hash.strip():
        return "Empty commit hash"
    if commit_hash.startswith("-"):
        return f"Invalid commit hash (must not start with '-'): {commit_hash!r}"
    if not _COMMIT_HASH_RE.match(commit_hash):
        return f"Invalid commit hash (expected 4-64 hex characters): {commit_hash!r}"
    return None


def _validate_file_path(file_path: str, working_dir: str) -> Optional[str]:
    """Validate a file path to prevent path traversal outside the working directory.

    Returns an error string if invalid, None if valid.
    """
    if not file_path or not file_path.strip():
        return "Empty file path"
    if os.path.isabs(file_path):
        return f"File path must be relative, got absolute path: {file_path!r}"
    abs_workdir = _normalize_path(working_dir)
    resolved = (abs_workdir / file_path).resolve()
    try:
        resolved.relative_to(abs_workdir)
    except ValueError:
        return f"File path escapes the working directory via traversal: {file_path!r}"
    return None


# ---------------------------------------------------------------------------
# Path / hash helpers
# ---------------------------------------------------------------------------

def _normalize_path(path_value: str) -> Path:
    """Return a canonical absolute path for checkpoint operations."""
    return Path(path_value).expanduser().resolve()


def _path_is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _checkpoint_skip_reason(path_value: str) -> Optional[str]:
    """Return why a working directory should not be checkpointed, if any."""
    try:
        path = _normalize_path(path_value)
    except Exception:
        return None

    roots: List[Tuple[Path, str]] = [
        (CHECKPOINT_BASE, "M-Claw checkpoint store"),
    ]
    mclaw_home = get_mclaw_home()
    roots.extend(
        (mclaw_home / name, f"M-Claw runtime directory: {name}")
        for name in _MCLAW_RUNTIME_CHECKPOINT_SKIP_DIRS
    )

    for root, reason in roots:
        try:
            normalized_root = root.expanduser().resolve()
        except Exception:
            normalized_root = root.expanduser()
        if _path_is_relative_to(path, normalized_root):
            return reason
    return None


def _project_hash(working_dir: str) -> str:
    """Deterministic per-project hash: sha256(abs_path)[:16]."""
    abs_path = str(_normalize_path(working_dir))
    return hashlib.sha256(abs_path.encode()).hexdigest()[:16]


def _store_path(base: Optional[Path] = None) -> Path:
    """Return the single shared shadow store path."""
    return (base or CHECKPOINT_BASE) / _STORE_DIRNAME


def _index_path(store: Path, dir_hash: str) -> Path:
    return store / _INDEXES_DIRNAME / dir_hash


def _ref_name(dir_hash: str) -> str:
    return f"{_REFS_PREFIX}/{dir_hash}"


def _project_meta_path(store: Path, dir_hash: str) -> Path:
    return store / _PROJECTS_DIRNAME / f"{dir_hash}.json"


def _checkpoint_meta_path(store: Path, commit_hash: str) -> Path:
    return store / _META_DIRNAME / f"{commit_hash}.json"


def _write_checkpoint_meta(store: Path, commit_hash: str, metadata: Optional[Dict]) -> None:
    if not metadata:
        return
    try:
        meta_path = _checkpoint_meta_path(store, commit_hash)
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta = dict(metadata)
        meta.setdefault("commit", commit_hash)
        meta.setdefault("created_at", time.time())
        meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        logger.debug("Could not write checkpoint metadata for %s: %s", commit_hash, exc)


def _read_checkpoint_meta(store: Path, commit_hash: str) -> Optional[Dict]:
    try:
        meta_path = _checkpoint_meta_path(store, commit_hash)
        if not meta_path.exists():
            return None
        value = json.loads(meta_path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Git env
# ---------------------------------------------------------------------------

def _git_env(
    store: Path,
    working_dir: str,
    index_file: Optional[Path] = None,
) -> dict:
    """Build env dict that redirects git to the shared store.

    The shared store is internal M-Claw infrastructure — it must NOT inherit
    the user's global or system git config.  User-level settings like
    ``commit.gpgsign = true``, signing hooks, or credential helpers would
    either break background snapshots or, worse, spawn interactive prompts
    (pinentry GUI windows) mid-session every time a file is written.

    Isolation strategy:
    * ``GIT_CONFIG_GLOBAL=<os.devnull>`` — ignore ``~/.gitconfig`` (git 2.32+).
    * ``GIT_CONFIG_SYSTEM=<os.devnull>`` — ignore ``/etc/gitconfig`` (git 2.32+).
    * ``GIT_CONFIG_NOSYSTEM=1`` — extra guard against system git config.

    ``index_file``, if given, forces git to use a per-project index under
    ``store/indexes/<hash>`` so projects don't race on a shared index.
    """
    normalized_working_dir = _normalize_path(working_dir)
    env = os.environ.copy()
    env["GIT_DIR"] = str(store)
    env["GIT_WORK_TREE"] = str(normalized_working_dir)
    env.pop("GIT_NAMESPACE", None)
    env.pop("GIT_ALTERNATE_OBJECT_DIRECTORIES", None)
    if index_file is not None:
        env["GIT_INDEX_FILE"] = str(index_file)
    else:
        env.pop("GIT_INDEX_FILE", None)
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    return env


def _run_git(
    args: List[str],
    store: Path,
    working_dir: str,
    timeout: int = _GIT_TIMEOUT,
    allowed_returncodes: Optional[Set[int]] = None,
    index_file: Optional[Path] = None,
) -> Tuple[bool, str, str]:
    """Run a git command against the shared store.  Returns (ok, stdout, stderr).

    ``allowed_returncodes`` suppresses error logging for known/expected non-zero
    exits while preserving the normal ``ok = (returncode == 0)`` contract.
    Example: ``git diff --cached --quiet`` returns 1 when changes exist.
    """
    normalized_working_dir = _normalize_path(working_dir)
    if not normalized_working_dir.exists():
        msg = f"working directory not found: {normalized_working_dir}"
        logger.error("Git command skipped: %s (%s)", " ".join(["git"] + list(args)), msg)
        return False, "", msg
    if not normalized_working_dir.is_dir():
        msg = f"working directory is not a directory: {normalized_working_dir}"
        logger.error("Git command skipped: %s (%s)", " ".join(["git"] + list(args)), msg)
        return False, "", msg

    env = _git_env(store, str(normalized_working_dir), index_file=index_file)
    cmd = ["git"] + list(args)
    allowed_returncodes = allowed_returncodes or set()
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
            cwd=str(normalized_working_dir),
        )
        ok = result.returncode == 0
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()
        if not ok and result.returncode not in allowed_returncodes:
            logger.error(
                "Git command failed: %s (rc=%d) stderr=%s",
                " ".join(cmd), result.returncode, stderr,
            )
        return ok, stdout, stderr
    except subprocess.TimeoutExpired:
        msg = f"git timed out after {timeout}s: {' '.join(cmd)}"
        logger.error(msg, exc_info=True)
        return False, "", msg
    except FileNotFoundError as exc:
        missing_target = getattr(exc, "filename", None)
        if missing_target == "git":
            logger.error("Git executable not found: %s", " ".join(cmd), exc_info=True)
            return False, "", "git not found"
        msg = f"working directory not found: {normalized_working_dir}"
        logger.error("Git command failed before execution: %s (%s)", " ".join(cmd), msg, exc_info=True)
        return False, "", msg
    except Exception as exc:
        logger.error("Unexpected git error running %s: %s", " ".join(cmd), exc, exc_info=True)
        return False, "", str(exc)


def _commit_belongs_to_project(store: Path, working_dir: str, commit_hash: str) -> bool:
    """Return True only when commit_hash is reachable from this workdir's ref."""
    ref = _ref_name(_project_hash(working_dir))
    ok, _, _ = _run_git(
        ["merge-base", "--is-ancestor", commit_hash, ref],
        store,
        working_dir,
        allowed_returncodes={1, 128},
    )
    return ok


# ---------------------------------------------------------------------------
# Store initialisation
# ---------------------------------------------------------------------------


def _init_store(store: Path) -> Optional[str]:
    """Initialise the shared shadow store if needed.  Returns error or None."""
    base = store.parent
    if not store.exists():
        try:
            base.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return f"Could not create checkpoint base: {exc}"

    if (store / "HEAD").exists():
        return None

    store.mkdir(parents=True, exist_ok=True)
    (store / _INDEXES_DIRNAME).mkdir(exist_ok=True)
    (store / _PROJECTS_DIRNAME).mkdir(exist_ok=True)
    (store / _META_DIRNAME).mkdir(exist_ok=True)

    # ``git init --bare`` rejects GIT_WORK_TREE, so we can't use _run_git
    # here (which always sets GIT_DIR + GIT_WORK_TREE).  Use a raw
    # subprocess with just the config-isolation env vars.
    init_env = os.environ.copy()
    init_env["GIT_CONFIG_GLOBAL"] = os.devnull
    init_env["GIT_CONFIG_SYSTEM"] = os.devnull
    init_env["GIT_CONFIG_NOSYSTEM"] = "1"
    # Drop any inherited GIT_* that would interfere.
    for k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_NAMESPACE",
              "GIT_ALTERNATE_OBJECT_DIRECTORIES"):
        init_env.pop(k, None)
    try:
        result = subprocess.run(
            ["git", "init", "--bare", str(store)],
            capture_output=True, text=True,
            env=init_env, timeout=_GIT_TIMEOUT,
        )
        if result.returncode != 0:
            return f"Shadow store init failed: {result.stderr.strip()}"
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return f"Shadow store init failed: {exc}"

    # Per-store config (isolated by env vars above, but belt-and-suspenders).
    # Use the base dir as the working_dir for config commands — it always
    # exists since we just created the store inside it.
    cfg_wd = str(base)
    _run_git(["config", "user.email", "mclaw@local"], store, cfg_wd)
    _run_git(["config", "user.name", "M-Claw Checkpoint"], store, cfg_wd)
    _run_git(["config", "commit.gpgsign", "false"], store, cfg_wd)
    _run_git(["config", "tag.gpgSign", "false"], store, cfg_wd)
    _run_git(["config", "gc.auto", "0"], store, cfg_wd)

    info_dir = store / "info"
    info_dir.mkdir(exist_ok=True)
    (info_dir / "exclude").write_text(
        "\n".join(DEFAULT_EXCLUDES) + "\n", encoding="utf-8"
    )

    logger.debug("Initialised checkpoint store at %s", store)
    return None


def _register_project(store: Path, working_dir: str) -> None:
    """Create or update ``projects/<hash>.json`` with workdir + timestamps."""
    dir_hash = _project_hash(working_dir)
    meta_path = _project_meta_path(store, dir_hash)
    now = time.time()
    meta: Dict = {"workdir": str(_normalize_path(working_dir)),
                  "created_at": now, "last_touch": now}
    if meta_path.exists():
        try:
            existing = json.loads(meta_path.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                meta["created_at"] = existing.get("created_at", now)
        except (OSError, ValueError):
            pass
    try:
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
    except OSError as exc:
        logger.debug("Could not write project metadata %s: %s", meta_path, exc)


def _touch_project(store: Path, working_dir: str) -> None:
    """Update last_touch for a project, preserving created_at."""
    dir_hash = _project_hash(working_dir)
    meta_path = _project_meta_path(store, dir_hash)
    if not meta_path.exists():
        _register_project(store, working_dir)
        return
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    meta["workdir"] = str(_normalize_path(working_dir))
    meta["last_touch"] = time.time()
    meta.setdefault("created_at", meta["last_touch"])
    try:
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
    except OSError as exc:
        logger.debug("Could not update project metadata %s: %s", meta_path, exc)


def _list_projects(store: Path) -> List[Dict]:
    """Return all registered projects under the store."""
    projects_dir = store / _PROJECTS_DIRNAME
    if not projects_dir.exists():
        return []
    out: List[Dict] = []
    for meta_path in projects_dir.glob("*.json"):
        dir_hash = meta_path.stem
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(meta, dict):
            continue
        meta["_hash"] = dir_hash
        out.append(meta)
    return out


def _dir_file_count(path: str) -> int:
    """Quick file count estimate (stops early if over _MAX_FILES)."""
    count = 0
    try:
        for _ in Path(path).rglob("*"):
            count += 1
            if count > _MAX_FILES:
                return count
    except (PermissionError, OSError):
        pass
    return count


def _dir_size_bytes(path: Path) -> int:
    """Best-effort recursive size in bytes.  Returns 0 on error."""
    total = 0
    try:
        for p in path.rglob("*"):
            try:
                if p.is_file():
                    total += p.stat().st_size
            except OSError:
                continue
    except OSError:
        pass
    return total


# ---------------------------------------------------------------------------
# CheckpointManager
# ---------------------------------------------------------------------------

class CheckpointManager:
    """Manages automatic filesystem checkpoints.

    Designed to be owned by AIAgent.  Call ``new_turn()`` at the start of
    each conversation turn and ``ensure_checkpoint(dir, reason)`` before
    any file-mutating tool call.  The manager deduplicates so at most one
    snapshot is taken per directory per turn.

    Parameters
    ----------
    enabled : bool
        Master switch (from config / CLI flag).
    max_snapshots : int
        Keep at most this many checkpoints per directory.
    max_total_size_mb : int
        Hard ceiling on total store size.  Oldest checkpoints per project
        are dropped when the store exceeds this after a commit.
    max_file_size_mb : int
        Skip adding any single file larger than this to a checkpoint.
        (Implemented via ``.gitignore`` excludes + a post-stage size check.)
    """

    def __init__(
        self,
        enabled: bool = False,
        max_snapshots: int = 20,
        max_total_size_mb: int = 500,
        max_file_size_mb: int = 10,
    ):
        self.enabled = enabled
        self.max_snapshots = max(1, int(max_snapshots))
        self.max_total_size_mb = max(0, int(max_total_size_mb))
        self.max_file_size_mb = max(0, int(max_file_size_mb))
        self._checkpointed_dirs: Set[str] = set()
        self._checkpointed_targets: Dict[str, Set[str]] = {}
        self._checkpointed_missing_targets: Dict[str, Set[str]] = {}
        self._git_available: Optional[bool] = None  # lazy probe
        self.last_attempt: Dict = {}

    # ------------------------------------------------------------------
    # Turn lifecycle
    # ------------------------------------------------------------------

    def new_turn(self) -> None:
        """Reset per-turn dedup.  Call at the start of each agent iteration."""
        self._checkpointed_dirs.clear()
        self._checkpointed_targets.clear()
        self._checkpointed_missing_targets.clear()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def ensure_checkpoint(
        self,
        working_dir: str,
        reason: str = "auto",
        metadata: Optional[Dict] = None,
        target_paths: Optional[List[str]] = None,
    ) -> bool:
        """Take a checkpoint if enabled and not already done this turn.

        Returns True if a checkpoint was taken, False otherwise.
        Never raises — all errors are silently logged.
        """
        if not self.enabled:
            self._record_attempt("disabled", working_dir, reason)
            return False

        if self._git_available is None:
            self._git_available = shutil.which("git") is not None
            if not self._git_available:
                logger.debug("Checkpoints disabled: git not found")
        if not self._git_available:
            self._record_attempt("skipped", working_dir, reason, detail="git not found")
            return False

        abs_dir = str(_normalize_path(working_dir))
        skip_reason = _checkpoint_skip_reason(abs_dir)
        if skip_reason:
            logger.debug("Checkpoint skipped: %s (%s)", skip_reason, abs_dir)
            self._record_attempt("skipped", abs_dir, reason, detail=skip_reason)
            return False

        target_info = self._target_info(abs_dir, target_paths)
        target_keys = set(target_info.get("rel") or [])
        targeted_candidate = bool(target_keys) and _dir_file_count(abs_dir) > _MAX_FILES

        # Skip root, home, and other overly broad directories
        if abs_dir in {"/", str(Path.home())}:
            logger.debug("Checkpoint skipped: directory too broad (%s)", abs_dir)
            self._record_attempt("skipped", abs_dir, reason, detail="directory too broad")
            return False

        if abs_dir in self._checkpointed_dirs:
            self._record_attempt("skipped", abs_dir, reason, detail="already checkpointed this turn")
            return False
        if targeted_candidate:
            seen_targets = self._checkpointed_targets.get(abs_dir, set())
            if target_keys and target_keys <= seen_targets:
                self._record_attempt("skipped", abs_dir, reason, detail="target already checkpointed this turn")
                return False
            if seen_targets:
                metadata = dict(metadata or {})
                metadata["_previous_target_paths_rel"] = sorted(seen_targets)
                metadata["_previous_missing_targets_rel"] = sorted(
                    self._checkpointed_missing_targets.get(abs_dir, set())
                )

        try:
            taken = self._take(abs_dir, reason, metadata=metadata, target_paths=target_paths)
            if taken:
                if targeted_candidate:
                    self._checkpointed_targets.setdefault(abs_dir, set()).update(target_keys)
                    self._checkpointed_missing_targets.setdefault(abs_dir, set()).update(
                        set(target_info.get("missing") or [])
                    )
                else:
                    self._checkpointed_dirs.add(abs_dir)
            return taken
        except Exception as e:
            logger.debug("Checkpoint failed (non-fatal): %s", e)
            self._record_attempt("failed", abs_dir, reason, detail=str(e))
            return False

    def create_checkpoint(
        self,
        working_dir: str,
        reason: str = "manual",
        metadata: Optional[Dict] = None,
        target_paths: Optional[List[str]] = None,
    ) -> bool:
        """Force a checkpoint without per-turn deduplication."""
        if not self.enabled:
            self._record_attempt("disabled", working_dir, reason)
            return False
        if self._git_available is None:
            self._git_available = shutil.which("git") is not None
        if not self._git_available:
            self._record_attempt("skipped", working_dir, reason, detail="git not found")
            return False
        abs_dir = str(_normalize_path(working_dir))
        skip_reason = _checkpoint_skip_reason(abs_dir)
        if skip_reason:
            logger.debug("Checkpoint skipped: %s (%s)", skip_reason, abs_dir)
            self._record_attempt("skipped", abs_dir, reason, detail=skip_reason)
            return False

        try:
            return self._take(abs_dir, reason, metadata=metadata, target_paths=target_paths)
        except Exception as e:
            logger.debug("Forced checkpoint failed (non-fatal): %s", e)
            self._record_attempt("failed", abs_dir, reason, detail=str(e))
            return False

    def list_checkpoints(self, working_dir: str) -> List[Dict]:
        """List available checkpoints for a directory (most recent first)."""
        abs_dir = str(_normalize_path(working_dir))
        if _checkpoint_skip_reason(abs_dir):
            return []

        store = _store_path(CHECKPOINT_BASE)

        if not (store / "HEAD").exists():
            return []

        ref = _ref_name(_project_hash(abs_dir))
        ok, stdout, _ = _run_git(
            ["log", ref, f"--format=%H|%h|%aI|%s", "-n", str(self.max_snapshots)],
            store, abs_dir,
            allowed_returncodes={128, 129},
        )

        if not ok or not stdout:
            return []

        results: List[Dict] = []
        for line in stdout.splitlines():
            parts = line.split("|", 3)
            if len(parts) == 4:
                entry = {
                    "hash": parts[0],
                    "short_hash": parts[1],
                    "timestamp": parts[2],
                    "reason": parts[3],
                    "metadata": _read_checkpoint_meta(store, parts[0]) or {},
                    "files_changed": 0,
                    "insertions": 0,
                    "deletions": 0,
                }
                stat_ok, stat_out, _ = _run_git(
                    ["diff", "--shortstat", f"{parts[0]}~1", parts[0]],
                    store, abs_dir,
                    allowed_returncodes={128, 129},
                )
                if stat_ok and stat_out:
                    self._parse_shortstat(stat_out, entry)
                results.append(entry)
        return results

    @staticmethod
    def _parse_shortstat(stat_line: str, entry: Dict) -> None:
        """Parse git --shortstat output into entry dict."""
        m = re.search(r'(\d+) file', stat_line)
        if m:
            entry["files_changed"] = int(m.group(1))
        m = re.search(r'(\d+) insertion', stat_line)
        if m:
            entry["insertions"] = int(m.group(1))
        m = re.search(r'(\d+) deletion', stat_line)
        if m:
            entry["deletions"] = int(m.group(1))

    def diff(self, working_dir: str, commit_hash: str) -> Dict:
        """Show diff between a checkpoint and the current working tree."""
        hash_err = _validate_commit_hash(commit_hash)
        if hash_err:
            return {"success": False, "error": hash_err}

        abs_dir = str(_normalize_path(working_dir))
        skip_reason = _checkpoint_skip_reason(abs_dir)
        if skip_reason:
            return {"success": False, "error": f"Checkpoints are skipped for {skip_reason}"}

        store = _store_path(CHECKPOINT_BASE)

        if not (store / "HEAD").exists():
            return {"success": False, "error": "No checkpoints exist for this directory"}

        ok, _, err = _run_git(
            ["cat-file", "-t", commit_hash], store, abs_dir,
        )
        if not ok:
            return {"success": False, "error": f"Checkpoint '{commit_hash}' not found"}
        if not _commit_belongs_to_project(store, abs_dir, commit_hash):
            return {"success": False, "error": f"Checkpoint '{commit_hash}' does not belong to this directory"}
        metadata = _read_checkpoint_meta(store, commit_hash) or {}

        dir_hash = _project_hash(abs_dir)
        index_file = _index_path(store, dir_hash)

        # Stage current state into the per-project index to compare.
        if metadata.get("targeted") and metadata.get("target_paths_rel"):
            _run_git(["read-tree", commit_hash], store, abs_dir, index_file=index_file, allowed_returncodes={128})
            for rel in metadata.get("target_paths_rel") or []:
                path = Path(abs_dir) / rel
                if path.exists():
                    _run_git(["add", "-f", "--", rel], store, abs_dir,
                             timeout=_GIT_TIMEOUT * 2, index_file=index_file)
                else:
                    _run_git(["rm", "--cached", "--ignore-unmatch", "--", rel], store, abs_dir, index_file=index_file)
        else:
            _run_git(["add", "-A"], store, abs_dir,
                     timeout=_GIT_TIMEOUT * 2, index_file=index_file)

        ok_stat, stat_out, _ = _run_git(
            ["diff", "--stat", commit_hash, "--cached"],
            store, abs_dir, index_file=index_file,
        )
        ok_diff, diff_out, _ = _run_git(
            ["diff", commit_hash, "--cached", "--no-color"],
            store, abs_dir, index_file=index_file,
        )

        # Reset staged tree back to the project's last checkpoint so the
        # index doesn't drift out of sync with the ref.
        ref = _ref_name(dir_hash)
        _run_git(["read-tree", ref], store, abs_dir,
                 index_file=index_file,
                 allowed_returncodes={128})

        if not ok_stat and not ok_diff:
            return {"success": False, "error": "Could not generate diff"}

        return {
            "success": True,
            "stat": stat_out if ok_stat else "",
            "diff": diff_out if ok_diff else "",
        }

    def restore(
        self,
        working_dir: str,
        commit_hash: str,
        file_path: str = None,
        create_pre_snapshot: bool = True,
    ) -> Dict:
        """Restore files to a checkpoint state."""
        hash_err = _validate_commit_hash(commit_hash)
        if hash_err:
            return {"success": False, "error": hash_err}

        abs_dir = str(_normalize_path(working_dir))
        skip_reason = _checkpoint_skip_reason(abs_dir)
        if skip_reason:
            return {"success": False, "error": f"Checkpoints are skipped for {skip_reason}"}

        if file_path:
            path_err = _validate_file_path(file_path, abs_dir)
            if path_err:
                return {"success": False, "error": path_err}

        store = _store_path(CHECKPOINT_BASE)

        if not (store / "HEAD").exists():
            return {"success": False, "error": "No checkpoints exist for this directory"}

        ok, _, err = _run_git(
            ["cat-file", "-t", commit_hash], store, abs_dir,
        )
        if not ok:
            return {"success": False, "error": f"Checkpoint '{commit_hash}' not found",
                    "debug": err or None}
        if not _commit_belongs_to_project(store, abs_dir, commit_hash):
            return {
                "success": False,
                "error": f"Checkpoint '{commit_hash}' does not belong to this directory",
            }
        metadata = _read_checkpoint_meta(store, commit_hash) or {}

        if create_pre_snapshot:
            # Take a pre-rollback snapshot so you can undo the undo.
            pre_target_paths = metadata.get("target_paths") if metadata.get("targeted") else None
            self._take(
                abs_dir,
                f"pre-rollback snapshot (restoring to {commit_hash[:8]})",
                metadata={"rollback_target": commit_hash},
                target_paths=pre_target_paths,
            )

        dir_hash = _project_hash(abs_dir)
        index_file = _index_path(store, dir_hash)

        missing_at_checkpoint = set(metadata.get("missing_at_checkpoint") or [])
        targeted_paths = list(metadata.get("target_paths_rel") or [])
        restore_targets = [file_path] if file_path else (targeted_paths if metadata.get("targeted") else ["."])
        ok = True
        stdout = ""
        err = ""
        for restore_target in restore_targets:
            if restore_target == "." and missing_at_checkpoint:
                ok_tree, tree_files, _ = _run_git(
                    ["ls-tree", "-r", "--name-only", commit_hash],
                    store,
                    abs_dir,
                )
                if ok_tree and not tree_files.strip():
                    continue
            if restore_target in missing_at_checkpoint:
                target = (Path(abs_dir) / restore_target).resolve()
                try:
                    if target.is_file() or target.is_symlink():
                        target.unlink()
                    elif target.is_dir():
                        shutil.rmtree(target)
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    ok = False
                    err = str(exc)
                    break
                continue
            ok, stdout, err = _run_git(
                ["checkout", commit_hash, "--", restore_target],
                store, abs_dir, timeout=_GIT_TIMEOUT * 2,
                index_file=index_file,
            )
            if not ok:
                break

        if not ok:
            return {"success": False, "error": f"Restore failed: {err}",
                    "debug": err or None}
        if not metadata.get("targeted") and not file_path:
            for rel in missing_at_checkpoint:
                target = (Path(abs_dir) / rel).resolve()
                try:
                    if target.is_file() or target.is_symlink():
                        target.unlink()
                    elif target.is_dir():
                        shutil.rmtree(target)
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    return {"success": False, "error": f"Restore cleanup failed: {exc}", "debug": str(exc)}

        ok2, reason_out, _ = _run_git(
            ["log", "--format=%s", "-1", commit_hash], store, abs_dir,
        )
        reason = reason_out if ok2 else "unknown"

        result = {
            "success": True,
            "restored_to": commit_hash[:8],
            "restored_hash": commit_hash,
            "reason": reason,
            "directory": abs_dir,
            "metadata": metadata,
        }
        if file_path:
            result["file"] = file_path
        return result

    def get_working_dir_for_path(self, file_path: str) -> str:
        """Resolve a file path to its working directory for checkpointing."""
        path = _normalize_path(file_path)
        if path.is_dir():
            candidate = path
        else:
            candidate = path.parent

        markers = {".git", "pyproject.toml", "package.json", "Cargo.toml",
                    "go.mod", "Makefile", "pom.xml", ".hg", "Gemfile"}
        check = candidate
        while check != check.parent:
            if any((check / m).exists() for m in markers):
                return str(check)
            check = check.parent

        return str(candidate)

    def _record_attempt(self, status: str, working_dir: str, reason: str, **extra) -> None:
        self.last_attempt = {
            "status": status,
            "working_dir": str(_normalize_path(working_dir)) if working_dir else "",
            "reason": reason,
            "timestamp": time.time(),
            **extra,
        }

    def _target_info(self, working_dir: str, target_paths: Optional[List[str]]) -> Dict:
        if not target_paths:
            return {"rel": [], "abs": [], "missing": [], "oversize": []}
        abs_dir = _normalize_path(working_dir)
        rel_paths: List[str] = []
        abs_paths: List[str] = []
        missing: List[str] = []
        oversize: List[str] = []
        seen: Set[str] = set()
        for item in target_paths:
            if not item:
                continue
            path = _normalize_path(str(item))
            try:
                rel = path.relative_to(abs_dir)
            except ValueError:
                continue
            rel_s = rel.as_posix()
            if rel_s in seen:
                continue
            seen.add(rel_s)
            rel_paths.append(rel_s)
            abs_paths.append(str(path))
            if not path.exists():
                missing.append(rel_s)
                continue
            if path.is_file() and self.max_file_size_mb > 0:
                try:
                    if path.stat().st_size > self.max_file_size_mb * 1024 * 1024:
                        oversize.append(rel_s)
                except OSError:
                    oversize.append(rel_s)
        return {"rel": rel_paths, "abs": abs_paths, "missing": missing, "oversize": oversize}

    def _metadata_with_targets(
        self,
        working_dir: str,
        metadata: Optional[Dict],
        target_info: Dict,
        *,
        targeted: bool,
    ) -> Dict:
        enriched = dict(metadata or {})
        if target_info.get("rel"):
            previous_targets = set(enriched.pop("_previous_target_paths_rel", []) or [])
            previous_missing = set(enriched.pop("_previous_missing_targets_rel", []) or [])
            target_rel = sorted(previous_targets | set(target_info.get("rel", [])))
            target_abs = [str((Path(_normalize_path(working_dir)) / rel).resolve()) for rel in target_rel]
            missing_rel = sorted(previous_missing | set(target_info.get("missing", [])))
            enriched.update({
                "checkpoint_work_dir": str(_normalize_path(working_dir)),
                "targeted": targeted,
                "target_paths": target_abs,
                "target_paths_rel": target_rel,
                "missing_at_checkpoint": missing_rel,
                "oversize_at_checkpoint": target_info.get("oversize", []),
            })
        return enriched

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _take(
        self,
        working_dir: str,
        reason: str,
        metadata: Optional[Dict] = None,
        target_paths: Optional[List[str]] = None,
    ) -> bool:
        """Take a snapshot.  Returns True on success."""
        store = _store_path(CHECKPOINT_BASE)

        err = _init_store(store)
        if err:
            logger.debug("Checkpoint store init failed: %s", err)
            return False

        _touch_project(store, working_dir)
        target_info = self._target_info(working_dir, target_paths)
        too_many_files = _dir_file_count(working_dir) > _MAX_FILES

        # Quick size guard — don't try to snapshot enormous directories
        if too_many_files:
            if target_info.get("rel"):
                targeted_metadata = self._metadata_with_targets(
                    working_dir,
                    metadata,
                    target_info,
                    targeted=True,
                )
                return self._take_targeted(store, working_dir, reason, targeted_metadata, target_info)
            logger.debug("Checkpoint skipped: >%d files in %s", _MAX_FILES, working_dir)
            self._record_attempt("skipped", working_dir, reason, detail=f"directory has more than {_MAX_FILES} files")
            return False

        metadata = self._metadata_with_targets(
            working_dir,
            metadata,
            target_info,
            targeted=False,
        )

        dir_hash = _project_hash(working_dir)
        index_file = _index_path(store, dir_hash)
        ref = _ref_name(dir_hash)

        # Seed the per-project index from the last checkpoint, if any, so the
        # diff/commit machinery sees only changes since then.  On first call,
        # clear the index so ``git add -A`` produces a clean tree.
        if index_file.exists():
            # Reset index to current ref tip to avoid accumulating stale paths.
            ok_ref, ref_commit, _ = _run_git(
                ["rev-parse", "--verify", ref + "^{commit}"],
                store, working_dir,
                allowed_returncodes={128},
            )
            if ok_ref and ref_commit:
                _run_git(
                    ["read-tree", ref_commit],
                    store, working_dir,
                    index_file=index_file,
                    allowed_returncodes={128},
                )
            else:
                try:
                    index_file.unlink()
                except OSError:
                    pass
        else:
            # First snapshot for this project.
            index_file.parent.mkdir(parents=True, exist_ok=True)

        # Stage with per-project index.  Include a per-stage file-size filter
        # via ``core.bigFileThreshold`` is not what we want — instead, we
        # rely on the exclude file for broad patterns and post-stage prune
        # any path whose size exceeds max_file_size_mb.
        ok, _, err = _run_git(
            ["add", "-A"], store, working_dir,
            timeout=_GIT_TIMEOUT * 2, index_file=index_file,
        )
        if not ok:
            logger.debug("Checkpoint git-add failed: %s", err)
            return False

        if self.max_file_size_mb > 0:
            self._drop_oversize_from_index(store, working_dir, index_file)
        if target_info.get("rel"):
            self._stage_explicit_targets(store, working_dir, index_file, target_info)

        # Compare against the current ref tip (not HEAD — HEAD points to a
        # branch that doesn't exist on a bare store, so ``diff --cached``
        # against HEAD would always show "new file" for every staged path).
        ok_ref, ref_commit, _ = _run_git(
            ["rev-parse", "--verify", ref + "^{commit}"],
            store, working_dir,
            allowed_returncodes={128},
        )
        has_ref = ok_ref and bool(ref_commit)

        if has_ref:
            ok_diff, _, _ = _run_git(
                ["diff-index", "--cached", "--quiet", ref_commit],
                store, working_dir,
                allowed_returncodes={1},
                index_file=index_file,
            )
            if ok_diff and not target_info.get("rel"):
                logger.debug("Checkpoint skipped: no changes in %s", working_dir)
                self._record_attempt("skipped", working_dir, reason, detail="no changes")
                return False
        else:
            # No ref yet — skip only if the index is empty.
            ok_ls, ls_out, _ = _run_git(
                ["ls-files", "--cached"],
                store, working_dir,
                index_file=index_file,
            )
            if ok_ls and not ls_out.strip() and not target_info.get("rel"):
                logger.debug("Checkpoint skipped: empty tree in %s", working_dir)
                self._record_attempt("skipped", working_dir, reason, detail="empty tree")
                return False

        # Write tree from per-project index.
        ok_tree, tree_sha, err = _run_git(
            ["write-tree"], store, working_dir,
            index_file=index_file,
        )
        if not ok_tree or not tree_sha:
            logger.debug("Checkpoint write-tree failed: %s", err)
            self._record_attempt("failed", working_dir, reason, detail=err or "write-tree failed")
            return False

        # Build commit (parent = current ref tip, if any).
        commit_args = ["commit-tree", tree_sha, "-m", reason, "--no-gpg-sign"]
        if has_ref:
            commit_args = ["commit-tree", tree_sha, "-p", ref_commit, "-m", reason, "--no-gpg-sign"]
        ok_commit, new_sha, err = _run_git(
            commit_args, store, working_dir,
            index_file=index_file,
        )
        if not ok_commit or not new_sha:
            logger.debug("Checkpoint commit-tree failed: %s", err)
            self._record_attempt("failed", working_dir, reason, detail=err or "commit-tree failed")
            return False

        # Update the per-project ref.
        update_args = ["update-ref", ref, new_sha]
        if has_ref:
            update_args = ["update-ref", ref, new_sha, ref_commit]
        ok_update, _, err = _run_git(
            update_args, store, working_dir,
        )
        if not ok_update:
            logger.debug("Checkpoint update-ref failed: %s", err)
            self._record_attempt("failed", working_dir, reason, detail=err or "update-ref failed")
            return False

        _write_checkpoint_meta(store, new_sha, metadata)
        self._record_attempt(
            "taken",
            working_dir,
            reason,
            commit=new_sha,
            metadata=metadata,
            target_paths=target_info.get("abs", []),
        )

        logger.debug("Checkpoint taken in %s: %s (%s)", working_dir, reason, new_sha[:8])

        # Real pruning — drop old commits beyond max_snapshots.
        self._prune(store, working_dir, ref)

        # Enforce global size cap.
        self._enforce_size_cap(store)

        return True

    def _stage_explicit_targets(
        self,
        store: Path,
        working_dir: str,
        index_file: Path,
        target_info: Dict,
    ) -> None:
        for rel in target_info.get("rel", []):
            if rel in target_info.get("oversize", []):
                _run_git(["rm", "--cached", "--ignore-unmatch", "--", rel], store, working_dir, index_file=index_file)
                continue
            if rel in target_info.get("missing", []):
                _run_git(["rm", "--cached", "--ignore-unmatch", "--", rel], store, working_dir, index_file=index_file)
                continue
            _run_git(["add", "-f", "--", rel], store, working_dir, timeout=_GIT_TIMEOUT * 2, index_file=index_file)

    def _take_targeted(
        self,
        store: Path,
        working_dir: str,
        reason: str,
        metadata: Dict,
        target_info: Dict,
    ) -> bool:
        rel_paths = [p for p in target_info.get("rel", []) if p not in target_info.get("oversize", [])]
        if not rel_paths:
            self._record_attempt("skipped", working_dir, reason, detail="no target files eligible for checkpoint")
            return False

        dir_hash = _project_hash(working_dir)
        index_file = _index_path(store, dir_hash)
        ref = _ref_name(dir_hash)
        index_file.parent.mkdir(parents=True, exist_ok=True)

        ok_ref, ref_commit, _ = _run_git(
            ["rev-parse", "--verify", ref + "^{commit}"],
            store,
            working_dir,
            allowed_returncodes={128},
        )
        has_ref = ok_ref and bool(ref_commit)
        if has_ref:
            _run_git(["read-tree", ref_commit], store, working_dir, index_file=index_file, allowed_returncodes={128})
        elif index_file.exists():
            try:
                index_file.unlink()
            except OSError:
                pass

        self._stage_explicit_targets(store, working_dir, index_file, target_info)

        if has_ref:
            ok_diff, _, _ = _run_git(
                ["diff-index", "--cached", "--quiet", ref_commit],
                store,
                working_dir,
                allowed_returncodes={1},
                index_file=index_file,
            )
            if ok_diff and not rel_paths:
                self._record_attempt("skipped", working_dir, reason, detail="no changes")
                return False

        ok_tree, tree_sha, err = _run_git(["write-tree"], store, working_dir, index_file=index_file)
        if not ok_tree or not tree_sha:
            self._record_attempt("failed", working_dir, reason, detail=err or "write-tree failed")
            return False

        commit_args = ["commit-tree", tree_sha, "-m", reason, "--no-gpg-sign"]
        if has_ref:
            commit_args = ["commit-tree", tree_sha, "-p", ref_commit, "-m", reason, "--no-gpg-sign"]
        ok_commit, new_sha, err = _run_git(commit_args, store, working_dir, index_file=index_file)
        if not ok_commit or not new_sha:
            self._record_attempt("failed", working_dir, reason, detail=err or "commit-tree failed")
            return False

        update_args = ["update-ref", ref, new_sha]
        if has_ref:
            update_args = ["update-ref", ref, new_sha, ref_commit]
        ok_update, _, err = _run_git(update_args, store, working_dir)
        if not ok_update:
            self._record_attempt("failed", working_dir, reason, detail=err or "update-ref failed")
            return False

        _write_checkpoint_meta(store, new_sha, metadata)
        self._record_attempt(
            "taken",
            working_dir,
            reason,
            commit=new_sha,
            metadata=metadata,
            target_paths=target_info.get("abs", []),
        )
        self._prune(store, working_dir, ref)
        self._enforce_size_cap(store)
        return True

    def _drop_oversize_from_index(
        self, store: Path, working_dir: str, index_file: Path,
    ) -> None:
        """Remove any staged file larger than ``max_file_size_mb`` from the index.

        Lets the agent keep snapshotting source code while refusing to
        swallow generated assets (datasets, model weights, logs, videos).
        """
        cap = self.max_file_size_mb * 1024 * 1024
        if cap <= 0:
            return
        ok, stdout, _ = _run_git(
            ["ls-files", "--cached", "-z"],
            store, working_dir, index_file=index_file,
        )
        if not ok or not stdout:
            return
        # ls-files -z output is NUL-separated. _run_git strips trailing
        # whitespace but that leaves NULs alone; rebuild list.
        paths = [p for p in stdout.split("\x00") if p]
        abs_workdir = _normalize_path(working_dir)
        oversize: List[str] = []
        for rel in paths:
            try:
                size = (abs_workdir / rel).stat().st_size
            except OSError:
                continue
            if size > cap:
                oversize.append(rel)
        if not oversize:
            return
        logger.debug(
            "Checkpoint: dropping %d oversize file(s) (>%d MB) from index",
            len(oversize), self.max_file_size_mb,
        )
        # Use --pathspec-from-file for safety with many paths.
        # Chunk into manageable batches.
        BATCH = 200
        for i in range(0, len(oversize), BATCH):
            chunk = oversize[i:i + BATCH]
            _run_git(
                ["rm", "--cached", "--quiet", "--"] + chunk,
                store, working_dir, index_file=index_file,
                allowed_returncodes={128},
            )

    def _prune(self, store: Path, working_dir: str, ref: str) -> None:
        """Keep only the last ``max_snapshots`` commits on the per-project ref.

        The ref is rewritten to drop commits older than ``max_snapshots`` and
        then ``git gc`` reclaims unreachable objects.
        """
        ok, stdout, _ = _run_git(
            ["rev-list", "--count", ref], store, working_dir,
            allowed_returncodes={128},
        )
        if not ok:
            return
        try:
            count = int(stdout)
        except ValueError:
            return
        if count <= self.max_snapshots:
            return

        # Collect commits oldest → newest, take last N.
        ok_list, list_out, _ = _run_git(
            ["rev-list", "--reverse", ref], store, working_dir,
        )
        if not ok_list or not list_out:
            return
        commits = list_out.splitlines()
        keep = commits[-self.max_snapshots:]

        # Rebuild a linear chain off keep[0]'s tree.
        new_parent: Optional[str] = None
        for sha in keep:
            ok_tree, tree_sha, _ = _run_git(
                ["rev-parse", f"{sha}^{{tree}}"], store, working_dir,
            )
            if not ok_tree or not tree_sha:
                return
            ok_msg, msg, _ = _run_git(
                ["log", "--format=%s", "-1", sha], store, working_dir,
            )
            commit_msg = msg if ok_msg and msg else "checkpoint"
            args = ["commit-tree", tree_sha, "-m", commit_msg, "--no-gpg-sign"]
            if new_parent is not None:
                args = ["commit-tree", tree_sha, "-p", new_parent,
                        "-m", commit_msg, "--no-gpg-sign"]
            ok_commit, new_sha, _ = _run_git(args, store, working_dir)
            if not ok_commit or not new_sha:
                return
            new_parent = new_sha

        if new_parent is None:
            return
        _run_git(["update-ref", ref, new_parent], store, working_dir)

        # Reclaim objects from the dropped commits.
        _run_git(
            ["reflog", "expire", "--expire=now", "--all"],
            store, working_dir,
        )
        _run_git(
            ["gc", "--prune=now", "--quiet"],
            store, working_dir, timeout=_GIT_TIMEOUT * 3,
        )

    def _enforce_size_cap(self, store: Path) -> None:
        """If total store size exceeds ``max_total_size_mb``, drop oldest
        checkpoints across ALL projects until under the cap.
        """
        if self.max_total_size_mb <= 0:
            return
        cap_bytes = self.max_total_size_mb * 1024 * 1024
        size = _dir_size_bytes(store)
        if size <= cap_bytes:
            return
        logger.info(
            "Checkpoint store exceeded %d MB (actual %d MB) — pruning oldest",
            self.max_total_size_mb, size // (1024 * 1024),
        )

        # Collect (commit_time, ref, sha) across all per-project refs.
        ok, stdout, _ = _run_git(
            ["for-each-ref", "--format=%(refname)", _REFS_PREFIX],
            store, str(store.parent),
            allowed_returncodes={128},
        )
        if not ok or not stdout:
            return
        refs = [r for r in stdout.splitlines() if r.strip()]

        any_dropped = False
        # Round-robin-drop oldest commit per ref until under cap.
        for _ in range(20):  # hard upper bound to avoid pathological loops
            size = _dir_size_bytes(store)
            if size <= cap_bytes:
                break
            for ref in refs:
                ok_count, count_out, _ = _run_git(
                    ["rev-list", "--count", ref], store, str(store.parent),
                    allowed_returncodes={128},
                )
                try:
                    count = int(count_out) if ok_count else 0
                except ValueError:
                    count = 0
                if count <= 1:
                    continue  # keep at least one snapshot per project
                ok_list, list_out, _ = _run_git(
                    ["rev-list", "--reverse", ref], store, str(store.parent),
                )
                if not ok_list or not list_out:
                    continue
                commits = list_out.splitlines()
                keep = commits[1:]  # drop oldest
                new_parent: Optional[str] = None
                fail = False
                for sha in keep:
                    ok_tree, tree_sha, _ = _run_git(
                        ["rev-parse", f"{sha}^{{tree}}"], store, str(store.parent),
                    )
                    if not ok_tree or not tree_sha:
                        fail = True
                        break
                    ok_msg, msg, _ = _run_git(
                        ["log", "--format=%s", "-1", sha], store, str(store.parent),
                    )
                    commit_msg = msg if ok_msg and msg else "checkpoint"
                    args = ["commit-tree", tree_sha, "-m", commit_msg, "--no-gpg-sign"]
                    if new_parent is not None:
                        args = ["commit-tree", tree_sha, "-p", new_parent,
                                "-m", commit_msg, "--no-gpg-sign"]
                    ok_commit, new_sha, _ = _run_git(args, store, str(store.parent))
                    if not ok_commit or not new_sha:
                        fail = True
                        break
                    new_parent = new_sha
                if fail or new_parent is None:
                    continue
                _run_git(["update-ref", ref, new_parent], store, str(store.parent))
                any_dropped = True
            if not any_dropped:
                break

        _run_git(
            ["reflog", "expire", "--expire=now", "--all"],
            store, str(store.parent),
        )
        _run_git(
            ["gc", "--prune=now", "--quiet"],
            store, str(store.parent), timeout=_GIT_TIMEOUT * 3,
        )


def format_checkpoint_list(checkpoints: List[Dict], directory: str) -> str:
    """Format checkpoint list for display to user."""
    if not checkpoints:
        return f"No checkpoints found for {directory}"

    lines = [f"📸 Checkpoints for {directory}:\n"]
    for i, cp in enumerate(checkpoints, 1):
        ts = cp["timestamp"]
        if "T" in ts:
            ts = ts.split("T")[1].split("+")[0].split("-")[0][:5]
            date = cp["timestamp"].split("T")[0]
            ts = f"{date} {ts}"

        files = cp.get("files_changed", 0)
        ins = cp.get("insertions", 0)
        dele = cp.get("deletions", 0)
        if files:
            stat = f"  ({files} file{'s' if files != 1 else ''}, +{ins}/-{dele})"
        else:
            stat = ""

        lines.append(f"  {i}. {cp['short_hash']}  {ts}  {cp['reason']}{stat}")

    lines.append("\n  /rollback <N>             restore to checkpoint N")
    lines.append("  /rollback diff <N>        preview changes since checkpoint N")
    lines.append("  /rollback <N> <file>      restore a single file from checkpoint N")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Auto-maintenance
# ---------------------------------------------------------------------------

_PRUNE_MARKER_NAME = ".last_prune"


def _delete_ref(store: Path, ref: str) -> bool:
    """Delete a ref from the store.  Returns True on success."""
    ok, _, _ = _run_git(
        ["update-ref", "-d", ref], store, str(store.parent),
        allowed_returncodes={128},
    )
    return ok


def prune_checkpoints(
    retention_days: int = 7,
    delete_orphans: bool = True,
    checkpoint_base: Optional[Path] = None,
    max_total_size_mb: int = 0,
) -> Dict[str, int]:
    """Delete stale/orphan checkpoints and reclaim store space.

    A project entry is deleted when either:

    * ``delete_orphans=True`` and its ``workdir`` no longer exists on disk
      (the original project was deleted / moved); OR
    * its ``last_touch`` is older than ``retention_days`` days.

    Additionally, if ``max_total_size_mb > 0`` and the store exceeds that
    after orphan/stale pruning, the oldest commit per remaining project is
    dropped until the store is under the cap.

    Returns a dict with counts ``{"scanned", "deleted_orphan",
    "deleted_stale", "errors", "bytes_freed"}``.

    Never raises — maintenance must never block interactive startup.
    """
    base = checkpoint_base or CHECKPOINT_BASE
    result = {
        "scanned": 0,
        "deleted_orphan": 0,
        "deleted_stale": 0,
        "errors": 0,
        "bytes_freed": 0,
    }
    if not base.exists():
        return result

    size_before = _dir_size_bytes(base)

    cutoff = 0.0
    if retention_days > 0:
        cutoff = time.time() - retention_days * 86400

    store = _store_path(base)
    if (store / "HEAD").exists():
        for meta in _list_projects(store):
            dir_hash = meta.get("_hash") or ""
            workdir = meta.get("workdir") or ""
            if not dir_hash:
                continue
            result["scanned"] += 1
            reason = None
            if delete_orphans and (not workdir or not Path(workdir).exists()):
                reason = "orphan"
            elif retention_days > 0:
                last_touch = float(meta.get("last_touch", 0) or 0)
                if last_touch > 0 and last_touch < cutoff:
                    reason = "stale"
            if reason is None:
                continue
            ref = _ref_name(dir_hash)
            _delete_ref(store, ref)
            # Drop per-project index and metadata.
            try:
                idx = _index_path(store, dir_hash)
                if idx.exists():
                    idx.unlink()
            except OSError:
                pass
            try:
                mp = _project_meta_path(store, dir_hash)
                if mp.exists():
                    mp.unlink()
            except OSError:
                pass
            if reason == "orphan":
                result["deleted_orphan"] += 1
            else:
                result["deleted_stale"] += 1

        # GC the store to reclaim unreachable objects from dropped refs.
        _run_git(
            ["reflog", "expire", "--expire=now", "--all"],
            store, str(base),
        )
        _run_git(
            ["gc", "--prune=now", "--quiet"],
            store, str(base), timeout=_GIT_TIMEOUT * 3,
        )

        # Size-cap pass across remaining projects.
        if max_total_size_mb > 0:
            cap_bytes = max_total_size_mb * 1024 * 1024
            for _i in range(20):
                size = _dir_size_bytes(store)
                if size <= cap_bytes:
                    break
                ok, stdout, _ = _run_git(
                    ["for-each-ref", "--format=%(refname)", _REFS_PREFIX],
                    store, str(base),
                    allowed_returncodes={128},
                )
                refs = [r for r in stdout.splitlines() if r.strip()] if ok else []
                if not refs:
                    break
                any_drop = False
                for ref in refs:
                    ok_c, count_out, _ = _run_git(
                        ["rev-list", "--count", ref], store, str(base),
                        allowed_returncodes={128},
                    )
                    try:
                        count = int(count_out) if ok_c else 0
                    except ValueError:
                        count = 0
                    if count <= 1:
                        continue
                    ok_l, lo, _ = _run_git(
                        ["rev-list", "--reverse", ref], store, str(base),
                    )
                    if not ok_l or not lo:
                        continue
                    commits = lo.splitlines()
                    keep = commits[1:]
                    new_parent: Optional[str] = None
                    fail = False
                    for sha in keep:
                        ok_t, tsha, _ = _run_git(
                            ["rev-parse", f"{sha}^{{tree}}"], store, str(base),
                        )
                        if not ok_t or not tsha:
                            fail = True
                            break
                        ok_m, m, _ = _run_git(
                            ["log", "--format=%s", "-1", sha], store, str(base),
                        )
                        msg = m if ok_m and m else "checkpoint"
                        args = ["commit-tree", tsha, "-m", msg, "--no-gpg-sign"]
                        if new_parent is not None:
                            args = ["commit-tree", tsha, "-p", new_parent,
                                    "-m", msg, "--no-gpg-sign"]
                        ok_cm, new_sha, _ = _run_git(args, store, str(base))
                        if not ok_cm or not new_sha:
                            fail = True
                            break
                        new_parent = new_sha
                    if fail or new_parent is None:
                        continue
                    _run_git(["update-ref", ref, new_parent], store, str(base))
                    any_drop = True
                if not any_drop:
                    break
            _run_git(
                ["reflog", "expire", "--expire=now", "--all"],
                store, str(base),
            )
            _run_git(
                ["gc", "--prune=now", "--quiet"],
                store, str(base), timeout=_GIT_TIMEOUT * 3,
            )

    size_after = _dir_size_bytes(base)
    delta = size_before - size_after
    result["bytes_freed"] = max(result["bytes_freed"], delta)

    return result


def maybe_auto_prune_checkpoints(
    retention_days: int = 7,
    min_interval_hours: int = 24,
    delete_orphans: bool = True,
    checkpoint_base: Optional[Path] = None,
    max_total_size_mb: int = 0,
) -> Dict[str, object]:
    """Idempotent wrapper around ``prune_checkpoints`` for startup hooks.

    Writes ``CHECKPOINT_BASE/.last_prune`` on completion so subsequent
    calls within ``min_interval_hours`` short-circuit.

    Returns ``{"skipped": bool, "result": prune_checkpoints-dict,
    "error": optional str}``.
    """
    base = checkpoint_base or CHECKPOINT_BASE
    out: Dict[str, object] = {"skipped": False}

    try:
        if not base.exists():
            out["result"] = {
                "scanned": 0, "deleted_orphan": 0, "deleted_stale": 0,
                "errors": 0, "bytes_freed": 0,
            }
            return out

        marker = base / _PRUNE_MARKER_NAME
        now = time.time()
        if marker.exists():
            try:
                last_ts = float(marker.read_text(encoding="utf-8").strip())
                if now - last_ts < min_interval_hours * 3600:
                    out["skipped"] = True
                    return out
            except (OSError, ValueError):
                pass  # corrupt marker — treat as no prior run

        result = prune_checkpoints(
            retention_days=retention_days,
            delete_orphans=delete_orphans,
            checkpoint_base=base,
            max_total_size_mb=max_total_size_mb,
        )
        out["result"] = result

        try:
            marker.write_text(str(now), encoding="utf-8")
        except OSError as exc:
            logger.debug("Could not write checkpoint prune marker: %s", exc)

        total = result["deleted_orphan"] + result["deleted_stale"]
        if total > 0:
            logger.info(
                "checkpoint auto-maintenance: pruned %d entry(ies) "
                "(%d orphan, %d stale), reclaimed %.1f MB",
                total,
                result["deleted_orphan"],
                result["deleted_stale"],
                result["bytes_freed"] / (1024 * 1024),
            )
    except Exception as exc:
        logger.warning("checkpoint auto-maintenance failed: %s", exc)
        out["error"] = str(exc)

    return out


# ---------------------------------------------------------------------------
# Public helpers for `mclaw checkpoints` CLI
# ---------------------------------------------------------------------------

def store_status(checkpoint_base: Optional[Path] = None) -> Dict:
    """Return a summary of the shadow store.

    ``{"base": path, "store_size_bytes": N, "total_size_bytes": N,
       "project_count": N, "projects": [...]}``
    """
    base = checkpoint_base or CHECKPOINT_BASE
    out: Dict = {
        "base": str(base),
        "store_size_bytes": 0,
        "total_size_bytes": 0,
        "project_count": 0,
        "projects": [],
    }
    if not base.exists():
        return out

    store = _store_path(base)
    if store.exists():
        out["store_size_bytes"] = _dir_size_bytes(store)
        if (store / "HEAD").exists():
            for meta in _list_projects(store):
                dir_hash = meta.get("_hash") or ""
                workdir = meta.get("workdir") or ""
                ref = _ref_name(dir_hash)
                ok, count_out, _ = _run_git(
                    ["rev-list", "--count", ref], store, str(base),
                    allowed_returncodes={128},
                )
                try:
                    commits = int(count_out) if ok else 0
                except ValueError:
                    commits = 0
                out["projects"].append({
                    "hash": dir_hash,
                    "workdir": workdir,
                    "exists": bool(workdir) and Path(workdir).exists(),
                    "created_at": meta.get("created_at"),
                    "last_touch": meta.get("last_touch"),
                    "commits": commits,
                })
    out["project_count"] = len(out["projects"])

    out["total_size_bytes"] = _dir_size_bytes(base)
    return out


def clear_all(checkpoint_base: Optional[Path] = None) -> Dict[str, int]:
    """Nuke the entire checkpoint base. Irreversible.

    Returns ``{"bytes_freed": N, "deleted": bool}``.
    """
    base = checkpoint_base or CHECKPOINT_BASE
    out = {"bytes_freed": 0, "deleted": False}
    if not base.exists():
        return out
    size = _dir_size_bytes(base)
    try:
        shutil.rmtree(base)
        out["bytes_freed"] = size
        out["deleted"] = True
    except OSError as exc:
        logger.warning("Could not clear checkpoint base %s: %s", base, exc)
    return out
