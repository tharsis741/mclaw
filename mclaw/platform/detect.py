# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Runtime platform detection helpers for M-Claw.

This module keeps platform facts in one place so prompts, doctor diagnostics,
and optional desktop features do not each guess Linux/Windows behavior
separately.
"""

from __future__ import annotations

import os
import platform
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PlatformInfo:
    """Snapshot of host and runtime facts exposed to diagnostics and prompts."""
    os_name: str
    os_release: str
    python_version: str
    cwd: str
    shell_path: str
    shell_name: str
    command_hint: str
    is_windows: bool
    is_linux: bool
    is_macos: bool
    is_wsl: bool
    gui_available: bool
    audio_input_available: bool
    runtime_mode: str


def is_wsl() -> bool:
    """Detect Windows Subsystem for Linux without requiring WSL-specific APIs."""
    if sys.platform != "linux":
        return False
    try:
        release = platform.release().lower()
        version = platform.version().lower()
        if "microsoft" in release or "microsoft" in version or "wsl" in release:
            return True
    except Exception:
        pass
    return bool(os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"))


def gui_available() -> bool:
    """Return whether desktop GUI features can plausibly start on this host."""
    if sys.platform == "win32" or sys.platform == "darwin":
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def audio_input_available() -> bool:
    """Return whether local audio capture is likely available for ASR."""
    if sys.platform == "win32" or sys.platform == "darwin":
        return True
    if Path("/proc/asound/cards").exists():
        try:
            text = Path("/proc/asound/cards").read_text(encoding="utf-8", errors="ignore").strip()
            return bool(text and "no soundcards" not in text.lower())
        except Exception:
            return True
    return Path("/dev/snd").exists()


def get_platform_info(config: dict | None = None) -> PlatformInfo:
    """Build platform diagnostics from host probes and the active runtime."""
    from mclaw.runtime.manager import RuntimeManager

    runtime = RuntimeManager.current(config)
    system = platform.system()
    return PlatformInfo(
        os_name=system,
        os_release=platform.release(),
        python_version=sys.version.split()[0],
        cwd=os.environ.get("TERMINAL_CWD") or os.getcwd(),
        shell_path=runtime.shell.executable,
        shell_name=f"{runtime.kind}:{runtime.shell.name}",
        command_hint=(
            f"Commands execute through {runtime.kind} using "
            f"{runtime.shell.name}; search provider={runtime.search.provider}."
        ),
        is_windows=sys.platform == "win32",
        is_linux=sys.platform.startswith("linux"),
        is_macos=sys.platform == "darwin",
        is_wsl=is_wsl(),
        gui_available=gui_available(),
        audio_input_available=audio_input_available(),
        runtime_mode=runtime.kind,
    )
