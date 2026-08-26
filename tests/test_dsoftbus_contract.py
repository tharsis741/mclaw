# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
from collections import deque
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from mclaw.dsoftbus import protocol
from mclaw.dsoftbus.a2a import (
    A2AError,
    CoreMethodCall,
    PRIVATE_PROFILE_LABEL,
    build_agent_card,
    build_rpc_success,
    compact_json_bytes,
    encode_error_frame,
    encode_frame_or_error,
    encode_request_frame,
    encode_success_frame,
    parse_request_frame,
    parse_response_frame,
    parse_softbus_url,
    validate_agent_card,
    validate_core_method,
    validate_service_parameters,
)
from mclaw.dsoftbus.binding import (
    BindingError,
    BindingPhase,
    InMemoryBinding,
    LocalBindingIdentity,
    derive_public_agent_id,
)
from mclaw.dsoftbus.manifest import (
    MANIFEST_SCHEMA,
    ManifestError,
    build_public_manifest,
    load_local_manifest_template,
    parse_local_manifest_template,
    validate_manifest_descriptor,
    validate_public_manifest,
)
from mclaw.dsoftbus.product import ProductDiscoveryOwnerResources, ProductRuntimeInputs
from mclaw.dsoftbus.softbus_binding import (
    OutboundFrame,
    SoftBusA2ABinding,
    SoftBusBindingError,
    SoftBusSendScheduler,
)


_FIXTURES = Path(__file__).parent / "fixtures" / "dsoftbus_contract"
_REQUEST_ID = "12345678-1234-4234-9234-123456789abc"
_RUNTIME_A = "11111111-1111-4111-8111-111111111111"
_RUNTIME_B = "22222222-2222-4222-8222-222222222222"
_DEVICE_A = "urn:mclaw:device:oh:" + "a" * 64
_DEVICE_B = "urn:mclaw:device:oh:" + "b" * 64

_LOCAL_YAML = b"""schemaVersion: mclaw.device-manifest/v1
revision: 7
generatedAt: "2026-07-31T00:00:00Z"
device:
  manufacturer: Kaihong
  model: M-Robots
  displayName: Lab robot
  os:
    name: KaihongOS
    version: "6.1.0.04"
    apiLevel: 23
    arch: aarch64
resources:
  - resourceId: host.system
    type: system
    name: Host system
    capabilities: [status]
    operations: [read]
bindings:
  host.system:
    reader: system
    config: {}
"""


def test_device_manifest_is_device_local_state_not_a_packaged_sample() -> None:
    path = (
        Path(__file__).resolve().parents[1]
        / "mclaw"
        / "dsoftbus"
        / "assets"
        / "device.yaml"
    )
    assert not path.exists()


def _golden(name: str) -> bytes:
    raw = (_FIXTURES / name).read_bytes()
    assert raw.endswith(b"\n") and not raw.endswith(b"\n\n") and b"\r" not in raw
    return raw[:-1]


def _manifest(device_id: str):
    return build_public_manifest(parse_local_manifest_template(_LOCAL_YAML), device_id)


def _card(device_id: str, *, ready: bool = True):
    return build_agent_card(
        device_id=device_id,
        agent_id=derive_public_agent_id(device_id),
        provider_ready=ready,
        provider_readiness_code="" if ready else "PROVIDER_MISSING",
    )


def _text_only_card_document(device_id: str, *, ready: bool = True) -> dict:
    """Return the previously published Card shape without task-file support."""

    document = json.loads(_card(device_id, ready=ready).canonical_bytes)
    document["capabilities"]["extensions"] = [
        document["capabilities"]["extensions"][0]
    ]
    document["skills"][0]["description"] = (
        "Accept a text task and return a text result."
    )
    document["skills"][0]["tags"] = ["mclaw", "text", "device-agent"]
    return document


def _identity(device_id: str, runtime_id: str) -> LocalBindingIdentity:
    public = _manifest(device_id)
    return LocalBindingIdentity.create(
        device_id=device_id,
        agent_id=derive_public_agent_id(device_id),
        runtime_instance_id=runtime_id,
        manifest=public.descriptor,
    )


def _bindings() -> tuple[InMemoryBinding, InMemoryBinding]:
    side_a = InMemoryBinding(
        local=_identity(_DEVICE_A, _RUNTIME_A),
        authenticated_peer_device_id=_DEVICE_B,
        authenticated_peer_agent_id=derive_public_agent_id(_DEVICE_B),
        initiator=True,
        connection_generation=11,
        nonce_factory=lambda count: "c" * (count * 2),
    )
    side_b = InMemoryBinding(
        local=_identity(_DEVICE_B, _RUNTIME_B),
        authenticated_peer_device_id=_DEVICE_A,
        authenticated_peer_agent_id=derive_public_agent_id(_DEVICE_A),
        initiator=False,
        connection_generation=19,
    )
    return side_a, side_b


def _complete_open(side_a: InMemoryBinding, side_b: InMemoryBinding) -> None:
    open_a = side_a.make_local_open()
    ack_a = side_b.accept_peer_open(open_a)
    side_a.accept_local_open_ack(dict(ack_a))
    open_b = side_b.make_local_open()
    ack_b = side_a.accept_peer_open(open_b)
    side_b.accept_local_open_ack(dict(ack_b))


