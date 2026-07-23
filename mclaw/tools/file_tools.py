# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Register file-system tools and enforce their runtime safety boundaries.

This module exposes the public tool schemas and handlers for read, write,
patch, edit, delete, search, and directory listing operations. Low-level file
IO remains in file_operations; this layer owns tool-facing limits,
Skill-store protection, and registry registration.
"""

import json
import logging
import os
from pathlib import Path

from mclaw.tools import file_operations as ops
from mclaw.tools.cancellation import cancellation_checkpoint
from mclaw.tools.interrupt import get_interrupt_event
from mclaw.tools.registry import registry, tool_error
from mclaw.constants import get_skills_dir
from mclaw.skills_hub.paths import get_skill_drafting_dir

logger = logging.getLogger(__name__)

_PRUNED_WRITE_CONTENT_PREFIX = "[MCLAW_INTERNAL_WRITE_CONTENT_PRUNED:"
_LEGACY_PRUNED_WRITE_CONTENT_PREFIX = (
    "[Earlier content argument cleared after successful write; "
)


def _is_pruned_write_content(content: str) -> bool:
    """Reject internal history markers if a model copies them into a new write."""
    marker = str(content or "").strip()
    return marker.startswith(_PRUNED_WRITE_CONTENT_PREFIX) or (
        marker.startswith(_LEGACY_PRUNED_WRITE_CONTENT_PREFIX)
        and marker.endswith(" chars]")
    )


def _is_path_within(path: Path, root: Path) -> bool:
    """Return whether path is equal to or nested under root without prefix leaks."""
    try:
        return path == root or path.is_relative_to(root)
    except ValueError:
        return False
    except AttributeError:
        try:
            return os.path.commonpath([
                os.path.normcase(str(path)),
                os.path.normcase(str(root)),
            ]) == os.path.normcase(str(root))
        except ValueError:
            return False


def _skill_store_mutation_error(file_path: str, tool_name: str) -> str | None:
    """Return an error when a generic file mutation targets Skill storage."""
    try:
        target = Path(ops._normalize_path(str(file_path or "")))
        protected_roots = [
            Path(ops._normalize_path(str(get_skills_dir()))),
            Path(ops._normalize_path(str(get_skill_drafting_dir()))),
        ]
    except Exception:
        logger.debug("Skill storage protection check failed for %s", tool_name, exc_info=True)
        return None

    for root in protected_roots:
        if _is_path_within(target, root):
            return (
                f"Skill package storage is managed by skill_manage. "
                f"Do not use {tool_name} under {root}. "
                "For new Skills, call skill_manage(action=\"create_scaffold\") first. "
                "For existing Skills, use skill_manage(action=\"write_file\"|\"remove_file\") "
                "for non-system Skill files, skill_manage(action=\"patch\"|\"edit\") for SKILL.md, "
                "and skill_manage(action=\"validate\") before reporting creation success."
            )
    return None


# read_file tool.

# Safety cap for a single read_file result.
# 100K characters is roughly 25K-50K tokens across common tokenizers. Files
# larger than this should be read in targeted offset+limit ranges.
_READ_FILE_MAX_CHARS = 100_000

# Default read size when the model does not provide an explicit limit.
_READ_FILE_DEFAULT_LIMIT = 50_000


def _coerce_read_limit(value, default: int = _READ_FILE_MAX_CHARS) -> int:
    """Clamp configured read limits to the tool's hard safety ceiling."""
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(limit, _READ_FILE_MAX_CHARS))


def _configured_read_limit(parent_agent=None) -> int:
    """Read the per-agent file read ceiling without exceeding module policy."""
    config = getattr(parent_agent, "config", None) if parent_agent is not None else None
    if isinstance(config, dict) and config.get("file_read_max_chars") is not None:
        return _coerce_read_limit(config.get("file_read_max_chars"))
    return _READ_FILE_MAX_CHARS


