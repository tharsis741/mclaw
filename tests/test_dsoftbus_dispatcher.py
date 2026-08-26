# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import base64
import hashlib
import threading
import time
from collections import deque
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from mclaw.dsoftbus import protocol
from mclaw.dsoftbus.a2a import (
    build_agent_card,
    build_artifact_update,
    build_status_update,
    build_task,
)
from mclaw.dsoftbus.agent_message import AgentMessageError, RemoteTurnRequest
from mclaw.dsoftbus.task_artifact import TaskArtifactCollector
from mclaw.dsoftbus.binding import LocalBindingIdentity, derive_public_agent_id
from mclaw.dsoftbus.discovery_resources import DiscoveryOwnerResources, _Connection
from mclaw.dsoftbus.manifest import MANIFEST_SCHEMA, ManifestDescriptor
from mclaw.dsoftbus.softbus_binding import ApplicationResponse, SoftBusA2ABinding
from mclaw.dsoftbus.workspace import DsoftbusWorkspace, peer_directory

_DEVICE_A = "urn:mclaw:device:oh:" + "a" * 64
_DEVICE_B = "urn:mclaw:device:oh:" + "b" * 64
_RUNTIME_A = "00000000-0000-4000-8000-000000000101"
_RUNTIME_B = "00000000-0000-4000-8000-000000000102"
_PROVIDER_A = object()
_PROVIDER_B = object()


def _id(value: int) -> str:
    return f"00000000-0000-4000-8000-{value:012d}"

def _config(**overrides: Any) -> dict[str, Any]:
    value = {
        "accept_remote_messages": True,
        "global_requests_per_minute": 12,
        "per_peer_requests_per_minute": 6,
        "remote_token_budget_per_hour": 100_000,
    }
    value.update(overrides)
    return value

class _Executor:
    def __init__(self) -> None:
        self.calls: list[RemoteTurnRequest] = []
        self.gate: asyncio.Event | None = None
        self.started = asyncio.Event()
        self.estimate = 100
        self.include_usage = True
        self.interrupts: list[str] = []
        self.forgotten: list[str] = []
        self.provider_updates: list[Any | None] = []
        self.dispose_calls = 0
        self.result_override: Mapping[str, Any] | None = None
        self.result_factory: Any = None

    async def execute(self, request: RemoteTurnRequest) -> Mapping[str, Any]:
        self.calls.append(request)
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.result_factory is not None:
            return self.result_factory(request)
        if self.result_override is not None:
            return self.result_override
        messages = [dict(item) for item in request.history]
        messages.extend(
            [
                {"role": "user", "content": request.text},
                {"role": "assistant", "content": f"reply:{request.text}"},
            ]
        )
        result: dict[str, Any] = {
            "completed": True,
            "final_response": f"reply:{request.text}",
            "interrupted": False,
            "messages": messages,
        }
        if self.include_usage:
            result["token_usage"] = {"input_tokens": 10, "output_tokens": 10}
        return result

    def estimate_budget(self, _request: RemoteTurnRequest) -> int:
        return self.estimate

    def update_provider_runtime(self, context: Any | None) -> None:
        self.provider_updates.append(context)

    def interrupt(self, session_id: str) -> bool:
        self.interrupts.append(session_id)
        return True

    async def cancel_and_reap(
        self,
        session_id: str,
        _task_id: str,
        _deadline: float,
    ) -> bool:
        self.interrupts.append(session_id)
        return True

    async def forget_session(self, session_id: str, _deadline: float) -> bool:
        self.forgotten.append(session_id)
        return True

    async def dispose(self, _deadline: float) -> bool:
        self.dispose_calls += 1
        return True

def _ready_wire_bindings(
    *, generation: int = 1
) -> tuple[SoftBusA2ABinding, SoftBusA2ABinding]:
    descriptor = ManifestDescriptor(MANIFEST_SCHEMA, 1, "sha256:" + "d" * 64)

    def _binding(
        *,
        local_device: str,
        local_runtime: str,
        peer_device: str,
        initiator: bool,
        prefix: str,
    ) -> SoftBusA2ABinding:
        ids = deque(
            f"{prefix * 8}-{prefix * 4}-4{prefix * 3}-8{prefix * 3}-{index:012x}"
            for index in range(1, 64)
        )
        return SoftBusA2ABinding(
            local=LocalBindingIdentity.create(
                device_id=local_device,
                agent_id=derive_public_agent_id(local_device),
                runtime_instance_id=local_runtime,
                manifest=descriptor,
            ),
            local_card=build_agent_card(
                device_id=local_device,
                agent_id=derive_public_agent_id(local_device),
                provider_ready=True,
                provider_readiness_code="",
            ),
            authenticated_peer_device_id=peer_device,
            authenticated_peer_agent_id=derive_public_agent_id(peer_device),
            initiator=initiator,
            connection_generation=generation,
            negotiated_mtu=protocol.REMOTE_FRAME_MAX,
            nonce_factory=lambda count: "c" * (count * 2),
            request_id_factory=ids.popleft,
        )

    side_a = _binding(
        local_device=_DEVICE_A,
        local_runtime=_RUNTIME_A,
        peer_device=_DEVICE_B,
        initiator=True,
        prefix="a",
    )
    side_b = _binding(
        local_device=_DEVICE_B,
        local_runtime=_RUNTIME_B,
        peer_device=_DEVICE_A,
        initiator=False,
        prefix="b",
    )
    pending = deque((side_b, frame) for frame in side_a.start())
    while pending:
        receiver, frame = pending.popleft()
        result = receiver.receive(frame.data)
        other = side_a if receiver is side_b else side_b
        pending.extend((other, outbound) for outbound in result.outbound)
    assert side_a.ready and side_b.ready
    return side_a, side_b


class _WireSupervisor:
    def __init__(self) -> None:
        self.deliver: Any = None
        self.closed: list[int] = []
        self.send_count = 0

    def send_bytes(self, socket: int, data: bytes) -> int:
        assert self.deliver is not None
        self.send_count += 1
        self.deliver(data)
        return len(data)

    def close_socket(self, socket: int) -> None:
        self.closed.append(socket)

    @staticmethod
    def health_updates() -> Mapping[str, Any]:
        return {"workerAlive": True}

    def diagnostic_snapshot(self) -> Mapping[str, Any]:
        return {
            "activeThreadCount": 0,
            "operationCounts": {"send_bytes": self.send_count},
            "workerAlive": True,
        }


def _ready_wire_resources(
    workspace_root: Path | None = None,
) -> tuple[
    DiscoveryOwnerResources,
    DiscoveryOwnerResources,
    _WireSupervisor,
    _WireSupervisor,
]:
    side_a, side_b = _ready_wire_bindings()
    supervisor_a = _WireSupervisor()
    supervisor_b = _WireSupervisor()
    resources_a = DiscoveryOwnerResources(
        supervisor=supervisor_a,  # type: ignore[arg-type]
        provider_ready=True,
        provider_readiness_code="",
        provider_runtime=_PROVIDER_A,
        message_config=_config(),
        message_executor=_Executor(),
        agent_workspace_root=(
            None if workspace_root is None else workspace_root / "a-workspace"
        ),
        task_state_root=(
            None if workspace_root is None else workspace_root / "a-state"
        ),
    )
    resources_b = DiscoveryOwnerResources(
        supervisor=supervisor_b,  # type: ignore[arg-type]
        provider_ready=True,
        provider_readiness_code="",
        provider_runtime=_PROVIDER_B,
        message_config=_config(),
        message_executor=_Executor(),
        agent_workspace_root=(
            None if workspace_root is None else workspace_root / "b-workspace"
        ),
        task_state_root=(
            None if workspace_root is None else workspace_root / "b-state"
        ),
    )
    resources_a._require_owner(establish=True)
    resources_b._require_owner(establish=True)
    resources_a._connections_by_socket[11] = _Connection(
        device_id=_DEVICE_B,
        generation=1,
        mtu=protocol.REMOTE_FRAME_MAX,
        network_id="network-b",
        socket=11,
        binding=side_a,
    )
    resources_a._socket_by_device[_DEVICE_B] = 11
    resources_a._send_scheduler.register(11, 1)
    resources_b._connections_by_socket[22] = _Connection(
        device_id=_DEVICE_A,
        generation=1,
        mtu=protocol.REMOTE_FRAME_MAX,
        network_id="network-a",
        socket=22,
        binding=side_b,
    )
    resources_b._socket_by_device[_DEVICE_A] = 22
    resources_b._send_scheduler.register(22, 1)
    return resources_a, resources_b, supervisor_a, supervisor_b


