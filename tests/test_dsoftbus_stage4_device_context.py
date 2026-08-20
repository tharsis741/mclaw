# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import pytest

from mclaw.dsoftbus import protocol
from mclaw.dsoftbus.a2a import build_agent_card
from mclaw.dsoftbus.binding import LocalBindingIdentity, derive_public_agent_id
from mclaw.dsoftbus.device_context import (
    DEVICE_STATE_SCHEMA,
    DeviceContextError,
    LocalDeviceStateService,
    RemoteDeviceContextStore,
    validate_device_state,
)
from mclaw.dsoftbus.discovery_resources import DiscoveryOwnerResources, _Connection
from mclaw.dsoftbus.manifest import build_public_manifest, parse_local_manifest_template
from mclaw.dsoftbus.runtime import DsoftbusRuntime, DsoftbusRuntimeError
from mclaw.dsoftbus.softbus_binding import (
    OutboundFrame,
    SoftBusA2ABinding,
    SoftBusBindingError,
    SoftBusSendScheduler,
)

_DEVICE_A = "urn:mclaw:device:oh:" + "a" * 64
_DEVICE_B = "urn:mclaw:device:oh:" + "b" * 64
_RUNTIME_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
_RUNTIME_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"

_MANIFEST_YAML = b"""\
schemaVersion: mclaw.device-manifest/v1
revision: 7
generatedAt: "2026-08-11T00:00:00Z"
device:
  manufacturer: Kaihong
  model: Board
  displayName: Device
  os:
    name: KaihongOS
    version: "6.1"
    apiLevel: 23
    arch: aarch64
resources:
  - resourceId: host.system
    type: system
    name: Host system
    capabilities: [status]
    operations: [read]
  - resourceId: static.info
    type: metadata
    name: Static info
    capabilities: [status]
    operations: [read]
bindings:
  host.system:
    reader: system
    config: {}
"""


def _template():
    return parse_local_manifest_template(_MANIFEST_YAML)


def _manifest(device_id: str):
    return build_public_manifest(_template(), device_id)


def _resource(*, provider_ready: bool = True) -> dict[str, Any]:
    return {
        "availability": "online",
        "health": "healthy",
        "values": {
            "mclawAgent": "ready",
            "dsoftbusRuntime": "ready",
            "providerReady": provider_ready,
        },
        "quality": {"source": "system", "stale": False},
    }


def _state_document(
    *,
    device_id: str = _DEVICE_B,
    runtime_id: str = _RUNTIME_B,
    sequence: int = 1,
    valid_for_ms: int = 10_000,
) -> dict[str, Any]:
    return {
        "schemaVersion": DEVICE_STATE_SCHEMA,
        "deviceId": device_id,
        "manifestRevision": 7,
        "epoch": runtime_id,
        "sequence": sequence,
        "observedAt": "2026-08-11T00:00:00.000Z",
        "validForMs": valid_for_ms,
        "resources": {
            "host.system": _resource(),
            "static.info": {
                "availability": "unknown",
                "health": "unknown",
                "values": {},
                "quality": {"source": "manifest", "stale": True},
            },
        },
    }


def test_device_state_validation_is_exact_bounded_and_manifest_bound() -> None:
    manifest = _manifest(_DEVICE_B)
    state = validate_device_state(
        _state_document(), manifest=manifest, expected_device_id=_DEVICE_B
    )
    assert state.epoch == _RUNTIME_B
    assert state.sequence == 1
    assert len(state.canonical_bytes) <= protocol.DEVICE_DOCUMENT_MAX
    assert state.digest.startswith("sha256:")

    extra = _state_document()
    extra["extra"] = True
    with pytest.raises(DeviceContextError) as invalid_extra:
        validate_device_state(extra, manifest=manifest)
    assert invalid_extra.value.code == "STATE_SCHEMA_INVALID"

    unknown = _state_document()
    unknown["resources"]["unknown.resource"] = _resource()
    with pytest.raises(DeviceContextError) as invalid_resource:
        validate_device_state(unknown, manifest=manifest)
    assert invalid_resource.value.code == "STATE_RESOURCE_UNKNOWN"

    too_deep = _state_document()
    too_deep["resources"]["host.system"]["values"] = {
        "a": {"b": {"c": {"d": {"e": {"f": {"g": {"h": 1}}}}}}}
    }
    with pytest.raises(DeviceContextError) as invalid_depth:
        validate_device_state(too_deep, manifest=manifest)
    assert invalid_depth.value.code == "STATE_SCHEMA_INVALID"


