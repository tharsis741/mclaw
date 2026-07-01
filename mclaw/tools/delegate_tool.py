# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Delegated subagent execution with isolated runtime state.

The delegate tool spawns child ``MClaw`` instances for independent subtasks.
Each child receives a self-contained prompt, a restricted toolset, its own
runtime workspace, and no inherited conversation history from the parent.

The parent context only receives the delegation call and final summaries.
Intermediate child tool calls stay out of the parent message history so large
exploration tasks do not contaminate or bloat the main conversation.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Empty, Queue
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

from mclaw.tools.path_extract import extract_absolute_paths
from mclaw.tools.registry import registry, tool_error

if TYPE_CHECKING:
    from mclaw.agent.core import MClaw

logger = logging.getLogger(__name__)

DELEGATE_BLOCKED_TOOLS = frozenset([
    "delegate_task",    # prevent recursive delegation
    "memory_read",      # children must not mutate persistent memory
    "memory_add",
    "memory_replace",
    "memory_remove",
    "session_search",   # cross-session search breaks isolation
    "skill_manage",     # children must not mutate shared Skill storage
    "skill_search",     # children must not discover or install external Skills
])

_BLOCKED_TOOLSET_NAMES = frozenset(["delegation", "memory"])

DEFAULT_DELEGATE_TOOLSETS = ["terminal", "file"]
ALLOWED_DELEGATE_TOOLSETS = frozenset([
    "terminal",
    "file",
    "vision",
    "web",
    "browser",
    "delegation_skill_read",
])

MAX_CONCURRENT_CHILDREN = 5
MAX_DELEGATE_DEPTH = 2
DEFAULT_MAX_ITERATIONS = 10  # hard cap after workspace preloading limits exploration
MAX_SUMMARY_CHARS = 800  # keep child summaries concise but useful
MAX_WORKSPACE_COPY_FILES = 300
MAX_WORKSPACE_COPY_BYTES = 30 * 1024 * 1024
DELEGATION_COPY_EXCLUDES = frozenset({
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    "dist",
    "build",
    ".next",
    ".turbo",
})

class SubtaskEvent:
    """Thread-safe progress event emitted by a child agent."""
    def __init__(
        self,
        task_index: int,
        event_type: str,
        data: Any = None,
        timestamp: float = None,
    ):
        self.task_index = task_index
        self.event_type = event_type  # "started" | "tool_call" | "completed" | "error"
        self.data = data
        self.timestamp = timestamp or time.time()

    def __repr__(self):
        return f"SubtaskEvent(task={self.task_index}, type={self.event_type})"


SUBAGENT_STARTED = "started"
SUBAGENT_TOOL_CALL = "tool_call"
SUBAGENT_COMPLETED = "completed"
SUBAGENT_ERROR = "error"

ProgressCallback = Callable[[SubtaskEvent], None]

_subagent_results: Queue = Queue()
_subagent_result_stash: dict[str, Dict[str, Any]] = {}
_subagent_result_stash_lock = threading.Lock()

def _build_child_system_prompt(
    goal: str,
    context: Optional[str] = None,
    *,
    workspace_path: Optional[str] = None,
    max_iterations: int = 10,
) -> str:
    """Build the lightweight child prompt without inheriting the parent prompt."""
    parts = [
        "你是一个子代理，负责完成父代理委托的独立任务。",
        "",
        f"任务目标：\n{goal}",
    ]
    if context and context.strip():
        parts.append(f"\n上下文信息：\n{context.strip()}")
    if workspace_path and str(workspace_path).strip():
        parts.append(
            f"\n工作区路径：\n{workspace_path.strip()}"
        )
    parts.append(
        "\n执行方式：\n"
        "- 以任务目标为准，结合上下文完成可独立处理的部分。\n"
        "- 本地文件读写以工作区为默认范围；优先使用工作区内已有文件和上下文提供的路径。\n"
        "- 只能通过 terminal 访问 delegation workspace 内的文件，不要声称拥有更高权限。\n"
        "- 不要用 terminal 直接访问 delegation workspace 之外的路径。\n"
        "- 路径或文件不明确时，先做最小范围探索，再读取关键文件。\n"
        "- 信息收集类任务直接返回结论；需要交付文件时再创建或修改文件。\n"
        "- 遇到缺失文件、权限限制、工具不可用或上下文不足时，说明影响和已完成部分。\n"
    )
    parts.append(
        "\n完成要求：\n"
        "完成后返回简洁摘要：\n"
        "- 你做了什么\n"
        "- 完成了什么\n"
        "- 关键发现或结果\n"
        "- 创建或修改了哪些文件\n"
        "- 未完成项或阻塞点\n\n"
        "你的回复会交给父代理继续处理，保持事实化、简洁；保持简洁。"
    )
    return "\n".join(parts)


