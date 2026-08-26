# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Strict Device Context documents, readers, and verified peer cache.

This module is owner-loop only for mutable operations.  It intentionally has
no Native dependency: SoftBus authenticates the peer and transports bytes,
while this module validates the public Manifest/State documents carried by the
already-bound A2A connection.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import math
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, NoReturn

from . import protocol
from .a2a import A2AError, CoreMethodCall, validate_core_method
from .manifest import (
    LocalManifestTemplate,
    ManifestDescriptor,
    ManifestError,
    PublicManifest,
    validate_public_manifest,
)

# This is an interoperable wire label fixed by the protocol specification.  It
# does not describe a development branch or an implementation iteration.
DEVICE_STATE_SCHEMA = "mclaw.device-state/v1"
STATE_VALID_FOR_MS = 10_000
STATE_READER_TIMEOUT_S = 0.5

_MAX_INT64 = 2**63 - 1
_DEVICE_ID = re.compile(r"^urn:mclaw:device:oh:[0-9a-f]{64}$")
_RESOURCE_ID = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,127}$")
_JSON_KEY = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_RFC3339_UTC = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?Z$"
)
_AVAILABILITY = frozenset({"online", "offline", "unavailable", "unknown", "busy"})
_HEALTH = frozenset({"healthy", "degraded", "fault", "unknown"})
_CONTEXT_AVAILABILITY = frozenset(
    {
        "UNKNOWN",
        "FETCHING",
        "MANIFEST_READY",
        "STATE_FRESH",
        "STATE_STALE",
        "UNAVAILABLE",
        "CONFLICT",
    }
)