@pytest.mark.asyncio
async def test_local_state_service_system_reader_filter_sequence_and_no_leak() -> None:
    manifest = _manifest(_DEVICE_A)
    service = LocalDeviceStateService(
        template=_template(),
        manifest=manifest,
        runtime_instance_id=_RUNTIME_A,
        health_snapshot=lambda: {
            "state": "READY",
            "providerReady": True,
            "provider": {"apiKey": "must-not-leak", "model": "private"},
        },
        utc_now=lambda: datetime(2026, 8, 11, tzinfo=UTC),
    )

    first = await service.get_state(_DEVICE_B, {})
    second = await service.get_state(_DEVICE_B, {"resourceIds": ["host.system"]})

    assert first.sequence == 1 and second.sequence == 2
    assert set(first.document["resources"]) == {"host.system", "static.info"}
    assert set(second.document["resources"]) == {"host.system"}
    system = second.document["resources"]["host.system"]
    assert dict(system["values"]) == {
        "mclawAgent": "ready",
        "dsoftbusRuntime": "ready",
        "providerReady": True,
    }
    encoded = second.canonical_bytes.decode("utf-8")
    assert "apiKey" not in encoded and "private" not in encoded
    assert service.reader_call_count == 2

    before = service.reader_call_count
    with pytest.raises(DeviceContextError) as unknown:
        await service.get_state(_DEVICE_B, {"resourceIds": ["missing.resource"]})
    assert unknown.value.code == "INVALID_PARAMS"
    assert service.reader_call_count == before


@pytest.mark.asyncio
async def test_same_peer_resource_is_single_flight_but_documents_are_unique() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def reader():
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return _resource()

    service = LocalDeviceStateService(
        template=_template(),
        manifest=_manifest(_DEVICE_A),
        runtime_instance_id=_RUNTIME_A,
        health_snapshot=lambda: {"state": "READY", "providerReady": False},
        reader_registry={"system": reader},
    )
    one = asyncio.create_task(
        service.get_state(_DEVICE_B, {"resourceIds": ["host.system"]})
    )
    two = asyncio.create_task(
        service.get_state(_DEVICE_B, {"resourceIds": ["host.system"]})
    )
    await started.wait()
    release.set()
    results = await asyncio.gather(one, two)

    assert calls == service.reader_call_count == 1
    assert {result.sequence for result in results} == {1, 2}
    assert results[0].document["resources"] == results[1].document["resources"]


@pytest.mark.asyncio
async def test_state_rate_limit_rejects_without_reader_or_quota_mutation() -> None:
    clock = [100.0]
    calls = 0

    def reader():
        nonlocal calls
        calls += 1
        return _resource()

    service = LocalDeviceStateService(
        template=_template(),
        manifest=_manifest(_DEVICE_A),
        runtime_instance_id=_RUNTIME_A,
        health_snapshot=lambda: {"state": "READY", "providerReady": False},
        monotonic=lambda: clock[0],
        reader_registry={"system": reader},
    )
    for _ in range(protocol.STATE_READ_PER_PEER_PER_MINUTE):
        await service.get_state(_DEVICE_B, {"resourceIds": ["host.system"]})
        await asyncio.sleep(0)
    before = service.reader_call_count
    with pytest.raises(DeviceContextError) as limited:
        await service.get_state(_DEVICE_B, {"resourceIds": ["host.system"]})
    assert limited.value.code == "RATE_LIMITED"
    assert service.reader_call_count == before == calls

    clock[0] = 160.001
    recovered = await service.get_state(_DEVICE_B, {"resourceIds": ["host.system"]})
    assert recovered.sequence == protocol.STATE_READ_PER_PEER_PER_MINUTE + 1


