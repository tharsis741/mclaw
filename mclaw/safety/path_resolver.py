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
_SHELL_TOKEN_RE = re.compile(r'(&&|\|\||[;|])|"([^"]+)"|\'([^\']+)\'|([^\s;&|<>`]+)')
_REDIRECT_TARGET_RE = re.compile(
    r'(?:^|[\s;&|])(?:\d*)>{1,2}(?![>&])\s*(?:"([^"]+)"|\'([^\']+)\'|([^\s;&|]+))'
)
_SHELL_SEPARATORS = {"|", "&&", "||", ";"}
_WINDOWS_SWITCHES = {"/a", "/b", "/d", "/f", "/q", "/s", "/y", "/-y"}


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
    home = str(Path.home())
    value = re.sub(
        r"(?i)\$env:([A-Z_][A-Z0-9_]*)",
        lambda match: os.environ.get(match.group(1), match.group(0)),
        value,
    )
    value = value.replace("${HOME}", home).replace("$HOME", home)
    value = os.path.expandvars(value)
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


def extract_mutation_targets_from_command(command: str, workdir: str = "") -> list[str]:
    """Return the deduplicated paths from mutation target/action pairs."""
    pairs = extract_mutation_target_actions_from_command(command, workdir)
    return _clean_targets(target for _action, target in pairs)


def extract_mutation_target_actions_from_command(command: str, workdir: str = "") -> list[tuple[str, str]]:
    """Extract filesystem targets changed by common shell mutation commands.

    This is intentionally a small operand parser, not a shell interpreter. Any
    destructive form it cannot resolve remains targetless and is blocked by the
    risk policy instead of guessing.
    """
    command = repair_common_mojibake(command or "")
    tokens = _shell_tokens(command)
    targets = [("overwrite", target) for target in _redirection_targets(command)]

    copy_verbs = {"cp", "copy", "copy-item", "ci", "install"}
    move_verbs = {"mv", "move", "move-item", "mi", "ren", "rename", "rename-item", "rni"}
    delete_verbs = {"rm", "rmdir", "del", "erase", "rd", "remove-item", "ri", "shred"}
    content_verbs = {
        "set-content",
        "sc",
        "add-content",
        "ac",
        "clear-content",
        "clc",
        "out-file",
        "new-item",
        "ni",
        "truncate",
    }

    for index, _verb, operands in _verb_operands(tokens, copy_verbs):
        destination = _parameter_value(tokens, index, {"-destination", "-dest"})
        destination = destination or _option_value(tokens, index, {"-t", "--target-directory"})
        if not destination and operands:
            destination = operands[-1]
        if destination:
            action = "overwrite" if _segment_has_option(tokens, index, {"-T", "--no-target-directory"}) else "copy"
            targets.append((action, destination))

    for index, _verb, operands in _verb_operands(tokens, move_verbs):
        destination = _parameter_value(tokens, index, {"-destination", "-dest"})
        destination = destination or _option_value(tokens, index, {"-t", "--target-directory"})
        if not destination and operands:
            destination = operands[-1]
            sources = operands[:-1]
        else:
            sources = list(operands)
            if destination in sources:
                sources.remove(destination)
        targets.extend(("move", source) for source in sources)
        if destination:
            action = "overwrite" if _segment_has_option(tokens, index, {"-T", "--no-target-directory"}) else "move_destination"
            targets.append((action, destination))

    for _index, _verb, operands in _verb_operands(tokens, delete_verbs):
        targets.extend(("delete", operand) for operand in operands)

    for index, verb, operands in _verb_operands(tokens, content_verbs):
        target = _parameter_value(tokens, index, {"-path", "-literalpath", "-filepath"})
        if not target and operands:
            target = operands[-1] if verb == "truncate" else operands[0]
        if target:
            action = "write" if verb in {"add-content", "ac", "new-item", "ni"} else "overwrite"
            targets.append((action, target))

    for _index, _verb, operands in _verb_operands(tokens, {"sed"}):
        if re.search(r"(?:^|\s)-i(?:\s|$)", command, re.IGNORECASE) and operands:
            targets.append(("overwrite", operands[-1]))

    for token in tokens:
        if token.lower().startswith("of="):
            targets.append(("overwrite", token[3:]))

    if re.search(r"\bgit\s+(?:reset|clean|checkout)\b", command, re.IGNORECASE):
        targets.append(("unknown_destructive", workdir or "."))

    return _clean_target_actions(targets)