def test_local_manifest_is_strict_private_and_deeply_frozen() -> None:
    template = parse_local_manifest_template(_LOCAL_YAML)
    assert template.revision == 7
    assert template.source_byte_length == len(_LOCAL_YAML)
    assert template.source_sha256 == hashlib.sha256(_LOCAL_YAML).hexdigest()
    assert template.document["bindings"]["host.system"]["reader"] == "system"
    with pytest.raises(TypeError):
        template.document["revision"] = 8  # type: ignore[index]
    with pytest.raises(TypeError):
        template.document["bindings"]["host.system"]["reader"] = "other"  # type: ignore[index]


def test_public_manifest_removes_private_bindings_and_freezes_digest() -> None:
    template = parse_local_manifest_template(_LOCAL_YAML)
    public = build_public_manifest(template, _DEVICE_A)
    plain = json.loads(public.canonical_bytes)
    assert set(plain) == {
        "schemaVersion",
        "deviceId",
        "revision",
        "generatedAt",
        "device",
        "resources",
        "digest",
    }
    assert "bindings" not in plain
    preimage = dict(plain)
    digest = preimage.pop("digest")
    expected = "sha256:" + hashlib.sha256(
        json.dumps(
            preimage,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    assert digest == expected == public.descriptor.digest
    assert public.descriptor.revision == 7
    assert dict(public.descriptor.as_mapping()) == {
        "schemaVersion": MANIFEST_SCHEMA,
        "revision": 7,
        "digest": expected,
    }
    assert validate_public_manifest(plain).canonical_bytes == public.canonical_bytes
    with pytest.raises(TypeError):
        public.document["deviceId"] = _DEVICE_B  # type: ignore[index]


@pytest.mark.parametrize(
    "raw",
    [
        _LOCAL_YAML.replace(b"revision: 7", b"revision: 7\nrevision: 8"),
        _LOCAL_YAML + b"extra: true\n",
        _LOCAL_YAML.replace(b"revision: 7", b"revision: true"),
        _LOCAL_YAML.replace(b"operations: [read]", b"operations: [write]"),
        _LOCAL_YAML.replace(b"manufacturer: Kaihong", b"manufacturer: &x Kaihong").replace(
            b"model: M-Robots", b"model: *x"
        ),
        _LOCAL_YAML.replace(b"device:", b"base: &base {manufacturer: Kaihong}\ndevice:").replace(
            b"  manufacturer: Kaihong", b"  <<: *base"
        ),
        _LOCAL_YAML.replace(b"manufacturer: Kaihong", b"manufacturer: !private Kaihong"),
        b"\xef\xbb\xbf" + _LOCAL_YAML,
    ],
)
def test_local_manifest_rejects_ambiguous_or_unknown_yaml(raw: bytes) -> None:
    with pytest.raises(ManifestError):
        parse_local_manifest_template(raw)


def test_local_manifest_no_follow_mode_owner_and_stable_read(tmp_path: Path) -> None:
    path = tmp_path / "device.yaml"
    path.write_bytes(_LOCAL_YAML)
    path.chmod(0o600)
    observed_uid = path.stat().st_uid
    if os.name == "nt" and path.stat().st_mode & 0o022:
        with pytest.raises(ManifestError, match="mode"):
            load_local_manifest_template(path, expected_uid=observed_uid)
        pytest.skip("Windows does not expose the target POSIX DAC mode semantics")
    loaded = load_local_manifest_template(path, expected_uid=observed_uid)
    assert loaded.source_sha256 == hashlib.sha256(_LOCAL_YAML).hexdigest()

    path.chmod(0o622)
    if path.stat().st_mode & 0o022:
        with pytest.raises(ManifestError, match="mode"):
            load_local_manifest_template(path, expected_uid=observed_uid)


def test_local_manifest_symlink_is_rejected_when_available(tmp_path: Path) -> None:
    target = tmp_path / "target.yaml"
    target.write_bytes(_LOCAL_YAML)
    target.chmod(0o600)
    link = tmp_path / "device.yaml"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    with pytest.raises(ManifestError):
        load_local_manifest_template(link, expected_uid=target.stat().st_uid)


def test_descriptor_rejects_bool_revision_extra_and_bad_digest() -> None:
    valid = {
        "schemaVersion": MANIFEST_SCHEMA,
        "revision": 1,
        "digest": "sha256:" + "a" * 64,
    }
    assert validate_manifest_descriptor(valid).revision == 1
    for mutation in (
        {**valid, "revision": True},
        {**valid, "extra": False},
        {**valid, "digest": "a" * 64},
    ):
        with pytest.raises(ManifestError):
            validate_manifest_descriptor(mutation)


def test_product_phase_a_loads_once_before_worker_and_generates_nothing() -> None:
    calls: list[str] = []
    dynamic_diagnostic = {
        "activeThreadCount": 0,
        "agentSessionTaskCount": 1,
        "dispatchExecutionCount": 7,
        "dispatchQueueBytes": 4096,
        "dispatchQueueCount": 3,
        "inflightMessageCount": 3,
        "localTurnCount": 2,
        "operationCounts": {operation: 0 for operation in protocol.WORKER_OPERATIONS},
        "peerCount": 0,
        "remoteAccepted": 3,
        "remoteBudgetUsed": 0,
        "remoteRejectedByCode": {},
        "responseCacheCount": 0,
        "tokenReserved": 11,
        "workerAlive": False,
    }
    template = parse_local_manifest_template(_LOCAL_YAML)
    profile_sha = "a" * 64
    profile = SimpleNamespace(
        sha256=profile_sha,
        document={
            "runtimeClosure": {
                "python": {
                    "dynamicLibpython": {
                        "path": "/data/local/release/usr/lib/libpython3.12.so.1.0"
                    }
                },
                "softbusSocketCap": 16,
            }
        },
    )

    class FakeDiscovery:
        async def start(self, runtime_instance_id: str):
            calls.append("worker-start")
            assert runtime_instance_id == _RUNTIME_A
            return {
                "degradedReasons": ("PRODUCT_INTEGRATION_UNVERIFIED",),
                "healthUpdates": {},
                "state": "DEGRADED",
            }

        async def begin_shutdown(self):
            return {}

        async def stop(self, deadline):
            return {}

        async def update_provider_runtime(self, context):
            return {}

        def emergency_reap(self):
            return None

        def cached_diagnostic(self):
            return dict(dynamic_diagnostic)

        def cached_public_peers(self):
            return ()

        def publication_snapshot(self):
            return {
                "agentCardGenerationCount": 0,
                "listenerGenerationCount": 0,
                "manifestDescriptorGenerationCount": 0,
                "manifestPhaseBComplete": False,
                "publicManifestGenerationCount": 0,
                "stateEpochFrozen": False,
            }

    def load_profile(path: str):
        calls.append("profile-load")
        assert path == "/profile"
        return profile

    def load_manifest():
        calls.append("manifest-load")
        return template

    def make_discovery(
        profile_value, token: str, manifest_value, provider_runtime
    ):
        calls.append("discovery-create")
        assert profile_value is profile
        assert token == "42"
        assert manifest_value is template
        assert provider_runtime is None
        return FakeDiscovery()

    resources = ProductDiscoveryOwnerResources(
        inputs=ProductRuntimeInputs(
            profile_path="/profile",
            profile_sha256=profile_sha,
            python_preload_path=(
                "/data/local/release/usr/lib/libpython3.12.so.1.0"
            ),
            raw_token_id="42",
            socket_cap=16,
            status_code="",
        ),
        profile_loader=load_profile,
        manifest_loader=load_manifest,
        discovery_factory=make_discovery,
    )
    result = asyncio.run(resources.start(_RUNTIME_A))
    assert result["state"] == "DEGRADED"
    assert {
        key: resources.cached_diagnostic()[key]
        for key in (
            "dispatchExecutionCount",
            "inflightMessageCount",
            "localTurnCount",
            "tokenReserved",
        )
    } == {
        "dispatchExecutionCount": 7,
        "inflightMessageCount": 3,
        "localTurnCount": 2,
        "tokenReserved": 11,
    }
    dynamic_diagnostic.update(
        {
            "agentSessionTaskCount": 0,
            "dispatchQueueBytes": 0,
            "dispatchQueueCount": 0,
            "inflightMessageCount": 0,
            "localTurnCount": 0,
            "remoteRejectedByCode": {"RUNTIME_STOPPING": 3},
        }
    )
    assert {
        key: resources.cached_diagnostic()[key]
        for key in (
            "agentSessionTaskCount",
            "dispatchQueueBytes",
            "dispatchQueueCount",
            "inflightMessageCount",
            "localTurnCount",
            "remoteRejectedByCode",
        )
    } == {
        "agentSessionTaskCount": 0,
        "dispatchQueueBytes": 0,
        "dispatchQueueCount": 0,
        "inflightMessageCount": 0,
        "localTurnCount": 0,
        "remoteRejectedByCode": {"RUNTIME_STOPPING": 3},
    }
    assert calls == [
        "profile-load",
        "manifest-load",
        "discovery-create",
        "worker-start",
    ]
    assert dict(resources.manifest_phase_snapshot()) == {
        "agentCardGenerationCount": 0,
        "listenerGenerationCount": 0,
        "manifestDescriptorGenerationCount": 0,
        "manifestTemplateLoaded": True,
        "manifestTemplateReadCount": 1,
        "publicManifestGenerationCount": 0,
    }


def test_product_phase_a_failure_prevents_worker_construction() -> None:
    calls: list[str] = []
    profile_sha = "a" * 64
    profile = SimpleNamespace(
        sha256=profile_sha,
        document={
            "runtimeClosure": {
                "python": {
                    "dynamicLibpython": {
                        "path": "/data/local/release/usr/lib/libpython3.12.so.1.0"
                    }
                },
                "softbusSocketCap": 16,
            }
        },
    )

    def fail_manifest():
        calls.append("manifest-load")
        raise ManifestError("MANIFEST_FILE_INVALID")

    resources = ProductDiscoveryOwnerResources(
        inputs=ProductRuntimeInputs(
            profile_path="/profile",
            profile_sha256=profile_sha,
            python_preload_path=(
                "/data/local/release/usr/lib/libpython3.12.so.1.0"
            ),
            raw_token_id="42",
            socket_cap=16,
            status_code="",
        ),
        profile_loader=lambda path: calls.append("profile-load") or profile,
        manifest_loader=fail_manifest,
        discovery_factory=lambda *_args: calls.append("worker-create"),
    )
    result = asyncio.run(resources.start(_RUNTIME_A))
    assert result["state"] == "DEGRADED"
    assert result["degradedReasons"] == ("WORKER_START_FAILED",)
    assert calls == ["profile-load", "manifest-load"]
    assert resources.manifest_phase_snapshot()["manifestTemplateReadCount"] == 1
    assert resources.manifest_phase_snapshot()["publicManifestGenerationCount"] == 0


def test_request_response_and_error_match_golden_bytes() -> None:
    request = encode_request_frame(
        "ListTasks",
        {},
        negotiated_mtu=protocol.REMOTE_FRAME_MAX,
        request_id=_REQUEST_ID,
    )
    assert request.data == _golden("list_tasks_request.json")
    parsed_request = parse_request_frame(
        request.data, negotiated_mtu=protocol.REMOTE_FRAME_MAX  # type: ignore[arg-type]
    )
    assert parsed_request.request_id == _REQUEST_ID
    assert parsed_request.method == "ListTasks"
    assert dict(parsed_request.service_parameters.values) == {"A2A-Version": "1.0"}

    call = validate_core_method("ListTasks", {})
    assert isinstance(call, CoreMethodCall)
    assert call.params["pageSize"] == 50
    result = {
        "tasks": (),
        "nextPageToken": "",
        "pageSize": 50,
        "totalSize": 0,
    }
    response = encode_success_frame(
        _REQUEST_ID,
        result,
        negotiated_mtu=protocol.REMOTE_FRAME_MAX,
        phase=BindingPhase.READY,
    )
    assert response.data == _golden("list_tasks_response.json")
    parsed_response = parse_response_frame(
        response.data, negotiated_mtu=protocol.REMOTE_FRAME_MAX  # type: ignore[arg-type]
    )
    assert parsed_response.error is None
    assert parsed_response.result["tasks"] == ()

    error = encode_error_frame(
        _REQUEST_ID,
        "PEER_NOT_READY",
        negotiated_mtu=protocol.REMOTE_FRAME_MAX,
        phase=BindingPhase.READY,
    )
    assert error.data == _golden("peer_not_ready_error.json")
    assert len(error.data) <= protocol.TERMINAL_ERROR_FRAME_MAX  # type: ignore[arg-type]
    parsed_error = parse_response_frame(
        error.data, negotiated_mtu=protocol.REMOTE_FRAME_MAX  # type: ignore[arg-type]
    )
    assert parsed_error.error["code"] == -32010  # type: ignore[index]


def test_service_parameters_are_case_insensitive_bounded_and_negotiated() -> None:
    parameters = validate_service_parameters(
        {
            "a2a-version": "1.0",
            "A2A-Extensions": (
                "https://example.invalid/ignored, "
                + protocol.DEVICE_CONTEXT_EXTENSION_URI
            ),
        }
    )
    assert parameters.advertises(protocol.DEVICE_CONTEXT_EXTENSION_URI)
    assert parameters.advertises("https://example.invalid/ignored")
    with pytest.raises(A2AError) as duplicate:
        validate_service_parameters(
            {"A2A-Version": "1.0", "a2a-version": "1.0"}
        )
    assert duplicate.value.reason == "INVALID_REQUEST"
    with pytest.raises(A2AError) as unknown:
        validate_service_parameters({"A2A-Version": "1.0", "Header": "x"})
    assert unknown.value.reason == "INVALID_REQUEST"
    with pytest.raises(A2AError) as version:
        validate_service_parameters({"A2A-Version": "2.0"})
    assert version.value.reason == "VERSION_NOT_SUPPORTED"


@pytest.mark.parametrize(
    "raw",
    [
        b'[{"jsonrpc":"2.0"}]',
        b'\xef\xbb\xbf{}',
        b" \t{}",
        b'{"binding":1,"binding":2}',
        compact_json_bytes(
            {
                "binding": protocol.PROTOCOL_BINDING,
                "bindingVersion": protocol.BINDING_VERSION,
                "serviceParameters": {"A2A-Version": "1.0"},
                "rpc": {"jsonrpc": "2.0", "method": "ListTasks", "params": {}},
            }
        ),
    ],
)
def test_outer_rejects_batch_bom_duplicate_and_notification(raw: bytes) -> None:
    with pytest.raises(A2AError):
        parse_request_frame(raw, negotiated_mtu=protocol.REMOTE_FRAME_MAX)


def test_outer_rejects_depth_and_response_oneof_conflict() -> None:
    nested: object = 1
    for _ in range(20):
        nested = {"x": nested}
    frame = compact_json_bytes(
        {
            "binding": protocol.PROTOCOL_BINDING,
            "bindingVersion": protocol.BINDING_VERSION,
            "serviceParameters": {"A2A-Version": "1.0"},
            "rpc": {
                "jsonrpc": "2.0",
                "id": _REQUEST_ID,
                "method": "ListTasks",
                "params": {"future": nested},
            },
        }
    )
    with pytest.raises(A2AError) as depth:
        parse_request_frame(frame, negotiated_mtu=protocol.REMOTE_FRAME_MAX)
    assert depth.value.reason == "INVALID_REQUEST"

    response = compact_json_bytes(
        {
            "binding": protocol.PROTOCOL_BINDING,
            "bindingVersion": protocol.BINDING_VERSION,
            "rpc": {
                "jsonrpc": "2.0",
                "id": _REQUEST_ID,
                "result": {},
                "error": {},
            },
        }
    )
    with pytest.raises(A2AError):
        parse_response_frame(response, negotiated_mtu=protocol.REMOTE_FRAME_MAX)


def test_shared_encoder_uses_bounded_phase_specific_oversize_error() -> None:
    large_rpc = build_rpc_success(_REQUEST_ID, {"value": "x" * 8_000})
    binding = encode_frame_or_error(
        large_rpc,
        phase=BindingPhase.CARD_VERIFYING,
        negotiated_mtu=protocol.MIN_NEGOTIATED_FRAME,
        inbound_request_id=_REQUEST_ID,
    )
    assert binding.terminal_reason == "BINDING_INCOMPATIBLE"
    assert binding.close_generation is True
    assert binding.data is not None and len(binding.data) <= 1_024
    parsed = parse_response_frame(
        binding.data, negotiated_mtu=protocol.MIN_NEGOTIATED_FRAME
    )
    assert parsed.error["data"][0]["metadata"] == {  # type: ignore[index]
        "failureReason": "FRAME_TOO_LARGE",
        "outcomeUnknown": "false",
    }

    ready = encode_frame_or_error(
        large_rpc,
        phase=BindingPhase.READY,
        negotiated_mtu=protocol.MIN_NEGOTIATED_FRAME,
        inbound_request_id=_REQUEST_ID,
    )
    assert ready.terminal_reason == "FRAME_TOO_LARGE"
    assert ready.close_generation is False
    with pytest.raises(A2AError) as local:
        encode_frame_or_error(
            large_rpc,
            phase=BindingPhase.READY,
            negotiated_mtu=protocol.MIN_NEGOTIATED_FRAME,
        )
    assert local.value.reason == "FRAME_TOO_LARGE"


def test_core_static_semantics_and_validation_order() -> None:
    result = validate_core_method("ListTasks", {"pageSize": 7, "future": True})
    assert isinstance(result, CoreMethodCall)
    assert result.method == "ListTasks"
    assert dict(result.params) == {"pageSize": 7}
    with pytest.raises(A2AError) as bool_size:
        validate_core_method("ListTasks", {"pageSize": True})
    assert bool_size.value.reason == "INVALID_PARAMS"
    with pytest.raises(A2AError) as extended:
        validate_core_method("GetExtendedAgentCard", {})
    assert extended.value.reason == "UNSUPPORTED_OPERATION"
    with pytest.raises(A2AError) as unknown:
        validate_core_method("UnknownMethod", {})
    assert unknown.value.reason == "METHOD_NOT_FOUND"
    with pytest.raises(A2AError) as custom_extra:
        validate_core_method("mclaw.agentCard.get", {"future": True})
    assert custom_extra.value.reason == "INVALID_PARAMS"

    lease = validate_core_method(
        "mclaw.taskLease.renew",
        {
            "sequence": 7,
            "taskIds": ["00000000-0000-4000-8000-000000000007"],
        },
    )
    assert isinstance(lease, CoreMethodCall)
    assert lease.params["sequence"] == 7
    with pytest.raises(A2AError) as duplicate_task:
        validate_core_method(
            "mclaw.taskLease.renew",
            {
                "sequence": 8,
                "taskIds": [
                    "00000000-0000-4000-8000-000000000007",
                    "00000000-0000-4000-8000-000000000007",
                ],
            },
        )
    assert duplicate_task.value.reason == "INVALID_PARAMS"


def test_send_message_validates_text_and_rejects_before_business_admission() -> None:
    request = {
        "message": {
            "messageId": _REQUEST_ID,
            "role": "ROLE_USER",
            "parts": [{"text": "hello", "future": "ignored"}, {"text": "world"}],
            "future": "ignored",
        },
        "configuration": {"acceptedOutputModes": ["text/plain"]},
        "future": "ignored",
    }
    call = validate_core_method("SendMessage", request)
    assert call.normalized_text == "hello\nworld"  # type: ignore[union-attr]
    assert call.params["configuration"]["acceptedOutputModes"] == (  # type: ignore[union-attr]
        "text/plain",
    )

    immediate = copy.deepcopy(request)
    immediate["configuration"]["returnImmediately"] = True
    immediate_call = validate_core_method("SendMessage", immediate)
    assert isinstance(immediate_call, CoreMethodCall)
    assert immediate_call.params["configuration"]["returnImmediately"] is True

    streaming_call = validate_core_method("SendStreamingMessage", request)
    assert isinstance(streaming_call, CoreMethodCall)
    assert streaming_call.normalized_text == "hello\nworld"

    binary = copy.deepcopy(request)
    binary["message"]["parts"] = [{"raw": "YQ=="}]
    with pytest.raises(A2AError) as content:
        validate_core_method("SendMessage", binary)
    assert content.value.reason == "CONTENT_TYPE_NOT_SUPPORTED"

    invalid_binary = copy.deepcopy(request)
    invalid_binary["message"]["parts"] = [{"raw": "not-base64"}]
    with pytest.raises(A2AError) as invalid_raw:
        validate_core_method("SendMessage", invalid_binary)
    assert invalid_raw.value.reason == "INVALID_PARAMS"

    with pytest.raises(A2AError) as push:
        validate_core_method(
            "CreateTaskPushNotificationConfig",
            {"url": "https://callback.invalid/path"},
        )
    assert push.value.reason == "PUSH_NOT_SUPPORTED"


def test_send_message_continuation_requires_a_bound_input_request() -> None:
    task_id = "00000000-0000-4000-8000-000000000201"
    context_id = "00000000-0000-4000-8000-000000000202"
    input_request_id = "00000000-0000-4000-8000-000000000203"
    request = {
        "message": {
            "messageId": _REQUEST_ID,
            "contextId": context_id,
            "taskId": task_id,
            "role": "ROLE_USER",
            "parts": [{"text": "authentication"}],
            "metadata": {"mclaw.inputRequestId": input_request_id},
        }
    }

    call = validate_core_method("SendStreamingMessage", request)
    assert call.params["message"]["taskId"] == task_id
    assert call.params["message"]["contextId"] == context_id
    assert call.params["message"]["metadata"]["mclaw.inputRequestId"] == (
        input_request_id
    )

    missing_request = copy.deepcopy(request)
    missing_request["message"].pop("metadata")
    with pytest.raises(A2AError) as missing:
        validate_core_method("SendStreamingMessage", missing_request)
    assert missing.value.reason == "INPUT_REQUEST_MISMATCH"

    initial_with_request = copy.deepcopy(request)
    initial_with_request["message"].pop("taskId")
    with pytest.raises(A2AError) as unbound:
        validate_core_method("SendStreamingMessage", initial_with_request)
    assert unbound.value.reason == "INPUT_REQUEST_MISMATCH"


def test_agent_card_exact_outbound_forward_compatible_inbound_and_bounded() -> None:
    card = _card(_DEVICE_A, ready=False)
    assert len(card.canonical_bytes) <= protocol.AGENT_CARD_MAX
    plain = json.loads(card.canonical_bytes)
    plain["futureTop"] = True
    plain["supportedInterfaces"][0]["futureInterface"] = True
    plain["capabilities"]["futureCapability"] = True
    plain["capabilities"]["extensions"][0]["futureExtension"] = True
    plain["skills"][0]["futureSkill"] = True
    normalized = validate_agent_card(
        plain,
        expected_device_id=_DEVICE_A,
        expected_agent_id=derive_public_agent_id(_DEVICE_A),
    )
    normalized_plain = json.loads(normalized.canonical_bytes)
    assert all("future" not in key.lower() for key in normalized_plain)
    assert "futureInterface" not in normalized_plain["supportedInterfaces"][0]
    assert "futureCapability" not in normalized_plain["capabilities"]
    assert "futureExtension" not in normalized_plain["capabilities"]["extensions"][0]
    assert "futureSkill" not in normalized_plain["skills"][0]

    custom_extra = json.loads(card.canonical_bytes)
    custom_extra["capabilities"]["extensions"][0]["params"]["secret"] = "x"
    with pytest.raises(A2AError):
        validate_agent_card(
            custom_extra,
            expected_device_id=_DEVICE_A,
            expected_agent_id=derive_public_agent_id(_DEVICE_A),
        )
    with pytest.raises(A2AError):
        build_agent_card(
            device_id=_DEVICE_A,
            agent_id=derive_public_agent_id(_DEVICE_B),
            provider_ready=True,
            provider_readiness_code="",
        )


def test_text_only_agent_card_remains_valid_without_task_file_extension() -> None:
    card = validate_agent_card(
        _text_only_card_document(_DEVICE_A),
        expected_device_id=_DEVICE_A,
        expected_agent_id=derive_public_agent_id(_DEVICE_A),
    )

    assert card.supports_extension(protocol.DEVICE_CONTEXT_EXTENSION_URI)
    assert not card.supports_extension(protocol.TASK_FILES_EXTENSION_URI)
    assert len(card.document["capabilities"]["extensions"]) == 1


def test_text_only_peer_binds_but_rejects_task_file_requests_locally() -> None:
    ids_a = iter(
        f"aaaaaaaa-aaaa-4aaa-8aaa-{index:012x}" for index in range(1, 10)
    )
    ids_b = iter(
        f"bbbbbbbb-bbbb-4bbb-8bbb-{index:012x}" for index in range(1, 10)
    )
    side_a = SoftBusA2ABinding(
        local=_identity(_DEVICE_A, _RUNTIME_A),
        local_card=_card(_DEVICE_A),
        authenticated_peer_device_id=_DEVICE_B,
        authenticated_peer_agent_id=derive_public_agent_id(_DEVICE_B),
        initiator=True,
        connection_generation=1,
        negotiated_mtu=protocol.REMOTE_FRAME_MAX,
        nonce_factory=lambda count: "c" * (count * 2),
        request_id_factory=lambda: next(ids_a),
    )
    side_b = SoftBusA2ABinding(
        local=_identity(_DEVICE_B, _RUNTIME_B),
        local_card=_text_only_card_document(_DEVICE_B, ready=False),
        authenticated_peer_device_id=_DEVICE_A,
        authenticated_peer_agent_id=derive_public_agent_id(_DEVICE_A),
        initiator=False,
        connection_generation=1,
        negotiated_mtu=protocol.REMOTE_FRAME_MAX,
        request_id_factory=lambda: next(ids_b),
    )
    pending = deque((side_b, frame) for frame in side_a.start())
    while pending:
        receiver, frame = pending.popleft()
        result = receiver.receive(frame.data)
        other = side_a if receiver is side_b else side_b
        pending.extend((other, outbound) for outbound in result.outbound)

    assert side_a.ready and side_b.ready
    assert side_a.peer_card is not None
    assert not side_a.peer_card.supports_extension(
        protocol.TASK_FILES_EXTENSION_URI
    )
    text_request = side_a.request_application(
        "SendMessage",
        {
            "message": {
                "messageId": "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
                "role": "ROLE_USER",
                "parts": [{"text": "纯文本任务"}],
            }
        },
    )
    assert text_request.method == "SendMessage"

    with pytest.raises(
        SoftBusBindingError,
        match="EXTENSION_SUPPORT_REQUIRED",
    ):
        side_a.request_application(
            "mclaw.taskArtifact.open",
            {
                "taskId": "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
                "artifactId": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
                "transferId": "ffffffff-ffff-4fff-8fff-ffffffffffff",
            },
            extensions=(protocol.TASK_FILES_EXTENSION_URI,),
        )


def test_null_error_id_is_always_a_close_after_send_terminal() -> None:
    frame = encode_error_frame(
        None,
        "PARSE_ERROR",
        negotiated_mtu=protocol.REMOTE_FRAME_MAX,
        phase=BindingPhase.READY,
    )
    assert frame.close_generation is True
    assert parse_response_frame(
        frame.data, negotiated_mtu=protocol.REMOTE_FRAME_MAX  # type: ignore[arg-type]
    ).request_id is None


@pytest.mark.parametrize(
    "url",
    [
        "softbus://" + "A" * 64 + "/mclaw.a2a.v1",
        "softbus://user@" + "a" * 64 + "/mclaw.a2a.v1",
        "softbus://" + "a" * 64 + ":1/mclaw.a2a.v1",
        "softbus://" + "a" * 64 + "/mclaw.a2a.v1?x=1",
        "softbus://" + "a" * 64 + "/mclaw.a2a.v1#x",
        "softbus://" + "a" * 64 + "/extra/mclaw.a2a.v1",
        "https://" + "a" * 64 + "/mclaw.a2a.v1",
    ],
)
def test_softbus_url_parser_has_no_generic_network_surface(url: str) -> None:
    with pytest.raises(A2AError):
        parse_softbus_url(url)
    valid = f"softbus://{'a' * 64}/mclaw.a2a.v1"
    assert parse_softbus_url(valid) == valid


def test_binding_open_order_identity_and_card_gate_reach_ready() -> None:
    side_a, side_b = _bindings()
    assert side_a.phase == side_b.phase == BindingPhase.BINDING
    assert side_a.method_allowed("mclaw.binding.open") is True
    assert side_a.method_allowed("mclaw.agentCard.get") is False
    _complete_open(side_a, side_b)
    assert side_a.phase == side_b.phase == BindingPhase.BINDING_OPEN
    assert side_a.method_allowed("mclaw.agentCard.get") is True
    assert side_a.method_allowed("ListTasks") is False

    side_a.begin_card_verification()
    side_b.begin_card_verification()
    side_a.complete_card_verification(_card(_DEVICE_B))
    side_b.complete_card_verification(_card(_DEVICE_A, ready=False))
    assert side_a.phase == side_b.phase == BindingPhase.READY
    assert side_a.method_allowed("SendMessage") is True
    assert side_a.peer_identity.runtime_instance_id == _RUNTIME_B  # type: ignore[union-attr]
    snapshot = dict(side_a.public_snapshot())
    assert snapshot["peerDeviceId"] == _DEVICE_B
    assert "connectionNonce" not in snapshot
    assert "networkId" not in snapshot
    assert "udid" not in {key.lower() for key in snapshot}


def test_binding_rejects_out_of_order_claim_mismatch_and_bad_ack() -> None:
    side_a, side_b = _bindings()
    with pytest.raises(BindingError) as passive_early:
        side_b.make_local_open()
    assert passive_early.value.code == "PEER_NOT_READY"

    open_a = dict(side_a.make_local_open())
    with pytest.raises(BindingError) as active_peer_early:
        side_a.accept_peer_open(open_a)
    assert active_peer_early.value.code == "PEER_NOT_READY"

    wrong = dict(open_a)
    wrong["manifest"] = dict(wrong["manifest"])
    wrong["deviceId"] = _DEVICE_B
    wrong["agentId"] = derive_public_agent_id(_DEVICE_B)
    with pytest.raises(BindingError) as mismatch:
        side_b.accept_peer_open(wrong)
    assert mismatch.value.code == "BINDING_INCOMPATIBLE"

    fresh_a, fresh_b = _bindings()
    fresh_b.accept_peer_open(fresh_a.make_local_open())
    with pytest.raises(BindingError) as ack:
        fresh_a.accept_local_open_ack({"accepted": 1})
    assert ack.value.code == "BINDING_INCOMPATIBLE"


def test_binding_card_identity_failure_closes_generation() -> None:
    side_a, side_b = _bindings()
    _complete_open(side_a, side_b)
    side_a.begin_card_verification()
    with pytest.raises(BindingError) as mismatch:
        side_a.complete_card_verification(_card(_DEVICE_A))
    assert mismatch.value.code == "BINDING_INCOMPATIBLE"
    assert side_a.phase == BindingPhase.CLOSED
    with pytest.raises(BindingError) as stale:
        side_a.require_method("ListTasks")
    assert stale.value.code == "STALE_GENERATION"


def test_softbus_binding_wire_handshake_reaches_ready_on_both_sides() -> None:
    ids_a = iter(
        (
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1",
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2",
        )
    )
    ids_b = iter(
        (
            "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1",
            "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb2",
        )
    )
    side_a = SoftBusA2ABinding(
        local=_identity(_DEVICE_A, _RUNTIME_A),
        local_card=_card(_DEVICE_A),
        authenticated_peer_device_id=_DEVICE_B,
        authenticated_peer_agent_id=derive_public_agent_id(_DEVICE_B),
        initiator=True,
        connection_generation=1,
        negotiated_mtu=protocol.REMOTE_FRAME_MAX,
        nonce_factory=lambda count: "c" * (count * 2),
        request_id_factory=lambda: next(ids_a),
    )
    side_b = SoftBusA2ABinding(
        local=_identity(_DEVICE_B, _RUNTIME_B),
        local_card=_card(_DEVICE_B, ready=False),
        authenticated_peer_device_id=_DEVICE_A,
        authenticated_peer_agent_id=derive_public_agent_id(_DEVICE_A),
        initiator=False,
        connection_generation=1,
        negotiated_mtu=protocol.REMOTE_FRAME_MAX,
        request_id_factory=lambda: next(ids_b),
    )

    assert side_b.start() == ()
    pending = deque((side_b, frame) for frame in side_a.start())
    payloads: list[bytes] = []
    while pending:
        receiver, frame = pending.popleft()
        payloads.append(frame.data)
        result = receiver.receive(frame.data)
        assert result.application_request is None
        assert result.close_generation is False
        other = side_a if receiver is side_b else side_b
        pending.extend((other, outbound) for outbound in result.outbound)

    assert len(payloads) == 8
    assert side_a.phase == side_b.phase == BindingPhase.READY
    assert side_a.peer_card is not None and side_a.peer_card.device_id == _DEVICE_B
    assert side_b.peer_card is not None and side_b.peer_card.device_id == _DEVICE_A
    assert side_a.public_snapshot()["pendingRequestCount"] == 0
    assert side_b.public_snapshot()["pendingRequestCount"] == 0
    assert side_a.public_snapshot()["peerCardVerified"] is True
    assert side_b.public_snapshot()["peerCardVerified"] is True


def test_softbus_binding_identity_mismatch_returns_terminal_and_closes() -> None:
    side_a = SoftBusA2ABinding(
        local=_identity(_DEVICE_A, _RUNTIME_A),
        local_card=_card(_DEVICE_A),
        authenticated_peer_device_id=_DEVICE_B,
        authenticated_peer_agent_id=derive_public_agent_id(_DEVICE_B),
        initiator=True,
        connection_generation=3,
        negotiated_mtu=protocol.REMOTE_FRAME_MAX,
        nonce_factory=lambda count: "d" * (count * 2),
        request_id_factory=lambda: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa3",
    )
    side_b = SoftBusA2ABinding(
        local=_identity(_DEVICE_B, _RUNTIME_B),
        local_card=_card(_DEVICE_B),
        authenticated_peer_device_id=_DEVICE_A,
        authenticated_peer_agent_id=derive_public_agent_id(_DEVICE_A),
        initiator=False,
        connection_generation=4,
        negotiated_mtu=protocol.REMOTE_FRAME_MAX,
    )
    side_b.start()
    open_frame = side_a.start()[0]
    tampered = json.loads(open_frame.data)
    tampered["rpc"]["params"]["deviceId"] = _DEVICE_B
    tampered["rpc"]["params"]["agentId"] = derive_public_agent_id(_DEVICE_B)

    result = side_b.receive(compact_json_bytes(tampered))

    assert side_b.phase == BindingPhase.CLOSED
    assert result.close_generation is False
    assert len(result.outbound) == 1
    assert result.outbound[0].close_after_send is True
    error = parse_response_frame(
        result.outbound[0].data, negotiated_mtu=protocol.REMOTE_FRAME_MAX
    )
    assert error.error["data"][0]["reason"] == "BINDING_INCOMPATIBLE"  # type: ignore[index]


def test_send_scheduler_enforces_caps_round_robin_fairness_and_release() -> None:
    scheduler = SoftBusSendScheduler()
    scheduler.register(10, 1)
    scheduler.register(20, 1)
    for index in range(9):
        scheduler.enqueue(10, 1, OutboundFrame(f"a{index}".encode()))
    scheduler.enqueue(10, 1, OutboundFrame(b"control", queue="control"))
    scheduler.enqueue(20, 1, OutboundFrame(b"peer"))
    sent: list[tuple[int, bytes]] = []

    while scheduler.drain_one(
        lambda socket, data: sent.append((socket, data)) or len(data)
    ):
        pass

    assert sent[0][0] == 10 and sent[1][0] == 20
    socket_ten = [data for socket, data in sent if socket == 10]
    assert socket_ten[:8] == [f"a{index}".encode() for index in range(8)]
    assert socket_ten[8:] == [b"control", b"a8"]
    assert dict(scheduler.diagnostic_snapshot()) == {
        "sendBusinessQueueBytes": 0,
        "sendBusinessQueueCount": 0,
        "sendControlQueueBytes": 0,
        "sendControlQueueCount": 0,
        "sendQueueOverflowCount": 0,
    }

    for _ in range(protocol.SOCKET_SEND_QUEUE_MAX):
        scheduler.enqueue(10, 1, OutboundFrame(b"x"))
    with pytest.raises(SoftBusBindingError) as full:
        scheduler.enqueue(10, 1, OutboundFrame(b"x"))
    assert full.value.code == "CAPACITY_BUSY"
    assert scheduler.diagnostic_snapshot()["sendQueueOverflowCount"] == 1
    assert scheduler.unregister(10, 1) is True
    assert scheduler.diagnostic_snapshot()["sendBusinessQueueCount"] == 0


def test_send_scheduler_closes_generation_after_terminal_send() -> None:
    scheduler = SoftBusSendScheduler()
    scheduler.register(7, 9)
    scheduler.enqueue(
        7,
        9,
        OutboundFrame(b"terminal", queue="control", close_after_send=True),
    )
    completion = scheduler.drain_one(lambda _socket, data: len(data))
    assert completion is not None and completion.close_after_send is True
    assert completion.socket == 7 and completion.generation == 9
    with pytest.raises(SoftBusBindingError) as stale:
        scheduler.enqueue(7, 9, OutboundFrame(b"late"))
    assert stale.value.code == "STALE_GENERATION"


def test_machine_compatibility_labels_are_not_development_branches() -> None:
    assert PRIVATE_PROFILE_LABEL == "mclaw-private-profile/v1"
    assert MANIFEST_SCHEMA == "mclaw.device-manifest/v1"
    assert protocol.PROTOCOL_BINDING.endswith("/a2a-softbus/v1")


def test_worker_import_does_not_load_pydantic_or_main_a2a_module() -> None:
    script = (
        "import sys; import mclaw.dsoftbus.worker; "
        "assert 'pydantic' not in sys.modules; "
        "assert 'mclaw.dsoftbus.a2a' not in sys.modules"
    )
    completed = subprocess.run(
        [sys.executable, "-I", "-B", "-c", script],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=20,
        cwd=Path(__file__).parents[1],
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")