def test_remote_context_cache_enforces_descriptor_sequence_epoch_and_ttl() -> None:
    clock = [10.0]
    manifest = _manifest(_DEVICE_B)
    card = build_agent_card(
        device_id=_DEVICE_B,
        agent_id=derive_public_agent_id(_DEVICE_B),
        provider_ready=True,
        provider_readiness_code="",
    )
    store = RemoteDeviceContextStore(monotonic=lambda: clock[0])
    store.begin_generation(
        device_id=_DEVICE_B,
        runtime_instance_id=_RUNTIME_B,
        generation=3,
        descriptor=manifest.descriptor,
        agent_card=card.document,
    )
    store.accept_manifest_result(
        device_id=_DEVICE_B,
        runtime_instance_id=_RUNTIME_B,
        generation=3,
        result={"manifest": json.loads(manifest.canonical_bytes)},
    )
    first = store.accept_state_result(
        device_id=_DEVICE_B,
        runtime_instance_id=_RUNTIME_B,
        generation=3,
        result={"state": _state_document(valid_for_ms=1_000)},
    )
    assert first.sequence == 1
    assert store.snapshot(_DEVICE_B)["deviceContextAvailability"] == "STATE_FRESH"

    stale_response = store.accept_state_result(
        device_id=_DEVICE_B,
        runtime_instance_id=_RUNTIME_B,
        generation=3,
        result={"state": _state_document(sequence=1, valid_for_ms=1_000)},
    )
    assert stale_response.digest == first.digest
    clock[0] = 11.001
    snapshot = store.snapshot(_DEVICE_B)
    assert snapshot["agentAvailability"] == "READY"
    assert snapshot["deviceContextAvailability"] == "STATE_STALE"
    assert snapshot["reason"] == "STATE_TTL_EXPIRED"

    wrong_epoch = _state_document(sequence=2)
    wrong_epoch["epoch"] = _RUNTIME_A
    with pytest.raises(DeviceContextError) as epoch:
        store.accept_state_result(
            device_id=_DEVICE_B,
            runtime_instance_id=_RUNTIME_B,
            generation=3,
            result={"state": wrong_epoch},
        )
    assert epoch.value.code == "INVALID_AGENT_RESPONSE"
    assert store.snapshot(_DEVICE_B)["agentAvailability"] == "READY"


def test_manifest_full_not_modified_and_conflict_are_descriptor_bound() -> None:
    manifest = _manifest(_DEVICE_B)
    store = RemoteDeviceContextStore()
    store.begin_generation(
        device_id=_DEVICE_B,
        runtime_instance_id=_RUNTIME_B,
        generation=9,
        descriptor=manifest.descriptor,
        agent_card={"claimed": "remote"},
    )
    accepted = store.accept_manifest_result(
        device_id=_DEVICE_B,
        runtime_instance_id=_RUNTIME_B,
        generation=9,
        result={"manifest": json.loads(manifest.canonical_bytes)},
    )
    assert accepted.canonical_bytes == manifest.canonical_bytes
    assert dict(store.manifest_condition(_DEVICE_B)) == {
        "ifRevision": manifest.descriptor.revision,
        "ifDigest": manifest.descriptor.digest,
    }
    assert (
        store.accept_manifest_result(
            device_id=_DEVICE_B,
            runtime_instance_id=_RUNTIME_B,
            generation=9,
            result={
                "notModified": {
                    "revision": manifest.descriptor.revision,
                    "digest": manifest.descriptor.digest,
                }
            },
        ).canonical_bytes
        == manifest.canonical_bytes
    )

    with pytest.raises(DeviceContextError) as conflict:
        store.accept_manifest_result(
            device_id=_DEVICE_B,
            runtime_instance_id=_RUNTIME_B,
            generation=9,
            result={
                "notModified": {
                    "revision": manifest.descriptor.revision,
                    "digest": "sha256:" + "0" * 64,
                }
            },
        )
    assert conflict.value.code == "MANIFEST_CONFLICT"
    assert store.availability(_DEVICE_B) == ("CONFLICT", "MANIFEST_CONFLICT")


