"""Tool discovery, schema collection, and dispatch for M-Claw.
"""

import asyncio
import json
import logging
import os
import re
import threading
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextvars import ContextVar, copy_context
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from mclaw.tools.registry import registry
from mclaw.tools.toolsets import resolve_toolset, resolve_multiple_toolsets, validate_toolset

logger = logging.getLogger(__name__)

# ── 单次调用上下文：SessionDB 和当前 session_id ────────────────────────
# Set by core.py before calling handle_function_calls; retrieved by tools.
_current_session_db: ContextVar[Any] = ContextVar("current_session_db", default=None)
_current_session_id: ContextVar[str] = ContextVar("current_session_id", default="")
_tool_whitelist: ContextVar[Optional[Set[str]]] = ContextVar("tool_whitelist", default=None)
_tool_action_whitelist: ContextVar[Optional[Dict[str, Set[str]]]] = ContextVar("tool_action_whitelist", default=None)


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
    tool_whitelist: Optional[Set[str]] = None,
    action_whitelist: Optional[Dict[str, Set[str]]] = None,
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


def _policy_error(tool_name: str, arguments: Dict[str, Any]) -> Optional[str]:
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

# 绝不并行执行的工具，需要保持顺序语义。
_NEVER_PARALLEL_TOOLS = frozenset({"clarify"})

# 允许并发执行的只读工具。
_PARALLEL_SAFE_TOOLS = frozenset({
    "read_file", "search_files",
    "skill_view", "skill_tree", "skills_list", "skill_search", "vision_analyze",
    "web_search", "batch_read",
    "read", "grep", "glob",
})

# 读写路径的工具需要进行路径重叠检测。
_PATH_SCOPED_TOOLS = frozenset({"read_file", "write_file", "patch", "edit_file", "delete_file", "skill_manage"})

# 执行前会触发 checkpoint 快照的写工具。
_WRITE_SNAPSHOT_TOOLS = frozenset({"write_file", "patch", "edit_file", "delete_file", "skill_manage"})

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
_QUOTED_WINDOWS_ABS_PATH_RE = re.compile(r'["\']([A-Za-z]:[\\/][^"\']+)["\']')
_WINDOWS_ABS_PATH_RE = re.compile(r'[A-Za-z]:[\\/][^\s"\'<>|`]+(?:[\\/][^\s"\'<>|`]+)*')
_QUOTED_MSYS_ABS_PATH_RE = re.compile(r'["\'](/[A-Za-z]/[^"\']+)["\']')
_MSYS_ABS_PATH_RE = re.compile(r'(?<!\S)/[A-Za-z]/[^\s"\'<>|`]+')
_MOJIBAKE_MARKERS = ("娴嬭瘯", "娴嬭", "瘯")
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
    "vision_analyze": 180,
    "skill_search": 60,
}

_discovery_done = False
_discovery_lock = threading.Lock()

# 异步工具处理器的桥接函数。
_worker_loop = None
_worker_loop_lock = threading.Lock()


def _get_worker_loop():
    global _worker_loop
    with _worker_loop_lock:
        if _worker_loop is None or _worker_loop.is_closed():
            if _worker_loop is not None:
                try:
                    _worker_loop.close()
                except Exception:
                    pass
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


def _discover_tools():
    """Import all tool modules to trigger their registry.register() calls."""
    global _discovery_done
    with _discovery_lock:
        if _discovery_done:
            return
        _discovery_done = True

    # 导入工具模块；每个模块会在模块级调用 registry.register()。
    _tool_modules = [
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
        "mclaw.tools.browser_tool",
        "mclaw.tools.weixin_tool",
        "mclaw.tools.dingtalk_tool",
    ]

    for module_name in _tool_modules:
        try:
            __import__(module_name)
        except Exception as e:
            logger.debug("Failed to import tool module %s: %s", module_name, e)


