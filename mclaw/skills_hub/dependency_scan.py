# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Dependency hint scanning for external Skill installs."""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any, Iterator

from mclaw.agent.skill_utils import parse_frontmatter
from mclaw.tools.cancellation import cancellation_checkpoint
from mclaw.tools.interrupt import get_interrupt_event

ENV_RE = re.compile(r"\b[A-Z_][A-Z0-9_]{0,63}\b")
ENV_SUFFIXES = ("API_KEY", "_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_CLIENT_ID", "_ROBOT_CODE")
SCAN_EXTENSIONS = {".md", ".py", ".js", ".ts", ".tsx", ".jsx", ".sh", ".ps1", ".json", ".yaml", ".yml", ".toml"}
SKIP_PARTS = {".git", "__pycache__", "node_modules", ".venv", "venv", "dist", "build"}


def _looks_like_secret_env(name: str) -> bool:
    """Classify environment variable names that likely gate external services."""

    if name in {"PATH", "HOME", "USER", "USERNAME", "SHELL", "PWD", "OLDPWD", "LANG"}:
        return False
    return name.endswith(ENV_SUFFIXES) or name in {
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "DASHSCOPE_API_KEY",
        "QWEN_API_KEY",
        "TAVILY_API_KEY",
    }


def _collect_env_values(
    value: Any,
    cancel_event: threading.Event | None = None,
) -> set[str]:
    """Collect secret-like environment names from structured metadata only."""

    cancellation_checkpoint(cancel_event)
    found: set[str] = set()
    if isinstance(value, str):
        candidate = value.strip().upper()
        if ENV_RE.fullmatch(candidate) and _looks_like_secret_env(candidate):
            found.add(candidate)
        return found
    if isinstance(value, dict):
        for item in value.values():
            cancellation_checkpoint(cancel_event)
            found.update(_collect_env_values(item, cancel_event=cancel_event))
        return found
    if isinstance(value, (list, tuple, set)):
        for item in value:
            cancellation_checkpoint(cancel_event)
            found.update(_collect_env_values(item, cancel_event=cancel_event))
    return found


def _scan_frontmatter(
    skill_md: Path,
    cancel_event: threading.Event | None = None,
) -> set[str]:
    """Extract declared dependency hints from SKILL.md frontmatter."""

    cancellation_checkpoint(cancel_event)
    if not skill_md.is_file():
        return set()
    try:
        frontmatter, _body = parse_frontmatter(skill_md.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return set()
    cancellation_checkpoint(cancel_event)
    return _collect_env_values(frontmatter, cancel_event=cancel_event)


def _iter_scan_files(
    root: Path,
    cancel_event: threading.Event | None = None,
) -> Iterator[Path]:
    """Yield small text-like package files while skipping caches and vendored trees."""

    for path in root.rglob("*"):
        cancellation_checkpoint(cancel_event)
        if any(part in SKIP_PARTS for part in path.parts):
            continue
        try:
            if not path.is_file() or path.stat().st_size > 512_000:
                continue
        except OSError:
            continue
        if path.name == "SKILL.md" or path.suffix.lower() in SCAN_EXTENSIONS:
            yield path


def scan_skill_dependencies(
    package_root: Path,
    cancel_event: threading.Event | None = None,
) -> list[dict[str, Any]]:
    """Return dependency hints for a Skill package without collecting secret values.

    The scanner records only variable names and the files that mention them; it never
    expands environment variables or reads credentials from the host process.
    """
    cancel_event = cancel_event or get_interrupt_event()
    cancellation_checkpoint(cancel_event)
    root = Path(package_root)
    candidates: dict[str, dict[str, Any]] = {}

    for env_var in _scan_frontmatter(root / "SKILL.md", cancel_event=cancel_event):
        cancellation_checkpoint(cancel_event)
        candidates.setdefault(
            env_var,
            {"env_var": env_var, "source": "frontmatter", "confidence": "high", "files": []},
        )
        candidates[env_var]["files"].append("SKILL.md")

    for path in _iter_scan_files(root, cancel_event=cancel_event):
        cancellation_checkpoint(cancel_event)
        rel = path.relative_to(root).as_posix()
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        cancellation_checkpoint(cancel_event)
        if path.suffix.lower() == ".json":
            try:
                payload = json.loads(text)
                cancellation_checkpoint(cancel_event)
                for env_var in _collect_env_values(payload, cancel_event=cancel_event):
                    cancellation_checkpoint(cancel_event)
                    item = candidates.setdefault(
                        env_var,
                        {"env_var": env_var, "source": "metadata", "confidence": "medium", "files": []},
                    )
                    if rel not in item["files"]:
                        item["files"].append(rel)
            except json.JSONDecodeError:
                continue
        for env_var in sorted(set(ENV_RE.findall(text))):
            cancellation_checkpoint(cancel_event)
            if not _looks_like_secret_env(env_var):
                continue
            item = candidates.setdefault(
                env_var,
                {"env_var": env_var, "source": "static_scan", "confidence": "medium", "files": []},
            )
            if rel not in item["files"]:
                item["files"].append(rel)

    cancellation_checkpoint(cancel_event)
    return sorted(candidates.values(), key=lambda item: item["env_var"])