def test_state_sequence_conflict_lower_sequence_and_exact_ttl_boundary() -> None:
    clock = [20.0]
    manifest = _manifest(_DEVICE_B)
    store = RemoteDeviceContextStore(monotonic=lambda: clock[0])
    store.begin_generation(
        device_id=_DEVICE_B,
        runtime_instance_id=_RUNTIME_B,
        generation=4,
        descriptor=manifest.descriptor,
        agent_card={"claimed": "remote"},
    )
    store.accept_manifest_result(
        device_id=_DEVICE_B,
        runtime_instance_id=_RUNTIME_B,
        generation=4,
        result={"manifest": json.loads(manifest.canonical_bytes)},
    )
    current = store.accept_state_result(
        device_id=_DEVICE_B,
        runtime_instance_id=_RUNTIME_B,
        generation=4,
        result={"state": _state_document(sequence=2, valid_for_ms=1_000)},
    )
    lower = store.accept_state_result(
        device_id=_DEVICE_B,
        runtime_instance_id=_RUNTIME_B,
        generation=4,
        result={"state": _state_document(sequence=1, valid_for_ms=60_000)},
    )
    assert lower.digest == current.digest
    clock[0] = 21.0
    assert store.availability(_DEVICE_B) == ("STATE_STALE", "STATE_TTL_EXPIRED")

    conflicting = _state_document(sequence=2, valid_for_ms=1_000)
    conflicting["resources"]["host.system"]["values"]["providerReady"] = False
    with pytest.raises(DeviceContextError) as conflict:
        store.accept_state_result(
            device_id=_DEVICE_B,
            runtime_instance_id=_RUNTIME_B,
            generation=4,
            result={"state": conflicting},
        )
    assert conflict.value.code == "MANIFEST_CONFLICT"
    assert store.availability(_DEVICE_B) == (
        "CONFLICT",
        "STATE_SEQUENCE_CONFLICT",
    )


def test_new_runtime_generation_accepts_sequence_one_in_new_epoch() -> None:
    manifest = _manifest(_DEVICE_B)
    store = RemoteDeviceContextStore()
    for generation, runtime_id in ((1, _RUNTIME_B), (2, _RUNTIME_A)):
        store.begin_generation(
            device_id=_DEVICE_B,
            runtime_instance_id=runtime_id,
            generation=generation,
            descriptor=manifest.descriptor,
            agent_card={"generation": generation},
        )
        store.accept_manifest_result(
            device_id=_DEVICE_B,
            runtime_instance_id=runtime_id,
            generation=generation,
            result={"manifest": json.loads(manifest.canonical_bytes)},
        )
        state = _state_document(runtime_id=runtime_id, sequence=1)
        accepted = store.accept_state_result(
            device_id=_DEVICE_B,
            runtime_instance_id=runtime_id,
            generation=generation,
            result={"state": state},
        )
        assert accepted.sequence == 1 and accepted.epoch == runtime_id


