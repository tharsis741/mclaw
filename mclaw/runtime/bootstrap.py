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


class BootstrapPathResolver:
    """Resolve process-wide MCLAW_HOME before user config is loaded."""

    @staticmethod
    def is_kaihong_host() -> bool:
        """Detect Kaihong/OpenHarmony-style hosts using low-level markers only."""
        if not sys.platform.startswith("linux"):
            return False
        machine = platform.machine().lower()
        if machine not in {"aarch64", "arm64"}:
            return False

        score = 0
        for path in (
            "/bin/run",
            "/data/local/release",
            "/data/local/release/bin/python3",
            "/data/acs/acs/file_sharing",
        ):
            if Path(path).exists():
                score += 1

        for param_path in (
            "/etc/param/ohos.para",
            "/system/etc/param/ohos.para",
            "/data/service/el1/public/startup/parameters",
        ):
            try:
                text = Path(param_path).read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            lowered = text.lower()
            if "ohos" in lowered or "openharmony" in lowered or "kaihong" in lowered:
                score += 1
                break

        return score >= 3

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
    def default_workspace() -> Path:
        """Return the bootstrap workspace derived from the resolved home."""
        return BootstrapPathResolver.resolve_mclaw_home() / "workspace"

    @staticmethod
    def ensure_env() -> Path:
        """Set MCLAW_HOME in-process so later imports share one path root."""
        home = BootstrapPathResolver.resolve_mclaw_home()
        os.environ["MCLAW_HOME"] = str(home)
        return home
