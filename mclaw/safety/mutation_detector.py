# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Mutation intent detection for file-safety operations."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from mclaw.safety.path_resolver import extract_absolute_paths_from_command, repair_common_mojibake

_DESTRUCTIVE_TERMINAL_PATTERNS = re.compile(
    r"""(?:^|\s|&&|\|\||;|`)(?:
        rm\s|rmdir\s|del\s|erase\s|rd\s|
        cp\s|copy\s|install\s|
        mv\s|move\s|ren\s|rename\s|
        sed\s+-i|
        truncate\s|
        dd\s|
        shred\s|
        remove-item\b|ri\b|
        move-item\b|mi\b|
        copy-item\b|ci\b|
        rename-item\b|rni\b|
        new-item\b|ni\b|
        set-content\b|sc\b|
        add-content\b|ac\b|
        clear-content\b|clc\b|
        out-file\b|
        git\s+(?:reset|clean|checkout)\s
    )""",
    re.IGNORECASE | re.VERBOSE,
)
_REDIRECT_OVERWRITE = re.compile(r'[^>]>[^>]|^>[^>]')


@dataclass
class MutationIntent:
    """Normalized description of whether a tool call may change files."""

    mutates: bool
    action: str
    raw_command: str = ""
    target_paths: list[str] | None = None
    confidence: str = "detected"

    def __post_init__(self) -> None:
        if self.target_paths is None:
            self.target_paths = []


def detect_mutation(tool_name: str, arguments: dict[str, Any]) -> MutationIntent:
    """Classify tool-call arguments before safety policy and checkpointing."""
    if tool_name in {"write_file", "patch", "edit_file", "delete_file"}:
        path = arguments.get("path")
        return MutationIntent(True, tool_name, target_paths=[path] if path else [])
    if tool_name == "skill_manage":
        action = str(arguments.get("action") or "skill_manage")
        return MutationIntent(action not in {"list", "view", "search"}, action)
    if tool_name != "terminal":
        return MutationIntent(False, tool_name)

    raw = repair_common_mojibake(str(arguments.get("command") or ""))
    if not is_destructive_terminal_command(raw):
        return MutationIntent(False, "read", raw_command=raw)
    target_paths = extract_absolute_paths_from_command(raw)
    return MutationIntent(
        True,
        terminal_action(raw),
        raw_command=raw,
        target_paths=target_paths,
        confidence="targeted" if target_paths else "workspace",
    )


def is_destructive_terminal_command(command: str) -> bool:
    """Detect shell forms that can mutate files even when paths are implicit."""
    if not command:
        return False
    return bool(_DESTRUCTIVE_TERMINAL_PATTERNS.search(command) or _REDIRECT_OVERWRITE.search(command))


def terminal_action(command: str) -> str:
    """Map a destructive terminal command into the policy action vocabulary."""
    command = repair_common_mojibake(command or "").lower()
    if re.search(r'\b(rm|del|erase|remove-item|ri|rmdir|rd)\b', command):
        if re.search(r'(-r|-recurse|/s)\b', command):
            return "directory_delete"
        return "delete"
    if re.search(r'\b(mv|move|ren|rename|move-item|rename-item)\b', command):
        return "move"
    if re.search(r'\b(cp|copy|copy-item)\b', command):
        return "copy"
    if _REDIRECT_OVERWRITE.search(command) or re.search(r'\b(set-content|out-file|truncate)\b', command):
        return "overwrite"
    if re.search(r'\b(add-content|new-item)\b', command):
        return "write"
    return "unknown_destructive"