@pytest.mark.asyncio
async def test_reader_error_and_timeout_degrade_only_requested_resource(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mclaw.dsoftbus import device_context

    monkeypatch.setattr(device_context, "STATE_READER_TIMEOUT_S", 0.01)

    async def timeout_reader() -> Mapping[str, Any]:
        await asyncio.sleep(1)
        return _resource()

    service = LocalDeviceStateService(
        template=_template(),
        manifest=_manifest(_DEVICE_A),
        runtime_instance_id=_RUNTIME_A,
        health_snapshot=lambda: {"state": "READY", "providerReady": True},
        reader_registry={"system": timeout_reader},
    )
    state = await service.get_state(
        _DEVICE_B, {"resourceIds": ["host.system", "static.info"]}
    )
    resources = state.document["resources"]
    assert resources["host.system"] == {
        "availability": "unavailable",
        "health": "degraded",
        "values": {},
        "quality": {"source": "system", "stale": True},
    }
    assert resources["static.info"]["health"] == "unknown"
    assert service.reader_call_count == 1

    def failed_reader() -> Mapping[str, Any]:
        raise RuntimeError("private reader failure")

    failed = LocalDeviceStateService(
        template=_template(),
        manifest=_manifest(_DEVICE_A),
        runtime_instance_id=_RUNTIME_A,
        health_snapshot=lambda: {"state": "READY", "providerReady": True},
        reader_registry={"system": failed_reader},
    )
    degraded = await failed.get_state(
        _DEVICE_B, {"resourceIds": ["host.system"]}
    )
    assert degraded.document["resources"]["host.system"]["health"] == "degraded"


@pytest.mark.asyncio
async def test_global_state_rate_limit_is_atomic_and_window_boundary_is_inclusive() -> None:
    clock = [100.0]
    calls = 0

    def reader() -> Mapping[str, Any]:
        nonlocal calls
        calls += 1
        return _resource()

    service = LocalDeviceStateService(
        template=_template(),
        manifest=_manifest(_DEVICE_A),
        runtime_instance_id=_RUNTIME_A,
        health_snapshot=lambda: {"state": "READY", "providerReady": True},
        monotonic=lambda: clock[0],
        reader_registry={"system": reader},
    )
    peers = ["urn:mclaw:device:oh:" + value * 64 for value in "bcde"]
    for peer in peers:
        for _ in range(protocol.STATE_READ_PER_PEER_PER_MINUTE):
            await service.get_state(peer, {"resourceIds": ["host.system"]})
    assert calls == protocol.STATE_READ_GLOBAL_PER_MINUTE
    with pytest.raises(DeviceContextError) as limited:
        await service.get_state(
            "urn:mclaw:device:oh:" + "f" * 64,
            {"resourceIds": ["host.system"]},
        )
    assert limited.value.code == "RATE_LIMITED"
    assert calls == protocol.STATE_READ_GLOBAL_PER_MINUTE

    clock[0] = 160.0
    recovered = await service.get_state(
        "urn:mclaw:device:oh:" + "f" * 64,
        {"resourceIds": ["host.system"]},
    )
    assert recovered.sequence == protocol.STATE_READ_GLOBAL_PER_MINUTE + 1


def test_state_document_limit_and_local_provenance_override() -> None:
    oversized = _state_document()
    oversized["resources"]["host.system"]["values"] = {
        f"field{index}": "x" * 1_024 for index in range(30)
    }
    with pytest.raises(DeviceContextError) as too_large:
        validate_device_state(oversized, manifest=_manifest(_DEVICE_B))
    assert too_large.value.code == "STATE_TOO_LARGE"

    manifest = _manifest(_DEVICE_B)
    store = RemoteDeviceContextStore()
    store.begin_generation(
        device_id=_DEVICE_B,
        runtime_instance_id=_RUNTIME_B,
        generation=5,
        descriptor=manifest.descriptor,
        agent_card={
            "_mclawProvenance": {"verifiedBinding": False},
            "claimed": "remote",
        },
    )
    snapshot = store.snapshot(_DEVICE_B)
    assert snapshot["_mclawProvenance"] == {
        "kind": "peer",
        "source": "mclaw.dsoftbus.runtime",
        "peerDeviceId": _DEVICE_B,
        "peerRuntimeInstanceId": _RUNTIME_B,
        "connectionGeneration": 5,
        "receivedVia": "softbus",
        "verifiedBinding": True,
    }
    assert snapshot["_untrustedRemoteData"] is True


def test_agent_card_provider_hint_becomes_stale_after_ten_seconds() -> None:
    clock = [50.0]
    manifest = _manifest(_DEVICE_B)
    card = build_agent_card(
        device_id=_DEVICE_B,
        agent_id=derive_public_agent_id(_DEVICE_B),
        provider_ready=True,
        provider_readiness_code="",
    )
    store = RemoteDeviceContextStore(monotonic=lambda: clock[0])
    store.begin_generation(
        device_id=_DEVICE_B,
        runtime_instance_id=_RUNTIME_B,
        generation=1,
        descriptor=manifest.descriptor,
        agent_card=card.document,
    )
    assert store.public_summary(_DEVICE_B)["providerReadyStale"] is False
    clock[0] = 60.0
    assert store.public_summary(_DEVICE_B)["providerReadyStale"] is False
    clock[0] = 60.001
    assert store.public_summary(_DEVICE_B)["providerReadyStale"] is True


def test_device_context_cache_is_peer_bounded_and_reclaims_disconnected_record() -> None:
    manifest = _manifest(_DEVICE_B)
    store = RemoteDeviceContextStore()
    device_ids = [
        "urn:mclaw:device:oh:" + f"{index:064x}"
        for index in range(1, protocol.PEER_REGISTRY_MAX + 2)
    ]
    for device_id in device_ids[: protocol.PEER_REGISTRY_MAX]:
        store.begin_generation(
            device_id=device_id,
            runtime_instance_id=_RUNTIME_B,
            generation=1,
            descriptor=manifest.descriptor,
            agent_card={},
        )
    with pytest.raises(DeviceContextError) as full:
        store.begin_generation(
            device_id=device_ids[-1],
            runtime_instance_id=_RUNTIME_B,
            generation=1,
            descriptor=manifest.descriptor,
            agent_card={},
        )
    assert full.value.code == "CAPACITY_BUSY"

    store.mark_disconnected(device_ids[0], 1)
    store.begin_generation(
        device_id=device_ids[-1],
        runtime_instance_id=_RUNTIME_B,
        generation=1,
        descriptor=manifest.descriptor,
        agent_card={},
    )
    with pytest.raises(DeviceContextError) as reclaimed:
        store.snapshot(device_ids[0])
    assert reclaimed.value.code == "PEER_NOT_READY"


def test_response_capacity_is_reserved_before_handler_and_released_once() -> None:
    scheduler = SoftBusSendScheduler()
    scheduler.register(17, 3)
    response_ids = [
        f"00000000-0000-4000-8000-{index:012x}" for index in range(1, 34)
    ]
    for response_id in response_ids[: protocol.SOCKET_SEND_QUEUE_MAX]:
        scheduler.reserve_response(
            17,
            3,
            response_id,
            protocol.REMOTE_FRAME_MAX,
        )
    assert dict(scheduler.diagnostic_snapshot()) == {
        "sendBusinessQueueBytes": protocol.SOCKET_SEND_QUEUE_BYTES_MAX,
        "sendBusinessQueueCount": protocol.SOCKET_SEND_QUEUE_MAX,
        "sendControlQueueBytes": 0,
        "sendControlQueueCount": 0,
        "sendQueueOverflowCount": 0,
    }
    with pytest.raises(SoftBusBindingError) as full:
        scheduler.reserve_response(
            17,
            3,
            response_ids[-1],
            protocol.REMOTE_FRAME_MAX,
        )
    assert full.value.code == "CAPACITY_BUSY"

    scheduler.begin_shutdown()
    scheduler.enqueue(
        17,
        3,
        OutboundFrame(b"terminal", response_reservation_id=response_ids[0]),
    )
    sent: list[bytes] = []
    completion = scheduler.drain_one(
        lambda _socket, data: sent.append(data) or len(data)
    )
    assert completion is not None and sent == [b"terminal"]
    after_send = scheduler.diagnostic_snapshot()
    assert after_send["sendBusinessQueueCount"] == 31
    assert after_send["sendBusinessQueueBytes"] == 31 * protocol.REMOTE_FRAME_MAX
    assert scheduler.cancel_response_reservation(17, 3, response_ids[1]) is True
    assert scheduler.cancel_response_reservation(17, 3, response_ids[1]) is False
    with pytest.raises(SoftBusBindingError) as stopped:
        scheduler.enqueue(17, 3, OutboundFrame(b"new-business"))
    assert stopped.value.code == "RUNTIME_STOPPING"


def test_device_state_service_is_not_called_before_response_and_extension_gates() -> None:
    side_a, side_b = _ready_bindings()
    service = LocalDeviceStateService(
        template=_template(),
        manifest=_manifest(_DEVICE_B),
        runtime_instance_id=_RUNTIME_B,
        health_snapshot=lambda: {"state": "READY", "providerReady": True},
    )
    resources = DiscoveryOwnerResources(supervisor=cast(Any, object()))
    resources._state_service = service
    resources._send_scheduler.register(17, 1)
    connection = _Connection(
        device_id=_DEVICE_A,
        generation=1,
        mtu=protocol.REMOTE_FRAME_MAX,
        network_id="private-network",
        socket=17,
        binding=side_b,
    )
    for index in range(protocol.SOCKET_SEND_QUEUE_MAX):
        resources._send_scheduler.reserve_response(
            17,
            1,
            f"dddddddd-dddd-4ddd-8ddd-{index:012x}",
            protocol.REMOTE_FRAME_MAX,
        )
    request = side_a.request_application(
        "mclaw.deviceState.get",
        {},
        extensions=(protocol.DEVICE_CONTEXT_EXTENSION_URI,),
    )
    inbound = side_b.receive(request.frame.data).application_request
    assert inbound is not None
    rejected = resources._handle_application_request(connection, inbound)
    assert len(rejected) == 1 and rejected[0].queue == "control"
    assert service.reader_call_count == 0
    assert resources._inbound_application_tasks == {}

    resources._send_scheduler.unregister(17, 1)
    resources._send_scheduler.register(17, 1)
    no_extension = side_a.request_application("mclaw.deviceState.get", {})
    unnegotiated = side_b.receive(no_extension.frame.data).application_request
    assert unnegotiated is not None
    extension_error = resources._handle_application_request(connection, unnegotiated)
    assert len(extension_error) == 1
    assert extension_error[0].response_reservation_id == no_extension.request_id
    assert service.reader_call_count == 0
    assert resources._inbound_application_tasks == {}
    resources._send_scheduler.enqueue(17, 1, extension_error[0])
    resources._send_scheduler.drain_one(lambda _socket, data: len(data))
    assert resources._send_scheduler.diagnostic_snapshot()[
        "sendBusinessQueueCount"
    ] == 0


@pytest.mark.asyncio
async def test_runtime_and_tool_device_context_facades_are_copying_and_bounded(
    tmp_path: Any,
) -> None:
    from mclaw.dsoftbus.active import clear_active_runtime, install_active_runtime
    from mclaw.dsoftbus.tools import get_device_context_handler

    context = {
        "success": True,
        "agentAvailability": "READY",
        "deviceContextAvailability": "STATE_FRESH",
        "agentCard": {"name": "remote"},
        "deviceManifest": {"revision": 7},
        "deviceState": {"sequence": 1},
        "reason": "",
        "_mclawProvenance": {
            "kind": "peer",
            "source": "mclaw.dsoftbus.runtime",
            "peerDeviceId": _DEVICE_B,
            "peerRuntimeInstanceId": _RUNTIME_B,
            "connectionGeneration": 1,
            "receivedVia": "softbus",
            "verifiedBinding": True,
        },
        "_untrustedRemoteData": True,
    }

    class Endpoint:
        def release(self) -> None:
            return None

    class Driver:
        def __init__(self) -> None:
            self.refresh_calls = 0
            self.cancel_refresh = False

        def start(self, _runtime_id: str, _endpoint: Any) -> Mapping[str, Any]:
            return {"degradedReasons": (), "state": "READY"}

        def begin_shutdown(self) -> None:
            return None

        def stop(self, _deadline: Any) -> None:
            return None

        def update_provider_runtime(self, _context: Any | None) -> None:
            return None

        def cached_device_context(self, _device_id: str) -> Mapping[str, Any]:
            return context

        async def refresh_device_context_async(
            self, _device_id: str
        ) -> Mapping[str, Any]:
            self.refresh_calls += 1
            if self.cancel_refresh:
                raise asyncio.CancelledError
            refreshed = json.loads(json.dumps(context))
            refreshed["deviceState"]["sequence"] = 2
            return refreshed

    driver = Driver()
    runtime = DsoftbusRuntime(
        provider_runtime=None,
        config={"dsoftbus": {}},
        workspace=tmp_path.resolve(),
        state_root=(tmp_path / "state").resolve(),
        driver=driver,
        endpoint_lock_factory=lambda _root, _runtime_id: Endpoint(),
        uuid_factory=lambda: UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
    )
    runtime.start()
    cached = runtime.get_cached_device_context(_DEVICE_B)
    cached["agentCard"]["name"] = "local-copy"
    assert context["agentCard"]["name"] == "remote"
    refreshed = await runtime.aget_device_context(_DEVICE_B, refresh_state=True)
    assert refreshed["deviceState"]["sequence"] == 2
    assert driver.refresh_calls == 1
    with pytest.raises(DsoftbusRuntimeError) as invalid:
        await runtime.aget_device_context(_DEVICE_B, refresh_state=1)  # type: ignore[arg-type]
    assert invalid.value.code == "INVALID_PARAMS"

    install_active_runtime(runtime)
    try:
        tool_result = json.loads(
            await get_device_context_handler(
                {"device_id": _DEVICE_B, "refresh_state": False}
            )
        )
    finally:
        clear_active_runtime(runtime)
    assert tool_result["success"] is True
    assert tool_result["deviceState"]["sequence"] == 1
    assert tool_result["_untrustedRemoteData"] is True

    driver.cancel_refresh = True
    with pytest.raises(asyncio.CancelledError):
        await runtime.aget_device_context(_DEVICE_B, refresh_state=True)
    runtime.stop(time.monotonic() + 1)


def _binding(
    *,
    local_device: str,
    local_runtime: str,
    peer_device: str,
    initiator: bool,
    ids: deque[str],
) -> SoftBusA2ABinding:
    manifest = _manifest(local_device)
    return SoftBusA2ABinding(
        local=LocalBindingIdentity.create(
            device_id=local_device,
            agent_id=derive_public_agent_id(local_device),
            runtime_instance_id=local_runtime,
            manifest=manifest.descriptor,
        ),
        local_card=build_agent_card(
            device_id=local_device,
            agent_id=derive_public_agent_id(local_device),
            provider_ready=False,
            provider_readiness_code="PROVIDER_MISSING",
        ),
        authenticated_peer_device_id=peer_device,
        authenticated_peer_agent_id=derive_public_agent_id(peer_device),
        initiator=initiator,
        connection_generation=1,
        negotiated_mtu=protocol.REMOTE_FRAME_MAX,
        nonce_factory=lambda count: "c" * (count * 2),
        request_id_factory=ids.popleft,
    )


def _ready_bindings() -> tuple[SoftBusA2ABinding, SoftBusA2ABinding]:
    ids_a = deque(f"aaaaaaaa-aaaa-4aaa-8aaa-{index:012x}" for index in range(1, 20))
    ids_b = deque(f"bbbbbbbb-bbbb-4bbb-8bbb-{index:012x}" for index in range(1, 20))
    side_a = _binding(
        local_device=_DEVICE_A,
        local_runtime=_RUNTIME_A,
        peer_device=_DEVICE_B,
        initiator=True,
        ids=ids_a,
    )
    side_b = _binding(
        local_device=_DEVICE_B,
        local_runtime=_RUNTIME_B,
        peer_device=_DEVICE_A,
        initiator=False,
        ids=ids_b,
    )
    pending = deque((side_b, frame) for frame in side_a.start())
    while pending:
        receiver, frame = pending.popleft()
        result = receiver.receive(frame.data)
        other = side_a if receiver is side_b else side_b
        pending.extend((other, outbound) for outbound in result.outbound)
    assert side_a.ready and side_b.ready
    return side_a, side_b


def test_ready_binding_routes_application_success_and_error_without_closing() -> None:
    side_a, side_b = _ready_bindings()
    request = side_a.request_application(
        "mclaw.deviceManifest.get",
        {},
        extensions=(protocol.DEVICE_CONTEXT_EXTENSION_URI,),
    )
    inbound = side_b.receive(request.frame.data)
    assert inbound.application_request is not None
    assert side_b.device_context_extension_allowed(inbound.application_request)
    response = side_b.complete_application_request(
        inbound.application_request,
        result={"manifest": json.loads(_manifest(_DEVICE_B).canonical_bytes)},
    )
    assert response.outbound[0].response_reservation_id == request.request_id
    routed = side_a.receive(response.outbound[0].data)
    assert routed.application_response is not None
    assert routed.application_response.request_id == request.request_id
    assert routed.application_response.method == "mclaw.deviceManifest.get"
    assert routed.application_response.error_reason is None
    assert side_a.ready and side_b.ready

    state_request = side_b.request_application(
        "mclaw.deviceState.get",
        {},
        extensions=(protocol.DEVICE_CONTEXT_EXTENSION_URI,),
    )
    state_inbound = side_a.receive(state_request.frame.data).application_request
    assert state_inbound is not None
    error = side_a.complete_application_request(
        state_inbound, error_reason="RATE_LIMITED"
    )
    assert error.outbound[0].response_reservation_id == state_request.request_id
    terminal = side_b.receive(error.outbound[0].data).application_response
    assert terminal is not None and terminal.error_reason == "RATE_LIMITED"
    assert side_a.ready and side_b.ready

    no_extension = side_a.request_application("mclaw.deviceState.get", {})
    unnegotiated = side_b.receive(no_extension.frame.data).application_request
    assert unnegotiated is not None
    assert side_b.device_context_extension_allowed(unnegotiated) is False
