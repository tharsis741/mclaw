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
import os
import threading
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from pathlib import Path
from typing import Any

from mclaw.safety.mutation_detector import terminal_action
from mclaw.safety.path_resolver import repair_common_mojibake
from mclaw.tools.registry import registry
from mclaw.tools.toolsets import resolve_multiple_toolsets

logger = logging.getLogger(__name__)

# Per-call context set by core.py before handle_function_calls and read by tools.
_current_session_db: ContextVar[Any] = ContextVar("current_session_db", default=None)
_current_session_id: ContextVar[str] = ContextVar("current_session_id", default="")
_tool_whitelist: ContextVar[set[str] | None] = ContextVar("tool_whitelist", default=None)
_tool_action_whitelist: ContextVar[dict[str, set[str]] | None] = ContextVar("tool_action_whitelist", default=None)


def set_tool_context(session_db: Any = None, session_id: str = "") -> None:
    """Set context for the duration of tool execution. Call from core.py."""
    _current_session_db.set(session_db)
    _current_session_id.set(session_id)


def get_session_db() -> Any:
    """Get the current SessionDB instance from context."""
    return _current_session_db.get()


def get_current_session_id() -> str:
    """Get the current session_id from context."""
    return _current_session_id.get()


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
_MAX_TOOL_WORKERS = 8
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


def _run_async(coro):
    """Bridge an async tool handler to sync context."""
    loop = _get_worker_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    try:
        return future.result(timeout=600)
    except RuntimeError:
        loop = _get_worker_loop()
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result(timeout=600)


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
) -> str:
    """Dispatch a tool call by name, returning JSON string result."""
    _discover_tools()
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
        operation = None
        journal = None
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
                )
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
    except Exception:
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
        )
        if parent_agent is not None:
            op_map = getattr(parent_agent, "_tool_operation_ids", None)
            if op_map is None:
                op_map = {}
                setattr(parent_agent, "_tool_operation_ids", op_map)
            tool_call_id = operation.get("tool_call_id")
            if tool_call_id:
                op_map[tool_call_id] = operation.get("operation_id")
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
                if backend == "dashscope" or fallback:
                    slow_timeout = _positive_int(
                        web_cfg.get(
                            "dashscope_deep_timeout",
                            web_cfg.get("dashscope_timeout", 90),
                        ),
                        90,
                    )
                    timeout = max(fast_timeout, slow_timeout)
                else:
                    timeout = fast_timeout
            elif tool_name == "web_extract":
                extract_cfg = auxiliary.get("web_extract", {})
                if isinstance(extract_cfg, dict):
                    request_timeout = _positive_int(extract_cfg.get("timeout"), 30)
                    timeout = request_timeout * 5 + 30
            elif tool_name == "vision_analyze":
                vision_cfg = auxiliary.get("vision", {})
                if isinstance(vision_cfg, dict):
                    timeout = (
                        _positive_int(vision_cfg.get("timeout"), timeout)
                        + _positive_int(vision_cfg.get("download_timeout"), 0)
                    )
    return max(1, timeout)


def _serial_tool_timeout(tool_name: str, func: dict, parent_agent: Any = None) -> int:
    """Return per-call timeout for tools that must run on the serial path."""
    timeout = 120
    if tool_name != "terminal":
        return timeout
    try:
        from mclaw.tools.terminal_tool import DEFAULT_TIMEOUT
    except ImportError:
        return timeout
    timeout = DEFAULT_TIMEOUT
    cfg = getattr(parent_agent, "config", {}) if parent_agent is not None else {}
    if isinstance(cfg, dict):
        terminal_cfg = cfg.get("terminal", {})
        if isinstance(terminal_cfg, dict):
            timeout = _positive_int(terminal_cfg.get("timeout"), timeout)
    try:
        args = json.loads(func.get("arguments", "{}") or "{}")
    except (json.JSONDecodeError, TypeError, ValueError):
        args = {}
    if isinstance(args, dict) and args.get("timeout") is not None:
        timeout = _positive_int(args.get("timeout"), timeout)
    return max(1, timeout) + 5


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

    # Layer 1: direct builtins, including memory when a manager is provided.
    builtin_result = _invoke_tool_builtin(tool_name, arguments, memory_manager, parent_agent)
    if builtin_result is not None:
        _finalize_operation(operation, builtin_result, _tool_result_success(builtin_result), parent_agent)
        return builtin_result

    try:
        result = registry.dispatch(tool_name, arguments, parent_agent=parent_agent)
    except Exception as e:
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


