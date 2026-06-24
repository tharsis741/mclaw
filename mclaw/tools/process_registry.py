"""Background process registry for local backend."""

from __future__ import annotations

import json
import logging
import os
import platform as _platform_mod
import queue
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

_IS_WINDOWS = _platform_mod.system() == "Windows"

from mclaw.constants import get_mclaw_home
from mclaw.runtime.manager import RuntimeManager
from mclaw.runtime.process import kill_process_tree, sanitize_subprocess_env
from mclaw.runtime.secrets import redact_secret_values
from mclaw.tools.ansi_strip import strip_ansi
from mclaw.tools.interrupt import is_interrupted
from mclaw.tools.registry import registry, tool_error
from mclaw.utils import atomic_json_write

logger = logging.getLogger(__name__)

CHECKPOINT_PATH = get_mclaw_home() / "processes.json"
MAX_OUTPUT_CHARS = 200_000
FINISHED_TTL_SECONDS = 1800
MAX_PROCESSES = 64


@dataclass
class ProcessSession:
    id: str
    command: str
    task_id: str = ""
    session_key: str = ""
    pid: Optional[int] = None
    process: Optional[subprocess.Popen] = None
    cwd: Optional[str] = None
    started_at: float = 0.0
    exited: bool = False
    exit_code: Optional[int] = None
    output_buffer: str = ""
    max_output_chars: int = MAX_OUTPUT_CHARS
    detached: bool = False
    pid_scope: str = "host"
    runtime_kind: str = ""
    process_group_id: Optional[int] = None
    log_path: str = ""
    env_hash: str = ""
    shell_profile: str = ""
    notify_on_complete: bool = False
    watcher_platform: str = ""
    watcher_chat_id: str = ""
    watcher_thread_id: str = ""
    watcher_interval: int = 0
    secret_redactions: tuple[str, ...] = field(default_factory=tuple, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _reader_thread: Optional[threading.Thread] = field(default=None, repr=False)
    _pty: Any = field(default=None, repr=False)  # ptyprocess/winpty handle when use_pty=True


class ProcessRegistry:
    _SHELL_NOISE_SUBSTRINGS = (
        "bash: cannot set terminal process group",
        "bash: no job control in this shell",
        "no job control in this shell",
        "cannot set terminal process group",
        "tcsetattr: Inappropriate ioctl for device",
    )

    def __init__(self) -> None:
        self._running: Dict[str, ProcessSession] = {}
        self._finished: Dict[str, ProcessSession] = {}
        self._lock = threading.Lock()
        self.pending_watchers: List[Dict[str, Any]] = []
        self._next_watcher_due: Optional[float] = None
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
    def _is_host_pid_alive(pid: Optional[int]) -> bool:
        if not pid:
            return False
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError, OSError):
            return False

    def _refresh_detached_session(
        self, session: Optional[ProcessSession]
    ) -> Optional[ProcessSession]:
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
        try:
            if session._pty is not None:
                try:
                    session._pty.terminate(force=True)
                except Exception:
                    pass
            elif session.pid:
                kill_process_tree(session.pid)
        finally:
            with self._lock:
                self._running.pop(session.id, None)
            with session._lock:
                session.exited = True
                session.exit_code = None
        raise RuntimeError(
            f"Failed to persist background process registry for {session.id}: {exc}"
        ) from exc

    def _register_running_or_abort(self, session: ProcessSession) -> None:
        """Register a running session and persist it before returning to caller."""
        with self._lock:
            self._prune_if_needed()
            self._running[session.id] = session
        try:
            self._write_checkpoint()
        except Exception as exc:
            self._abort_spawn_after_checkpoint_failure(session, exc)

    def update_session_metadata(
        self,
        session_id: str,
        *,
        notify_on_complete: Optional[bool] = None,
        watcher_interval: Optional[int] = None,
        watcher_platform: Optional[str] = None,
        watcher_chat_id: Optional[str] = None,
        watcher_thread_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Update persisted process metadata without exposing direct mutation."""
        session = self.get(session_id)
        if session is None:
            return None
        with session._lock:
            if notify_on_complete is not None:
                session.notify_on_complete = bool(notify_on_complete)
            if watcher_interval is not None:
                session.watcher_interval = int(watcher_interval or 0)
            if watcher_platform is not None:
                session.watcher_platform = watcher_platform
            if watcher_chat_id is not None:
                session.watcher_chat_id = watcher_chat_id
            if watcher_thread_id is not None:
                session.watcher_thread_id = watcher_thread_id
            result = {
                "session_id": session.id,
                "notify_on_complete": session.notify_on_complete,
                "watcher_interval": session.watcher_interval,
                "watcher_platform": session.watcher_platform,
                "watcher_chat_id": session.watcher_chat_id,
                "watcher_thread_id": session.watcher_thread_id,
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
        platform: str = "",
        chat_id: str = "",
        thread_id: str = "",
    ) -> Optional[Dict[str, Any]]:
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
                    "platform": platform,
                    "chat_id": chat_id,
                    "thread_id": thread_id,
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
                existing["platform"] = platform or existing.get("platform", "")
                existing["chat_id"] = chat_id or existing.get("chat_id", "")
                existing["thread_id"] = thread_id or existing.get("thread_id", "")
                existing.setdefault("last_output_len", 0)
                existing["next_check_at_monotonic"] = now + interval
            session = self._running.get(session_id)
            if session is not None:
                with session._lock:
                    session.watcher_interval = interval
                    session.notify_on_complete = bool(
                        notify_on_complete or session.notify_on_complete
                    )
                    session.watcher_platform = platform or session.watcher_platform
                    session.watcher_chat_id = chat_id or session.watcher_chat_id
                    session.watcher_thread_id = thread_id or session.watcher_thread_id
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

        events: List[Dict[str, Any]] = []
        updated_watchers: List[Dict[str, Any]] = []

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
    ) -> ProcessSession:
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
        if spawned.pty_disabled_reason:
            logger.warning(spawned.pty_disabled_reason)
        if session._pty is not None:
            reader = threading.Thread(
                target=self._pty_reader_loop,
                args=(session,),
                daemon=True,
                name=f"proc-pty-reader-{session.id}",
            )
            session._reader_thread = reader
            reader.start()
            self._register_running_or_abort(session)
            return session
        reader = threading.Thread(
            target=self._reader_loop,
            args=(session,),
            daemon=True,
            name=f"proc-reader-{session.id}",
        )
        session._reader_thread = reader
        reader.start()
        self._register_running_or_abort(session)
        return session

    # ── Reader thread ──

    def _reader_loop(self, session: ProcessSession) -> None:
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
        session.exited = True
        session.exit_code = session.process.returncode
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
                except Exception:
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
        except Exception:
            pass
        session.exited = True
        try:
            session.exit_code = pty.exitstatus
        except Exception:
            session.exit_code = None
        self._move_to_finished(session)

    # ── State transitions ──

    def _move_to_finished(self, session: ProcessSession) -> None:
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

    def get(self, session_id: str) -> Optional[ProcessSession]:
        with self._lock:
            session = self._running.get(session_id) or self._finished.get(session_id)
        session = self._refresh_detached_session(session)
        if session is not None and not session.exited:
            self._ensure_checkpoint_present()
        return session

    def poll(self, session_id: str) -> dict:
        session = self.get(session_id)
        if session is None:
            return {"status": "not_found", "error": f"No process with ID {session_id}"}
        with session._lock:
            preview = self._redacted_output_tail(session, 1000)
        result: Dict[str, Any] = {
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
        session = self.get(session_id)
        if session is None:
            return {"status": "not_found", "error": f"No process with ID {session_id}"}
        with session._lock:
            full_output = strip_ansi(session.output_buffer)
            full_output = self._redact_session_output(session, full_output)
        lines = full_output.splitlines()
        total_lines = len(lines)
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
        try:
            from mclaw.cli.config import load_config

            cfg = load_config()
            default_timeout = int(cfg.get("terminal", {}).get("timeout", 180))
        except Exception:
            default_timeout = int(os.getenv("TERMINAL_TIMEOUT", "180"))
        max_timeout = default_timeout
        requested = timeout
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

        deadline = time.monotonic() + effective_timeout
        while time.monotonic() < deadline:
            session = self._refresh_detached_session(session)
            assert session is not None
            if session.exited:
                result: Dict[str, Any] = {
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
                    "output": self._redacted_output_tail(session, 1000),
                    "note": "User interrupted wait",
                }
                if timeout_note:
                    result["timeout_note"] = timeout_note
                return result
            time.sleep(1)

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

    def kill_process(self, session_id: str) -> dict:
        session = self.get(session_id)
        if session is None:
            return {"status": "not_found", "error": f"No process with ID {session_id}"}
        if session.exited:
            return {"status": "already_exited", "exit_code": session.exit_code}
        try:
            if session._pty is not None:
                # PTY 模式：通过 pty 句柄结束进程。
                try:
                    session._pty.terminate(force=True)
                except Exception:
                    pass
            elif session.process and session.pid:
                # Windows 和 Unix 都通过平台封装结束进程树。
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
            session.exited = True
            session.exit_code = -15
            self._move_to_finished(session)
            return {"status": "killed", "session_id": session.id}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def kill_all(self, task_id: str | None = None) -> int:
        """Kill all running processes, optionally filtered by task_id."""
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

    # ── Stdin ──

    def write_stdin(self, session_id: str, data: str) -> dict:
        session = self.get(session_id)
        if session is None:
            return {"status": "not_found", "error": f"No process with ID {session_id}"}
        if session.exited:
            return {"status": "already_exited", "error": "Process has already finished"}
        # PTY 模式。
        if session._pty is not None:
            try:
                raw = data.encode("utf-8") if isinstance(data, str) else data
                session._pty.write(raw)
                return {"status": "ok", "bytes_written": len(data)}
            except Exception as e:
                return {"status": "error", "error": str(e)}
        if not session.process or not session.process.stdin:
            return {
                "status": "error",
                "error": "stdin not available (non-local backend or stdin closed)",
            }
        try:
            session.process.stdin.write(data)
            session.process.stdin.flush()
            return {"status": "ok", "bytes_written": len(data)}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def submit_stdin(self, session_id: str, data: str = "") -> dict:
        return self.write_stdin(session_id, data + "\n")

    def close_stdin(self, session_id: str) -> dict:
        session = self.get(session_id)
        if session is None:
            return {"status": "not_found", "error": f"No process with ID {session_id}"}
        if session.exited:
            return {"status": "already_exited", "error": "Process has already finished"}
        # PTY 模式。
        if session._pty is not None:
            try:
                session._pty.sendeof()
                return {"status": "ok", "message": "EOF sent to PTY"}
            except Exception as e:
                return {"status": "error", "error": str(e)}
        if not session.process or not session.process.stdin:
            return {"status": "error", "error": "stdin not available (non-local backend or stdin closed)"}
        try:
            session.process.stdin.close()
            return {"status": "ok", "message": "stdin closed"}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    # ── Listing / active checks ──

    def list_sessions(self, task_id: str | None = None) -> list:
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

    def has_active_for_session(self, session_key: str) -> bool:
        """Check if any running processes belong to the given session_key."""
        with self._lock:
            return any(
                s.session_key == session_key and not s.exited
                for s in self._running.values()
            )

    # ── Pruning / checkpoint ──

    def _prune_if_needed(self) -> None:
        now = time.time()
        expired = [
            sid
            for sid, s in self._finished.items()
            if (now - s.started_at) > FINISHED_TTL_SECONDS
        ]
        for sid in expired:
            del self._finished[sid]
        total = len(self._running) + len(self._finished)
        if total >= MAX_PROCESSES and self._finished:
            oldest_id = min(
                self._finished, key=lambda sid: self._finished[sid].started_at
            )
            del self._finished[oldest_id]

    def _write_checkpoint(self) -> None:
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
                        "watcher_platform": s.watcher_platform,
                        "watcher_chat_id": s.watcher_chat_id,
                        "watcher_thread_id": s.watcher_thread_id,
                        "watcher_interval": s.watcher_interval,
                        "notify_on_complete": s.notify_on_complete,
                    }
                )
        atomic_json_write(CHECKPOINT_PATH, entries)

    def recover_from_checkpoint(self) -> int:
        """Recover detached processes from the checkpoint file after restart.

        Dead PIDs are silently dropped and the checkpoint is rewritten, so
        the file never accumulates stale entries across restarts.
        """
        if not CHECKPOINT_PATH.exists():
            return 0
        try:
            data = json.loads(CHECKPOINT_PATH.read_text(encoding="utf-8"))
        except Exception:
            return 0
        recovered = 0
        for entry in data:
            pid = entry.get("pid")
            pid_scope = entry.get("pid_scope", "host")
            session_id = entry.get("session_id", "")
            if pid_scope != "host" or not pid:
                # Non-host or malformed entry — drop silently
                continue
            if not self._is_host_pid_alive(pid):
                # Dead PID — skip and let checkpoint rewrite below clean it up
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
                watcher_platform=entry.get("watcher_platform", ""),
                watcher_chat_id=entry.get("watcher_chat_id", ""),
                watcher_thread_id=entry.get("watcher_thread_id", ""),
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
                    platform=session.watcher_platform,
                    chat_id=session.watcher_chat_id,
                    thread_id=session.watcher_thread_id,
                )
            recovered += 1
            logger.info("Recovered detached process: %s (pid=%d)", session_id, pid)

        # 恢复后始终重写 checkpoint，以清除失效 PID；
        # 重启后只保留仍存活的恢复会话。
        self._write_checkpoint()
        return recovered


process_registry = ProcessRegistry()


# ---------------------------------------------------------------------------
# 工具 schema 和处理器。
# ---------------------------------------------------------------------------

PROCESS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "process",
        "description": (
            "Manage background processes started with terminal(background=true). "
            "Actions:\n"
            "  list   — list all background processes (optionally filtered by task_id);\n"
            "  poll   — check status + latest output preview for a session_id;\n"
            "  log    — read paginated stdout/stderr lines (offset, limit params);\n"
            "  wait   — block until process exits or timeout expires;\n"
            "  kill   — terminate a running process by session_id."
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
                    "description": "Line offset for 'log' (0 = from start; negative unsupported).",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max lines to return for 'log' (default 200; 0 = last 200 lines).",
                    "minimum": 1,
                },
            },
            "required": ["action"],
        },
    },
}


def _handle_process(args: dict, **kw: Any) -> str:
    import json as _json

    task_id = kw.get("task_id")
    action = args.get("action", "")
    sid = args.get("session_id")
    session_id = str(sid) if sid is not None else ""

    if action == "list":
        # 这里不要按 task_id 过滤：list 应展示所有进程；
        # checkpoint 恢复的进程仍带旧 session 的 task_id，
        # 否则重启后会不可见。
        return _json.dumps(
            {"processes": process_registry.list_sessions(task_id=None)},
            ensure_ascii=False,
        )
    if action in ("poll", "log", "wait", "kill"):
        if not session_id:
            return tool_error(f"session_id is required for {action}")
        if action == "poll":
            return _json.dumps(process_registry.poll(session_id), ensure_ascii=False)
        if action == "log":
            return _json.dumps(
                process_registry.read_log(
                    session_id,
                    offset=args.get("offset", 0),
                    limit=args.get("limit", 200),
                ),
                ensure_ascii=False,
            )
        if action == "wait":
            return _json.dumps(
                process_registry.wait(session_id, timeout=args.get("timeout")),
                ensure_ascii=False,
            )
        if action == "kill":
            return _json.dumps(
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
    description="管理后台进程（与 terminal background 配套）",
    emoji="⚙️",
)
