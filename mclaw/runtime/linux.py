# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Linux and POSIX host runtime implementation.

The runtime selects shell, path, process, search, and optional feature profiles
for non-Windows hosts while keeping platform capability checks local to this
module.
"""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

from mclaw.constants import get_mclaw_home
from mclaw.runtime.base import Runtime
from mclaw.runtime.features import FeatureState, runtime_features
from mclaw.runtime.paths import PathPolicy
from mclaw.runtime.process import ProcessProfile
from mclaw.runtime.shell import ShellProfile


def _feature_from_module(module: str) -> FeatureState:
    """Map an import probe to a runtime feature availability state."""
    return FeatureState.ENABLED if importlib.util.find_spec(module) else FeatureState.AVAILABLE_WITH_INSTALL


class LinuxRuntime(Runtime):
    """Runtime profile for Linux, macOS, and other POSIX-like hosts."""
    kind = "linux"

    def __init__(self) -> None:
        home = get_mclaw_home()
        shell = self._shell_profile()
        paths = PathPolicy(
            kind=self.kind,
            mclaw_home=home,
            workspace_root=home / "workspace",
            runtime_roots=(home,),
            system_roots=(Path("/etc"), Path("/usr"), Path("/bin"), Path("/sbin"), Path("/boot")),
            device_roots=(Path("/proc"), Path("/sys"), Path("/dev")),
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
        """Prefer bash login semantics, falling back to POSIX sh."""
        executable = shutil.which("bash") or ("/usr/bin/bash" if Path("/usr/bin/bash").exists() else "") or ("/bin/bash" if Path("/bin/bash").exists() else "") or shutil.which("sh") or "/bin/sh"
        name = "bash" if Path(executable).name == "bash" else "sh"
        args = ("-lc",) if name == "bash" else ("-c",)
        return ShellProfile(name=name, executable=executable, family="posix", args_prefix=args, supports_tty=True)