class DeviceContextError(RuntimeError):
    """Stable, non-sensitive Device Context failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _fail(code: str) -> NoReturn:
    raise DeviceContextError(code)


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return copy.deepcopy(value)


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze(item) for key, item in value.items()}
        )
    if isinstance(value, (tuple, list)):
        return tuple(_freeze(item) for item in value)
    return copy.deepcopy(value)


def _compact_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, UnicodeEncodeError, ValueError) as error:
        raise DeviceContextError("STATE_SCHEMA_INVALID") from error


def _exact(value: Any, keys: frozenset[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or frozenset(value) != keys:
        _fail("STATE_SCHEMA_INVALID")
    return value


def _strict_integer(value: Any, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _fail("STATE_SCHEMA_INVALID")
    return value


def _timestamp(value: Any) -> str:
    if not isinstance(value, str) or _RFC3339_UTC.fullmatch(value) is None:
        _fail("STATE_SCHEMA_INVALID")
    try:
        datetime.fromisoformat(value)
    except ValueError as error:
        raise DeviceContextError("STATE_SCHEMA_INVALID") from error
    return value


def _canonical_uuid4(value: Any) -> str:
    try:
        return protocol.canonical_uuid4(value, "epoch")
    except protocol.ProtocolError as error:
        raise DeviceContextError("STATE_SCHEMA_INVALID") from error


def _validate_json_object(value: Any) -> dict[str, Any]:
    """Validate the bounded strict-JSON subset used by values and quality."""

    if not isinstance(value, dict):
        _fail("STATE_SCHEMA_INVALID")
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        if depth > 8:
            _fail("STATE_SCHEMA_INVALID")
        if isinstance(current, dict):
            for key, item in current.items():
                if not isinstance(key, str) or _JSON_KEY.fullmatch(key) is None:
                    _fail("STATE_SCHEMA_INVALID")
                stack.append((item, depth + 1))
        elif isinstance(current, list):
            if len(current) > 64:
                _fail("STATE_SCHEMA_INVALID")
            stack.extend((item, depth + 1) for item in current)
        elif isinstance(current, str):
            try:
                size = len(current.encode("utf-8"))
            except UnicodeEncodeError as error:
                raise DeviceContextError("STATE_SCHEMA_INVALID") from error
            if size > 1_024 or "\x00" in current:
                _fail("STATE_SCHEMA_INVALID")
        elif current is None or type(current) in {bool, int}:
            continue
        elif type(current) is float:
            if not math.isfinite(current):
                _fail("STATE_SCHEMA_INVALID")
        else:
            _fail("STATE_SCHEMA_INVALID")
    return copy.deepcopy(value)


def _manifest_parts(
    manifest: PublicManifest | Mapping[str, Any],
) -> tuple[str, int, tuple[str, ...]]:
    document = manifest.document if isinstance(manifest, PublicManifest) else manifest
    if not isinstance(document, Mapping):
        _fail("STATE_MANIFEST_INVALID")
    try:
        device_id = str(document["deviceId"])
        revision = int(document["revision"])
        resources = document["resources"]
    except (KeyError, TypeError, ValueError) as error:
        raise DeviceContextError("STATE_MANIFEST_INVALID") from error
    if (
        _DEVICE_ID.fullmatch(device_id) is None
        or type(document["revision"]) is not int
        or not 1 <= revision <= _MAX_INT64
        or not isinstance(resources, (tuple, list))
    ):
        _fail("STATE_MANIFEST_INVALID")
    resource_ids: list[str] = []
    for resource in resources:
        if not isinstance(resource, Mapping):
            _fail("STATE_MANIFEST_INVALID")
        resource_id = resource.get("resourceId")
        if (
            not isinstance(resource_id, str)
            or _RESOURCE_ID.fullmatch(resource_id) is None
        ):
            _fail("STATE_MANIFEST_INVALID")
        resource_ids.append(resource_id)
    if len(resource_ids) != len(set(resource_ids)):
        _fail("STATE_MANIFEST_INVALID")
    return device_id, revision, tuple(resource_ids)


@dataclass(frozen=True, slots=True)
class DeviceState:
    """A canonical, deeply immutable Device State document."""

    document: Mapping[str, Any]
    canonical_bytes: bytes
    digest: str
    epoch: str
    sequence: int


def validate_device_state(
    value: Any,
    *,
    manifest: PublicManifest | Mapping[str, Any] | None = None,
    expected_device_id: str | None = None,
    expected_resource_ids: tuple[str, ...] | list[str] | None = None,
) -> DeviceState:
    """Validate one complete State document against optional binding facts."""

    if not isinstance(value, Mapping):
        _fail("STATE_SCHEMA_INVALID")
    document = _plain(value)
    state = _exact(
        document,
        frozenset(
            {
                "schemaVersion",
                "deviceId",
                "manifestRevision",
                "epoch",
                "sequence",
                "observedAt",
                "validForMs",
                "resources",
            }
        ),
    )
    if state["schemaVersion"] != DEVICE_STATE_SCHEMA:
        _fail("STATE_SCHEMA_INVALID")
    device_id = state["deviceId"]
    if not isinstance(device_id, str) or _DEVICE_ID.fullmatch(device_id) is None:
        _fail("STATE_SCHEMA_INVALID")
    if expected_device_id is not None and device_id != expected_device_id:
        _fail("STATE_BINDING_MISMATCH")
    manifest_revision = _strict_integer(state["manifestRevision"], 1, _MAX_INT64)
    epoch = _canonical_uuid4(state["epoch"])
    sequence = _strict_integer(state["sequence"], 1, _MAX_INT64)
    _timestamp(state["observedAt"])
    _strict_integer(state["validForMs"], 1_000, 60_000)
    resources = state["resources"]
    if not isinstance(resources, dict) or len(resources) > 128:
        _fail("STATE_SCHEMA_INVALID")
    for resource_id, raw_resource in resources.items():
        if (
            not isinstance(resource_id, str)
            or _RESOURCE_ID.fullmatch(resource_id) is None
        ):
            _fail("STATE_SCHEMA_INVALID")
        resource = _exact(
            raw_resource,
            frozenset({"availability", "health", "values", "quality"}),
        )
        if resource["availability"] not in _AVAILABILITY:
            _fail("STATE_SCHEMA_INVALID")
        if resource["health"] not in _HEALTH:
            _fail("STATE_SCHEMA_INVALID")
        _validate_json_object(resource["values"])
        _validate_json_object(resource["quality"])

    if manifest is not None:
        manifest_device, revision, manifest_resources = _manifest_parts(manifest)
        if device_id != manifest_device or manifest_revision != revision:
            _fail("STATE_MANIFEST_MISMATCH")
        if not set(resources).issubset(manifest_resources):
            _fail("STATE_RESOURCE_UNKNOWN")
    if expected_resource_ids is not None:
        normalized = tuple(expected_resource_ids)
        if len(normalized) != len(set(normalized)) or set(resources) != set(normalized):
            _fail("STATE_RESOURCE_MISMATCH")

    encoded = _compact_json(state)
    if len(encoded) > protocol.DEVICE_DOCUMENT_MAX:
        _fail("STATE_TOO_LARGE")
    return DeviceState(
        document=_freeze(state),
        canonical_bytes=encoded,
        digest=f"sha256:{hashlib.sha256(encoded).hexdigest()}",
        epoch=epoch,
        sequence=sequence,
    )


def _utc_timestamp(now: datetime) -> str:
    if not isinstance(now, datetime) or now.tzinfo is None:
        _fail("STATE_CLOCK_INVALID")
    normalized = now.astimezone(UTC)
    return normalized.isoformat(timespec="milliseconds").replace("+00:00", "Z")


Reader = Callable[[], Mapping[str, Any] | Awaitable[Mapping[str, Any]]]


class LocalDeviceStateService:
    """Bounded owner-loop State reader service for authenticated peers."""

    def __init__(
        self,
        *,
        template: LocalManifestTemplate,
        manifest: PublicManifest,
        runtime_instance_id: str,
        health_snapshot: Callable[[], Mapping[str, Any]],
        monotonic: Callable[[], float] = time.monotonic,
        utc_now: Callable[[], datetime] = lambda: datetime.now(UTC),
        reader_registry: Mapping[str, Reader] | None = None,
        valid_for_ms: int = STATE_VALID_FOR_MS,
    ) -> None:
        if not isinstance(template, LocalManifestTemplate):
            raise TypeError("template must be LocalManifestTemplate")
        if not isinstance(manifest, PublicManifest):
            raise TypeError("manifest must be PublicManifest")
        try:
            self._epoch = protocol.canonical_uuid4(
                runtime_instance_id, "runtimeInstanceId"
            )
        except protocol.ProtocolError as error:
            raise DeviceContextError("STATE_EPOCH_INVALID") from error
        if type(valid_for_ms) is not int or not 1_000 <= valid_for_ms <= 60_000:
            raise ValueError("valid_for_ms must be an integer in 1000..60000")
        if (
            not callable(health_snapshot)
            or not callable(monotonic)
            or not callable(utc_now)
        ):
            raise TypeError("State service callbacks must be callable")
        self._manifest = manifest
        self._health_snapshot = health_snapshot
        self._monotonic = monotonic
        self._utc_now = utc_now
        self._valid_for_ms = valid_for_ms
        self._sequence = 0
        self._global_reads: deque[float] = deque()
        self._peer_reads: dict[str, deque[float]] = {}
        self._inflight: dict[tuple[str, str], asyncio.Task[Mapping[str, Any]]] = {}
        self._reader_call_count = 0

        bindings = template.document["bindings"]
        if not isinstance(bindings, Mapping):
            _fail("STATE_READER_CONFIG_INVALID")
        self._bindings = _freeze(bindings)
        if reader_registry is None:
            registry: dict[str, Reader] = {"system": self._read_system_state}
        else:
            registry = dict(reader_registry)
        if any(
            not isinstance(name, str) or not callable(reader)
            for name, reader in registry.items()
        ):
            raise TypeError("reader_registry must map names to callables")
        for binding in self._bindings.values():
            if (
                not isinstance(binding, Mapping)
                or binding.get("reader") not in registry
            ):
                _fail("STATE_READER_CONFIG_INVALID")
        self._readers = MappingProxyType(registry)

        manifest_device, manifest_revision, manifest_resources = _manifest_parts(
            manifest
        )
        self._device_id = manifest_device
        self._manifest_revision = manifest_revision
        self._resource_ids = manifest_resources

    @property
    def epoch(self) -> str:
        return self._epoch

    @property
    def reader_call_count(self) -> int:
        return self._reader_call_count

    @property
    def sequence(self) -> int:
        return self._sequence

    def _read_system_state(self) -> Mapping[str, Any]:
        snapshot = self._health_snapshot()
        if not isinstance(snapshot, Mapping):
            _fail("STATE_READER_FAILED")
        runtime_ready = snapshot.get("state") == "READY"
        provider_ready = snapshot.get("providerReady") is True
        return MappingProxyType(
            {
                "availability": "online",
                "health": "healthy" if runtime_ready else "degraded",
                "values": MappingProxyType(
                    {
                        "mclawAgent": "ready",
                        "dsoftbusRuntime": "ready" if runtime_ready else "degraded",
                        "providerReady": provider_ready,
                    }
                ),
                "quality": MappingProxyType({"source": "system", "stale": False}),
            }
        )

    @staticmethod
    def _degraded_resource(reader_name: str) -> Mapping[str, Any]:
        return MappingProxyType(
            {
                "availability": "unavailable",
                "health": "degraded",
                "values": MappingProxyType({}),
                "quality": MappingProxyType({"source": reader_name, "stale": True}),
            }
        )

    @staticmethod
    def _static_resource() -> Mapping[str, Any]:
        return MappingProxyType(
            {
                "availability": "unknown",
                "health": "unknown",
                "values": MappingProxyType({}),
                "quality": MappingProxyType({"source": "manifest", "stale": True}),
            }
        )

    def _normalize_resource(self, value: Any) -> Mapping[str, Any]:
        probe = {
            "schemaVersion": DEVICE_STATE_SCHEMA,
            "deviceId": self._device_id,
            "manifestRevision": self._manifest_revision,
            "epoch": self._epoch,
            "sequence": 1,
            "observedAt": "2000-01-01T00:00:00Z",
            "validForMs": self._valid_for_ms,
            "resources": {"host.system": _plain(value)},
        }
        try:
            state = validate_device_state(probe)
        except DeviceContextError as error:
            raise DeviceContextError("STATE_READER_FAILED") from error
        return state.document["resources"]["host.system"]

    async def _invoke_reader(self, reader_name: str) -> Mapping[str, Any]:
        reader = self._readers[reader_name]
        started = self._monotonic()
        self._reader_call_count += 1
        try:
            value = reader()
            if inspect.isawaitable(value):
                value = await asyncio.wait_for(value, timeout=STATE_READER_TIMEOUT_S)
            elapsed = self._monotonic() - started
            if elapsed > STATE_READER_TIMEOUT_S:
                raise TimeoutError
            return self._normalize_resource(value)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - reader faults degrade only this resource.
            return self._degraded_resource(reader_name)

    async def _read_resource(
        self, peer_device_id: str, resource_id: str
    ) -> Mapping[str, Any]:
        binding = self._bindings.get(resource_id)
        if binding is None:
            return self._static_resource()
        reader_name = str(binding["reader"])
        key = (peer_device_id, resource_id)
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.create_task(
                self._invoke_reader(reader_name),
                name=f"mclaw-dsoftbus-state-reader-{resource_id}",
            )
            self._inflight[key] = task

            def clear(done: asyncio.Task[Mapping[str, Any]]) -> None:
                if self._inflight.get(key) is done:
                    del self._inflight[key]

            task.add_done_callback(clear)
        return await asyncio.shield(task)

    @staticmethod
    def _prune(values: deque[float], now: float) -> None:
        while values and values[0] <= now - 60.0:
            values.popleft()

    def _admit_read(self, peer_device_id: str) -> None:
        if (
            not isinstance(peer_device_id, str)
            or _DEVICE_ID.fullmatch(peer_device_id) is None
        ):
            _fail("INVALID_PARAMS")
        now = self._monotonic()
        self._prune(self._global_reads, now)
        peer = self._peer_reads.get(peer_device_id)
        if peer is None:
            reclaimable: list[str] = []
            for key, values in self._peer_reads.items():
                self._prune(values, now)
                if not values:
                    reclaimable.append(key)
            for key in reclaimable:
                if not any(inflight_peer == key for inflight_peer, _ in self._inflight):
                    del self._peer_reads[key]
            if len(self._peer_reads) >= protocol.PEER_REGISTRY_MAX:
                _fail("CAPACITY_BUSY")
            peer = deque()
            self._peer_reads[peer_device_id] = peer
        self._prune(peer, now)
        if (
            len(peer) >= protocol.STATE_READ_PER_PEER_PER_MINUTE
            or len(self._global_reads) >= protocol.STATE_READ_GLOBAL_PER_MINUTE
        ):
            _fail("RATE_LIMITED")
        peer.append(now)
        self._global_reads.append(now)

    @staticmethod
    def _normalized_resource_ids(params: Mapping[str, Any]) -> tuple[str, ...] | None:
        try:
            call = validate_core_method("mclaw.deviceState.get", params)
        except A2AError as error:
            raise DeviceContextError(error.reason) from error
        if not isinstance(call, CoreMethodCall):
            _fail("INVALID_PARAMS")
        if not call.params:
            return None
        return tuple(call.params["resourceIds"])

    async def get_state(
        self, peer_device_id: str, params: Mapping[str, Any]
    ) -> DeviceState:
        """Read one filtered snapshot after validation and quota admission."""

        requested = self._normalized_resource_ids(params)
        resource_ids = self._resource_ids if requested is None else requested
        if not set(resource_ids).issubset(self._resource_ids):
            _fail("INVALID_PARAMS")
        self._admit_read(peer_device_id)
        raw_resources = await asyncio.gather(
            *(
                self._read_resource(peer_device_id, resource_id)
                for resource_id in resource_ids
            )
        )
        if self._sequence >= _MAX_INT64:
            _fail("INTERNAL_ERROR")
        self._sequence += 1
        document = {
            "schemaVersion": DEVICE_STATE_SCHEMA,
            "deviceId": self._device_id,
            "manifestRevision": self._manifest_revision,
            "epoch": self._epoch,
            "sequence": self._sequence,
            "observedAt": _utc_timestamp(self._utc_now()),
            "validForMs": self._valid_for_ms,
            "resources": {
                resource_id: _plain(resource)
                for resource_id, resource in zip(
                    resource_ids, raw_resources, strict=True
                )
            },
        }
        return validate_device_state(
            document,
            manifest=self._manifest,
            expected_device_id=self._device_id,
            expected_resource_ids=list(resource_ids),
        )

    def release_peer(self, peer_device_id: str) -> None:
        """Cancel peer readers and reclaim an empty, expired limiter bucket."""

        for key, task in tuple(self._inflight.items()):
            if key[0] == peer_device_id:
                task.cancel()
        values = self._peer_reads.get(peer_device_id)
        if values is not None:
            self._prune(values, self._monotonic())
            if not values and not any(
                key[0] == peer_device_id for key in self._inflight
            ):
                del self._peer_reads[peer_device_id]

    def close(self) -> None:
        for task in tuple(self._inflight.values()):
            task.cancel()
        self._inflight.clear()
        self._peer_reads.clear()
        self._global_reads.clear()


@dataclass(slots=True)
class _RemoteRecord:
    device_id: str
    runtime_instance_id: str
    generation: int
    descriptor: ManifestDescriptor
    agent_card: Mapping[str, Any]
    agent_card_received_at: float
    manifest: PublicManifest | None = None
    state: DeviceState | None = None
    state_received_at: float | None = None
    availability: str = "FETCHING"
    reason: str = ""
    connected: bool = True


class RemoteDeviceContextStore:
    """Generation-fenced verified peer Device Context cache."""

    def __init__(self, *, monotonic: Callable[[], float] = time.monotonic) -> None:
        if not callable(monotonic):
            raise TypeError("monotonic must be callable")
        self._monotonic = monotonic
        self._records: dict[str, _RemoteRecord] = {}

    @staticmethod
    def _record_matches(
        record: _RemoteRecord, runtime_instance_id: str, generation: int
    ) -> bool:
        return (
            record.runtime_instance_id == runtime_instance_id
            and record.generation == generation
            and record.connected
        )

    def begin_generation(
        self,
        *,
        device_id: str,
        runtime_instance_id: str,
        generation: int,
        descriptor: ManifestDescriptor,
        agent_card: Mapping[str, Any],
    ) -> None:
        if (
            not isinstance(device_id, str)
            or _DEVICE_ID.fullmatch(device_id) is None
            or type(generation) is not int
            or not 1 <= generation <= _MAX_INT64
            or not isinstance(descriptor, ManifestDescriptor)
            or not isinstance(agent_card, Mapping)
        ):
            _fail("CONTEXT_BINDING_INVALID")
        try:
            runtime = protocol.canonical_uuid4(runtime_instance_id, "runtimeInstanceId")
        except protocol.ProtocolError as error:
            raise DeviceContextError("CONTEXT_BINDING_INVALID") from error
        if device_id not in self._records and len(self._records) >= protocol.PEER_REGISTRY_MAX:
            reclaim = next(
                (
                    key
                    for key, record in self._records.items()
                    if not record.connected
                ),
                None,
            )
            if reclaim is None:
                _fail("CAPACITY_BUSY")
            del self._records[reclaim]
        self._records[device_id] = _RemoteRecord(
            device_id=device_id,
            runtime_instance_id=runtime,
            generation=generation,
            descriptor=descriptor,
            agent_card=_freeze(agent_card),
            agent_card_received_at=self._monotonic(),
        )

    def _current(
        self, device_id: str, runtime_instance_id: str, generation: int
    ) -> _RemoteRecord:
        record = self._records.get(device_id)
        if record is None or not self._record_matches(
            record, runtime_instance_id, generation
        ):
            _fail("STALE_GENERATION")
        return record

    def accept_manifest_result(
        self,
        *,
        device_id: str,
        runtime_instance_id: str,
        generation: int,
        result: Any,
    ) -> PublicManifest:
        record = self._current(device_id, runtime_instance_id, generation)
        try:
            value = _plain(result)
            if not isinstance(value, dict) or frozenset(value) not in {
                frozenset({"manifest"}),
                frozenset({"notModified"}),
            }:
                raise DeviceContextError("MANIFEST_RESULT_INVALID")
            if "manifest" in value:
                manifest = validate_public_manifest(value["manifest"])
                descriptor = manifest.descriptor
                if (
                    manifest.document["deviceId"] != device_id
                    or descriptor.revision != record.descriptor.revision
                    or descriptor.digest != record.descriptor.digest
                ):
                    raise DeviceContextError("MANIFEST_CONFLICT")
                if (
                    record.manifest is not None
                    and record.manifest.descriptor.revision == descriptor.revision
                    and record.manifest.descriptor.digest != descriptor.digest
                ):
                    raise DeviceContextError("MANIFEST_CONFLICT")
                record.manifest = manifest
            else:
                not_modified = _exact(
                    value["notModified"], frozenset({"revision", "digest"})
                )
                if (
                    type(not_modified["revision"]) is not int
                    or not_modified["revision"] != record.descriptor.revision
                    or not_modified["digest"] != record.descriptor.digest
                    or record.manifest is None
                ):
                    raise DeviceContextError("MANIFEST_CONFLICT")
                manifest = record.manifest
        except (ManifestError, DeviceContextError):
            record.availability = "CONFLICT"
            record.reason = "MANIFEST_CONFLICT"
            raise DeviceContextError("MANIFEST_CONFLICT")
        record.availability = "MANIFEST_READY"
        record.reason = ""
        return manifest

    def accept_state_result(
        self,
        *,
        device_id: str,
        runtime_instance_id: str,
        generation: int,
        result: Any,
        expected_resource_ids: tuple[str, ...] | list[str] | None = None,
    ) -> DeviceState:
        record = self._current(device_id, runtime_instance_id, generation)
        if record.manifest is None:
            record.availability = "UNAVAILABLE"
            record.reason = "MANIFEST_NOT_READY"
            _fail("PEER_NOT_READY")
        value = _plain(result)
        if not isinstance(value, dict) or frozenset(value) != frozenset({"state"}):
            record.availability = "UNAVAILABLE"
            record.reason = "STATE_RESULT_INVALID"
            _fail("INVALID_AGENT_RESPONSE")
        try:
            state = validate_device_state(
                value["state"],
                manifest=record.manifest,
                expected_device_id=device_id,
                expected_resource_ids=expected_resource_ids,
            )
        except DeviceContextError as error:
            record.availability = "UNAVAILABLE"
            record.reason = error.code
            raise
        if state.epoch != runtime_instance_id:
            record.availability = "UNAVAILABLE"
            record.reason = "STATE_EPOCH_MISMATCH"
            _fail("INVALID_AGENT_RESPONSE")
        current = record.state
        if current is not None and current.epoch == state.epoch:
            if state.sequence < current.sequence:
                record.reason = "STATE_SEQUENCE_STALE"
                return current
            if state.sequence == current.sequence:
                if state.digest != current.digest:
                    record.availability = "CONFLICT"
                    record.reason = "STATE_SEQUENCE_CONFLICT"
                    _fail("MANIFEST_CONFLICT")
                return current
        record.state = state
        record.state_received_at = self._monotonic()
        record.availability = "STATE_FRESH"
        record.reason = ""
        return state

    def record_error(
        self,
        *,
        device_id: str,
        runtime_instance_id: str,
        generation: int,
        method: str,
        reason: str,
    ) -> None:
        record = self._current(device_id, runtime_instance_id, generation)
        if method == "mclaw.deviceManifest.get":
            record.availability = (
                "CONFLICT" if reason == "MANIFEST_CONFLICT" else "UNAVAILABLE"
            )
            record.reason = reason
            return
        if method == "mclaw.deviceState.get":
            if record.state is None:
                record.availability = "UNAVAILABLE"
            elif not self._state_fresh(record):
                record.availability = "STATE_STALE"
            record.reason = reason

    def mark_disconnected(self, device_id: str, generation: int) -> None:
        record = self._records.get(device_id)
        if record is None or record.generation != generation:
            return
        record.connected = False
        record.availability = (
            "STATE_STALE" if record.state is not None else "UNAVAILABLE"
        )
        record.reason = "STALE_GENERATION"

    def _state_fresh(self, record: _RemoteRecord) -> bool:
        if (
            record.state is None
            or record.state_received_at is None
            or not record.connected
        ):
            return False
        expires = (
            record.state_received_at + int(record.state.document["validForMs"]) / 1000.0
        )
        return self._monotonic() < expires

    def _refresh_availability(self, record: _RemoteRecord) -> None:
        if record.availability == "CONFLICT":
            return
        if record.state is not None:
            if self._state_fresh(record):
                record.availability = "STATE_FRESH"
                if record.reason == "STATE_TTL_EXPIRED":
                    record.reason = ""
            else:
                record.availability = "STATE_STALE"
                if record.connected:
                    record.reason = "STATE_TTL_EXPIRED"
            return
        if record.manifest is not None and record.connected:
            record.availability = "MANIFEST_READY"

    def availability(self, device_id: str) -> tuple[str, str]:
        record = self._records.get(device_id)
        if record is None:
            return "UNKNOWN", ""
        self._refresh_availability(record)
        return record.availability, record.reason

    def manifest_condition(self, device_id: str) -> Mapping[str, Any]:
        """Return the exact conditional request fields for the current cache."""

        record = self._records.get(device_id)
        if record is None or record.manifest is None or not record.connected:
            return MappingProxyType({})
        return MappingProxyType(
            {
                "ifRevision": record.manifest.descriptor.revision,
                "ifDigest": record.manifest.descriptor.digest,
            }
        )

    def state_fresh_count(self) -> int:
        return sum(
            1
            for record in self._records.values()
            if (
                self._refresh_availability(record) is None
                and record.availability == "STATE_FRESH"
            )
        )

    def snapshot(self, device_id: str) -> Mapping[str, Any]:
        record = self._records.get(device_id)
        if record is None:
            _fail("PEER_NOT_READY")
        self._refresh_availability(record)
        remote_data = bool(
            record.agent_card or record.manifest is not None or record.state is not None
        )
        return MappingProxyType(
            {
                "success": True,
                "agentAvailability": "READY" if record.connected else "UNAVAILABLE",
                "deviceContextAvailability": record.availability,
                "agentCard": _freeze(record.agent_card),
                "deviceManifest": (
                    None
                    if record.manifest is None
                    else _freeze(record.manifest.document)
                ),
                "deviceState": None
                if record.state is None
                else _freeze(record.state.document),
                "reason": record.reason,
                "_mclawProvenance": MappingProxyType(
                    {
                        "kind": "peer",
                        "source": "mclaw.dsoftbus.runtime",
                        "peerDeviceId": record.device_id,
                        "peerRuntimeInstanceId": record.runtime_instance_id,
                        "connectionGeneration": record.generation,
                        "receivedVia": "softbus",
                        "verifiedBinding": True,
                    }
                ),
                "_untrustedRemoteData": remote_data,
            }
        )

    def public_summary(self, device_id: str) -> Mapping[str, Any]:
        record = self._records.get(device_id)
        if record is None:
            return MappingProxyType(
                {"deviceContextAvailability": "UNKNOWN", "reason": ""}
            )
        self._refresh_availability(record)
        provider_ready: bool | None = None
        try:
            provider_ready = bool(
                record.agent_card["capabilities"]["extensions"][0]["params"][
                    "providerReady"
                ]
            )
        except (KeyError, IndexError, TypeError):
            provider_ready = None
        os_name: str | None = None
        os_version: str | None = None
        if record.manifest is not None:
            os_value = record.manifest.document["device"]["os"]
            os_name = str(os_value["name"])
            os_version = str(os_value["version"])
        return MappingProxyType(
            {
                "deviceContextAvailability": record.availability,
                "manifestRevision": (
                    None
                    if record.manifest is None
                    else record.manifest.descriptor.revision
                ),
                "osFamily": os_name,
                "osVersion": os_version,
                "providerReady": provider_ready,
                "providerReadyStale": (
                    not record.connected
                    or self._monotonic() - record.agent_card_received_at > 10.0
                ),
                "reason": record.reason,
            }
        )

    def clear(self) -> None:
        self._records.clear()


__all__ = [
    "DEVICE_STATE_SCHEMA",
    "STATE_READER_TIMEOUT_S",
    "STATE_VALID_FOR_MS",
    "DeviceContextError",
    "DeviceState",
    "LocalDeviceStateService",
    "RemoteDeviceContextStore",
    "validate_device_state",
]
