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
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from mclaw.runtime.bootstrap import BootstrapPathResolver, OHOS_IDENTITY_KEYS


@dataclass(frozen=True)
class PlatformInfo:
    """Snapshot of host and runtime facts exposed to diagnostics and prompts."""
    os_name: str
    os_release: str
    architecture: str
    python_version: str
    python_executable: str
    cwd: str
    shell_path: str
    shell_name: str
    shell_implementation: str
    kaihong_version: str
    base_system: str
    distribution: str
    device_name: str
    device_model: str
    is_windows: bool
    is_linux: bool
    is_macos: bool
    is_wsl: bool
    gui_available: bool
    audio_input_available: bool
    runtime_mode: str

    def os_label(self) -> str:
        """Return a factual, human-readable OS identity."""
        if self.runtime_mode != "kaihong":
            return f"{self.os_name} {self.os_release}".strip()
        primary = self.kaihong_version or self.distribution or self.base_system
        if not primary:
            return f"{self.os_name} {self.os_release}".strip()
        if self.base_system and self.base_system.casefold() != primary.casefold():
            return f"{primary}, based on {self.base_system}"
        return primary

    def distinct_distribution(self) -> str:
        """Return the product distribution only when it adds information."""
        if not self.distribution:
            return ""
        if self.kaihong_version and self.distribution.casefold() == self.kaihong_version.casefold():
            return ""
        return self.distribution

    def device_label(self) -> str:
        """Return the product name and model without inventing missing values."""
        if self.device_name and self.device_model and self.device_model.casefold() != self.device_name.casefold():
            return f"{self.device_name} ({self.device_model})"
        return self.device_name or self.device_model

    def shell_label(self) -> str:
        """Return the real shell executable plus its probed implementation."""
        if self.shell_implementation:
            return f"{self.shell_path} ({self.shell_implementation})"
        return self.shell_path


def _valid_parameter_value(value: str) -> str:
    value = str(value or "").strip()
    lowered = value.casefold()
    if not value or lowered.startswith("get parameter ") or "fail! errnum" in lowered:
        return ""
    return value


def _read_live_parameter(executable: str, key: str) -> str:
    env = os.environ.copy()
    env.pop("LD_LIBRARY_PATH", None)
    env.pop("LD_PRELOAD", None)
    try:
        result = subprocess.run(
            [executable, "get", key],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=1.5,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    return _valid_parameter_value(result.stdout)


def _shell_implementation(shell_path: str) -> str:
    try:
        result = subprocess.run(
            [shell_path, "-c", "printf '%s' \"$KSH_VERSION\""],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=1.5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    raw = result.stdout.strip()
    match = re.search(r"MIRBSD\s+KSH\s+(R[0-9A-Za-z._-]+)", raw, re.IGNORECASE)
    return f"MirBSD mksh {match.group(1)}" if match else raw


@lru_cache(maxsize=4)
def _probe_kaihong_facts(shell_path: str) -> dict[str, str]:
    """Probe stable Kaihong/OpenHarmony facts once per process."""
    parameters = BootstrapPathResolver.read_ohos_parameters()
    executable = shutil.which("param")
    if not executable and Path("/bin/param").is_file():
        executable = "/bin/param"
    if executable:
        for key in OHOS_IDENTITY_KEYS:
            if not _valid_parameter_value(parameters.get(key, "")):
                value = _read_live_parameter(executable, key)
                if value:
                    parameters[key] = value
    parameters["shell_implementation"] = _shell_implementation(shell_path)
    return parameters


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
    facts = _probe_kaihong_facts(runtime.shell.executable) if runtime.kind == "kaihong" else {}
    return PlatformInfo(
        os_name=system,
        os_release=platform.release(),
        architecture=platform.machine(),
        python_version=sys.version.split()[0],
        python_executable=sys.executable,
        cwd=os.environ.get("TERMINAL_CWD") or os.getcwd(),
        shell_path=runtime.shell.executable,
        shell_name=runtime.shell.name,
        shell_implementation=facts.get("shell_implementation", ""),
        kaihong_version=_valid_parameter_value(facts.get("const.ohos.version", "")),
        base_system=_valid_parameter_value(facts.get("const.ohos.fullname", "")),
        distribution=_valid_parameter_value(facts.get("const.product.software.version", "")),
        device_name=_valid_parameter_value(facts.get("const.product.name", "")),
        device_model=_valid_parameter_value(facts.get("const.product.model", "")),
        is_windows=sys.platform == "win32",
        is_linux=sys.platform.startswith("linux"),
        is_macos=sys.platform == "darwin",
        is_wsl=is_wsl(),
        gui_available=gui_available(),
        audio_input_available=audio_input_available(),
        runtime_mode=runtime.kind,
    )
