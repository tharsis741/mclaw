# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from prompt_toolkit.document import Document

from mclaw.cli.slash_completer import SlashCompleter
from mclaw.cli.tui.renderers.commands import CommandsRenderer
from mclaw.dsoftbus.device_management import (
    DeviceManagementCoordinator,
    DeviceManagementError,
    DeviceManagementResult,
    ManagedDevice,
    TrustedDevice,
)


DEVICE_ID = f"urn:mclaw:device:oh:{'a' * 64}"
DEVICE_ID_SHA256 = "d" * 64
PAIRABLE_ID_SHA256 = "e" * 64


def _peer(**updates):
    value = {
        "agentAvailability": "READY",
        "connectionState": "OPEN",
        "deviceId": DEVICE_ID,
        "deviceName": "Kaihong A",
        "devicePresence": "ONLINE",
        "networkId": "must-not-cross-device-management-boundary",
    }
    value.update(updates)
    return value


def _trusted(**updates):
    value = {
        "deviceIdSha256": DEVICE_ID_SHA256,
        "deviceName": "Kaihong A",
        "deviceTypeId": 533,
        "online": True,
        "publicDeviceId": DEVICE_ID,
    }
    value.update(updates)
    return value


def _pairable(**updates):
    value = {
        "deviceIdSha256": PAIRABLE_ID_SHA256,
        "deviceName": "Kaihong B",
        "deviceTypeId": 533,
    }
    value.update(updates)
    return value


def _runtime(
    list_peers,
    *,
    trusted_devices=None,
    discover_devices=None,
    pair_device=None,
    unbind_device=None,
):
    return SimpleNamespace(
        health=lambda: {"state": "READY"},
        list_peers=list_peers,
        list_trusted_devices=(
            (lambda: [_trusted()])
            if trusted_devices is None
            else trusted_devices
        ),
        discover_devices=(
            (lambda: [_pairable()])
            if discover_devices is None
            else discover_devices
        ),
        pair_device=(
            (
                lambda digest: {
                    "bound": True,
                    "deviceIdSha256": digest,
                    "nativeCode": 0,
                    "status": "bound",
                }
            )
            if pair_device is None
            else pair_device
        ),
        unbind_device=(
            (
                lambda digest: {
                    "deviceIdSha256": digest,
                    "publicDeviceId": DEVICE_ID,
                    "unbound": True,
                }
            )
            if unbind_device is None
            else unbind_device
        ),
    )


def test_devices_projects_runtime_trust_and_redacted_pairable_candidates() -> None:
    runtime = _runtime(lambda *, ready_only=False: [_peer()])
    coordinator = DeviceManagementCoordinator(runtime)

    result = coordinator.execute("devices")
    public = result.public_dict()

    assert public == {
        "action": "devices",
        "devices": [
            {
                "agentAvailability": "READY",
                "connectionState": "OPEN",
                "deviceId": DEVICE_ID,
                "deviceName": "Kaihong A",
                "devicePresence": "ONLINE",
            }
        ],
        "pairableDevices": [_pairable()],
        "selectedDeviceIdSha256": "",
        "status": "listed",
        "systemUiOpened": False,
        "trustedDevices": [_trusted()],
    }
    assert "networkId" not in json.dumps(public)


def test_pair_and_unpair_select_confirm_and_mutate_exact_targets_once() -> None:
    paired: list[str] = []
    unbound: list[str] = []
    prompts: list[str] = []

    def no_peer_read(*, ready_only=False):
        pytest.fail("pair/unpair must not depend on the Runtime Peer cache")

    coordinator = DeviceManagementCoordinator(
        _runtime(
            no_peer_read,
            pair_device=lambda digest: (
                paired.append(digest)
                or {
                    "bound": True,
                    "deviceIdSha256": digest,
                    "nativeCode": 0,
                    "status": "bound",
                }
            ),
            unbind_device=lambda digest: (
                unbound.append(digest)
                or {
                    "deviceIdSha256": digest,
                    "publicDeviceId": DEVICE_ID,
                    "unbound": True,
                }
            ),
        ),
        device_selector=lambda devices: devices[0].device_id_sha256,
        pair_device_selector=lambda devices: devices[0].device_id_sha256,
        pair_confirmation=lambda device: device.device_id_sha256
        == PAIRABLE_ID_SHA256,
        unpair_confirmation=lambda device: device.device_id_sha256
        == DEVICE_ID_SHA256,
        terminal_prompt_runner=lambda prompt: (
            prompts.append("prompt") or prompt()
        ),
    )

    pair = coordinator.execute("pair")
    unpair = coordinator.execute("unpair")
    assert pair.status == "bound"
    assert pair.system_ui_opened is True
    assert unpair.status == "unbound"
    assert unpair.system_ui_opened is False
    assert paired == [PAIRABLE_ID_SHA256]
    assert unbound == [DEVICE_ID_SHA256]
    assert prompts == ["prompt", "prompt", "prompt", "prompt"]


