# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Delegated subagent execution with isolated conversation state.

The delegate tool spawns child ``MClaw`` instances for independent subtasks.
Each child receives a self-contained prompt, a restricted toolset, the same
host filesystem access as its parent, and no inherited conversation history.

The parent context only receives the delegation call and final summaries.
Intermediate child tool calls stay out of the parent message history so large
exploration tasks do not contaminate or bloat the main conversation.
"""

from __future__ import annotations

import json
import logging
import os
import time
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Empty, Queue
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

from mclaw.tools.interrupt import get_interrupt_event
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
DEFAULT_MAX_ITERATIONS = 10
DEFAULT_SUBAGENT_TIMEOUT_SECONDS = 600
MAX_INLINE_SUMMARY_CHARS = 800


def normalize_timeout_seconds(value: Any) -> float:
    """Apply the delegate tool's single timeout fallback rule."""
    try:
        timeout = float(value)
        if timeout <= 0:
            raise ValueError
        return timeout
    except (TypeError, ValueError):
        return float(DEFAULT_SUBAGENT_TIMEOUT_SECONDS)


class SubtaskEvent:
    """Thread-safe progress event emitted by a child agent."""
    def __init__(
        self,
        task_index: int,
        event_type: str,
        data: Any = None,
        timestamp: float = None,
        delegation_id: str = "",
    ):
        self.task_index = task_index
        self.event_type = event_type  # "started" | "tool_call" | "completed" | "error"
        self.data = data
        self.timestamp = timestamp or time.time()
        self.delegation_id = delegation_id

    def __repr__(self):
        return f"SubtaskEvent(task={self.task_index}, type={self.event_type})"