@pytest.mark.asyncio
async def test_task_owner_lease_renews_over_authenticated_softbus_wire() -> None:
    resources_a, resources_b, supervisor_a, supervisor_b = (
        _ready_wire_resources()
    )
    supervisor_a.deliver = lambda data: resources_b._handle_binding_bytes(
        22, base64.b64encode(data).decode("ascii")
    )
    supervisor_b.deliver = lambda data: resources_a._handle_binding_bytes(
        11, base64.b64encode(data).decode("ascii")
    )
    dispatcher_a = resources_a._task_dispatcher
    dispatcher_b = resources_b._task_dispatcher
    assert dispatcher_a is not None and dispatcher_b is not None
    executor_b = dispatcher_b._executor
    assert isinstance(executor_b, _Executor)
    executor_b.gate = asyncio.Event()

    pending = asyncio.create_task(
        resources_a.run_agent_task(
            _DEVICE_B,
            "keep this task active",
            context_id=None,
            message_id=_id(2189),
        )
    )
    await asyncio.wait_for(executor_b.started.wait(), timeout=2)
    assert len(resources_a._outbound_task_ids[_DEVICE_B]) == 1

    await resources_a._renew_all_outbound_task_leases()
    diagnostic = dispatcher_b.diagnostic_snapshot()
    assert diagnostic["ownerLeaseCount"] == 1
    assert diagnostic["ownerLeaseRenewed"] == 1

    executor_b.gate.set()
    result = await asyncio.wait_for(pending, timeout=2)
    assert result["task_state"] == "TASK_STATE_COMPLETED"
    assert resources_a._outbound_task_ids == {}
    await dispatcher_a.drain(time.monotonic() + 1.0)
    await dispatcher_b.drain(time.monotonic() + 1.0)


@pytest.mark.asyncio
async def test_unavailable_lease_reconciles_terminal_task_and_releases_sources(
    tmp_path: Path,
) -> None:
    resources_a, resources_b, supervisor_a, supervisor_b = (
        _ready_wire_resources(tmp_path)
    )
    supervisor_a.deliver = lambda data: resources_b._handle_binding_bytes(
        22, base64.b64encode(data).decode("ascii")
    )
    supervisor_b.deliver = lambda data: resources_a._handle_binding_bytes(
        11, base64.b64encode(data).decode("ascii")
    )
    dispatcher_a = resources_a._task_dispatcher
    dispatcher_b = resources_b._task_dispatcher
    assert dispatcher_a is not None and dispatcher_b is not None
    executor_b = dispatcher_b._executor
    assert isinstance(executor_b, _Executor)
    executor_b.result_override = {
        "completed": False,
        "interrupted": False,
        "pending_task_input": True,
        "input_request": {
            "message": "请补充目标文件",
            "accepts": ["file"],
        },
        "messages": [
            {"role": "user", "content": "inspect the shared source"},
            {"role": "assistant", "content": "请补充目标文件"},
        ],
        "token_usage": {"input_tokens": 4, "output_tokens": 4},
    }
    source = tmp_path / "shared-project"
    source.mkdir()
    (source / "main.py").write_text("print('ready')\n", encoding="utf-8")

    waiting = await resources_a.run_agent_task(
        _DEVICE_B,
        "inspect the shared source",
        context_id=None,
        message_id=_id(2198),
        input_paths=(str(source.resolve()),),
    )
    task_id = str(waiting["task_id"])
    assert task_id in resources_a._outbound_task_ids[_DEVICE_B]
    assert (_DEVICE_B, task_id) in resources_a._outbound_source_services
    requested_workspace = (
        tmp_path
        / "a-workspace"
        / "tasks"
        / "requested"
        / peer_directory(_DEVICE_B)
        / task_id
    )
    assert requested_workspace.exists()

    lease_key = next(
        key for key in dispatcher_b._owner_leases if key[2] == task_id
    )
    dispatcher_b._owner_leases[lease_key] = 0.0
    await resources_a._renew_all_outbound_task_leases()

    mirrored = dispatcher_a.task_store.get_task("received", _DEVICE_B, task_id)
    assert mirrored is not None
    assert mirrored["status"]["state"] == "TASK_STATE_CANCELED"
    assert task_id not in resources_a._outbound_task_ids.get(_DEVICE_B, set())
    assert (_DEVICE_B, task_id) not in resources_a._outbound_task_reconciliation
    assert (_DEVICE_B, task_id) not in resources_a._outbound_source_services
    assert (_DEVICE_B, task_id) not in resources_a._outbound_prepared_tasks
    assert not requested_workspace.exists()
    await dispatcher_a.drain(time.monotonic() + 1.0)
    await dispatcher_b.drain(time.monotonic() + 1.0)


@pytest.mark.asyncio
async def test_unavailable_lease_wakes_an_active_task_for_get_task_reconciliation(
) -> None:
    resources_a, resources_b, supervisor_a, supervisor_b = (
        _ready_wire_resources()
    )
    supervisor_a.deliver = lambda data: resources_b._handle_binding_bytes(
        22, base64.b64encode(data).decode("ascii")
    )
    supervisor_b.deliver = lambda data: resources_a._handle_binding_bytes(
        11, base64.b64encode(data).decode("ascii")
    )
    dispatcher_a = resources_a._task_dispatcher
    dispatcher_b = resources_b._task_dispatcher
    assert dispatcher_a is not None and dispatcher_b is not None
    executor_b = dispatcher_b._executor
    assert isinstance(executor_b, _Executor)
    executor_b.gate = asyncio.Event()
    pending = asyncio.create_task(
        resources_a.run_agent_task(
            _DEVICE_B,
            "keep working until the owner lease changes",
            context_id=None,
            message_id=_id(2205),
        )
    )
    await asyncio.wait_for(executor_b.started.wait(), timeout=2)
    task_id = next(iter(resources_a._outbound_task_ids[_DEVICE_B]))
    lease_key = next(
        key for key in dispatcher_b._owner_leases if key[2] == task_id
    )
    dispatcher_b._owner_leases[lease_key] = 0.0

    await resources_a._renew_all_outbound_task_leases()
    with pytest.raises(AgentMessageError, match="OWNER_LEASE_EXPIRED"):
        await asyncio.wait_for(pending, timeout=2)

    assert (_DEVICE_B, task_id) not in resources_a._outbound_task_reconciliation
    assert task_id not in resources_a._outbound_task_ids.get(_DEVICE_B, set())
    await dispatcher_a.drain(time.monotonic() + 1.0)
    await dispatcher_b.drain(time.monotonic() + 1.0)