def read_file_tool(
    path: str,
    offset: int = 0,
    limit: int | None = None,
    task_id: str = "default",
    max_chars: int | None = None,
) -> str:
    """Read a file with optional character offset and limit.

    Args:
        path: Absolute or relative file path to read.
        offset: Character offset to start reading from (default: 0).
        limit: Maximum characters to read (default: 50,000, hard cap: 100,000).

    Returns:
        JSON string with content, path, and staleness info.
    """
    from mclaw.tools.read_tracker import check_dedup, record_read

    try:
        # Dedup check.
        hard_limit = _coerce_read_limit(max_chars)
        default_limit = min(_READ_FILE_DEFAULT_LIMIT, hard_limit)
        dedup_msg = check_dedup(path, offset, limit or default_limit, task_id=task_id)
        if dedup_msg:
            return json.dumps({
                "content": dedup_msg,
                "path": path,
                "dedup": True,
            }, ensure_ascii=False)

        if limit is None:
            # Probe whether the remaining content exceeds the hard safety cap.
            probe_limit = hard_limit + 1
            content = ops.read_file(path, offset=offset, limit=probe_limit)

            if len(content) > hard_limit:
                return json.dumps({
                    "error": (
                        f"Read produced {len(content):,} characters which exceeds "
                        f"the safety limit ({hard_limit:,} chars). "
                        "Use offset and limit to read a smaller range."
                    ),
                }, ensure_ascii=False)

            # Apply the default read window for normal responses.
            effective_limit = default_limit
            hint = None
            if len(content) > effective_limit:
                content = content[:effective_limit]
                hint = (
                    f"Output truncated at {effective_limit:,} characters. "
                    f"Use offset={offset + effective_limit} to continue reading."
                )

            result = {
                "content": content,
                "path": str(path),
                "chars_read": len(content),
            }
            if hint:
                result["_hint"] = hint

        else:
            # Model supplied an explicit limit — respect it but never exceed the hard cap.
            effective_limit = min(limit, hard_limit)
            content = ops.read_file(path, offset=offset, limit=effective_limit)

            result = {
                "content": content,
                "path": str(path),
                "chars_read": len(content),
            }
            if len(content) >= effective_limit:
                result["_hint"] = (
                    f"Output truncated at {effective_limit:,} characters. "
                    f"Use offset={offset + effective_limit} to continue reading."
                )

        # Track consecutive reads to prevent tool-call loops.
        count, should_block = record_read(path, offset, limit or default_limit, task_id=task_id)
        if should_block:
            return json.dumps({
                "error": (
                    f"Repeated read blocked after {count} identical attempts. "
                    "The content has not changed. Use the earlier read_file result "
                    "and continue the task."
                ),
                "path": path,
                "already_read": count,
            }, ensure_ascii=False)
        elif count >= 3:
            result["_warning"] = (
                f"You have read this exact file region {count} times consecutively. "
                "The content has not changed since your last read. Use the information you already have. "
                "If you are stuck in a loop, stop reading and proceed with writing or responding."
            )

        return json.dumps(result, ensure_ascii=False)

    except OSError as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# write_file tool.

def write_file_tool(path: str, content: str) -> str:
    """Write content to a file atomically.

    Args:
        path: File path to write.
        content: Content to write.

    Returns:
        JSON string with written path and UTF-8 byte count.
    """
    if _is_pruned_write_content(content):
        return json.dumps({
            "success": False,
            "error": (
                "Refusing to write an internal context-pruning marker. "
                "The earlier content was already written successfully; use read_file "
                "to inspect it, or regenerate the intended content for a new write."
            ),
        }, ensure_ascii=False)

    blocked = _skill_store_mutation_error(path, "write_file")
    if blocked:
        return json.dumps({"success": False, "error": blocked}, ensure_ascii=False)
    cancel_event = get_interrupt_event()
    try:
        cancellation_checkpoint(cancel_event)
        written = ops.write_file(path, content, cancel_event=cancel_event)
        return json.dumps({
            "path": written,
            "bytes_written": len(content.encode("utf-8")),
        }, ensure_ascii=False)
    except InterruptedError as exc:
        return tool_error(str(exc), success=False, status="cancelled", interrupted=True)
    except OSError as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# patch tool.

