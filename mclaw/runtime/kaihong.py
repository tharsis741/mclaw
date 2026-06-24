"""KaihongOS/OpenHarmony device-local runtime."""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from mclaw.constants import get_mclaw_home
from mclaw.runtime.base import Runtime
from mclaw.runtime.features import FeatureState, runtime_features
from mclaw.runtime.paths import PathPolicy
from mclaw.runtime.process import ProcessProfile, sanitize_subprocess_env
from mclaw.runtime.shell import ShellProfile


@dataclass(frozen=True)
class LaunchDomain:
    name: str
    stdin_tty: bool
    stdout_tty: bool
    terminal_columns: int
    terminal_rows: int
    uid: int
    selinux_context: str

    def to_dict(self) -> dict[str, str | int | bool]:
        return {
            "name": self.name,
            "stdin_tty": self.stdin_tty,
            "stdout_tty": self.stdout_tty,
            "terminal_columns": self.terminal_columns,
            "terminal_rows": self.terminal_rows,
            "uid": self.uid,
            "selinux_context": self.selinux_context,
        }


class KaihongRuntime(Runtime):
    kind = "kaihong"
    release_root = Path("/data/local/release")

    def __init__(self) -> None:
        home = get_mclaw_home()
        paths = PathPolicy(
            kind=self.kind,
            mclaw_home=home,
            workspace_root=home / "workspace",
            exchange_roots=(
                Path("/data/acs/acs/file_sharing/download"),
                Path("/data/acs/acs/file_sharing/documents"),
                Path("/data/acs/acs/file_sharing/desktop"),
                Path("/mnt/data/external/data-1"),
            ),
            workspace_roots=(Path("/data/local/tmp/mclaw_src"),),
            workspace_prefixes=("/data/local/tmp/M-Claw",),
            runtime_roots=(self.release_root, Path("/bin"), Path("/usr"), Path("/proc/self")),
            system_roots=(Path("/"), Path("/system"), Path("/vendor"), Path("/sys_prod"), Path("/chip_prod")),
            device_roots=(Path("/proc"), Path("/sys"), Path("/dev"), Path("/data/docker"), Path("/data/app"), Path("/data/service")),
            tmp_root=Path("/data/local/tmp"),
        )
        features = runtime_features(
            checkpoint=FeatureState.DISABLED,
            pet=FeatureState.DISABLED,
            browser_tool=FeatureState.DISABLED,
            reasons={
                "checkpoint": "KaihongRuntime disables checkpoint/git provider",
                "pet": "KaihongRuntime has no PySide6/GUI provider in this runtime domain",
                "browser_tool": "KaihongRuntime does not expose browser automation",
            },
        )
        shell = ShellProfile(name="kaihong-sh", executable="/bin/sh", family="posix", args_prefix=("-c",), supports_tty=True)
        super().__init__(shell=shell, paths=paths, process=ProcessProfile(), features=features)
        self.launch_domain = self.probe_launch_domain()

    def build_env(
        self,
        extra: dict | None = None,
        *,
        allowed_sensitive: set[str] | None = None,
    ) -> dict[str, str]:
        env = sanitize_subprocess_env(os.environ, allowed_sensitive=allowed_sensitive)
        release = str(self.release_root)
        env.update(
            {
                "MCLAW_HOME": str(self.paths.mclaw_home),
                "RELEASE_ROOT": release,
                "PATH": ":".join(
                    [
                        "/usr/local/bin",
                        "/bin",
                        "/usr/bin",
                        f"{release}/bin",
                        f"{release}/sbin",
                        f"{release}/usr/bin",
                        f"{release}/usr/sbin",
                    ]
                ),
                "LD_LIBRARY_PATH": ":".join(
                    [
                        f"{release}/mesa-panfork/usr/lib",
                        f"{release}/lib",
                        f"{release}/lib64",
                        f"{release}/usr/lib",
                        f"{release}/usr/lib64",
                        f"{release}/usr/lib/aarch64-linux-ohos",
                    ]
                ),
                "LD_PRELOAD": f"{release}/usr/lib/libGLEW.so:{release}/usr/lib/libpython3.12.so.1.0",
                "PYTHONHOME": f"{release}/usr",
                "PYTHONPATH": f"{release}/usr/lib/python3.12/site-packages",
                "TMPDIR": "/data/local/tmp",
                "TERM": "xterm-256color",
            }
        )
        if extra:
            env.update({str(key): str(value) for key, value in extra.items()})
        return env

    def prompt_os_label(self, os_name: str, os_release: str) -> str:
        return f"Kaihong/OpenHarmony (Linux kernel {os_release})"

    @staticmethod
    def probe_launch_domain() -> LaunchDomain:
        stdin_tty = bool(sys.stdin and sys.stdin.isatty())
        stdout_tty = bool(sys.stdout and sys.stdout.isatty())
        size = shutil.get_terminal_size(fallback=(0, 0))
        try:
            uid = os.getuid()
        except AttributeError:
            uid = -1
        try:
            context = (
                Path("/proc/self/attr/current")
                .read_text(encoding="utf-8", errors="ignore")
                .replace("\x00", "")
                .strip()
            )
        except OSError:
            context = ""

        if "khttyd" in context:
            name = "direct_ttyd_shell"
        elif "u:r:su:s0" in context and (stdin_tty or stdout_tty):
            name = "local_hdc_shell"
        elif "u:r:su:s0" in context:
            name = "external_hdc_probe"
        else:
            name = "headless_service" if not (stdin_tty or stdout_tty) else "device_tty_shell"

        return LaunchDomain(
            name=name,
            stdin_tty=stdin_tty,
            stdout_tty=stdout_tty,
            terminal_columns=int(size.columns),
            terminal_rows=int(size.lines),
            uid=uid,
            selinux_context=context,
        )
