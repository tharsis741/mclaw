# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import base64
import threading
import time
from collections import deque
from collections.abc import Mapping
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
from mclaw.dsoftbus.binding import LocalBindingIdentity, derive_public_agent_id
from mclaw.dsoftbus.discovery_resources import DiscoveryOwnerResources, _Connection
from mclaw.dsoftbus.manifest import MANIFEST_SCHEMA, ManifestDescriptor
from mclaw.dsoftbus.softbus_binding import ApplicationResponse, SoftBusA2ABinding

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

    async def execute(self, request: RemoteTurnRequest) -> Mapping[str, Any]:
        self.calls.append(request)
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
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


def _ready_wire_resources() -> tuple[
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
    )
    resources_b = DiscoveryOwnerResources(
        supervisor=supervisor_b,  # type: ignore[arg-type]
        provider_ready=True,
        provider_readiness_code="",
        provider_runtime=_PROVIDER_B,
        message_config=_config(),
        message_executor=_Executor(),
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