def patch_tool(path: str, old_str: str, new_str: str) -> str:
    """Replace the first occurrence of old_str with new_str in a file.

    Args:
        path: File path to patch.
        old_str: Exact string to find and replace.
        new_str: Replacement string.

    Returns:
        JSON string with patched path.
    """
    blocked = _skill_store_mutation_error(path, "patch")
    if blocked:
        return json.dumps({"success": False, "error": blocked}, ensure_ascii=False)
    cancel_event = get_interrupt_event()
    try:
        cancellation_checkpoint(cancel_event)
        patched = ops.patch_file(path, old_str, new_str, cancel_event=cancel_event)
        return json.dumps({"path": patched}, ensure_ascii=False)
    except InterruptedError as exc:
        return tool_error(str(exc), success=False, status="cancelled", interrupted=True)
    except (OSError, ValueError) as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# edit_file tool.

def edit_file_tool(path: str, old_block: str, new_block: str) -> str:
    """Replace one exact old_block with new_block.

    More robust for multi-line literal replacements than patch.

    Args:
        path: File path to edit.
        old_block: Block of text to find and replace.
        new_block: Replacement block.

    Returns:
        JSON string with edited path.
    """
    blocked = _skill_store_mutation_error(path, "edit_file")
    if blocked:
        return json.dumps({"success": False, "error": blocked}, ensure_ascii=False)
    cancel_event = get_interrupt_event()
    try:
        cancellation_checkpoint(cancel_event)
        edited = ops.edit_file(path, old_block, new_block, cancel_event=cancel_event)
        return json.dumps({"path": edited}, ensure_ascii=False)
    except InterruptedError as exc:
        return tool_error(str(exc), success=False, status="cancelled", interrupted=True)
    except (OSError, ValueError) as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# delete_file tool.

def delete_file_tool(path: str) -> str:
    """Delete one file after Runtime PathPolicy approval."""
    blocked = _skill_store_mutation_error(path, "delete_file")
    if blocked:
        return json.dumps({"success": False, "error": blocked}, ensure_ascii=False)
    cancel_event = get_interrupt_event()
    try:
        cancellation_checkpoint(cancel_event)
        deleted = ops.delete_file(path, cancel_event=cancel_event)
        return json.dumps({"path": deleted, "deleted": True}, ensure_ascii=False)
    except InterruptedError as exc:
        return tool_error(str(exc), success=False, status="cancelled", interrupted=True)
    except OSError as e:
        return json.dumps({"error": str(e), "success": False}, ensure_ascii=False)


# search_files tool.

def search_files_tool(
    directory: str,
    pattern: str,
    file_pattern: str | None = None,
    limit: int | None = None,
) -> str:
    """Search for pattern (regex) in files under a directory.

    Args:
        directory: Directory to search in.
        pattern: Regex pattern to search for.
        file_pattern: Optional glob pattern to filter files (e.g. "*.py").
        limit: Max number of matching lines to return (default: 50, max: 200).

    Returns:
        Search results as formatted string (grep-style: path:lineno:content).
    """
    cancel_event = get_interrupt_event()
    try:
        cancellation_checkpoint(cancel_event)
        results = ops.search_files(
            directory,
            pattern,
            file_pattern=file_pattern,
            limit=limit,
            cancel_event=cancel_event,
        )
        cancellation_checkpoint(cancel_event)
        return json.dumps({"results": results}, ensure_ascii=False)
    except InterruptedError as exc:
        return tool_error(
            str(exc),
            success=False,
            status="cancelled",
            interrupted=True,
        )
    except OSError as e:
        try:
            cancellation_checkpoint(cancel_event)
        except InterruptedError as cancel_exc:
            return tool_error(
                str(cancel_exc),
                success=False,
                status="cancelled",
                interrupted=True,
            )
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# list_directory tool.

