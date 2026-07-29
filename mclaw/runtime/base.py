# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Base runtime interface for command execution and host capabilities.

Runtime implementations share path policy, shell profile, process spawning,
and search behavior through this interface so tools can stay platform-neutral.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass

from mclaw.runtime.features import RuntimeFeatures
from mclaw.runtime.paths import PathPolicy
from mclaw.runtime.process import (
    ProcessProfile,
    PROCESS_TERMINATION_BUDGET_SECONDS,
    SpawnResult,
    env_hash,
    get_process_group_id,
    kill_process_group,
    kill_process_tree,
    sanitize_subprocess_env,
    wait_for_process_group_exit,
)
from mclaw.runtime.search import SearchProfile
from mclaw.runtime.shell import CWD_MARKER, ShellProfile
from mclaw.tools.interrupt import get_cancel_id, safe_cancel_trace

logger = logging.getLogger(__name__)


class UnresolvedOperationFence:
    """Persistent fence used when an external process tree may still be alive.

    The parent ``Popen`` exiting does not prove that descendants exited, so this
    fence intentionally cannot clear itself. Reusing the agent would be unsafe;
    recovery requires restarting the owning runtime after external inspection.
    """

    blocking_reason = (
        "An external process tree could not be confirmed stopped; inspect it and "
        "restart this runtime before starting another turn"
    )
    persistent = True

    def is_alive(self) -> bool:
        return True


@dataclass(frozen=True)
class ExecResult:
    """Synchronous command result normalized across runtime implementations."""
    output: str
    returncode: int
    cwd: str
    runtime_kind: str
    shell_profile: str
    termination_confirmed: bool = True
    termination_fence: UnresolvedOperationFence | None = None


