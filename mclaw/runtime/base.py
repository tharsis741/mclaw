# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Base runtime interface for command execution and host capabilities.

Runtime implementations share path policy, shell profile, process spawning,
and search behavior through this interface so tools can stay platform-neutral.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from mclaw.runtime.features import RuntimeFeatures
from mclaw.runtime.paths import PathPolicy
from mclaw.runtime.process import ProcessProfile, SpawnResult, env_hash, kill_process_tree, sanitize_subprocess_env
from mclaw.runtime.search import SearchProfile
from mclaw.runtime.shell import CWD_MARKER, ShellProfile


@dataclass(frozen=True)
class ExecResult:
    output: str
    returncode: int
    cwd: str
    runtime_kind: str
    shell_profile: str


class Runtime:
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
        env = sanitize_subprocess_env(os.environ, allowed_sensitive=allowed_sensitive)
        env["MCLAW_HOME"] = str(self.paths.mclaw_home)
        if extra:
            env.update({str(key): str(value) for key, value in extra.items()})
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
    ) -> ExecResult:
        cwd_value = cwd or os.getcwd()
        decision = self.paths.check("execute", cwd_value)
        if not decision.allowed:
            raise PermissionError(decision.error_message())
        run_env = self.build_env(env, allowed_sensitive=scoped_secret_keys)
        argv = self.shell.argv(command, decision.resolved)
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
            preexec_fn=None if os.name == "nt" else os.setsid,
            creationflags=(
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
                if os.name == "nt"
                else 0
            ),
        )
        try:
            output, _ = proc.communicate(stdin_data, timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_process_tree(proc.pid)
            output, _ = proc.communicate(timeout=5)
            return ExecResult(
                output=self._strip_cwd_marker(output or "")[0],
                returncode=124,
                cwd=str(decision.resolved),
                runtime_kind=self.kind,
                shell_profile=self.shell.name,
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
    ) -> SpawnResult:
        cwd_value = cwd or os.getcwd()
        decision = self.paths.check("execute", cwd_value)
        if not decision.allowed:
            raise PermissionError(decision.error_message())
        run_env = self.build_env(env, allowed_sensitive=scoped_secret_keys)
        run_env["PYTHONUNBUFFERED"] = "1"
        argv = self.shell.argv(command, decision.resolved)

        if use_pty:
            try:
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
            except Exception as exc:
                pty_reason = f"PTY spawn failed: {exc}; used pipe mode"
        else:
            pty_reason = ""

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
            preexec_fn=None if os.name == "nt" else os.setsid,
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
        return self.paths.check(action, path, base=base)

    def doctor(self) -> dict:
        data = {
            "kind": self.kind,
            "shell": self.shell.name,
            "shell_executable": self.shell.executable,
            "search_provider": self.search.provider,
            "mclaw_home": str(self.paths.mclaw_home),
            "workspace": str(self.paths.default_workspace()),
            "features": self.features.to_dict(),
        }
        launch_domain = getattr(self, "launch_domain", None)
        if launch_domain is not None:
            to_dict = getattr(launch_domain, "to_dict", None)
            data["launch_domain"] = to_dict() if callable(to_dict) else str(launch_domain)
        return data

    def prompt_os_label(self, os_name: str, os_release: str) -> str:
        """Return the OS label exposed to the model in the system prompt."""
        return f"{os_name} {os_release}".strip()

    @staticmethod
    def _strip_cwd_marker(output: str) -> tuple[str, str]:
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
