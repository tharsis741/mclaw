# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Low-level file operations routed through the active Runtime PathPolicy.

This module owns filesystem primitives only. Tool-facing schemas, delegation
checks, and response shaping live in file_tools, while every direct path touch
here is normalized or approved by Runtime PathPolicy before execution.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

from mclaw.runtime.manager import RuntimeManager

logger = logging.getLogger(__name__)


def _runtime():
    return RuntimeManager.current()


def _normalize_path(path: str) -> str:
    return str(_runtime().paths.normalize(path))


def _checked_path(path: str, action: str) -> Path:
    """Resolve a path only after the runtime policy authorizes the action."""
    decision = _runtime().paths.check(action, path)
    if not decision.allowed:
        raise PermissionError(decision.error_message())
    return decision.resolved


def _skip_chars(handle, count: int) -> None:
    """Advance a decoded text stream by character count rather than bytes."""
    remaining = max(0, int(count or 0))
    while remaining > 0:
        chunk = handle.read(min(remaining, 8192))
        if not chunk:
            return
        remaining -= len(chunk)


def read_file(path: str, offset: int = 0, limit: int | None = None) -> str:
    """Read UTF-8 text after runtime path approval."""
    abs_path = _checked_path(path, "read")
    try:
        abs_path.stat()
    except OSError as exc:
        raise OSError(f"Cannot stat file: {path}") from exc
    if limit is not None and limit <= 0:
        return ""
    try:
        with open(abs_path, "r", encoding="utf-8", errors="replace") as handle:
            if offset > 0:
                _skip_chars(handle, offset)
            return handle.read(limit)
    except OSError as exc:
        raise OSError(f"Cannot read file: {path}") from exc


def write_file(path: str, content: str) -> str:
    """Write UTF-8 content through a same-directory temporary file."""
    abs_path = _checked_path(path, "write")
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name = ""
    try:
        # The temporary file lives beside the destination so os.replace keeps
        # the final swap atomic on normal local filesystems.
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(abs_path.parent),
            delete=False,
        ) as tmp:
            tmp.write(content)
            tmp_name = tmp.name
        os.replace(tmp_name, abs_path)
    except OSError as exc:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:
                logger.debug("Failed to remove temporary file after write failure: %s", tmp_name, exc_info=True)
        raise OSError(f"Cannot write file: {path}") from exc
    return str(abs_path)


def patch_file(path: str, old_str: str, new_str: str) -> str:
    """Replace the first exact string occurrence after write-policy approval."""
    abs_path = _checked_path(path, "write")
    try:
        content = abs_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise OSError(f"Cannot read file for patching: {path}") from exc
    if old_str not in content:
        raise ValueError(f"String to replace not found in file: {path}")
    return write_file(str(abs_path), content.replace(old_str, new_str, 1))


def edit_file(path: str, old_block: str, new_block: str) -> str:
    """Replace one exact multi-line block after write-policy approval."""
    abs_path = _checked_path(path, "write")
    try:
        content = abs_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise OSError(f"Cannot read file for editing: {path}") from exc
    idx = content.find(old_block)
    if idx == -1:
        raise ValueError(
            f"Could not find block to edit in {path}. "
            "The file content may have changed."
        )
    return write_file(str(abs_path), content[:idx] + new_block + content[idx + len(old_block):])


def delete_file(path: str) -> str:
    """Delete a single file; directory removal is intentionally out of scope."""
    abs_path = _checked_path(path, "write")
    if not abs_path.exists():
        raise FileNotFoundError(f"File not found: {path}")
    if abs_path.is_dir():
        raise IsADirectoryError(f"Refusing to delete directory with delete_file: {path}")
    try:
        abs_path.unlink()
    except OSError as exc:
        raise OSError(f"Cannot delete file: {path}") from exc
    return str(abs_path)


def search_files(
    directory: str,
    pattern: str,
    file_pattern: str | None = None,
    limit: int | None = None,
) -> str:
    """Delegate content search to the active runtime search provider."""
    return _runtime().search.search(
        directory,
        pattern,
        file_pattern=file_pattern,
        limit=limit or 50,
    )


def list_directory(path: str) -> tuple[str, str | None]:
    """Return a bounded tabular directory listing plus an optional truncation hint."""
    abs_path = _checked_path(path, "list")
    if not abs_path.is_dir():
        raise NotADirectoryError(f"Not a directory: {path}")
    max_entries = 200
    rows = ["name\tsize\tmtime\ttype"]
    hint = None
    try:
        entries = sorted(abs_path.iterdir(), key=lambda item: item.name)
        if len(entries) > max_entries:
            hint = (
                f"Directory listing truncated at {max_entries} entries. "
                f"{len(entries) - max_entries} entries omitted."
            )
            entries = entries[:max_entries]
        for entry in entries:
            try:
                stat = entry.stat()
                rows.append(
                    f"{entry.name}\t{stat.st_size}\t{int(stat.st_mtime)}\t"
                    f"{'dir' if entry.is_dir() else 'file'}"
                )
            except OSError:
                rows.append(f"{entry.name}\t?\t?\t?")
    except OSError as exc:
        raise OSError(f"Cannot list directory: {path}") from exc
    return "\n".join(rows), hint