def _resolve_workspace_hint(parent_agent) -> Optional[str]:
    """Return the best local workspace hint available from the parent agent."""
    candidates = [
        os.getenv("TERMINAL_CWD"),
        getattr(parent_agent, "terminal_cwd", None),
        getattr(parent_agent, "cwd", None),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            text = os.path.abspath(os.path.expanduser(str(candidate)))
        except Exception:
            continue
        if os.path.isabs(text) and os.path.isdir(text):
            return text
    return None


def _extract_paths_from_text(text: str) -> List[str]:
    """Extract absolute Windows or Unix paths from free-form text."""
    return [p for p in extract_absolute_paths(text) if os.path.isabs(os.path.normpath(p))]


def _workspace_preparation_for_child(child: Any) -> Dict[str, Any]:
    """Return the full workspace-copy report attached during child setup."""
    preparation = getattr(child, "_workspace_preparation", None)
    return preparation if isinstance(preparation, dict) else {}


def _workspace_preparation_summary(child: Any) -> Dict[str, Any]:
    """Collapse workspace-copy details for parent-facing pending metadata."""
    preparation = _workspace_preparation_for_child(child)
    return {
        "detected_count": len(preparation.get("detected_paths", [])),
        "copied_count": len(preparation.get("copied_paths", [])),
        "failed_count": len(preparation.get("failed_paths", [])),
        "skipped_count": len(preparation.get("skipped_paths", [])),
        "workspace_path": preparation.get("workspace_path"),
    }


def _generate_dir_tree(path: str, max_depth: int = 4, max_files_per_dir: int = 30) -> str:
    """Generate a compact directory tree to reduce blind child exploration."""
    root = Path(path)
    if not root.exists() or not root.is_dir():
        return ""

    lines: list[str] = []

    def _walk(p: Path, prefix: str, depth: int):
        if depth > max_depth:
            return
        try:
            entries = sorted(p.iterdir(), key=lambda e: (e.is_file(), e.name.lower()))
        except OSError:
            return
        # Cap each directory so large trees do not flood the prompt.
        shown = entries[:max_files_per_dir]
        for i, entry in enumerate(shown):
            is_last = (i == len(shown) - 1)
            connector = "└── " if is_last else "├── "
            suffix = "/" if entry.is_dir() else ""
            lines.append(f"{prefix}{connector}{entry.name}{suffix}")
            if entry.is_dir() and depth < max_depth:
                ext = "    " if is_last else "│   "
                _walk(entry, prefix + ext, depth + 1)
        if len(entries) > max_files_per_dir:
            lines.append(f"{prefix}... ({len(entries) - max_files_per_dir} more items)")

    lines.append(f"{root.name}/")
    _walk(root, "", 1)
    return "\n".join(lines)


def _copy_path_limited(src: Path, dest: Path) -> tuple[int, int, list[str]]:
    """Copy one file or directory into a bounded delegation workspace."""
    copied_files = 0
    copied_bytes = 0
    skipped: list[str] = []

    def _copy_file(file_src: Path, file_dest: Path) -> None:
        nonlocal copied_files, copied_bytes
        try:
            size = file_src.stat().st_size
        except OSError:
            skipped.append(str(file_src))
            return
        if copied_files + 1 > MAX_WORKSPACE_COPY_FILES:
            skipped.append(str(file_src))
            return
        if copied_bytes + size > MAX_WORKSPACE_COPY_BYTES:
            skipped.append(str(file_src))
            return
        file_dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(file_src, file_dest)
        copied_files += 1
        copied_bytes += size

    if src.is_file():
        _copy_file(src, dest)
        return copied_files, copied_bytes, skipped

    for item in src.rglob("*"):
        rel = item.relative_to(src)
        if any(part in DELEGATION_COPY_EXCLUDES for part in rel.parts):
            if item.is_dir():
                skipped.append(str(item))
            continue
        if item.is_dir():
            continue
        _copy_file(item, dest / rel)

    return copied_files, copied_bytes, skipped


def _prepare_delegation_workspace(
    goal: str,
    context: Optional[str],
    child_delegation_dir: Path,
) -> tuple[str, Optional[str], str, Dict[str, Any]]:
    """Copy referenced external paths into the child workspace and rewrite paths.

    Child agents may only work inside their delegation workspace. Absolute
    paths mentioned in the task are copied into that workspace under hard file
    and byte limits, then the goal/context are rewritten so child terminal
    access stays within the isolated tree.

    Returns:
        (new_goal, new_context, workspace_path, preparation_report)
    """
    workspace = child_delegation_dir / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    text = f"{goal or ''} {context or ''}"
    paths = _extract_paths_from_text(text)
    # Replace longer paths first to avoid partial substitutions.
    paths = sorted(paths, key=len, reverse=True)

    preparation: Dict[str, Any] = {
        "workspace_path": str(workspace),
        "detected_paths": paths,
        "copied_paths": [],
        "failed_paths": [],
        "skipped_paths": [],
    }

    copied: Dict[str, str] = {}
    covered_norms: set = set()

    for original in paths:
        norm = os.path.normpath(original)
        if not os.path.exists(norm):
            preparation["failed_paths"].append({
                "path": original,
                "reason": "not_found",
            })
            logger.warning("[delegate] external path not found for subagent copy: %s", original)
            continue

        # Skip paths already covered by a copied parent directory.
        is_covered = False
        for covered in covered_norms:
            if norm == covered or norm.startswith(covered + os.sep):
                is_covered = True
                break
        if is_covered:
            preparation["skipped_paths"].append({
                "path": original,
                "reason": "covered_by_parent_copy",
            })
            continue

        # Skip M-Claw internal runtime paths.
        lower = norm.lower()
        if ".mclaw" in lower:
            skip = False
            for marker in ("\\workspace\\", "/workspace/", "\\delegations\\", "/delegations/"):
                if marker in lower:
                    skip = True
                    break
            if skip:
                preparation["skipped_paths"].append({
                    "path": original,
                    "reason": "mclaw_internal_path",
                })
                continue

        basename = os.path.basename(norm) or "item"
        dest = workspace / basename
        counter = 1
        while dest.exists() and os.path.normpath(dest) != norm:
            dest = workspace / f"{basename}_{counter}"
            counter += 1

        try:
            if os.path.isdir(norm):
                copied_files, copied_bytes, skipped = _copy_path_limited(Path(norm), dest)
                logger.info(
                    "[delegate] copied directory for subagent: %s -> %s files=%d bytes=%d skipped=%d",
                    norm,
                    dest,
                    copied_files,
                    copied_bytes,
                    len(skipped),
                )
                if copied_files <= 0:
                    preparation["skipped_paths"].append({
                        "path": original,
                        "reason": "copy_limit_or_empty_directory",
                        "skipped": skipped[:20],
                    })
                    continue
            else:
                copied_files, copied_bytes, skipped = _copy_path_limited(Path(norm), dest)
                if copied_files <= 0:
                    preparation["skipped_paths"].append({
                        "path": original,
                        "reason": "copy_limit_or_empty_file",
                        "skipped": skipped[:20],
                    })
                    continue
            copied[original] = str(dest)
            covered_norms.add(norm)
            preparation["copied_paths"].append({
                "source": original,
                "destination": str(dest),
                "files": copied_files,
                "bytes": copied_bytes,
            })
            if skipped:
                preparation["skipped_paths"].append({
                    "path": original,
                    "reason": "partial_copy_skipped_items",
                    "skipped": skipped[:20],
                    "skipped_count": len(skipped),
                })
            logger.info("[delegate] copied for subagent: %s -> %s", norm, dest)
        except Exception as exc:
            preparation["failed_paths"].append({
                "path": original,
                "reason": "copy_error",
                "error": str(exc),
            })
            logger.warning("[delegate] failed to copy %s: %s", norm, exc)

    new_goal = goal or ""
    new_context = context or ""
    for original in sorted(copied.keys(), key=len, reverse=True):
        replacement = copied[original]
        new_goal = new_goal.replace(original, replacement)
        if new_context:
            new_context = new_context.replace(original, replacement)
        # Also replace forward-slash variants, which commonly appear in JSON.
        if "\\" in original:
            forward = original.replace("\\", "/")
            new_goal = new_goal.replace(forward, replacement)
            if new_context:
                new_context = new_context.replace(forward, replacement)

    if paths:
        prep_lines = [
            "\n\n【Delegation workspace 准备情况】",
            f"- workspace: {workspace}",
            f"- 检测到外部路径: {len(preparation['detected_paths'])}",
            f"- 已复制: {len(preparation['copied_paths'])}",
            f"- 失败: {len(preparation['failed_paths'])}",
            f"- 跳过: {len(preparation['skipped_paths'])}",
        ]
        for item in preparation["copied_paths"][:10]:
            prep_lines.append(f"- 可用副本: {item['destination']}")
        for item in preparation["failed_paths"][:10]:
            prep_lines.append(f"- 未复制: {item['path']} ({item.get('reason', 'unknown')})")
        if preparation["failed_paths"]:
            prep_lines.append(
                "处理文件时优先使用 workspace 内可用副本；缺少必要文件时，在总结中说明缺失路径和影响。"
            )
        new_context = (new_context or "") + "\n".join(prep_lines)

    # Preload a directory tree so children can jump straight to relevant files.
    dir_tree = _generate_dir_tree(str(workspace))
    if dir_tree:
        tree_block = (
            f"\n\n【项目目录结构】\n"
            f"```\n{dir_tree}\n```\n"
            f"以上已包含 workspace 内目录树；请直接读取关键文件进行分析。"
        )
        new_context = (new_context or "") + tree_block

    return new_goal, new_context or None, str(workspace), preparation

def _strip_blocked_toolsets(toolsets: List[str]) -> List[str]:
    """Remove blocked toolset names from a requested child toolset list."""
    return [
        t for t in toolsets
        if t in ALLOWED_DELEGATE_TOOLSETS and t not in _BLOCKED_TOOLSET_NAMES
    ]


def _filter_blocked_tools(tool_names: List[str]) -> List[str]:
    """Filter individual blocked tool names as the final safety barrier."""
    return [t for t in tool_names if t not in DELEGATE_BLOCKED_TOOLS]


def _resolve_child_toolsets(
    requested_toolsets: Optional[List[str]],
    parent_enabled: Optional[List[str]],
    parent_available_toolsets: Optional[List[str]],
) -> List[str]:
    """Resolve the effective child toolsets.

    Priority:
      1. Explicit requested toolsets, limited to ALLOWED_DELEGATE_TOOLSETS.
      2. DEFAULT_DELEGATE_TOOLSETS when no explicit toolsets are requested.
    """
    desired = list(DEFAULT_DELEGATE_TOOLSETS)
    if requested_toolsets:
        desired.extend(str(t) for t in requested_toolsets if str(t).strip())

    parent_allowed = set(parent_available_toolsets) if parent_available_toolsets is not None else None
    effective: list[str] = []
    seen: set[str] = set()
    for toolset in desired:
        if toolset in seen:
            continue
        seen.add(toolset)
        if toolset not in ALLOWED_DELEGATE_TOOLSETS or toolset in _BLOCKED_TOOLSET_NAMES:
            continue
        if parent_allowed is not None and toolset not in parent_allowed:
            continue
        effective.append(toolset)

    return effective

def _build_child_agent(
    task_index: int,
    goal: str,
    context: Optional[str],
    toolsets: Optional[List[str]],
    max_iterations: int,
    parent_agent,
) -> "MClaw":
    """Construct a child MClaw instance with isolated state and tools.

    The parent decides the maximum capability envelope. This builder narrows it
    again for delegation, resolves optional delegation-specific provider
    credentials, preloads referenced files into an isolated workspace, and
    installs filtered tool definitions without re-running discovery.
    """
    from mclaw.agent.core import MClaw
    from mclaw.tools.dispatch import get_tool_definitions, get_toolset_for_tool

    logger.info("[subagent-%d] 构建中, depth=%d", task_index, getattr(parent_agent, "_delegate_depth", 0) + 1)

    # Resolve parent toolset information.
    parent_enabled = getattr(parent_agent, "enabled_toolsets", None)

    # Infer parent_available_toolsets from valid_tool_names. Delegation must
    # not grant capabilities the parent could not access.
    parent_available_toolsets: Optional[List[str]] = None
    try:
        parent_available = set()
        for t in getattr(parent_agent, "valid_tool_names", []):
            ts = get_toolset_for_tool(t)
            if ts:
                parent_available.add(ts)
        parent_available_toolsets = list(parent_available) if parent_available else None
    except Exception:
        parent_available_toolsets = None

    # Compute the child toolset.
    child_toolsets = _resolve_child_toolsets(
        requested_toolsets=toolsets,
        parent_enabled=parent_enabled,
        parent_available_toolsets=parent_available_toolsets,
    )
    if not child_toolsets:
        raise ValueError("No delegate toolsets are available under the parent agent's current tool permissions.")

    # Load tool definitions and remove blocked tools.
    parent_config = getattr(parent_agent, "config", {}) if parent_agent is not None else {}
    all_tool_defs, all_tool_names = get_tool_definitions(
        enabled_toolsets=child_toolsets,
        config=parent_config if isinstance(parent_config, dict) else None,
    )

    # Tool-name filtering is the final safety barrier.
    safe_tool_names = _filter_blocked_tools(list(all_tool_names))
    safe_tool_defs = [t for t in all_tool_defs if t["function"]["name"] in safe_tool_names]

    # Pre-create the session id and runtime workspace before prompt assembly.
    child_session_id = f"delegate_{getattr(parent_agent, 'session_id', 'unknown')}_{task_index}_{uuid.uuid4().hex[:6]}"
    from mclaw.runtime.manager import RuntimeManager

    runtime = RuntimeManager.current(parent_config if isinstance(parent_config, dict) else None)
    child_runtime_workspace = runtime.paths.session_root(child_session_id)
    child_runtime_workspace.mkdir(parents=True, exist_ok=True)
    delegation_root = runtime.paths.delegation_root()
    child_delegation_dir = delegation_root / child_session_id
    child_delegation_dir.mkdir(parents=True, exist_ok=True)

    # Copy external files into the child workspace and rewrite goal/context.
    prepared_goal, prepared_context, workspace_path, workspace_preparation = _prepare_delegation_workspace(
        goal, context, child_delegation_dir
    )

    # Build the child prompt.
    child_prompt = _build_child_system_prompt(
        prepared_goal, prepared_context, workspace_path=workspace_path, max_iterations=max_iterations
    )

    # Inherit parent provider credentials unless delegation overrides them.
    parent_api_key = getattr(parent_agent, "api_key", None) or ""
    parent_base_url = getattr(parent_agent, "base_url", None) or ""
    parent_model = getattr(parent_agent, "model", None) or ""
    parent_provider = getattr(parent_agent, "provider", None) or ""
    parent_api_mode = getattr(parent_agent, "api_mode", None) or "chat_completions"
    delegation_cfg = parent_config.get("delegation", {}) if isinstance(parent_config, dict) else {}

    child_model = delegation_cfg.get("model") or parent_model
    child_api_key = parent_api_key
    child_base_url = parent_base_url
    child_provider = parent_provider
    child_api_mode = parent_api_mode

    has_delegation_auth_override = any(
        delegation_cfg.get(k) for k in ("provider", "base_url")
    )
    if has_delegation_auth_override:
        try:
            from mclaw.cli.auth import resolve_provider
            resolved = resolve_provider(
                model=child_model,
                provider=delegation_cfg.get("provider") or "",
                base_url=delegation_cfg.get("base_url") or "",
                api_key="",
                config=parent_config if isinstance(parent_config, dict) else None,
            )
            child_model = resolved.get("model") or child_model
            child_api_key = resolved.get("api_key") or child_api_key
            child_base_url = resolved.get("base_url") or child_base_url
            child_provider = resolved.get("provider") or child_provider
            child_api_mode = resolved.get("api_mode") or child_api_mode
        except Exception as exc:
            logger.warning(
                "[subagent-%d] delegation provider override failed, using parent credentials: %s",
                task_index,
                exc,
            )

    # Create the child agent.
    child = MClaw(
        model=child_model,
        api_key=child_api_key,
        base_url=child_base_url,
        api_mode=child_api_mode,
        provider=child_provider,
        system_prompt=child_prompt,          # inject lightweight prompt directly
        skip_memory=True,                    # disable the memory subsystem
        enabled_toolsets=child_toolsets,     # restricted toolset
        max_iterations=max_iterations,
        session_db=getattr(parent_agent, "_session_db", None),
        session_id=child_session_id,
        parent_session_id=getattr(parent_agent, "session_id", None),
        workspace=getattr(parent_agent, "workspace_path", None),
        config=parent_config,
    )

    # Reuse context compressor metadata to avoid duplicate network lookups.
    # Children use the same model as the parent unless overridden above.
    parent_compressor = getattr(parent_agent, "context_compressor", None)
    if parent_compressor and child.context_compressor:
        child.context_compressor.context_length = parent_compressor.context_length
        child.context_compressor.threshold_tokens = parent_compressor.threshold_tokens
        child.context_compressor.last_prompt_tokens = parent_compressor.last_prompt_tokens
        child.context_compressor.last_completion_tokens = parent_compressor.last_completion_tokens

    # Install the already-filtered tool definitions without rediscovery.
    child.tools = safe_tool_defs
    child.valid_tool_names = set(safe_tool_names)

    # Set child depth for recursive-delegation enforcement.
    parent_depth = getattr(parent_agent, "_delegate_depth", 0)
    child._delegate_depth = parent_depth + 1

    # Attach runtime and delegation workspace paths.
    child._runtime_workspace_dir = child_runtime_workspace
    child._delegation_dir = child_delegation_dir
    child._prepared_goal = prepared_goal
    child._workspace_preparation = workspace_preparation

    # Inherit parent callbacks for streaming/status plumbing.
    child._print_fn = getattr(parent_agent, "_print_fn", print)
    child._stream_callback = getattr(parent_agent, "_stream_callback", None)
    child._tool_callback = getattr(parent_agent, "_tool_callback", None)
    child._status_callback = getattr(parent_agent, "_status_callback", None)

    logger.info(
        "[subagent-%d] 已构建, runtime_workspace=%s, delegation=%s, tools=%s, depth=%d",
        task_index, str(child_runtime_workspace), str(child_delegation_dir),
        list(safe_tool_names), child._delegate_depth
    )

    return child

def _run_single_child(
    task_index: int,
    goal: str,
    child: "MClaw",
    parent_agent,
    progress_callback: ProgressCallback = None,
) -> Dict[str, Any]:
    """Run one child agent and collect its result."""
    child_start = time.monotonic()

    logger.info(
        "[subagent-%d] 启动, depth=%d, goal=%.50s",
        task_index, child._delegate_depth, goal
    )

    # Relay child tool calls to progress_callback without invoking the
    # parent's _tool_callback; child tool calls should not render in the
    # parent TUI as normal parent actions.
    if progress_callback:
        def _relay_tool(tool_name: str, args: dict):
            progress_callback(SubtaskEvent(
                task_index, SUBAGENT_TOOL_CALL,
                {"tool": tool_name, "args_bytes": len(str(args))}
            ))

        child._tool_callback = _relay_tool

        # Emit a started event.
        progress_callback(SubtaskEvent(
            task_index, SUBAGENT_STARTED,
            {"goal": goal[:100], "depth": child._delegate_depth}
        ))

    try:
        # Run the child without parent conversation history.
        logger.info("[subagent-%d] entering run_conversation", task_index)
        effective_goal = getattr(child, "_prepared_goal", None) or goal
        result = child.run_conversation(user_message=effective_goal)
        logger.info("[subagent-%d] run_conversation returned", task_index)

        duration = round(time.monotonic() - child_start, 2)

        summary = result.get("final_response") or ""
        completed = result.get("completed", False)
        interrupted = result.get("interrupted", False)
        api_calls = result.get("api_calls", 0)

        if interrupted:
            status = "interrupted"
            exit_reason = "interrupted"
        elif summary:
            status = "completed"
            exit_reason = "completed" if completed else "max_iterations"
        else:
            status = "failed"
            exit_reason = "max_iterations"

        logger.info(
            "[subagent-%d] 完成, duration=%.2fs, status=%s, exit=%s",
            task_index, duration, status, exit_reason
        )

        # Collect token statistics.
        input_tokens = getattr(child, "session_input_tokens", 0) or 0
        output_tokens = getattr(child, "session_output_tokens", 0) or 0

        # Truncate oversized summaries before returning them to the parent.
        if len(summary) > MAX_SUMMARY_CHARS:
            summary = summary[:MAX_SUMMARY_CHARS] + "……[内容已截断]"

        entry: Dict[str, Any] = {
            "task_index": task_index,
            "goal": goal,
            "status": status,
            "summary": summary,
            "api_calls": api_calls,
            "duration_seconds": duration,
            "model": child.model if isinstance(child.model, str) else None,
            "exit_reason": exit_reason,
            "tokens": {
                "input": input_tokens if isinstance(input_tokens, (int, float)) else 0,
                "output": output_tokens if isinstance(output_tokens, (int, float)) else 0,
            },
            "tool_trace": [],
        }
        workspace_preparation = _workspace_preparation_for_child(child)
        if workspace_preparation:
            entry["workspace_preparation"] = workspace_preparation

        if status == "failed":
            entry["error"] = result.get("error", "子代理未产生响应")

        # Emit a completed event.
        if progress_callback:
            progress_callback(SubtaskEvent(
                task_index, SUBAGENT_COMPLETED,
                {"status": status, "duration": duration, "summary": summary, "api_calls": api_calls}
            ))

        return entry

    except Exception as exc:
        duration = round(time.monotonic() - child_start, 2)
        logger.error("[subagent-%d] 异常: %s", task_index, exc)

        # Emit an error event.
        if progress_callback:
            progress_callback(SubtaskEvent(
                task_index, SUBAGENT_ERROR,
                {"error": str(exc)}
            ))

        return {
            "task_index": task_index,
            "goal": goal,
            "status": "error",
            "summary": None,
            "error": str(exc),
            "api_calls": 0,
            "duration_seconds": duration,
        }

# Hard wall-clock timeout for each delegated task. HTTP timeouts are not enough:
# one child may make several API calls, and browser-heavy work can accumulate
# navigation, snapshot, scrolling, and provider latency.
_SUBAGENT_MAX_WALL_TIME = 300  # 5 minutes


def _run_all_children_background(
    task_list: list,
    children: list,
    parent_agent,
    progress_callback: ProgressCallback,
    task_id: str,
    start_time: float,
) -> None:
    """Run all children in a daemon thread and queue their final results.

    Non-blocking TUI mode cannot wait on every child future inline. Results are
    therefore placed on the module queue and later matched by task id by the
    runtime command layer.
    """
    results: List[Dict[str, Any]] = []
    goal_map = {i: task["goal"] for i, task, _child in children}

    # The executor lifecycle is controlled explicitly so result delivery stays
    # independent from slow child workers.
    executor = ThreadPoolExecutor(max_workers=min(len(children), MAX_CONCURRENT_CHILDREN))
    try:
        futures = {}
        for i, task, child in children:
            fut = executor.submit(
                _run_single_child,
                task_index=i,
                goal=task["goal"],
                child=child,
                parent_agent=parent_agent,
                progress_callback=progress_callback,
            )
            futures[fut] = i

        # Wait for all children under one global wall-clock deadline. The
        # FIRST_COMPLETED loop avoids serial future timeouts multiplying by
        # child count.
        from concurrent.futures import wait, FIRST_COMPLETED

        pending = set(futures.keys())
        deadline = time.time() + _SUBAGENT_MAX_WALL_TIME

        while pending:
            remaining = max(0.0, deadline - time.time())
            if remaining <= 0:
                break
            done, pending = wait(pending, timeout=remaining, return_when=FIRST_COMPLETED)
            for future in done:
                idx = futures[future]
                try:
                    entry = future.result()
                except Exception as exc:
                    logger.error("[subagent-%d] 执行异常或超时: %s", idx, exc)
                    entry = {
                        "task_index": idx,
                        "goal": goal_map.get(idx, ""),
                        "status": "error",
                        "summary": None,
                        "error": str(exc),
                        "api_calls": 0,
                        "duration_seconds": _SUBAGENT_MAX_WALL_TIME,
                    }
                    if progress_callback:
                        progress_callback(SubtaskEvent(
                            idx, SUBAGENT_ERROR,
                            {"error": str(exc)}
                        ))
                results.append(entry)

        # Mark remaining children as timed out.
        for future in pending:
            idx = futures[future]
            future.cancel()
            logger.error("[subagent-%d] 执行异常或超时: %s", idx, "wall-clock timeout")
            entry = {
                "task_index": idx,
                "goal": goal_map.get(idx, ""),
                "status": "error",
                "summary": None,
                "error": "子代理执行超时（5分钟）",
                "api_calls": 0,
                "duration_seconds": _SUBAGENT_MAX_WALL_TIME,
            }
            child = next((c for i, _task, c in children if i == idx), None)
            workspace_preparation = _workspace_preparation_for_child(child)
            if workspace_preparation:
                entry["workspace_preparation"] = workspace_preparation
            if progress_callback:
                progress_callback(SubtaskEvent(
                    idx, SUBAGENT_ERROR,
                    {"error": "子代理执行超时（5分钟）"}
                ))
            results.append(entry)
    finally:
        # Return immediately. Stuck workers may continue until their API call
        # times out, but they no longer block the parent flow.
        executor.shutdown(wait=False)

    results.sort(key=lambda r: r["task_index"])
    _subagent_results.put({
        "task_id": task_id,
        "results": results,
        "total_duration_seconds": round(time.time() - start_time, 2),
    })
    logger.info(
        "delegate_task result queued task_id=%s results=%d duration=%.2fs",
        task_id,
        len(results),
        time.time() - start_time,
    )


def get_pending_results(timeout: float = 0.05) -> Optional[Dict]:
    """Return queued child results without blocking the TUI for long."""
    try:
        return _subagent_results.get(timeout=timeout)
    except Empty:
        return None


def get_pending_result_for_task(task_id: str, timeout: float = 0.05) -> Optional[Dict]:
    """Return results for one task id and stash unmatched results."""
    if not task_id:
        return get_pending_results(timeout=timeout)

    with _subagent_result_stash_lock:
        stashed = _subagent_result_stash.pop(task_id, None)
        if stashed is not None:
            logger.info("delegate_task result loaded from stash task_id=%s", task_id)
            return stashed

    deadline = time.monotonic() + max(0.0, timeout)
    unmatched: list[Dict[str, Any]] = []
    result: Optional[Dict[str, Any]] = None

    while True:
        remaining = max(0.0, deadline - time.monotonic())
        try:
            item = _subagent_results.get(timeout=remaining)
        except Empty:
            break

        item_task_id = item.get("task_id") if isinstance(item, dict) else None
        if item_task_id == task_id:
            result = item
            break
        if isinstance(item, dict) and item_task_id:
            unmatched.append(item)
            logger.info(
                "delegate_task result stashed while waiting for task_id=%s got=%s",
                task_id,
                item_task_id,
            )
        if time.monotonic() >= deadline:
            break

    if unmatched:
        with _subagent_result_stash_lock:
            for item in unmatched:
                item_task_id = item.get("task_id")
                if item_task_id:
                    _subagent_result_stash[item_task_id] = item

    return result

def delegate_task(
    tasks: Optional[List[Dict[str, Any]]] = None,
    parent_agent=None,
) -> str:
    """Spawn one or more isolated child agents for delegated tasks.

    The model-facing API always uses a tasks array, including a single task.
    At most MAX_CONCURRENT_CHILDREN tasks run in parallel. When the parent TUI
    supplies a progress callback, this function returns pending-task metadata
    immediately and the runtime later drains results from the shared queue.

    Returns a JSON string with child results or pending-task metadata.
    """
    # Validate parent_agent.
    if parent_agent is None:
        return tool_error("delegate_task 需要 parent_agent 上下文")

    # Enforce maximum delegation depth.
    parent_depth = getattr(parent_agent, "_delegate_depth", 0)
    if parent_depth >= MAX_DELEGATE_DEPTH:
        logger.warning(
            "delegate_task 深度超限: %d >= %d",
            parent_depth, MAX_DELEGATE_DEPTH
        )
        return json.dumps({
            "error": (
                f"代理深度已达上限（{MAX_DELEGATE_DEPTH}）。"
                "子代理不能创建进一步的子代理。"
            ),
            "success": False,
        }, ensure_ascii=False)

    # Normalize configuration.
    parent_config = getattr(parent_agent, "config", {}) if parent_agent is not None else {}
    delegation_cfg = parent_config.get("delegation", {}) if isinstance(parent_config, dict) else {}
    configured_max_iter = delegation_cfg.get("max_iterations", DEFAULT_MAX_ITERATIONS)
    try:
        configured_max_iter = int(configured_max_iter)
    except (TypeError, ValueError):
        configured_max_iter = DEFAULT_MAX_ITERATIONS
    effective_max_iter = configured_max_iter

    # Parse task list.
    if not isinstance(tasks, list):
        return tool_error("delegate_task 需要 tasks 数组；单任务也使用 tasks=[{goal, context?, toolsets?}]")
    if len(tasks) > MAX_CONCURRENT_CHILDREN:
        return tool_error(
            f"tasks 最多支持 {MAX_CONCURRENT_CHILDREN} 个任务，当前收到 {len(tasks)} 个"
        )
    task_list = tasks

    if not task_list:
        return tool_error("任务列表为空")

    # Validate that every task has a goal.
    for i, task in enumerate(task_list):
        if not isinstance(task, dict):
            return tool_error(f"任务 {i} 必须是对象")
        goal_text = str(task.get("goal") or "").strip()
        if not goal_text:
            return tool_error(f"任务 {i} 缺少 goal")
        task["goal"] = goal_text
        if "toolsets" in task and task.get("toolsets") is not None and not isinstance(task.get("toolsets"), list):
            return tool_error(f"任务 {i} 的 toolsets 必须是字符串数组")

    logger.info("delegate_task 处理 %d 个任务", len(task_list))

    # Build all child agents on the main thread.
    children = []
    try:
        for i, task in enumerate(task_list):
            child = _build_child_agent(
                task_index=i,
                goal=task["goal"],
                context=task.get("context"),
                toolsets=task.get("toolsets"),
                max_iterations=effective_max_iter,
                parent_agent=parent_agent,
            )
            children.append((i, task, child))
    except Exception as exc:
        logger.exception("构建子代理失败")
        return json.dumps({
            "error": f"构建子代理失败: {exc}",
            "success": False,
        }, ensure_ascii=False)

    # A progress callback means the TUI expects non-blocking execution.
    progress_callback: Optional[ProgressCallback] = getattr(
        parent_agent, "_delegate_progress_callback", None
    )

    # Run child agents.
    start_time = time.time()
    task_id = str(uuid.uuid4())[:8]

    if progress_callback is not None:
        # Non-blocking mode: start a daemon thread and return immediately.
        thread = threading.Thread(
            target=_run_all_children_background,
            args=(task_list, children, parent_agent, progress_callback, task_id, start_time),
            daemon=True,
        )
        thread.start()

        task_info = {
            "task_id": task_id,
            "num_tasks": len(task_list),
            "goals": [t["goal"][:80] for t in task_list],
            "workspace_preparation": [
                _workspace_preparation_summary(child)
                for _i, _task, child in children
            ],
        }

        return json.dumps({
            "pending": True,
            "task_id": task_id,
            "num_tasks": len(task_list),
            "task_info": task_info,
            "success": True,
        }, ensure_ascii=False)

    # Synchronous mode when the caller did not request progress callbacks.
    overall_start = time.monotonic()
    results = []
    goal_map = {i: task["goal"] for i, task, _child in children}

    if len(children) == 1:
        # Single task: run directly to avoid thread-pool overhead.
        _, _, child = children[0]
        result = _run_single_child(0, children[0][1]["goal"], child, parent_agent)
        results.append(result)
    else:
        # Batch mode: run concurrently with wall-clock timeouts.
        with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_CHILDREN) as executor:
            futures = {}
            for i, task, child in children:
                future = executor.submit(
                    _run_single_child,
                    task_index=i,
                    goal=task["goal"],
                    child=child,
                    parent_agent=parent_agent,
                )
                futures[future] = i

            # Per-future timeouts keep each child task bounded independently.
            for future, idx in futures.items():
                try:
                    entry = future.result(timeout=_SUBAGENT_MAX_WALL_TIME)
                except Exception as exc:
                    logger.error("[subagent-%d] 执行异常或超时: %s", idx, exc)
                    entry = {
                        "task_index": idx,
                        "goal": goal_map.get(idx, ""),
                        "status": "error",
                        "summary": None,
                        "error": str(exc),
                        "api_calls": 0,
                        "duration_seconds": _SUBAGENT_MAX_WALL_TIME,
                    }
                results.append(entry)

        # Preserve input order in the returned results.
        results.sort(key=lambda r: r["task_index"])

    total_duration = round(time.monotonic() - overall_start, 2)

    logger.info(
        "delegate_task 完成, %d 个任务, 总耗时=%.2fs",
        len(results), total_duration
    )

    return json.dumps({
        "results": results,
        "total_duration_seconds": total_duration,
        "success": True,
        "workspace_preparation": [
            _workspace_preparation_for_child(child)
            for _i, _task, child in children
        ],
    }, ensure_ascii=False)

