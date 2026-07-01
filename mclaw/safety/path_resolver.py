# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Path and workspace resolution for file-safety operations."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

_QUOTED_WINDOWS_ABS_PATH_RE = re.compile(r'["\']([A-Za-z]:[\\/][^"\']+)["\']')
_WINDOWS_ABS_PATH_RE = re.compile(r'[A-Za-z]:[\\/][^\s"\'<>|`]+(?:[\\/][^\s"\'<>|`]+)*')
_QUOTED_MSYS_ABS_PATH_RE = re.compile(r'["\'](/[A-Za-z]/[^"\']+)["\']')
_MSYS_ABS_PATH_RE = re.compile(r'(?<!\S)/[A-Za-z]/[^\s"\'<>|`]+')
_MOJIBAKE_MARKERS = ("娴嬭瘯", "娴嬭", "瘯")


def repair_common_mojibake(text: str) -> str:
    """Repair known Windows console mojibake in command and path text."""
    if not text or not any(marker in text for marker in _MOJIBAKE_MARKERS):
        return text
    try:
        return text.encode("gbk").decode("utf-8")
    except UnicodeError:
        return text


def msys_to_windows_path(path: str) -> str:
    """Convert Git Bash-style drive paths to Windows paths."""
    if len(path) >= 3 and path[0] == "/" and path[2] == "/":
        return f"{path[1].upper()}:/{path[3:]}"
    return path


def normalize_path(path_value: str, cwd: str | None = None) -> str:
    """Normalize shell-facing path text into an absolute comparison path."""
    value = repair_common_mojibake(str(path_value or "")).strip().strip("'\"")
    if not value:
        return ""
    value = msys_to_windows_path(value)
    path = Path(value).expanduser()
    if not path.is_absolute() and cwd:
        path = Path(cwd).expanduser() / path
    try:
        return str(path.resolve())
    except OSError:
        return str(path)


def is_mclaw_runtime_path(path_value: Any) -> bool:
    """Treat M-Claw runtime internals as unsafe defaults for user workspace scope."""
    try:
        from mclaw.runtime.manager import RuntimeManager

        return RuntimeManager.current().paths.is_runtime_internal_path(path_value)
    except Exception:
        return False


def extract_absolute_paths_from_command(command: str) -> list[str]:
    """Extract explicit absolute paths from Windows and Git Bash command text."""
    command = repair_common_mojibake(command or "")
    paths: list[str] = []
    seen = set()
    quoted_spans = []
    for match in _QUOTED_WINDOWS_ABS_PATH_RE.finditer(command):
        _append_path(paths, seen, match.group(1))
        quoted_spans.append(match.span())
    for match in _QUOTED_MSYS_ABS_PATH_RE.finditer(command):
        _append_path(paths, seen, msys_to_windows_path(match.group(1)))
        quoted_spans.append(match.span())

    def inside_quote(start: int, end: int) -> bool:
        return any(start >= q_start and end <= q_end for q_start, q_end in quoted_spans)

    for match in _WINDOWS_ABS_PATH_RE.finditer(command):
        if not inside_quote(match.start(), match.end()):
            _append_path(paths, seen, match.group(0))
    for match in _MSYS_ABS_PATH_RE.finditer(command):
        if not inside_quote(match.start(), match.end()):
            _append_path(paths, seen, msys_to_windows_path(match.group(0)))
    return paths


def resolve_workspace(
    *,
    explicit_workdir: str = "",
    target_paths: Iterable[str] | None = None,
    terminal_cwd: str = "",
    launch_cwd: str = "",
    recent_checkpoint_dir: str = "",
    fallback_cwd: str = "",
) -> str:
    """Choose the best workspace root from explicit targets and runtime fallbacks."""
    candidates = [
        explicit_workdir,
        _first_target_parent(target_paths),
        terminal_cwd if terminal_cwd and not is_mclaw_runtime_path(terminal_cwd) else "",
        launch_cwd if launch_cwd and not is_mclaw_runtime_path(launch_cwd) else "",
        fallback_cwd if fallback_cwd and not is_mclaw_runtime_path(fallback_cwd) else "",
        recent_checkpoint_dir,
    ]
    for candidate in candidates:
        if candidate:
            return normalize_path(candidate)
    return normalize_path(os.getcwd())


def _first_target_parent(target_paths: Iterable[str] | None) -> str:
    for target in target_paths or []:
        normalized = normalize_path(str(target))
        if normalized:
            path = Path(normalized)
            return str(path if path.is_dir() else path.parent)
    return ""


def _append_path(paths: list[str], seen: set[str], value: str) -> None:
    cleaned = normalize_path(str(value).strip().rstrip(";,"))
    if cleaned and cleaned not in seen:
        paths.append(cleaned)
        seen.add(cleaned)
