# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Checkpoint Manager — Transparent filesystem snapshots via a single shared
shadow git store.

Creates automatic snapshots of working directories before file-mutating
operations (``write_file``, ``patch``, ``terminal`` with destructive flags),
deduplicated within each tool-dispatch batch. The agent resets this scope
before executing the next model-emitted batch. Provides checkpoint restoration.

This is NOT a tool — the LLM never sees it.  It is transparent runtime
infrastructure controlled by the ``checkpoints`` configuration.

Storage layout (single shared store, git objects deduplicated across projects)
-----------------------------------------------------------------------------

    M-Claw home checkpoints/
        store/                          — single bare-ish git repo
            HEAD, config, objects/      — standard git internals (shared)
            refs/mclaw/<hash16>         — per-project latest snapshot
            refs/mclaw-snapshots/       — immutable per-snapshot refs
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
import threading
import time
import uuid
from pathlib import Path
from mclaw.constants import get_mclaw_home
from mclaw.runtime.manager import RuntimeManager
from mclaw.runtime.process import run_captured_process
from mclaw.tools.cancellation import cancellation_checkpoint
from mclaw.tools.interrupt import get_interrupt_event
from mclaw.utils import atomic_json_write
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CHECKPOINT_BASE = get_mclaw_home() / "checkpoints"

# Single shared store directory under CHECKPOINT_BASE.
_STORE_DIRNAME = "store"
_REFS_PREFIX = "refs/mclaw"
_SNAPSHOT_REFS_PREFIX = "refs/mclaw-snapshots"
_INDEXES_DIRNAME = "indexes"
_PROJECTS_DIRNAME = "projects"
_META_DIRNAME = "meta"
_RECOVERY_DIRNAME = "recovery"
# Bump when PathPolicy's credential matching expands so existing stores are
# scanned once under the new rules.
_CREDENTIAL_SANITIZE_MARKER_NAME = ".credentials-sanitized-v1"
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
    # Worktree sibling directories are outside snapshot scope.
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
    "**/.ssh/id_rsa",
    "**/.ssh/id_dsa",
    "**/.ssh/id_ecdsa",
    "**/.ssh/id_ed25519",
    "**/.aws/credentials",
    "**/.kube/config",
    "**/.docker/config.json",
    ".git-credentials",
    ".netrc",
    ".npmrc",
    ".pypirc",
    # OS junk
    ".DS_Store",
    "Thumbs.db",
    # Logs
    "*.log",
]

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


# Git subprocess timeout (seconds).
_GIT_TIMEOUT: int = max(10, min(60, _env_int("MCLAW_CHECKPOINT_TIMEOUT", 30)))

# Max files to snapshot — skip huge directories to avoid slowdowns.
_MAX_FILES = 50_000

# Valid git commit hash pattern: 4-64 hex chars (short or full SHA-1/SHA-256).
_COMMIT_HASH_RE = re.compile(r'^[0-9a-fA-F]{4,64}$')


# ---------------------------------------------------------------------------
# Input validation helpers
# ---------------------------------------------------------------------------

def _validate_commit_hash(commit_hash: str) -> Optional[str]:
    """Validate a commit hash to prevent git argument injection.

    Returns an error string if invalid, None if valid.
    Values starting with '-' are git option tokens such as '--patch' or '-p',
    not revision specifiers.
    """
    if not commit_hash or not commit_hash.strip():
        return "Empty commit hash"
    if commit_hash.startswith("-"):
        return f"Invalid commit hash (must not start with '-'): {commit_hash!r}"
    if not _COMMIT_HASH_RE.match(commit_hash):
        return f"Invalid commit hash (expected 4-64 hex characters): {commit_hash!r}"
    return None


def _normalize_restore_targets(working_dir: str, target_paths: List[str]) -> Tuple[List[str], Optional[str]]:
    """Return literal, relative restore targets that stay inside working_dir."""
    try:
        root = _normalize_path(working_dir)
    except (OSError, RuntimeError) as exc:
        return [], f"Could not resolve restore workspace: {exc}"
    normalized: List[str] = []
    seen: Set[str] = set()
    restores_all = False
    for item in target_paths:
        raw = str(item or "").strip()
        if not raw:
            return [], "Empty restore target"
        path = Path(raw).expanduser()
        try:
            resolved = path.resolve() if path.is_absolute() else (root / path).resolve()
            relative = resolved.relative_to(root)
        except (OSError, RuntimeError, ValueError):
            return [], f"Restore target escapes the working directory: {raw!r}"
        value = relative.as_posix() or "."
        if value == ".":
            restores_all = True
            continue
        if value not in seen:
            normalized.append(value)
            seen.add(value)
    if restores_all:
        return ["."], None
    if not normalized:
        return [], "No restore targets were provided"
    return normalized, None


def _restore_target_contains(target: str, relative_path: str) -> bool:
    target = target.rstrip("/")
    return target == "." or relative_path == target or relative_path.startswith(f"{target}/")


def _restore_policy_error(checks: List[Tuple[str, str]]) -> Optional[str]:
    """Fail closed when Runtime PathPolicy rejects or cannot validate a restore."""
    try:
        policy = RuntimeManager.current().paths
        for action, path_value in checks:
            decision = policy.check(action, path_value)
            if not decision.allowed:
                return decision.error_message()
    except Exception as exc:
        return f"PathPolicy could not validate restore targets: {exc}"
    return None


# ---------------------------------------------------------------------------
# Path / hash helpers
# ---------------------------------------------------------------------------

def _normalize_path(path_value: str) -> Path:
    """Return a canonical absolute path for checkpoint operations."""
    return Path(path_value).expanduser().resolve()


def _path_is_relative_to(path: Path, root: Path) -> bool:
    """Compatibility helper for proving a path stays inside a root."""
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
        (get_mclaw_home(), "M-Claw runtime directory"),
    ]

    for root, reason in roots:
        try:
            normalized_root = root.expanduser().resolve()
        except Exception:
            normalized_root = root.expanduser()
        if _path_is_relative_to(path, normalized_root):
            return reason
        if _path_is_relative_to(normalized_root, path):
            return f"workspace contains {reason}"
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


def _snapshot_ref_prefix(dir_hash: str) -> str:
    return f"{_SNAPSHOT_REFS_PREFIX}/{dir_hash}"


def _snapshot_ref_name(dir_hash: str, commit_hash: str) -> str:
    return f"{_snapshot_ref_prefix(dir_hash)}/{commit_hash}"


def _project_meta_path(store: Path, dir_hash: str) -> Path:
    return store / _PROJECTS_DIRNAME / f"{dir_hash}.json"


def _checkpoint_meta_path(store: Path, commit_hash: str) -> Path:
    return store / _META_DIRNAME / f"{commit_hash}.json"


def _write_checkpoint_meta(store: Path, commit_hash: str, metadata: Optional[Dict]) -> None:
    """Persist optional runtime metadata beside the shadow commit."""
    try:
        meta_path = _checkpoint_meta_path(store, commit_hash)
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta = dict(metadata or {})
        meta.setdefault("commit", commit_hash)
        meta.setdefault("created_at", time.time())
        atomic_json_write(meta_path, meta)
    except Exception as exc:
        logger.debug("Could not write checkpoint metadata for %s: %s", commit_hash, exc)


def _read_checkpoint_meta(store: Path, commit_hash: str) -> Optional[Dict]:
    """Load optional metadata for a checkpoint commit."""
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


