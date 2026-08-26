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
    {
        "UNAVAILABLE",
        "UNBOUND",
        "BINDING",
        "BINDING_OPEN",
        "CARD_VERIFYING",
        "READY",
        "CLOSED",
    }
)
_PRODUCT_ERROR_CODES = frozenset(
    {
        "DEVICE_BIND_FAILED",
        "DEVICE_BIND_TIMEOUT",
        "DEVICE_BIND_UNCONFIRMED",
        "DEVICE_DISCOVERY_BUSY",
        "DEVICE_DISCOVERY_FAILED",
        "DEVICE_LIST_UNAVAILABLE",
        "DEVICE_MANAGER_PERMISSION_DENIED",
        "DEVICE_MANAGER_TIMEOUT",
        "DEVICE_MANAGER_UNAVAILABLE",
        "DEVICE_NOT_FOUND",
        "DEVICE_NOT_MANAGED",
        "DEVICE_RUNTIME_INACTIVE",
        "DEVICE_RUNTIME_PROTOCOL_ERROR",
        "DEVICE_STATE_INVALID",
        "DEVICE_TARGET_AMBIGUOUS",
        "DEVICE_UNBIND_FAILED",
        "DEVICE_UNBIND_UNCONFIRMED",
        "WORKER_DIED",
        "WORKER_NOT_READY",
    }
)
_TIMEOUT_ERROR_CODES = frozenset(
    {
        "DEVICE_MANAGER_BRIDGE_TIMEOUT",
        "NATIVE_TIMEOUT",
        "OWNER_LOOP_TIMEOUT",
        "WORKER_CONTROL_TIMEOUT",
    }
)
_UNAVAILABLE_ERROR_CODES = frozenset(
    {
        "DEVICE_MANAGER_BRIDGE_CLOSED",
        "DEVICE_MANAGER_BRIDGE_START_FAILED",
        "DEVICE_MANAGER_SERVICE_DIED",
        "OWNER_LOOP_DIED",
        "WORKER_START_FAILED",
    }
)
_INVALID_STATE_ERROR_CODES = frozenset(
    {
        "DEVICE_MANAGER_DATA_INVALID",
        "NATIVE_DATA_INVALID",
        "OWNER_RESOURCE_RESULT_INVALID",
        "RUNTIME_DRIVER_INVALID",
    }
)
_PROTOCOL_ERROR_CODES = frozenset(
    {
        "DEVICE_MANAGER_BRIDGE_PROTOCOL_ERROR",
        "NATIVE_EVENT_INVALID",
        "WORKER_PROTOCOL_ERROR",
    }
)


