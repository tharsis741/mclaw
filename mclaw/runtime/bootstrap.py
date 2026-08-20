# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Early runtime path bootstrap.

This module intentionally avoids importing mclaw.constants or config code.  It
is used before dotenv, logging, setup, session DB, and runtime manager startup.
"""

from __future__ import annotations

import os
import platform
import sys
from pathlib import Path


_OHOS_PARAMETER_FILES = (
    Path("/etc/param/ohos.para"),
    Path("/system/etc/param/ohos.para"),
)
_KAIHONG_RUN = Path("/bin/run")
_KAIHONG_RELEASE_ROOT = Path("/data/local/release")
OHOS_IDENTITY_KEYS = (
    "const.ohos.fullname",
    "const.ohos.version",
    "const.ohos.apiversion",
    "const.product.software.version",
    "const.product.manufacturer",
    "const.product.brand",
    "const.product.name",
    "const.product.model",
    "const.product.cpu.abilist",
)


class BootstrapPathResolver:
    """Resolve process-wide MCLAW_HOME before user config is loaded."""

    @staticmethod
    def read_ohos_parameters() -> dict[str, str]:
        """Read stable OpenHarmony identity parameters without spawning commands."""
        parameters: dict[str, str] = {}
        for param_path in _OHOS_PARAMETER_FILES:
            try:
                text = param_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for raw_line in text.splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                if key in OHOS_IDENTITY_KEYS:
                    parameters.setdefault(key, value.strip().strip("\"'"))
        return parameters

    @staticmethod
    def is_kaihong_host() -> bool:
        """Detect Kaihong identity or a compatible OpenHarmony release runtime."""
        if not sys.platform.startswith("linux"):
            return False
        machine = platform.machine().lower()
        if machine not in {"aarch64", "arm64"}:
            return False

        parameters = BootstrapPathResolver.read_ohos_parameters()
        brand = parameters.get("const.product.brand", "").casefold()
        ohos_version = parameters.get("const.ohos.version", "").casefold()
        kaihong_identity = brand == "kaihong" or ohos_version.startswith("kaihongos")
        openharmony_family = any(key.startswith("const.ohos.") for key in parameters)
        if not openharmony_family:
            openharmony_family = any(path.exists() for path in _OHOS_PARAMETER_FILES)
        release_runtime = _KAIHONG_RUN.is_file() and _KAIHONG_RELEASE_ROOT.is_dir()
        return kaihong_identity or (openharmony_family and release_runtime)

    @staticmethod
    def resolve_mclaw_home() -> Path:
        """Resolve MCLAW_HOME before config, dotenv, or logging are available."""
        configured = os.environ.get("MCLAW_HOME", "").strip()
        if configured:
            return Path(configured).expanduser()
        if BootstrapPathResolver.is_kaihong_host():
            return Path("/data/local/tmp/.mclaw")
        if sys.platform == "win32":
            user_profile = os.environ.get("USERPROFILE", "").strip()
            if user_profile:
                return Path(user_profile) / ".mclaw"
        return Path.home() / ".mclaw"

    @staticmethod
    def ensure_env() -> Path:
        """Set MCLAW_HOME in-process so later imports share one path root."""
        home = BootstrapPathResolver.resolve_mclaw_home()
        os.environ["MCLAW_HOME"] = str(home)
        return home