def list_directory_tool(path: str) -> str:
    """List directory contents with file size and modification time.

    Args:
        path: Directory path to list.

    Returns:
        JSON string with tab-separated listing: name, size, mtime, type.
    """
    try:
        listing, hint = ops.list_directory(path)
        result: dict[str, str] = {"listing": listing}
        if hint:
            result["_hint"] = hint
        return json.dumps(result, ensure_ascii=False)
    except OSError as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# Function-calling tool schemas.

READ_FILE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "Read the contents of a file from disk with optional offset and limit. "
                       "Defaults to 50,000 characters; hard cap at 100,000 characters. "
                       "Use offset+limit pagination for large files.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to read.",
                },
                "offset": {
                    "type": "integer",
                    "description": "Character offset to start reading from (default: 0).",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum characters to read (default: 50,000, max: 100,000).",
                },
            },
            "required": ["path"],
        },
    },
}

WRITE_FILE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": "Write content to a file atomically (writes to a temp file first, "
                       "then renames to prevent corruption). Creates parent directories if needed.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to write.",
                },
                "content": {
                    "type": "string",
                    "description": "Content to write to the file.",
                },
            },
            "required": ["path", "content"],
        },
    },
}

PATCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "patch",
        "description": "Replace the first occurrence of old_str with new_str in a file. "
                       "Requires exact string match.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to patch.",
                },
                "old_str": {
                    "type": "string",
                    "description": "Exact string to find and replace.",
                },
                "new_str": {
                    "type": "string",
                    "description": "Replacement string.",
                },
            },
            "required": ["path", "old_str", "new_str"],
        },
    },
}

EDIT_FILE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "edit_file",
        "description": (
            "Replace one exact text block in a file. old_block must be copied exactly "
            "from the current file content; new_block is the replacement text. "
            "Do not pass unified diff syntax."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to edit.",
                },
                "old_block": {
                    "type": "string",
                    "description": "Exact existing text block to find. Must match file content literally.",
                },
                "new_block": {
                    "type": "string",
                    "description": "Replacement text block. Do not include diff markers.",
                },
            },
            "required": ["path", "old_block", "new_block"],
        },
    },
}

DELETE_FILE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "delete_file",
        "description": "Delete a single file after runtime path policy approval. Directories are refused.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to delete.",
                },
            },
            "required": ["path"],
        },
    },
}

SEARCH_FILES_SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_files",
        "description": "Search for a regex pattern inside file contents under a directory. "
                       "Uses the active runtime search provider. "
                       "Results are capped at 50 lines by default to prevent context overflow.",
        "parameters": {
            "type": "object",
            "properties": {
                "directory": {
                    "type": "string",
                    "description": "Directory to search in.",
                },
                "pattern": {
                    "type": "string",
                    "description": "Regex pattern to search for.",
                },
                "file_pattern": {
                    "type": "string",
                    "description": "Optional glob pattern to filter files (e.g. '*.py').",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of matching lines to return (default: 50, max: 200).",
                },
            },
            "required": ["directory", "pattern"],
        },
    },
}

LIST_DIRECTORY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "list_directory",
        "description": "List directory contents with file size, modification time, and type.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory path to list.",
                },
            },
            "required": ["path"],
        },
    },
}

# Dispatch wrappers: registry calls handler(args_dict, **meta_kwargs).

def _resolve_agent_path(path: str, parent_agent=None) -> str:
    """Resolve relative tool paths against the agent's inherited workspace."""
    workspace = str(getattr(parent_agent, "workspace_path", "") or "").strip()
    if not workspace:
        return path
    value = os.path.expandvars(str(path or "")).strip().strip('"').strip("'") or "."
    candidate = Path(value).expanduser()
    return str(candidate if candidate.is_absolute() else Path(workspace).expanduser() / candidate)