class DeviceManagementError(RuntimeError):
    """Stable product-facing failure for one device-management command."""

    def __init__(
        self,
        code: str,
        *,
        source_code: str | None = None,
        native_code: int | None = None,
        phase: str = "",
        outcome_unknown: bool = False,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.source_code = source_code or code
        self.native_code = native_code
        self.phase = phase
        self.outcome_unknown = outcome_unknown


def _fail(code: str) -> NoReturn:
    raise DeviceManagementError(code)


def _classify_error(source_code: str, fallback_code: str) -> str:
    if source_code in _PRODUCT_ERROR_CODES:
        return source_code
    if source_code in _TIMEOUT_ERROR_CODES:
        return "DEVICE_MANAGER_TIMEOUT"
    if source_code in _UNAVAILABLE_ERROR_CODES:
        return "DEVICE_MANAGER_UNAVAILABLE"
    if source_code in _INVALID_STATE_ERROR_CODES:
        return "DEVICE_STATE_INVALID"
    if source_code in _PROTOCOL_ERROR_CODES:
        return "DEVICE_RUNTIME_PROTOCOL_ERROR"
    if source_code in {"CAPACITY_BUSY", "DEVICE_DISCOVERY_INACTIVE"}:
        return "DEVICE_DISCOVERY_BUSY"
    if source_code in {"RUNTIME_STOPPED", "RUNTIME_STOPPING"}:
        return "DEVICE_RUNTIME_INACTIVE"
    return fallback_code


def _wrap_error(
    error: BaseException,
    fallback_code: str,
    *,
    phase: str,
) -> DeviceManagementError:
    source_code = str(
        getattr(error, "source_code", None)
        or getattr(error, "code", None)
        or fallback_code
    )
    return DeviceManagementError(
        _classify_error(source_code, fallback_code),
        source_code=source_code,
        native_code=getattr(error, "native_code", None),
        phase=str(getattr(error, "phase", "")) or phase,
        outcome_unknown=bool(getattr(error, "outcome_unknown", False)),
    )


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


def _short_candidate_code(value: str) -> str:
    """Format one validated DeviceManager digest for human selection."""

    prefix = value[:12].upper()
    return "-".join(prefix[index : index + 4] for index in range(0, 12, 4))


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
class LocalDevice:
    """Verified public identity of the Runtime running this TUI."""

    device_id: str
    device_name: str
    manufacturer: str
    model: str
    os_name: str
    os_version: str
    api_level: int
    arch: str

    def public_dict(self) -> dict[str, Any]:
        return {
            "apiLevel": self.api_level,
            "arch": self.arch,
            "deviceId": self.device_id,
            "deviceName": self.device_name,
            "manufacturer": self.manufacturer,
            "model": self.model,
            "osName": self.os_name,
            "osVersion": self.os_version,
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
    public_device_id: str

    def public_dict(self) -> dict[str, Any]:
        return {
            "deviceIdSha256": self.device_id_sha256,
            "deviceName": self.device_name,
            "deviceTypeId": self.device_type_id,
            "publicDeviceId": self.public_device_id,
        }


@dataclass(frozen=True)
class DeviceManagementResult:
    """Bounded result returned to either interactive frontend."""

    action: str
    devices: tuple[ManagedDevice, ...]
    local_device: LocalDevice | None = None
    trusted_devices: tuple[TrustedDevice, ...] = ()
    pairable_devices: tuple[PairableDevice, ...] = ()
    discovery_warning: DeviceDiscoveryWarning | None = None
    system_ui_opened: bool = False
    status: str = ""
    selected_device_id_sha256: str = ""

    def public_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "devices": [device.public_dict() for device in self.devices],
            "discoveryWarning": (
                self.discovery_warning.public_dict()
                if self.discovery_warning is not None
                else None
            ),
            "pairableDevices": [
                device.public_dict() for device in self.pairable_devices
            ],
            "localDevice": (
                self.local_device.public_dict()
                if self.local_device is not None
                else None
            ),
            "selectedDeviceIdSha256": self.selected_device_id_sha256,
            "status": self.status,
            "systemUiOpened": self.system_ui_opened,
            "trustedDevices": [device.public_dict() for device in self.trusted_devices],
        }


@dataclass(frozen=True)
class DeviceDiscoveryWarning:
    """Non-fatal indication that a validated candidate list is incomplete."""

    code: str
    source_code: str
    native_code: int | None
    phase: str = ""
    outcome_unknown: bool = False

    def public_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "nativeCode": self.native_code,
            "sourceCode": self.source_code,
        }


class PublicPeerRuntime(Protocol):
    def health(self) -> dict[str, Any]: ...

    def local_device(self) -> dict[str, Any]: ...

    def list_peers(self, *, ready_only: bool = False) -> list[dict[str, Any]]: ...

    def list_trusted_devices(self) -> list[dict[str, Any]]: ...

    def discover_devices(self) -> dict[str, Any]: ...

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