def get_tool_definitions(
    enabled_toolsets: List[str] = None,
    disabled_toolsets: List[str] = None,
    config: dict | None = None,
) -> Tuple[List[dict], Set[str]]:
    """Return (tool_definitions, valid_tool_names) for the enabled toolsets."""
    _discover_tools()

    if enabled_toolsets:
        tool_names = resolve_multiple_toolsets(enabled_toolsets)
    else:
        tool_names = resolve_multiple_toolsets(["mclaw-required"])

    if disabled_toolsets:
        disabled_tools = resolve_multiple_toolsets(disabled_toolsets)
        tool_names -= disabled_tools

    try:
        from mclaw.runtime.manager import RuntimeManager

        runtime = RuntimeManager.current(config)
        tool_names = runtime.features.filter_tool_names(tool_names)
    except Exception:
        logger.debug("Runtime feature filtering failed", exc_info=True)

    definitions = registry.get_definitions(tool_names, quiet=True, config=config)
    valid_names = {d["function"]["name"] for d in definitions}
    return definitions, valid_names


def handle_function_call(
    tool_name: str,
    tool_args: Dict[str, Any],
    task_id: str = "",
    session_id: str = "",
    enabled_tools: Set[str] = None,
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


def get_toolset_for_tool(name: str) -> Optional[str]:
    return registry.get_toolset_for_tool(name)


def get_all_tool_names() -> List[str]:
    _discover_tools()
    return registry.get_all_tool_names()


def check_toolset_requirements() -> Dict[str, bool]:
    _discover_tools()
    return registry.check_toolset_requirements()


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


def _extract_path_from_args(tool_name: str, arguments: dict) -> Optional[str]:
    """Extract file path from tool arguments for path-overlap detection."""
    if tool_name in _PATH_SCOPED_TOOLS:
        if tool_name == "skill_manage":
            # Skill 2.0 only permits M-Claw home skills/<skill-name>.
            from mclaw.constants import get_skills_dir
            root = str(get_skills_dir().resolve())
            name = arguments.get("name", "")
            parts = [root]
            if name:
                parts.append(name)
            return str(Path(*parts))
        return arguments.get("file_path") or arguments.get("path")
    return None


def _is_destructive_terminal_command(command: str) -> bool:
    if not command:
        return False
    return bool(_DESTRUCTIVE_TERMINAL_PATTERNS.search(command) or _REDIRECT_OVERWRITE.search(command))


def _terminal_workdir(arguments: dict, parent_agent: Any = None) -> str:
    workdir = str(arguments.get("workdir") or "").strip()
    if workdir:
        return workdir
    cfg = getattr(parent_agent, "config", {}) if parent_agent is not None else {}
    if isinstance(cfg, dict):
        terminal_cfg = cfg.get("terminal", {})
        if isinstance(terminal_cfg, dict):
            configured = str(terminal_cfg.get("cwd") or "").strip()
            if configured and configured != ".":
                return configured
        launch_cwd = str(cfg.get("_launch_cwd") or "").strip()
        if launch_cwd and not _is_mclaw_runtime_path(launch_cwd):
            return launch_cwd
    try:
        from mclaw.tools import terminal_tool

        current_id = getattr(terminal_tool, "_current_session_id", None)
        env = getattr(terminal_tool, "_env_registry", {}).get(current_id) if current_id else None
        cwd = getattr(env, "cwd", None)
        if cwd and not _is_mclaw_runtime_path(cwd):
            return str(cwd)
    except Exception:
        pass
    launch_cwd = os.environ.get("TERMINAL_CWD") or os.getcwd()
    if launch_cwd and not _is_mclaw_runtime_path(launch_cwd):
        return str(launch_cwd)
    try:
        from mclaw.tools import terminal_tool

        current_id = getattr(terminal_tool, "_current_session_id", None)
        env = getattr(terminal_tool, "_env_registry", {}).get(current_id) if current_id else None
        cwd = getattr(env, "cwd", None)
        if cwd:
            return str(cwd)
    except Exception:
        pass
    return os.getcwd()


def _is_mclaw_runtime_path(path_value: Any) -> bool:
    try:
        from mclaw.runtime.manager import RuntimeManager

        return RuntimeManager.current().paths.is_runtime_internal_path(path_value)
    except Exception:
        return False


def _extract_absolute_paths_from_command(command: str) -> list[str]:
    if not command:
        return []
    command = _repair_common_mojibake(command)
    paths = []
    seen = set()
    quoted_spans = []
    for match in _QUOTED_WINDOWS_ABS_PATH_RE.finditer(command):
        value = match.group(1).strip().rstrip(";,")
        if value and value not in seen:
            paths.append(value)
            seen.add(value)
        quoted_spans.append(match.span())
    for match in _QUOTED_MSYS_ABS_PATH_RE.finditer(command):
        value = _msys_to_windows_path(match.group(1).strip().rstrip(";,"))
        if value and value not in seen:
            paths.append(value)
            seen.add(value)
        quoted_spans.append(match.span())

    def inside_quoted_span(start: int, end: int) -> bool:
        return any(start >= q_start and end <= q_end for q_start, q_end in quoted_spans)

    for match in _WINDOWS_ABS_PATH_RE.finditer(command):
        if inside_quoted_span(match.start(), match.end()):
            continue
        value = match.group(0).strip().rstrip(";,")
        if value and value not in seen:
            paths.append(value)
            seen.add(value)
    for match in _MSYS_ABS_PATH_RE.finditer(command):
        if inside_quoted_span(match.start(), match.end()):
            continue
        value = _msys_to_windows_path(match.group(0).strip().rstrip(";,"))
        if value and value not in seen:
            paths.append(value)
            seen.add(value)
    return paths


def _msys_to_windows_path(path: str) -> str:
    if len(path) >= 3 and path[0] == "/" and path[2] == "/":
        drive = path[1].upper()
        rest = path[3:]
        return f"{drive}:/{rest}"
    return path


def _repair_common_mojibake(text: str) -> str:
    if not text or not any(marker in text for marker in _MOJIBAKE_MARKERS):
        return text
    try:
        return text.encode("gbk").decode("utf-8")
    except UnicodeError:
        return text


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
    command = _repair_common_mojibake(str(arguments.get("command") or ""))
    return f"before terminal: {command[:60]}"


def _mutation_action(tool_name: str, arguments: dict) -> str:
    if tool_name in {"write_file", "patch", "edit_file", "delete_file"}:
        return tool_name
    if tool_name == "skill_manage":
        return str(arguments.get("action") or "skill_manage")
    if tool_name != "terminal":
        return tool_name
    command = _repair_common_mojibake(str(arguments.get("command") or "")).lower()
    if re.search(r'\b(rm|del|erase|remove-item|ri|rmdir|rd)\b', command):
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


def _file_safety_enabled(parent_agent: Any = None) -> bool:
    cfg = getattr(parent_agent, "config", {}) if parent_agent is not None else {}
    if not isinstance(cfg, dict):
        return True
    safety_cfg = cfg.get("file_safety", {})
    if isinstance(safety_cfg, bool):
        return safety_cfg
    if isinstance(safety_cfg, dict):
        return bool(safety_cfg.get("enabled", True)) and bool(safety_cfg.get("journal_enabled", True))
    return True


def _build_file_safety_plan(tool_name: str, arguments: dict, checkpoint_manager: Optional[Any], parent_agent: Any = None):
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
    checkpoint_manager: Optional[Any],
    parent_agent: Any = None,
) -> Optional[str]:
    plan = _build_file_safety_plan(tool_name, arguments, checkpoint_manager, parent_agent)
    if not plan or not plan.mutates:
        return None
    decision = plan.decision
    if decision.action != "block":
        return None
    from mclaw.tools.registry import tool_error

    return tool_error(f"File safety blocked {tool_name}: {decision.reason}", success=False)