def _remove_index_lock(index_file: Optional[Path], *, reason: str) -> None:
    """Remove the lock left by a stopped Git command using this private index."""
    if index_file is None:
        return
    lock_path = Path(f"{index_file}.lock")
    try:
        lock_path.unlink()
    except FileNotFoundError:
        return
    except OSError:
        logger.warning("Could not remove %s checkpoint index lock: %s", reason, lock_path)
    else:
        logger.info("[CANCEL_TRACE] checkpoint_index_lock_removed reason=%s path=%s", reason, lock_path)


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
        result = run_captured_process(
            cmd,
            timeout=timeout,
            env=env,
            cwd=str(normalized_working_dir),
            cancel_event=get_interrupt_event(),
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
    except InterruptedError:
        _remove_index_lock(index_file, reason="cancelled")
        raise
    except subprocess.TimeoutExpired:
        _remove_index_lock(index_file, reason="timed-out")
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
        if getattr(exc, "termination_fence", None) is not None:
            raise
        logger.error("Unexpected git error running %s: %s", " ".join(cmd), exc, exc_info=True)
        return False, "", str(exc)


def _ensure_snapshot_refs(
    store: Path,
    working_dir: str,
    dir_hash: str,
) -> Optional[str]:
    """Add immutable refs for checkpoints created by the legacy linear format."""
    head_ref = _ref_name(dir_hash)
    ok, stdout, error = _run_git(
        ["rev-list", "--reverse", head_ref],
        store,
        working_dir,
        allowed_returncodes={128},
    )
    if not ok:
        exists, _, _ = _run_git(
            ["show-ref", "--verify", "--quiet", head_ref],
            store,
            working_dir,
            allowed_returncodes={1, 128},
        )
        return (error or "could not list legacy checkpoints") if exists else None
    if not stdout:
        return None
    for commit_hash in stdout.splitlines():
        snapshot_ref = _snapshot_ref_name(dir_hash, commit_hash)
        exists, _, _ = _run_git(
            ["show-ref", "--verify", "--quiet", snapshot_ref],
            store,
            working_dir,
            allowed_returncodes={1, 128},
        )
        if not exists:
            created, _, error = _run_git(
                ["update-ref", snapshot_ref, commit_hash],
                store,
                working_dir,
            )
            if not created:
                return error or f"could not preserve legacy checkpoint {commit_hash}"
    return None


def _list_snapshot_records(
    store: Path,
    working_dir: str,
    dir_hash: str,
) -> List[Dict]:
    _ensure_snapshot_refs(store, working_dir, dir_hash)
    ok, stdout, _ = _run_git(
        ["for-each-ref", "--format=%(objectname)", _snapshot_ref_prefix(dir_hash)],
        store,
        working_dir,
        allowed_returncodes={128},
    )
    if not ok or not stdout:
        return []
    records: List[Dict] = []
    for commit_hash in dict.fromkeys(stdout.splitlines()):
        metadata = _read_checkpoint_meta(store, commit_hash) or {}
        try:
            created_at = float(metadata.get("created_at") or 0)
        except (TypeError, ValueError):
            created_at = 0.0
        if created_at <= 0:
            time_ok, commit_time, _ = _run_git(
                ["show", "-s", "--format=%ct", commit_hash],
                store,
                working_dir,
            )
            try:
                created_at = float(commit_time) if time_ok else 0.0
            except ValueError:
                created_at = 0.0
        records.append({
            "hash": commit_hash,
            "created_at": created_at,
            "pinned": bool(metadata.get("recovery_pins")),
            "metadata": metadata,
        })
    return sorted(records, key=lambda record: (record["created_at"], record["hash"]))


def _publish_checkpoint(
    store: Path,
    working_dir: str,
    dir_hash: str,
    commit_hash: str,
    previous_head: str | None,
) -> Optional[str]:
    snapshot_ref = _snapshot_ref_name(dir_hash, commit_hash)
    existed, _, _ = _run_git(
        ["show-ref", "--verify", "--quiet", snapshot_ref],
        store,
        working_dir,
        allowed_returncodes={1, 128},
    )
    ok_snapshot, _, error = _run_git(
        ["update-ref", snapshot_ref, commit_hash],
        store,
        working_dir,
    )
    if not ok_snapshot:
        return error or "snapshot ref update failed"
    update_args = ["update-ref", _ref_name(dir_hash), commit_hash]
    if previous_head:
        update_args.append(previous_head)
    ok_head, _, error = _run_git(update_args, store, working_dir)
    if not ok_head:
        if not existed:
            _run_git(["update-ref", "-d", snapshot_ref], store, working_dir)
        return error or "project head update failed"
    return None


def _pin_checkpoint(store: Path, commit_hash: str, pin_id: str) -> bool:
    metadata = _read_checkpoint_meta(store, commit_hash) or {}
    pins = {str(value) for value in metadata.get("recovery_pins") or [] if value}
    pins.add(str(pin_id))
    metadata["recovery_pins"] = sorted(pins)
    try:
        metadata.setdefault("commit", commit_hash)
        metadata.setdefault("created_at", time.time())
        atomic_json_write(_checkpoint_meta_path(store, commit_hash), metadata)
        return True
    except OSError as exc:
        logger.error("Could not pin recovery checkpoint %s: %s", commit_hash, exc)
        return False


def _unpin_checkpoint(store: Path, commit_hash: str, pin_id: str) -> bool:
    metadata = _read_checkpoint_meta(store, commit_hash) or {}
    pins = {str(value) for value in metadata.get("recovery_pins") or [] if value}
    pins.discard(str(pin_id))
    if pins:
        metadata["recovery_pins"] = sorted(pins)
    else:
        metadata.pop("recovery_pins", None)
    try:
        atomic_json_write(_checkpoint_meta_path(store, commit_hash), metadata)
        return True
    except OSError as exc:
        logger.error("Could not unpin recovery checkpoint %s: %s", commit_hash, exc)
        return False


def _restore_intent_path(store: Path, intent_id: str) -> Path:
    return store / _RECOVERY_DIRNAME / f"{intent_id}.json"


def _write_restore_intent(store: Path, intent: Dict) -> None:
    intent["updated_at"] = time.time()
    atomic_json_write(_restore_intent_path(store, str(intent["id"])), intent)


def _read_restore_intents(store: Path) -> List[Dict]:
    recovery_dir = store / _RECOVERY_DIRNAME
    if not recovery_dir.exists():
        return []
    intents: List[Dict] = []
    for path in recovery_dir.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and payload.get("id"):
                intents.append(payload)
        except (OSError, json.JSONDecodeError):
            logger.warning("Ignoring unreadable restore intent: %s", path)
    return sorted(
        intents,
        key=lambda item: float(item.get("updated_at") or item.get("created_at") or 0),
        reverse=True,
    )


def _set_restore_intent_status(store: Path, intent: Optional[Dict], status: str, **extra) -> bool:
    if not intent:
        return True
    intent.update(extra)
    intent["status"] = status
    try:
        _write_restore_intent(store, intent)
        return True
    except OSError as exc:
        logger.error("Could not update restore intent %s: %s", intent.get("id"), exc)
        return False


def _resolve_project_commit(
    store: Path,
    working_dir: str,
    commit_hash: str,
) -> Optional[str]:
    """Resolve an abbreviated hash only when it belongs to this workdir."""
    dir_hash = _project_hash(working_dir)
    _ensure_snapshot_refs(store, working_dir, dir_hash)
    resolved, full_hash, _ = _run_git(
        ["rev-parse", "--verify", f"{commit_hash}^{{commit}}"],
        store,
        working_dir,
        allowed_returncodes={128},
    )
    if not resolved or not full_hash:
        return None
    snapshot_ref = _snapshot_ref_name(dir_hash, full_hash)
    registered, _, _ = _run_git(
        ["show-ref", "--verify", "--quiet", snapshot_ref],
        store,
        working_dir,
        allowed_returncodes={1, 128},
    )
    if registered:
        return full_hash
    ok, _, _ = _run_git(
        ["merge-base", "--is-ancestor", full_hash, _ref_name(dir_hash)],
        store,
        working_dir,
        allowed_returncodes={1, 128},
    )
    return full_hash if ok else None


def _commit_belongs_to_project(store: Path, working_dir: str, commit_hash: str) -> bool:
    """Return True only when commit_hash is registered for this workdir."""
    return _resolve_project_commit(store, working_dir, commit_hash) is not None


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
        return _sync_default_excludes(store)

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
        result = run_captured_process(
            ["git", "init", "--bare", str(store)],
            env=init_env,
            cwd=str(base),
            timeout=_GIT_TIMEOUT,
            cancel_event=get_interrupt_event(),
        )
        if result.returncode != 0:
            return f"Shadow store init failed: {result.stderr.strip()}"
    except InterruptedError:
        raise
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

    exclude_error = _sync_default_excludes(store)
    if exclude_error:
        return exclude_error

    logger.debug("Initialised checkpoint store at %s", store)
    return None


def _sync_default_excludes(store: Path) -> Optional[str]:
    """Keep credential and bulk-file exclusions current for existing stores."""
    content = "\n".join(DEFAULT_EXCLUDES) + "\n"
    path = store / "info" / "exclude"
    try:
        path.parent.mkdir(exist_ok=True)
        if not path.exists() or path.read_text(encoding="utf-8") != content:
            path.write_text(content, encoding="utf-8")
    except OSError as exc:
        return f"Could not update checkpoint exclusions: {exc}"
    return _sanitize_existing_credential_storage(store)


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


def _sanitize_existing_credential_storage(store: Path) -> Optional[str]:
    """Remove credentials already tracked by indexes or retained snapshot refs."""
    marker = store / _CREDENTIAL_SANITIZE_MARKER_NAME
    if marker.exists():
        return None
    try:
        policy = RuntimeManager.current().paths
    except Exception as exc:
        return f"Could not load PathPolicy while sanitizing checkpoints: {exc}"

    refs_changed = False
    for project in _list_projects(store):
        dir_hash = str(project.get("_hash") or "")
        workdir = str(project.get("workdir") or "")
        if not dir_hash or not workdir:
            continue
        root = _normalize_path(workdir)
        git_cwd = str(root) if root.is_dir() else str(store.parent)
        index_file = _index_path(store, dir_hash)
        if index_file.exists():
            ok, stdout, error = _run_git(
                ["ls-files", "--cached", "-z"],
                store,
                git_cwd,
                index_file=index_file,
            )
            if not ok:
                return error or f"Could not inspect checkpoint index {dir_hash}"
            credentials = [
                rel
                for rel in stdout.split("\x00")
                if rel and policy.is_credential_path(root / rel)
            ]
            for offset in range(0, len(credentials), 200):
                removed, _, error = _run_git(
                    [
                        "--literal-pathspecs",
                        "update-index",
                        "--force-remove",
                        "--",
                        *credentials[offset:offset + 200],
                    ],
                    store,
                    git_cwd,
                    index_file=index_file,
                )
                if not removed:
                    return error or f"Could not sanitize checkpoint index {dir_hash}"

        migration_error = _ensure_snapshot_refs(store, git_cwd, dir_hash)
        if migration_error:
            return f"Could not inspect checkpoint history {dir_hash}: {migration_error}"
        records = _list_snapshot_records(store, git_cwd, dir_hash)
        contaminated: Set[str] = set()
        legacy: Set[str] = set()
        for record in records:
            commit_hash = record["hash"]
            if (record.get("metadata") or {}).get("snapshot_format") != "root-v1":
                legacy.add(commit_hash)
            ok, stdout, error = _run_git(
                ["ls-tree", "-r", "-z", "--name-only", commit_hash],
                store,
                git_cwd,
            )
            if not ok:
                return error or f"Could not inspect checkpoint {commit_hash}"
            if any(
                policy.is_credential_path(root / rel)
                for rel in stdout.split("\x00")
                if rel
            ):
                contaminated.add(commit_hash)

        legacy_contaminated = bool(contaminated & legacy)
        drop_hashes = contaminated | (legacy if legacy_contaminated else set())
        if not drop_hashes:
            continue
        ok, head, _ = _run_git(
            ["rev-parse", "--verify", _ref_name(dir_hash) + "^{commit}"],
            store,
            git_cwd,
            allowed_returncodes={128},
        )
        if ok and head in drop_hashes and not _delete_ref(store, _ref_name(dir_hash)):
            return f"Could not remove credential-bearing project head {dir_hash}"
        for commit_hash in drop_hashes:
            if not _delete_snapshot_ref(
                store,
                _snapshot_ref_name(dir_hash, commit_hash),
                commit_hash,
            ):
                return f"Could not remove credential-bearing checkpoint {commit_hash}"
        refs_changed = True

    if refs_changed:
        expired, _, error = _run_git(
            ["reflog", "expire", "--expire=now", "--all"],
            store,
            str(store.parent),
        )
        if not expired:
            return error or "Could not expire credential-bearing checkpoint reflogs"
        collected, _, error = _run_git(
            ["gc", "--prune=now", "--quiet"],
            store,
            str(store.parent),
            timeout=_GIT_TIMEOUT * 3,
        )
        if not collected:
            return error or "Could not prune credential-bearing checkpoint objects"
    try:
        marker.write_text("1\n", encoding="utf-8")
    except OSError as exc:
        return f"Could not record checkpoint credential migration: {exc}"
    return None


def _dir_file_count(
    path: str,
    cancel_event: threading.Event | None = None,
) -> int:
    """Quick file count estimate (stops early if over _MAX_FILES)."""
    cancel_event = cancel_event or get_interrupt_event()
    count = 0
    try:
        for _ in Path(path).rglob("*"):
            cancellation_checkpoint(cancel_event)
            count += 1
            if count > _MAX_FILES:
                return count
    except InterruptedError:
        raise
    except (PermissionError, OSError):
        pass
    return count


def _dir_size_bytes(path: Path) -> int:
    """Best-effort recursive size in bytes.  Returns 0 on error."""
    cancel_event = get_interrupt_event()
    total = 0
    try:
        for p in path.rglob("*"):
            cancellation_checkpoint(cancel_event)
            try:
                if p.is_file():
                    total += p.stat().st_size
            except OSError:
                continue
    except InterruptedError:
        raise
    except OSError:
        pass
    return total


# ---------------------------------------------------------------------------
# CheckpointManager
# ---------------------------------------------------------------------------

class CheckpointManager:
    """Manages automatic filesystem checkpoints.

    Owned by the M-Claw agent runtime. The core calls ``new_turn()`` before
    each tool-dispatch batch, then ``ensure_checkpoint(dir, reason)`` before
    file mutations. Full-directory snapshots are deduplicated within that
    scope; targeted snapshots also track which paths have been captured.

    Parameters
    ----------
    enabled : bool
        Runtime configuration switch.
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
        """Reset snapshot deduplication before one model-emitted tool batch."""
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
        Records non-fatal failures in ``last_attempt`` instead of raising.
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

        # Reject root, home, and other overly broad directories.
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
        except InterruptedError:
            raise
        except Exception as e:
            if getattr(e, "termination_fence", None) is not None:
                raise
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
        self.last_attempt = {}
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
            taken = self._take(abs_dir, reason, metadata=metadata, target_paths=target_paths)
            if not taken and not self.last_attempt:
                self._record_attempt("failed", abs_dir, reason, detail="checkpoint creation failed")
            return taken
        except InterruptedError:
            raise
        except Exception as e:
            if getattr(e, "termination_fence", None) is not None:
                raise
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

        results: List[Dict] = []
        records = _list_snapshot_records(store, abs_dir, _project_hash(abs_dir))
        for record in reversed(records[-self.max_snapshots:]):
            commit_hash = record["hash"]
            ok, line, _ = _run_git(
                ["show", "-s", "--format=%H|%h|%aI|%s", commit_hash],
                store,
                abs_dir,
            )
            if not ok:
                continue
            parts = line.split("|", 3)
            if len(parts) == 4:
                metadata = record.get("metadata") or {}
                entry = {
                    "hash": parts[0],
                    "short_hash": parts[1],
                    "timestamp": parts[2],
                    "reason": parts[3],
                    "metadata": metadata,
                    "files_changed": 0,
                    "insertions": 0,
                    "deletions": 0,
                }
                previous = metadata.get("previous_commit")
                diff_base = previous if previous else f"{parts[0]}~1"
                stat_ok, stat_out, _ = _run_git(
                    ["diff", "--shortstat", diff_base, parts[0]],
                    store, abs_dir,
                    allowed_returncodes={128, 129},
                )
                if stat_ok and stat_out:
                    self._parse_shortstat(stat_out, entry)
                results.append(entry)
        return results

    def has_checkpoint(self, working_dir: str, commit_hash: str) -> bool:
        """Return whether a checkpoint is still available for this directory."""
        if _validate_commit_hash(commit_hash):
            return False
        abs_dir = str(_normalize_path(working_dir))
        store = _store_path(CHECKPOINT_BASE)
        return bool(
            (store / "HEAD").exists()
            and _resolve_project_commit(store, abs_dir, commit_hash)
        )

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

    def diff(
        self,
        working_dir: str,
        commit_hash: str,
        target_paths: Optional[List[str]] = None,
    ) -> Dict:
        """Show diff between a checkpoint and the current working tree.

        Targeted checkpoints compare only the paths captured in metadata so
        large-project snapshots do not accidentally report unrelated changes.
        """
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
        resolved_commit = _resolve_project_commit(store, abs_dir, commit_hash)
        if not resolved_commit:
            return {"success": False, "error": f"Checkpoint '{commit_hash}' does not belong to this directory"}
        commit_hash = resolved_commit
        metadata = _read_checkpoint_meta(store, commit_hash) or {}
        requested_targets = (
            list(target_paths)
            if target_paths is not None
            else list(metadata.get("target_paths_rel") or [])
            if metadata.get("targeted")
            else ["."]
        )
        diff_targets, target_error = _normalize_restore_targets(abs_dir, requested_targets)
        if target_error:
            return {"success": False, "error": target_error}

        dir_hash = _project_hash(abs_dir)
        index_file = _index_path(store, dir_hash)

        # Stage current state into the per-project index to compare.
        if diff_targets != ["."]:
            _run_git(["read-tree", commit_hash], store, abs_dir, index_file=index_file, allowed_returncodes={128})
            for rel in diff_targets:
                path = Path(abs_dir) / rel
                if path.exists():
                    _run_git(["--literal-pathspecs", "add", "-f", "--", rel], store, abs_dir,
                             timeout=_GIT_TIMEOUT * 2, index_file=index_file)
                else:
                    _run_git(
                        ["--literal-pathspecs", "rm", "--cached", "--ignore-unmatch", "--", rel],
                        store,
                        abs_dir,
                        index_file=index_file,
                    )
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

    def list_restore_intents(
        self,
        working_dir: Optional[str] = None,
        *,
        unresolved_only: bool = True,
    ) -> List[Dict]:
        """Return durable restore attempts, newest first."""
        store = _store_path(CHECKPOINT_BASE)
        if not (store / "HEAD").exists():
            return []
        workspace = str(_normalize_path(working_dir)) if working_dir else None
        unresolved = {"applying", "interrupted", "failed"}
        return [
            intent
            for intent in _read_restore_intents(store)
            if (not workspace or intent.get("workspace") == workspace)
            and (not unresolved_only or intent.get("status") in unresolved)
        ]

    def recover_restore(
        self,
        intent_id: Optional[str] = None,
        *,
        working_dir: Optional[str] = None,
        defer_completion: bool = False,
    ) -> Dict:
        """Restore the pre-operation snapshot recorded by an interrupted restore."""
        cancellation_checkpoint(get_interrupt_event())
        intents = self.list_restore_intents(working_dir)
        if intent_id:
            matches = [
                item
                for item in intents
                if item.get("id") == intent_id or str(item.get("id") or "").startswith(intent_id)
            ]
            if len(matches) != 1:
                return {
                    "success": False,
                    "error": (
                        f"Restore recovery intent '{intent_id}' was not found"
                        if not matches
                        else f"Restore recovery intent '{intent_id}' is ambiguous"
                    ),
                }
            intent = matches[0]
        elif intents:
            intent = intents[0]
        else:
            return {"success": False, "error": "No interrupted restore needs recovery"}

        recovery_commit = str(intent.get("recovery_commit") or "")
        workspace = str(intent.get("workspace") or "")
        target_paths = list(intent.get("target_paths") or [])
        if not recovery_commit or not workspace or not target_paths:
            return {"success": False, "error": "Restore recovery intent is incomplete", "intent": intent}
        if "target_commits" in intent and "context_recovery" not in intent:
            return {
                "success": False,
                "error": "Restore recovery intent predates coordinated context recovery; refusing file-only recovery",
                "intent": intent,
            }
        context_recovery = intent.get("context_recovery")
        if context_recovery and (
            not isinstance(context_recovery, dict)
            or context_recovery.get("action") not in {"restore", "reapply"}
            or not isinstance(context_recovery.get("rollback_ids"), list)
            or not context_recovery.get("rollback_ids")
            or not all(
                isinstance(value, str) and value
                for value in context_recovery.get("rollback_ids") or []
            )
        ):
            return {
                "success": False,
                "error": "Restore recovery context metadata is invalid",
                "intent": intent,
            }
        if context_recovery and not defer_completion:
            return {
                "success": False,
                "error": "Restore recovery also requires chat context recovery",
                "intent": intent,
            }

        result = self.restore(
            workspace,
            recovery_commit,
            target_paths=target_paths,
            create_pre_snapshot=True,
        )
        if defer_completion and result.get("success"):
            return {
                "success": True,
                "error": None,
                "intent_id": intent.get("id"),
                "intent": intent,
                "restore": result,
            }
        store = _store_path(CHECKPOINT_BASE)
        status_saved = _set_restore_intent_status(
            store,
            intent,
            "recovered" if result.get("success") else "failed",
            recovery_result={
                "success": bool(result.get("success")),
                "error": result.get("error"),
                "restored_hash": result.get("restored_hash"),
            },
        )
        status_error = None if status_saved else "Recovery completed but its intent status could not be persisted"
        return {
            "success": bool(result.get("success") and status_saved),
            "error": result.get("error") or status_error,
            "intent_id": intent.get("id"),
            "intent": intent,
            "restore": result,
        }

    def begin_restore_intent(
        self,
        working_dir: str,
        *,
        target_commits: List[str],
        recovery_commit: Optional[str] = None,
        target_paths: List[str],
        kind: str = "restore-group",
        context_recovery: Optional[Dict] = None,
    ) -> Dict:
        """Persist and pin one recovery boundary owned by a higher-level operation."""
        abs_dir = str(_normalize_path(working_dir))
        store = _store_path(CHECKPOINT_BASE)
        if not (store / "HEAD").exists():
            return {"success": False, "error": "No checkpoints exist for this directory"}

        normalized_targets, target_error = _normalize_restore_targets(abs_dir, target_paths)
        if target_error:
            return {"success": False, "error": target_error}
        policy_error = _restore_policy_error([
            (action, str((Path(abs_dir) / target).resolve()))
            for target in normalized_targets
            for action in ("overwrite", "delete")
        ])
        if policy_error:
            return {"success": False, "error": policy_error}

        resolved_targets: List[str] = []
        for commit_hash in target_commits:
            if not commit_hash:
                continue
            hash_error = _validate_commit_hash(commit_hash)
            resolved = (
                None
                if hash_error
                else _resolve_project_commit(store, abs_dir, commit_hash)
            )
            if hash_error or not resolved:
                return {
                    "success": False,
                    "error": hash_error or (
                        f"Checkpoint '{commit_hash}' does not belong to this directory"
                    ),
                }
            if resolved not in resolved_targets:
                resolved_targets.append(resolved)

        intent_id = uuid.uuid4().hex
        pinned: List[str] = []

        def release_pins() -> None:
            for pinned_hash in pinned:
                _unpin_checkpoint(store, pinned_hash, intent_id)

        for commit_hash in resolved_targets:
            if commit_hash in pinned:
                continue
            if not _pin_checkpoint(store, commit_hash, intent_id):
                release_pins()
                return {"success": False, "error": "Could not pin every restore checkpoint"}
            pinned.append(commit_hash)

        if recovery_commit:
            recovery_error = _validate_commit_hash(recovery_commit)
            resolved_recovery = (
                None
                if recovery_error
                else _resolve_project_commit(store, abs_dir, recovery_commit)
            )
            if recovery_error or not resolved_recovery:
                release_pins()
                return {
                    "success": False,
                    "error": recovery_error or (
                        f"Recovery checkpoint '{recovery_commit}' does not belong to this directory"
                    ),
                }
        else:
            try:
                created = self.create_checkpoint(
                    abs_dir,
                    f"pre-{kind} snapshot",
                    metadata={"restore_intent_id": intent_id, "restore_kind": kind},
                    target_paths=[
                        str((Path(abs_dir) / target).resolve())
                        for target in normalized_targets
                    ],
                )
            except BaseException:
                release_pins()
                raise
            attempt = self.last_attempt or {}
            resolved_recovery = attempt.get("commit") if created else None
            if resolved_recovery:
                resolved_recovery = _resolve_project_commit(store, abs_dir, resolved_recovery)
            if not resolved_recovery:
                release_pins()
                detail = attempt.get("detail") or attempt.get("status") or "unknown error"
                return {
                    "success": False,
                    "error": f"Could not create restore recovery checkpoint: {detail}",
                }

        if resolved_recovery not in pinned:
            if not _pin_checkpoint(store, resolved_recovery, intent_id):
                release_pins()
                return {"success": False, "error": "Could not pin every restore checkpoint"}
            pinned.append(resolved_recovery)

        intent = {
            "id": intent_id,
            "kind": kind,
            "status": "applying",
            "created_at": time.time(),
            "workspace": abs_dir,
            "target_commit": resolved_targets[0] if resolved_targets else "",
            "target_commits": resolved_targets,
            "recovery_commit": resolved_recovery,
            "target_paths": normalized_targets,
            "context_recovery": dict(context_recovery) if context_recovery else None,
        }
        try:
            _write_restore_intent(store, intent)
        except Exception as exc:
            release_pins()
            return {
                "success": False,
                "error": f"Could not persist restore recovery intent: {exc}",
            }
        return {
            "success": True,
            "intent_id": intent_id,
            "recovery_commit": resolved_recovery,
            "target_commits": resolved_targets,
            "target_paths": normalized_targets,
        }

    def finish_restore_intent(
        self,
        intent_id: str,
        status: str,
        *,
        error: Optional[str] = None,
    ) -> bool:
        """Finish a deferred batch intent after its context transaction."""
        if status not in {"completed", "recovered", "failed", "interrupted"}:
            return False
        store = _store_path(CHECKPOINT_BASE)
        intent = next(
            (item for item in _read_restore_intents(store) if item.get("id") == intent_id),
            None,
        )
        if not intent:
            return False
        return _set_restore_intent_status(store, intent, status, error=error)

    def restore_batch(
        self,
        working_dir: str,
        restores: List[Dict],
        *,
        defer_completion: bool = False,
        context_recovery: Optional[Dict] = None,
    ) -> Dict:
        """Apply several targeted checkpoints with one durable recovery snapshot."""
        if not restores:
            return {"success": False, "error": "No restores were provided"}
        abs_dir = str(_normalize_path(working_dir))
        store = _store_path(CHECKPOINT_BASE)
        if not (store / "HEAD").exists():
            return {"success": False, "error": "No checkpoints exist for this directory"}

        prepared: List[Dict] = []
        union_targets: List[str] = []
        for item in restores:
            commit_hash = str(item.get("commit_hash") or "")
            hash_error = _validate_commit_hash(commit_hash)
            if hash_error:
                return {"success": False, "error": hash_error}
            resolved_commit = _resolve_project_commit(store, abs_dir, commit_hash)
            if not resolved_commit:
                return {
                    "success": False,
                    "error": f"Checkpoint '{commit_hash}' does not belong to this directory",
                }
            targets, target_error = _normalize_restore_targets(
                abs_dir,
                list(item.get("target_paths") or []),
            )
            if target_error:
                return {"success": False, "error": target_error}
            prepared.append({"commit_hash": resolved_commit, "target_paths": targets})
            union_targets.extend(target for target in targets if target not in union_targets)

        policy_error = _restore_policy_error([
            (action, str((Path(abs_dir) / target).resolve()))
            for target in union_targets
            for action in ("overwrite", "delete")
        ])
        if policy_error:
            return {"success": False, "error": policy_error}

        intent_id = uuid.uuid4().hex
        pinned_commits: Set[str] = set()

        def pin(commit_hash: str) -> bool:
            if commit_hash in pinned_commits:
                return True
            if not _pin_checkpoint(store, commit_hash, intent_id):
                return False
            pinned_commits.add(commit_hash)
            return True

        def release_pins() -> None:
            for commit_hash in pinned_commits:
                _unpin_checkpoint(store, commit_hash, intent_id)

        for item in prepared:
            if not pin(item["commit_hash"]):
                release_pins()
                return {"success": False, "error": "Could not pin every batch restore target"}

        try:
            created = self.create_checkpoint(
                abs_dir,
                "pre-batch-restore snapshot",
                metadata={"restore_intent_id": intent_id, "restore_batch": True},
                target_paths=[
                    str((Path(abs_dir) / target).resolve())
                    for target in union_targets
                ],
            )
        except BaseException:
            release_pins()
            raise
        attempt = self.last_attempt or {}
        recovery_commit = attempt.get("commit") if created else None
        if not recovery_commit or not pin(recovery_commit):
            release_pins()
            return {
                "success": False,
                "error": (
                    "Could not create the batch recovery checkpoint"
                    if not recovery_commit
                    else "Could not pin the batch recovery checkpoint"
                ),
            }

        intent = {
            "id": intent_id,
            "kind": "restore-batch",
            "status": "applying",
            "created_at": time.time(),
            "workspace": abs_dir,
            "target_commit": prepared[0]["commit_hash"],
            "target_commits": [item["commit_hash"] for item in prepared],
            "recovery_commit": recovery_commit,
            "target_paths": union_targets,
            "context_recovery": dict(context_recovery) if context_recovery else None,
        }
        try:
            _write_restore_intent(store, intent)
        except Exception as exc:
            release_pins()
            return {
                "success": False,
                "error": f"Could not persist batch restore recovery intent: {exc}",
            }

        results: List[Dict] = []
        try:
            for item in prepared:
                result = self.restore(
                    abs_dir,
                    item["commit_hash"],
                    target_paths=item["target_paths"],
                    create_pre_snapshot=False,
                )
                results.append(result)
                if result.get("success"):
                    continue
                compensation = self.restore(
                    abs_dir,
                    recovery_commit,
                    target_paths=union_targets,
                    create_pre_snapshot=False,
                )
                recovered = bool(compensation.get("success"))
                _set_restore_intent_status(
                    store,
                    intent,
                    "recovered" if recovered else "failed",
                    error=result.get("error"),
                )
                return {
                    "success": False,
                    "error": (
                        f"{result.get('error') or 'Batch restore failed'}; "
                        + (
                            f"restored the pre-batch state from checkpoint {recovery_commit[:8]}"
                            if recovered
                            else f"batch recovery also failed: {compensation.get('error') or 'unknown error'}"
                        )
                    ),
                    "results": results,
                    "recovery_commit": recovery_commit,
                    "restore_intent_id": intent_id,
                    "compensation": compensation,
                }
        except InterruptedError as exc:
            _set_restore_intent_status(store, intent, "interrupted", error=str(exc))
            raise
        except Exception as exc:
            _set_restore_intent_status(
                store,
                intent,
                "interrupted" if getattr(exc, "termination_fence", None) is not None else "failed",
                error=str(exc),
            )
            raise

        if not defer_completion:
            if not _set_restore_intent_status(store, intent, "completed"):
                compensation = self.restore(
                    abs_dir,
                    recovery_commit,
                    target_paths=union_targets,
                    create_pre_snapshot=False,
                )
                return {
                    "success": False,
                    "error": (
                        "Batch restore intent could not be finalized; "
                        + (
                            f"restored the pre-batch state from checkpoint {recovery_commit[:8]}"
                            if compensation.get("success")
                            else "automatic recovery also failed: "
                            f"{compensation.get('error') or 'unknown error'}"
                        )
                    ),
                    "results": results,
                    "recovery_commit": recovery_commit,
                    "restore_intent_id": intent_id,
                    "compensation": compensation,
                }
        return {
            "success": True,
            "results": results,
            "recovery_commit": recovery_commit,
            "restore_intent_id": intent_id,
            "target_paths": union_targets,
        }

    def restore(
        self,
        working_dir: str,
        commit_hash: str,
        file_path: Optional[str] = None,
        create_pre_snapshot: bool = True,
        target_paths: Optional[List[str]] = None,
        recovery_commit: Optional[str] = None,
        recovery_target_paths: Optional[List[str]] = None,
        recovery_intent_id: Optional[str] = None,
    ) -> Dict:
        """Restore files to a checkpoint state, optionally limited to explicit targets.

        Targeted checkpoints may record paths that were absent at snapshot time;
        restoring those paths means deleting files created after the checkpoint.
        """
        hash_err = _validate_commit_hash(commit_hash)
        if hash_err:
            return {"success": False, "error": hash_err}

        abs_dir = str(_normalize_path(working_dir))
        skip_reason = _checkpoint_skip_reason(abs_dir)
        if skip_reason:
            return {"success": False, "error": f"Checkpoints are skipped for {skip_reason}"}

        policy_error = _restore_policy_error([("overwrite", abs_dir)])
        if policy_error:
            return {"success": False, "error": policy_error}
        if file_path and target_paths is not None:
            return {"success": False, "error": "Use file_path or target_paths, not both"}

        store = _store_path(CHECKPOINT_BASE)

        if not (store / "HEAD").exists():
            return {"success": False, "error": "No checkpoints exist for this directory"}

        ok, _, err = _run_git(
            ["cat-file", "-t", commit_hash], store, abs_dir,
        )
        if not ok:
            return {"success": False, "error": f"Checkpoint '{commit_hash}' not found",
                    "debug": err or None}
        resolved_commit = _resolve_project_commit(store, abs_dir, commit_hash)
        if not resolved_commit:
            return {
                "success": False,
                "error": f"Checkpoint '{commit_hash}' does not belong to this directory",
            }
        commit_hash = resolved_commit
        if recovery_commit:
            recovery_hash_error = _validate_commit_hash(recovery_commit)
            resolved_recovery = (
                None
                if recovery_hash_error
                else _resolve_project_commit(store, abs_dir, recovery_commit)
            )
            if recovery_hash_error or not resolved_recovery:
                return {
                    "success": False,
                    "error": recovery_hash_error or (
                        f"Recovery checkpoint '{recovery_commit}' does not belong to this directory"
                    ),
                }
            recovery_commit = resolved_recovery
        metadata = _read_checkpoint_meta(store, commit_hash) or {}
        ok_reason, reason_out, _ = _run_git(
            ["log", "--format=%s", "-1", commit_hash], store, abs_dir,
        )
        reason = reason_out if ok_reason else "unknown"

        requested_targets = (
            [file_path]
            if file_path
            else list(target_paths)
            if target_paths is not None
            else list(metadata.get("target_paths_rel") or [])
            if metadata.get("targeted")
            else ["."]
        )
        restore_targets, target_error = _normalize_restore_targets(abs_dir, requested_targets)
        if target_error:
            return {"success": False, "error": target_error}
        intent_targets = restore_targets
        if recovery_target_paths is not None:
            intent_targets, target_error = _normalize_restore_targets(abs_dir, recovery_target_paths)
            if target_error:
                return {"success": False, "error": target_error}
            policy_error = _restore_policy_error([
                ("overwrite", str((Path(abs_dir) / target).resolve()))
                for target in intent_targets
            ])
            if policy_error:
                return {"success": False, "error": policy_error}

        missing_at_checkpoint = set(metadata.get("missing_at_checkpoint") or [])
        ok_tree, tree_output, tree_error = _run_git(
            ["ls-tree", "-r", "-z", "--name-only", commit_hash],
            store,
            abs_dir,
        )
        if not ok_tree:
            return {
                "success": False,
                "error": f"Could not inspect checkpoint restore targets: {tree_error}",
                "debug": tree_error or None,
            }
        tree_paths = [path for path in tree_output.split("\0") if path]
        stored_targets = tree_paths + list(missing_at_checkpoint)
        if stored_targets:
            _, stored_target_error = _normalize_restore_targets(abs_dir, stored_targets)
            if stored_target_error:
                return {
                    "success": False,
                    "error": f"Checkpoint contains an unsafe restore target: {stored_target_error}",
                }
        selected_tree_paths = [
            path
            for path in tree_paths
            if any(_restore_target_contains(target, path) for target in restore_targets)
        ]
        cleanup_targets = sorted(
            path
            for path in missing_at_checkpoint
            if any(_restore_target_contains(target, path) for target in restore_targets)
        )
        policy_checks = [
            (
                "delete" if target in missing_at_checkpoint else "overwrite",
                str((Path(abs_dir) / target).resolve()),
            )
            for target in restore_targets
        ]
        policy_checks.extend(
            ("overwrite", str((Path(abs_dir) / path).resolve()))
            for path in selected_tree_paths
        )
        policy_checks.extend(
            ("delete", str((Path(abs_dir) / path).resolve()))
            for path in cleanup_targets
        )
        policy_error = _restore_policy_error(policy_checks)
        if policy_error:
            return {"success": False, "error": policy_error}

        pre_restore_commit = recovery_commit
        intent_id = uuid.uuid4().hex
        intent: Optional[Dict] = None
        pinned_commits: Set[str] = set()
        needs_intent = bool((create_pre_snapshot or recovery_commit) and not recovery_intent_id)

        def pin(commit: str) -> bool:
            if commit in pinned_commits:
                return True
            if not _pin_checkpoint(store, commit, intent_id):
                return False
            pinned_commits.add(commit)
            return True

        def release_pins() -> None:
            for pinned_commit in pinned_commits:
                _unpin_checkpoint(store, pinned_commit, intent_id)

        if needs_intent and not pin(commit_hash):
            return {
                "success": False,
                "error": "Could not pin the restore target before starting",
            }
        if create_pre_snapshot:
            # Take a pre-rollback snapshot so the rollback can be reversed.
            pre_target_paths = (
                None
                if restore_targets == ["."]
                else [str((Path(abs_dir) / target).resolve()) for target in restore_targets]
            )
            try:
                snapshot_created = self.create_checkpoint(
                    abs_dir,
                    f"pre-rollback snapshot (restoring to {commit_hash[:8]})",
                    metadata={"rollback_target": commit_hash, "restore_intent_id": intent_id},
                    target_paths=pre_target_paths,
                )
            except BaseException:
                release_pins()
                raise
            attempt = self.last_attempt or {}
            pre_restore_commit = attempt.get("commit") if snapshot_created else None
            if not pre_restore_commit and attempt.get("status") == "skipped" and attempt.get("detail") == "no changes":
                ref = _ref_name(_project_hash(abs_dir))
                try:
                    ref_ok, ref_commit, _ = _run_git(
                        ["rev-parse", "--verify", ref + "^{commit}"],
                        store,
                        abs_dir,
                        allowed_returncodes={128},
                    )
                except BaseException:
                    release_pins()
                    raise
                if ref_ok:
                    pre_restore_commit = ref_commit
            if not pre_restore_commit:
                detail = attempt.get("detail") or attempt.get("status") or "unknown error"
                release_pins()
                return {
                    "success": False,
                    "error": f"Could not create pre-restore checkpoint: {detail}",
                }
            try:
                retained = _commit_belongs_to_project(store, abs_dir, pre_restore_commit)
            except BaseException:
                release_pins()
                raise
            if not retained:
                release_pins()
                return {
                    "success": False,
                    "error": "Pre-restore checkpoint was not retained by checkpoint pruning",
                }
            if not pin(pre_restore_commit):
                release_pins()
                return {
                    "success": False,
                    "error": "Could not pin the pre-restore checkpoint before starting",
                }
            try:
                target_ok, _, target_error = _run_git(
                    ["cat-file", "-t", commit_hash],
                    store,
                    abs_dir,
                )
            except BaseException:
                release_pins()
                raise
            if not target_ok:
                release_pins()
                return {
                    "success": False,
                    "error": f"Restore checkpoint became unavailable while preparing recovery: {target_error}",
                }
        elif needs_intent and recovery_commit and not pin(recovery_commit):
            release_pins()
            return {
                "success": False,
                "error": "Could not pin the supplied recovery checkpoint before starting",
            }

        dir_hash = _project_hash(abs_dir)
        index_file = _index_path(store, dir_hash)

        policy_error = _restore_policy_error(policy_checks)
        if policy_error:
            release_pins()
            return {"success": False, "error": policy_error}

        if needs_intent:
            intent = {
                "id": intent_id,
                "status": "applying",
                "created_at": time.time(),
                "workspace": abs_dir,
                "target_commit": commit_hash,
                "recovery_commit": pre_restore_commit,
                "target_paths": intent_targets,
            }
            try:
                _write_restore_intent(store, intent)
            except Exception as exc:
                release_pins()
                return {
                    "success": False,
                    "error": f"Could not persist restore recovery intent: {exc}",
                }

        ok = True
        stdout = ""
        err = ""
        restored_targets: List[str] = []

        def restore_failure(message: str, failed_target: str, debug: str = "") -> Dict:
            result = {
                "success": False,
                "error": message,
                "debug": debug or None,
                "failed_target": failed_target,
                "restored_targets": list(restored_targets),
                "pre_restore_commit": pre_restore_commit,
                "restore_intent_id": intent_id if intent else None,
            }
            if not pre_restore_commit:
                _set_restore_intent_status(store, intent, "failed", error=message)
                return result
            compensation = self.restore(
                abs_dir,
                pre_restore_commit,
                target_paths=restore_targets,
                create_pre_snapshot=False,
            )
            result["compensation"] = compensation
            result["recovered"] = bool(compensation.get("success"))
            _set_restore_intent_status(
                store,
                intent,
                "recovered" if result["recovered"] else "failed",
                error=message,
                compensation={
                    "success": result["recovered"],
                    "error": compensation.get("error"),
                },
            )
            if result["recovered"]:
                result["error"] = (
                    f"{message}; restored the pre-restore state from checkpoint "
                    f"{pre_restore_commit[:8]}"
                )
            else:
                recovery_error = compensation.get("error") or "unknown error"
                result["error"] = (
                    f"{message}; automatic recovery from checkpoint "
                    f"{pre_restore_commit[:8]} also failed: {recovery_error}"
                )
            return result

        try:
            for restore_target in restore_targets:
                cancellation_checkpoint(get_interrupt_event())
                if restore_target == "." and not tree_paths:
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
                    restored_targets.append(restore_target)
                    continue
                ok, stdout, err = _run_git(
                    ["--literal-pathspecs", "checkout", commit_hash, "--", restore_target],
                    store, abs_dir, timeout=_GIT_TIMEOUT * 2,
                    index_file=index_file,
                )
                if not ok:
                    break
                restored_targets.append(restore_target)

            if not ok:
                return restore_failure(
                    f"Restore failed at {restore_target!r}: {err}",
                    restore_target,
                    err,
                )
            for rel in cleanup_targets:
                if rel in restore_targets:
                    continue
                cancellation_checkpoint(get_interrupt_event())
                target = (Path(abs_dir) / rel).resolve()
                try:
                    if target.is_file() or target.is_symlink():
                        target.unlink()
                    elif target.is_dir():
                        shutil.rmtree(target)
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    return restore_failure(
                        f"Restore cleanup failed at {rel!r}: {exc}",
                        rel,
                        str(exc),
                    )
                restored_targets.append(rel)
        except InterruptedError as exc:
            _set_restore_intent_status(store, intent, "interrupted", error=str(exc))
            raise
        except Exception as exc:
            _set_restore_intent_status(
                store,
                intent,
                "interrupted" if getattr(exc, "termination_fence", None) is not None else "failed",
                error=str(exc),
            )
            raise

        if not _set_restore_intent_status(store, intent, "completed"):
            compensation = self.restore(
                abs_dir,
                pre_restore_commit,
                target_paths=intent_targets,
                create_pre_snapshot=False,
            )
            return {
                "success": False,
                "error": (
                    "Restore intent could not be finalized; "
                    + (
                        f"restored the pre-restore state from checkpoint {pre_restore_commit[:8]}"
                        if compensation.get("success")
                        else "automatic recovery also failed: "
                        f"{compensation.get('error') or 'unknown error'}"
                    )
                ),
                "pre_restore_commit": pre_restore_commit,
                "restore_intent_id": intent_id if intent else None,
                "compensation": compensation,
                "recovered": bool(compensation.get("success")),
            }

        result = {
            "success": True,
            "restored_to": commit_hash[:8],
            "restored_hash": commit_hash,
            "reason": reason,
            "directory": abs_dir,
            "metadata": metadata,
            "pre_restore_commit": pre_restore_commit,
            "restore_intent_id": intent_id if intent else None,
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
        """Normalize explicit target paths into metadata for targeted snapshots."""
        if not target_paths:
            return {"rel": [], "abs": [], "missing": [], "oversize": []}
        abs_dir = _normalize_path(working_dir)
        rel_paths: List[str] = []
        abs_paths: List[str] = []
        missing: List[str] = []
        oversize: List[str] = []
        seen: Set[str] = set()
        for item in target_paths:
            cancellation_checkpoint(get_interrupt_event())
            if not item:
                continue
            path = _normalize_path(str(item))
            try:
                path_policy = RuntimeManager.current().paths
                if path_policy.is_runtime_internal_path(path) or not path_policy.check("read", path).allowed:
                    continue
            except Exception:
                continue
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
        """Merge target metadata from repeated per-turn targeted checkpoints."""
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

        # Size guard for very large working directories.
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

        # Stage with a per-project index. Broad exclusions come from the
        # exclude file, then oversized paths are pruned after staging.
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
        if not self._drop_credentials_from_index(store, working_dir, index_file):
            self._record_attempt(
                "failed",
                working_dir,
                reason,
                detail="could not remove credential paths from checkpoint index",
            )
            return False

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

        # Preserve legacy checkpoints before switching the project head to the
        # immutable, independent snapshot format.
        migration_error = _ensure_snapshot_refs(store, working_dir, dir_hash)
        if migration_error:
            self._record_attempt(
                "failed",
                working_dir,
                reason,
                detail=f"could not preserve legacy checkpoints: {migration_error}",
            )
            return False
        commit_args = [
            "commit-tree",
            tree_sha,
            "-m",
            reason,
            "-m",
            f"mclaw-checkpoint: {time.time_ns()}",
            "--no-gpg-sign",
        ]
        ok_commit, new_sha, err = _run_git(
            commit_args, store, working_dir,
            index_file=index_file,
        )
        if not ok_commit or not new_sha:
            logger.debug("Checkpoint commit-tree failed: %s", err)
            self._record_attempt("failed", working_dir, reason, detail=err or "commit-tree failed")
            return False

        publish_error = _publish_checkpoint(
            store,
            working_dir,
            dir_hash,
            new_sha,
            ref_commit if has_ref else None,
        )
        if publish_error:
            err = publish_error
            logger.debug("Checkpoint update-ref failed: %s", err)
            self._record_attempt("failed", working_dir, reason, detail=err or "update-ref failed")
            return False

        metadata["previous_commit"] = ref_commit if has_ref else None
        metadata["snapshot_format"] = "root-v1"
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
        """Force-stage only the explicit targets tracked by a targeted checkpoint."""
        for rel in target_info.get("rel", []):
            cancellation_checkpoint(get_interrupt_event())
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
        """Snapshot selected paths when the workspace is too large for full staging."""
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
        if not self._drop_credentials_from_index(store, working_dir, index_file):
            self._record_attempt(
                "failed",
                working_dir,
                reason,
                detail="could not remove credential paths from checkpoint index",
            )
            return False

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

        migration_error = _ensure_snapshot_refs(store, working_dir, dir_hash)
        if migration_error:
            self._record_attempt(
                "failed",
                working_dir,
                reason,
                detail=f"could not preserve legacy checkpoints: {migration_error}",
            )
            return False
        commit_args = [
            "commit-tree",
            tree_sha,
            "-m",
            reason,
            "-m",
            f"mclaw-checkpoint: {time.time_ns()}",
            "--no-gpg-sign",
        ]
        ok_commit, new_sha, err = _run_git(commit_args, store, working_dir, index_file=index_file)
        if not ok_commit or not new_sha:
            self._record_attempt("failed", working_dir, reason, detail=err or "commit-tree failed")
            return False

        publish_error = _publish_checkpoint(
            store,
            working_dir,
            dir_hash,
            new_sha,
            ref_commit if has_ref else None,
        )
        if publish_error:
            err = publish_error
            self._record_attempt("failed", working_dir, reason, detail=err or "update-ref failed")
            return False

        metadata["previous_commit"] = ref_commit if has_ref else None
        metadata["snapshot_format"] = "root-v1"
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

        Keeps source checkpoints available while excluding generated assets
        such as datasets, model weights, logs, and videos.
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
            cancellation_checkpoint(get_interrupt_event())
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
            cancellation_checkpoint(get_interrupt_event())
            chunk = oversize[i:i + BATCH]
            _run_git(
                ["rm", "--cached", "--quiet", "--"] + chunk,
                store, working_dir, index_file=index_file,
                allowed_returncodes={128},
            )

    def _drop_credentials_from_index(
        self,
        store: Path,
        working_dir: str,
        index_file: Path,
    ) -> bool:
        """Remove credentials that an older per-project index already tracked."""
        try:
            policy = RuntimeManager.current().paths
        except Exception as exc:
            logger.error("Could not load PathPolicy while sanitizing checkpoint index: %s", exc)
            return False
        ok, stdout, _ = _run_git(
            ["ls-files", "--cached", "-z"],
            store,
            working_dir,
            index_file=index_file,
        )
        if not ok:
            return False
        root = _normalize_path(working_dir)
        credentials = [
            rel
            for rel in stdout.split("\x00")
            if rel and policy.is_credential_path(root / rel)
        ]
        for offset in range(0, len(credentials), 200):
            removed, _, _ = _run_git(
                [
                    "--literal-pathspecs",
                    "rm",
                    "--cached",
                    "--quiet",
                    "--ignore-unmatch",
                    "--",
                    *credentials[offset:offset + 200],
                ],
                store,
                working_dir,
                index_file=index_file,
            )
            if not removed:
                return False
        return True

    def _prune(self, store: Path, working_dir: str, ref: str) -> None:
        """Drop old unpinned snapshot refs without rewriting commit hashes."""
        records = _list_snapshot_records(store, working_dir, _project_hash(working_dir))
        unpinned = [record for record in records if not record["pinned"]]
        drop = unpinned[:-self.max_snapshots]
        if not drop:
            return
        for record in drop:
            _delete_snapshot_ref(
                store,
                _snapshot_ref_name(_project_hash(working_dir), record["hash"]),
                record["hash"],
            )
        _run_git(
            ["reflog", "expire", "--expire=now", "--all"],
            store, working_dir,
        )
        _run_git(
            ["gc", "--prune=now", "--quiet"],
            store, working_dir, timeout=_GIT_TIMEOUT * 3,
        )

    def _enforce_size_cap(self, store: Path) -> None:
        """Drop oldest unpinned snapshots until the shared store fits."""
        if self.max_total_size_mb <= 0:
            return
        _prune_store_to_size(store, self.max_total_size_mb * 1024 * 1024)


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


def _delete_snapshot_ref(store: Path, ref: str, commit_hash: str) -> bool:
    if not _delete_ref(store, ref):
        return False
    ok, stdout, _ = _run_git(
        ["for-each-ref", "--format=%(refname)", "--points-at", commit_hash, _SNAPSHOT_REFS_PREFIX],
        store,
        str(store.parent),
        allowed_returncodes={128},
    )
    if ok and not stdout:
        try:
            _checkpoint_meta_path(store, commit_hash).unlink(missing_ok=True)
        except OSError:
            pass
    return True


def _prune_store_to_size(store: Path, cap_bytes: int) -> None:
    if cap_bytes <= 0 or _dir_size_bytes(store) <= cap_bytes:
        return

    candidates: List[Tuple[float, str, str]] = []
    for project in _list_projects(store):
        dir_hash = project.get("_hash") or ""
        if not dir_hash:
            continue
        project_workdir = str(project.get("workdir") or "")
        git_cwd = project_workdir if project_workdir and Path(project_workdir).exists() else str(store.parent)
        records = _list_snapshot_records(store, git_cwd, dir_hash)
        ok, head, _ = _run_git(
            ["rev-parse", "--verify", _ref_name(dir_hash) + "^{commit}"],
            store,
            git_cwd,
            allowed_returncodes={128},
        )
        current = head if ok else ""
        for record in records:
            if not record["pinned"] and record["hash"] != current:
                candidates.append((
                    record["created_at"],
                    _snapshot_ref_name(dir_hash, record["hash"]),
                    record["hash"],
                ))

    for _, ref, commit_hash in sorted(candidates):
        if not _delete_snapshot_ref(store, ref, commit_hash):
            continue
        _run_git(["reflog", "expire", "--expire=now", "--all"], store, str(store.parent))
        _run_git(
            ["gc", "--prune=now", "--quiet"],
            store,
            str(store.parent),
            timeout=_GIT_TIMEOUT * 3,
        )
        if _dir_size_bytes(store) <= cap_bytes:
            break


def _delete_project_refs(store: Path, dir_hash: str, working_dir: str) -> bool:
    git_cwd = working_dir if working_dir and Path(working_dir).exists() else str(store.parent)
    records = _list_snapshot_records(store, git_cwd, dir_hash)
    if any(record["pinned"] for record in records):
        return False
    _delete_ref(store, _ref_name(dir_hash))
    for record in records:
        _delete_snapshot_ref(
            store,
            _snapshot_ref_name(dir_hash, record["hash"]),
            record["hash"],
        )
    return True


def _expire_restore_intents(store: Path, cutoff: float) -> None:
    if cutoff <= 0:
        return
    for intent in _read_restore_intents(store):
        if intent.get("status") not in {"completed", "recovered"}:
            continue
        if float(intent.get("updated_at") or intent.get("created_at") or 0) >= cutoff:
            continue
        intent_id = str(intent.get("id") or "")
        commits = {
            str(intent.get("target_commit") or ""),
            str(intent.get("recovery_commit") or ""),
            *(
                str(commit_hash)
                for commit_hash in intent.get("target_commits") or []
                if commit_hash
            ),
        }
        released = True
        for commit_hash in commits:
            if commit_hash and not _unpin_checkpoint(store, commit_hash, intent_id):
                released = False
        if released:
            try:
                _restore_intent_path(store, intent_id).unlink(missing_ok=True)
            except OSError:
                pass


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

    Uses best-effort deletion so maintenance does not block normal runtime work.
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
        _expire_restore_intents(store, cutoff)
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
            if not _delete_project_refs(store, dir_hash, workdir):
                continue
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

        if max_total_size_mb > 0:
            _prune_store_to_size(store, max_total_size_mb * 1024 * 1024)

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
                pass  # Invalid marker; run maintenance normally.

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
# Public maintenance helpers used by checkpoint command handlers.
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
                git_cwd = workdir if workdir and Path(workdir).exists() else str(base)
                _ensure_snapshot_refs(store, git_cwd, dir_hash)
                ok, count_out, _ = _run_git(
                    ["for-each-ref", "--count=999999", "--format=%(refname)", _snapshot_ref_prefix(dir_hash)],
                    store, git_cwd,
                    allowed_returncodes={128},
                )
                commits = len(count_out.splitlines()) if ok and count_out else 0
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
    """Remove the entire checkpoint base. Irreversible.

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
