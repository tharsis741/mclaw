# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Dependency hint scanning for external Skill installs."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterator

from mclaw.agent.skill_utils import parse_frontmatter

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


def _collect_env_values(value: Any) -> set[str]:
    """Collect secret-like environment names from structured metadata only."""

    found: set[str] = set()
    if isinstance(value, str):
        candidate = value.strip().upper()
        if ENV_RE.fullmatch(candidate) and _looks_like_secret_env(candidate):
            found.add(candidate)
        return found
    if isinstance(value, dict):
        for item in value.values():
            found.update(_collect_env_values(item))
        return found
    if isinstance(value, (list, tuple, set)):
        for item in value:
            found.update(_collect_env_values(item))
    return found


def _scan_frontmatter(skill_md: Path) -> set[str]:
    """Extract declared dependency hints from SKILL.md frontmatter."""

    if not skill_md.is_file():
        return set()
    try:
        frontmatter, _body = parse_frontmatter(skill_md.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return set()
    return _collect_env_values(frontmatter)


def _iter_scan_files(root: Path) -> Iterator[Path]:
    """Yield small text-like package files while skipping caches and vendored trees."""

    for path in root.rglob("*"):
        if any(part in SKIP_PARTS for part in path.parts):
            continue
        try:
            if not path.is_file() or path.stat().st_size > 512_000:
                continue
        except OSError:
            continue
        if path.name == "SKILL.md" or path.suffix.lower() in SCAN_EXTENSIONS:
            yield path


def scan_skill_dependencies(package_root: Path) -> list[dict[str, Any]]:
    """Return dependency hints for a Skill package without collecting secret values.

    The scanner records only variable names and the files that mention them; it never
    expands environment variables or reads credentials from the host process.
    """
    root = Path(package_root)
    candidates: dict[str, dict[str, Any]] = {}

    for env_var in _scan_frontmatter(root / "SKILL.md"):
        candidates.setdefault(
            env_var,
            {"env_var": env_var, "source": "frontmatter", "confidence": "high", "files": []},
        )
        candidates[env_var]["files"].append("SKILL.md")

    for path in _iter_scan_files(root):
        rel = path.relative_to(root).as_posix()
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if path.suffix.lower() == ".json":
            try:
                for env_var in _collect_env_values(json.loads(text)):
                    item = candidates.setdefault(
                        env_var,
                        {"env_var": env_var, "source": "metadata", "confidence": "medium", "files": []},
                    )
                    if rel not in item["files"]:
                        item["files"].append(rel)
            except json.JSONDecodeError:
                continue
        for env_var in sorted(set(ENV_RE.findall(text))):
            if not _looks_like_secret_env(env_var):
                continue
            item = candidates.setdefault(
                env_var,
                {"env_var": env_var, "source": "static_scan", "confidence": "medium", "files": []},
            )
            if rel not in item["files"]:
                item["files"].append(rel)

    return sorted(candidates.values(), key=lambda item: item["env_var"])
