# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Product-side DSoftBus Runtime state and deadline lifecycle skeleton."""

from __future__ import annotations

import asyncio
import copy
import math
import re
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol

from . import protocol
from .endpoint_lock import DsoftbusEndpointLock, EndpointLockError
from .protocol import DSOFTBUS_SHUTDOWN_TIMEOUT_S

_DEGRADED_REASON_PRIORITY = (
    "PRODUCT_INTEGRATION_UNVERIFIED",
    "MAIN_ABI_CONTAMINATED",
    "WORKER_START_FAILED",
    "WORKER_PROTOCOL_ERROR",
    "WORKER_RESTART_EXHAUSTED",
    "NODE_SNAPSHOT_OVERFLOW",
    "LISTENER_START_FAILED",
    "HEALTH_INVARIANT_VIOLATION",
)
_DEGRADED_REASONS = frozenset(_DEGRADED_REASON_PRIORITY)
_DEVICE_ID = re.compile(r"^urn:mclaw:device:oh:[0-9a-f]{64}$")
_DEVICE_ID_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class RuntimeState(str):
    NEW = "NEW"
    STARTING = "STARTING"
    READY = "READY"
    DEGRADED = "DEGRADED"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"


class DsoftbusRuntimeError(RuntimeError):
    """Stable product Runtime lifecycle failure."""

    def __init__(
        self,
        code: str,
        *,
        outcome_unknown: bool = False,
        interrupted: bool = False,
        native_code: int | None = None,
        phase: str = "",
    ) -> None:
        super().__init__(code)
        self.code = code
        self.outcome_unknown = outcome_unknown
        self.interrupted = interrupted
        self.native_code = native_code
        self.phase = phase


def _propagated_runtime_error(
    error: BaseException,
    fallback_code: str,
    *,
    outcome_unknown: bool | None = None,
) -> DsoftbusRuntimeError:
    return DsoftbusRuntimeError(
        str(getattr(error, "code", fallback_code)),
        outcome_unknown=(
            bool(getattr(error, "outcome_unknown", False))
            if outcome_unknown is None
            else outcome_unknown
        ),
        interrupted=bool(getattr(error, "interrupted", False)),
        native_code=getattr(error, "native_code", None),
        phase=str(getattr(error, "phase", "")),
    )