class Runtime:
    """Base runtime facade for shell execution, path policy, and capabilities."""
    kind: str = "unknown"

    def __init__(
        self,
        *,
        shell: ShellProfile,
        paths: PathPolicy,
        process: ProcessProfile,
        features: RuntimeFeatures,
    ) -> None:
        self.shell = shell
        self.paths = paths
        self.process = process
        self.features = features
        self.search = SearchProfile(self)

    def build_env(
        self,
        extra: dict | None = None,
        *,
        allowed_sensitive: set[str] | None = None,
    ) -> dict[str, str]:
        """Build a subprocess environment with only authorized secrets retained."""
        env = sanitize_subprocess_env(os.environ, allowed_sensitive=allowed_sensitive)
        env["MCLAW_HOME"] = str(self.paths.mclaw_home)
        if extra:
            env.update(sanitize_subprocess_env(extra, allowed_sensitive=allowed_sensitive))
        return env

    def exec(
        self,
        command: str,
        *,
        cwd: str | os.PathLike | None = None,
        timeout: int | None = None,
        stdin_data: str | None = None,
        env: dict | None = None,
        scoped_secret_keys: set[str] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> ExecResult:
        """Run a foreground command under path policy and return captured output."""
        cwd_value = cwd or os.getcwd()
        decision = self.paths.check("execute", cwd_value)
        if not decision.allowed:
            raise PermissionError(decision.error_message())
        run_env = self.build_env(env, allowed_sensitive=scoped_secret_keys)
        argv = self.shell.argv(command, decision.resolved)
        if cancel_event is not None and cancel_event.is_set():
            safe_cancel_trace(
                lambda: logger.info(
                    "[CANCEL_TRACE] process_cancel_before_spawn cancel_id=%s "
                    "operation=foreground_exec",
                    get_cancel_id(cancel_event),
                )
            )
            return ExecResult(
                output="",
                returncode=130,
                cwd=str(decision.resolved),
                runtime_kind=self.kind,
                shell_profile=self.shell.name,
            )
        proc = subprocess.Popen(
            argv,
            text=True,
            cwd=str(decision.resolved),
            env=run_env,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=os.name != "nt",
            creationflags=(
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
                if os.name == "nt"
                else 0
            ),
        )
        process_group_id = get_process_group_id(proc.pid)
        stop_code: int | None = None
        output = ""
        try:
            if cancel_event is None:
                output, _ = proc.communicate(stdin_data, timeout=timeout)
            else:
                deadline = None if timeout is None else time.monotonic() + timeout
                pending_input = stdin_data
                while True:
                    # A command that became terminal after the previous poll owns
                    # the result; a later cancellation must not mask its effects.
                    if proc.poll() is not None:
                        try:
                            output, _ = proc.communicate(timeout=0.05)
                        except subprocess.TimeoutExpired:
                            # A descendant may still own an inherited output pipe;
                            # keep the normal cancel/deadline path bounded.
                            pass
                        else:
                            if cancel_event.is_set():
                                safe_cancel_trace(
                                    lambda: logger.info(
                                        "[CANCEL_TRACE] process_completion_won_cancel "
                                        "cancel_id=%s operation=foreground_exec pid=%s",
                                        get_cancel_id(cancel_event),
                                        proc.pid,
                                    )
                                )
                            break
                    if cancel_event.is_set():
                        if proc.poll() is not None:
                            try:
                                output, _ = proc.communicate(timeout=0.05)
                                break
                            except subprocess.TimeoutExpired:
                                pass
                        stop_code = 130
                        break
                    remaining = None if deadline is None else deadline - time.monotonic()
                    if remaining is not None and remaining <= 0:
                        stop_code = 124
                        break
                    try:
                        output, _ = proc.communicate(
                            pending_input,
                            timeout=min(0.05, remaining) if remaining is not None else 0.05,
                        )
                        break
                    except subprocess.TimeoutExpired:
                        pending_input = None
        except subprocess.TimeoutExpired:
            stop_code = 124

        if stop_code is not None:
            cancel_id = (
                get_cancel_id(cancel_event)
                if cancel_event is not None
                else "none"
            )
            trigger = "user_interrupt" if stop_code == 130 else "deadline"
            termination_started = time.monotonic()
            termination_deadline = (
                termination_started + PROCESS_TERMINATION_BUDGET_SECONDS
            )
            if os.name != "nt" and process_group_id is None:
                process_group_id = get_process_group_id(proc.pid)
            tree_targeted = (
                kill_process_group(process_group_id)
                if process_group_id is not None
                else kill_process_tree(proc.pid)
            )
            safe_cancel_trace(
                lambda: logger.warning(
                    "[CANCEL_TRACE] termination_start cancel_id=%s "
                    "operation=foreground_exec pid=%s trigger=%s",
                    cancel_id,
                    proc.pid,
                    trigger,
                )
            )
            def drain(cap: float) -> bool:
                nonlocal output
                budget = min(cap, max(0.0, termination_deadline - time.monotonic()))
                if budget <= 0:
                    return False
                try:
                    output, _ = proc.communicate(timeout=budget)
                    return True
                except subprocess.TimeoutExpired as exc:
                    partial = exc.output
                    if partial:
                        output = (
                            partial.decode("utf-8", errors="replace")
                            if isinstance(partial, bytes)
                            else str(partial)
                        )
                    return False

            drained = drain(0.5)
            if not drained:
                try:
                    proc.kill()
                except OSError:
                    pass
                drained = drain(0.2)
            if not drained:
                # Closing a Windows Popen pipe can wait indefinitely for the
                # communicate() reader thread. Keep the stop budget hard and
                # fail closed below when output never drains.
                wait_budget = min(
                    0.1,
                    max(0.0, termination_deadline - time.monotonic()),
                )
                if wait_budget > 0:
                    try:
                        proc.wait(timeout=wait_budget)
                    except (subprocess.TimeoutExpired, OSError):
                        pass
            direct_exit_confirmed = proc.poll() is not None
            tree_confirmed = (
                wait_for_process_group_exit(
                    process_group_id,
                    timeout=max(0.0, termination_deadline - time.monotonic()),
                )
                if process_group_id is not None
                else tree_targeted if os.name == "nt" else False
            )
            termination_confirmed = (
                drained and tree_confirmed and direct_exit_confirmed
            )
            log = logger.info if termination_confirmed else logger.warning
            safe_cancel_trace(
                lambda: log(
                    "[CANCEL_TRACE] termination_result cancel_id=%s "
                    "operation=foreground_exec pid=%s trigger=%s tree_targeted=%s "
                    "tree_confirmed=%s "
                    "direct_exit_confirmed=%s termination_confirmed=%s "
                    "persistent_fence=%s elapsed_ms=%d",
                    cancel_id,
                    proc.pid,
                    trigger,
                    tree_targeted,
                    tree_confirmed,
                    direct_exit_confirmed,
                    termination_confirmed,
                    not termination_confirmed,
                    int((time.monotonic() - termination_started) * 1000),
                )
            )
            if termination_confirmed and proc.returncode == 0:
                safe_cancel_trace(
                    lambda: logger.info(
                        "[CANCEL_TRACE] process_completion_won_stop cancel_id=%s "
                        "operation=foreground_exec pid=%s trigger=%s phase=post_signal",
                        cancel_id,
                        proc.pid,
                        trigger,
                    )
                )
                cleaned, latest_cwd = self._strip_cwd_marker(output or "")
                return ExecResult(
                    output=cleaned,
                    returncode=0,
                    cwd=latest_cwd or str(decision.resolved),
                    runtime_kind=self.kind,
                    shell_profile=self.shell.name,
                )
            return ExecResult(
                output=self._strip_cwd_marker(output or "")[0],
                returncode=stop_code,
                cwd=str(decision.resolved),
                runtime_kind=self.kind,
                shell_profile=self.shell.name,
                termination_confirmed=termination_confirmed,
                termination_fence=(
                    None if termination_confirmed else UnresolvedOperationFence()
                ),
            )
        cleaned, latest_cwd = self._strip_cwd_marker(output or "")
        return ExecResult(
            output=cleaned,
            returncode=int(proc.returncode or 0),
            cwd=latest_cwd or str(decision.resolved),
            runtime_kind=self.kind,
            shell_profile=self.shell.name,
        )

    def spawn(
        self,
        command: str,
        *,
        cwd: str | os.PathLike | None = None,
        env: dict | None = None,
        use_pty: bool = False,
        scoped_secret_keys: set[str] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> SpawnResult:
        """Start a background command with PTY fallback and sanitized env state."""
        cwd_value = cwd or os.getcwd()
        decision = self.paths.check("execute", cwd_value)
        if not decision.allowed:
            raise PermissionError(decision.error_message())
        run_env = self.build_env(env, allowed_sensitive=scoped_secret_keys)
        run_env["PYTHONUNBUFFERED"] = "1"
        argv = self.shell.argv(command, decision.resolved)

        if use_pty:
            try:
                if cancel_event is not None and cancel_event.is_set():
                    raise InterruptedError("Background process start was cancelled")
                if os.name == "nt":
                    from winpty import PtyProcess as PtyProcessCls
                else:
                    from ptyprocess import PtyProcess as PtyProcessCls
                pty_proc = PtyProcessCls.spawn(
                    argv,
                    cwd=str(decision.resolved),
                    env=run_env,
                    dimensions=(30, 120),
                )
                return SpawnResult(
                    pid=pty_proc.pid,
                    pty=pty_proc,
                    cwd=str(decision.resolved),
                    env_hash=env_hash(run_env),
                    shell_profile=self.shell.name,
                    used_pty=True,
                )
            except ImportError:
                pty_reason = "PTY dependency is not installed; used pipe mode"
            except InterruptedError:
                raise
            except Exception as exc:
                pty_reason = f"PTY spawn failed: {exc}; used pipe mode"
        else:
            pty_reason = ""

        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError("Background process start was cancelled")
        proc = subprocess.Popen(
            argv,
            text=True,
            cwd=str(decision.resolved),
            env=run_env,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.PIPE if self.process.stdin_pipe else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=os.name != "nt",
            creationflags=(
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
                if os.name == "nt"
                else 0
            ),
        )
        return SpawnResult(
            pid=proc.pid,
            process=proc,
            cwd=str(decision.resolved),
            env_hash=env_hash(run_env),
            shell_profile=self.shell.name,
            used_pty=False,
            pty_disabled_reason=pty_reason,
        )

    def resolve_path(
        self,
        path: str | os.PathLike,
        *,
        base: str | os.PathLike | None = None,
        action: str = "read",
    ):
        """Apply runtime path policy to a caller-supplied path."""
        return self.paths.check(action, path, base=base)

    def doctor(self) -> dict:
        """Return machine-readable runtime diagnostics for mclaw doctor."""
        data = {
            "kind": self.kind,
            "shell": self.shell.name,
            "shell_executable": self.shell.executable,
            "search_provider": self.search.provider,
            "mclaw_home": str(self.paths.mclaw_home),
            "filesystem_access": "full",
            "credential_files": "protected",
            "features": self.features.to_dict(),
        }
        return data

    @staticmethod
    def _strip_cwd_marker(output: str) -> tuple[str, str]:
        """Remove shell-injected cwd marker while preserving command output."""
        latest = ""
        kept: list[str] = []
        for line in output.splitlines():
            if line.startswith(CWD_MARKER):
                latest = line[len(CWD_MARKER):].strip()
                continue
            kept.append(line)
        text = "\n".join(kept)
        if output.endswith("\n") and text:
            text += "\n"
        return text, latest