@pytest.mark.asyncio
async def test_unavailable_lease_keeps_state_when_get_task_outcome_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resources_a, resources_b, _supervisor_a, _supervisor_b = (
        _ready_wire_resources()
    )
    dispatcher_a = resources_a._task_dispatcher
    dispatcher_b = resources_b._task_dispatcher
    assert dispatcher_a is not None and dispatcher_b is not None
    task_id = _id(2199)
    dispatcher_a.task_store.put_task(
        "received",
        _DEVICE_B,
        build_task(
            task_id=task_id,
            context_id=_id(2200),
            state="TASK_STATE_INPUT_REQUIRED",
        ),
    )
    resources_a._track_outbound_task(_DEVICE_B, task_id)
    resources_a._mark_outbound_task_for_reconciliation(_DEVICE_B, task_id)

    async def unknown_response(
        *_args: Any,
        **_kwargs: Any,
    ) -> ApplicationResponse:
        return ApplicationResponse(
            _id(2201),
            "GetTask",
            {"id": task_id},
            None,
            "DEADLINE_EXCEEDED",
            outcome_unknown=True,
        )

    monkeypatch.setattr(resources_a, "_request_application", unknown_response)
    await resources_a._reconcile_inactive_outbound_tasks()

    assert task_id not in resources_a._outbound_task_ids.get(_DEVICE_B, set())
    assert (_DEVICE_B, task_id) in resources_a._outbound_task_reconciliation
    mirrored = dispatcher_a.task_store.get_task("received", _DEVICE_B, task_id)
    assert mirrored is not None
    assert mirrored["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    await dispatcher_a.drain(time.monotonic() + 1.0)
    await dispatcher_b.drain(time.monotonic() + 1.0)


@pytest.mark.asyncio
async def test_unavailable_lease_releases_state_after_definitive_task_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resources_a, resources_b, _supervisor_a, _supervisor_b = (
        _ready_wire_resources()
    )
    dispatcher_a = resources_a._task_dispatcher
    dispatcher_b = resources_b._task_dispatcher
    assert dispatcher_a is not None and dispatcher_b is not None
    task_id = _id(2202)
    dispatcher_a.task_store.put_task(
        "received",
        _DEVICE_B,
        build_task(
            task_id=task_id,
            context_id=_id(2203),
            state="TASK_STATE_INPUT_REQUIRED",
        ),
    )
    resources_a._track_outbound_task(_DEVICE_B, task_id)
    resources_a._mark_outbound_task_for_reconciliation(_DEVICE_B, task_id)

    async def missing_response(
        *_args: Any,
        **_kwargs: Any,
    ) -> ApplicationResponse:
        return ApplicationResponse(
            _id(2204),
            "GetTask",
            {"id": task_id},
            None,
            "TASK_NOT_FOUND",
        )

    monkeypatch.setattr(resources_a, "_request_application", missing_response)
    await resources_a._reconcile_inactive_outbound_tasks()

    assert (_DEVICE_B, task_id) not in resources_a._outbound_task_reconciliation
    mirrored = dispatcher_a.task_store.get_task("received", _DEVICE_B, task_id)
    assert mirrored is not None
    assert mirrored["status"]["state"] == "TASK_STATE_FAILED"
    assert mirrored["metadata"]["mclaw.failureReason"] == "TASK_NOT_FOUND"
    await dispatcher_a.drain(time.monotonic() + 1.0)
    await dispatcher_b.drain(time.monotonic() + 1.0)


@pytest.mark.asyncio
async def test_verified_chunked_file_reaches_remote_task_workspace(
    tmp_path: Path,
) -> None:
    resources_a, resources_b, supervisor_a, supervisor_b = (
        _ready_wire_resources(tmp_path)
    )
    supervisor_a.deliver = lambda data: resources_b._handle_binding_bytes(
        22, base64.b64encode(data).decode("ascii")
    )
    supervisor_b.deliver = lambda data: resources_a._handle_binding_bytes(
        11, base64.b64encode(data).decode("ascii")
    )
    dispatcher_b = resources_b._task_dispatcher
    assert dispatcher_b is not None
    executor_b = dispatcher_b._executor
    assert isinstance(executor_b, _Executor)
    executor_b.gate = asyncio.Event()
    content = bytes(range(251)) * 200
    source = tmp_path / "caller-input.png"
    source.write_bytes(content)

    pending = asyncio.create_task(
        resources_a.run_agent_task(
            _DEVICE_B,
            "inspect the attached input",
            context_id=None,
            message_id=_id(2190),
            input_paths=(str(source.resolve()),),
        )
    )
    await asyncio.wait_for(executor_b.started.wait(), timeout=2)

    request = executor_b.calls[0]
    work = Path(request.workspace_path)
    received = tuple(work.rglob("caller-input.png"))
    assert len(received) == 1
    assert received[0].is_absolute()
    assert received[0].read_bytes() == content
    assert str(received[0]) in request.system_context
    assert "vision_analyze" in request.system_context
    assert len(request.attachments) == 1
    assert request.attachments[0].path == str(received[0])
    assert request.attachments[0].mime_type == "image/png"
    assert request.attachments[0].kind.value == "image"
    assert request.attachments[0].origin.value == "dsoftbus"
    assert supervisor_a.send_count > 4

    executor_b.gate.set()
    result = await asyncio.wait_for(pending, timeout=2)
    assert result["success"] is True
    assert result["text"] == "reply:inspect the attached input"
    assert not work.parent.exists()
    assert resources_a._outbound_prepared_tasks == {}
    assert resources_a._task_dispatcher is not None
    await resources_a._task_dispatcher.drain(time.monotonic() + 1.0)
    await dispatcher_b.drain(time.monotonic() + 1.0)


@pytest.mark.asyncio
async def test_verified_chunked_artifact_returns_to_requesting_workspace(
    tmp_path: Path,
) -> None:
    resources_a, resources_b, supervisor_a, supervisor_b = (
        _ready_wire_resources(tmp_path)
    )
    supervisor_a.deliver = lambda data: resources_b._handle_binding_bytes(
        22, base64.b64encode(data).decode("ascii")
    )
    supervisor_b.deliver = lambda data: resources_a._handle_binding_bytes(
        11, base64.b64encode(data).decode("ascii")
    )
    dispatcher_b = resources_b._task_dispatcher
    assert dispatcher_b is not None
    executor_b = dispatcher_b._executor
    assert isinstance(executor_b, _Executor)
    content = bytes(range(251)) * 240

    def result_factory(request: RemoteTurnRequest) -> Mapping[str, Any]:
        output = Path(request.workspace_path) / "generated.bin"
        output.write_bytes(content)
        collector = TaskArtifactCollector(
            task_id=request.task_id,
            context_id=request.conversation_key.context_id,
            peer_device_id=request.conversation_key.peer_device_id,
            workspace=DsoftbusWorkspace(tmp_path / "b-workspace"),
        )
        artifact = collector.add_file(
            name="generated.bin",
            path=output,
            media_type="application/octet-stream",
            description="Generated test output.",
        )
        return {
            "completed": True,
            "final_response": "generated output",
            "interrupted": False,
            "messages": [
                {"role": "user", "content": request.text},
                {"role": "assistant", "content": "generated output"},
            ],
            "token_usage": {"input_tokens": 10, "output_tokens": 10},
            "artifacts": [dict(artifact)],
        }

    executor_b.result_factory = result_factory
    result = await resources_a.run_agent_task(
        _DEVICE_B,
        "generate a binary output",
        context_id=None,
        message_id=_id(2192),
    )

    assert result["success"] is True
    assert result["text"] == "generated output"
    artifact_parts = [
        part
        for receipt in result["artifacts"]
        for part in receipt["parts"]
        if part["filename"] == "01-generated.bin"
    ]
    assert len(artifact_parts) == 1
    local_path = Path(artifact_parts[0]["localPath"])
    assert local_path.is_absolute()
    assert local_path.is_relative_to(
        tmp_path
        / "a-workspace"
        / "artifacts"
        / "received"
        / peer_directory(_DEVICE_B)
        / result["task_id"]
    )
    assert local_path.read_bytes() == content
    assert artifact_parts[0]["byteLength"] == len(content)
    assert artifact_parts[0]["sha256"] == hashlib.sha256(content).hexdigest()
    produced_task = (
        tmp_path
        / "b-workspace"
        / "artifacts"
        / "produced"
        / peer_directory(_DEVICE_A)
        / result["task_id"]
    )
    assert not produced_task.exists()
    assert supervisor_a.send_count >= 8
    assert resources_a._task_dispatcher is not None
    await resources_a._task_dispatcher.drain(time.monotonic() + 1.0)
    await dispatcher_b.drain(time.monotonic() + 1.0)


@pytest.mark.asyncio
async def test_lost_result_ack_is_durably_retried_without_dropping_artifacts(
    tmp_path: Path,
) -> None:
    resources_a, resources_b, supervisor_a, supervisor_b = (
        _ready_wire_resources(tmp_path)
    )
    supervisor_a.deliver = lambda data: resources_b._handle_binding_bytes(
        22, base64.b64encode(data).decode("ascii")
    )
    supervisor_b.deliver = lambda data: resources_a._handle_binding_bytes(
        11, base64.b64encode(data).decode("ascii")
    )
    dispatcher_a = resources_a._task_dispatcher
    dispatcher_b = resources_b._task_dispatcher
    assert dispatcher_a is not None and dispatcher_b is not None
    original_request = resources_a._request_application
    ack_attempts = 0
    retained_before_retry = False

    async def fail_first_ack(connection, method, params, **kwargs):
        nonlocal ack_attempts, retained_before_retry
        if method == "mclaw.taskResult.ack":
            ack_attempts += 1
            if ack_attempts == 1:
                task_id = str(params["id"])
                pending = dispatcher_a.task_store.list_pending_result_acks(
                    _DEVICE_B,
                )
                assert [task["id"] for task in pending] == [task_id]
                produced_task = (
                    tmp_path
                    / "b-workspace"
                    / "artifacts"
                    / "produced"
                    / peer_directory(_DEVICE_A)
                    / task_id
                )
                retained_before_retry = produced_task.is_dir()
                raise AgentMessageError(
                    "DEADLINE_EXCEEDED", outcome_unknown=True
                )
        return await original_request(connection, method, params, **kwargs)

    resources_a._request_application = fail_first_ack  # type: ignore[method-assign]
    result = await resources_a.run_agent_task(
        _DEVICE_B,
        "return a result after a lost acknowledgement",
        context_id=None,
        message_id=_id(2193),
    )
    for _ in range(100):
        if ack_attempts >= 2 and _DEVICE_B not in resources_a._result_ack_retry_tasks:
            break
        await asyncio.sleep(0)
    else:
        pytest.fail("durable result acknowledgement was not retried")

    assert result["success"] is True
    assert retained_before_retry is True
    assert ack_attempts == 2
    assert dispatcher_a.task_store.list_pending_result_acks(
        _DEVICE_B,
    ) == ()
    produced_task = (
        tmp_path
        / "b-workspace"
        / "artifacts"
        / "produced"
        / peer_directory(_DEVICE_A)
        / result["task_id"]
    )
    assert not produced_task.exists()
    await dispatcher_a.drain(time.monotonic() + 1.0)
    await dispatcher_b.drain(time.monotonic() + 1.0)


@pytest.mark.asyncio
async def test_remote_task_browses_searches_and_fetches_shared_directory(
    tmp_path: Path,
) -> None:
    resources_a, resources_b, supervisor_a, supervisor_b = (
        _ready_wire_resources(tmp_path)
    )
    supervisor_a.deliver = lambda data: resources_b._handle_binding_bytes(
        22, base64.b64encode(data).decode("ascii")
    )
    supervisor_b.deliver = lambda data: resources_a._handle_binding_bytes(
        11, base64.b64encode(data).decode("ascii")
    )
    dispatcher_b = resources_b._task_dispatcher
    assert dispatcher_b is not None
    executor_b = dispatcher_b._executor
    assert isinstance(executor_b, _Executor)
    executor_b.gate = asyncio.Event()
    project = tmp_path / "project"
    source_file = project / "src" / "login.py"
    source_file.parent.mkdir(parents=True)
    content = (b"def authenticate(user):\n    return user == 'trusted'\n" * 500)
    source_file.write_bytes(content)
    (project / "README.md").write_text("trusted device sample", encoding="utf-8")

    pending = asyncio.create_task(
        resources_a.run_agent_task(
            _DEVICE_B,
            "inspect the shared project and fetch login.py",
            context_id=None,
            message_id=_id(2191),
            input_paths=(str(project.resolve()),),
        )
    )
    await asyncio.wait_for(executor_b.started.wait(), timeout=2)

    request = executor_b.calls[0]
    client = request.source_client
    assert client is not None
    scope = client.source_scopes[0]
    scope_id = str(scope["scopeId"])
    listing = await client.list_entries(
        {
            "scopeId": scope_id,
            "path": "",
            "depth": 2,
            "pageSize": 100,
            "pageToken": "",
        }
    )
    assert {entry["path"] for entry in listing["entries"]} >= {
        "README.md",
        "src",
        "src/login.py",
    }
    searched = await client.search(
        {
            "scopeId": scope_id,
            "path": "",
            "query": "authenticate",
            "mode": "content",
            "maxResults": 10,
        }
    )
    assert any(match["path"] == "src/login.py" for match in searched["matches"])
    fetched = await client.fetch(scope_id=scope_id, paths=("src/login.py",))
    receipt = fetched["files"][0]
    local_path = Path(receipt["localPath"])
    assert local_path.is_absolute()
    assert local_path.read_bytes() == content
    assert local_path.is_relative_to(Path(request.workspace_path))
    assert scope_id in request.system_context
    assert "dsoft_bus_source_fetch" in request.system_context

    executor_b.gate.set()
    result = await asyncio.wait_for(pending, timeout=2)
    assert result["success"] is True
    assert not Path(request.workspace_path).parent.exists()
    assert resources_a._outbound_source_services == {}
    assert resources_a._task_dispatcher is not None
    await resources_a._task_dispatcher.drain(time.monotonic() + 1.0)
    await dispatcher_b.drain(time.monotonic() + 1.0)


@pytest.mark.asyncio
async def test_input_required_resumes_the_same_task_with_text(
    tmp_path: Path,
) -> None:
    resources_a, resources_b, supervisor_a, supervisor_b = (
        _ready_wire_resources(tmp_path)
    )
    supervisor_a.deliver = lambda data: resources_b._handle_binding_bytes(
        22, base64.b64encode(data).decode("ascii")
    )
    supervisor_b.deliver = lambda data: resources_a._handle_binding_bytes(
        11, base64.b64encode(data).decode("ascii")
    )
    dispatcher_b = resources_b._task_dispatcher
    assert dispatcher_b is not None
    executor_b = dispatcher_b._executor
    assert isinstance(executor_b, _Executor)

    def result_factory(request: RemoteTurnRequest) -> Mapping[str, Any]:
        if len(executor_b.calls) == 1:
            return {
                "completed": False,
                "interrupted": False,
                "pending_task_input": True,
                "input_request": {
                    "message": "请提供目标模块名称",
                    "accepts": ["text"],
                },
                "messages": [
                    {"role": "user", "content": request.text},
                    {
                        "role": "assistant",
                        "content": "需要目标模块名称后才能继续。",
                    },
                ],
                "artifacts": [
                    {
                        "artifactId": _id(2200),
                        "name": "checkpoint.json",
                        "parts": [
                            {
                                "data": {"phase": "waiting-for-input"},
                                "mediaType": "application/json",
                            }
                        ],
                    }
                ],
                "token_usage": {"input_tokens": 8, "output_tokens": 5},
            }
        return {
            "completed": True,
            "interrupted": False,
            "final_response": f"已处理模块：{request.text}",
            "messages": [
                *request.history,
                {"role": "user", "content": request.text},
                {
                    "role": "assistant",
                    "content": f"已处理模块：{request.text}",
                },
            ],
            "token_usage": {"input_tokens": 12, "output_tokens": 7},
        }

    executor_b.result_factory = result_factory
    initial_input = tmp_path / "initial-context.txt"
    initial_input.write_text("initial context", encoding="utf-8")
    waiting = await resources_a.run_agent_task(
        _DEVICE_B,
        "检查项目",
        context_id=None,
        message_id=_id(2194),
        input_paths=(str(initial_input.resolve()),),
    )
    assert waiting["task_state"] == "TASK_STATE_INPUT_REQUIRED"
    assert waiting["input_request"]["accepts"] == ("text",)
    assert len(waiting["artifacts"]) == 1

    completed = await resources_a.continue_agent_task(
        _DEVICE_B,
        waiting["task_id"],
        waiting["input_request"]["requestId"],
        text="authentication",
        message_id=_id(2195),
    )

    assert completed["task_id"] == waiting["task_id"]
    assert completed["context_id"] == waiting["context_id"]
    assert completed["task_state"] == "TASK_STATE_COMPLETED"
    assert completed["text"] == "已处理模块：authentication"
    assert len(completed["artifacts"]) == 2
    assert len(executor_b.calls) == 2
    assert executor_b.calls[0].task_id == executor_b.calls[1].task_id
    assert executor_b.calls[1].history[-1]["content"] == (
        "需要目标模块名称后才能继续。"
    )
    assert resources_a._outbound_prepared_tasks == {}
    requested_task = (
        tmp_path
        / "a-workspace"
        / "tasks"
        / "requested"
        / peer_directory(_DEVICE_B)
        / waiting["task_id"]
    )
    assert not requested_task.exists()
    assert resources_a._task_dispatcher is not None
    await resources_a._task_dispatcher.drain(time.monotonic() + 1.0)
    await dispatcher_b.drain(time.monotonic() + 1.0)


@pytest.mark.asyncio
async def test_input_required_receives_a_verified_supplemental_file(
    tmp_path: Path,
) -> None:
    resources_a, resources_b, supervisor_a, supervisor_b = (
        _ready_wire_resources(tmp_path)
    )
    supervisor_a.deliver = lambda data: resources_b._handle_binding_bytes(
        22, base64.b64encode(data).decode("ascii")
    )
    supervisor_b.deliver = lambda data: resources_a._handle_binding_bytes(
        11, base64.b64encode(data).decode("ascii")
    )
    dispatcher_b = resources_b._task_dispatcher
    assert dispatcher_b is not None
    executor_b = dispatcher_b._executor
    assert isinstance(executor_b, _Executor)
    expected = bytes(range(251)) * 240
    received_paths: list[Path] = []

    def result_factory(request: RemoteTurnRequest) -> Mapping[str, Any]:
        if len(executor_b.calls) == 1:
            return {
                "completed": False,
                "interrupted": False,
                "pending_task_input": True,
                "input_request": {
                    "message": "请补充配置文件",
                    "accepts": ["file"],
                },
                "messages": [
                    {"role": "user", "content": request.text},
                    {"role": "assistant", "content": "缺少配置文件。"},
                ],
                "token_usage": {"input_tokens": 8, "output_tokens": 5},
            }
        matches = tuple(Path(request.workspace_path).rglob("config.bin"))
        assert len(matches) == 1
        assert matches[0].is_absolute()
        assert matches[0].read_bytes() == expected
        assert str(matches[0]) in request.system_context
        received_paths.append(matches[0])
        return {
            "completed": True,
            "interrupted": False,
            "final_response": "配置文件校验并处理完成",
            "messages": [
                *request.history,
                {"role": "user", "content": request.text},
                {"role": "assistant", "content": "配置文件校验并处理完成"},
            ],
            "token_usage": {"input_tokens": 15, "output_tokens": 6},
        }

    executor_b.result_factory = result_factory
    waiting = await resources_a.run_agent_task(
        _DEVICE_B,
        "检查配置",
        context_id=None,
        message_id=_id(2196),
    )
    source = tmp_path / "config.bin"
    source.write_bytes(expected)
    completed = await resources_a.continue_agent_task(
        _DEVICE_B,
        waiting["task_id"],
        waiting["input_request"]["requestId"],
        text="",
        message_id=_id(2197),
        input_paths=(str(source.resolve()),),
    )

    assert completed["task_id"] == waiting["task_id"]
    assert completed["task_state"] == "TASK_STATE_COMPLETED"
    assert completed["text"] == "配置文件校验并处理完成"
    assert len(received_paths) == 1
    assert "supplements" in received_paths[0].parts
    assert not received_paths[0].parents[4].exists()
    assert resources_a._task_dispatcher is not None
    await resources_a._task_dispatcher.drain(time.monotonic() + 1.0)
    await dispatcher_b.drain(time.monotonic() + 1.0)


@pytest.mark.asyncio
async def test_continuation_enforces_persisted_task_wide_file_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(protocol, "TASK_INPUT_TASK_BYTES_MAX", 10)
    resources_a, resources_b, supervisor_a, supervisor_b = (
        _ready_wire_resources(tmp_path)
    )
    supervisor_a.deliver = lambda data: resources_b._handle_binding_bytes(
        22, base64.b64encode(data).decode("ascii")
    )
    supervisor_b.deliver = lambda data: resources_a._handle_binding_bytes(
        11, base64.b64encode(data).decode("ascii")
    )
    dispatcher_b = resources_b._task_dispatcher
    assert dispatcher_b is not None
    executor_b = dispatcher_b._executor
    assert isinstance(executor_b, _Executor)

    def result_factory(request: RemoteTurnRequest) -> Mapping[str, Any]:
        return {
            "completed": False,
            "interrupted": False,
            "pending_task_input": True,
            "input_request": {
                "message": "请继续补充一个文件",
                "accepts": ["file"],
            },
            "messages": [
                *request.history,
                {"role": "user", "content": request.text},
                {"role": "assistant", "content": "还需要一个文件。"},
            ],
            "token_usage": {"input_tokens": 8, "output_tokens": 5},
        }

    executor_b.result_factory = result_factory
    first_wait = await resources_a.run_agent_task(
        _DEVICE_B,
        "收集输入文件",
        context_id=None,
        message_id=_id(2220),
    )
    first_file = tmp_path / "first.bin"
    first_file.write_bytes(b"123456")
    second_wait = await resources_a.continue_agent_task(
        _DEVICE_B,
        first_wait["task_id"],
        first_wait["input_request"]["requestId"],
        text="",
        message_id=_id(2221),
        input_paths=(str(first_file.resolve()),),
    )
    assert second_wait["task_state"] == "TASK_STATE_INPUT_REQUIRED"
    persisted = dispatcher_b.task_store.get_input_wait(
        _DEVICE_A, second_wait["task_id"]
    )
    assert persisted is not None
    assert persisted["inputBytesUsed"] == 6
    assert resources_a._task_workspace is not None
    requested_paths = resources_a._task_workspace.ensure_task(
        "requested",
        _DEVICE_B,
        second_wait["task_id"],
    )
    assert requested_paths.outgoing is not None
    accepted_snapshots = set(requested_paths.outgoing.glob("*.snapshot"))
    assert len(accepted_snapshots) == 1

    # Force the same durable rehydration path used after a Runtime restart.
    dispatcher_b._records.pop(second_wait["task_id"])
    second_file = tmp_path / "second.bin"
    second_file.write_bytes(b"12345")
    with pytest.raises(AgentMessageError, match="TASK_INPUT_TOO_LARGE"):
        await resources_a.continue_agent_task(
            _DEVICE_B,
            second_wait["task_id"],
            second_wait["input_request"]["requestId"],
            text="",
            message_id=_id(2222),
            input_paths=(str(second_file.resolve()),),
        )
    assert dispatcher_b.task_store.get_input_wait(
        _DEVICE_A, second_wait["task_id"]
    )["inputBytesUsed"] == 6
    assert set(requested_paths.outgoing.glob("*.snapshot")) == accepted_snapshots
    assert resources_a._task_dispatcher is not None
    await resources_a._task_dispatcher.drain(time.monotonic() + 1.0)
    await dispatcher_b.drain(time.monotonic() + 1.0)


@pytest.mark.asyncio
async def test_continuation_rejects_accumulated_source_scope_overflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(protocol, "TASK_INPUT_PATH_MAX", 1)
    resources_a, resources_b, supervisor_a, supervisor_b = (
        _ready_wire_resources(tmp_path)
    )
    supervisor_a.deliver = lambda data: resources_b._handle_binding_bytes(
        22, base64.b64encode(data).decode("ascii")
    )
    supervisor_b.deliver = lambda data: resources_a._handle_binding_bytes(
        11, base64.b64encode(data).decode("ascii")
    )
    dispatcher_b = resources_b._task_dispatcher
    assert dispatcher_b is not None
    executor_b = dispatcher_b._executor
    assert isinstance(executor_b, _Executor)

    def result_factory(request: RemoteTurnRequest) -> Mapping[str, Any]:
        return {
            "completed": False,
            "interrupted": False,
            "pending_task_input": True,
            "input_request": {
                "message": "请继续补充目录范围",
                "accepts": ["directory"],
            },
            "messages": [
                *request.history,
                {"role": "user", "content": request.text},
                {"role": "assistant", "content": "还需要目录范围。"},
            ],
            "token_usage": {"input_tokens": 8, "output_tokens": 5},
        }

    executor_b.result_factory = result_factory
    first_wait = await resources_a.run_agent_task(
        _DEVICE_B,
        "收集目录范围",
        context_id=None,
        message_id=_id(2223),
    )
    first_directory = tmp_path / "first-directory"
    first_directory.mkdir()
    second_wait = await resources_a.continue_agent_task(
        _DEVICE_B,
        first_wait["task_id"],
        first_wait["input_request"]["requestId"],
        text="",
        message_id=_id(2224),
        input_paths=(str(first_directory.resolve()),),
    )
    second_directory = tmp_path / "second-directory"
    second_directory.mkdir()
    with pytest.raises(AgentMessageError, match="TASK_INPUT_INVALID"):
        await resources_a.continue_agent_task(
            _DEVICE_B,
            second_wait["task_id"],
            second_wait["input_request"]["requestId"],
            text="",
            message_id=_id(2225),
            input_paths=(str(second_directory.resolve()),),
        )
    assert resources_a._task_dispatcher is not None
    await resources_a._task_dispatcher.drain(time.monotonic() + 1.0)
    await dispatcher_b.drain(time.monotonic() + 1.0)


@pytest.mark.asyncio
async def test_continuation_rejects_accumulated_system_context_overflow(
    tmp_path: Path,
) -> None:
    resources_a, resources_b, supervisor_a, supervisor_b = (
        _ready_wire_resources(tmp_path)
    )
    supervisor_a.deliver = lambda data: resources_b._handle_binding_bytes(
        22, base64.b64encode(data).decode("ascii")
    )
    supervisor_b.deliver = lambda data: resources_a._handle_binding_bytes(
        11, base64.b64encode(data).decode("ascii")
    )
    dispatcher_b = resources_b._task_dispatcher
    assert dispatcher_b is not None
    executor_b = dispatcher_b._executor
    assert isinstance(executor_b, _Executor)
    executor_b.result_factory = lambda request: {
        "completed": False,
        "interrupted": False,
        "pending_task_input": True,
        "input_request": {"message": "请补充文件", "accepts": ["file"]},
        "messages": [
            {"role": "user", "content": request.text},
            {"role": "assistant", "content": "等待文件。"},
        ],
        "token_usage": {"input_tokens": 8, "output_tokens": 5},
    }
    waiting = await resources_a.run_agent_task(
        _DEVICE_B,
        "检查上下文边界",
        context_id=None,
        message_id=_id(2226),
    )
    record = dispatcher_b._records[waiting["task_id"]]
    record.system_context = "x" * protocol.REMOTE_CONTEXT_UTF8_MAX
    source = tmp_path / "supplement.bin"
    source.write_bytes(b"x")

    with pytest.raises(AgentMessageError, match="TASK_INPUT_INVALID"):
        await resources_a.continue_agent_task(
            _DEVICE_B,
            waiting["task_id"],
            waiting["input_request"]["requestId"],
            text="",
            message_id=_id(2227),
            input_paths=(str(source.resolve()),),
        )
    assert len(record.system_context.encode("utf-8")) == (
        protocol.REMOTE_CONTEXT_UTF8_MAX
    )
    assert resources_a._task_dispatcher is not None
    await resources_a._task_dispatcher.drain(time.monotonic() + 1.0)
    await dispatcher_b.drain(time.monotonic() + 1.0)


@pytest.mark.asyncio
async def test_input_required_adds_a_bounded_read_only_directory_scope(
    tmp_path: Path,
) -> None:
    resources_a, resources_b, supervisor_a, supervisor_b = (
        _ready_wire_resources(tmp_path)
    )
    supervisor_a.deliver = lambda data: resources_b._handle_binding_bytes(
        22, base64.b64encode(data).decode("ascii")
    )
    supervisor_b.deliver = lambda data: resources_a._handle_binding_bytes(
        11, base64.b64encode(data).decode("ascii")
    )
    dispatcher_b = resources_b._task_dispatcher
    assert dispatcher_b is not None

    class DirectoryExecutor(_Executor):
        async def execute(self, request: RemoteTurnRequest) -> Mapping[str, Any]:
            self.calls.append(request)
            self.started.set()
            if len(self.calls) == 1:
                return {
                    "completed": False,
                    "interrupted": False,
                    "pending_task_input": True,
                    "input_request": {
                        "message": "请提供项目目录范围",
                        "accepts": ["directory"],
                    },
                    "messages": [
                        {"role": "user", "content": request.text},
                        {"role": "assistant", "content": "需要查看项目目录。"},
                    ],
                    "token_usage": {"input_tokens": 8, "output_tokens": 5},
                }
            assert request.source_client is not None
            scope = request.source_client.source_scopes[-1]
            listing = await request.source_client.list_entries(
                {
                    "scopeId": scope["scopeId"],
                    "path": "",
                    "depth": 2,
                    "pageSize": 100,
                    "pageToken": "",
                }
            )
            assert "src/module.py" in {
                entry["path"] for entry in listing["entries"]
            }
            fetched = await request.source_client.fetch(
                scope_id=scope["scopeId"],
                paths=("src/module.py",),
            )
            path = Path(fetched["files"][0]["localPath"])
            assert path.read_text(encoding="utf-8") == "VALUE = 42\n"
            return {
                "completed": True,
                "interrupted": False,
                "final_response": "目录范围分析完成",
                "messages": [
                    *request.history,
                    {"role": "user", "content": request.text},
                    {"role": "assistant", "content": "目录范围分析完成"},
                ],
                "token_usage": {"input_tokens": 15, "output_tokens": 6},
            }

    executor_b = DirectoryExecutor()
    dispatcher_b._executor = executor_b
    project = tmp_path / "project-scope"
    source = project / "src" / "module.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 42\n", encoding="utf-8")

    waiting = await resources_a.run_agent_task(
        _DEVICE_B,
        "分析项目模块",
        context_id=None,
        message_id=_id(2198),
    )
    completed = await resources_a.continue_agent_task(
        _DEVICE_B,
        waiting["task_id"],
        waiting["input_request"]["requestId"],
        text="",
        message_id=_id(2199),
        input_paths=(str(project.resolve()),),
    )

    assert completed["task_id"] == waiting["task_id"]
    assert completed["task_state"] == "TASK_STATE_COMPLETED"
    assert completed["text"] == "目录范围分析完成"
    assert len(executor_b.calls) == 2
    assert resources_a._task_dispatcher is not None
    await resources_a._task_dispatcher.drain(time.monotonic() + 1.0)
    await dispatcher_b.drain(time.monotonic() + 1.0)