def _maybe_checkpoint_before_tool(
    tool_name: str,
    arguments: dict,
    checkpoint_manager: Optional[Any],
    parent_agent: Any = None,
) -> Optional[dict]:
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
        if _file_safety_enabled(parent_agent):
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
                operation_id=(operation or {}).get("operation_id") or (operation or {}).get("op_id", ""),
            ),
            target_paths=target_paths or None,
        )
        attempt = getattr(checkpoint_manager, "last_attempt", {}) or {}
        if operation is not None and journal is not None:
            try:
                journal.update_checkpoint(
                    operation,
                    checkpoint_commit=attempt.get("commit") or attempt.get("hash"),
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
    except Exception:
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


def _finalize_operation(operation: Optional[dict], result: str, success: bool, parent_agent: Any = None) -> None:
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
                op_map[tool_call_id] = operation.get("operation_id") or operation.get("op_id")
    except Exception:
        logger.debug("Operation journal finalize failed", exc_info=True)


def _should_parallelize_tool_batch(calls: list) -> bool:
    """Determine if a batch of tool calls should run concurrently.

    Mirrors _should_parallelize_tool_batch() logic:
      1. Single call or 'clarify' present → never parallel
      2. JSON parse failure / non-dict args → never parallel
      3. Path overlap on _PATH_SCOPED_TOOLS → never parallel
      4. Any tool not in _PARALLEL_SAFE_TOOLS → never parallel
      5. Otherwise → parallel
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
                try:
                    web_cfg = auxiliary.get("web_search", {})
                    if not isinstance(web_cfg, dict):
                        web_cfg = {}
                    fast_timeout = int(web_cfg.get("timeout", timeout))
                    backend = str(web_cfg.get("backend") or "auto").lower()
                    fallback = bool(web_cfg.get("fallback", True))
                    if backend == "dashscope" or fallback:
                        slow_timeout = int(
                            web_cfg.get(
                                "dashscope_deep_timeout",
                                web_cfg.get("dashscope_timeout", 90),
                            )
                        )
                        timeout = max(fast_timeout, slow_timeout)
                    else:
                        timeout = fast_timeout
                except Exception:
                    pass
            elif tool_name == "vision_analyze":
                try:
                    vision_cfg = auxiliary.get("vision", {})
                    timeout = int(vision_cfg.get("timeout", timeout)) + int(vision_cfg.get("download_timeout", 0))
                except Exception:
                    pass
    return max(1, timeout)


def _invoke_tool_builtin(
    tool_name: str,
    arguments: dict,
    memory_manager: Any = None,
    parent_agent: Any = None,
) -> Optional[str]:
    """Layer 1: direct builtin handlers. Returns JSON string result or None to fall through.

    Builtin tools (todo, memory, session_search, delegate_task) are handled here
    before falling through to the registry. Currently a stub — m-claw does not
    have these builtins yet, but the hook is here for future expansion.
    Memory tool IS routed here when memory_manager is provided.
    """
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
        # 优先截断最长的直接字符串字段。
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

        # 没有直接长字符串时，递归处理嵌套结构。
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


def _extract_json_object(s: str) -> Optional[dict]:
    """从可能包含垃圾后缀的字符串中提取第一个 JSON 对象。

    LLM 有时会在 JSON 闭括号后追加换行/control 字符，导致 json.loads
    抛出 JSONDecodeError。此函数用 json.JSONDecoder.raw_decode 只解析
    第一个合法 JSON 对象，忽略后续垃圾。
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
    checkpoint_manager: Optional[Any],
    memory_manager: Optional[Any] = None,
    parent_agent: Any = None,
) -> str:
    """Single tool dispatch with availability check + optional checkpoint."""
    func = call.get("function", {})
    tool_name = func.get("name", "")
    raw_args = func.get("arguments", "{}")
    arguments: Dict[str, Any] = {}
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

    # 第 0 层：可用性门禁，阻止幻觉工具名。
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

    # 第 1 层：内置工具；提供 manager 时包含 memory 路由。
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

    # ── Result size guard ──
    # 工具返回异常大 payload 时，在进入对话上下文前截断。
    # 这是工具内部限制之后的最后一道防线，防止异常工具撑爆上下文。
    max_size = registry.get_max_result_size(tool_name)
    if max_size is not None and isinstance(result, str) and len(result) > max_size:
        # Try structured truncation for JSON so we don't break parseability.
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

            # 如果仍然过大，则退回最小错误 JSON。
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
            # 纯文本按最后一个换行截断，保持输出整洁。
            truncated = result[:max_size]
            last_nl = truncated.rfind("\n")
            if last_nl > max_size // 2:
                truncated = truncated[:last_nl + 1]
            result = (
                f"{truncated}\n"
                f"[Result truncated: output was {len(result):,} chars, "
                f"exceeds limit of {max_size:,}. Use more specific parameters.]"
            )

    # 执行非读取工具时重置连续读取计数。
    # we only warn/block on *truly consecutive* reads.
    if tool_name not in ("read_file", "search_files"):
        try:
            from mclaw.tools.read_tracker import notify_other_tool_call
            task_id = getattr(parent_agent, "session_id", "default") if parent_agent else "default"
            notify_other_tool_call(task_id=task_id)
        except Exception:
            pass

    _finalize_operation(operation, result, _tool_result_success(result), parent_agent)
    return result