def _plain_copy(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain_copy(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain_copy(item) for item in value]
    if isinstance(value, list):
        return [_plain_copy(item) for item in value]
    return copy.deepcopy(value)


class RuntimeDriver(Protocol):
    """Resource driver owned by the Runtime lifecycle."""

    def start(
        self, runtime_instance_id: str, endpoint_lock: Any
    ) -> Mapping[str, Any]: ...

    def begin_shutdown(self) -> None: ...

    def stop(self, deadline: Callable[[], float]) -> None: ...

    def update_provider_runtime(self, context: Any | None) -> None: ...

    def local_turn_started(self, token: str) -> None: ...

    def local_turn_finished(self, token: str) -> None: ...

    def local_device_snapshot(self) -> Mapping[str, Any]: ...

    def list_trusted_devices(self) -> tuple[Mapping[str, Any], ...]: ...

    def discover_devices(self) -> Mapping[str, Any]: ...

    def pair_device(self, device_id_sha256: str) -> Mapping[str, Any]: ...

    def unbind_device(self, device_id_sha256: str) -> Mapping[str, Any]: ...


class _AdmissionOnlyDriver:
    """Current safe default until the bounded Worker supervisor is attached."""

    def start(self, runtime_instance_id: str, endpoint_lock: Any) -> Mapping[str, Any]:
        return MappingProxyType(
            {
                "degradedReasons": ("PRODUCT_INTEGRATION_UNVERIFIED",),
                "state": RuntimeState.DEGRADED,
            }
        )

    def begin_shutdown(self) -> None:
        return None

    def stop(self, deadline: Callable[[], float]) -> None:
        return None

    def update_provider_runtime(self, context: Any | None) -> None:
        return None

    def local_turn_started(self, token: str) -> None:
        return None

    def local_turn_finished(self, token: str) -> None:
        return None

    def local_device_snapshot(self) -> Mapping[str, Any]:
        return MappingProxyType({})

    def list_trusted_devices(self) -> tuple[Mapping[str, Any], ...]:
        raise DsoftbusRuntimeError("WORKER_NOT_READY")

    def discover_devices(self) -> Mapping[str, Any]:
        raise DsoftbusRuntimeError("WORKER_NOT_READY")

    def pair_device(self, device_id_sha256: str) -> Mapping[str, Any]:
        raise DsoftbusRuntimeError("WORKER_NOT_READY")

    def unbind_device(self, device_id_sha256: str) -> Mapping[str, Any]:
        raise DsoftbusRuntimeError("WORKER_NOT_READY")


EndpointLockFactory = Callable[[Path, str], Any]


def _default_endpoint_lock_factory(
    state_root: Path, runtime_instance_id: str
) -> DsoftbusEndpointLock:
    return DsoftbusEndpointLock.acquire(state_root, runtime_instance_id)


def _validate_start_outcome(value: Mapping[str, Any]) -> tuple[str, tuple[str, ...]]:
    if not isinstance(value, Mapping) or frozenset(value) != frozenset(
        {"degradedReasons", "state"}
    ):
        raise DsoftbusRuntimeError("RUNTIME_DRIVER_INVALID")
    state = value["state"]
    reasons = value["degradedReasons"]
    if not isinstance(reasons, (list, tuple)):
        raise DsoftbusRuntimeError("RUNTIME_DRIVER_INVALID")
    normalized = tuple(str(reason) for reason in reasons)
    if (
        len(normalized) != len(set(normalized))
        or any(reason not in _DEGRADED_REASONS for reason in normalized)
        or tuple(reason for reason in _DEGRADED_REASON_PRIORITY if reason in normalized)
        != normalized
    ):
        raise DsoftbusRuntimeError("RUNTIME_DRIVER_INVALID")
    if state == RuntimeState.READY and normalized:
        raise DsoftbusRuntimeError("RUNTIME_DRIVER_INVALID")
    if state == RuntimeState.DEGRADED and not normalized:
        raise DsoftbusRuntimeError("RUNTIME_DRIVER_INVALID")
    if state not in {RuntimeState.READY, RuntimeState.DEGRADED}:
        raise DsoftbusRuntimeError("RUNTIME_DRIVER_INVALID")
    return state, normalized


class DsoftbusRuntime:
    """One process-local Runtime generation with idempotent bounded shutdown."""

    def __init__(
        self,
        *,
        provider_runtime: Any | None,
        config: Mapping[str, Any],
        workspace: str | Path,
        state_root: str | Path,
        driver: RuntimeDriver | None = None,
        endpoint_lock_factory: EndpointLockFactory = _default_endpoint_lock_factory,
        monotonic: Callable[[], float] = time.monotonic,
        uuid_factory: Callable[[], uuid.UUID] = uuid.uuid4,
    ) -> None:
        if not isinstance(config, Mapping):
            raise TypeError("config must be a mapping")
        workspace_path = Path(workspace)
        state_root_path = Path(state_root)
        if not workspace_path.is_absolute() or not state_root_path.is_absolute():
            raise ValueError("workspace and state_root must be absolute")
        dsoftbus = config.get("dsoftbus")
        if not isinstance(dsoftbus, Mapping):
            raise DsoftbusRuntimeError("DSOFTBUS_CONFIG_INVALID")
        runtime_id = str(uuid_factory())
        try:
            parsed_id = uuid.UUID(runtime_id)
        except ValueError as error:
            raise DsoftbusRuntimeError("RUNTIME_INSTANCE_ID_INVALID") from error
        if str(parsed_id) != runtime_id or parsed_id.version != 4:
            raise DsoftbusRuntimeError("RUNTIME_INSTANCE_ID_INVALID")

        self._config = MappingProxyType(copy.deepcopy(dict(config)))
        self._workspace = workspace_path
        self._state_root = state_root_path
        self._driver: RuntimeDriver = driver or _AdmissionOnlyDriver()
        self._endpoint_lock_factory = endpoint_lock_factory
        self._monotonic = monotonic
        self._runtime_instance_id = runtime_id
        self._uuid_factory = uuid_factory
        self._condition = threading.Condition(threading.RLock())
        self._state = RuntimeState.NEW
        self._provider_runtime = provider_runtime
        self._degraded_reasons: tuple[str, ...] = ()
        self._primary_error_code = ""
        self._endpoint_lock: Any | None = None
        self._start_in_progress = False
        self._driver_start_called = False
        self._driver_begin_called = False
        self._driver_begin_error: BaseException | None = None
        self._stop_leader = False
        self._stop_complete = False
        self._stop_error: DsoftbusRuntimeError | None = None
        self._shutdown_deadline: float | None = None

    @property
    def runtime_instance_id(self) -> str:
        return self._runtime_instance_id

    @property
    def state(self) -> str:
        with self._condition:
            return self._state

    @property
    def shutdown_deadline(self) -> float | None:
        with self._condition:
            return self._shutdown_deadline

    def _snapshot_locked(self) -> Mapping[str, Any]:
        provider_ready = self._provider_runtime is not None
        return MappingProxyType(
            {
                "degradedReasons": self._degraded_reasons,
                "primaryErrorCode": self._primary_error_code,
                "providerReadinessCode": "" if provider_ready else "PROVIDER_MISSING",
                "providerReady": provider_ready,
                "runtimeInstanceId": self._runtime_instance_id,
                "shutdownDeadline": self._shutdown_deadline,
                "state": self._state,
            }
        )

    def health_snapshot(self) -> Mapping[str, Any]:
        with self._condition:
            use_driver_snapshot = self._driver_start_called
            fallback = self._snapshot_locked()
        driver_snapshot = getattr(self._driver, "health_snapshot", None)
        if use_driver_snapshot and callable(driver_snapshot):
            return driver_snapshot()
        return fallback

    def health(self) -> dict[str, Any]:
        """Return an independent plain snapshot for local tools and diagnostics."""

        return _plain_copy(self.health_snapshot())

    def diagnostic_snapshot(self) -> dict[str, Any]:
        """Return a bounded local-only view without crossing into owner-loop I/O."""

        with self._condition:
            lifecycle = _plain_copy(self._snapshot_locked())
            use_driver_snapshot = self._driver_start_called
        resource: Mapping[str, Any] = MappingProxyType({})
        accessor = getattr(self._driver, "diagnostic_snapshot", None)
        if use_driver_snapshot and callable(accessor):
            candidate = accessor()
            if isinstance(candidate, Mapping):
                resource = candidate
        return {
            "lifecycle": lifecycle,
            "resource": _plain_copy(resource),
        }

    def list_peers(self, *, ready_only: bool = False) -> list[dict[str, Any]]:
        """Read the verified public Peer cache without scheduling network work."""

        if type(ready_only) is not bool:
            raise DsoftbusRuntimeError("INVALID_PARAMS")
        with self._condition:
            state = self._state
            use_driver_snapshot = self._driver_start_called
        if state in {RuntimeState.STOPPING, RuntimeState.STOPPED}:
            raise DsoftbusRuntimeError("RUNTIME_STOPPING")
        if state in {RuntimeState.NEW, RuntimeState.STARTING}:
            raise DsoftbusRuntimeError("PEER_NOT_READY")
        accessor = getattr(self._driver, "public_peers_snapshot", None)
        peers: Any = accessor() if use_driver_snapshot and callable(accessor) else ()
        if not isinstance(peers, (list, tuple)) or any(
            not isinstance(peer, Mapping) for peer in peers
        ):
            raise DsoftbusRuntimeError("RUNTIME_DRIVER_INVALID")
        result = [_plain_copy(peer) for peer in peers]
        if ready_only:
            result = [
                peer for peer in result if peer.get("agentAvailability") == "READY"
            ]
        result.sort(
            key=lambda peer: str(peer.get("device_id", peer.get("deviceId", "")))
        )
        return result

    def local_device(self) -> dict[str, Any]:
        """Return this Runtime's verified, non-secret public device identity."""

        with self._condition:
            state = self._state
            use_driver_snapshot = self._driver_start_called
        if state in {RuntimeState.STOPPING, RuntimeState.STOPPED}:
            raise DsoftbusRuntimeError("RUNTIME_STOPPING")
        if state in {RuntimeState.NEW, RuntimeState.STARTING}:
            raise DsoftbusRuntimeError("WORKER_NOT_READY")
        accessor = getattr(self._driver, "local_device_snapshot", None)
        value: Any = (
            accessor()
            if use_driver_snapshot and callable(accessor)
            else MappingProxyType({})
        )
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
        if not isinstance(value, Mapping) or frozenset(value) != required:
            raise DsoftbusRuntimeError("RUNTIME_DRIVER_INVALID")
        if (
            not isinstance(value["deviceId"], str)
            or _DEVICE_ID.fullmatch(value["deviceId"]) is None
            or type(value["apiLevel"]) is not int
            or not 1 <= value["apiLevel"] <= 2**16 - 1
        ):
            raise DsoftbusRuntimeError("RUNTIME_DRIVER_INVALID")
        for field, maximum in (
            ("arch", 32),
            ("deviceName", 127),
            ("manufacturer", 127),
            ("model", 127),
            ("osName", 63),
            ("osVersion", 127),
        ):
            item = value[field]
            if (
                not isinstance(item, str)
                or "\x00" in item
                or not item
                or len(item.encode("utf-8")) > maximum
            ):
                raise DsoftbusRuntimeError("RUNTIME_DRIVER_INVALID")
        return _plain_copy(value)

    def discover_devices(self) -> dict[str, Any]:
        """Run one bounded M-Claw DeviceManager scan for pairable devices."""

        with self._condition:
            state = self._state
            use_driver = self._driver_start_called
        if state in {RuntimeState.STOPPING, RuntimeState.STOPPED}:
            raise DsoftbusRuntimeError("RUNTIME_STOPPING")
        if state in {RuntimeState.NEW, RuntimeState.STARTING}:
            raise DsoftbusRuntimeError("WORKER_NOT_READY")
        accessor = getattr(self._driver, "discover_devices", None)
        if not use_driver or not callable(accessor):
            raise DsoftbusRuntimeError("WORKER_NOT_READY")
        try:
            values = accessor()
        except DsoftbusRuntimeError:
            raise
        except Exception as error:
            raise _propagated_runtime_error(error, "INTERNAL_ERROR") from error
        if not isinstance(values, Mapping) or frozenset(values) != frozenset(
            {"devices", "failureNativeCode"}
        ):
            raise DsoftbusRuntimeError("RUNTIME_DRIVER_INVALID")
        failure_native_code = values["failureNativeCode"]
        raw_devices = values["devices"]
        if not isinstance(raw_devices, (list, tuple)) or (
            failure_native_code is not None
            and (
                type(failure_native_code) is not int
                or failure_native_code == 0
                or not -(2**31) <= failure_native_code <= 2**31 - 1
            )
        ):
            raise DsoftbusRuntimeError("RUNTIME_DRIVER_INVALID")
        result: list[dict[str, Any]] = []
        for value in raw_devices:
            if not isinstance(value, Mapping) or frozenset(value) != frozenset(
                {
                    "deviceIdSha256",
                    "deviceName",
                    "deviceTypeId",
                    "publicDeviceId",
                }
            ):
                raise DsoftbusRuntimeError("RUNTIME_DRIVER_INVALID")
            digest = value["deviceIdSha256"]
            name = value["deviceName"]
            device_type = value["deviceTypeId"]
            public_device_id = value["publicDeviceId"]
            if (
                not isinstance(digest, str)
                or _DEVICE_ID_SHA256.fullmatch(digest) is None
                or not isinstance(name, str)
                or "\x00" in name
                or len(name.encode("utf-8")) > 127
                or type(device_type) is not int
                or not 0 <= device_type <= 2**16 - 1
                or not isinstance(public_device_id, str)
                or (
                    public_device_id != ""
                    and _DEVICE_ID.fullmatch(public_device_id) is None
                )
            ):
                raise DsoftbusRuntimeError("RUNTIME_DRIVER_INVALID")
            result.append(_plain_copy(value))
        if len({item["deviceIdSha256"] for item in result}) != len(result):
            raise DsoftbusRuntimeError("RUNTIME_DRIVER_INVALID")
        result.sort(key=lambda item: item["deviceIdSha256"])
        return {
            "devices": result,
            "failureNativeCode": failure_native_code,
        }

    def pair_device(self, device_id_sha256: str) -> dict[str, Any]:
        """Bind one explicitly selected candidate without command replay."""

        if (
            not isinstance(device_id_sha256, str)
            or _DEVICE_ID_SHA256.fullmatch(device_id_sha256) is None
        ):
            raise DsoftbusRuntimeError("INVALID_PARAMS")
        with self._condition:
            state = self._state
            use_driver = self._driver_start_called
        if state in {RuntimeState.STOPPING, RuntimeState.STOPPED}:
            raise DsoftbusRuntimeError("RUNTIME_STOPPING")
        if state in {RuntimeState.NEW, RuntimeState.STARTING}:
            raise DsoftbusRuntimeError("WORKER_NOT_READY")
        accessor = getattr(self._driver, "pair_device", None)
        if not use_driver or not callable(accessor):
            raise DsoftbusRuntimeError("WORKER_NOT_READY")
        try:
            value = accessor(device_id_sha256)
        except DsoftbusRuntimeError:
            raise
        except Exception as error:
            raise DsoftbusRuntimeError(
                str(getattr(error, "code", "INTERNAL_ERROR")),
                outcome_unknown=bool(getattr(error, "outcome_unknown", False)),
            ) from error
        if not isinstance(value, Mapping) or frozenset(value) != frozenset(
            {"bound", "deviceIdSha256", "nativeCode", "status"}
        ):
            raise DsoftbusRuntimeError("RUNTIME_DRIVER_INVALID")
        status = value["status"]
        native_code = value["nativeCode"]
        bound = value["bound"]
        if (
            value["deviceIdSha256"] != device_id_sha256
            or status not in {"bound", "failed"}
            or type(native_code) is not int
            or not -(2**31) <= native_code <= 2**31 - 1
            or type(bound) is not bool
            or (status == "bound" and (bound is not True or native_code != 0))
            or (status == "failed" and (bound is not False or native_code == 0))
        ):
            raise DsoftbusRuntimeError("RUNTIME_DRIVER_INVALID")
        return _plain_copy(value)

    def list_trusted_devices(self) -> list[dict[str, Any]]:
        """Enumerate redacted DeviceManager targets through the tokenized Worker."""

        with self._condition:
            state = self._state
            use_driver = self._driver_start_called
        if state in {RuntimeState.STOPPING, RuntimeState.STOPPED}:
            raise DsoftbusRuntimeError("RUNTIME_STOPPING")
        if state in {RuntimeState.NEW, RuntimeState.STARTING}:
            raise DsoftbusRuntimeError("WORKER_NOT_READY")
        accessor = getattr(self._driver, "list_trusted_devices", None)
        if not use_driver or not callable(accessor):
            raise DsoftbusRuntimeError("WORKER_NOT_READY")
        try:
            values = accessor()
        except DsoftbusRuntimeError:
            raise
        except Exception as error:
            raise _propagated_runtime_error(error, "INTERNAL_ERROR") from error
        if not isinstance(values, (list, tuple)):
            raise DsoftbusRuntimeError("RUNTIME_DRIVER_INVALID")
        result: list[dict[str, Any]] = []
        for value in values:
            if not isinstance(value, Mapping) or frozenset(value) != frozenset(
                {
                    "deviceIdSha256",
                    "deviceName",
                    "deviceTypeId",
                    "online",
                    "publicDeviceId",
                }
            ):
                raise DsoftbusRuntimeError("RUNTIME_DRIVER_INVALID")
            digest = value["deviceIdSha256"]
            name = value["deviceName"]
            device_type = value["deviceTypeId"]
            online = value["online"]
            public_device_id = value["publicDeviceId"]
            if (
                not isinstance(digest, str)
                or _DEVICE_ID_SHA256.fullmatch(digest) is None
                or not isinstance(name, str)
                or "\x00" in name
                or len(name.encode("utf-8")) > 127
                or type(device_type) is not int
                or not 0 <= device_type <= 2**16 - 1
                or type(online) is not bool
                or not isinstance(public_device_id, str)
                or (
                    public_device_id != ""
                    and _DEVICE_ID.fullmatch(public_device_id) is None
                )
                or online != bool(public_device_id)
            ):
                raise DsoftbusRuntimeError("RUNTIME_DRIVER_INVALID")
            result.append(_plain_copy(value))
        if len({item["deviceIdSha256"] for item in result}) != len(result):
            raise DsoftbusRuntimeError("RUNTIME_DRIVER_INVALID")
        result.sort(key=lambda item: item["deviceIdSha256"])
        return result

    def unbind_device(self, device_id_sha256: str) -> dict[str, Any]:
        """Remove one explicitly selected trust relation without replay."""

        if (
            not isinstance(device_id_sha256, str)
            or _DEVICE_ID_SHA256.fullmatch(device_id_sha256) is None
        ):
            raise DsoftbusRuntimeError("INVALID_PARAMS")
        with self._condition:
            state = self._state
            use_driver = self._driver_start_called
        if state in {RuntimeState.STOPPING, RuntimeState.STOPPED}:
            raise DsoftbusRuntimeError("RUNTIME_STOPPING")
        if state in {RuntimeState.NEW, RuntimeState.STARTING}:
            raise DsoftbusRuntimeError("WORKER_NOT_READY")
        accessor = getattr(self._driver, "unbind_device", None)
        if not use_driver or not callable(accessor):
            raise DsoftbusRuntimeError("WORKER_NOT_READY")
        try:
            value = accessor(device_id_sha256)
        except DsoftbusRuntimeError:
            raise
        except Exception as error:
            code = str(getattr(error, "code", "INTERNAL_ERROR"))
            raise DsoftbusRuntimeError(
                code,
                outcome_unknown=(code == "DEVICE_UNBIND_UNCONFIRMED"),
            ) from error
        if not isinstance(value, Mapping) or frozenset(value) != frozenset(
            {"deviceIdSha256", "publicDeviceId", "unbound"}
        ):
            raise DsoftbusRuntimeError("RUNTIME_DRIVER_INVALID")
        public_device_id = value["publicDeviceId"]
        if (
            value["deviceIdSha256"] != device_id_sha256
            or value["unbound"] is not True
            or not isinstance(public_device_id, str)
            or (
                public_device_id != ""
                and _DEVICE_ID.fullmatch(public_device_id) is None
            )
        ):
            raise DsoftbusRuntimeError("RUNTIME_DRIVER_INVALID")
        return _plain_copy(value)

    def get_cached_device_context(self, device_id: str) -> dict[str, Any]:
        """Return one verified partial Device Context from the local cache."""

        if not isinstance(device_id, str) or _DEVICE_ID.fullmatch(device_id) is None:
            raise DsoftbusRuntimeError("INVALID_PARAMS")
        with self._condition:
            state = self._state
            use_driver_snapshot = self._driver_start_called
        if state in {RuntimeState.STOPPING, RuntimeState.STOPPED}:
            raise DsoftbusRuntimeError("RUNTIME_STOPPING")
        if state in {RuntimeState.NEW, RuntimeState.STARTING}:
            raise DsoftbusRuntimeError("PEER_NOT_READY")
        accessor = getattr(self._driver, "cached_device_context", None)
        if not use_driver_snapshot or not callable(accessor):
            raise DsoftbusRuntimeError("PEER_NOT_READY")
        try:
            value = accessor(device_id)
        except DsoftbusRuntimeError:
            raise
        except Exception as error:
            raise DsoftbusRuntimeError(
                str(getattr(error, "code", "INTERNAL_ERROR"))
            ) from error
        if not isinstance(value, Mapping):
            raise DsoftbusRuntimeError("RUNTIME_DRIVER_INVALID")
        return _plain_copy(value)

    async def aget_device_context(
        self, device_id: str, *, refresh_state: bool = False
    ) -> dict[str, Any]:
        if (
            type(refresh_state) is not bool
            or not isinstance(device_id, str)
            or _DEVICE_ID.fullmatch(device_id) is None
        ):
            raise DsoftbusRuntimeError("INVALID_PARAMS")
        if not refresh_state:
            return self.get_cached_device_context(device_id)
        with self._condition:
            state = self._state
            use_driver = self._driver_start_called
        if state in {RuntimeState.STOPPING, RuntimeState.STOPPED}:
            raise DsoftbusRuntimeError("RUNTIME_STOPPING")
        if state in {RuntimeState.NEW, RuntimeState.STARTING}:
            raise DsoftbusRuntimeError("PEER_NOT_READY")
        accessor = getattr(self._driver, "refresh_device_context_async", None)
        if not use_driver or not callable(accessor):
            raise DsoftbusRuntimeError("PEER_NOT_READY")
        try:
            value = await accessor(device_id)
        except DsoftbusRuntimeError:
            raise
        except Exception as error:
            raise DsoftbusRuntimeError(
                str(getattr(error, "code", "INTERNAL_ERROR"))
            ) from error
        if not isinstance(value, Mapping):
            raise DsoftbusRuntimeError("RUNTIME_DRIVER_INVALID")
        return _plain_copy(value)

    def prepare_device_context_refresh(
        self,
        device_id: str,
    ) -> tuple[asyncio.AbstractEventLoop, Any]:
        if not isinstance(device_id, str) or _DEVICE_ID.fullmatch(device_id) is None:
            raise DsoftbusRuntimeError("INVALID_PARAMS")
        with self._condition:
            state = self._state
            use_driver = self._driver_start_called
        if state in {RuntimeState.STOPPING, RuntimeState.STOPPED}:
            raise DsoftbusRuntimeError("RUNTIME_STOPPING")
        if state in {RuntimeState.NEW, RuntimeState.STARTING}:
            raise DsoftbusRuntimeError("PEER_NOT_READY")
        loop_accessor = getattr(self._driver, "outbound_loop", None)
        operation_accessor = getattr(
            self._driver,
            "refresh_device_context_on_owner",
            None,
        )
        if (
            not use_driver
            or not callable(loop_accessor)
            or not callable(operation_accessor)
        ):
            raise DsoftbusRuntimeError("PEER_NOT_READY")
        try:
            loop = loop_accessor()
            operation = operation_accessor(device_id)
        except Exception as error:
            code = str(getattr(error, "code", "INTERNAL_ERROR"))
            if code not in protocol.RPC_ERROR_CODES:
                code = "INTERNAL_ERROR"
            raise DsoftbusRuntimeError(code) from error
        if not isinstance(loop, asyncio.AbstractEventLoop) or not hasattr(
            operation,
            "__await__",
        ):
            close = getattr(operation, "close", None)
            if callable(close):
                close()
            raise DsoftbusRuntimeError("RUNTIME_DRIVER_INVALID")
        return loop, operation

    async def arun_agent_task(
        self,
        device_id: str,
        text: str,
        *,
        context_id: str | None = None,
        message_id: str | None = None,
        event_sink: Callable[[Mapping[str, Any]], Any] | None = None,
        input_paths: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        (
            device_id,
            text,
            normalized_context_id,
            normalized_message_id,
        ) = self._normalize_agent_task(
            device_id,
            text,
            context_id=context_id,
            message_id=message_id,
        )
        normalized_input_paths = self._normalize_agent_task_input_paths(input_paths)
        with self._condition:
            state = self._state
            use_driver = self._driver_start_called
        if state in {RuntimeState.STOPPING, RuntimeState.STOPPED}:
            raise DsoftbusRuntimeError("RUNTIME_STOPPING")
        if state in {RuntimeState.NEW, RuntimeState.STARTING}:
            raise DsoftbusRuntimeError("PEER_NOT_READY")
        accessor = getattr(self._driver, "run_agent_task_async", None)
        if not use_driver or not callable(accessor):
            raise DsoftbusRuntimeError("PEER_NOT_READY")
        try:
            value = await accessor(
                device_id,
                text,
                context_id=normalized_context_id,
                message_id=normalized_message_id,
                event_sink=event_sink,
                input_paths=normalized_input_paths,
            )
        except DsoftbusRuntimeError:
            raise
        except Exception as error:
            code = str(getattr(error, "code", "INTERNAL_ERROR"))
            if code not in protocol.RPC_ERROR_CODES:
                code = "INTERNAL_ERROR"
            raise DsoftbusRuntimeError(
                code,
                outcome_unknown=bool(getattr(error, "outcome_unknown", False)),
                interrupted=code == "AGENT_INTERRUPTED",
            ) from error
        if not isinstance(value, Mapping):
            raise DsoftbusRuntimeError("RUNTIME_DRIVER_INVALID")
        return _plain_copy(value)

    async def acontinue_agent_task(
        self,
        device_id: str,
        task_id: str,
        input_request_id: str,
        *,
        text: str = "",
        message_id: str | None = None,
        event_sink: Callable[[Mapping[str, Any]], Any] | None = None,
        input_paths: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        (
            device_id,
            task_id,
            input_request_id,
            text,
            normalized_message_id,
        ) = self._normalize_agent_task_continuation(
            device_id,
            task_id,
            input_request_id,
            text=text,
            message_id=message_id,
        )
        normalized_input_paths = self._normalize_agent_task_input_paths(input_paths)
        if not text and not normalized_input_paths:
            raise DsoftbusRuntimeError("INVALID_PARAMS")
        with self._condition:
            state = self._state
            use_driver = self._driver_start_called
        if state in {RuntimeState.STOPPING, RuntimeState.STOPPED}:
            raise DsoftbusRuntimeError("RUNTIME_STOPPING")
        if state in {RuntimeState.NEW, RuntimeState.STARTING}:
            raise DsoftbusRuntimeError("PEER_NOT_READY")
        accessor = getattr(self._driver, "continue_agent_task_async", None)
        if not use_driver or not callable(accessor):
            raise DsoftbusRuntimeError("PEER_NOT_READY")
        try:
            value = await accessor(
                device_id,
                task_id,
                input_request_id,
                text=text,
                message_id=normalized_message_id,
                event_sink=event_sink,
                input_paths=normalized_input_paths,
            )
        except DsoftbusRuntimeError:
            raise
        except Exception as error:
            code = str(getattr(error, "code", "INTERNAL_ERROR"))
            if code not in protocol.RPC_ERROR_CODES:
                code = "INTERNAL_ERROR"
            raise DsoftbusRuntimeError(
                code,
                outcome_unknown=bool(getattr(error, "outcome_unknown", False)),
                interrupted=code == "AGENT_INTERRUPTED",
            ) from error
        if not isinstance(value, Mapping):
            raise DsoftbusRuntimeError("RUNTIME_DRIVER_INVALID")
        return _plain_copy(value)

    def _normalize_agent_task(
        self,
        device_id: str,
        text: str,
        *,
        context_id: str | None,
        message_id: str | None,
    ) -> tuple[str, str, str | None, str]:
        if (
            not isinstance(device_id, str)
            or _DEVICE_ID.fullmatch(device_id) is None
            or not isinstance(text, str)
        ):
            raise DsoftbusRuntimeError("INVALID_PARAMS")
        try:
            text_bytes = text.encode("utf-8")
        except UnicodeEncodeError as error:
            raise DsoftbusRuntimeError("INVALID_PARAMS") from error
        if not 1 <= len(text_bytes) <= 24_576:
            raise DsoftbusRuntimeError("INVALID_PARAMS")
        try:
            normalized_context_id = (
                None
                if context_id is None
                else protocol.canonical_uuid4(context_id, "contextId")
            )
            normalized_message_id = protocol.canonical_uuid4(
                str(self._uuid_factory()) if message_id is None else message_id,
                "messageId",
            )
        except protocol.ProtocolError as error:
            raise DsoftbusRuntimeError("INVALID_PARAMS") from error
        return device_id, text, normalized_context_id, normalized_message_id

    def _normalize_agent_task_continuation(
        self,
        device_id: str,
        task_id: str,
        input_request_id: str,
        *,
        text: str,
        message_id: str | None,
    ) -> tuple[str, str, str, str, str]:
        if (
            not isinstance(device_id, str)
            or _DEVICE_ID.fullmatch(device_id) is None
            or not isinstance(text, str)
        ):
            raise DsoftbusRuntimeError("INVALID_PARAMS")
        try:
            text_bytes = text.encode("utf-8")
            normalized_task_id = protocol.canonical_uuid4(task_id, "taskId")
            normalized_request_id = protocol.canonical_uuid4(
                input_request_id,
                "inputRequestId",
            )
            normalized_message_id = protocol.canonical_uuid4(
                str(self._uuid_factory()) if message_id is None else message_id,
                "messageId",
            )
        except (UnicodeEncodeError, protocol.ProtocolError) as error:
            raise DsoftbusRuntimeError("INVALID_PARAMS") from error
        if len(text_bytes) > 24_576:
            raise DsoftbusRuntimeError("INVALID_PARAMS")
        return (
            device_id,
            normalized_task_id,
            normalized_request_id,
            text,
            normalized_message_id,
        )

    @staticmethod
    def _normalize_agent_task_input_paths(
        input_paths: tuple[str, ...],
    ) -> tuple[str, ...]:
        if (
            not isinstance(input_paths, tuple)
            or len(input_paths) > protocol.TASK_INPUT_PATH_MAX
            or any(
                not isinstance(path, str) or not path or "\x00" in path
                for path in input_paths
            )
        ):
            raise DsoftbusRuntimeError("INVALID_PARAMS")
        return input_paths

    def prepare_run_agent_task_outbound(
        self,
        device_id: str,
        text: str,
        *,
        context_id: str | None = None,
        message_id: str | None = None,
        event_sink: Callable[[Mapping[str, Any]], Any] | None = None,
        input_paths: tuple[str, ...] = (),
    ) -> tuple[asyncio.AbstractEventLoop, Any, str | None, str]:
        """Build one owner-loop coroutine for the common outbound fence."""

        (
            device_id,
            text,
            normalized_context_id,
            normalized_message_id,
        ) = self._normalize_agent_task(
            device_id,
            text,
            context_id=context_id,
            message_id=message_id,
        )
        normalized_input_paths = self._normalize_agent_task_input_paths(input_paths)
        with self._condition:
            state = self._state
            use_driver = self._driver_start_called
        if state in {RuntimeState.STOPPING, RuntimeState.STOPPED}:
            raise DsoftbusRuntimeError("RUNTIME_STOPPING")
        if state in {RuntimeState.NEW, RuntimeState.STARTING}:
            raise DsoftbusRuntimeError("PEER_NOT_READY")
        loop_accessor = getattr(self._driver, "outbound_loop", None)
        operation_accessor = getattr(self._driver, "run_agent_task_on_owner", None)
        if (
            not use_driver
            or not callable(loop_accessor)
            or not callable(operation_accessor)
        ):
            raise DsoftbusRuntimeError("PEER_NOT_READY")
        try:
            loop = loop_accessor()
            operation = operation_accessor(
                device_id,
                text,
                context_id=normalized_context_id,
                message_id=normalized_message_id,
                event_sink=event_sink,
                input_paths=normalized_input_paths,
            )
        except Exception as error:
            code = str(getattr(error, "code", "INTERNAL_ERROR"))
            if code not in protocol.RPC_ERROR_CODES:
                code = "INTERNAL_ERROR"
            raise DsoftbusRuntimeError(
                code,
            ) from error
        if not isinstance(loop, asyncio.AbstractEventLoop) or not hasattr(
            operation, "__await__"
        ):
            close = getattr(operation, "close", None)
            if callable(close):
                close()
            raise DsoftbusRuntimeError("RUNTIME_DRIVER_INVALID")
        return (
            loop,
            operation,
            normalized_context_id,
            normalized_message_id,
        )

    def prepare_continue_agent_task_outbound(
        self,
        device_id: str,
        task_id: str,
        input_request_id: str,
        *,
        text: str = "",
        message_id: str | None = None,
        event_sink: Callable[[Mapping[str, Any]], Any] | None = None,
        input_paths: tuple[str, ...] = (),
    ) -> tuple[asyncio.AbstractEventLoop, Any, str]:
        """Build one owner-loop continuation coroutine for the outbound fence."""

        (
            device_id,
            task_id,
            input_request_id,
            text,
            normalized_message_id,
        ) = self._normalize_agent_task_continuation(
            device_id,
            task_id,
            input_request_id,
            text=text,
            message_id=message_id,
        )
        normalized_input_paths = self._normalize_agent_task_input_paths(input_paths)
        if not text and not normalized_input_paths:
            raise DsoftbusRuntimeError("INVALID_PARAMS")
        with self._condition:
            state = self._state
            use_driver = self._driver_start_called
        if state in {RuntimeState.STOPPING, RuntimeState.STOPPED}:
            raise DsoftbusRuntimeError("RUNTIME_STOPPING")
        if state in {RuntimeState.NEW, RuntimeState.STARTING}:
            raise DsoftbusRuntimeError("PEER_NOT_READY")
        loop_accessor = getattr(self._driver, "outbound_loop", None)
        operation_accessor = getattr(
            self._driver,
            "continue_agent_task_on_owner",
            None,
        )
        if (
            not use_driver
            or not callable(loop_accessor)
            or not callable(operation_accessor)
        ):
            raise DsoftbusRuntimeError("PEER_NOT_READY")
        try:
            loop = loop_accessor()
            operation = operation_accessor(
                device_id,
                task_id,
                input_request_id,
                text=text,
                message_id=normalized_message_id,
                event_sink=event_sink,
                input_paths=normalized_input_paths,
            )
        except Exception as error:
            code = str(getattr(error, "code", "INTERNAL_ERROR"))
            if code not in protocol.RPC_ERROR_CODES:
                code = "INTERNAL_ERROR"
            raise DsoftbusRuntimeError(code) from error
        if not isinstance(loop, asyncio.AbstractEventLoop) or not hasattr(
            operation,
            "__await__",
        ):
            close = getattr(operation, "close", None)
            if callable(close):
                close()
            raise DsoftbusRuntimeError("RUNTIME_DRIVER_INVALID")
        return loop, operation, normalized_message_id

    def _release_endpoint(self) -> BaseException | None:
        with self._condition:
            endpoint = self._endpoint_lock
            self._endpoint_lock = None
        if endpoint is None:
            return None
        try:
            endpoint.release()
        except BaseException as error:
            return error
        return None

    def _rollback_failed_start(self) -> None:
        errors: list[BaseException] = []
        if self._driver_start_called:
            try:
                self._driver.begin_shutdown()
            except BaseException as error:
                errors.append(error)
            try:
                rollback_deadline = self._monotonic() + DSOFTBUS_SHUTDOWN_TIMEOUT_S
                self._driver.stop(lambda: rollback_deadline)
            except BaseException as error:
                errors.append(error)
        endpoint_error = self._release_endpoint()
        if endpoint_error is not None:
            errors.append(endpoint_error)
        with self._condition:
            self._start_in_progress = False
            if self._state == RuntimeState.STOPPING:
                self._state = RuntimeState.STOPPED
                self._stop_complete = True
            else:
                self._state = RuntimeState.NEW
            if errors:
                self._primary_error_code = "START_ROLLBACK_FAILED"
            self._condition.notify_all()

    def start(self) -> Mapping[str, Any]:
        """Acquire one resource set and start it at most once."""
        with self._condition:
            while self._state == RuntimeState.STARTING and self._start_in_progress:
                self._condition.wait()
            if self._state in {RuntimeState.READY, RuntimeState.DEGRADED}:
                return self._snapshot_locked()
            if self._state == RuntimeState.STOPPING:
                raise DsoftbusRuntimeError("RUNTIME_STOPPING")
            if self._state == RuntimeState.STOPPED:
                raise DsoftbusRuntimeError("RUNTIME_STOPPED")
            self._state = RuntimeState.STARTING
            self._start_in_progress = True
            self._degraded_reasons = ()
            self._primary_error_code = ""

        try:
            endpoint = self._endpoint_lock_factory(
                self._state_root, self._runtime_instance_id
            )
            canceled_before_driver = False
            with self._condition:
                self._endpoint_lock = endpoint
                if self._state == RuntimeState.STARTING:
                    self._driver_start_called = True
                else:
                    canceled_before_driver = True
                    self._start_in_progress = False
                    snapshot = self._snapshot_locked()
                    self._condition.notify_all()
            if canceled_before_driver:
                return snapshot
            outcome = self._driver.start(self._runtime_instance_id, endpoint)
            next_state, reasons = _validate_start_outcome(outcome)
        except EndpointLockError as error:
            with self._condition:
                self._start_in_progress = False
                if self._state == RuntimeState.STOPPING:
                    self._state = RuntimeState.STOPPED
                    self._stop_complete = True
                else:
                    self._state = RuntimeState.NEW
                self._condition.notify_all()
            raise DsoftbusRuntimeError(error.code) from error
        except BaseException:
            self._rollback_failed_start()
            raise

        stopping = False
        with self._condition:
            self._start_in_progress = False
            if self._state == RuntimeState.STARTING:
                self._state = next_state
                self._degraded_reasons = reasons
                self._primary_error_code = reasons[0] if reasons else ""
            else:
                stopping = self._state == RuntimeState.STOPPING
            snapshot = self._snapshot_locked()
            self._condition.notify_all()
        if stopping:
            self._invoke_driver_begin_shutdown()
        return snapshot

    def _invoke_driver_begin_shutdown(self) -> BaseException | None:
        with self._condition:
            if not self._driver_start_called:
                return None
            if self._driver_begin_called:
                return self._driver_begin_error
            self._driver_begin_called = True
        try:
            self._driver.begin_shutdown()
        except BaseException as error:
            with self._condition:
                self._driver_begin_error = error
            return error
        return None

    def begin_shutdown(self) -> None:
        """Close new admission while retaining resources needed to drain."""
        invoke_driver = False
        with self._condition:
            if self._state == RuntimeState.NEW:
                self._state = RuntimeState.STOPPED
                self._stop_complete = True
                self._condition.notify_all()
                return
            if self._state == RuntimeState.STOPPED:
                return
            if self._state != RuntimeState.STOPPING:
                self._state = RuntimeState.STOPPING
            invoke_driver = not self._start_in_progress
            self._condition.notify_all()
        if invoke_driver:
            self._invoke_driver_begin_shutdown()

    @staticmethod
    def _validated_deadline(value: float) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("absolute_deadline must be a finite number")
        deadline = float(value)
        if not math.isfinite(deadline):
            raise ValueError("absolute_deadline must be a finite number")
        return deadline

    def _remaining_locked(self) -> float:
        assert self._shutdown_deadline is not None
        return max(0.0, self._shutdown_deadline - self._monotonic())

    def _current_shutdown_deadline(self) -> float:
        with self._condition:
            if self._shutdown_deadline is None:
                raise DsoftbusRuntimeError("SHUTDOWN_DEADLINE_MISSING")
            return self._shutdown_deadline

    def stop(self, absolute_deadline: float) -> Mapping[str, Any]:
        """Stop once, with all callers sharing the earliest absolute deadline."""
        deadline = self._validated_deadline(absolute_deadline)
        completed_snapshot: Mapping[str, Any] | None = None
        completed_error: DsoftbusRuntimeError | None = None
        with self._condition:
            if self._shutdown_deadline is None or deadline < self._shutdown_deadline:
                self._shutdown_deadline = deadline
            if self._state == RuntimeState.NEW:
                self._state = RuntimeState.STOPPED
                self._stop_complete = True
                self._condition.notify_all()
            elif self._state != RuntimeState.STOPPED:
                self._state = RuntimeState.STOPPING

            if self._stop_complete:
                completed_snapshot = self._snapshot_locked()
                completed_error = self._stop_error
            if completed_snapshot is not None:
                pass
            elif self._stop_leader:
                while not self._stop_complete:
                    remaining = self._remaining_locked()
                    if remaining <= 0:
                        raise DsoftbusRuntimeError("SHUTDOWN_TIMEOUT")
                    self._condition.wait(remaining)
                completed_snapshot = self._snapshot_locked()
                completed_error = self._stop_error
            else:
                self._stop_leader = True

            while completed_snapshot is None and self._start_in_progress:
                remaining = self._remaining_locked()
                if remaining <= 0:
                    self._stop_leader = False
                    self._condition.notify_all()
                    raise DsoftbusRuntimeError("SHUTDOWN_TIMEOUT")
                self._condition.wait(remaining)

        if completed_snapshot is not None:
            from .active import clear_active_runtime

            clear_active_runtime(self)
            if completed_error is not None:
                raise completed_error
            return completed_snapshot

        errors: list[BaseException] = []
        begin_error = self._invoke_driver_begin_shutdown()
        if begin_error is not None:
            errors.append(begin_error)
        if self._driver_start_called:
            try:
                self._driver.stop(self._current_shutdown_deadline)
            except BaseException as error:
                errors.append(error)
        endpoint_error = self._release_endpoint()
        if endpoint_error is not None:
            errors.append(endpoint_error)

        with self._condition:
            self._state = RuntimeState.STOPPED
            self._degraded_reasons = ()
            self._primary_error_code = ""
            self._stop_error = (
                DsoftbusRuntimeError("RUNTIME_STOP_FAILED") if errors else None
            )
            self._stop_complete = True
            self._stop_leader = False
            snapshot = self._snapshot_locked()
            self._condition.notify_all()

        from .active import clear_active_runtime

        clear_active_runtime(self)
        if self._stop_error is not None:
            raise self._stop_error
        return snapshot

    def update_provider_runtime(self, context: Any | None) -> None:
        """Replace only the local Provider snapshot on the same Runtime."""
        with self._condition:
            if self._state in {RuntimeState.STOPPING, RuntimeState.STOPPED}:
                raise DsoftbusRuntimeError("RUNTIME_STOPPING")
            previous = self._provider_runtime
            self._provider_runtime = context
        try:
            self._driver.update_provider_runtime(context)
        except BaseException:
            with self._condition:
                if self._provider_runtime is context:
                    self._provider_runtime = previous
            raise

    @staticmethod
    def _validate_local_turn_token(token: str) -> str:
        if not isinstance(token, str) or not token:
            raise DsoftbusRuntimeError("INVALID_PARAMS")
        try:
            encoded = token.encode("utf-8")
        except UnicodeEncodeError as error:
            raise DsoftbusRuntimeError("INVALID_PARAMS") from error
        if len(encoded) > 128 or "\x00" in token:
            raise DsoftbusRuntimeError("INVALID_PARAMS")
        return token

    def local_turn_started(self, token: str) -> None:
        token = self._validate_local_turn_token(token)
        with self._condition:
            state = self._state
            use_driver = self._driver_start_called
        if state in {RuntimeState.STOPPING, RuntimeState.STOPPED}:
            raise DsoftbusRuntimeError("RUNTIME_STOPPING")
        if not use_driver or state in {RuntimeState.NEW, RuntimeState.STARTING}:
            raise DsoftbusRuntimeError("PEER_NOT_READY")
        self._driver.local_turn_started(token)

    def local_turn_finished(self, token: str) -> None:
        token = self._validate_local_turn_token(token)
        with self._condition:
            state = self._state
            use_driver = self._driver_start_called
        if state == RuntimeState.STOPPED:
            raise DsoftbusRuntimeError("RUNTIME_STOPPING")
        if not use_driver or state in {RuntimeState.NEW, RuntimeState.STARTING}:
            raise DsoftbusRuntimeError("PEER_NOT_READY")
        self._driver.local_turn_finished(token)


__all__ = [
    "DsoftbusRuntime",
    "DsoftbusRuntimeError",
    "RuntimeDriver",
    "RuntimeState",
]