def _shell_tokens(command: str) -> list[str]:
    tokens: list[str] = []
    for match in _SHELL_TOKEN_RE.finditer(command or ""):
        token = next((group for group in match.groups() if group), "")
        if token:
            tokens.append(token.strip())
    return tokens


def _command_name(token: str) -> str:
    cleaned = str(token or "").strip().strip("\"'").replace("\\", "/")
    name = cleaned.rsplit("/", 1)[-1].lower()
    return name[:-4] if name.endswith(".exe") else name


def _operands_after(tokens: list[str], index: int) -> list[str]:
    operands: list[str] = []
    for token in tokens[index + 1:]:
        cleaned = str(token or "").strip().strip("\"'")
        if not cleaned:
            continue
        lowered = cleaned.lower()
        if lowered in _SHELL_SEPARATORS:
            break
        if re.match(r"^\d*>", cleaned) or cleaned.startswith(">"):
            continue
        if cleaned.startswith("-") or lowered in _WINDOWS_SWITCHES:
            continue
        operands.append(cleaned)
    return operands


def _verb_operands(tokens: list[str], verbs: set[str]):
    for index, token in enumerate(tokens):
        verb = _command_name(token)
        if verb in verbs:
            yield index, verb, _operands_after(tokens, index)


def _parameter_value(tokens: list[str], index: int, names: set[str]) -> str:
    for offset in range(index + 1, len(tokens) - 1):
        token = tokens[offset]
        if token in _SHELL_SEPARATORS:
            break
        if token.lower() in names:
            return tokens[offset + 1].strip().strip("\"'")
    return ""


def _option_value(tokens: list[str], index: int, names: set[str]) -> str:
    for offset in range(index + 1, len(tokens)):
        token = tokens[offset]
        if token in _SHELL_SEPARATORS:
            break
        lowered = token.lower()
        if lowered in names and offset + 1 < len(tokens):
            return tokens[offset + 1].strip().strip("\"'")
        for name in names:
            prefix = name + "="
            if lowered.startswith(prefix):
                return token[len(prefix):].strip().strip("\"'")
    return ""


def _segment_has_option(tokens: list[str], index: int, names: set[str]) -> bool:
    for token in tokens[index + 1:]:
        if token in _SHELL_SEPARATORS:
            break
        if token in names:
            return True
    return False


def _redirection_targets(command: str) -> list[str]:
    targets: list[str] = []
    for match in _REDIRECT_TARGET_RE.finditer(command or ""):
        target = next((group for group in match.groups() if group), "")
        if target and not target.startswith("&"):
            targets.append(target)
    return targets


def _clean_targets(targets: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    cleaned_targets: list[str] = []
    for target in targets:
        cleaned = _clean_target(target)
        if not cleaned:
            continue
        key = os.path.normcase(os.path.normpath(cleaned))
        if key not in seen:
            seen.add(key)
            cleaned_targets.append(cleaned)
    return cleaned_targets


def _clean_target_actions(targets: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    seen: set[tuple[str, str]] = set()
    cleaned_targets: list[tuple[str, str]] = []
    for action, target in targets:
        cleaned = _clean_target(target)
        key = (action, os.path.normcase(os.path.normpath(cleaned))) if cleaned else None
        if key is not None and key not in seen:
            seen.add(key)
            cleaned_targets.append((action, cleaned))
    return cleaned_targets


def _clean_target(target: str) -> str:
    cleaned = str(target or "").strip().strip("\"'").rstrip(".,;)]}")
    expanded = re.sub(
        r"(?i)\$env:([A-Z_][A-Z0-9_]*)",
        lambda match: os.environ.get(match.group(1), match.group(0)),
        cleaned,
    )
    expanded = expanded.replace("${HOME}", str(Path.home())).replace("$HOME", str(Path.home()))
    expanded = os.path.expandvars(expanded)
    if not cleaned or re.search(r"\$\(|`", cleaned):
        return ""
    if re.search(r"\$(?:\{)?[A-Za-z_]", expanded) or re.search(r"%[A-Za-z_][A-Za-z0-9_]*%", expanded):
        return ""
    return cleaned


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