def test_binding_keeps_one_request_open_for_ordered_task_stream() -> None:
    side_a, side_b = _ready_wire_bindings()
    message_id = _id(2201)
    task_id = _id(2202)
    context_id = _id(2203)
    artifact_id = _id(2204)
    request = side_a.request_application(
        "SendStreamingMessage",
        {
            "message": {
                "messageId": message_id,
                "role": "ROLE_USER",
                "parts": [{"text": "stream me"}],
            }
        },
    )
    inbound = side_b.receive(request.frame.data).application_request
    assert inbound is not None
    assert request.request_id in side_a._pending

    events = (
        {"task": build_task(
            task_id=task_id,
            context_id=context_id,
            state="TASK_STATE_SUBMITTED",
        )},
        build_status_update(
            task_id=task_id,
            context_id=context_id,
            state="TASK_STATE_WORKING",
        ),
        build_artifact_update(
            task_id=task_id,
            context_id=context_id,
            artifact={
                "artifactId": artifact_id,
                "name": "answer.txt",
                "parts": [{"text": "done", "mediaType": "text/plain"}],
            },
        ),
        build_status_update(
            task_id=task_id,
            context_id=context_id,
            state="TASK_STATE_COMPLETED",
        ),
    )
    routed = []
    for index, event in enumerate(events):
        completed = side_b.complete_application_stream_event(
            inbound,
            result=event,
            first=index == 0,
        )
        response = side_a.receive(completed.outbound[0].data).application_response
        assert response is not None
        assert response.request_id == request.request_id
        routed.append(response)
        if index < len(events) - 1:
            assert request.request_id in side_a._pending

    assert [response.stream_end for response in routed] == [
        False,
        False,
        False,
        True,
    ]
    assert request.request_id not in side_a._pending

