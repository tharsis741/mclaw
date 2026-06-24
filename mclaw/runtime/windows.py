"""Windows host runtime."""

from __future__ import annotations

import importlib.util
import os
import shutil
from pathlib import Path

from mclaw.constants import get_mclaw_home
from mclaw.runtime.base import Runtime
from mclaw.runtime.features import FeatureState, runtime_features
from mclaw.runtime.paths import PathPolicy
from mclaw.runtime.process import ProcessProfile
from mclaw.runtime.shell import ShellProfile


def _feature_from_module(module: str) -> FeatureState:
    return FeatureState.ENABLED if importlib.util.find_spec(module) else FeatureState.AVAILABLE_WITH_INSTALL


class WindowsRuntime(Runtime):
    kind = "windows"

    def __init__(self) -> None:
        home = get_mclaw_home()
        shell = self._shell_profile()
        paths = PathPolicy(
            kind=self.kind,
            mclaw_home=home,
            workspace_root=home / "workspace",
            runtime_roots=(home,),
            system_roots=tuple(Path(p) for p in (
                os.environ.get("SystemRoot", r"C:\Windows"),
                os.environ.get("ProgramFiles", r"C:\Program Files"),
                os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
            ) if p),
        )
        features = runtime_features(
            checkpoint=FeatureState.ENABLED if shutil.which("git") else FeatureState.DISABLED,
            pet=_feature_from_module("PySide6"),
            browser_tool=_feature_from_module("playwright"),
            reasons={
                "checkpoint": "git available" if shutil.which("git") else "git not found; checkpoint disabled",
                "pet": "PySide6 dependency probe",
                "browser_tool": "Playwright dependency probe",
            },
        )
        super().__init__(shell=shell, paths=paths, process=ProcessProfile(), features=features)

    @staticmethod
    def _shell_profile() -> ShellProfile:
        pwsh = shutil.which("pwsh") or shutil.which("powershell.exe")
        if pwsh:
            return ShellProfile(
                name="powershell",
                executable=pwsh,
                family="powershell",
                args_prefix=("-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command"),
                supports_tty=True,
            )
        comspec = os.environ.get("COMSPEC") or "cmd.exe"
        return ShellProfile(name="cmd", executable=comspec, family="cmd", args_prefix=("/d", "/s", "/c"), supports_tty=True)