@pytest.mark.parametrize("cancel_at", ["selection", "confirmation"])
def test_pair_cancellation_never_starts_system_bind(cancel_at: str) -> None:
    paired: list[str] = []
    coordinator = DeviceManagementCoordinator(
        _runtime(
            lambda *, ready_only=False: [],
            pair_device=lambda digest: paired.append(digest),
        ),
        pair_device_selector=(
            (lambda _devices: None)
            if cancel_at == "selection"
            else (lambda devices: devices[0].device_id_sha256)
        ),
        pair_confirmation=lambda _device: False,
    )

    result = coordinator.execute("pair")
    assert result.status == "cancelled"
    assert result.system_ui_opened is False
    assert paired == []


def test_pair_rejects_selector_result_outside_current_discovery_snapshot() -> None:
    coordinator = DeviceManagementCoordinator(
        _runtime(lambda *, ready_only=False: []),
        pair_device_selector=lambda _devices: "f" * 64,
        pair_confirmation=lambda _device: pytest.fail(
            "invalid target must fail before confirmation"
        ),
    )
    with pytest.raises(DeviceManagementError) as raised:
        coordinator.execute("pair")
    assert raised.value.code == "INVALID_PARAMS"


@pytest.mark.parametrize("cancel_at", ["selection", "confirmation"])
def test_unpair_cancellation_never_mutates_system_trust(cancel_at: str) -> None:
    unbound: list[str] = []
    coordinator = DeviceManagementCoordinator(
        _runtime(
            lambda *, ready_only=False: [],
            unbind_device=lambda digest: unbound.append(digest),
        ),
        device_selector=(
            (lambda _devices: None)
            if cancel_at == "selection"
            else (lambda devices: devices[0].device_id_sha256)
        ),
        unpair_confirmation=lambda _device: False,
    )

    result = coordinator.execute("unpair")
    assert result.status == "cancelled"
    assert result.system_ui_opened is False
    assert unbound == []


def test_unpair_rejects_a_selector_result_outside_the_enumerated_set() -> None:
    coordinator = DeviceManagementCoordinator(
        _runtime(lambda *, ready_only=False: []),
        device_selector=lambda _devices: "e" * 64,
        unpair_confirmation=lambda _device: pytest.fail(
            "invalid target must fail before confirmation"
        ),
    )
    with pytest.raises(DeviceManagementError) as raised:
        coordinator.execute("unpair")
    assert raised.value.code == "INVALID_PARAMS"


@pytest.mark.parametrize("state", ["NEW", "STARTING", "STOPPING", "STOPPED"])
def test_device_commands_require_an_active_runtime(state: str) -> None:
    coordinator = DeviceManagementCoordinator(
        SimpleNamespace(
            health=lambda: {"state": state},
            list_peers=lambda *, ready_only=False: pytest.fail(
                "inactive Runtime must not expose Peer state"
            ),
        ),
    )

    with pytest.raises(DeviceManagementError) as raised:
        coordinator.execute("pair")
    assert raised.value.code == "DEVICE_RUNTIME_INACTIVE"


def test_device_projection_rejects_ambiguous_state_before_ui_launch() -> None:
    runtime = _runtime(
        lambda *, ready_only=False: [_peer(), _peer(deviceName="duplicate")]
    )
    coordinator = DeviceManagementCoordinator(runtime)

    with pytest.raises(DeviceManagementError) as raised:
        coordinator.execute("devices")
    assert raised.value.code == "DEVICE_STATE_INVALID"