@pytest.mark.asyncio
async def test_local_stop_uses_standard_cancel_task_after_shutdown_begins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resources_a, resources_b, supervisor_a, supervisor_b = _ready_wire_resources()
    supervisor_a.deliver = lambda data: resources_b._handle_binding_bytes(
        22, base64.b64encode(data).decode("ascii")
    )
    supervisor_b.deliver = lambda data: resources_a._handle_binding_bytes(
        11, base64.b64encode(data).decode("ascii")
    )
    cancel_methods: list[str] = []
    request_application = resources_a._request_application

    async def counted_request(
        connection: Any,
        method: str,
        params: Mapping[str, Any],
        **kwargs: Any,
    ) -> ApplicationResponse:
        if method == "CancelTask":
            cancel_methods.append(method)
        return await request_application(connection, method, params, **kwargs)

    monkeypatch.setattr(resources_a, "_request_application", counted_request)
    dispatcher_b = resources_b._task_dispatcher
    assert dispatcher_b is not None
    executor_b = dispatcher_b._executor
    assert isinstance(executor_b, _Executor)
    executor_b.gate = asyncio.Event()
    message_id = _id(2009)
    pending = asyncio.create_task(
        resources_a.run_agent_task(
            _DEVICE_B,
            "cancel-over-control",
            context_id=None,
            message_id=message_id,
        )
    )
    await executor_b.started.wait()

    for _ in range(50):
        tracked = resources_a._outbound_task_ids.get(_DEVICE_B, set())
        if tracked:
            break
        await asyncio.sleep(0)
    else:
        pytest.fail("outbound Task was not tracked")
    task_id = next(iter(tracked))
    direct_cancel = asyncio.create_task(
        resources_a._cancel_outbound_task(_DEVICE_B, task_id)
    )
    begin_shutdown = asyncio.create_task(resources_a.begin_shutdown())
    assert await direct_cancel is True
    await begin_shutdown
    with pytest.raises(AgentMessageError, match="AGENT_INTERRUPTED"):
        await asyncio.wait_for(pending, timeout=1.0)
    await resources_a._cancel_tracked_outbound_tasks()

    assert len(executor_b.interrupts) == 1
    assert cancel_methods == ["CancelTask"]
    assert resources_a._outbound_task_ids == {}
    assert resources_a._outbound_cancel_attempts == {}
    assert 11 in resources_a._connections_by_socket
    assert resources_a._application_waiters == {}
    assert supervisor_a.send_count >= 2
    assert supervisor_b.send_count >= 1
    assert resources_a._send_scheduler.diagnostic_snapshot()[
        "sendControlQueueCount"
    ] == 0
    assert resources_b._send_scheduler.diagnostic_snapshot()[
        "sendControlQueueCount"
    ] == 0
    assert resources_b._send_scheduler.diagnostic_snapshot()[
        "sendBusinessQueueCount"
    ] == 0
    assert resources_b._send_scheduler.diagnostic_snapshot()[
        "sendBusinessQueueBytes"
    ] == 0

    executor_b.gate.set()
    for _ in range(50):
        await asyncio.sleep(0.01)
        diagnostic = dispatcher_b.diagnostic_snapshot()
        binding_a = resources_a._connections_by_socket[11].binding
        if diagnostic["agentSessionTaskCount"] == 0 and not binding_a._pending:
            break
    assert diagnostic["agentSessionTaskCount"] == 0
    assert diagnostic["inflightMessageCount"] == 0
    assert binding_a._pending == {}
    assert resources_a._task_dispatcher is not None
    assert await resources_a._task_dispatcher.drain(time.monotonic() + 1.0)
    assert await dispatcher_b.drain(time.monotonic() + 1.0)


