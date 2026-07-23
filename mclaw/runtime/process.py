# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Process spawning, cleanup, and environment helpers for runtimes.

The helpers sanitize subprocess environments, select process profiles, and
terminate process trees in a way that works across local runtime backends.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import signal
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
_SENSITIVE_ENV_RE = re.compile(r"(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH)", re.IGNORECASE)
PROCESS_TERMINATION_BUDGET_SECONDS = 1.5


def sanitize_subprocess_env(env: dict[str, str] | os._Environ, *, allowed_sensitive: set[str] | None = None) -> dict[str, str]:
    """Drop sensitive environment variables unless explicitly scoped in."""
    allowed = {str(item) for item in (allowed_sensitive or set())}
    clean: dict[str, str] = {}
    for key, value in dict(env).items():
        text_key = str(key)
        if _SENSITIVE_ENV_RE.search(text_key) and text_key not in allowed:
            continue
        clean[text_key] = str(value)
    return clean


def env_hash(env: dict[str, str]) -> str:
    """Return a stable non-secret fingerprint for a sanitized environment."""
    payload = "\n".join(f"{key}={env[key]}" for key in sorted(env))
    return hashlib.sha256(payload.encode("utf-8", errors="ignore")).hexdigest()[:16]


def kill_process_tree(pid: int) -> bool:
    """Best-effort signal a process tree and report whether it was targeted.

    On POSIX this does not confirm termination.  The owning ``Popen``/PTY must
    first reap its direct child, then call :func:`wait_for_process_group_exit`.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            completed = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                timeout=1.0,
            )
            if completed.returncode == 0:
                return True
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass
        try:
            os.kill(pid, 9)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        return False
    pgid = get_process_group_id(pid)
    if pgid is not None:
        return kill_process_group(pgid)
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    return False


def get_process_group_id(pid: int) -> int | None:
    """Return a POSIX process group id while the direct child is observable."""
    if os.name == "nt" or pid <= 0:
        return None
    try:
        return os.getpgid(pid)
    except (ProcessLookupError, PermissionError, OSError):
        return None


def kill_process_group(process_group_id: int) -> bool:
    """Signal a known POSIX process group; do not claim that it has exited."""
    if os.name == "nt" or process_group_id <= 0:
        return False
    try:
        os.killpg(process_group_id, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except (PermissionError, OSError):
        return False
    time.sleep(0.5)
    try:
        os.killpg(process_group_id, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except (PermissionError, OSError):
        return False
    return True


def wait_for_process_group_exit(
    process_group_id: int,
    *,
    timeout: float = 1.0,
) -> bool:
    """Confirm that a POSIX process group disappeared within ``timeout``."""
    if os.name == "nt" or process_group_id <= 0:
        return False
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        try:
            os.killpg(process_group_id, 0)
        except ProcessLookupError:
            return True
        except (PermissionError, OSError):
            return False
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def run_captured_process(
    argv: list[str],
    *,
    timeout: float,
    cancel_event: threading.Event | None = None,
    cwd: str | os.PathLike | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run argv without a shell; on stop, kill, drain, reap, and confirm its tree."""
    from mclaw.tools.cancellation import cancellation_checkpoint

    cancellation_checkpoint(cancel_event)
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=cwd,
        env=env,
        start_new_session=os.name != "nt",
        creationflags=(
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            if os.name == "nt"
            else 0
        ),
    )
    process_group_id = get_process_group_id(proc.pid)
    deadline = time.monotonic() + max(0.001, timeout)
    while True:
        if proc.poll() is not None:
            drain_budget = min(0.05, max(0.0, deadline - time.monotonic()))
            if drain_budget > 0:
                try:
                    stdout, stderr = proc.communicate(timeout=drain_budget)
                    return subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)
                except subprocess.TimeoutExpired:
                    # A descendant can keep inherited pipes open after its parent
                    # exits. Continue to cancellation/deadline handling.
                    pass

        cancelled = cancel_event is not None and cancel_event.is_set()
        remaining = deadline - time.monotonic()
        if cancelled or remaining <= 0:
            from mclaw.tools.interrupt import get_cancel_id, safe_cancel_trace

            stop_observed_at = time.monotonic()
            # The child can exit after the poll above but before the stop signal
            # is observed. Consume that real result instead of reporting a
            # completed external effect as safely cancelled.
            if proc.poll() is not None:
                try:
                    stdout, stderr = proc.communicate(timeout=0.05)
                except subprocess.TimeoutExpired:
                    # A descendant still owns a pipe; terminate/probe the saved
                    # process group below rather than blocking on EOF.
                    pass
                else:
                    safe_cancel_trace(
                        lambda: logger.info(
                            "[CANCEL_TRACE] captured_process_completion_won_stop "
                            "cancel_id=%s pid=%s trigger=%s",
                            get_cancel_id(cancel_event),
                            proc.pid,
                            "user_interrupt" if cancelled else "deadline",
                        )
                    )
                    return subprocess.CompletedProcess(
                        argv,
                        proc.returncode,
                        stdout,
                        stderr,
                    )
            termination_deadline = (
                stop_observed_at + PROCESS_TERMINATION_BUDGET_SECONDS
            )
            tree_targeted = (
                kill_process_group(process_group_id)
                if process_group_id is not None
                else kill_process_tree(proc.pid)
            )
            stdout = ""
            stderr = ""

            def drain(cap: float) -> bool:
                nonlocal stdout, stderr
                budget = min(cap, max(0.0, termination_deadline - time.monotonic()))
                if budget <= 0:
                    return False
                try:
                    stdout, stderr = proc.communicate(timeout=budget)
                    return True
                except subprocess.TimeoutExpired as exc:
                    stdout = exc.output or stdout
                    stderr = exc.stderr or stderr
                    return False

            drained = drain(0.5)
            if not drained:
                try:
                    proc.kill()
                except OSError:
                    pass
                drained = drain(0.2)
            if not drained:
                # Do not close Popen pipes here. On Windows communicate() owns
                # reader threads, and BufferedReader.close() can block on their
                # internal lock until an uncontained descendant closes the pipe.
                wait_budget = min(
                    0.1,
                    max(0.0, termination_deadline - time.monotonic()),
                )
                if wait_budget > 0:
                    try:
                        proc.wait(timeout=wait_budget)
                    except (subprocess.TimeoutExpired, OSError):
                        pass
            direct_stopped = proc.poll() is not None
            tree_stopped = (
                wait_for_process_group_exit(
                    process_group_id,
                    timeout=max(0.0, termination_deadline - time.monotonic()),
                )
                if process_group_id is not None
                else tree_targeted if os.name == "nt" else False
            )
            if not drained or not direct_stopped or not tree_stopped:
                # Keep the same fail-closed fence used by foreground execution.
                # This import is local because runtime.base imports this module.
                from mclaw.runtime.base import UnresolvedOperationFence

                error = RuntimeError("Process tree could not be confirmed stopped.")
                error.pid = proc.pid
                error.process_group_id = process_group_id
                error.termination_fence = UnresolvedOperationFence()
                safe_cancel_trace(
                    lambda: logger.warning(
                        "[CANCEL_TRACE] captured_process_termination_unconfirmed "
                        "cancel_id=%s pid=%s trigger=%s persistent_fence=true",
                        get_cancel_id(cancel_event),
                        proc.pid,
                        "user_interrupt" if cancelled else "deadline",
                    )
                )
                raise error
            if proc.returncode == 0:
                safe_cancel_trace(
                    lambda: logger.info(
                        "[CANCEL_TRACE] captured_process_completion_won_stop "
                        "cancel_id=%s pid=%s trigger=%s phase=post_signal",
                        get_cancel_id(cancel_event),
                        proc.pid,
                        "user_interrupt" if cancelled else "deadline",
                    )
                )
                return subprocess.CompletedProcess(
                    argv,
                    proc.returncode,
                    stdout,
                    stderr,
                )
            if cancelled:
                raise InterruptedError("Tool operation interrupted by cancellation.")
            raise subprocess.TimeoutExpired(argv, timeout, output=stdout, stderr=stderr)

        try:
            stdout, stderr = proc.communicate(timeout=min(0.05, remaining))
            return subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)
        except subprocess.TimeoutExpired:
            continue


@dataclass(frozen=True)
class ProcessProfile:
    """Runtime spawn options shared by platform-specific process launchers."""

    start_new_session: bool = True
    stdout_pipe: bool = True
    stderr_to_stdout: bool = True
    stdin_pipe: bool = False


@dataclass
class SpawnResult:
    """Process handle metadata returned by runtime spawn implementations."""

    pid: int
    process: subprocess.Popen | None = None
    pty: Any = None
    cwd: str = ""
    env_hash: str = ""
    shell_profile: str = ""
    used_pty: bool = False
    pty_disabled_reason: str = ""


@contextmanager
def null_file_lock(path: str | Path):
    """Provide a file-backed context for runtimes without native locking."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+", encoding="utf-8") as lock_file:
        yield lock_file