def test_device_name_control_sequences_are_not_renderable() -> None:
    coordinator = DeviceManagementCoordinator(
        _runtime(lambda *, ready_only=False: [_peer(deviceName="A\x1b[31m")]),
    )
    name = coordinator.execute("devices").devices[0].device_name
    assert "\x1b" not in name
    assert name == "A�[31m"


def test_product_only_commands_are_dynamic_in_router_completion_and_help() -> None:
    from mclaw.cli.app import InteractiveChat

    direct = InteractiveChat.__new__(InteractiveChat)
    direct.dsoftbus_runtime = None
    product = InteractiveChat.__new__(InteractiveChat)
    product.dsoftbus_runtime = object()

    assert {"devices", "pair", "unpair"}.isdisjoint(
        direct._get_command_router().command_names
    )
    assert {"devices", "pair", "unpair"} <= product._get_command_router().command_names

    registry = SimpleNamespace(_last_scan=1, list_skills=lambda: [])
    direct_names = {
        completion.text
        for completion in SlashCompleter(
            registry,
            include_dsoftbus=lambda: False,
        ).get_completions(Document("/"), None)
    }
    product_names = {
        completion.text
        for completion in SlashCompleter(
            registry,
            include_dsoftbus=lambda: True,
        ).get_completions(Document("/"), None)
    }
    assert {"/devices", "/pair", "/unpair"}.isdisjoint(direct_names)
    assert {"/devices", "/pair", "/unpair"} <= product_names

    direct_panels = []
    CommandsRenderer(printer=lambda _text: None, panel_sink=direct_panels.append).render_help(
        push_to_talk_label="Ctrl+Space",
        dsoftbus_enabled=False,
    )
    product_panels = []
    CommandsRenderer(printer=lambda _text: None, panel_sink=product_panels.append).render_help(
        push_to_talk_label="Ctrl+Space",
        dsoftbus_enabled=True,
    )
    assert "/devices" not in repr(direct_panels[0])
    assert "/devices" in repr(product_panels[0])


def test_providerless_standard_tui_exposes_device_commands_without_agent() -> None:
    from mclaw.cli.app import InteractiveChat

    actions: list[str] = []
    rendered: list[tuple[str, object]] = []
    device = ManagedDevice(
        device_id=DEVICE_ID,
        device_name="Kaihong A",
        device_presence="ONLINE",
        connection_state="OPEN",
        agent_availability="READY",
    )
    trusted = TrustedDevice(
        device_id_sha256=DEVICE_ID_SHA256,
        device_name="Kaihong A",
        device_type_id=533,
        online=True,
        public_device_id=DEVICE_ID,
    )

    class Coordinator:
        def execute(self, action: str) -> DeviceManagementResult:
            actions.append(action)
            return DeviceManagementResult(
                action=action,
                devices=(device,) if action == "devices" else (),
                trusted_devices=(trusted,) if action == "devices" else (),
                system_ui_opened=action != "unpair",
                status="unbound" if action == "unpair" else "listed",
            )

    class Renderer:
        def render_dsoftbus_devices(self, devices, trusted, pairable) -> None:
            rendered.append(("devices", (devices, trusted, pairable)))

        def render_notice(self, title, message, **kwargs) -> None:
            rendered.append((title, message))

    chat = InteractiveChat.__new__(InteractiveChat)
    chat.agent = None
    chat.pending_provider_runtime = None
    chat.dsoftbus_runtime = object()
    chat._device_management_coordinator = Coordinator()
    chat._commands_renderer = Renderer()

    assert chat.model == "未配置模型"
    assert chat.provider == "未配置"
    assert chat.process_command("/devices") is True
    assert chat.process_command("/pair") is True
    assert chat.process_command("/unpair") is True

    assert actions == ["devices", "pair", "unpair"]
    assert rendered[0][0] == "devices"
    assert rendered[0][1][0][0]["deviceId"] == DEVICE_ID
    assert rendered[1][1] == "设备配对已完成。"
    assert rendered[2][1] == "已解除设备配对。"
