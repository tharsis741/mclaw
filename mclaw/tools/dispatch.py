# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Discover tools, collect schemas, and dispatch model tool calls.

This module is the boundary between model-emitted tool calls and registered
Python handlers. It imports tool modules, builds the enabled schema set,
enforces per-call policy, chooses concurrent dispatch only for safe read-only
batches, wraps write tools with checkpoint/file-safety handling, and truncates
oversized results before they enter conversation history.
"""

import asyncio
import json
import logging
import math
import os
import threading
import time
from concurrent.futures import Future, TimeoutError as FutureTimeout
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from pathlib import Path
from typing import Any

from mclaw.safety.mutation_detector import terminal_action
from mclaw.safety.path_resolver import repair_common_mojibake
from mclaw.tools.interrupt import (
    get_cancel_id,
    get_interrupt_event,
    reset_interrupt_event,
    safe_cancel_trace,
    set_interrupt_event,
)
from mclaw.tools.registry import registry
from mclaw.tools.toolsets import DSOFTBUS_TOOLS, resolve_multiple_toolsets

logger = logging.getLogger(__name__)

_DSOFTBUS_INBOUND_FORBIDDEN_TOOLS = frozenset(DSOFTBUS_TOOLS)


def _dsoftbus_forbidden_result(tool_name: str) -> str:
    from mclaw.tools.registry import tool_error

    return tool_error(
        f"Tool '{tool_name}' is not available to an inbound DSoftBus agent.",
        code="AGENT_TOOLS_FORBIDDEN",
        success=False,
    )

# Per-call context set by core.py before handle_function_calls and read by tools.
_current_session_db: ContextVar[Any] = ContextVar("current_session_db", default=None)
_current_session_id: ContextVar[str] = ContextVar("current_session_id", default="")
_current_task_id: ContextVar[str] = ContextVar("current_task_id", default="")
_tool_whitelist: ContextVar[set[str] | None] = ContextVar("tool_whitelist", default=None)
_tool_action_whitelist: ContextVar[dict[str, set[str]] | None] = ContextVar("tool_action_whitelist", default=None)


def set_tool_context(
    session_db: Any = None,
    session_id: str = "",
    cancel_event: threading.Event | None = None,
) -> tuple:
    """Set context for the duration of tool execution. Call from core.py."""
    return (
        _current_session_db.set(session_db),
        _current_session_id.set(session_id),
        set_interrupt_event(cancel_event),
    )


def reset_tool_context(tokens: tuple) -> None:
    """Restore the context that preceded one ``set_tool_context`` call."""
    session_db_token, session_id_token, interrupt_token = tokens
    reset_interrupt_event(interrupt_token)
    _current_session_id.reset(session_id_token)
    _current_session_db.reset(session_db_token)


def get_session_db() -> Any:
    """Get the current SessionDB instance from context."""
    return _current_session_db.get()


def get_current_session_id() -> str:
    """Get the current session_id from context."""
    return _current_session_id.get()


def set_current_task_id(task_id: str) -> Any:
    """Bind an owning Task id across Agent and tool worker context copies."""
    return _current_task_id.set(str(task_id or ""))


def reset_current_task_id(token: Any) -> None:
    """Restore the Task id that preceded a remote Agent execution."""
    _current_task_id.reset(token)


def get_current_task_id() -> str:
    """Return the Task id that owns processes spawned by the current tool call."""
    return _current_task_id.get()


@contextmanager
def tool_dispatch_policy(
    *,
    tool_whitelist: set[str] | None = None,
    action_whitelist: dict[str, set[str]] | None = None,
):
    """Temporarily restrict tool names and per-tool action values."""
    token_tools = _tool_whitelist.set(set(tool_whitelist) if tool_whitelist is not None else None)
    normalized_actions = None
    if action_whitelist is not None:
        normalized_actions = {str(k): set(v) for k, v in action_whitelist.items()}
    token_actions = _tool_action_whitelist.set(normalized_actions)
    try:
        yield
    finally:
        _tool_action_whitelist.reset(token_actions)
        _tool_whitelist.reset(token_tools)


def _policy_error(tool_name: str, arguments: dict[str, Any]) -> str | None:
    """Return a JSON policy error when contextual restrictions reject a call."""
    from mclaw.tools.registry import tool_error

    whitelist = _tool_whitelist.get()
    if whitelist is not None and tool_name not in whitelist:
        return tool_error(f"Tool '{tool_name}' is not allowed in this execution context.", success=False)

    action_whitelist = _tool_action_whitelist.get()
    allowed_actions = action_whitelist.get(tool_name) if action_whitelist else None
    if allowed_actions is not None:
        action = str(arguments.get("action") or "").strip()
        if action not in allowed_actions:
            return tool_error(
                f"Action '{action}' for tool '{tool_name}' is not allowed in this execution context.",
                success=False,
            )
    return None


# ── Tool classification for concurrent dispatch ──────────────────────────────

# Tools that must remain serial because their semantics depend on order.
_NEVER_PARALLEL_TOOLS = frozenset()

# Read-only tools that may run concurrently after path-overlap checks.
_PARALLEL_SAFE_TOOLS = frozenset({
    "read_file", "search_files",
    "skill_view", "skill_tree", "skills_list", "skill_search", "vision_analyze",
    "web_search", "web_extract",
})

# Path-scoped tools need overlap checks before concurrent dispatch.
_PATH_SCOPED_TOOLS = frozenset({"read_file", "write_file", "patch", "edit_file", "delete_file", "skill_manage"})

_CHECKPOINT_METADATA_REDACT_KEYS = {
    "content",
    "new_content",
    "old_content",
    "replacement",
    "text",
    "stdin",
    "input",
    "password",
    "token",
    "api_key",
    "secret",
}
_INTERRUPT_POLL_INTERVAL = 0.05
_INTERRUPT_CLEANUP_GRACE = 1.0
_ASYNC_HANDLER_TIMEOUT_SECONDS = 600.0
_DEFAULT_CONCURRENT_TOOL_TIMEOUT = 30
_CONCURRENT_TOOL_TIMEOUTS = {
    "web_search": 180,
    "web_extract": 180,
    "vision_analyze": 180,
    "skill_search": 60,
}

_TOOL_MODULES = (
    "mclaw.tools.file_tools",
    "mclaw.tools.secret_tool",
    "mclaw.tools.terminal_tool",
    "mclaw.tools.process_registry",
    "mclaw.tools.memory_tool",
    "mclaw.tools.skill_tools",
    "mclaw.tools.session_search_tool",
    "mclaw.tools.delegate_tool",
    "mclaw.tools.vision_tool",
    "mclaw.tools.web_search_tool",
    "mclaw.tools.web_extract_tool",
    "mclaw.tools.browser_tool",
    "mclaw.tools.weixin_tool",
    "mclaw.tools.dingtalk_tool",
    "mclaw.dsoftbus.tools",
)
_discovery_done = False
_discovery_failed_modules: set[str] = set()
_discovery_lock = threading.Lock()

_BROWSER_WEB_PRIORITY = "For information retrieval, prefer web_search or web_extract."

_worker_loop = None
_worker_loop_lock = threading.Lock()


def _get_worker_loop():
    """Return the shared event loop used to run async tool handlers."""
    global _worker_loop
    with _worker_loop_lock:
        if _worker_loop is None or _worker_loop.is_closed():
            if _worker_loop is not None:
                try:
                    _worker_loop.close()
                except Exception:
                    logger.debug("Failed to close stale worker event loop", exc_info=True)
            _worker_loop = asyncio.new_event_loop()
            t = threading.Thread(target=_worker_loop.run_forever, daemon=True)
            t.start()
        return _worker_loop


class _AsyncHandlerFence:
    """Track the real lifetime of one async handler, including slow cancellation."""

    def __init__(self, diagnostic_name: str = "async_tool_handler") -> None:
        self.future: Future | None = None
        self._task: asyncio.Task | None = None
        self._lock = threading.Lock()
        self._finished = threading.Event()
        self._cancel_requested = False
        self._outcome_ready = False
        self._outcome = None
        self._outcome_error: BaseException | None = None
        self.created_at = time.monotonic()
        self.diagnostic_name = diagnostic_name

    @property
    def blocking_reason(self) -> str:
        return (
            "An async tool handler is still shutting down after cancellation "
            f"({max(0.0, time.monotonic() - self.created_at):.1f}s)"
        )

    def is_alive(self) -> bool:
        return not self._finished.is_set()

    def bind_task(self, task: asyncio.Task) -> bool:
        """Bind the real asyncio task and report a cancellation raced submission."""
        with self._lock:
            self._task = task
            return self._cancel_requested

    def request_cancel(self, loop: asyncio.AbstractEventLoop) -> None:
        """Request Task cancellation without marking the proxy Future done early."""
        with self._lock:
            self._cancel_requested = True
            task = self._task
        if task is not None:
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                # A stopped loop cannot prove that the coroutine reached a terminal
                # state. Leave the fence alive so the parent remains fail-closed.
                safe_cancel_trace(
                    lambda: logger.debug(
                        "Async tool loop stopped before cancellation was delivered"
                    )
                )

    def set_terminal_outcome(
        self,
        *,
        result: Any = None,
        error: BaseException | None = None,
    ) -> None:
        with self._lock:
            self._outcome_ready = True
            self._outcome = result
            self._outcome_error = error

    def terminal_outcome(self) -> tuple[bool, Any, BaseException | None]:
        with self._lock:
            return self._outcome_ready, self._outcome, self._outcome_error

    def finish(self) -> None:
        self._finished.set()


def _close_unsubmitted_coroutine(coro: Any) -> None:
    close = getattr(coro, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            logger.debug("Failed to close an unsubmitted async tool coroutine", exc_info=True)


def _run_async(
    coro,
    parent_agent: Any = None,
    *,
    diagnostic_name: str = "async_tool_handler",
    timeout_seconds: float | None = None,
    raise_on_stop: bool = False,
):
    """Bridge an async tool handler while fencing its real asyncio Task lifetime."""
    cancel_event = get_interrupt_event()
    if cancel_event is None:
        current_event = getattr(parent_agent, "current_turn_cancel_event", None)
        cancel_event = current_event() if callable(current_event) else None
    if cancel_event is not None and cancel_event.is_set():
        _close_unsubmitted_coroutine(coro)
        if raise_on_stop:
            raise InterruptedError(f"{diagnostic_name} cancelled before start")
        return _cancelled_tool_result("async handler", started=False)

    loop = _get_worker_loop()
    fence = _AsyncHandlerFence(diagnostic_name)
    _register_turn_worker(parent_agent, fence)

    async def _tracked_handler():
        task = asyncio.current_task()
        cancel_raced_submission = task is not None and fence.bind_task(task)
        if cancel_raced_submission and task is not None:
            task.cancel()
        try:
            result = await coro
        except BaseException as exc:
            fence.set_terminal_outcome(error=exc)
            raise
        else:
            fence.set_terminal_outcome(result=result)
            return result
        finally:
            fence.finish()
            _unregister_turn_worker(parent_agent, fence)

    tracked = _tracked_handler()
    try:
        future = asyncio.run_coroutine_threadsafe(tracked, loop)
        fence.future = future
    except BaseException:
        tracked.close()
        _close_unsubmitted_coroutine(coro)
        fence.finish()
        _unregister_turn_worker(parent_agent, fence)
        raise

    handler_timeout = (
        _ASYNC_HANDLER_TIMEOUT_SECONDS
        if timeout_seconds is None
        else max(0.001, float(timeout_seconds))
    )
    deadline = time.monotonic() + handler_timeout
    while True:
        # Prefer a real terminal result when completion won the race with a
        # cancellation signal or the hard deadline.
        if future.done():
            return future.result()
        if cancel_event is not None and cancel_event.is_set():
            fence.request_cancel(loop)
            outcome_ready, outcome, outcome_error = fence.terminal_outcome()
            if outcome_ready and not isinstance(outcome_error, asyncio.CancelledError):
                safe_cancel_trace(
                    lambda: logger.info(
                        "[CANCEL_TRACE] async_handler_completion_won_stop "
                        "cancel_id=%s session=%s trigger=event success=%s elapsed_ms=%d",
                        get_cancel_id(cancel_event),
                        getattr(parent_agent, "session_id", "?"),
                        outcome_error is None,
                        int((time.monotonic() - fence.created_at) * 1000),
                    )
                )
                if outcome_error is not None:
                    raise outcome_error
                return outcome
            safe_cancel_trace(
                lambda: logger.warning(
                    "[CANCEL_TRACE] async_handler_cancel cancel_id=%s session=%s "
                    "trigger=event elapsed_ms=%d completion_unknown=true",
                    get_cancel_id(cancel_event),
                    getattr(parent_agent, "session_id", "?"),
                    int((time.monotonic() - fence.created_at) * 1000),
                )
            )
            if raise_on_stop:
                raise InterruptedError(f"{diagnostic_name} cancelled")
            return _cancelled_tool_result("async handler", started=True)

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            fence.request_cancel(loop)
            outcome_ready, outcome, outcome_error = fence.terminal_outcome()
            if outcome_ready and not isinstance(outcome_error, asyncio.CancelledError):
                safe_cancel_trace(
                    lambda: logger.info(
                        "[CANCEL_TRACE] async_handler_completion_won_stop "
                        "cancel_id=%s session=%s trigger=deadline success=%s elapsed_ms=%d",
                        get_cancel_id(cancel_event),
                        getattr(parent_agent, "session_id", "?"),
                        outcome_error is None,
                        int((time.monotonic() - fence.created_at) * 1000),
                    )
                )
                if outcome_error is not None:
                    raise outcome_error
                return outcome
            safe_cancel_trace(
                lambda: logger.warning(
                    "[CANCEL_TRACE] async_handler_cancel cancel_id=%s session=%s "
                    "trigger=deadline elapsed_ms=%d completion_unknown=true",
                    get_cancel_id(cancel_event),
                    getattr(parent_agent, "session_id", "?"),
                    int((time.monotonic() - fence.created_at) * 1000),
                )
            )
            if raise_on_stop:
                raise TimeoutError(
                    f"{diagnostic_name} exceeded its {handler_timeout}-second deadline"
                )
            return _timed_out_tool_result(
                diagnostic_name.replace("_", " "),
                handler_timeout,
            )
        try:
            return future.result(timeout=min(_INTERRUPT_POLL_INTERVAL, remaining))
        except FutureTimeout:
            continue


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _discover_tools():
    """Import all tool modules to trigger their registry.register() calls."""
    global _discovery_done
    with _discovery_lock:
        if _discovery_done and not _discovery_failed_modules:
            return
        if _discovery_done:
            modules_to_import = tuple(sorted(_discovery_failed_modules))
        else:
            modules_to_import = _TOOL_MODULES
            _discovery_done = True

    failed_modules: set[str] = set()
    for module_name in modules_to_import:
        try:
            __import__(module_name)
        except Exception as e:
            failed_modules.add(module_name)
            logger.warning("Tool module import failed: %s: %s", module_name, e, exc_info=True)

    with _discovery_lock:
        _discovery_failed_modules.difference_update(modules_to_import)
        _discovery_failed_modules.update(failed_modules)


def _scope_browser_web_priority(definitions: list[dict], valid_names: set[str]) -> None:
    """Keep browser_navigate cross-references limited to available web tools."""
    available_web = [name for name in ("web_search", "web_extract") if name in valid_names]
    replacement = (
        f"For information retrieval, prefer {' or '.join(available_web)}."
        if available_web
        else ""
    )
    if replacement == _BROWSER_WEB_PRIORITY:
        return
    for definition in definitions:
        function = definition.get("function", {})
        if function.get("name") != "browser_navigate":
            continue
        description = str(function.get("description") or "")
        function["description"] = description.replace(
            f"{_BROWSER_WEB_PRIORITY} ",
            f"{replacement} " if replacement else "",
        )
        return


def get_tool_definitions(
    enabled_toolsets: list[str] | None = None,
    disabled_toolsets: list[str] | None = None,
    config: dict | None = None,
) -> tuple[list[dict], set[str]]:
    """Return (tool_definitions, valid_tool_names) for the enabled toolsets."""
    _discover_tools()

    if enabled_toolsets:
        tool_names = resolve_multiple_toolsets(enabled_toolsets)
    else:
        tool_names = resolve_multiple_toolsets(["mclaw-required"])

    if disabled_toolsets:
        disabled_tools = resolve_multiple_toolsets(disabled_toolsets)
        tool_names -= disabled_tools

    if isinstance(config, dict):
        tools_config = config.get("tools", {})
        raw_disabled = tools_config.get("disabled", []) if isinstance(tools_config, dict) else []
        if isinstance(raw_disabled, (list, tuple, set)):
            tool_names -= {str(name) for name in raw_disabled}

    try:
        from mclaw.runtime.manager import RuntimeManager

        runtime = RuntimeManager.current(config)
        tool_names = runtime.features.filter_tool_names(tool_names)
    except Exception:
        logger.debug("Runtime feature filtering failed", exc_info=True)

    definitions = registry.get_definitions(tool_names, config=config)
    valid_names = {d["function"]["name"] for d in definitions}
    _scope_browser_web_priority(definitions, valid_names)
    return definitions, valid_names


def handle_function_call(
    tool_name: str,
    tool_args: dict[str, Any],
    task_id: str = "",
    session_id: str = "",
    enabled_tools: set[str] | None = None,
    call_source: str = "turn",
) -> str:
    """Dispatch a tool call by name, returning JSON string result."""
    _discover_tools()
    if (
        call_source == "dsoftbus"
        and tool_name in _DSOFTBUS_INBOUND_FORBIDDEN_TOOLS
    ):
        return _dsoftbus_forbidden_result(tool_name)
    if enabled_tools is not None and tool_name not in enabled_tools:
        return json.dumps(
            {"error": f"Tool '{tool_name}' is not enabled for this session."},
            ensure_ascii=False,
        )
    policy_error = _policy_error(tool_name, tool_args or {})
    if policy_error is not None:
        return policy_error
    return registry.dispatch(
        tool_name,
        tool_args,
        task_id=task_id,
        session_id=session_id,
        enabled_tools=enabled_tools,
    )


def get_toolset_for_tool(name: str) -> str | None:
    return registry.get_toolset_for_tool(name)


# ── Concurrent batch dispatch ─────────────────────────────────────────────────

def _paths_overlap(path1: str, path2: str) -> bool:
    """Return True if two paths share a common prefix (file/dir containment)."""
    try:
        p1 = Path(path1).resolve()
        p2 = Path(path2).resolve()
        try:
            return p1.is_relative_to(p2) or p2.is_relative_to(p1)
        except AttributeError:
            p1_s = os.path.normcase(str(p1))
            p2_s = os.path.normcase(str(p2))
            return os.path.commonpath([p1_s, p2_s]) in {p1_s, p2_s}
    except Exception:
        return True  # on resolve error, conservatively treat as overlapping


def _extract_path_from_args(tool_name: str, arguments: dict) -> str | None:
    """Extract file path from tool arguments for path-overlap detection."""
    if tool_name in _PATH_SCOPED_TOOLS:
        if tool_name == "skill_manage":
            # Managed Skill operations are scoped to M-Claw home storage.
            from mclaw.constants import get_skills_dir
            root = str(get_skills_dir().resolve())
            name = arguments.get("name", "")
            parts = [root]
            if name:
                parts.append(name)
            return str(Path(*parts))
        return arguments.get("path")
    return None


def _redact_checkpoint_value(value: Any, key: str = "") -> Any:
    key_l = key.lower()
    if key_l in _CHECKPOINT_METADATA_REDACT_KEYS or any(
        marker in key_l for marker in ("password", "token", "secret", "api_key")
    ):
        return "<redacted>"
    if isinstance(value, dict):
        return {str(k): _redact_checkpoint_value(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_checkpoint_value(item, key) for item in value[:20]]
    if isinstance(value, str):
        return value if len(value) <= 240 else value[:240] + "...<truncated>"
    return value


def _checkpoint_metadata(
    tool_name: str,
    arguments: dict,
    parent_agent: Any = None,
    operation_id: str = "",
) -> dict:
    safe_args = _redact_checkpoint_value(arguments)
    metadata = {
        "tool_name": tool_name,
        "tool_args_preview": json.dumps(safe_args, ensure_ascii=False, default=str)[:1000],
    }
    if parent_agent is not None:
        metadata.update({
            "session_id": getattr(parent_agent, "session_id", None),
            "turn_id": getattr(parent_agent, "_checkpoint_turn_id", None),
            "message_id_before_turn": getattr(parent_agent, "_checkpoint_message_id_before_turn", None),
            "messages_len_before_turn": getattr(parent_agent, "_checkpoint_messages_len_before_turn", None),
        })
    if operation_id:
        metadata["operation_id"] = operation_id
    return metadata


def _checkpoint_reason(tool_name: str, arguments: dict) -> str:
    if tool_name != "terminal":
        return f"before {tool_name}"
    command = repair_common_mojibake(str(arguments.get("command") or ""))
    return f"before terminal: {command[:60]}"


def _mutation_action(tool_name: str, arguments: dict) -> str:
    if tool_name in {"write_file", "patch", "edit_file", "delete_file"}:
        return tool_name
    if tool_name == "skill_manage":
        return str(arguments.get("action") or "skill_manage")
    if tool_name != "terminal":
        return tool_name
    return terminal_action(str(arguments.get("command") or ""))


def _file_safety_enabled(parent_agent: Any = None) -> bool:
    cfg = getattr(parent_agent, "config", {}) if parent_agent is not None else {}
    if not isinstance(cfg, dict):
        return True
    safety_cfg = cfg.get("file_safety", {})
    if isinstance(safety_cfg, bool):
        return safety_cfg
    if isinstance(safety_cfg, dict):
        return bool(safety_cfg.get("enabled", True))
    return True


def _operation_journal_enabled(parent_agent: Any = None) -> bool:
    cfg = getattr(parent_agent, "config", {}) if parent_agent is not None else {}
    if not isinstance(cfg, dict):
        return True
    safety_cfg = cfg.get("file_safety", {})
    if isinstance(safety_cfg, bool):
        return safety_cfg
    if isinstance(safety_cfg, dict):
        return bool(safety_cfg.get("enabled", True)) and bool(safety_cfg.get("journal_enabled", True))
    return True


def _build_file_safety_plan(tool_name: str, arguments: dict, checkpoint_manager: Any | None, parent_agent: Any = None):
    if not _file_safety_enabled(parent_agent):
        return None
    try:
        from mclaw.safety import MClawSafetyLayer

        cfg = getattr(parent_agent, "config", {}) if parent_agent is not None else {}
        layer = MClawSafetyLayer(
            config=cfg if isinstance(cfg, dict) else {},
            checkpoint_manager=checkpoint_manager,
        )
        return layer.plan(tool_name, arguments, parent_agent=parent_agent)
    except Exception:
        logger.debug("File safety planning failed for %s", tool_name, exc_info=True)
        return None


def _file_safety_block_error(
    tool_name: str,
    arguments: dict,
    checkpoint_manager: Any | None,
    parent_agent: Any = None,
) -> str | None:
    plan = _build_file_safety_plan(tool_name, arguments, checkpoint_manager, parent_agent)
    if not plan or not plan.mutates:
        return None
    decision = plan.decision
    if decision.allowed:
        return None
    from mclaw.tools.registry import tool_error

    return tool_error(
        f"File safety blocked {tool_name}: {decision.reason} (action={decision.action})",
        success=False,
    )


def _maybe_checkpoint_before_tool(
    tool_name: str,
    arguments: dict,
    checkpoint_manager: Any | None,
    parent_agent: Any = None,
) -> dict | None:
    """Take a pre-mutation checkpoint and start an operation journal entry."""
    if checkpoint_manager is None:
        return None
    operation = None
    journal = None
    try:
        if tool_name == "terminal":
            include_terminal = True
            cfg = getattr(parent_agent, "config", {}) if parent_agent is not None else {}
            if isinstance(cfg, dict):
                cp_cfg = cfg.get("checkpoints", {})
                if isinstance(cp_cfg, dict):
                    include_terminal = bool(cp_cfg.get("include_terminal", True))
            if not include_terminal:
                return None

        plan = _build_file_safety_plan(tool_name, arguments, checkpoint_manager, parent_agent)
        if not plan or not plan.mutates:
            return None
        work_dir = plan.workspace
        target_paths = plan.target_paths
        raw_command = plan.intent.raw_command
        if not work_dir:
            return None
        if _operation_journal_enabled(parent_agent):
            try:
                from mclaw.safety.operation_journal import default_journal
                journal = default_journal()
                operation = journal.begin(
                    session_id=getattr(parent_agent, "session_id", "") if parent_agent is not None else "",
                    turn_id=getattr(parent_agent, "_checkpoint_turn_id", "") if parent_agent is not None else "",
                    message_id_before_turn=getattr(parent_agent, "_checkpoint_message_id_before_turn", None)
                    if parent_agent is not None else None,
                    messages_len_before_turn=getattr(parent_agent, "_checkpoint_messages_len_before_turn", None)
                    if parent_agent is not None else None,
                    tool_call_id=str(arguments.get("_tool_call_id") or ""),
                    tool_name=tool_name,
                    action=_mutation_action(tool_name, arguments),
                    cwd=str(work_dir),
                    workspace=str(work_dir),
                    raw_command=raw_command,
                    targets=target_paths,
                    risk=getattr(plan.decision, "level", "normal"),
                    checkpoint_reason=_checkpoint_reason(tool_name, arguments),
                    cancel_event=get_interrupt_event(),
                )
                if operation is not None and parent_agent is not None:
                    op_map = getattr(parent_agent, "_tool_operation_ids", None)
                    if op_map is None:
                        op_map = {}
                        setattr(parent_agent, "_tool_operation_ids", op_map)
                    tool_call_id = operation.get("tool_call_id")
                    if tool_call_id:
                        op_map[tool_call_id] = operation.get("operation_id")
            except InterruptedError:
                raise
            except Exception:
                logger.debug("Operation journal begin failed for %s", tool_name, exc_info=True)
        checkpoint_manager.ensure_checkpoint(
            work_dir,
            _checkpoint_reason(tool_name, arguments),
            metadata=_checkpoint_metadata(
                tool_name,
                arguments,
                parent_agent,
                operation_id=(operation or {}).get("operation_id", ""),
            ),
            target_paths=target_paths or None,
        )
        attempt = getattr(checkpoint_manager, "last_attempt", {}) or {}
        if operation is not None and journal is not None:
            try:
                journal.update_checkpoint(
                    operation,
                    checkpoint_commit=attempt.get("commit"),
                    checkpoint_status=attempt.get("status"),
                    checkpoint_reason=attempt.get("reason"),
                )
            except Exception:
                logger.debug("Operation journal checkpoint update failed for %s", tool_name, exc_info=True)
        if parent_agent is not None and attempt.get("status") == "taken":
            setattr(parent_agent, "_last_checkpoint_work_dir", attempt.get("working_dir"))
            setattr(parent_agent, "_last_checkpoint_targets", attempt.get("target_paths") or [])
            setattr(parent_agent, "_last_checkpoint_attempt", attempt)
        return operation
    except InterruptedError:
        return operation
    except Exception as exc:
        if getattr(exc, "termination_fence", None) is not None:
            raise
        logger.debug("Checkpoint preflight failed for %s", tool_name, exc_info=True)
        return None


def _tool_result_success(result: str) -> bool:
    try:
        parsed = json.loads(result)
    except (json.JSONDecodeError, TypeError):
        return True
    if isinstance(parsed, dict):
        if (
            parsed.get("requires_confirmation") is True
            and parsed.get("confirmation_type") == "skill_enable_drafting"
        ):
            return True
        if parsed.get("success") is False:
            return False
        if parsed.get("error") and parsed.get("success") is not True:
            return False
    return True


def _tool_result_completion_unknown(result: Any) -> bool:
    """Return whether a structured tool result leaves external effects unresolved."""
    if not isinstance(result, str):
        return False
    try:
        parsed = json.loads(result)
    except (json.JSONDecodeError, TypeError):
        return False
    return isinstance(parsed, dict) and parsed.get("completion_unknown") is True


def _finalize_operation(operation: dict | None, result: str, success: bool, parent_agent: Any = None) -> None:
    """Finalize an operation journal entry after tool execution."""
    if not operation:
        return
    try:
        from mclaw.safety.operation_journal import default_journal
        default_journal().finalize(
            operation,
            success=success,
            result_preview=result if isinstance(result, str) else str(result),
            after_commit=None,
            cancel_event=get_interrupt_event(),
        )
    except Exception:
        logger.debug("Operation journal finalize failed", exc_info=True)


def _should_parallelize_tool_batch(calls: list) -> bool:
    """Determine if a batch of tool calls should run concurrently.

    The dispatch layer only parallelizes independent read-only operations:
      1. Single call: never parallel
      2. JSON parse failure / non-dict args: never parallel
      3. Path overlap on _PATH_SCOPED_TOOLS: never parallel
      4. Any tool not in _PARALLEL_SAFE_TOOLS: never parallel
      5. Otherwise: parallel
    """
    if len(calls) <= 1:
        return False

    reserved_paths: list[str] = []

    for call in calls:
        func = call.get("function", {})
        name = func.get("name", "")

        if name in _NEVER_PARALLEL_TOOLS:
            return False

        try:
            raw_args = func.get("arguments", "{}")
            if isinstance(raw_args, str):
                args = json.loads(raw_args)
            elif isinstance(raw_args, dict):
                args = raw_args
            else:
                return False
        except (json.JSONDecodeError, TypeError):
            return False
        if not isinstance(args, dict):
            return False

        path = _extract_path_from_args(name, args)
        if path:
            for reserved in reserved_paths:
                if _paths_overlap(reserved, path):
                    return False
            reserved_paths.append(path)

        if name not in _PARALLEL_SAFE_TOOLS:
            return False

    return True


def _concurrent_tool_timeout(tool_name: str, parent_agent: Any = None) -> int:
    """Return timeout for a concurrent read-only tool."""
    timeout = _CONCURRENT_TOOL_TIMEOUTS.get(tool_name, _DEFAULT_CONCURRENT_TOOL_TIMEOUT)
    cfg = getattr(parent_agent, "config", {}) if parent_agent is not None else {}
    if isinstance(cfg, dict):
        auxiliary = cfg.get("auxiliary", {})
        if isinstance(auxiliary, dict):
            if tool_name == "web_search":
                web_cfg = auxiliary.get("web_search", {})
                if not isinstance(web_cfg, dict):
                    web_cfg = {}
                fast_timeout = _positive_int(web_cfg.get("tavily_timeout"), timeout)
                backend = str(web_cfg.get("backend") or "auto").lower()
                try:
                    from mclaw.tools.search.config import parse_bool_config

                    fallback = parse_bool_config(web_cfg.get("fallback"), default=True)
                except Exception:
                    fallback = True
                slow_timeout = max(
                    _positive_int(web_cfg.get("dashscope_timeout"), 90),
                    _positive_int(web_cfg.get("dashscope_deep_timeout"), 120),
                )
                if backend == "dashscope":
                    timeout = slow_timeout + 10
                elif backend == "tavily":
                    timeout = fast_timeout + (slow_timeout if fallback else 0) + 10
                elif fallback:
                    timeout = fast_timeout + slow_timeout + 10
                else:
                    timeout = max(fast_timeout, slow_timeout) + 10
            elif tool_name == "web_extract":
                extract_cfg = auxiliary.get("web_extract", {})
                if isinstance(extract_cfg, dict):
                    request_timeout = _positive_int(extract_cfg.get("timeout"), 30)
                    timeout = request_timeout * 5 + 30
            elif tool_name == "vision_analyze":
                vision_cfg = auxiliary.get("vision", {})
                if isinstance(vision_cfg, dict):
                    model_timeout = _positive_int(vision_cfg.get("timeout"), timeout)
                    download_timeout = _positive_int(vision_cfg.get("download_timeout"), 30)
                    timeout = 2 * model_timeout + 3 * download_timeout + 16
    return max(1, timeout)


def _serial_tool_timeout(tool_name: str, func: dict, parent_agent: Any = None) -> float:
    """Return per-call timeout for tools that must run on the serial path."""
    timeout = 120
    cfg = getattr(parent_agent, "config", {}) if parent_agent is not None else {}
    if not isinstance(cfg, dict):
        cfg = {}
    try:
        args = json.loads(func.get("arguments", "{}") or "{}")
    except (json.JSONDecodeError, TypeError, ValueError):
        args = {}
    if not isinstance(args, dict):
        args = {}

    if tool_name in {
        "dsoftbus_run_agent_task",
        "dsoftbus_continue_agent_task",
    }:
        # The Task lifecycle is terminated by completion, explicit CancelTask,
        # operation-specific limits, or Runtime shutdown.  The serial worker
        # remains cancellation-aware while deliberately having no total cap.
        return math.inf

    if tool_name == "terminal":
        try:
            from mclaw.tools.terminal_tool import _resolve_timeout
        except ImportError:
            return timeout
        # Leave cleanup headroom beyond the command timeout. The runtime owns
        # process-tree termination and pipe-drain bounds; this is an outer cap.
        return _resolve_timeout(args.get("timeout"), cfg) + 18

    if tool_name == "process" and args.get("action") == "wait":
        terminal_cfg = cfg.get("terminal", {})
        if not isinstance(terminal_cfg, dict):
            terminal_cfg = {}
        default_wait = _positive_int(terminal_cfg.get("timeout"), 180)
        requested_wait = _positive_int(args.get("timeout"), default_wait)
        return min(requested_wait, default_wait) + 5

    if tool_name == "delegate_task":
        from mclaw.tools.delegate_tool import normalize_timeout_seconds

        delegation_cfg = cfg.get("delegation", {})
        if not isinstance(delegation_cfg, dict):
            delegation_cfg = {}
        configured = normalize_timeout_seconds(
            delegation_cfg.get("timeout_seconds", 600)
        )
        return configured + 30

    return timeout


def _invoke_tool_builtin(
    tool_name: str,
    arguments: dict,
    memory_manager: Any = None,
    parent_agent: Any = None,
) -> str | None:
    """Route memory tools through the active memory manager before registry dispatch."""
    # Memory tools route through MemoryManager so provider config and snapshot refresh apply.
    if memory_manager is not None and memory_manager.has_tool(tool_name):
        return memory_manager.handle_tool_call(tool_name, arguments)
    return None


def _truncate_json_value(obj, excess):
    """Recursively trim longest string by ``excess`` chars.

    Returns ``(new_obj, trimmed_chars)``.  Only performs one truncation
    per call so the caller can re-serialize and re-check size.
    """
    if isinstance(obj, str):
        if excess <= 0:
            return obj, 0
        # Keep at least 100 chars of original content
        trim = min(excess, max(0, len(obj) - 100))
        if trim > 0:
            truncated = obj[: len(obj) - trim]
            suffix = f"\n[... {trim:,} chars truncated ...]"
            return truncated + suffix, trim
        return obj, 0

    if isinstance(obj, dict):
        # Trim the longest direct string field first.
        string_items = [
            (k, v) for k, v in obj.items() if isinstance(v, str) and len(v) > 100
        ]
        string_items.sort(key=lambda x: len(x[1]), reverse=True)
        for k, v in string_items:
            new_v, trimmed = _truncate_json_value(v, excess)
            if trimmed:
                obj = dict(obj)
                obj[k] = new_v
                return obj, trimmed

        # If no direct string is large enough, recurse into nested structures.
        for k, v in list(obj.items()):
            if isinstance(v, (dict, list)):
                new_v, trimmed = _truncate_json_value(v, excess)
                if trimmed:
                    obj = dict(obj)
                    obj[k] = new_v
                    return obj, trimmed
        return obj, 0

    if isinstance(obj, list):
        for i, item in enumerate(obj):
            if isinstance(item, (str, dict, list)):
                new_item, trimmed = _truncate_json_value(item, excess)
                if trimmed:
                    obj = list(obj)
                    obj[i] = new_item
                    return obj, trimmed
        return obj, 0

    return obj, 0


def _extract_json_object(s: str) -> dict | None:
    """Extract the first JSON object from a string with trailing content.

    Some providers append newlines or control characters after the closing
    brace. ``raw_decode`` parses the first valid object and leaves the
    remaining suffix outside the tool arguments.
    """
    if not s:
        return None
    start = s.find("{")
    if start == -1:
        return None
    decoder = json.JSONDecoder()
    try:
        obj, _ = decoder.raw_decode(s, start)
        if isinstance(obj, dict):
            return obj
    except (json.JSONDecodeError, ValueError):
        pass
    return None


def _dispatch_single(
    call: dict,
    tool_names: set,
    checkpoint_manager: Any | None,
    memory_manager: Any | None = None,
    parent_agent: Any = None,
    forbidden_tool_names: set[str] | frozenset[str] | None = None,
) -> str:
    """Single tool dispatch with availability check + optional checkpoint."""
    func = call.get("function", {})
    tool_name = func.get("name", "")
    raw_args = func.get("arguments", "{}")
    arguments: dict[str, Any] = {}
    try:
        if isinstance(raw_args, str):
            arguments = json.loads(raw_args)
        elif isinstance(raw_args, dict):
            arguments = raw_args
    except (json.JSONDecodeError, TypeError):
        if isinstance(raw_args, str):
            extracted = _extract_json_object(raw_args)
            if extracted is not None:
                arguments = extracted

    # Layer -1: source-specific hard boundary, independent of the schema list.
    if forbidden_tool_names is not None and tool_name in forbidden_tool_names:
        return _dsoftbus_forbidden_result(tool_name)

    # Layer 0: availability gate to block hallucinated tool names.
    if tool_name not in tool_names:
        from mclaw.tools.registry import tool_error
        return tool_error(
            f"Tool '{tool_name}' is not available. "
            f"Available tools: {', '.join(sorted(tool_names))}",
            success=False,
        )

    policy_error = _policy_error(tool_name, arguments)
    if policy_error is not None:
        return policy_error

    arguments_with_call = dict(arguments)
    if call.get("id"):
        arguments_with_call["_tool_call_id"] = call.get("id")
    safety_error = _file_safety_block_error(tool_name, arguments_with_call, checkpoint_manager, parent_agent)
    if safety_error is not None:
        return safety_error
    operation = _maybe_checkpoint_before_tool(tool_name, arguments_with_call, checkpoint_manager, parent_agent)

    # Safety/checkpoint preparation can perform I/O. Recheck immediately before
    # entering any handler so a cancellation during preparation cannot mutate.
    cancel_event = get_interrupt_event()
    if cancel_event is not None and cancel_event.is_set():
        result = _cancelled_tool_result(tool_name, started=False)
        _finalize_operation(operation, result, False, parent_agent)
        return result

    # Layer 1: direct builtins, including memory when a manager is provided.
    builtin_result = _invoke_tool_builtin(tool_name, arguments, memory_manager, parent_agent)
    if builtin_result is not None:
        _finalize_operation(operation, builtin_result, _tool_result_success(builtin_result), parent_agent)
        return builtin_result

    try:
        result = registry.dispatch(tool_name, arguments, parent_agent=parent_agent)
    except Exception as e:
        if getattr(e, "termination_fence", None) is not None:
            raise
        from mclaw.tools.registry import tool_error
        result = tool_error(str(e), success=False)
        _finalize_operation(operation, result, False, parent_agent)
        return result

    # Final result-size guard before tool output enters conversation history.
    # Individual tools should limit themselves first; this catches abnormal
    # payloads that would otherwise exceed the context budget.
    max_size = registry.get_max_result_size(tool_name)
    if max_size is not None and isinstance(result, str) and len(result) > max_size:
        # Structured truncation preserves JSON parseability.
        parsed = None
        try:
            parsed = json.loads(result)
        except (ValueError, TypeError):
            pass

        if parsed is not None:
            excess = len(result) - (max_size - 500)  # Reserve 500 chars for JSON wrapper
            while excess > 0:
                parsed, trimmed = _truncate_json_value(parsed, excess)
                if trimmed == 0:
                    break
                new_result = json.dumps(parsed, ensure_ascii=False)
                if len(new_result) >= len(result):  # No size reduction → stop
                    break
                result = new_result
                excess = len(result) - (max_size - 500)

            if isinstance(parsed, dict):
                parsed["_truncated"] = True
                parsed["_original_size"] = len(result)
                parsed["_limit"] = max_size
            result = json.dumps(parsed, ensure_ascii=False)

            # Fall back to a minimal error JSON if structured trimming is still too large.
            if len(result) > max_size:
                result = json.dumps({
                    "_truncated": True,
                    "_original_size": len(result),
                    "_limit": max_size,
                    "error": (
                        f"Tool output exceeded size limit of {max_size:,} chars. "
                        "Use more specific parameters."
                    ),
                }, ensure_ascii=False)
        else:
            # For plain text, cut at the last newline when possible.
            truncated = result[:max_size]
            last_nl = truncated.rfind("\n")
            if last_nl > max_size // 2:
                truncated = truncated[:last_nl + 1]
            result = (
                f"{truncated}\n"
                f"[Result truncated: output was {len(result):,} chars, "
                f"exceeds limit of {max_size:,}. Use more specific parameters.]"
            )

    # Reset consecutive-read tracking after any non-read tool so warnings only
    # apply to truly consecutive repeated reads.
    if tool_name not in ("read_file", "search_files"):
        try:
            from mclaw.tools.read_tracker import notify_other_tool_call
            task_id = getattr(parent_agent, "session_id", "default") if parent_agent else "default"
            notify_other_tool_call(task_id=task_id)
        except Exception:
            logger.debug("Read tracker notification failed after %s", tool_name, exc_info=True)

    _finalize_operation(operation, result, _tool_result_success(result), parent_agent)
    return result


def _run_tool_worker(
    result_slot: dict,
    call: dict,
    tool_names: set,
    checkpoint_manager: Any,
    memory_manager: Any,
    parent_agent: Any,
    cancel_event: threading.Event | None,
    forbidden_tool_names: set[str] | frozenset[str] | None = None,
) -> None:
    """Run one tool into a private slot that the dispatcher snapshots once."""
    token = set_interrupt_event(cancel_event) if cancel_event is not None else None
    try:
        if cancel_event is not None and cancel_event.is_set():
            tool_name = call.get("function", {}).get("name", "?")
            result_slot["result"] = _cancelled_tool_result(tool_name, started=False)
            return
        result_slot["result"] = _dispatch_single(
            call,
            tool_names,
            checkpoint_manager,
            memory_manager,
            parent_agent,
            forbidden_tool_names,
        )
    except Exception as exc:
        from mclaw.tools.registry import tool_error

        termination_fence = getattr(exc, "termination_fence", None)
        if termination_fence is not None:
            _register_turn_worker(parent_agent, termination_fence)
            result_slot["result"] = tool_error(
                str(exc),
                success=False,
                completion_unknown=True,
            )
        else:
            result_slot["result"] = tool_error(str(exc), success=False)
    finally:
        result_slot["finished_at"] = time.monotonic()
        if token is not None:
            reset_interrupt_event(token)
        _unregister_turn_worker(parent_agent, threading.current_thread())


def _register_turn_worker(parent_agent: Any, worker: threading.Thread) -> None:
    """Fence a worker so its agent cannot begin another turn while it is alive."""
    register = getattr(parent_agent, "_register_turn_worker", None)
    if callable(register):
        register(worker)


def _unregister_turn_worker(parent_agent: Any, worker: threading.Thread) -> None:
    unregister = getattr(parent_agent, "_unregister_turn_worker", None)
    if callable(unregister):
        unregister(worker)


def _start_tool_worker(parent_agent: Any, worker: threading.Thread) -> None:
    """Register before start, undoing the fence if thread creation fails."""
    _register_turn_worker(parent_agent, worker)
    try:
        worker.start()
    except BaseException:
        _unregister_turn_worker(parent_agent, worker)
        raise


def _abort_tool_turn(
    parent_agent: Any,
    cancel_event: threading.Event,
    reason: str,
    tool_name: str = "batch",
) -> None:
    request_abort = getattr(parent_agent, "_request_turn_abort", None)
    if callable(request_abort):
        request_abort(reason, cancel_event)
    else:
        cancel_event.set()
    _log_dispatch_stop(
        parent_agent,
        cancel_event,
        tool_name=tool_name,
        trigger="deadline" if reason == "tool_timeout" else "completion_unknown",
    )


def _log_dispatch_stop(
    parent_agent: Any,
    cancel_event: threading.Event,
    *,
    tool_name: str,
    trigger: str,
) -> None:
    """Record one dispatcher stop decision without changing cancellation state."""
    safe_cancel_trace(
        lambda: logger.warning(
            "[CANCEL_TRACE] dispatch_stop cancel_id=%s session=%s tool=%s "
            "trigger=%s event_was_set=%s",
            get_cancel_id(cancel_event),
            getattr(parent_agent, "session_id", "?"),
            tool_name,
            trigger,
            cancel_event.is_set(),
        )
    )


def _wait_for_tool_worker(
    thread: threading.Thread,
    deadline: float,
    cancel_event: threading.Event | None,
) -> str:
    """Wait until completion, the absolute deadline, or turn cancellation."""
    while thread.is_alive():
        if cancel_event is not None and cancel_event.is_set():
            return "cancelled"
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "timeout"
        wait_for = min(_INTERRUPT_POLL_INTERVAL, remaining)
        if cancel_event is None:
            thread.join(wait_for)
        else:
            cancel_event.wait(wait_for)
    return "done"


def _completed_tool_result(result_slot: dict, tool_name: str) -> Any:
    """Snapshot a completed worker result without exposing its mutable slot."""
    if result_slot["result"] is not None:
        return result_slot["result"]
    from mclaw.tools.registry import tool_error

    return tool_error(f"Tool '{tool_name}' returned no result", success=False)


def _wait_for_tool_cleanup(thread: threading.Thread, deadline: float) -> None:
    """Give a cancelled worker a bounded chance to report its real result."""
    while thread.is_alive():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        thread.join(min(_INTERRUPT_POLL_INTERVAL, remaining))


def _cancelled_tool_result(tool_name: str, *, started: bool) -> str:
    from mclaw.tools.registry import tool_error

    if started:
        return tool_error(
            f"Cancellation requested for tool '{tool_name}'; completion is unknown",
            success=False,
            interrupted=True,
            status="cancel_requested",
            completion_unknown=True,
        )
    return tool_error(
        f"Tool '{tool_name}' was not started because the turn was cancelled",
        success=False,
        interrupted=True,
        status="cancelled",
    )


def _timed_out_tool_result(
    tool_name: str,
    timeout: int | float,
    *,
    completion_unknown: bool = True,
) -> str:
    from mclaw.tools.registry import tool_error

    return tool_error(
        (
            f"Tool '{tool_name}' timed out after {timeout} seconds; completion is unknown"
            if completion_unknown
            else f"Tool '{tool_name}' exceeded its {timeout}-second deadline and has stopped"
        ),
        success=False,
        status="timeout",
        completion_unknown=completion_unknown,
    )


def _skipped_after_unknown_result(tool_name: str) -> str:
    from mclaw.tools.registry import tool_error

    return tool_error(
        f"Tool '{tool_name}' was skipped because the previous tool may still be running",
        success=False,
        status="skipped",
        reason="previous_completion_unknown",
    )


def handle_function_calls(
    calls: list,
    tool_names: set,
    memory_manager: Any = None,
    checkpoint_manager: Any = None,
    parent_agent: Any = None,
    cancel_event: threading.Event | None = None,
    call_source: str = "turn",
) -> list:
    """Dispatch a batch of tool calls with turn cancellation and time bounds."""
    if not calls:
        return []

    if cancel_event is None:
        cancel_event = get_interrupt_event()
    forbidden_tool_names = (
        _DSOFTBUS_INBOUND_FORBIDDEN_TOOLS
        if call_source == "dsoftbus"
        else None
    )
    use_concurrent = _should_parallelize_tool_batch(calls)

    if use_concurrent:
        tool_call_names = [c.get("function", {}).get("name", "?") for c in calls]
        logger.info("[TOOL CONCURRENT START] tools=%s", tool_call_names)
        results: list = [None] * len(calls)
        workers: dict[int, tuple[threading.Thread, dict, float, int | float]] = {}
        timed_out_indices: set[int] = set()

        for idx, call in enumerate(calls):
            if cancel_event is not None and cancel_event.is_set():
                break
            tool_name = tool_call_names[idx]
            tool_timeout = _concurrent_tool_timeout(tool_name, parent_agent)
            result_slot = {"result": None, "finished_at": None}
            ctx = copy_context()
            thread = threading.Thread(
                target=ctx.run,
                args=(
                    _run_tool_worker,
                    result_slot,
                    call,
                    tool_names,
                    None,
                    memory_manager,
                    parent_agent,
                    cancel_event,
                    forbidden_tool_names,
                ),
                daemon=True,
                name=f"mclaw-tool-{tool_name}",
            )
            if cancel_event is not None and cancel_event.is_set():
                break
            deadline = time.monotonic() + tool_timeout
            _start_tool_worker(parent_agent, thread)
            workers[idx] = (thread, result_slot, deadline, tool_timeout)

        cancelled = cancel_event is not None and cancel_event.is_set()
        if not cancelled:
            deadline_order = sorted(workers.items(), key=lambda item: item[1][2])
            for idx, (thread, result_slot, deadline, tool_timeout) in deadline_order:
                outcome = _wait_for_tool_worker(thread, deadline, cancel_event)
                tool_name = tool_call_names[idx]
                if outcome == "cancelled":
                    if cancel_event is not None:
                        _log_dispatch_stop(
                            parent_agent,
                            cancel_event,
                            tool_name=tool_name,
                            trigger="user_interrupt",
                        )
                    cancelled = True
                    break
                if outcome == "timeout":
                    safe_cancel_trace(
                        lambda: logger.warning(
                            "[TOOL TIMEOUT] %s (idx=%d)", tool_name, idx
                        )
                    )
                    timed_out_indices.add(idx)
                    results[idx] = _timed_out_tool_result(tool_name, tool_timeout)
                    if cancel_event is not None:
                        _abort_tool_turn(
                            parent_agent,
                            cancel_event,
                            "tool_timeout",
                            tool_name,
                        )
                        cancelled = True
                        break
                else:
                    if result_slot["finished_at"] > deadline:
                        safe_cancel_trace(
                            lambda: logger.warning(
                                "[TOOL TIMEOUT] %s (idx=%d)", tool_name, idx
                            )
                        )
                        timed_out_indices.add(idx)
                        results[idx] = _timed_out_tool_result(tool_name, tool_timeout)
                        if cancel_event is not None:
                            _abort_tool_turn(
                                parent_agent,
                                cancel_event,
                                "tool_timeout",
                                tool_name,
                            )
                            cancelled = True
                            break
                    else:
                        results[idx] = _completed_tool_result(result_slot, tool_name)
                        if (
                            cancel_event is not None
                            and _tool_result_completion_unknown(results[idx])
                        ):
                            _abort_tool_turn(
                                parent_agent,
                                cancel_event,
                                "tool_completion_unknown",
                                tool_name,
                            )
                            cancelled = True
                            break
                        logger.info(
                            "[TOOL DONE] %s (idx=%d) result_len=%d",
                            tool_name,
                            idx,
                            len(results[idx]) if isinstance(results[idx], str) else 0,
                        )

        if timed_out_indices and not cancelled:
            # Without a turn Event (legacy/direct callers), every worker above was
            # still observed through its own deadline. Refine timeout completion
            # state from the real worker lifetime without adding another wait.
            for idx in timed_out_indices:
                thread, _result_slot, _deadline, tool_timeout = workers[idx]
                results[idx] = _timed_out_tool_result(
                    tool_call_names[idx],
                    tool_timeout,
                    completion_unknown=thread.is_alive(),
                )

        if cancelled:
            cleanup_deadline = time.monotonic() + _INTERRUPT_CLEANUP_GRACE
            for thread, _slot, _deadline, _timeout in workers.values():
                _wait_for_tool_cleanup(thread, cleanup_deadline)
            for idx, tool_name in enumerate(tool_call_names):
                worker = workers.get(idx)
                if worker is None:
                    if results[idx] is None:
                        results[idx] = _cancelled_tool_result(tool_name, started=False)
                    continue
                thread, result_slot, deadline, tool_timeout = worker
                if idx in timed_out_indices:
                    results[idx] = _timed_out_tool_result(
                        tool_name,
                        tool_timeout,
                        completion_unknown=thread.is_alive(),
                    )
                    continue
                if results[idx] is not None:
                    continue
                if thread.is_alive():
                    results[idx] = _cancelled_tool_result(tool_name, started=True)
                elif result_slot["finished_at"] > deadline:
                    safe_cancel_trace(
                        lambda: logger.warning(
                            "[TOOL TIMEOUT] %s (idx=%d)", tool_name, idx
                        )
                    )
                    results[idx] = _timed_out_tool_result(
                        tool_name,
                        tool_timeout,
                        completion_unknown=False,
                    )
                else:
                    results[idx] = _completed_tool_result(result_slot, tool_name)
            if cancel_event is not None and any(
                _tool_result_completion_unknown(result) for result in results
            ):
                _abort_tool_turn(
                    parent_agent,
                    cancel_event,
                    "tool_completion_unknown",
                    "batch",
                )

        logger.info("[TOOL CONCURRENT END]")
        return results

    results = []
    for idx, call in enumerate(calls):
        func = call.get("function", {})
        tool_name = func.get("name", "?")
        if cancel_event is not None and cancel_event.is_set():
            results.extend(
                _cancelled_tool_result(
                    pending.get("function", {}).get("name", "?"),
                    started=False,
                )
                for pending in calls[idx:]
            )
            break

        logger.debug("Serial tool %d/%d: %s", idx + 1, len(calls), tool_name)
        result_slot = {"result": None, "finished_at": None}
        ctx = copy_context()
        thread = threading.Thread(
            target=ctx.run,
            args=(
                _run_tool_worker,
                result_slot,
                call,
                tool_names,
                checkpoint_manager,
                memory_manager,
                parent_agent,
                cancel_event,
                forbidden_tool_names,
            ),
            daemon=True,
            name=f"mclaw-tool-{tool_name}",
        )
        tool_timeout = _serial_tool_timeout(tool_name, func, parent_agent)
        if cancel_event is not None and cancel_event.is_set():
            results.extend(
                _cancelled_tool_result(
                    pending.get("function", {}).get("name", "?"),
                    started=False,
                )
                for pending in calls[idx:]
            )
            break
        deadline = time.monotonic() + tool_timeout
        _start_tool_worker(parent_agent, thread)
        outcome = _wait_for_tool_worker(thread, deadline, cancel_event)
        if outcome == "cancelled" and cancel_event is not None:
            _log_dispatch_stop(
                parent_agent,
                cancel_event,
                tool_name=tool_name,
                trigger="user_interrupt",
            )

        if outcome == "done" and result_slot["finished_at"] <= deadline:
            completed_result = _completed_tool_result(result_slot, tool_name)
            results.append(completed_result)
            if _tool_result_completion_unknown(completed_result):
                if cancel_event is not None:
                    _abort_tool_turn(
                        parent_agent,
                        cancel_event,
                        "tool_completion_unknown",
                        tool_name,
                    )
                results.extend(
                    _skipped_after_unknown_result(
                        pending.get("function", {}).get("name", "?"),
                    )
                    for pending in calls[idx + 1:]
                )
                break
            continue
        if outcome in {"done", "timeout"}:
            safe_cancel_trace(
                lambda: logger.warning(
                    "Serial tool %s timed out after %ss", tool_name, tool_timeout
                )
            )
            if cancel_event is not None:
                _abort_tool_turn(
                    parent_agent,
                    cancel_event,
                    "tool_timeout",
                    tool_name,
                )
            _wait_for_tool_cleanup(thread, time.monotonic() + _INTERRUPT_CLEANUP_GRACE)
            completion_unknown = thread.is_alive()
            if cancel_event is not None and completion_unknown:
                _abort_tool_turn(
                    parent_agent,
                    cancel_event,
                    "tool_completion_unknown",
                    tool_name,
                )
            results.append(
                _timed_out_tool_result(
                    tool_name,
                    tool_timeout,
                    completion_unknown=completion_unknown,
                )
            )
            if cancel_event is not None:
                results.extend(
                    _cancelled_tool_result(
                        pending.get("function", {}).get("name", "?"),
                        started=False,
                    )
                    for pending in calls[idx + 1:]
                )
            else:
                results.extend(
                    _skipped_after_unknown_result(
                        pending.get("function", {}).get("name", "?"),
                    )
                    for pending in calls[idx + 1:]
                )
            break

        _wait_for_tool_cleanup(thread, time.monotonic() + _INTERRUPT_CLEANUP_GRACE)
        if thread.is_alive():
            results.append(_cancelled_tool_result(tool_name, started=True))
        elif result_slot["finished_at"] > deadline:
            results.append(
                _timed_out_tool_result(
                    tool_name,
                    tool_timeout,
                    completion_unknown=False,
                )
            )
        else:
            results.append(_completed_tool_result(result_slot, tool_name))
        if cancel_event is not None and _tool_result_completion_unknown(results[-1]):
            _abort_tool_turn(
                parent_agent,
                cancel_event,
                "tool_completion_unknown",
                tool_name,
            )
        results.extend(
            _cancelled_tool_result(
                pending.get("function", {}).get("name", "?"),
                started=False,
            )
            for pending in calls[idx + 1:]
        )
        break
    return results
