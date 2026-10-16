# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Process liveness and birth times for persisted ownership records."""

from __future__ import annotations

import math
import os
import subprocess
from datetime import datetime
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def _windows_api():
    import ctypes
    from ctypes import wintypes

    api = ctypes.WinDLL("kernel32", use_last_error=True)
    # HANDLE is pointer-sized; ctypes' default int return truncates it on Win64.
    api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.OpenProcess.restype = wintypes.HANDLE
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    api.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    api.GetExitCodeProcess.restype = wintypes.BOOL
    api.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    api.GetProcessTimes.restype = wintypes.BOOL
    return api


def is_process_alive(pid: int) -> bool:
    """Keep inaccessible processes fenced, but distinguish exited Windows handles."""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        api = _windows_api()
        process = api.OpenProcess(0x1000, False, pid)
        if not process:
            return ctypes.get_last_error() != 87  # ERROR_INVALID_PARAMETER: no such PID
        try:
            code = wintypes.DWORD()
            return not api.GetExitCodeProcess(process, ctypes.byref(code)) or code.value == 259
        finally:
            api.CloseHandle(process)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True


def _windows_cim_started_at(pid: int) -> float | None:
    """Read protected processes through CIM when a process handle is denied."""
    command = (
        f"$record = Get-CimInstance Win32_Process -Filter 'ProcessId = {int(pid)}' "
        "-ErrorAction Stop; if ($record.CreationDate) { "
        "([DateTimeOffset]$record.CreationDate).ToUnixTimeMilliseconds() }"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=3, check=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return int(result.stdout.strip()) / 1000
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _proc_started_at(pid: int, root: Path = Path("/proc")) -> float:
    # comm can contain spaces and ')'; fields after its closing ')' start at field 3.
    fields = (root / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
    ticks = int(fields[19])
    boot = next(int(line.split()[1]) for line in (root / "stat").read_text().splitlines()
                if line.startswith("btime "))
    return boot + ticks / os.sysconf("SC_CLK_TCK")


def process_started_at(pid: int) -> float | None:
    """Return a process birth time in UTC seconds, or None if unavailable."""
    if pid <= 0:
        return None
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            api = _windows_api()
            process = api.OpenProcess(0x1000, False, pid)
            if not process:
                return _windows_cim_started_at(pid) if ctypes.get_last_error() == 5 else None
            try:
                times = [wintypes.FILETIME() for _ in range(4)]
                if not api.GetProcessTimes(process, *(ctypes.byref(value) for value in times)):
                    return None
                ticks = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
                return (ticks - 116444736000000000) / 10_000_000
            finally:
                api.CloseHandle(process)
        return _proc_started_at(pid)
    except (OSError, ValueError, IndexError, StopIteration, AttributeError):
        return None


def process_matches_record(pid: int, started_at=None, *, recorded_at=None) -> bool:
    """Reject a reused PID only when its birth time proves ownership changed.

    Older locks have only second-resolution creation timestamps. Their owner
    must predate publication. Unknown birth times retain the existing fence.
    """
    actual = process_started_at(pid)
    if actual is None:
        return True
    if isinstance(started_at, (int, float)) and not isinstance(started_at, bool) and math.isfinite(started_at):
        # CIM exposes milliseconds, while the normal Windows probe is finer.
        return abs(actual - started_at) < 0.001
    if isinstance(recorded_at, str):
        try:
            recorded = datetime.fromisoformat(recorded_at.replace("Z", "+00:00"))
            if recorded.tzinfo is not None:
                return actual < recorded.timestamp() + 1
        except ValueError:
            pass
    return True
