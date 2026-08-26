# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from mclaw.dsoftbus.platform_adapter import (
    OPENHARMONY_ADAPTERS,
    REQUIRED_SOFTBUS_EXPORTS,
    select_openharmony_adapter,
)


@pytest.mark.parametrize(
    ("api_level", "fullname", "adapter_id"),
    [
        (11, "OpenHarmony-4.1-Release", "openharmony-api11"),
        (14, "OpenHarmony-5.0.2.123", "openharmony-api14"),
        (23, "OpenHarmony-6.1.0.31", "openharmony-api23"),
    ],
)
def test_supported_openharmony_abis_select_exact_adapter(
    api_level: int,
    fullname: str,
    adapter_id: str,
) -> None:
    adapter = select_openharmony_adapter(
        api_level=api_level,
        fullname=fullname,
        abi_values=("arm64-v8a",),
        machine="aarch64",
    )

    assert adapter is not None
    assert adapter.adapter_id == adapter_id
    assert adapter.target_triple.endswith(str(api_level))


@pytest.mark.parametrize(
    ("api_level", "fullname", "abi_values", "machine"),
    [
        (14, "OpenHarmony-5.0.1.123", ("arm64-v8a",), "aarch64"),
        (14, "OpenHarmony-5.0.2.123", ("armeabi-v7a",), "aarch64"),
        (14, "OpenHarmony-5.0.2.123", ("arm64-v8a",), "x86_64"),
        (13, "OpenHarmony-5.0.1.123", ("arm64-v8a",), "aarch64"),
        (23, "OpenHarmony-6.10.0.1", ("arm64-v8a",), "aarch64"),
    ],
)
def test_adapter_selection_fails_closed_on_mismatched_platform_facts(
    api_level: int,
    fullname: str,
    abi_values: tuple[str, ...],
    machine: str,
) -> None:
    assert (
        select_openharmony_adapter(
            api_level=api_level,
            fullname=fullname,
            abi_values=abi_values,
            machine=machine,
        )
        is None
    )


def test_all_adapters_share_the_actual_softbus_symbol_contract() -> None:
    assert tuple(OPENHARMONY_ADAPTERS) == (11, 14, 23)
    assert REQUIRED_SOFTBUS_EXPORTS == (
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


def test_adapters_use_separate_system_abi_device_manager_bridges() -> None:
    api11 = OPENHARMONY_ADAPTERS[11]
    api14 = OPENHARMONY_ADAPTERS[14]
    api23 = OPENHARMONY_ADAPTERS[23]

    assert api11.device_manager_strategy == "native-bridge"
    assert api11.device_manager_bridge == (
        "/data/local/release/opt/mclaw-dsoftbus/current/bin/"
        "mclaw_device_manager_bridge_api11"
    )
    assert api14.device_manager_strategy == "native-bridge"
    assert api14.device_manager_bridge == (
        "/data/local/release/opt/mclaw-dsoftbus/current/bin/"
        "mclaw_device_manager_bridge_api14"
    )
    assert api23.device_manager_strategy == "native-bridge"
    assert api23.device_manager_bridge == (
        "/data/local/release/opt/mclaw-dsoftbus/current/bin/"
        "mclaw_device_manager_bridge_api23"
    )
    expected_libraries = {
        "deviceManagerSdk": "/system/lib64/platformsdk/libdevicemanagersdk.z.so"
    }
    assert api11.device_manager_libraries == expected_libraries
    assert api14.device_manager_libraries == expected_libraries
    assert api23.device_manager_libraries == expected_libraries
