# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""OpenHarmony ABI selection for the device-local DSoftBus runtime.

The A2A protocol is shared by every supported OpenHarmony release.  This
module selects only the native closure that has to follow the platform ABI.
It is intentionally free of ``ctypes`` and native-library loading so it is
safe to use during early runtime detection and Setup.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
import re
from typing import Mapping


DEPLOYMENT_ROOT = "/data/local/release/opt/mclaw-dsoftbus"
REMOTE_SOFTBUS_LIBRARY = "/system/lib64/platformsdk/libsoftbus_client.z.so"
PERMISSION_LIBRARY = "/system/lib64/ndk/libability_access_control.so"
SYSTEM_PARAMETER_TOOL = "/bin/param"

# These are the symbols the native Shim actually calls.  Optional convenience
# APIs such as SendBytesAsync must not reject an otherwise compatible system.
REQUIRED_SOFTBUS_EXPORTS = (
    "Bind",
    "FreeNodeInfo",
    "GetAllNodeDeviceInfo",
    "GetLocalNodeDeviceInfo",
    "GetMtuSize",
    "GetNodeKeyInfo",
    "Listen",
    "RegNodeDeviceStateCb",
    "SendBytes",
    "Shutdown",
    "Socket",
    "UnregNodeDeviceStateCb",
)


@dataclass(frozen=True, slots=True)
class OpenHarmonyAdapter:
    """One explicitly supported native OpenHarmony ABI closure."""

    adapter_id: str
    api_level: int
    target_triple: str
    release_pattern: re.Pattern[str]
    shim_library: str
    system_libcxx_candidates: tuple[str, ...]
    permission_strategy: str
    socket_listener_strategy: str
    device_manager_strategy: str
    device_manager_bridge: str | None
    device_manager_libraries: Mapping[str, str]

    def accepts_release(self, fullname: str) -> bool:
        return self.release_pattern.search(fullname.strip()) is not None


def _shim(name: str) -> str:
    return str(PurePosixPath(DEPLOYMENT_ROOT) / "current" / "lib" / name)


def _device_manager_bridge(name: str) -> str:
    return str(PurePosixPath(DEPLOYMENT_ROOT) / "current" / "bin" / name)


_ADAPTERS = (
    OpenHarmonyAdapter(
        adapter_id="openharmony-api11",
        api_level=11,
        target_triple="aarch64-linux-ohos11",
        release_pattern=re.compile(r"^OpenHarmony-4\.1(?:[.\-]|$)", re.IGNORECASE),
        shim_library=_shim("libmclaw_dsoftbus_api11.so"),
        system_libcxx_candidates=(
            "/system/lib64/libc++.so",
            "/system/lib64/chipset-sdk-sp/libc++.so",
        ),
        permission_strategy="access-token-bridge",
        socket_listener_strategy="bind-validation",
        device_manager_strategy="native-bridge",
        device_manager_bridge=_device_manager_bridge(
            "mclaw_device_manager_bridge_api11"
        ),
        device_manager_libraries={
            "deviceManagerSdk": "/system/lib64/platformsdk/libdevicemanagersdk.z.so",
        },
    ),
    OpenHarmonyAdapter(
        adapter_id="openharmony-api14",
        api_level=14,
        target_triple="aarch64-linux-ohos14",
        release_pattern=re.compile(r"^OpenHarmony-5\.0\.2(?:[.\-]|$)", re.IGNORECASE),
        shim_library=_shim("libmclaw_dsoftbus_api14.so"),
        system_libcxx_candidates=(
            "/system/lib64/libc++.so",
            "/system/lib64/chipset-sdk-sp/libc++.so",
        ),
        permission_strategy="ability-access-control",
        socket_listener_strategy="negotiate",
        device_manager_strategy="native-bridge",
        device_manager_bridge=_device_manager_bridge(
            "mclaw_device_manager_bridge_api14"
        ),
        device_manager_libraries={
            "deviceManagerSdk": "/system/lib64/platformsdk/libdevicemanagersdk.z.so",
        },
    ),
    OpenHarmonyAdapter(
        adapter_id="openharmony-api23",
        api_level=23,
        target_triple="aarch64-linux-ohos23",
        release_pattern=re.compile(r"^OpenHarmony-6\.1(?:[.\-]|$)", re.IGNORECASE),
        shim_library=_shim("libmclaw_dsoftbus_oh61.so"),
        system_libcxx_candidates=(
            "/system/lib64/chipset-sdk-sp/libc++.so",
            "/system/lib64/libc++.so",
        ),
        permission_strategy="ability-access-control",
        socket_listener_strategy="negotiate",
        device_manager_strategy="native-bridge",
        device_manager_bridge=_device_manager_bridge(
            "mclaw_device_manager_bridge_api23"
        ),
        device_manager_libraries={
            "deviceManagerSdk": "/system/lib64/platformsdk/libdevicemanagersdk.z.so",
        },
    ),
)

OPENHARMONY_ADAPTERS = {adapter.api_level: adapter for adapter in _ADAPTERS}


def select_openharmony_adapter(
    *,
    api_level: int,
    fullname: str,
    abi_values: tuple[str, ...],
    machine: str,
) -> OpenHarmonyAdapter | None:
    """Return the exact ABI adapter for observed, mutually consistent facts."""

    normalized_machine = machine.strip().casefold()
    if normalized_machine == "arm64":
        normalized_machine = "aarch64"
    normalized_abis = {value.strip().casefold() for value in abi_values if value.strip()}
    if normalized_machine != "aarch64" or "arm64-v8a" not in normalized_abis:
        return None
    adapter = OPENHARMONY_ADAPTERS.get(api_level)
    if adapter is None or not adapter.accepts_release(fullname):
        return None
    return adapter


__all__ = [
    "DEPLOYMENT_ROOT",
    "OPENHARMONY_ADAPTERS",
    "OpenHarmonyAdapter",
    "PERMISSION_LIBRARY",
    "REMOTE_SOFTBUS_LIBRARY",
    "REQUIRED_SOFTBUS_EXPORTS",
    "SYSTEM_PARAMETER_TOOL",
    "select_openharmony_adapter",
]