@pytest.mark.asyncio
@pytest.mark.parametrize("valid", (True, False))
async def test_cancel_task_requires_an_exact_canceled_task_response(
    monkeypatch: pytest.MonkeyPatch,
    valid: bool,
) -> None:
    resources_a, resources_b, supervisor_a, _supervisor_b = _ready_wire_resources()
    task_id = _id(2011)
    result: Mapping[str, Any] = (
        build_task(
            task_id=task_id,
            context_id=_id(2012),
            state="TASK_STATE_CANCELED",
        )
        if valid
        else {"accepted": False}
    )

    async def response(*_args: Any, **_kwargs: Any) -> ApplicationResponse:
        return ApplicationResponse(
            _id(2010),
            "CancelTask",
            {"id": task_id},
            result,
            None,
        )

    monkeypatch.setattr(resources_a, "_request_application", response)
    assert await resources_a._cancel_outbound_task(_DEVICE_B, task_id) is valid
    assert (11 in resources_a._connections_by_socket) is valid
    assert supervisor_a.closed == ([] if valid else [11])
    assert resources_a._task_dispatcher is not None
    persisted = resources_a._task_dispatcher.task_store.get_task(
        "received", _DEVICE_B, task_id
    )
    assert (persisted is not None) is valid
    if persisted is not None:
        assert persisted["status"]["state"] == "TASK_STATE_CANCELED"
    assert resources_b._task_dispatcher is not None
    assert await resources_a._task_dispatcher.drain(time.monotonic() + 1.0)
    assert await resources_b._task_dispatcher.drain(time.monotonic() + 1.0)


