# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""KaihongOS/OpenHarmony device-local runtime."""

from __future__ import annotations

import importlib.util
import os
import platform as host_platform
import shutil
from pathlib import Path

from mclaw.constants import get_mclaw_home
from mclaw.dsoftbus.platform_adapter import select_openharmony_adapter
from mclaw.runtime.base import Runtime
from mclaw.runtime.bootstrap import BootstrapPathResolver
from mclaw.runtime.features import FeatureState, runtime_features
from mclaw.runtime.paths import PathPolicy
from mclaw.runtime.process import ProcessProfile
from mclaw.runtime.shell import ShellProfile


def _pet_feature() -> FeatureState:
    if importlib.util.find_spec("PySide6") is None:
        return FeatureState.AVAILABLE_WITH_INSTALL
    if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        return FeatureState.ENABLED
    return FeatureState.AVAILABLE_WITH_CONFIG


def _browser_feature() -> FeatureState:
    if importlib.util.find_spec("playwright") is None:
        return FeatureState.AVAILABLE_WITH_INSTALL
    try:
        from mclaw.tools.browser_requirements import check_browser_requirements

        return FeatureState.ENABLED if check_browser_requirements() else FeatureState.AVAILABLE_WITH_INSTALL
    except Exception:
        return FeatureState.AVAILABLE_WITH_INSTALL


_DSOFTBUS_IDENTITY_KEYS = (
    "const.ohos.version",
    "const.ohos.fullname",
    "const.product.software.version",
    "const.ohos.apiversion",
    "const.product.cpu.abilist",
)


def _dsoftbus_feature() -> tuple[FeatureState, str]:
    """Resolve a supported OpenHarmony ABI without loading Native code."""
    if host_platform.machine().strip().lower() not in {"aarch64", "arm64"}:
        return FeatureState.DISABLED, "OH_ARCH_UNSUPPORTED"

    parameters = dict(BootstrapPathResolver.read_ohos_parameters())
    missing = [key for key in _DSOFTBUS_IDENTITY_KEYS if not str(parameters.get(key) or "").strip()]
    if missing:
        executable = shutil.which("param")
        if not executable and Path("/bin/param").is_file():
            executable = "/bin/param"
        if executable:
            from mclaw.platform.detect import _read_live_parameter

            for key in missing:
                value = _read_live_parameter(executable, key)
                if value:
                    parameters[key] = value

    if any(not str(parameters.get(key) or "").strip() for key in _DSOFTBUS_IDENTITY_KEYS):
        return FeatureState.DISABLED, "OH_IDENTITY_INCOMPLETE"

    api_text = str(parameters["const.ohos.apiversion"]).strip()
    if not api_text.isdecimal():
        return FeatureState.DISABLED, "OH_IDENTITY_CONFLICT"
    adapter = select_openharmony_adapter(
        api_level=int(api_text),
        fullname=str(parameters["const.ohos.fullname"]),
        abi_values=tuple(str(parameters["const.product.cpu.abilist"]).split(",")),
        machine=host_platform.machine(),
    )
    if adapter is None:
        return FeatureState.DISABLED, "OH_RUNTIME_UNSUPPORTED"
    return FeatureState.ENABLED, "OH_DSOFTBUS_CANDIDATE"


class KaihongRuntime(Runtime):
    """Runtime profile for constrained device-local Kaihong shells."""
    kind = "kaihong"
    release_root = Path("/data/local/release")

    def __init__(self) -> None:
        home = get_mclaw_home()
        paths = PathPolicy(
            mclaw_home=home,
            protected_anchors=(
                Path("/"),
                Path("/system"),
                Path("/vendor"),
                Path("/sys_prod"),
                Path("/chip_prod"),
                Path("/data"),
                self.release_root,
                Path("/data/app"),
                Path("/data/service"),
                Path("/data/docker"),
            ),
            pseudo_roots=(Path("/proc"), Path("/sys"), Path("/dev")),
            protect_mount_points=True,
        )
        git_available = shutil.which("git") is not None
        pet_feature = _pet_feature()
        browser_feature = _browser_feature()
        dsoftbus_feature, dsoftbus_reason = _dsoftbus_feature()
        features = runtime_features(
            checkpoint=FeatureState.ENABLED if git_available else FeatureState.DISABLED,
            pet=pet_feature,
            browser_tool=browser_feature,
            dsoftbus=dsoftbus_feature,
            reasons={
                "checkpoint": "git available" if git_available else "git not found; checkpoint disabled",
                "pet": (
                    "PySide6 and a display session are available"
                    if pet_feature == FeatureState.ENABLED
                    else "PySide6 or a display session is unavailable"
                ),
                "browser_tool": (
                    "Playwright and Chromium are available"
                    if browser_feature == FeatureState.ENABLED
                    else "Playwright or Chromium is unavailable"
                ),
                "dsoftbus": dsoftbus_reason,
            },
        )
        shell = ShellProfile(name="sh", executable="/bin/sh", family="posix", args_prefix=("-c",), supports_tty=True)
        super().__init__(shell=shell, paths=paths, process=ProcessProfile(), features=features)

    def doctor(self) -> dict:
        """Report release-environment health without exposing environment values."""
        data = super().doctor()
        release = str(self.release_root)
        path_entries = [item for item in os.environ.get("PATH", "").split(":") if item]
        duplicates: dict[str, list[str]] = {}
        for name in ("PATH", "LD_LIBRARY_PATH", "LD_PRELOAD"):
            entries = [item for item in os.environ.get(name, "").split(":") if item]
            repeated = sorted({item for item in entries if entries.count(item) > 1})
            if repeated:
                duplicates[name] = repeated
        data.update(
            {
                "release_root": release,
                "release_environment_active": any(item.startswith(release) for item in path_entries),
                "environment_duplicates": duplicates,
            }
        )
        return data