SUBAGENT_STARTED = "started"
SUBAGENT_TOOL_CALL = "tool_call"
SUBAGENT_FINALIZING = "finalizing"
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
    working_directory: Optional[str] = None,
    max_iterations: int = 10,
    available_tool_names: Optional[List[str]] = None,
) -> str:
    """Build the lightweight child prompt without inheriting the parent prompt."""
    parts = [
        "你是一个子代理，负责完成父代理委托的独立任务。",
        "",
        f"任务目标：\n{goal}",
    ]
    if context and context.strip():
        parts.append(f"\n上下文信息：\n{context.strip()}")
    if working_directory and str(working_directory).strip():
        parts.append(
            f"\n当前工作目录：\n{working_directory.strip()}"
        )
    parts.append(
        "\n执行方式：\n"
        "- 以任务目标为准，结合上下文完成可独立处理的部分。\n"
        "- 文件工具和 terminal 拥有主机文件系统访问权限；优先在当前工作目录内完成任务。\n"
        "- 不要读取 .env 等凭据文件；凭据由运行时按作用域提供。\n"
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
    if any(name.startswith(("web_", "browser_")) for name in available_tool_names or []):
        from mclaw.agent.prompt_builder import WEB_CONTENT_SAFETY_GUIDANCE

        parts.append(WEB_CONTENT_SAFETY_GUIDANCE)
    return "\n".join(parts)


def _resolve_working_directory(parent_agent) -> Optional[str]:
    """Return the parent's current local working directory when available."""
    terminal_cwd = None
    parent_session_id = str(getattr(parent_agent, "session_id", "") or "")
    if parent_session_id:
        try:
            from mclaw.tools.terminal_tool import get_session_cwd

            terminal_cwd = get_session_cwd(parent_session_id)
        except ImportError:
            pass
    candidates = [
        terminal_cwd,
        getattr(parent_agent, "workspace_path", None),
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


def _persist_oversized_summary(child: Any, summary: str) -> str | None:
    """Persist a full child handoff while keeping the parent prompt compact."""
    delegation_dir = getattr(child, "_delegation_dir", None)
    if not delegation_dir or len(summary) <= MAX_INLINE_SUMMARY_CHARS:
        return None
    handoff_path = Path(delegation_dir) / f"mclaw_handoff_{uuid.uuid4().hex[:8]}.md"
    try:
        handoff_path.parent.mkdir(parents=True, exist_ok=True)
        handoff_path.write_text(summary, encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not persist full subagent handoff: %s", exc)
        return None
    return str(handoff_path)


def _dispose_child_agent(child: Any) -> None:
    """Close a child Agent before releasing its terminal-session state."""
    close = getattr(child, "close", None)
    child_session_id = getattr(child, "session_id", None)
    if not callable(close) and not child_session_id:
        return
    dispose_lock = getattr(child, "_delegate_dispose_lock", None)
    if dispose_lock is None:
        dispose_lock = threading.Lock()
        try:
            child._delegate_dispose_lock = dispose_lock
        except (AttributeError, TypeError):
            pass
    with dispose_lock:
        if getattr(child, "_delegate_disposed", False):
            return
        try:
            child._delegate_disposed = True
        except (AttributeError, TypeError):
            pass
    if callable(close):
        try:
            close()
        except BaseException:
            logger.warning("Could not close delegated child Agent", exc_info=True)
    if child_session_id:
        try:
            from mclaw.tools.terminal_tool import cleanup_session

            cleanup_session(child_session_id)
        except BaseException:
            logger.warning("Could not clean delegated child terminal state", exc_info=True)


def _strip_blocked_toolsets(toolsets: List[str]) -> List[str]:
    """Remove blocked toolset names from a requested child toolset list."""
    from mclaw.tools.toolsets import validate_toolset

    return [
        t for t in toolsets
        if (
            t in ALLOWED_DELEGATE_TOOLSETS
            and t not in _BLOCKED_TOOLSET_NAMES
            and validate_toolset(t, allow_platform=False, allow_scoped=False)
        )
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
    from mclaw.tools.toolsets import validate_toolset

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
        if not validate_toolset(toolset, allow_platform=False, allow_scoped=False):
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
    credentials, and installs filtered tool definitions without re-running
    discovery.
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

    # Keep child conversation artifacts separate without restricting filesystem access.
    child_session_id = f"delegate_{getattr(parent_agent, 'session_id', 'unknown')}_{task_index}_{uuid.uuid4().hex[:6]}"
    from mclaw.runtime.manager import RuntimeManager

    runtime = RuntimeManager.current(parent_config if isinstance(parent_config, dict) else None)
    delegation_root = runtime.paths.delegation_root()
    child_delegation_dir = delegation_root / child_session_id
    working_directory = _resolve_working_directory(parent_agent) or os.getcwd()

    # Build the child prompt.
    child_prompt = _build_child_system_prompt(
        goal,
        context,
        working_directory=working_directory,
        max_iterations=max_iterations,
        available_tool_names=list(safe_tool_names),
    )

    delegation_cfg = parent_config.get("delegation", {}) if isinstance(parent_config, dict) else {}
    delegation_cfg = delegation_cfg if isinstance(delegation_cfg, dict) else {}
    parent_runtime = parent_agent.provider_runtime
    child_model = str(delegation_cfg.get("model") or "").strip()
    child_provider = str(delegation_cfg.get("provider") or "").strip()
    child_base_url = str(delegation_cfg.get("base_url") or "").strip()

    from mclaw.providers.resolver import (
        default_model_for_provider,
        resolve_provider_runtime_context,
        restore_provider_runtime_context,
    )

    inherited_provider = not child_provider or child_provider.casefold() == "auto"
    if inherited_provider:
        if child_base_url:
            custom_provider = (
                "custom_anthropic"
                if parent_runtime.api_mode == "anthropic_messages"
                else "custom"
            )
            child_runtime = resolve_provider_runtime_context(
                model=child_model or parent_runtime.model,
                provider=custom_provider,
                base_url=child_base_url,
                api_key=parent_runtime.api_key,
                config=parent_config if isinstance(parent_config, dict) else None,
            )
        elif child_model:
            child_runtime = restore_provider_runtime_context(
                parent_runtime.snapshot(),
                config=parent_config if isinstance(parent_config, dict) else None,
                model=child_model,
                api_key=parent_runtime.api_key,
            )
        else:
            child_runtime = parent_runtime
    else:
        target_model = child_model
        if not target_model:
            target_model = default_model_for_provider(
                child_provider,
                config=parent_config if isinstance(parent_config, dict) else None,
            )
        child_runtime = resolve_provider_runtime_context(
            model=target_model,
            provider=child_provider,
            base_url=child_base_url,
            config=parent_config if isinstance(parent_config, dict) else None,
        )

    # Create the child agent.
    child = MClaw(
        provider_runtime=child_runtime,
        system_prompt=child_prompt,          # inject lightweight prompt directly
        skip_memory=True,                    # disable the memory subsystem
        enabled_toolsets=child_toolsets,     # restricted toolset
        max_iterations=max_iterations,
        session_db=getattr(parent_agent, "_session_db", None),
        session_id=child_session_id,
        parent_session_id=getattr(parent_agent, "session_id", None),
        workspace=working_directory,
        config=parent_config,
    )

    # Install the already-filtered tool definitions without rediscovery.
    child.tools = safe_tool_defs
    child.valid_tool_names = set(safe_tool_names)

    # Set child depth for recursive-delegation enforcement.
    parent_depth = getattr(parent_agent, "_delegate_depth", 0)
    child._delegate_depth = parent_depth + 1
    child._turn_worker_parent = parent_agent

    # Keep a private handoff location for oversized summaries only.
    child._delegation_dir = child_delegation_dir

    # Inherit parent callbacks for streaming/status plumbing.
    child._print_fn = getattr(parent_agent, "_print_fn", print)
    child._stream_callback = getattr(parent_agent, "_stream_callback", None)
    child._tool_callback = getattr(parent_agent, "_tool_callback", None)
    child._status_callback = getattr(parent_agent, "_status_callback", None)

    logger.info(
        "[subagent-%d] 已构建, cwd=%s, delegation=%s, tools=%s, depth=%d",
        task_index, working_directory, str(child_delegation_dir),
        list(safe_tool_names), child._delegate_depth
    )

    return child

def _run_single_child(
    task_index: int,
    goal: str,
    child: "MClaw",
    parent_agent,
    progress_callback: ProgressCallback = None,
    timeout_seconds: float | None = None,
    cancel_event: threading.Event | None = None,
    delegation_id: str = "",
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
    try:
        if progress_callback:
            def _relay_tool(tool_name: str, args: dict):
                progress_callback(SubtaskEvent(
                    task_index, SUBAGENT_TOOL_CALL,
                    {"tool": tool_name, "args_bytes": len(str(args))},
                    delegation_id=delegation_id,
                ))

            child._tool_callback = _relay_tool

            # Emit a started event.
            progress_callback(SubtaskEvent(
                task_index, SUBAGENT_STARTED,
                {"goal": goal[:100], "depth": child._delegate_depth},
                delegation_id=delegation_id,
            ))
    except BaseException:
        _dispose_child_agent(child)
        raise

    api_calls_before = int(getattr(child, "session_api_calls", 0) or 0)
    try:
        # Run the child without parent conversation history.
        logger.info("[subagent-%d] entering run_conversation", task_index)
        effective_goal = goal
        summary_instruction = (
            "请基于以上任务和已经获得的全部工具结果，直接给出最终总结；不要再调用工具。"
        )
        original_max_iterations = child.max_iterations
        original_tools = child.tools
        call_limit = max(1, int(original_max_iterations))
        exploration_stop_reason: str | None = None
        try:
            if call_limit == 1:
                result = child.run_conversation(
                    user_message=f"{effective_goal}\n\n{summary_instruction}",
                    disable_tools=True,
                    call_source="delegation",
                    cancel_event=cancel_event,
                )
            else:
                child.max_iterations = call_limit - 1
                deadline = (
                    time.monotonic() + timeout_seconds
                    if timeout_seconds is not None and timeout_seconds > 0
                    else None
                )
                result = child.run_conversation(
                    user_message=effective_goal,
                    call_source="delegation",
                    deadline_monotonic=deadline,
                    cancel_event=cancel_event,
                )
                exploration_api_calls = int(result.get("api_calls", 0) or 0)
                exploration_stop_reason = result.get("stop_reason")
                if (
                    (not result.get("final_response") or result.get("error"))
                    and not result.get("interrupted", False)
                ):
                    if progress_callback:
                        progress_callback(SubtaskEvent(
                            task_index,
                            SUBAGENT_FINALIZING,
                            {"reason": exploration_stop_reason or "max_iterations"},
                            delegation_id=delegation_id,
                        ))
                    child.max_iterations = 1
                    result = child.run_conversation(
                        user_message=summary_instruction,
                        conversation_history=result.get("messages") or [],
                        disable_tools=True,
                        advance_background_review=False,
                        call_source="delegation",
                        cancel_event=cancel_event,
                    )
                    result["api_calls"] = exploration_api_calls + int(
                        result.get("api_calls", 0) or 0
                    )
                    result["stop_reason"] = exploration_stop_reason
        finally:
            child.max_iterations = original_max_iterations
            child.tools = original_tools
        logger.info("[subagent-%d] run_conversation returned", task_index)

        duration = round(time.monotonic() - child_start, 2)

        summary = result.get("final_response") or ""
        completed = result.get("completed", False)
        interrupted = result.get("interrupted", False)
        abort_reason = str(result.get("abort_reason") or "")
        api_calls = result.get("api_calls", 0)

        if abort_reason == "tool_timeout":
            status = "timed_out"
            exit_reason = "tool_timeout"
        elif abort_reason == "tool_completion_unknown":
            status = "completion_unknown"
            exit_reason = "tool_completion_unknown"
        elif interrupted:
            status = "interrupted"
            exit_reason = "interrupted"
        elif summary and not result.get("error"):
            if exploration_stop_reason == "timeout":
                status = "timed_out"
                exit_reason = "timeout"
            else:
                status = "completed"
                exit_reason = "completed" if completed else "max_iterations"
        else:
            status = "failed"
            exit_reason = "error" if result.get("error") else "max_iterations"

        logger.info(
            "[subagent-%d] 完成, duration=%.2fs, status=%s, exit=%s",
            task_index, duration, status, exit_reason
        )

        # Collect token statistics.
        input_tokens = getattr(child, "session_input_tokens", 0) or 0
        output_tokens = getattr(child, "session_output_tokens", 0) or 0

        summary_path = _persist_oversized_summary(child, summary)
        inline_summary = summary
        if summary_path:
            inline_summary = (
                summary[:MAX_INLINE_SUMMARY_CHARS]
                + "……[完整结果已保存到交接文件，父代理必须读取]"
            )

        entry: Dict[str, Any] = {
            "task_index": task_index,
            "goal": goal,
            "status": status,
            "summary": inline_summary,
            "api_calls": api_calls,
            "duration_seconds": duration,
            "model": child.model if isinstance(child.model, str) else None,
            "exit_reason": exit_reason,
            "timed_out": abort_reason == "tool_timeout" or exploration_stop_reason == "timeout",
            "tokens": {
                "input": input_tokens if isinstance(input_tokens, (int, float)) else 0,
                "output": output_tokens if isinstance(output_tokens, (int, float)) else 0,
            },
            "tool_trace": [],
        }
        if summary_path:
            entry["summary_path"] = summary_path

        if status == "failed":
            entry["error"] = result.get("error", "子代理未产生响应")

        # Emit a completed event.
        if progress_callback:
            progress_callback(SubtaskEvent(
                task_index, SUBAGENT_COMPLETED,
                {
                    "status": status,
                    "duration": duration,
                    "summary": inline_summary,
                    "api_calls": api_calls,
                },
                delegation_id=delegation_id,
            ))

        return entry

    except Exception as exc:
        duration = round(time.monotonic() - child_start, 2)
        logger.error("[subagent-%d] 异常: %s", task_index, exc)

        # Emit an error event.
        if progress_callback:
            progress_callback(SubtaskEvent(
                task_index, SUBAGENT_ERROR,
                {"error": str(exc)},
                delegation_id=delegation_id,
            ))

        return {
            "task_index": task_index,
            "goal": goal,
            "status": "error",
            "summary": None,
            "error": str(exc),
            "api_calls": max(
                0,
                int(getattr(child, "session_api_calls", 0) or 0) - api_calls_before,
            ),
            "duration_seconds": duration,
        }
    finally:
        _dispose_child_agent(child)

def _run_all_children_background(
    task_list: list,
    children: list,
    parent_agent,
    progress_callback: ProgressCallback,
    task_id: str,
    start_time: float,
    timeout_seconds: float,
    cancel_event: threading.Event | None = None,
) -> None:
    """Run all children in a daemon thread and queue their final results.

    Non-blocking TUI mode cannot wait on every child future inline. Results are
    therefore placed on the module queue and later matched by task id by the
    runtime command layer.
    """
    results: List[Dict[str, Any]] = []
    goal_map = {i: task["goal"] for i, task, _child in children}

    # This coordinator may run off the TUI thread, but it does not hand results
    # to the parent until every child has finished or produced a timeout summary.
    with ThreadPoolExecutor(max_workers=min(len(children), MAX_CONCURRENT_CHILDREN)) as executor:
        futures = {}
        for i, task, child in children:
            fut = executor.submit(
                _run_single_child,
                task_index=i,
                goal=task["goal"],
                child=child,
                parent_agent=parent_agent,
                progress_callback=progress_callback,
                timeout_seconds=timeout_seconds,
                cancel_event=cancel_event,
                delegation_id=task_id,
            )
            futures[fut] = i

        for future, idx in futures.items():
            try:
                entry = future.result()
            except Exception as exc:
                logger.error("[subagent-%d] 执行异常: %s", idx, exc)
                entry = {
                    "task_index": idx,
                    "goal": goal_map.get(idx, ""),
                    "status": "error",
                    "summary": None,
                    "error": str(exc),
                    "api_calls": 0,
                    "duration_seconds": round(time.time() - start_time, 2),
                }
                if progress_callback:
                    progress_callback(SubtaskEvent(
                        idx, SUBAGENT_ERROR,
                        {"error": str(exc)},
                        delegation_id=task_id,
                    ))
            results.append(entry)

    results.sort(key=lambda r: r["task_index"])
    if cancel_event is not None and cancel_event.is_set():
        return
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
    configured_timeout = delegation_cfg.get(
        "timeout_seconds", DEFAULT_SUBAGENT_TIMEOUT_SECONDS
    )
    effective_timeout = normalize_timeout_seconds(configured_timeout)

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
        for _index, _task, built_child in children:
            _dispose_child_agent(built_child)
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
    cancel_event = get_interrupt_event()

    if cancel_event is not None and cancel_event.is_set():
        for _index, _task, child in children:
            _dispose_child_agent(child)
        return tool_error(
            "delegate_task was not started because the turn was cancelled",
            success=False,
            interrupted=True,
            status="cancelled",
        )

    if progress_callback is not None:
        # Keep collection off the TUI thread. The coordinator waits for every
        # child to finalize before publishing one complete result set.
        def _background_target() -> None:
            try:
                _run_all_children_background(
                    task_list,
                    children,
                    parent_agent,
                    progress_callback,
                    task_id,
                    start_time,
                    effective_timeout,
                    cancel_event,
                )
            finally:
                for _index, _task, child in children:
                    _dispose_child_agent(child)
                unregister = getattr(parent_agent, "_unregister_turn_worker", None)
                if callable(unregister):
                    unregister(threading.current_thread())

        thread = threading.Thread(target=_background_target, daemon=True)
        register = getattr(parent_agent, "_register_turn_worker", None)
        if callable(register):
            register(thread)
        try:
            thread.start()
        except BaseException:
            for _index, _task, child in children:
                _dispose_child_agent(child)
            unregister = getattr(parent_agent, "_unregister_turn_worker", None)
            if callable(unregister):
                unregister(thread)
            raise

        task_info = {
            "task_id": task_id,
            "num_tasks": len(task_list),
            "goals": [t["goal"] for t in task_list],
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
        result = _run_single_child(
            0,
            children[0][1]["goal"],
            child,
            parent_agent,
            timeout_seconds=effective_timeout,
            cancel_event=cancel_event,
        )
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
                    timeout_seconds=effective_timeout,
                    cancel_event=cancel_event,
                )
                futures[future] = i

            for future, idx in futures.items():
                try:
                    entry = future.result()
                except Exception as exc:
                    logger.error("[subagent-%d] 执行异常: %s", idx, exc)
                    entry = {
                        "task_index": idx,
                        "goal": goal_map.get(idx, ""),
                        "status": "error",
                        "summary": None,
                        "error": str(exc),
                        "api_calls": 0,
                        "duration_seconds": round(time.monotonic() - overall_start, 2),
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