def _normalize_local_device(value: Mapping[str, Any]) -> LocalDevice:
    required = frozenset(
        {
            "apiLevel",
            "arch",
            "deviceId",
            "deviceName",
            "manufacturer",
            "model",
            "osName",
            "osVersion",
        }
    )
    if frozenset(value) != required:
        _fail("DEVICE_STATE_INVALID")
    device_id = value.get("deviceId")
    api_level = value.get("apiLevel")
    if (
        not isinstance(device_id, str)
        or _DEVICE_ID.fullmatch(device_id) is None
        or type(api_level) is not int
        or not 1 <= api_level <= 2**16 - 1
    ):
        _fail("DEVICE_STATE_INVALID")
    arch = value.get("arch")
    if not isinstance(arch, str) or not arch or len(arch.encode("utf-8")) > 32:
        _fail("DEVICE_STATE_INVALID")
    return LocalDevice(
        device_id=device_id,
        device_name=_safe_display_name(value.get("deviceName")),
        manufacturer=_safe_display_name(value.get("manufacturer")),
        model=_safe_display_name(value.get("model")),
        os_name=_safe_display_name(value.get("osName")),
        os_version=_safe_display_name(value.get("osVersion")),
        api_level=api_level,
        arch=arch,
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
        or (public_device_id != "" and _DEVICE_ID.fullmatch(public_device_id) is None)
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
    public_device_id = value.get("publicDeviceId")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
        or type(device_type_id) is not int
        or not 0 <= device_type_id <= 2**16 - 1
        or not isinstance(public_device_id, str)
        or (
            public_device_id != ""
            and _DEVICE_ID.fullmatch(public_device_id) is None
        )
    ):
        _fail("DEVICE_STATE_INVALID")
    return PairableDevice(
        device_id_sha256=digest,
        device_name=_safe_display_name(value.get("deviceName")),
        device_type_id=device_type_id,
        public_device_id=public_device_id,
    )


