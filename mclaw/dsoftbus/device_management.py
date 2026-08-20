# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Kaihong product DeviceManager coordination for slash commands.

M-Claw owns candidate discovery, terminal selection, and the local confirmation
step.  The system DeviceManager still owns PIN entry, peer confirmation, and
authorization.  Raw system device identifiers never leave the isolated Worker.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, NoReturn, Protocol, TypeVar


DEVICE_MANAGER_ACTIONS = frozenset({"devices", "pair", "unpair"})

_DEVICE_ID = re.compile(r"^urn:mclaw:device:oh:[0-9a-f]{64}$")
_CONNECTION_STATES = frozenset({"CLOSED", "CONNECTING", "RECONNECTING", "OPEN"})
_AGENT_STATES = frozenset(
    {"UNAVAILABLE", "UNBOUND", "BINDING", "BINDING_OPEN", "CARD_VERIFYING", "READY", "CLOSED"}
)


class DeviceManagementError(RuntimeError):
    """Stable product-facing failure for one device-management command."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _fail(code: str) -> NoReturn:
    raise DeviceManagementError(code)


def _safe_display_name(value: Any) -> str:
    if not isinstance(value, str):
        _fail("DEVICE_STATE_INVALID")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeError as error:
        raise DeviceManagementError("DEVICE_STATE_INVALID") from error
    if len(encoded) > 127:
        _fail("DEVICE_STATE_INVALID")
    sanitized = "".join(
        "�" if unicodedata.category(character).startswith("C") else character
        for character in value
    ).strip()
    return sanitized or "未命名设备"


@dataclass(frozen=True)
class ManagedDevice:
    """A display-safe projection of one Runtime-verified public Peer."""

    device_id: str
    device_name: str
    device_presence: str
    connection_state: str
    agent_availability: str

    def public_dict(self) -> dict[str, str]:
        return {
            "agentAvailability": self.agent_availability,
            "connectionState": self.connection_state,
            "deviceId": self.device_id,
            "deviceName": self.device_name,
            "devicePresence": self.device_presence,
        }


@dataclass(frozen=True)
class TrustedDevice:
    """A display-safe DeviceManager target identified only by its digest."""

    device_id_sha256: str
    device_name: str
    device_type_id: int
    online: bool
    public_device_id: str

    def public_dict(self) -> dict[str, Any]:
        return {
            "deviceIdSha256": self.device_id_sha256,
            "deviceName": self.device_name,
            "deviceTypeId": self.device_type_id,
            "online": self.online,
            "publicDeviceId": self.public_device_id,
        }


@dataclass(frozen=True)
class PairableDevice:
    """A display-safe discovery candidate identified only by its digest."""

    device_id_sha256: str
    device_name: str
    device_type_id: int

    def public_dict(self) -> dict[str, Any]:
        return {
            "deviceIdSha256": self.device_id_sha256,
            "deviceName": self.device_name,
            "deviceTypeId": self.device_type_id,
        }


@dataclass(frozen=True)
class DeviceManagementResult:
    """Bounded result returned to either interactive frontend."""

    action: str
    devices: tuple[ManagedDevice, ...]
    trusted_devices: tuple[TrustedDevice, ...] = ()
    pairable_devices: tuple[PairableDevice, ...] = ()
    system_ui_opened: bool = False
    status: str = ""
    selected_device_id_sha256: str = ""

    def public_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "devices": [device.public_dict() for device in self.devices],
            "pairableDevices": [
                device.public_dict() for device in self.pairable_devices
            ],
            "selectedDeviceIdSha256": self.selected_device_id_sha256,
            "status": self.status,
            "systemUiOpened": self.system_ui_opened,
            "trustedDevices": [
                device.public_dict() for device in self.trusted_devices
            ],
        }


class PublicPeerRuntime(Protocol):
    def health(self) -> dict[str, Any]: ...

    def list_peers(self, *, ready_only: bool = False) -> list[dict[str, Any]]: ...

    def list_trusted_devices(self) -> list[dict[str, Any]]: ...

    def discover_devices(self) -> list[dict[str, Any]]: ...

    def pair_device(self, device_id_sha256: str) -> dict[str, Any]: ...

    def unbind_device(self, device_id_sha256: str) -> dict[str, Any]: ...


DeviceSelector = Callable[[tuple[TrustedDevice, ...]], str | None]
PairDeviceSelector = Callable[[tuple[PairableDevice, ...]], str | None]
PairConfirmation = Callable[[PairableDevice], bool]
UnpairConfirmation = Callable[[TrustedDevice], bool]
_PromptResult = TypeVar("_PromptResult")
TerminalPromptRunner = Callable[[Callable[[], _PromptResult]], _PromptResult]


def _run_prompt_direct(prompt: Callable[[], _PromptResult]) -> _PromptResult:
    return prompt()


def _normalize_peer(value: Mapping[str, Any]) -> ManagedDevice:
    device_id = value.get("deviceId")
    if not isinstance(device_id, str) or _DEVICE_ID.fullmatch(device_id) is None:
        _fail("DEVICE_STATE_INVALID")
    presence = value.get("devicePresence")
    connection = value.get("connectionState")
    agent = value.get("agentAvailability")
    if presence != "ONLINE":
        _fail("DEVICE_STATE_INVALID")
    if connection not in _CONNECTION_STATES or agent not in _AGENT_STATES:
        _fail("DEVICE_STATE_INVALID")
    return ManagedDevice(
        device_id=device_id,
        device_name=_safe_display_name(value.get("deviceName")),
        device_presence=presence,
        connection_state=connection,
        agent_availability=agent,
    )


def _normalize_trusted_device(value: Mapping[str, Any]) -> TrustedDevice:
    digest = value.get("deviceIdSha256")
    device_type_id = value.get("deviceTypeId")
    online = value.get("online")
    public_device_id = value.get("publicDeviceId")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
        or type(device_type_id) is not int
        or not 0 <= device_type_id <= 2**16 - 1
        or type(online) is not bool
        or not isinstance(public_device_id, str)
        or (
            public_device_id != ""
            and _DEVICE_ID.fullmatch(public_device_id) is None
        )
        or online != bool(public_device_id)
    ):
        _fail("DEVICE_STATE_INVALID")
    return TrustedDevice(
        device_id_sha256=digest,
        device_name=_safe_display_name(value.get("deviceName")),
        device_type_id=device_type_id,
        online=online,
        public_device_id=public_device_id,
    )


def _normalize_pairable_device(value: Mapping[str, Any]) -> PairableDevice:
    digest = value.get("deviceIdSha256")
    device_type_id = value.get("deviceTypeId")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
        or type(device_type_id) is not int
        or not 0 <= device_type_id <= 2**16 - 1
    ):
        _fail("DEVICE_STATE_INVALID")
    return PairableDevice(
        device_id_sha256=digest,
        device_name=_safe_display_name(value.get("deviceName")),
        device_type_id=device_type_id,
    )


def _select_pairable_device(
    devices: tuple[PairableDevice, ...]
) -> str | None:
    from mclaw.cli.tui.selection_prompt import prompt_single_select

    selected = prompt_single_select(
        "M-Claw · 选择要配对的设备",
        [
            {
                "id": device.device_id_sha256,
                "label": device.device_name,
                "description": (
                    f"设备类型 {device.device_type_id} · ID "
                    f"{device.device_id_sha256[:12]}"
                ),
            }
            for device in devices
        ],
        hint="↑/↓ 选择，Enter 确认，Esc 返回。系统随后负责 PIN 与对端确认。",
    )
    return selected or None


def _confirm_pair(device: PairableDevice) -> bool:
    from mclaw.cli.tui.selection_prompt import prompt_single_select

    answer = prompt_single_select(
        "M-Claw · 确认设备配对",
        [
            {
                "id": "no",
                "label": "否，返回",
                "description": "不创建系统信任关系",
            },
            {
                "id": "yes",
                "label": "是，开始配对",
                "description": f"与 {device.device_name} 建立 M-Claw 设备信任",
            },
        ],
        hint="确认后请按系统提示完成 PIN 和对端授权。",
        default_selected="no",
    )
    return answer == "yes"


def _select_trusted_device(devices: tuple[TrustedDevice, ...]) -> str | None:
    from mclaw.cli.tui.selection_prompt import prompt_single_select

    selected = prompt_single_select(
        "M-Claw · 选择要解除配对的可信设备",
        [
            {
                "id": device.device_id_sha256,
                "label": device.device_name,
                "description": (
                    ("当前在线" if device.online else "当前不可用")
                    + f" · ID {device.device_id_sha256[:12]}"
                ),
            }
            for device in devices
        ],
        hint="这里只显示由 M-Claw 创建且仍可管理的可信设备。Esc 返回。",
    )
    return selected or None


def _confirm_unpair(device: TrustedDevice) -> bool:
    from mclaw.cli.tui.selection_prompt import prompt_single_select

    answer = prompt_single_select(
        "M-Claw · 确认解除配对",
        [
            {
                "id": "no",
                "label": "否，返回",
                "description": "保留当前系统信任关系",
            },
            {
                "id": "yes",
                "label": "是，解除配对",
                "description": f"移除 {device.device_name} 的系统信任关系",
            },
        ],
        hint="解除后双方会断开；以后需要重新进行系统配对。",
        default_selected="no",
    )
    return answer == "yes"


class DeviceManagementCoordinator:
    """Coordinate M-Claw selection with system-owned trust confirmation."""

    def __init__(
        self,
        runtime: PublicPeerRuntime,
        *,
        device_selector: DeviceSelector = _select_trusted_device,
        pair_device_selector: PairDeviceSelector = _select_pairable_device,
        pair_confirmation: PairConfirmation = _confirm_pair,
        unpair_confirmation: UnpairConfirmation = _confirm_unpair,
        terminal_prompt_runner: TerminalPromptRunner = _run_prompt_direct,
    ) -> None:
        self._runtime = runtime
        self._device_selector = device_selector
        self._pair_device_selector = pair_device_selector
        self._pair_confirmation = pair_confirmation
        self._unpair_confirmation = unpair_confirmation
        self._terminal_prompt_runner = terminal_prompt_runner

    def _require_active_runtime(self) -> None:
        try:
            health = self._runtime.health()
        except Exception as error:
            raise DeviceManagementError("DEVICE_RUNTIME_INACTIVE") from error
        if not isinstance(health, Mapping) or health.get("state") not in {
            "READY",
            "DEGRADED",
        }:
            _fail("DEVICE_RUNTIME_INACTIVE")

    def _devices(self) -> tuple[ManagedDevice, ...]:
        try:
            peers = self._runtime.list_peers(ready_only=False)
        except DeviceManagementError:
            raise
        except Exception as error:
            raise DeviceManagementError("DEVICE_LIST_UNAVAILABLE") from error
        if not isinstance(peers, list) or any(
            not isinstance(peer, Mapping) for peer in peers
        ):
            _fail("DEVICE_STATE_INVALID")
        devices = tuple(_normalize_peer(peer) for peer in peers)
        if len({device.device_id for device in devices}) != len(devices):
            _fail("DEVICE_STATE_INVALID")
        return tuple(sorted(devices, key=lambda device: device.device_id))

    def _trusted_devices(self) -> tuple[TrustedDevice, ...]:
        try:
            values = self._runtime.list_trusted_devices()
        except DeviceManagementError:
            raise
        except Exception as error:
            code = str(getattr(error, "code", "DEVICE_LIST_UNAVAILABLE"))
            raise DeviceManagementError(code) from error
        if not isinstance(values, list) or any(
            not isinstance(value, Mapping) for value in values
        ):
            _fail("DEVICE_STATE_INVALID")
        devices = tuple(_normalize_trusted_device(value) for value in values)
        if len({device.device_id_sha256 for device in devices}) != len(devices):
            _fail("DEVICE_STATE_INVALID")
        return tuple(sorted(devices, key=lambda device: device.device_id_sha256))

    def _pairable_devices(self) -> tuple[PairableDevice, ...]:
        try:
            values = self._runtime.discover_devices()
        except DeviceManagementError:
            raise
        except Exception as error:
            code = str(getattr(error, "code", "DEVICE_DISCOVERY_FAILED"))
            raise DeviceManagementError(code) from error
        if not isinstance(values, list) or any(
            not isinstance(value, Mapping) for value in values
        ):
            _fail("DEVICE_STATE_INVALID")
        devices = tuple(_normalize_pairable_device(value) for value in values)
        if len({device.device_id_sha256 for device in devices}) != len(devices):
            _fail("DEVICE_STATE_INVALID")
        return tuple(sorted(devices, key=lambda device: device.device_id_sha256))

    def execute(self, action: str) -> DeviceManagementResult:
        if action not in DEVICE_MANAGER_ACTIONS:
            _fail("INVALID_PARAMS")
        self._require_active_runtime()
        if action == "devices":
            return DeviceManagementResult(
                action=action,
                devices=self._devices(),
                trusted_devices=self._trusted_devices(),
                pairable_devices=self._pairable_devices(),
                system_ui_opened=False,
                status="listed",
            )

        if action == "pair":
            pairable = self._pairable_devices()
            if not pairable:
                _fail("DEVICE_NOT_FOUND")
            try:
                selected_id = self._terminal_prompt_runner(
                    lambda: self._pair_device_selector(pairable)
                )
            except (EOFError, KeyboardInterrupt):
                selected_id = None
            if selected_id is None:
                return DeviceManagementResult(
                    action=action,
                    devices=(),
                    pairable_devices=pairable,
                    status="cancelled",
                )
            selected = next(
                (
                    device
                    for device in pairable
                    if device.device_id_sha256 == selected_id
                ),
                None,
            )
            if selected is None:
                _fail("INVALID_PARAMS")
            try:
                confirmed = self._terminal_prompt_runner(
                    lambda: self._pair_confirmation(selected)
                )
            except (EOFError, KeyboardInterrupt):
                confirmed = False
            if not confirmed:
                return DeviceManagementResult(
                    action=action,
                    devices=(),
                    pairable_devices=pairable,
                    status="cancelled",
                    selected_device_id_sha256=selected.device_id_sha256,
                )
            try:
                result = self._runtime.pair_device(selected.device_id_sha256)
            except DeviceManagementError:
                raise
            except Exception as error:
                code = str(getattr(error, "code", "DEVICE_BIND_FAILED"))
                raise DeviceManagementError(code) from error
            if (
                not isinstance(result, Mapping)
                or result.get("deviceIdSha256") != selected.device_id_sha256
                or result.get("status") not in {"bound", "failed"}
                or type(result.get("nativeCode")) is not int
                or type(result.get("bound")) is not bool
            ):
                _fail("DEVICE_STATE_INVALID")
            if result.get("bound") is not True or result.get("status") != "bound":
                _fail("DEVICE_BIND_FAILED")
            return DeviceManagementResult(
                action=action,
                devices=(),
                pairable_devices=pairable,
                system_ui_opened=True,
                status="bound",
                selected_device_id_sha256=selected.device_id_sha256,
            )

        trusted = self._trusted_devices()
        if not trusted:
            _fail("DEVICE_NOT_FOUND")
        try:
            selected_id = self._terminal_prompt_runner(
                lambda: self._device_selector(trusted)
            )
        except (EOFError, KeyboardInterrupt):
            selected_id = None
        if selected_id is None:
            return DeviceManagementResult(
                action=action,
                devices=(),
                trusted_devices=trusted,
                system_ui_opened=False,
                status="cancelled",
            )
        selected = next(
            (
                device
                for device in trusted
                if device.device_id_sha256 == selected_id
            ),
            None,
        )
        if selected is None:
            _fail("INVALID_PARAMS")
        try:
            confirmed = self._terminal_prompt_runner(
                lambda: self._unpair_confirmation(selected)
            )
        except (EOFError, KeyboardInterrupt):
            confirmed = False
        if not confirmed:
            return DeviceManagementResult(
                action=action,
                devices=(),
                trusted_devices=trusted,
                system_ui_opened=False,
                status="cancelled",
                selected_device_id_sha256=selected.device_id_sha256,
            )
        try:
            result = self._runtime.unbind_device(selected.device_id_sha256)
        except DeviceManagementError:
            raise
        except Exception as error:
            code = str(getattr(error, "code", "DEVICE_UNBIND_FAILED"))
            raise DeviceManagementError(code) from error
        if (
            not isinstance(result, Mapping)
            or result.get("unbound") is not True
            or result.get("deviceIdSha256") != selected.device_id_sha256
        ):
            _fail("DEVICE_STATE_INVALID")
        return DeviceManagementResult(
            action=action,
            devices=(),
            trusted_devices=trusted,
            system_ui_opened=False,
            status="unbound",
            selected_device_id_sha256=selected.device_id_sha256,
        )


__all__ = [
    "DEVICE_MANAGER_ACTIONS",
    "DeviceManagementCoordinator",
    "DeviceManagementError",
    "DeviceManagementResult",
    "ManagedDevice",
    "PairableDevice",
    "TrustedDevice",
]