@pytest.mark.asyncio
async def test_task_stream_disconnect_recovers_with_get_then_subscribe() -> None:
    resources_a, resources_b, supervisor_a, supervisor_b = _ready_wire_resources()
    supervisor_a.deliver = lambda data: resources_b._handle_binding_bytes(
        22, base64.b64encode(data).decode("ascii")
    )
    supervisor_b.deliver = lambda data: resources_a._handle_binding_bytes(
        11, base64.b64encode(data).decode("ascii")
    )
    dispatcher_b = resources_b._task_dispatcher
    assert dispatcher_b is not None
    executor_b = dispatcher_b._executor
    assert isinstance(executor_b, _Executor)
    executor_b.gate = asyncio.Event()

    pending = asyncio.create_task(
        resources_a.run_agent_task(
            _DEVICE_B,
            "survive-one-generation",
            context_id=None,
            message_id=_id(2099),
        )
    )
    await asyncio.wait_for(executor_b.started.wait(), timeout=1.0)

    resources_a._close_connection(11, reconnect=False)
    resources_b._close_connection(22, reconnect=False)
    for _ in range(5):
        await asyncio.sleep(0)
    assert pending.done() is False

    side_a, side_b = _ready_wire_bindings(generation=2)
    resources_a._connections_by_socket[12] = _Connection(
        device_id=_DEVICE_B,
        generation=2,
        mtu=protocol.REMOTE_FRAME_MAX,
        network_id="network-b-2",
        socket=12,
        binding=side_a,
    )
    resources_a._socket_by_device[_DEVICE_B] = 12
    resources_a._send_scheduler.register(12, 2)
    resources_b._connections_by_socket[23] = _Connection(
        device_id=_DEVICE_A,
        generation=2,
        mtu=protocol.REMOTE_FRAME_MAX,
        network_id="network-a-2",
        socket=23,
        binding=side_b,
    )
    resources_b._socket_by_device[_DEVICE_A] = 23
    resources_b._send_scheduler.register(23, 2)
    supervisor_a.deliver = lambda data: resources_b._handle_binding_bytes(
        23, base64.b64encode(data).decode("ascii")
    )
    supervisor_b.deliver = lambda data: resources_a._handle_binding_bytes(
        12, base64.b64encode(data).decode("ascii")
    )
    executor_b.gate.set()

    result = await asyncio.wait_for(pending, timeout=2.0)
    assert result["success"] is True
    assert result["text"] == "reply:survive-one-generation"
    assert result["task_state"] == "TASK_STATE_COMPLETED"
    assert result["_mclawProvenance"]["connectionGeneration"] == 2
    assert len(executor_b.calls) == 1
    assert resources_a._application_waiters == {}
    assert resources_a._task_dispatcher is not None
    assert resources_b._task_dispatcher is not None
    await resources_a._task_dispatcher.drain(time.monotonic() + 1.0)
    await resources_b._task_dispatcher.drain(time.monotonic() + 1.0)


@pytest.mark.asyncio
async def test_inflight_admission_refreshes_bounded_resource_health_cache() -> None:
    resources_a, resources_b, supervisor_a, supervisor_b = _ready_wire_resources()
    supervisor_a.deliver = lambda data: resources_b._handle_binding_bytes(
        22, base64.b64encode(data).decode("ascii")
    )
    supervisor_b.deliver = lambda data: resources_a._handle_binding_bytes(
        11, base64.b64encode(data).decode("ascii")
    )
    published: list[dict[str, Any]] = []
    resources_b.set_health_change_callback(lambda value: published.append(dict(value)))
    resources_b.local_turn_started("local-admission-fence")
    sender_send_baseline = resources_a.cached_diagnostic()["operationCounts"].get(
        "send_bytes", 0
    )

    pending = asyncio.create_task(
        resources_a.run_agent_task(
            _DEVICE_B,
            "held",
            context_id=None,
            message_id=_id(2100),
        )
    )
    for _ in range(20):
        diagnostic = dict(resources_b.cached_diagnostic())
        if diagnostic["inflightMessageCount"] == 1:
            break
        await asyncio.sleep(0)
    else:
        pytest.fail("inflight admission was not published")

    assert diagnostic["dispatchExecutionCount"] == 0
    assert diagnostic["localTurnCount"] == 1
    assert resources_a.cached_diagnostic()["operationCounts"]["send_bytes"] == (
        sender_send_baseline + 1
    )
    assert any(
        value.get("remoteAccepted") == 1
        and value.get("dispatchQueueBytes", 0) > 0
        for value in published
    )

    resources_b.local_turn_finished("local-admission-fence")
    result = await asyncio.wait_for(pending, timeout=1.0)
    assert result["success"] is True
    assert result["text"] == "reply:held"
    assert resources_a._task_dispatcher is not None
    assert resources_b._task_dispatcher is not None
    await resources_a._task_dispatcher.drain(time.monotonic() + 1.0)
    await resources_b._task_dispatcher.drain(time.monotonic() + 1.0)