DELEGATE_TASK_SCHEMA = {
    "type": "function",
    "function": {
        "name": "delegate_task",
        "description": (
            "Spawn up to 5 isolated subagents for independent subtasks. "
            "Always pass a tasks array; use one task object for a single subtask. "
            "Each task must be self-contained because subagents do not inherit conversation history. "
            "Subagents default to terminal+file and may only add allowed extra toolsets."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "tasks": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_CONCURRENT_CHILDREN,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "goal": {
                                "type": "string",
                                "description": (
                                    "Self-contained objective and completion criteria for one subagent."
                                ),
                            },
                            "context": {
                                "type": "string",
                                "description": (
                                    "Only necessary background: absolute file paths, errors, constraints, "
                                    "expected output, and relevant facts. The subagent sees no parent history."
                                ),
                            },
                            "toolsets": {
                                "type": "array",
                                "uniqueItems": True,
                                "items": {
                                    "type": "string",
                                    "enum": [
                                        "terminal",
                                        "file",
                                        "web",
                                        "vision",
                                        "browser",
                                        "delegation_skill_read",
                                    ],
                                },
                                "description": (
                                    "Omit by default. Include only extra capabilities this task needs. "
                                    "Default terminal+file is added automatically. Do not pass memory, "
                                    "session_search, delegate_task, skill_manage, skill_search, all, or unknown toolsets."
                                ),
                            },
                        },
                        "required": ["goal"],
                    },
                    "description": (
                        f"One to {MAX_CONCURRENT_CHILDREN} independent tasks. "
                        "Use length 1 for a single delegated task."
                    ),
                },
            },
            "required": ["tasks"],
        },
    },
}

registry.register(
    name="delegate_task",
    toolset="delegation",
    schema=DELEGATE_TASK_SCHEMA,
    handler=lambda args, **kw: delegate_task(
        tasks=args.get("tasks"),
        parent_agent=kw.get("parent_agent"),
    ),
    description="Delegate tasks to isolated subagents",
    emoji="🔀",
)