def handle_function_calls(
    calls: list,
    tool_names: set,
    available_toolsets: set = None,
    memory_manager: Any = None,
    checkpoint_manager: Any = None,
    parent_agent: Any = None,
) -> list:
    """Dispatch a batch of tool calls, automatically choosing serial or concurrent.

    Concurrent path is chosen when all calls are read-only and pass the
    _should_parallelize_tool_batch() checks (no path overlap, all in whitelist,
    no 'clarify' tool). Serial path uses CheckpointManager (passed in) for write tools.

    checkpoint_manager should be created once per agent turn and passed in to
    ensure the same file is snapshotted at most once per turn.
    """
    if not calls:
        return []

    use_concurrent = _should_parallelize_tool_batch(calls)

    if use_concurrent:
        # 并发路径：只读工具不做 checkpoint。
        # memory 工具不会进入 _PARALLEL_SAFE_TOOLS，因此正常不会走到这里。
        _tool_names = [c.get("function", {}).get("name", "?") for c in calls]
        logger.info("[TOOL CONCURRENT START] tools=%s", _tool_names)
        results: list = [None] * len(calls)
        # 使用 daemon 线程而不是 ThreadPoolExecutor，避免 shutdown(wait=True)
        # 等待卡住的 worker 导致整个子代理死锁。
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

    # 串行路径：写工具通过调用方 manager 做 checkpoint。
    results = []
    for i, call in enumerate(calls):
        func = call.get("function", {})
        tname = func.get("name", "?")
        logger.debug("Serial tool %d/%d: %s", i + 1, len(calls), tname)
        # 使用带超时的线程包装工具调用，避免单个卡死工具长期阻塞 agent 循环。
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
        tool_timeout = 120
        if tname == "terminal":
            try:
                from mclaw.tools.terminal_tool import DEFAULT_TIMEOUT
                tool_timeout = DEFAULT_TIMEOUT
                cfg = getattr(parent_agent, "config", {}) if parent_agent is not None else {}
                if isinstance(cfg, dict):
                    tool_timeout = int(cfg.get("terminal", {}).get("timeout", tool_timeout))
                try:
                    args = json.loads(func.get("arguments", "{}") or "{}")
                    if args.get("timeout") is not None:
                        tool_timeout = int(args["timeout"])
                except Exception:
                    pass
            except Exception:
                tool_timeout = 120
            tool_timeout = max(1, tool_timeout) + 5
        t.join(timeout=tool_timeout)
        if tool_result[0] is None:
            from mclaw.tools.registry import tool_error
            logger.warning("Serial tool %s timed out after %ss", tname, tool_timeout)
            results.append(tool_error(f"Tool '{tname}' timed out after {tool_timeout} seconds", success=False))
        else:
            results.append(tool_result[0])
    return results