@pytest.mark.asyncio
async def test_additional_text_artifact_does_not_replace_final_response() -> None:
    resources_a, resources_b, supervisor_a, supervisor_b = _ready_wire_resources()
    supervisor_a.deliver = lambda data: resources_b._handle_binding_bytes(
        22, base64.b64encode(data).decode("ascii")
    )
    supervisor_b.deliver = lambda data: resources_a._handle_binding_bytes(
        11, base64.b64encode(data).decode("ascii")
    )
    dispatcher_b = resources_b._task_dispatcher
    assert dispatcher_b is not None
    executor_b = dispatcher_b._executor
    assert isinstance(executor_b, _Executor)
    executor_b.result_override = {
        "completed": True,
        "interrupted": False,
        "final_response": "summary-for-a",
        "messages": [
            {"role": "user", "content": "return-text-artifact"},
            {"role": "assistant", "content": "summary-for-a"},
        ],
        "artifacts": [
            {
                "name": "details.txt",
                "parts": [
                    {
                        "text": "artifact-body-is-not-the-final-answer",
                        "mediaType": "text/plain",
                    }
                ],
            }
        ],
    }

    result = await resources_a.run_agent_task(
        _DEVICE_B,
        "return-text-artifact",
        context_id=None,
        message_id=_id(2103),
    )

    assert result["text"] == "summary-for-a"
    assert len(result["artifacts"]) == 2
    received = [
        part
        for artifact in result["artifacts"]
        for part in artifact["parts"]
    ]
    assert any(part["filename"] == "01-details.txt" for part in received)
    assert resources_a._task_dispatcher is not None
    await resources_a._task_dispatcher.drain(time.monotonic() + 1.0)
    await dispatcher_b.drain(time.monotonic() + 1.0)


@pytest.mark.asyncio
async def test_resource_shutdown_delivers_terminal_before_closing_send_path() -> None:
    resources_a, resources_b, supervisor_a, supervisor_b = _ready_wire_resources()
    supervisor_a.deliver = lambda data: resources_b._handle_binding_bytes(
        22, base64.b64encode(data).decode("ascii")
    )
    supervisor_b.deliver = lambda data: resources_a._handle_binding_bytes(
        11, base64.b64encode(data).decode("ascii")
    )
    resources_b.local_turn_started("local-shutdown-drain")
    pending = asyncio.create_task(
        resources_a.run_agent_task(
            _DEVICE_B,
            "stop-before-provider",
            context_id=None,
            message_id=_id(2105),
        )
    )
    for _ in range(20):
        dispatcher = resources_b._task_dispatcher
        assert dispatcher is not None
        if dispatcher.diagnostic_snapshot()["inflightMessageCount"] == 1:
            break
        await asyncio.sleep(0)
    else:
        pytest.fail("message was not admitted before resource shutdown")

    await resources_b.begin_shutdown()
    resources_b.local_turn_finished("local-shutdown-drain")
    with pytest.raises(AgentMessageError) as caught:
        await asyncio.wait_for(pending, timeout=1.0)
    assert caught.value.code == "RUNTIME_STOPPING"
    assert caught.value.outcome_unknown is False
    assert resources_a._application_waiters == {}
    diagnostic = dict(resources_b.cached_diagnostic())
    assert diagnostic["remoteAccepted"] == 1
    # The request was admitted, then terminalized during shutdown.  It is a
    # failed Task, not a rejected admission.
    assert diagnostic["remoteRejectedByCode"] == {}
    assert resources_b._health_updates()["remoteRejected"] == 0
    assert diagnostic["dispatchQueueCount"] == 0
    assert diagnostic["dispatchQueueBytes"] == 0
    assert diagnostic["agentSessionTaskCount"] == 0
    assert resources_b._send_scheduler.diagnostic_snapshot()[
        "sendBusinessQueueCount"
    ] == 0
    assert resources_a._task_dispatcher is not None
    assert resources_b._task_dispatcher is not None
    await resources_a._task_dispatcher.drain(time.monotonic() + 1.0)
    await resources_b._task_dispatcher.drain(time.monotonic() + 1.0)


@pytest.mark.asyncio
async def test_two_ready_resources_exchange_agent_messages_in_both_directions() -> None:
    side_a, side_b = _ready_wire_bindings()
    supervisor_a = _WireSupervisor()
    supervisor_b = _WireSupervisor()
    executor_a = _Executor()
    executor_b = _Executor()
    resources_a = DiscoveryOwnerResources(
        supervisor=supervisor_a,  # type: ignore[arg-type]
        provider_ready=True,
        provider_readiness_code="",
        provider_runtime=_PROVIDER_A,
        message_config=_config(),
        message_executor=executor_a,
    )
    resources_b = DiscoveryOwnerResources(
        supervisor=supervisor_b,  # type: ignore[arg-type]
        provider_ready=True,
        provider_readiness_code="",
        provider_runtime=_PROVIDER_B,
        message_config=_config(),
        message_executor=executor_b,
    )
    resources_a._require_owner(establish=True)
    resources_b._require_owner(establish=True)
    assert resources_a._owner_thread_id == threading.get_ident()
    connection_a = _Connection(
        device_id=_DEVICE_B,
        generation=1,
        mtu=protocol.REMOTE_FRAME_MAX,
        network_id="network-b",
        socket=11,
        binding=side_a,
    )
    connection_b = _Connection(
        device_id=_DEVICE_A,
        generation=1,
        mtu=protocol.REMOTE_FRAME_MAX,
        network_id="network-a",
        socket=22,
        binding=side_b,
    )
    resources_a._connections_by_socket[11] = connection_a
    resources_a._socket_by_device[_DEVICE_B] = 11
    resources_a._send_scheduler.register(11, 1)
    resources_b._connections_by_socket[22] = connection_b
    resources_b._socket_by_device[_DEVICE_A] = 22
    resources_b._send_scheduler.register(22, 1)
    supervisor_a.deliver = lambda data: resources_b._handle_binding_bytes(
        22, base64.b64encode(data).decode("ascii")
    )
    supervisor_b.deliver = lambda data: resources_a._handle_binding_bytes(
        11, base64.b64encode(data).decode("ascii")
    )

    a_to_b = await asyncio.wait_for(
        resources_a.run_agent_task(
            _DEVICE_B,
            "from-a",
            context_id=None,
            message_id=_id(2101),
        ),
        timeout=1.0,
    )
    b_to_a = await resources_b.run_agent_task(
        _DEVICE_A,
        "from-b",
        context_id=None,
        message_id=_id(2102),
    )

    assert a_to_b["success"] is True
    assert a_to_b["text"] == "reply:from-a"
    assert a_to_b["message_id"] == _id(2101)
    assert a_to_b["_mclawProvenance"]["peerRuntimeInstanceId"] == _RUNTIME_B
    assert b_to_a["success"] is True
    assert b_to_a["text"] == "reply:from-b"
    assert b_to_a["message_id"] == _id(2102)
    assert b_to_a["_mclawProvenance"]["peerRuntimeInstanceId"] == _RUNTIME_A
    assert len(executor_a.calls) == len(executor_b.calls) == 1
    assert resources_a._task_dispatcher is not None
    assert resources_b._task_dispatcher is not None
    assert resources_a._task_dispatcher.diagnostic_snapshot()[
        "idempotencyWaiterCount"
    ] == 0
    assert resources_b._task_dispatcher.diagnostic_snapshot()[
        "idempotencyWaiterCount"
    ] == 0
    health_a = resources_a._health_updates()
    health_b = resources_b._health_updates()
    assert health_a["remoteAccepted"] == health_b["remoteAccepted"] == 1
    assert health_a["remoteBudgetUsed"] == health_b["remoteBudgetUsed"] == 20
    assert health_a["remoteContextCount"] == health_b["remoteContextCount"] == 1
    assert health_a["agentIngressReservationCount"] == 0
    assert health_b["agentPendingCount"] == 0
    await resources_a._task_dispatcher.drain(time.monotonic() + 1.0)
    await resources_b._task_dispatcher.drain(time.monotonic() + 1.0)