def _handle_read_file(args: dict, **kw) -> str:
    parent_agent = kw.get("parent_agent")
    path = _resolve_agent_path(args.get("path", ""), parent_agent)
    task_id = getattr(parent_agent, "session_id", "default") if parent_agent else "default"
    return read_file_tool(
        path=path,
        offset=args.get("offset", 0),
        limit=args.get("limit"),
        task_id=task_id,
        max_chars=_configured_read_limit(parent_agent),
    )

def _handle_write_file(args: dict, **kw) -> str:
    path = _resolve_agent_path(args.get("path", ""), kw.get("parent_agent"))
    return write_file_tool(path=path, content=args.get("content", ""))

def _handle_patch(args: dict, **kw) -> str:
    path = _resolve_agent_path(args.get("path", ""), kw.get("parent_agent"))
    return patch_tool(
        path=path,
        old_str=args.get("old_str", ""),
        new_str=args.get("new_str", ""),
    )

def _handle_edit_file(args: dict, **kw) -> str:
    path = _resolve_agent_path(args.get("path", ""), kw.get("parent_agent"))
    return edit_file_tool(
        path=path,
        old_block=args.get("old_block", ""),
        new_block=args.get("new_block", ""),
    )

def _handle_delete_file(args: dict, **kw) -> str:
    path = _resolve_agent_path(args.get("path", ""), kw.get("parent_agent"))
    return delete_file_tool(path=path)

def _handle_search_files(args: dict, **kw) -> str:
    directory = _resolve_agent_path(args.get("directory", ""), kw.get("parent_agent"))
    return search_files_tool(
        directory=directory,
        pattern=args.get("pattern", ""),
        file_pattern=args.get("file_pattern"),
        limit=args.get("limit"),
    )

def _handle_list_directory(args: dict, **kw) -> str:
    path = _resolve_agent_path(args.get("path", ""), kw.get("parent_agent"))
    return list_directory_tool(path=path)

# Register all file tools in the tool registry.
registry.register(
    name="read_file",
    toolset="file",
    schema=READ_FILE_SCHEMA,
    handler=_handle_read_file,
    description="Read file contents",
    emoji="📖",
    max_result_size_chars=100_000,
)

registry.register(
    name="write_file",
    toolset="file",
    schema=WRITE_FILE_SCHEMA,
    handler=_handle_write_file,
    description="Create or overwrite a file",
    emoji="✏️",
    max_result_size_chars=10_000,
)

registry.register(
    name="patch",
    toolset="file",
    schema=PATCH_SCHEMA,
    handler=_handle_patch,
    description="Replace an exact string in a file",
    emoji="📝",
    max_result_size_chars=10_000,
)

registry.register(
    name="edit_file",
    toolset="file",
    schema=EDIT_FILE_SCHEMA,
    handler=_handle_edit_file,
    description="Replace an exact text block in a file",
    emoji="📝",
    max_result_size_chars=10_000,
)

registry.register(
    name="delete_file",
    toolset="file",
    schema=DELETE_FILE_SCHEMA,
    handler=_handle_delete_file,
    description="Delete a single file",
    emoji="🗑️",
    max_result_size_chars=10_000,
)

registry.register(
    name="search_files",
    toolset="file",
    schema=SEARCH_FILES_SCHEMA,
    handler=_handle_search_files,
    description="Search files in a directory",
    emoji="🔍",
    max_result_size_chars=100_000,
)

registry.register(
    name="list_directory",
    toolset="file",
    schema=LIST_DIRECTORY_SCHEMA,
    handler=_handle_list_directory,
    description="List directory contents",
    emoji="📁",
    max_result_size_chars=50_000,
)