def handle_function_calls(
    calls: list,
    tool_names: set,
    memory_manager: Any = None,
    checkpoint_manager: Any = None,
    parent_agent: Any = None,
) -> list:
    """Dispatch a batch of tool calls, automatically choosing serial or concurrent.

    Concurrent path is chosen when all calls are read-only and pass the
    _should_parallelize_tool_batch() checks (no path overlap, all in whitelist).
    Serial path uses CheckpointManager (passed in) for write tools.

    checkpoint_manager should be created once per agent turn and passed in to
    ensure the same file is snapshotted at most once per turn.
    """
    if not calls:
        return []

    use_concurrent = _should_parallelize_tool_batch(calls)

    if use_concurrent:
        # Concurrent path: read-only tools do not checkpoint. Memory tools are
        # excluded from _PARALLEL_SAFE_TOOLS, so they stay on the serial path.
        _tool_names = [c.get("function", {}).get("name", "?") for c in calls]
        logger.info("[TOOL CONCURRENT START] tools=%s", _tool_names)
        results: list = [None] * len(calls)
        # Daemon threads keep timed-out tool calls from blocking shutdown.
        _tool_threads: list[tuple[threading.Thread, int]] = []
        for i, call in enumerate(calls):
            ctx = copy_context()
            t = threading.Thread(
                target=lambda c, idx, context: context.run(
                    lambda: results.__setitem__(
                        idx,
                        _dispatch_single(c, tool_names, None, memory_manager, parent_agent),
                    )
                ),
                args=(call, i, ctx),
                daemon=True,
            )
            t.start()
            _tool_threads.append((t, i))
        for t, idx in _tool_threads:
            tname = _tool_names[idx]
            tool_timeout = _concurrent_tool_timeout(tname, parent_agent)
            t.join(timeout=tool_timeout)
            if results[idx] is None:
                from mclaw.tools.registry import tool_error
                logger.warning("[TOOL TIMEOUT] %s (idx=%d)", tname, idx)
                results[idx] = tool_error(
                    f"Tool '{tname}' timed out after {tool_timeout} seconds", success=False
                )
            else:
                logger.info(
                    "[TOOL DONE] %s (idx=%d) result_len=%d",
                    tname,
                    idx,
                    len(results[idx]) if isinstance(results[idx], str) else 0,
                )
        logger.info("[TOOL CONCURRENT END]")
        return results

    # Serial path: write tools checkpoint through the caller-provided manager.
    results = []
    for i, call in enumerate(calls):
        func = call.get("function", {})
        tname = func.get("name", "?")
        logger.debug("Serial tool %d/%d: %s", i + 1, len(calls), tname)
        # Wrap each tool call with a timeout so one stuck tool cannot block the
        # agent loop indefinitely.
        tool_result = [None]
        ctx = copy_context()
        def _run():
            try:
                tool_result[0] = _dispatch_single(call, tool_names, checkpoint_manager, memory_manager, parent_agent)
            except Exception as exc:
                from mclaw.tools.registry import tool_error
                tool_result[0] = tool_error(str(exc), success=False)
        t = threading.Thread(target=lambda: ctx.run(_run), daemon=True)
        t.start()
        tool_timeout = _serial_tool_timeout(tname, func, parent_agent)
        t.join(timeout=tool_timeout)
        if tool_result[0] is None:
            from mclaw.tools.registry import tool_error
            logger.warning("Serial tool %s timed out after %ss", tname, tool_timeout)
            results.append(tool_error(f"Tool '{tname}' timed out after {tool_timeout} seconds", success=False))
        else:
            results.append(tool_result[0])
    return results