def _select_pairable_device(devices: tuple[PairableDevice, ...]) -> str | None:
    from mclaw.cli.tui.selection_prompt import prompt_single_select

    selected = prompt_single_select(
        "M-Claw · 选择要配对的设备",
        [
            {
                "id": device.device_id_sha256,
                "label": device.device_name,
                "description": (
                    f"设备类型 {device.device_type_id} · "
                    + "扫描候选码 "
                    + _short_candidate_code(device.device_id_sha256)
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
                    + f" · 管理码 {_short_candidate_code(device.device_id_sha256)}"
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
            wrapped = _wrap_error(
                error,
                "DEVICE_RUNTIME_INACTIVE",
                phase="runtime_health",
            )
            raise DeviceManagementError(
                "DEVICE_RUNTIME_INACTIVE",
                source_code=wrapped.source_code,
                native_code=wrapped.native_code,
                phase=wrapped.phase,
                outcome_unknown=wrapped.outcome_unknown,
            ) from error
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
            raise _wrap_error(
                error,
                "DEVICE_LIST_UNAVAILABLE",
                phase="public_peer_list",
            ) from error
        if not isinstance(peers, list) or any(
            not isinstance(peer, Mapping) for peer in peers
        ):
            _fail("DEVICE_STATE_INVALID")
        devices = tuple(_normalize_peer(peer) for peer in peers)
        if len({device.device_id for device in devices}) != len(devices):
            _fail("DEVICE_STATE_INVALID")
        return tuple(sorted(devices, key=lambda device: device.device_id))

    def _local_device(self) -> LocalDevice:
        try:
            value = self._runtime.local_device()
        except DeviceManagementError:
            raise
        except Exception as error:
            raise _wrap_error(
                error,
                "DEVICE_LIST_UNAVAILABLE",
                phase="local_device",
            ) from error
        if not isinstance(value, Mapping):
            _fail("DEVICE_STATE_INVALID")
        return _normalize_local_device(value)

    def _trusted_devices(self) -> tuple[TrustedDevice, ...]:
        try:
            values = self._runtime.list_trusted_devices()
        except DeviceManagementError:
            raise
        except Exception as error:
            raise _wrap_error(
                error,
                "DEVICE_LIST_UNAVAILABLE",
                phase="trusted_device_list",
            ) from error
        if not isinstance(values, list) or any(
            not isinstance(value, Mapping) for value in values
        ):
            _fail("DEVICE_STATE_INVALID")
        devices = tuple(_normalize_trusted_device(value) for value in values)
        if len({device.device_id_sha256 for device in devices}) != len(devices):
            _fail("DEVICE_STATE_INVALID")
        return tuple(sorted(devices, key=lambda device: device.device_id_sha256))

    def _pairable_devices(
        self,
        *,
        trusted_devices: tuple[TrustedDevice, ...] = (),
        connected_devices: tuple[ManagedDevice, ...] = (),
    ) -> tuple[tuple[PairableDevice, ...], DeviceDiscoveryWarning | None]:
        try:
            report = self._runtime.discover_devices()
        except DeviceManagementError:
            raise
        except Exception as error:
            raise _wrap_error(
                error,
                "DEVICE_DISCOVERY_FAILED",
                phase="device_discovery",
            ) from error
        if not isinstance(report, Mapping) or frozenset(report) != frozenset(
            {"devices", "failureNativeCode"}
        ):
            _fail("DEVICE_STATE_INVALID")
        values = report["devices"]
        failure_native_code = report["failureNativeCode"]
        if not isinstance(values, list) or any(
            not isinstance(value, Mapping) for value in values
        ):
            _fail("DEVICE_STATE_INVALID")
        if failure_native_code is not None and (
            type(failure_native_code) is not int
            or failure_native_code == 0
            or not -(2**31) <= failure_native_code <= 2**31 - 1
        ):
            _fail("DEVICE_STATE_INVALID")
        devices = tuple(_normalize_pairable_device(value) for value in values)
        if len({device.device_id_sha256 for device in devices}) != len(devices):
            _fail("DEVICE_STATE_INVALID")
        trusted_digests = {
            device.device_id_sha256 for device in trusted_devices
        }
        occupied_public_ids = {
            device.public_device_id
            for device in trusted_devices
            if device.public_device_id
        }
        occupied_public_ids.update(device.device_id for device in connected_devices)
        devices = tuple(
            device
            for device in devices
            if device.device_id_sha256 not in trusted_digests
            and (
                not device.public_device_id
                or device.public_device_id not in occupied_public_ids
            )
        )
        devices = tuple(sorted(devices, key=lambda device: device.device_id_sha256))
        if failure_native_code is None:
            return devices, None
        if not devices:
            raise DeviceManagementError(
                "DEVICE_DISCOVERY_FAILED",
                source_code="DEVICE_DISCOVERY_FAILED",
                native_code=failure_native_code,
                phase="device_discovery",
            )
        return devices, DeviceDiscoveryWarning(
            code="DEVICE_DISCOVERY_PARTIAL",
            source_code="DEVICE_DISCOVERY_FAILED",
            native_code=failure_native_code,
        )

    def execute(self, action: str) -> DeviceManagementResult:
        if action not in DEVICE_MANAGER_ACTIONS:
            _fail("INVALID_PARAMS")
        self._require_active_runtime()
        if action == "devices":
            local_device = self._local_device()
            devices = self._devices()
            trusted = self._trusted_devices()
            try:
                pairable, discovery_warning = self._pairable_devices(
                    trusted_devices=trusted,
                    connected_devices=devices,
                )
            except DeviceManagementError as error:
                # DeviceManager discovery is independent from the cached
                # SoftBus peer view.  Keep the first two sections useful when
                # a system scan times out or the Worker is recovering.
                pairable = ()
                discovery_warning = DeviceDiscoveryWarning(
                    code=error.code,
                    source_code=error.source_code,
                    native_code=error.native_code,
                    phase=error.phase,
                    outcome_unknown=error.outcome_unknown,
                )
            return DeviceManagementResult(
                action=action,
                devices=devices,
                local_device=local_device,
                trusted_devices=trusted,
                pairable_devices=pairable,
                discovery_warning=discovery_warning,
                system_ui_opened=False,
                status="listed",
            )

        if action == "pair":
            trusted = self._trusted_devices()
            connected = self._devices()
            pairable, discovery_warning = self._pairable_devices(
                trusted_devices=trusted,
                connected_devices=connected,
            )
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
                    discovery_warning=discovery_warning,
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
                    discovery_warning=discovery_warning,
                    status="cancelled",
                    selected_device_id_sha256=selected.device_id_sha256,
                )
            try:
                result = self._runtime.pair_device(selected.device_id_sha256)
            except DeviceManagementError:
                raise
            except Exception as error:
                raise _wrap_error(
                    error,
                    "DEVICE_BIND_FAILED",
                    phase="device_bind",
                ) from error
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
                discovery_warning=discovery_warning,
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
            (device for device in trusted if device.device_id_sha256 == selected_id),
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
            raise _wrap_error(
                error,
                "DEVICE_UNBIND_FAILED",
                phase="device_unbind",
            ) from error
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
    "LocalDevice",
    "ManagedDevice",
    "PairableDevice",
    "TrustedDevice",
]
