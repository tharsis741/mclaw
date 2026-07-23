# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Track background terminal processes across session restarts.

The registry owns spawned process metadata, bounded output capture, optional
PTY handles, watcher state, and checkpoint persistence. It keeps long-running
terminal tasks observable after the foreground tool call has returned.
"""

from __future__ import annotations

import json
import logging
import os
import platform as _platform_mod
import queue
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mclaw.cli.config import ConfigError, load_config
from mclaw.constants import get_mclaw_home
from mclaw.runtime.manager import RuntimeManager
from mclaw.runtime.base import UnresolvedOperationFence
from mclaw.runtime.process import (
    PROCESS_TERMINATION_BUDGET_SECONDS,
    kill_process_group,
    kill_process_tree,
    sanitize_subprocess_env,
    wait_for_process_group_exit,
)
from mclaw.runtime.secrets import redact_secret_values
from mclaw.tools.ansi_strip import strip_ansi
from mclaw.tools.interrupt import (
    get_cancel_id,
    get_interrupt_event,
    is_interrupted,
    safe_cancel_trace,
)
from mclaw.tools.registry import registry, tool_error
from mclaw.utils import atomic_json_write

logger = logging.getLogger(__name__)


class BackgroundSpawnCancellationError(InterruptedError):
    """A just-spawned detached process could not be confirmed terminated."""

    def __init__(self, pid: int, message: str | None = None) -> None:
        super().__init__(message or f"Background process {pid} may still be running after cancellation")
        self.pid = pid
        self.termination_fence = UnresolvedOperationFence()

_IS_WINDOWS = _platform_mod.system() == "Windows"
CHECKPOINT_PATH = get_mclaw_home() / "processes.json"
MAX_OUTPUT_CHARS = 200_000
FINISHED_TTL_SECONDS = 1800
MAX_PROCESSES = 64
DEFAULT_WAIT_TIMEOUT_SECONDS = 180


@dataclass
class ProcessSession:
    """Runtime-owned record for one background terminal process."""

    id: str
    command: str
    task_id: str = ""
    session_key: str = ""
    pid: int | None = None
    process: subprocess.Popen | None = None
    cwd: str | None = None
    started_at: float = 0.0
    finished_at: float = 0.0
    exited: bool = False
    exit_code: int | None = None
    output_buffer: str = ""
    max_output_chars: int = MAX_OUTPUT_CHARS
    detached: bool = False
    pid_scope: str = "host"
    runtime_kind: str = ""
    process_group_id: int | None = None
    log_path: str = ""
    env_hash: str = ""
    shell_profile: str = ""
    notify_on_complete: bool = False
    watcher_interval: int = 0
    secret_redactions: tuple[str, ...] = field(default_factory=tuple, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _reader_thread: threading.Thread | None = field(default=None, repr=False)
    _pty: Any = field(default=None, repr=False)  # ptyprocess/winpty handle when use_pty=True


class ProcessRegistry:
    """Tracks background processes, output buffers, watchers, and checkpoints."""

    _SHELL_NOISE_SUBSTRINGS = (
        "bash: cannot set terminal process group",
        "bash: no job control in this shell",
        "no job control in this shell",
        "cannot set terminal process group",
        "tcsetattr: Inappropriate ioctl for device",
    )

    def __init__(self) -> None:
        self._running: dict[str, ProcessSession] = {}
        self._finished: dict[str, ProcessSession] = {}
        self._lock = threading.Lock()
        self.pending_watchers: list[dict[str, Any]] = []
        self._next_watcher_due: float | None = None
        self.completion_queue: queue.Queue = queue.Queue()

    # ── Utilities ──

    @staticmethod
    def _clean_shell_noise(text: str) -> str:
        lines = text.split("\n")
        while lines and any(
            noise in lines[0] for noise in ProcessRegistry._SHELL_NOISE_SUBSTRINGS
        ):
            lines.pop(0)
        return "\n".join(lines)

    @staticmethod
    def _redact_session_output(session: ProcessSession, output: str) -> str:
        return redact_secret_values(output, session.secret_redactions)

    @staticmethod
    def _redacted_output_tail(session: ProcessSession, limit: int) -> str:
        if not session.output_buffer:
            return ""
        output = strip_ansi(session.output_buffer)
        output = redact_secret_values(output, session.secret_redactions)
        return output[-limit:]

    @staticmethod
    def _append_output(session: ProcessSession, chunk: str) -> None:
        if not chunk:
            return
        if session.log_path:
            try:
                log_path = Path(session.log_path)
                log_path.parent.mkdir(parents=True, exist_ok=True)
                with open(log_path, "a", encoding="utf-8", errors="replace") as handle:
                    handle.write(chunk)
            except OSError:
                logger.debug("Failed to append process log for %s", session.id, exc_info=True)
        with session._lock:
            session.output_buffer += chunk
            if len(session.output_buffer) > session.max_output_chars:
                session.output_buffer = session.output_buffer[
                    -session.max_output_chars :
                ]

    @staticmethod
    def _is_host_pid_alive(pid: int | None) -> bool:
        if not pid:
            return False
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError, OSError):
            return False

    @staticmethod
    def _positive_int(value: Any, default: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return default
        return parsed if parsed > 0 else default

    @staticmethod
    def _non_negative_int(value: Any, default: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return default
        return max(0, parsed)

    @staticmethod
    def _default_wait_timeout() -> int:
        cfg = load_config(strict=True)
        terminal_cfg = cfg.get("terminal", {}) if isinstance(cfg, dict) else {}
        if not isinstance(terminal_cfg, dict):
            return DEFAULT_WAIT_TIMEOUT_SECONDS
        return ProcessRegistry._positive_int(
            terminal_cfg.get("timeout"),
            DEFAULT_WAIT_TIMEOUT_SECONDS,
        )

    def _refresh_detached_session(
        self, session: ProcessSession | None
    ) -> ProcessSession | None:
        """Mark recovered host processes exited once their PID disappears."""
        if (
            session is None
            or session.exited
            or not session.detached
            or session.pid_scope != "host"
        ):
            return session
        if self._is_host_pid_alive(session.pid):
            return session
        with session._lock:
            if session.exited:
                return session
            session.exited = True
            session.exit_code = None
            session.finished_at = time.time()
        self._move_to_finished(session)
        return session

    def _ensure_checkpoint_present(self) -> None:
        """Recreate the checkpoint if in-memory running processes exist."""
        with self._lock:
            has_running = any(not s.exited for s in self._running.values())
        if has_running and not CHECKPOINT_PATH.exists():
            try:
                self._write_checkpoint()
            except Exception as exc:
                logger.warning("Failed to recreate process checkpoint: %s", exc)

    def _abort_spawn_after_checkpoint_failure(
        self, session: ProcessSession, exc: BaseException
    ) -> None:
        """Terminate a just-spawned process that could not be persisted."""
        session._termination_trigger = "checkpoint_failure"
        termination_confirmed = self._terminate_uncommitted_spawn(session)
        if termination_confirmed:
            with self._lock:
                self._running.pop(session.id, None)
            with session._lock:
                session.exited = True
                session.exit_code = None
        else:
            raise BackgroundSpawnCancellationError(
                session.pid or 0,
                (
                    f"Background process {session.pid or 0} could not be persisted, "
                    "and its termination could not be confirmed"
                ),
            ) from exc
        raise RuntimeError(
            f"Failed to persist background process registry for {session.id}: {exc}"
        ) from exc

    def _terminate_uncommitted_spawn(self, session: ProcessSession) -> bool:
        """Best-effort stop a spawn and confirm both its tree and direct handle."""
        cancel_id = getattr(session, "_cancel_id", "none")
        trigger = getattr(session, "_termination_trigger", "cancel_before_commit")
        started = time.monotonic()
        termination_deadline = started + PROCESS_TERMINATION_BUDGET_SECONDS
        process_group_id = session.process_group_id if not _IS_WINDOWS else None
        if process_group_id:
            tree_targeted = kill_process_group(process_group_id)
        elif session.pid:
            # Target the recorded tree even if the direct parent already
            # exited; parent state alone says nothing about descendants.
            tree_targeted = kill_process_tree(session.pid)
        else:
            tree_targeted = False
        safe_cancel_trace(
            lambda: logger.warning(
                "[CANCEL_TRACE] termination_start cancel_id=%s "
                "operation=background_spawn pid=%s trigger=%s",
                cancel_id,
                session.pid,
                trigger,
            )
        )

        direct_termination_confirmed = False
        if session._pty is not None:
            try:
                isalive = getattr(session._pty, "isalive", None)
                direct_deadline = min(
                    termination_deadline,
                    time.monotonic() + 0.5,
                )
                while callable(isalive):
                    if not isalive():
                        direct_termination_confirmed = True
                        break
                    remaining = direct_deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    time.sleep(min(0.05, remaining))
            except Exception:
                safe_cancel_trace(
                    lambda: logger.debug(
                        "Failed to stop PTY cancelled during spawn", exc_info=True
                    )
                )
        elif session.process is not None:
            wait_budget = min(
                0.5,
                max(0.0, termination_deadline - time.monotonic()),
            )
            try:
                if wait_budget <= 0:
                    raise subprocess.TimeoutExpired("background_spawn", 0)
                session.process.wait(timeout=wait_budget)
            except (subprocess.TimeoutExpired, OSError):
                direct_termination_confirmed = False
            else:
                direct_termination_confirmed = session.process.poll() is not None

        tree_termination_confirmed = (
            wait_for_process_group_exit(
                process_group_id,
                timeout=max(0.0, termination_deadline - time.monotonic()),
            )
            if process_group_id
            else tree_targeted if _IS_WINDOWS else False
        )
        termination_confirmed = tree_termination_confirmed and direct_termination_confirmed
        log = logger.info if termination_confirmed else logger.warning
        safe_cancel_trace(
            lambda: log(
                "[CANCEL_TRACE] termination_result cancel_id=%s "
                "operation=background_spawn pid=%s trigger=%s tree_targeted=%s "
                "tree_confirmed=%s "
                "direct_exit_confirmed=%s termination_confirmed=%s "
                "persistent_fence=%s elapsed_ms=%d",
                cancel_id,
                session.pid,
                trigger,
                tree_targeted,
                tree_termination_confirmed,
                direct_termination_confirmed,
                termination_confirmed,
                not termination_confirmed,
                int((time.monotonic() - started) * 1000),
            )
        )
        return termination_confirmed

    def _abort_cancelled_spawn(self, session: ProcessSession) -> None:
        """Stop an uncommitted spawn or raise a permanent completion fence."""
        if not self._terminate_uncommitted_spawn(session):
            raise BackgroundSpawnCancellationError(session.pid or 0)
        raise InterruptedError("Background process start was cancelled")

    def _register_running_or_abort(
        self,
        session: ProcessSession,
        cancel_event: threading.Event | None = None,
    ) -> None:
        """Commit a running session, with cancellation checked at the commit point."""
        with self._lock:
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
            else:
                cancelled = False
                self._prune_if_needed()
                self._running[session.id] = session
        if cancelled:
            session._cancel_id = get_cancel_id(cancel_event)
            try:
                self._abort_cancelled_spawn(session)
            finally:
                safe_cancel_trace(
                    lambda: logger.warning(
                        "[CANCEL_TRACE] background_cancel_at_commit cancel_id=%s "
                        "session_id=%s pid=%s",
                        session._cancel_id,
                        session.id,
                        session.pid,
                    )
                )
        try:
            self._write_checkpoint()
        except Exception as exc:
            self._abort_spawn_after_checkpoint_failure(session, exc)

    def update_session_metadata(
        self,
        session_id: str,
        *,
        notify_on_complete: bool | None = None,
        watcher_interval: int | None = None,
    ) -> dict[str, Any] | None:
        """Update persisted process metadata without exposing direct mutation."""
        session = self.get(session_id)
        if session is None:
            return None
        with session._lock:
            if notify_on_complete is not None:
                session.notify_on_complete = bool(notify_on_complete)
            if watcher_interval is not None:
                session.watcher_interval = int(watcher_interval or 0)
            result = {
                "session_id": session.id,
                "notify_on_complete": session.notify_on_complete,
                "watcher_interval": session.watcher_interval,
            }
        self._write_checkpoint()
        return result

    def register_watcher(
        self,
        *,
        session_id: str,
        check_interval: int,
        session_key: str = "",
        notify_on_complete: bool = False,
    ) -> dict[str, Any] | None:
        """Register or update a watcher for a background process session."""
        if not session_id:
            return None
        interval = max(30, int(check_interval or 0))
        now = time.monotonic()
        with self._lock:
            existing = None
            for watcher in self.pending_watchers:
                if watcher.get("session_id") == session_id:
                    existing = watcher
                    break
            if existing is None:
                existing = {
                    "session_id": session_id,
                    "session_key": session_key,
                    "check_interval": interval,
                    "notify_on_complete": bool(notify_on_complete),
                    "last_output_len": 0,
                    "next_check_at_monotonic": now + interval,
                }
                self.pending_watchers.append(existing)
            else:
                existing["session_key"] = session_key or existing.get("session_key", "")
                existing["check_interval"] = interval
                existing["notify_on_complete"] = bool(
                    notify_on_complete or existing.get("notify_on_complete", False)
                )
                existing.setdefault("last_output_len", 0)
                existing["next_check_at_monotonic"] = now + interval
            session = self._running.get(session_id)
            if session is not None:
                with session._lock:
                    session.watcher_interval = interval
                    session.notify_on_complete = bool(
                        notify_on_complete or session.notify_on_complete
                    )
            self._refresh_next_watcher_due_locked()
            result = dict(existing)
        try:
            self._write_checkpoint()
        except Exception as exc:
            logger.warning("Failed to persist watcher metadata for %s: %s", session_id, exc)
        return result

    def pump_due_watchers(self) -> int:
        """Poll due watchers and enqueue watcher events into completion_queue."""
        now = time.monotonic()
        with self._lock:
            if not self.pending_watchers:
                self._next_watcher_due = None
                return 0
            due_at = self._next_watcher_due
            if due_at is not None and now < due_at:
                return 0
            watchers = list(self.pending_watchers)

        events: list[dict[str, Any]] = []
        updated_watchers: list[dict[str, Any]] = []

        for watcher in watchers:
            sid = str(watcher.get("session_id", ""))
            interval = max(30, int(watcher.get("check_interval", 30) or 30))
            next_due = float(watcher.get("next_check_at_monotonic", 0.0) or 0.0)
            if next_due > now:
                updated_watchers.append(watcher)
                continue

            if not sid:
                continue

            session = self.get(sid)
            if session is None:
                continue

            last_output_len = int(watcher.get("last_output_len", 0) or 0)
            with session._lock:
                current_output_len = len(session.output_buffer)
                tail = self._redacted_output_tail(session, 1000)

            if session.exited:
                if not watcher.get("notify_on_complete", False):
                    events.append(
                        {
                            "event_type": "watcher_complete",
                            "session_id": session.id,
                            "session_key": watcher.get("session_key", ""),
                            "command": self._redact_session_output(session, session.command),
                            "exit_code": session.exit_code,
                            "output": tail,
                        }
                    )
                continue

            if current_output_len > last_output_len:
                events.append(
                    {
                        "event_type": "watcher_update",
                        "session_id": session.id,
                        "session_key": watcher.get("session_key", ""),
                        "command": self._redact_session_output(session, session.command),
                        "output": tail,
                        "uptime_seconds": int(time.time() - session.started_at),
                    }
                )

            watcher["last_output_len"] = current_output_len
            watcher["next_check_at_monotonic"] = now + interval
            updated_watchers.append(watcher)

        with self._lock:
            self.pending_watchers = updated_watchers
            self._refresh_next_watcher_due_locked()

        for event in events:
            self.completion_queue.put(event)

        return len(events)

    def _refresh_next_watcher_due_locked(self) -> None:
        """Recompute the next due timestamp for pending watchers (lock required)."""
        if not self.pending_watchers:
            self._next_watcher_due = None
            return
        self._next_watcher_due = min(
            float(w.get("next_check_at_monotonic", 0.0) or 0.0)
            for w in self.pending_watchers
        )

    # ── Spawn ──

    def spawn_local(
        self,
        command: str,
        cwd: str | None = None,
        task_id: str = "",
        session_key: str = "",
        env_vars: dict | None = None,
        use_pty: bool = False,
        scoped_secret_keys: set[str] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> ProcessSession:
        """Spawn a local background command and persist it before returning."""
        session = ProcessSession(
            id=f"proc_{uuid.uuid4().hex[:12]}",
            command=command,
            task_id=task_id,
            session_key=session_key,
            cwd=cwd or os.getcwd(),
            started_at=time.time(),
        )
        allowed = set(scoped_secret_keys or set())
        merged = sanitize_subprocess_env(os.environ, allowed_sensitive=allowed)
        if env_vars:
            merged.update(env_vars)
        bg_env = sanitize_subprocess_env(merged, allowed_sensitive=allowed)
        bg_env["PYTHONUNBUFFERED"] = "1"
        session.secret_redactions = tuple(
            str(bg_env.get(key) or "")
            for key in sorted(allowed)
            if str(bg_env.get(key) or "")
        )

        runtime = RuntimeManager.current()
        log_root = runtime.paths.process_log_root()
        log_root.mkdir(parents=True, exist_ok=True)
        session.runtime_kind = runtime.kind
        session.log_path = str(log_root / f"{session.id}.log")
        spawned = runtime.spawn(
            command,
            cwd=session.cwd,
            env=bg_env,
            use_pty=use_pty,
            scoped_secret_keys=allowed,
            cancel_event=cancel_event,
        )
        session.process = spawned.process
        session.pid = spawned.pid
        session.cwd = spawned.cwd
        session.env_hash = spawned.env_hash
        session.shell_profile = spawned.shell_profile
        session._pty = spawned.pty
        if session.pid and not _IS_WINDOWS:
            try:
                session.process_group_id = os.getpgid(session.pid)
            except OSError:
                session.process_group_id = None
        if cancel_event is not None and cancel_event.is_set():
            session._cancel_id = get_cancel_id(cancel_event)
            try:
                self._abort_cancelled_spawn(session)
            finally:
                safe_cancel_trace(
                    lambda: logger.warning(
                        "[CANCEL_TRACE] background_cancel_post_spawn cancel_id=%s "
                        "session_id=%s pid=%s",
                        session._cancel_id,
                        session.id,
                        session.pid,
                    )
                )
        if spawned.pty_disabled_reason:
            safe_cancel_trace(lambda: logger.warning(spawned.pty_disabled_reason))
        if session._pty is not None:
            reader = threading.Thread(
                target=self._pty_reader_loop,
                args=(session,),
                daemon=True,
                name=f"proc-pty-reader-{session.id}",
            )
            session._reader_thread = reader
            self._register_running_or_abort(session, cancel_event)
            reader.start()
            return session
        reader = threading.Thread(
            target=self._reader_loop,
            args=(session,),
            daemon=True,
            name=f"proc-reader-{session.id}",
        )
        session._reader_thread = reader
        self._register_running_or_abort(session, cancel_event)
        reader.start()
        return session

    # ── Reader thread ──

    def _reader_loop(self, session: ProcessSession) -> None:
        """Background thread: capture pipe output and finalize process state."""
        assert session.process and session.process.stdout
        first_chunk = True
        try:
            for chunk in session.process.stdout:
                if first_chunk:
                    chunk = self._clean_shell_noise(chunk)
                    first_chunk = False
                self._append_output(session, chunk)
        except Exception as e:
            logger.debug("Process stdout reader ended: %s", e)

        try:
            session.process.wait(timeout=5)
        except Exception as e:
            logger.debug("Process wait: %s", e)
        with session._lock:
            session.exited = True
            session.exit_code = session.process.returncode
            session.finished_at = time.time()
        self._move_to_finished(session)

    def _pty_reader_loop(self, session: ProcessSession) -> None:
        """Background thread: read output from a PTY process handle."""
        pty = session._pty
        first_chunk = True
        try:
            while True:
                try:
                    chunk = pty.read(4096)
                except EOFError:
                    break
                except Exception as exc:
                    logger.debug("PTY read stopped for %s: %s", session.id, exc)
                    break
                if not chunk:
                    break
                if isinstance(chunk, bytes):
                    chunk = chunk.decode("utf-8", errors="replace")
                if first_chunk:
                    chunk = self._clean_shell_noise(chunk)
                    first_chunk = False
                self._append_output(session, chunk)
        except Exception as e:
            logger.debug("PTY reader ended: %s", e)

        try:
            pty.wait()
        except Exception as exc:
            logger.debug("PTY wait failed for %s: %s", session.id, exc)
        with session._lock:
            session.exited = True
            try:
                session.exit_code = pty.exitstatus
            except Exception:
                session.exit_code = None
            session.finished_at = time.time()
        self._move_to_finished(session)

    # ── State transitions ──

    def _move_to_finished(self, session: ProcessSession) -> None:
        """Move a session out of running state and enqueue completion if needed."""
        with session._lock:
            if session.finished_at <= 0:
                session.finished_at = time.time()
        with self._lock:
            self._running.pop(session.id, None)
            self._finished[session.id] = session
        try:
            self._write_checkpoint()
        except Exception as exc:
            logger.warning(
                "Checkpoint write failed for session %s (pid=%s): %s — "
                "session state is in-memory but not persisted",
                session.id, session.pid, exc,
            )
        if session.notify_on_complete:
            tail = self._redacted_output_tail(session, 2000)
            self.completion_queue.put(
                {
                    "event_type": "process_complete",
                    "session_id": session.id,
                    "session_key": session.session_key,
                    "command": self._redact_session_output(session, session.command),
                    "exit_code": session.exit_code,
                    "output": tail,
                }
            )

    # ── Query methods ──

    def get(self, session_id: str) -> ProcessSession | None:
        """Return a session after refreshing detached-process liveness."""
        with self._lock:
            session = self._running.get(session_id) or self._finished.get(session_id)
        session = self._refresh_detached_session(session)
        if session is not None and not session.exited:
            self._ensure_checkpoint_present()
        return session

    def poll(self, session_id: str) -> dict:
        """Return status and a redacted output preview for a process session."""
        session = self.get(session_id)
        if session is None:
            return {"status": "not_found", "error": f"No process with ID {session_id}"}
        with session._lock:
            preview = self._redacted_output_tail(session, 1000)
        result: dict[str, Any] = {
            "session_id": session.id,
            "command": self._redact_session_output(session, session.command),
            "status": "exited" if session.exited else "running",
            "pid": session.pid,
            "uptime_seconds": int(time.time() - session.started_at),
            "output_preview": preview,
        }
        if session.exited:
            result["exit_code"] = session.exit_code
        if session.detached:
            result["detached"] = True
            result["note"] = "Process recovered after restart — output history unavailable"
        return result

    def read_log(
        self, session_id: str, offset: int = 0, limit: int = 200
    ) -> dict:
        """Read redacted captured output using latest-lines or offset semantics."""
        session = self.get(session_id)
        if session is None:
            return {"status": "not_found", "error": f"No process with ID {session_id}"}
        with session._lock:
            full_output = strip_ansi(session.output_buffer)
            full_output = self._redact_session_output(session, full_output)
        lines = full_output.splitlines()
        total_lines = len(lines)
        offset = self._non_negative_int(offset, 0)
        limit = self._positive_int(limit, 200)
        if offset == 0 and limit > 0:
            selected = lines[-limit:]
        else:
            selected = lines[offset : offset + limit]
        return {
            "session_id": session.id,
            "status": "exited" if session.exited else "running",
            "output": "\n".join(selected),
            "total_lines": total_lines,
            "showing": f"{len(selected)} lines",
        }

    def wait(self, session_id: str, timeout: int | None = None) -> dict:
        """Block until a process exits, timeout expires, or user interrupt fires."""
        default_timeout = self._default_wait_timeout()
        max_timeout = default_timeout
        requested = self._positive_int(timeout, 0) if timeout is not None else None
        timeout_note = None
        if requested and requested > max_timeout:
            effective_timeout = max_timeout
            timeout_note = (
                f"Requested wait of {requested}s was clamped to {max_timeout}s"
            )
        else:
            effective_timeout = requested or max_timeout

        session = self.get(session_id)
        if session is None:
            return {"status": "not_found", "error": f"No process with ID {session_id}"}

        cancel_event = get_interrupt_event()
        deadline = time.monotonic() + effective_timeout
        while True:
            session = self._refresh_detached_session(session)
            assert session is not None
            if session.exited:
                result: dict[str, Any] = {
                    "status": "exited",
                    "exit_code": session.exit_code,
                    "output": self._redacted_output_tail(session, 2000),
                }
                if timeout_note:
                    result["timeout_note"] = timeout_note
                return result
            if is_interrupted():
                result = {
                    "status": "interrupted",
                    "interrupted": True,
                    "output": self._redacted_output_tail(session, 1000),
                    "note": "User interrupted wait",
                }
                if timeout_note:
                    result["timeout_note"] = timeout_note
                return result
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if cancel_event is not None:
                cancel_event.wait(min(0.1, remaining))
            else:
                time.sleep(min(0.1, remaining))

        result = {
            "status": "timeout",
            "output": self._redacted_output_tail(session, 1000),
        }
        if timeout_note:
            result["timeout_note"] = timeout_note
        else:
            result["timeout_note"] = (
                f"Waited {effective_timeout}s, process still running"
            )
        return result

    # ── Process control ──

    def _wait_for_exit_after_kill(
        self,
        session: ProcessSession,
        timeout: float = 5.0,
    ) -> int | None:
        """Confirm process termination after a kill request."""
        if session._pty is not None:
            pty = session._pty
            is_alive = getattr(pty, "isalive", None) or getattr(pty, "is_alive", None)
            if callable(is_alive):
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    try:
                        if not bool(is_alive()):
                            break
                    except Exception as exc:
                        logger.debug("PTY liveness check failed for %s: %s", session.id, exc)
                        break
                    time.sleep(0.05)
                else:
                    raise RuntimeError("Process did not exit after kill request")
            try:
                exit_status = getattr(pty, "exitstatus", None)
                return int(exit_status) if exit_status is not None else -15
            except (TypeError, ValueError):
                return -15

        if session.process is not None:
            try:
                return int(session.process.wait(timeout=timeout))
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError("Process did not exit after kill request") from exc

        if session.pid_scope == "host" and session.pid:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if not self._is_host_pid_alive(session.pid):
                    return None
                time.sleep(0.05)
            raise RuntimeError("Process did not exit after kill request")

        return None

    def kill_process(self, session_id: str) -> dict:
        """Terminate a running process through PTY, process tree, or recovered PID."""
        session = self.get(session_id)
        if session is None:
            return {"status": "not_found", "error": f"No process with ID {session_id}"}
        if session.exited:
            return {"status": "already_exited", "exit_code": session.exit_code}
        try:
            if session._pty is not None:
                # PTY mode: terminate through the PTY handle first.
                try:
                    session._pty.terminate(force=True)
                except Exception as exc:
                    logger.debug("PTY terminate failed for %s: %s", session.id, exc)
            elif session.process and session.pid:
                # Fall back to the platform process-tree killer on Windows and Unix.
                kill_process_tree(session.pid)
            elif session.detached and session.pid_scope == "host" and session.pid:
                if not self._is_host_pid_alive(session.pid):
                    with session._lock:
                        session.exited = True
                        session.exit_code = None
                    self._move_to_finished(session)
                    return {"status": "already_exited", "exit_code": session.exit_code}
                kill_process_tree(session.pid)
            else:
                return {
                    "status": "error",
                    "error": "Cannot kill: no process handle",
                }
            exit_code = self._wait_for_exit_after_kill(session)
            with session._lock:
                session.exited = True
                session.exit_code = exit_code if exit_code is not None else -15
                session.finished_at = time.time()
            self._move_to_finished(session)
            return {
                "status": "killed",
                "session_id": session.id,
                "exit_code": session.exit_code,
            }
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def kill_all(self, task_id: str | None = None) -> int:
        """Kill running processes, scoped to task_id when provided."""
        with self._lock:
            targets = list(self._running.values())
        if task_id:
            targets = [s for s in targets if s.task_id == task_id]
        killed = 0
        for s in targets:
            result = self.kill_process(s.id)
            if result.get("status") == "killed":
                killed += 1
        return killed

    # ── Listing / active checks ──

    def list_sessions(self, task_id: str | None = None) -> list:
        """Return process sessions, scoped to task_id only for internal callers."""
        self._ensure_checkpoint_present()
        with self._lock:
            all_sessions = list(self._running.values()) + list(self._finished.values())
        all_sessions = [self._refresh_detached_session(s) for s in all_sessions]
        if task_id:
            all_sessions = [s for s in all_sessions if s and s.task_id == task_id]
        result = []
        for s in all_sessions:
            if s is None:
                continue
            entry = {
                "session_id": s.id,
                "command": self._redact_session_output(s, s.command[:200]),
                "cwd": s.cwd,
                "pid": s.pid,
                "runtime_kind": s.runtime_kind,
                "shell_profile": s.shell_profile,
                "log_path": s.log_path,
                "started_at": time.strftime(
                    "%Y-%m-%dT%H:%M:%S", time.localtime(s.started_at)
                ),
                "uptime_seconds": int(time.time() - s.started_at),
                "status": "exited" if s.exited else "running",
                "output_preview": self._redacted_output_tail(s, 200),
            }
            if s.exited:
                entry["exit_code"] = s.exit_code
            if s.detached:
                entry["detached"] = True
            result.append(entry)
        return result

    def has_active_processes(self, task_id: str) -> bool:
        with self._lock:
            sessions = list(self._running.values())
        for session in sessions:
            self._refresh_detached_session(session)
        with self._lock:
            return any(
                s.task_id == task_id and not s.exited for s in self._running.values()
            )

    # ── Pruning / checkpoint ──

    def _prune_if_needed(self) -> None:
        now = time.time()
        expired = [
            sid
            for sid, s in self._finished.items()
            if (now - (s.finished_at or s.started_at)) > FINISHED_TTL_SECONDS
        ]
        for sid in expired:
            del self._finished[sid]
        total = len(self._running) + len(self._finished)
        if total >= MAX_PROCESSES and self._finished:
            oldest_id = min(
                self._finished,
                key=lambda sid: (
                    self._finished[sid].finished_at
                    or self._finished[sid].started_at
                ),
            )
            del self._finished[oldest_id]

    def _write_checkpoint(self) -> None:
        """Persist only still-running sessions so restart recovery is bounded."""
        with self._lock:
            entries = []
            for s in self._running.values():
                if s.exited:
                    continue
                entries.append(
                    {
                        "session_id": s.id,
                        "command": self._redact_session_output(s, s.command),
                        "pid": s.pid,
                        "pid_scope": s.pid_scope,
                        "runtime_kind": s.runtime_kind,
                        "process_group_id": s.process_group_id,
                        "log_path": s.log_path,
                        "env_hash": s.env_hash,
                        "shell_profile": s.shell_profile,
                        "cwd": s.cwd,
                        "started_at": s.started_at,
                        "task_id": s.task_id,
                        "session_key": s.session_key,
                        "watcher_interval": s.watcher_interval,
                        "notify_on_complete": s.notify_on_complete,
                    }
                )
        atomic_json_write(CHECKPOINT_PATH, entries)

    def recover_from_checkpoint(self) -> int:
        """Recover detached processes from the checkpoint file after restart.

        Dead PID entries are pruned during recovery, keeping the checkpoint
        file focused on currently observable host processes.
        """
        if not CHECKPOINT_PATH.exists():
            return 0
        try:
            data = json.loads(CHECKPOINT_PATH.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.debug("Failed to read process checkpoint: %s", exc)
            return 0
        recovered = 0
        for entry in data:
            pid = entry.get("pid")
            pid_scope = entry.get("pid_scope", "host")
            session_id = entry.get("session_id", "")
            if pid_scope != "host" or not pid:
                # Only host-scoped processes can be recovered after restart.
                continue
            if not self._is_host_pid_alive(pid):
                # Unreachable PIDs are removed when the checkpoint is rewritten.
                logger.debug("Dropping dead PID %s from checkpoint", pid)
                continue
            session = ProcessSession(
                id=session_id,
                command=entry.get("command", ""),
                task_id=entry.get("task_id", ""),
                session_key=entry.get("session_key", ""),
                pid=pid,
                cwd=entry.get("cwd"),
                started_at=entry.get("started_at", time.time()),
                detached=True,
                pid_scope="host",
                runtime_kind=entry.get("runtime_kind", ""),
                process_group_id=entry.get("process_group_id"),
                log_path=entry.get("log_path", ""),
                env_hash=entry.get("env_hash", ""),
                shell_profile=entry.get("shell_profile", ""),
                watcher_interval=entry.get("watcher_interval", 0),
                notify_on_complete=entry.get("notify_on_complete", False),
            )
            with self._lock:
                self._running[session_id] = session
            if session.watcher_interval > 0:
                self.register_watcher(
                    session_id=session_id,
                    check_interval=session.watcher_interval,
                    session_key=session.session_key,
                    notify_on_complete=session.notify_on_complete,
                )
            recovered += 1
            logger.info("Recovered detached process: %s (pid=%d)", session_id, pid)

        # Always rewrite the checkpoint after restore so stale PIDs are
        # removed and only still-running recovered sessions remain.
        self._write_checkpoint()
        return recovered


process_registry = ProcessRegistry()


# Tool schema and handlers.

PROCESS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "process",
        "description": (
            "Manage background processes started with terminal(background=true). "
            "Actions:\n"
            "  list   - list all background processes visible to the runtime;\n"
            "  poll   - check status + latest output preview for a session_id;\n"
            "  log    - read paginated stdout/stderr lines (offset, limit params);\n"
            "  wait   - block until process exits or timeout expires;\n"
            "  kill   - terminate a running process by session_id."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "list",
                        "poll",
                        "log",
                        "wait",
                        "kill",
                    ],
                    "description": (
                        "Action to perform. "
                        "'list' requires no session_id. "
                        "All others require session_id."
                    ),
                },
                "session_id": {
                    "type": "string",
                    "description": (
                        "Process session id returned by terminal(background=true). "
                        "Required for all actions except 'list'."
                    ),
                },
                "timeout": {
                    "type": "integer",
                    "description": "Max seconds to block for 'wait' (default: from config, max clamped).",
                    "minimum": 1,
                },
                "offset": {
                    "type": "integer",
                    "description": (
                        "Line offset for 'log'. Use 0 or omit it for the latest lines; "
                        "positive values read from that zero-based line offset."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Max lines to return for 'log' (default 200).",
                    "minimum": 1,
                },
            },
            "required": ["action"],
        },
    },
}


def _handle_process(args: dict, **kw: Any) -> str:
    action = args.get("action", "")
    sid = args.get("session_id")
    session_id = str(sid) if sid is not None else ""

    if action == "list":
        # The public process list is a runtime-wide view, keeping restored
        # and cross-task background sessions visible from the control surface.
        return json.dumps(
            {"processes": process_registry.list_sessions(task_id=None)},
            ensure_ascii=False,
        )
    if action in ("poll", "log", "wait", "kill"):
        if not session_id:
            return tool_error(f"session_id is required for {action}")
        if action == "poll":
            return json.dumps(process_registry.poll(session_id), ensure_ascii=False)
        if action == "log":
            return json.dumps(
                process_registry.read_log(
                    session_id,
                    offset=args.get("offset", 0),
                    limit=args.get("limit", 200),
                ),
                ensure_ascii=False,
            )
        if action == "wait":
            try:
                return json.dumps(
                    process_registry.wait(session_id, timeout=args.get("timeout")),
                    ensure_ascii=False,
                )
            except ConfigError as exc:
                return tool_error(f"Configuration error: {exc}")
        if action == "kill":
            return json.dumps(
                process_registry.kill_process(session_id), ensure_ascii=False
            )
    return tool_error(
        f"Unknown process action: {action}. Use: list, poll, log, wait, kill"
    )


registry.register(
    name="process",
    toolset="terminal",
    schema=PROCESS_SCHEMA,
    handler=_handle_process,
    description="Manage background processes",
    emoji="⚙️",
)
