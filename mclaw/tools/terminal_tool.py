# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Register the terminal tool with scoped secrets and small safety guards.

The terminal tool preserves per-session cwd state, supports background
processes through the process registry, scopes authorized secrets into command
environments, and blocks credential-file access and shell mutations against
managed Skill storage.
"""

import json
import logging
import os
import platform as _platform_mod
import re
from dataclasses import dataclass, field
from pathlib import Path

from mclaw.constants import get_skills_dir
from mclaw.runtime.manager import RuntimeManager
from mclaw.runtime.secrets import SecretRequestError, build_scoped_env, redact_secret_values
from mclaw.safety.mutation_detector import is_destructive_terminal_command
from mclaw.safety.path_resolver import extract_mutation_targets_from_command
from mclaw.skills_hub.paths import get_skill_drafting_dir
from mclaw.tools.registry import registry

logger = logging.getLogger(__name__)

_IS_WINDOWS = _platform_mod.system() == "Windows"

DEFAULT_TIMEOUT = 180
DEFAULT_MAX_TIMEOUT = 600
MAX_RESULT_SIZE_CHARS = 50000


@dataclass
class RuntimeTerminalSession:
    """Per-session terminal state preserved across foreground commands."""
    cwd: str = ""
    timeout: int = DEFAULT_TIMEOUT
    env: dict[str, str] = field(default_factory=dict)
    scoped_secret_keys: set[str] = field(default_factory=set)


_env_registry: dict[str, RuntimeTerminalSession] = {}
_current_session_id: str | None = None
_CREDENTIAL_FILE_REFERENCE_RE = re.compile(
    r"""(?:
        (?<![\w])\.env[^\s/\\\"';&|]*
        |(?:^|[/\\])\.ssh[/\\](?:id_rsa|id_dsa|id_ecdsa|id_ed25519)(?=$|[\s\"';&|])
        |(?:^|[/\\])\.aws[/\\]credentials(?=$|[\s\"';&|])
        |(?:^|[/\\])\.kube[/\\]config(?=$|[\s\"';&|])
        |(?:^|[/\\])\.docker[/\\]config\.json(?=$|[\s\"';&|])
        |(?<![\w.-])(?:\.git-credentials|\.netrc|\.npmrc|\.pypirc)(?![\w.-])
        |[/\\]proc[/\\](?:[^/\\\s\"']+[/\\](?:environ|mem)|kcore)\b
    )""",
    re.IGNORECASE | re.VERBOSE,
)


def _resolve_guard_path(path_value: str, cwd: str = "") -> Path | None:
    """Resolve a command path candidate for safety checks without executing it."""
    value = str(path_value or "").strip().strip("\"'")
    if not value:
        return None
    home = str(Path.home())
    value = re.sub(r"(?i)\$env:USERPROFILE", lambda _match: home, value)
    value = value.replace("${HOME}", home).replace("$HOME", home)
    value = os.path.expandvars(value)
    path = Path(value).expanduser()
    if not path.is_absolute() and cwd:
        path = Path(cwd).expanduser() / path
    try:
        return path.resolve()
    except OSError:
        return path.absolute()


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


def _protected_skill_roots() -> list[Path]:
    """Return Skill storage roots that terminal mutations must not touch."""
    roots: list[Path] = []
    for root in (get_skills_dir(), get_skill_drafting_dir()):
        resolved = _resolve_guard_path(str(root))
        if resolved is not None:
            roots.append(resolved)
    return roots


def _skill_store_terminal_error_message(root: Path) -> str:
    return (
        f"Skill package storage is managed by skill_manage. "
        f"Do not use terminal to mutate files under {root}. "
        "Use skill_manage(action=\"create_scaffold\") for new Skills, "
        "then skill_manage(action=\"write_file\"|\"remove_file\"|\"patch\"|\"edit\"|\"delete\") "
        "for Skill contents."
    )


def _skill_store_mutation_targets(command: str, workdir: str = "") -> list[str]:
    """Return shell mutation targets, not read-only Skill asset inputs."""
    return extract_mutation_targets_from_command(command, workdir)


def _skill_store_terminal_mutation_error(command: str, workdir: str = "") -> str | None:
    """Block terminal mutations that target Skill package storage."""
    if not is_destructive_terminal_command(command):
        return None

    protected_roots = _protected_skill_roots()
    effective_cwd = workdir or ""
    if not effective_cwd and _current_session_id:
        session_env = _env_registry.get(_current_session_id)
        effective_cwd = getattr(session_env, "cwd", "") if session_env is not None else ""

    for candidate in _skill_store_mutation_targets(command, effective_cwd):
        candidate_path = _resolve_guard_path(candidate, effective_cwd)
        if candidate_path is None:
            continue
        for root in protected_roots:
            if _is_path_within(candidate_path, root):
                return _skill_store_terminal_error_message(root)
    return None


def _credential_file_terminal_error(command: str) -> str | None:
    """Block direct credential-file access; scoped secret injection is supported."""
    if not _CREDENTIAL_FILE_REFERENCE_RE.search(command or ""):
        return None
    return "Credential file access is blocked; use secret_request_many and terminal(required_for=...)."


def set_current_session(session_id: str | None) -> None:
    """Set the session ID for terminal tool environment tracking.

    Switching session automatically cleans up the previous session's environment
    unless it still has active background processes.
    """
    global _current_session_id
    if _current_session_id is not None and _current_session_id in _env_registry:
        # Active background processes retain their session environment.
        try:
            from mclaw.tools.process_registry import process_registry
            if process_registry.has_active_processes(_current_session_id):
                _current_session_id = session_id
                return
        except ImportError:
            logger.debug("Process registry unavailable during terminal session switch", exc_info=True)
        cleanup_session(_current_session_id)
    _current_session_id = session_id


def _get_or_create_env(
    cwd: str,
    timeout: int,
    env_vars: dict | None = None,
    scoped_secret_keys: set[str] | None = None,
    session_key: str | None = None,
) -> RuntimeTerminalSession:
    """Create or update the active session environment snapshot."""
    active_session = session_key if session_key is not None else _current_session_id

    if not active_session:
        return RuntimeTerminalSession(cwd=cwd or os.getcwd(), timeout=timeout, env=dict(env_vars or {}), scoped_secret_keys=set(scoped_secret_keys or set()))

    if active_session not in _env_registry:
        env = RuntimeTerminalSession(cwd=cwd or os.getcwd(), timeout=timeout, env=dict(env_vars or {}), scoped_secret_keys=set(scoped_secret_keys or set()))
        _env_registry[active_session] = env
    else:
        env = _env_registry[active_session]
        if cwd:
            env.cwd = cwd
        env.timeout = timeout
        env.env = dict(env_vars or {})
        env.scoped_secret_keys = set(scoped_secret_keys or set())

    return env


def cleanup_session(session_id: str | None = None) -> None:
    """Drop terminal cwd/env state for a finished session."""
    global _env_registry, _current_session_id
    sid = session_id or _current_session_id
    if sid and sid in _env_registry:
        del _env_registry[sid]


def _truncate_output(output: str, limit: int = MAX_RESULT_SIZE_CHARS) -> str:
    """Keep command output bounded while preserving both beginning and end."""
    if len(output) <= limit:
        return output
    head_chars = int(limit * 0.4)
    tail_chars = limit - head_chars
    omitted = len(output) - limit
    return (
        output[:head_chars]
        + f"\n\n[... {omitted:,} characters truncated ...]\n\n"
        + output[-tail_chars:]
    )


def _coerce_timeout(value) -> int | None:
    if value is None:
        return None
    try:
        timeout = int(float(value))
    except (TypeError, ValueError):
        return None
    if timeout <= 0:
        return None
    return timeout


def _resolve_timeout(requested, config: dict | None = None) -> int:
    """Clamp requested timeout between configured terminal minimum and hard max."""
    cfg = config or {}
    terminal_cfg = cfg.get("terminal", {}) if isinstance(cfg, dict) else {}
    if not isinstance(terminal_cfg, dict):
        terminal_cfg = {}

    config_min = _coerce_timeout(terminal_cfg.get("timeout")) or DEFAULT_TIMEOUT
    hard_max = _coerce_timeout(terminal_cfg.get("max_timeout")) or DEFAULT_MAX_TIMEOUT
    if hard_max < config_min:
        hard_max = config_min

    requested_timeout = _coerce_timeout(requested)
    effective = requested_timeout if requested_timeout is not None else config_min
    effective = max(effective, config_min)
    effective = min(effective, hard_max)

    if requested is not None and effective != requested_timeout:
        logger.info(
            "[terminal] timeout normalized: requested=%r config_min=%s hard_max=%s effective=%s",
            requested,
            config_min,
            hard_max,
            effective,
        )
    return effective


def terminal_tool(
    command: str,
    timeout: int | str | None = None,
    workdir: str | None = None,
    background: bool = False,
    task_id: str | None = None,
    check_interval: int | None = None,
    pty: bool = False,
    notify_on_complete: bool = False,
    required_for: str | None = None,
    session_key: str | None = None,
) -> str:
    """Execute a shell command; optional background via process registry."""
    effective_timeout = _coerce_timeout(timeout) or DEFAULT_TIMEOUT
    effective_cwd = workdir or ""
    active_session = session_key if session_key is not None else _current_session_id
    session_env = _env_registry.get(active_session) if active_session else None
    try:
        scoped_env, scoped_secret_keys = build_scoped_env(required_for)
    except SecretRequestError as exc:
        return json.dumps(
            {
                "output": "",
                "returncode": 1,
                "error": str(exc),
            },
            ensure_ascii=False,
        )
    guard_cwd = effective_cwd or (session_env.cwd if session_env else "")
    blocked = _credential_file_terminal_error(command) or _skill_store_terminal_mutation_error(command, guard_cwd)
    if blocked:
        return json.dumps(
            {
                "output": "",
                "returncode": 1,
                "error": blocked,
            },
            ensure_ascii=False,
        )

    if background:
        from mclaw.tools.process_registry import process_registry

        effective_task_id = task_id or active_session or ""

        # Without an explicit workdir, inherit the current session cwd/env so
        # background and foreground commands share the same environment snapshot.
        # Explicit workdir takes priority; fall back to session_env.cwd only when not specified.
        bg_cwd = session_env.cwd if session_env else None
        if effective_cwd:
            bg_cwd = effective_cwd
        bg_env_vars = dict(scoped_env)

        # PTY support is optional; pipe mode remains the portable execution path.
        use_pty = False
        pty_disabled_reason = None
        if pty:
            try:
                if _IS_WINDOWS:
                    import winpty as _winpty  # noqa: F401
                else:
                    import ptyprocess as _ptyprocess  # noqa: F401
                use_pty = True
            except ImportError:
                pty_disabled_reason = (
                    "ptyprocess not installed; used pipe mode. "
                    "Install ptyprocess (Unix) or pywinpty (Windows) to enable PTY."
                )

        try:
            proc_session = process_registry.spawn_local(
                command=command,
                cwd=bg_cwd,
                task_id=effective_task_id,
                session_key=active_session or "",
                env_vars=bg_env_vars,
                use_pty=use_pty,
                scoped_secret_keys=scoped_secret_keys,
            )
        except Exception as exc:
            return json.dumps(
                {
                    "output": "",
                    "returncode": 1,
                    "error": f"Background process start failed: {exc}",
                },
                ensure_ascii=False,
            )
        if notify_on_complete:
            try:
                process_registry.update_session_metadata(
                    proc_session.id,
                    notify_on_complete=True,
                )
            except Exception as exc:
                try:
                    process_registry.kill_process(proc_session.id)
                except Exception:
                    logger.debug("Failed to kill background process after metadata persist failure", exc_info=True)
                return json.dumps(
                    {
                        "output": "",
                        "returncode": 1,
                        "error": f"Background process metadata persist failed: {exc}",
                        "session_id": proc_session.id,
                        "pid": proc_session.pid,
                    },
                    ensure_ascii=False,
                )
        result_data: dict = {
            "output": "Background process started",
            "session_id": proc_session.id,
            "pid": proc_session.pid,
            "status": "running",
            # No exit_code is available while the process is still running.
            # Use process(action="poll", session_id=...) to inspect exit state.
        }
        if check_interval:
            # Store watcher info on session for checkpoint persistence; consumed by CLI loop.
            try:
                requested_interval = int(check_interval)
            except (TypeError, ValueError):
                result_data["check_interval_note"] = (
                    f"Invalid check_interval={check_interval!r}; watcher disabled"
                )
            else:
                if requested_interval <= 0:
                    result_data["check_interval_note"] = (
                        f"Invalid check_interval={requested_interval}; watcher disabled"
                    )
                else:
                    effective_interval = max(30, requested_interval)
                    if requested_interval < 30:
                        result_data["check_interval_note"] = (
                            f"Requested {requested_interval}s raised to minimum 30s"
                        )
                    try:
                        watcher = process_registry.register_watcher(
                            session_id=proc_session.id,
                            check_interval=effective_interval,
                            session_key=active_session or "",
                            notify_on_complete=bool(notify_on_complete),
                        )
                    except Exception as exc:
                        try:
                            process_registry.kill_process(proc_session.id)
                        except Exception:
                            logger.debug("Failed to kill background process after watcher persist failure", exc_info=True)
                        return json.dumps(
                            {
                                "output": "",
                                "returncode": 1,
                                "error": f"Background process watcher persist failed: {exc}",
                                "session_id": proc_session.id,
                                "pid": proc_session.pid,
                            },
                            ensure_ascii=False,
                        )
                    if watcher:
                        result_data["check_interval"] = watcher.get(
                            "check_interval", effective_interval
                        )
        if pty_disabled_reason:
            result_data["pty_note"] = pty_disabled_reason
        if notify_on_complete:
            result_data["notify_on_complete"] = True
        return json.dumps(result_data, ensure_ascii=False)

    session = _get_or_create_env(
        cwd=effective_cwd or "",
        timeout=effective_timeout,
        env_vars=scoped_env,
        scoped_secret_keys=scoped_secret_keys,
        session_key=active_session,
    )

    try:
        runtime = RuntimeManager.current()
        result = runtime.exec(
            command,
            cwd=effective_cwd or session.cwd or os.getcwd(),
            timeout=effective_timeout,
            env=session.env,
            scoped_secret_keys=session.scoped_secret_keys,
        )
        session.cwd = result.cwd
    except Exception as exc:
        return json.dumps(
            {
                "output": "",
                "returncode": 1,
                "error": f"Terminal execution failed: {exc}",
            },
            ensure_ascii=False,
        )

    output = result.output
    returncode = result.returncode

    output = _truncate_output(redact_secret_values(output, scoped_env))

    if returncode == 130:
        error = "Command was interrupted by user"
    elif returncode == 124:
        error = f"Command timed out after {effective_timeout}s"
    elif returncode != 0:
        error = f"Command exited with code {returncode}"
    else:
        error = ""

    return json.dumps(
        {
            "output": output,
            "returncode": returncode,
            "error": redact_secret_values(error, scoped_env),
        },
        ensure_ascii=False,
    )


def _handle_terminal(args: dict, **kwargs) -> str:
    """Registry handler that applies parent configuration before execution."""
    parent_agent = kwargs.get("parent_agent")
    effective_workdir = args.get("workdir")
    command = args.get("command") or ""
    cfg = getattr(parent_agent, "config", {}) if parent_agent is not None else {}
    cfg = cfg or {}
    effective_timeout = _resolve_timeout(args.get("timeout"), cfg if isinstance(cfg, dict) else {})
    from mclaw.tools.dispatch import get_current_session_id

    session_key = get_current_session_id() or str(getattr(parent_agent, "session_id", "") or "")
    if not effective_workdir and session_key not in _env_registry:
        effective_workdir = str(getattr(parent_agent, "workspace_path", "") or "").strip() or None

    return terminal_tool(
        command=command,
        timeout=effective_timeout,
        workdir=effective_workdir,
        background=bool(args.get("background", False)),
        task_id=kwargs.get("task_id"),
        check_interval=args.get("check_interval"),
        pty=bool(args.get("pty", False)),
        notify_on_complete=bool(args.get("notify_on_complete", False)),
        required_for=args.get("required_for"),
        session_key=session_key or None,
    )


TERMINAL_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "terminal",
        "description": "Execute a shell command through the active runtime shell profile. "
        "Session preserves cwd across calls. Use background=true for long-running tasks; "
        "then use the process tool with the returned session_id. "
        "This tool cannot mutate M-Claw managed Skill package storage; "
        "use skill_manage for those changes.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to execute.",
                },
                "background": {
                    "type": "boolean",
                    "description": "Run in background; returns session_id for process tool.",
                    "default": False,
                },
                "timeout": {
                    "type": "integer",
                    "description": (
                        f"Timeout in seconds. Effective value is clamped by config: "
                        f"minimum terminal.timeout (default {DEFAULT_TIMEOUT}) and "
                        f"maximum terminal.max_timeout (default {DEFAULT_MAX_TIMEOUT})."
                    ),
                },
                "workdir": {
                    "type": "string",
                    "description": "Working directory for this command.",
                },
                "check_interval": {
                    "type": "integer",
                    "description": "Watcher polling interval in seconds for background tasks (minimum 30).",
                    "minimum": 30,
                },
                "pty": {
                    "type": "boolean",
                    "description": (
                        "Run a background command with a pseudo-terminal when the runtime dependency is available; "
                        "otherwise pipe mode is used."
                    ),
                    "default": False,
                },
                "notify_on_complete": {
                    "type": "boolean",
                    "description": "When true with background, completion is queued for CLI.",
                    "default": False,
                },
                "required_for": {
                    "type": "string",
                    "pattern": "^(skill|tool|runtime|channel):[^\\s:]+$",
                    "description": (
                        "Scoped secret context. Required when this command reads or uses env vars "
                        "authorized by secret_request_many. Must exactly match the earlier required_for "
                        "scope, for example skill:mmx-cli. Without this field, scoped Skill/tool/runtime/"
                        "channel secrets are not injected."
                    ),
                },
            },
            "required": ["command"],
        },
    },
}

registry.register(
    name="terminal",
    toolset="terminal",
    schema=TERMINAL_TOOL_SCHEMA,
    handler=_handle_terminal,
    description="Execute terminal commands",
    emoji="💻",
    max_result_size_chars=MAX_RESULT_SIZE_CHARS,
)
