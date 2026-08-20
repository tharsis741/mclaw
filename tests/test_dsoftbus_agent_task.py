# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

import pytest

from mclaw.dsoftbus import protocol
from mclaw.dsoftbus.a2a import build_status_update, validate_core_method
from mclaw.dsoftbus.agent_message import RemoteTurnRequest
from mclaw.dsoftbus.agent_message import AgentMessageError
from mclaw.dsoftbus.agent_task import DsoftbusTaskDispatcher, TaskSubscription


PEER = "urn:mclaw:device:oh:" + "a" * 64
RUNTIME = "9e13c126-e612-4f47-b882-8a7e9de3fba7"
REPLACEMENT_RUNTIME = "0a31a68d-b0bc-4e4c-83df-5da29b7c0844"
MESSAGE_ID = "0e4ba172-c081-48e9-a9d9-7b5714c98a42"


def _config() -> dict[str, Any]:
    return {
        "accept_remote_messages": True,
        "global_requests_per_minute": 10,
        "per_peer_requests_per_minute": 5,
        "remote_token_budget_per_hour": 100_000,
    }


def _call(method: str = "SendStreamingMessage"):
    return validate_core_method(
        method,
        {
            "message": {
                "messageId": MESSAGE_ID,
                "role": "ROLE_USER",
                "parts": [{"text": "检查项目并返回结果"}],
            }
        },
    )


def _message_call(
    message_id: str,
    text: str,
    *,
    context_id: str | None = None,
    method: str = "SendStreamingMessage",
):
    message: dict[str, Any] = {
        "messageId": message_id,
        "role": "ROLE_USER",
        "parts": [{"text": text}],
    }
    if context_id is not None:
        message["contextId"] = context_id
    return validate_core_method(method, {"message": message})


class _Executor:
    def __init__(self) -> None:
        self.requests: list[RemoteTurnRequest] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.interrupted: list[str] = []
        self.reaped: list[tuple[str, str]] = []
        self.reap_confirmed = True
        self.forgotten: list[str] = []
        self.artifacts: list[Mapping[str, Any]] | None = None
        self.messages: list[Mapping[str, Any]] | None = None

    async def execute(self, request: RemoteTurnRequest) -> Mapping[str, Any]:
        self.requests.append(request)
        self.started.set()
        if request.event_sink is not None:
            await request.event_sink(
                {
                    "type": "assistant.message",
                    "content": "先查看目录结构。",
                    "content_source": "reasoning_content",
                    "is_final": False,
                }
            )
            await request.event_sink(
                {
                    "type": "assistant.message",
                    "content": "目录分析完成，正在整理。",
                    "content_source": "content",
                    "is_final": False,
                }
            )
        await self.release.wait()
        result: dict[str, Any] = {
                "completed": True,
                "interrupted": False,
                "final_response": "远端完成结果",
                "messages": (
                    self.messages
                    if self.messages is not None
                    else [
                        {"role": "user", "content": request.text},
                        {"role": "assistant", "content": "远端完成结果"},
                    ]
                ),
                "token_usage": {"input_tokens": 10, "output_tokens": 5},
            }
        if self.artifacts is not None:
            result["artifacts"] = self.artifacts
        return MappingProxyType(result)

    def estimate_budget(self, _request: RemoteTurnRequest) -> int:
        return 100

    def update_provider_runtime(self, _context: Any | None) -> None:
        return None

    def interrupt(self, session_id: str) -> bool:
        self.interrupted.append(session_id)
        self.release.set()
        return True

    async def cancel_and_reap(
        self,
        session_id: str,
        task_id: str,
        _deadline: float,
    ) -> bool:
        self.interrupted.append(session_id)
        self.reaped.append((session_id, task_id))
        self.release.set()
        return self.reap_confirmed

    async def forget_session(self, _session_id: str, _deadline: float) -> bool:
        self.forgotten.append(_session_id)
        return True

    async def dispose(self, _deadline: float) -> bool:
        return True


class _Clock:
    def __init__(self) -> None:
        self.value = 1_000.0

    def __call__(self) -> float:
        return self.value


async def _collect(subscription) -> list[Mapping[str, Any]]:
    values: list[Mapping[str, Any]] = []
    while True:
        event = await subscription.next_event()
        if event is None:
            return values
        values.append(event)


async def _wait_for_terminal(
    dispatcher: DsoftbusTaskDispatcher,
    peer_runtime_instance_id: str,
    task_id: str,
) -> Mapping[str, Any]:
    for _ in range(200):
        task = dispatcher.get_task(PEER, peer_runtime_instance_id, task_id)
        if task["status"]["state"] in {
            "TASK_STATE_CANCELED",
            "TASK_STATE_COMPLETED",
            "TASK_STATE_FAILED",
        }:
            return task
        await asyncio.sleep(0.01)
    raise AssertionError("Task did not reach a terminal state")


def test_streaming_task_emits_existing_agent_messages_and_artifact(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        executor = _Executor()
        dispatcher = DsoftbusTaskDispatcher(
            config=_config(),
            executor=executor,
            provider_runtime=object(),
            provider_ready=True,
            state_root=tmp_path / "dsoftbus",
        )
        task, subscription = await dispatcher.submit(
            peer_device_id=PEER,
            peer_runtime_instance_id=RUNTIME,
            call=_call(),
            subscribe=True,
        )
        assert subscription is not None
        assert task["status"]["state"] == "TASK_STATE_SUBMITTED"
        await executor.started.wait()
        assert executor.requests[0].deadline_monotonic is None
        executor.release.set()
        events = await _collect(subscription)
        assert list(events[0]) == ["task"]
        streamed = [
            value["statusUpdate"]["status"]["message"]
            for value in events
            if "statusUpdate" in value
            and "message" in value["statusUpdate"]["status"]
        ]
        assert [item["parts"][0]["text"] for item in streamed] == [
            "先查看目录结构。",
            "目录分析完成，正在整理。",
        ]
        assert [
            item["metadata"]["mclaw.agentEvent"]["contentSource"]
            for item in streamed
        ] == ["reasoning_content", "content"]
        artifact_event = next(
            value["artifactUpdate"]
            for value in events
            if "artifactUpdate" in value
        )
        artifact = artifact_event["artifact"]
        assert artifact["parts"][0]["text"] == "远端完成结果"
        assert artifact["metadata"]["mclaw.artifactRole"] == "final-response"
        final = dispatcher.get_task(PEER, RUNTIME, task["id"])
        assert final["status"]["state"] == "TASK_STATE_COMPLETED"
        receipt = dispatcher.task_store.get_artifact_receipt(
            "owned", PEER, task["id"], artifact["artifactId"]
        )
        assert receipt is not None
        assert Path(receipt["parts"][0]["localPath"]).read_text(
            encoding="utf-8"
        ) == "远端完成结果"
        await dispatcher.drain(asyncio.get_running_loop().time() + 1)

    asyncio.run(scenario())


def test_structured_executor_artifact_is_streamed_and_materialized(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        executor = _Executor()
        executor.artifacts = [
            {
                "name": "report.json",
                "description": "Structured task result",
                "parts": [
                    {
                        "data": {"files": 3, "ok": True},
                        "mediaType": "application/json",
                    }
                ],
            }
        ]
        dispatcher = DsoftbusTaskDispatcher(
            config=_config(),
            executor=executor,
            provider_runtime=object(),
            provider_ready=True,
            state_root=tmp_path / "dsoftbus",
        )
        task, subscription = await dispatcher.submit(
            peer_device_id=PEER,
            peer_runtime_instance_id=RUNTIME,
            call=_call(),
            subscribe=True,
        )
        assert subscription is not None
        await executor.started.wait()
        executor.release.set()
        events = await _collect(subscription)
        updates = [
            event["artifactUpdate"]["artifact"]
            for event in events
            if "artifactUpdate" in event
        ]
        assert len(updates) == 2
        structured = next(
            artifact for artifact in updates if artifact.get("name") == "report.json"
        )
        receipt = dispatcher.task_store.get_artifact_receipt(
            "owned",
            PEER,
            task["id"],
            structured["artifactId"],
        )
        assert receipt is not None
        path = Path(receipt["parts"][0]["localPath"])
        assert path.name == "01-report.json"
        assert path.read_text(encoding="utf-8") == '{"files":3,"ok":true}'
        final = dispatcher.get_task(PEER, RUNTIME, task["id"])
        assert len(final["artifacts"]) == 2
        await dispatcher.drain(asyncio.get_running_loop().time() + 1)

    asyncio.run(scenario())


def test_stream_detach_does_not_cancel_and_subscribe_rejoins_active_task(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        executor = _Executor()
        dispatcher = DsoftbusTaskDispatcher(
            config=_config(),
            executor=executor,
            provider_runtime=object(),
            provider_ready=True,
            state_root=tmp_path / "dsoftbus",
        )
        task, first = await dispatcher.submit(
            peer_device_id=PEER,
            peer_runtime_instance_id=RUNTIME,
            call=_call(),
            subscribe=True,
        )
        assert first is not None
        await executor.started.wait()
        dispatcher.detach(first)
        dispatcher.generation_closed(PEER, RUNTIME, 1)
        assert executor.interrupted == []
        current = dispatcher.get_task(PEER, RUNTIME, task["id"])
        assert current["status"]["state"] == "TASK_STATE_WORKING"
        second = dispatcher.subscribe_task(PEER, RUNTIME, task["id"])
        initial = await second.next_event()
        assert initial is not None
        assert initial["task"]["id"] == task["id"]
        executor.release.set()
        remaining = await _collect(second)
        assert any("artifactUpdate" in event for event in remaining)
        await dispatcher.drain(asyncio.get_running_loop().time() + 1)

    asyncio.run(scenario())


def test_cancel_task_interrupts_receiver_and_returns_canceled_task(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        executor = _Executor()
        dispatcher = DsoftbusTaskDispatcher(
            config=_config(),
            executor=executor,
            provider_runtime=object(),
            provider_ready=True,
            state_root=tmp_path / "dsoftbus",
        )
        task, subscription = await dispatcher.submit(
            peer_device_id=PEER,
            peer_runtime_instance_id=RUNTIME,
            call=_call(),
            subscribe=True,
        )
        assert subscription is not None
        await executor.started.wait()
        canceled = await dispatcher.cancel_task(PEER, RUNTIME, task["id"])
        assert canceled["status"]["state"] == "TASK_STATE_CANCELED"
        assert executor.interrupted
        assert executor.reaped == [(executor.requests[0].conversation_key.session_id, task["id"])]
        events = await _collect(subscription)
        assert any(
            event.get("statusUpdate", {}).get("status", {}).get("state")
            == "TASK_STATE_CANCELED"
            for event in events
        )
        with pytest.raises(Exception, match="TASK_NOT_CANCELABLE"):
            await dispatcher.cancel_task(PEER, RUNTIME, task["id"])
        await dispatcher.drain(asyncio.get_running_loop().time() + 1)

    asyncio.run(scenario())


def test_cancel_task_fails_closed_when_process_tree_reap_is_unconfirmed(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        executor = _Executor()
        executor.reap_confirmed = False
        dispatcher = DsoftbusTaskDispatcher(
            config=_config(),
            executor=executor,
            provider_runtime=object(),
            provider_ready=True,
            state_root=tmp_path / "dsoftbus",
        )
        task, _subscription = await dispatcher.submit(
            peer_device_id=PEER,
            peer_runtime_instance_id=RUNTIME,
            call=_call(),
            subscribe=True,
        )
        await executor.started.wait()

        terminal = await dispatcher.cancel_task(PEER, RUNTIME, task["id"])

        assert terminal["status"]["state"] == "TASK_STATE_FAILED"
        assert terminal["metadata"]["mclaw.failureReason"] == "INTERNAL_ERROR"
        assert executor.reaped
        await dispatcher.drain(asyncio.get_running_loop().time() + 1)

    asyncio.run(scenario())


def test_send_message_returns_submitted_task_without_waiting_for_completion(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        executor = _Executor()
        dispatcher = DsoftbusTaskDispatcher(
            config=_config(),
            executor=executor,
            provider_runtime=object(),
            provider_ready=True,
            state_root=tmp_path / "dsoftbus",
        )
        task, subscription = await dispatcher.submit(
            peer_device_id=PEER,
            peer_runtime_instance_id=RUNTIME,
            call=_call("SendMessage"),
            subscribe=False,
        )
        assert subscription is None
        assert task["status"]["state"] == "TASK_STATE_SUBMITTED"
        await executor.started.wait()
        executor.release.set()
        for _ in range(100):
            current = dispatcher.get_task(PEER, RUNTIME, task["id"])
            if current["status"]["state"] == "TASK_STATE_COMPLETED":
                break
            await asyncio.sleep(0.01)
        assert current["status"]["state"] == "TASK_STATE_COMPLETED"
        await dispatcher.drain(asyncio.get_running_loop().time() + 1)

    asyncio.run(scenario())


def test_completed_request_replays_from_disk_without_second_execution(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        state_root = tmp_path / "dsoftbus"
        first_executor = _Executor()
        first = DsoftbusTaskDispatcher(
            config=_config(),
            executor=first_executor,
            provider_runtime=object(),
            provider_ready=True,
            state_root=state_root,
        )
        task, subscription = await first.submit(
            peer_device_id=PEER,
            peer_runtime_instance_id=RUNTIME,
            call=_call(),
            subscribe=True,
        )
        assert subscription is not None
        await first_executor.started.wait()
        first_executor.release.set()
        await _collect(subscription)
        assert first.get_task(PEER, RUNTIME, task["id"])["status"][
            "state"
        ] == "TASK_STATE_COMPLETED"
        await first.drain(asyncio.get_running_loop().time() + 1)

        second_executor = _Executor()
        second = DsoftbusTaskDispatcher(
            config=_config(),
            executor=second_executor,
            provider_runtime=object(),
            provider_ready=True,
            state_root=state_root,
        )
        replayed, replay = await second.submit(
            peer_device_id=PEER,
            peer_runtime_instance_id=RUNTIME,
            call=_call(),
            subscribe=True,
        )
        assert replay is not None
        events = await _collect(replay)
        assert replayed["id"] == task["id"]
        assert replayed["status"]["state"] == "TASK_STATE_COMPLETED"
        assert events == [{"task": replayed}]
        assert second_executor.requests == []
        await second.drain(asyncio.get_running_loop().time() + 1)

    asyncio.run(scenario())


def test_shutdown_terminalizes_and_releases_active_and_queued_tasks(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        executor = _Executor()
        dispatcher = DsoftbusTaskDispatcher(
            config=_config(),
            executor=executor,
            provider_runtime=object(),
            provider_ready=True,
            state_root=tmp_path / "dsoftbus",
        )
        dispatcher.local_turn_started("hold-local-turn")
        first_task, first_stream = await dispatcher.submit(
            peer_device_id=PEER,
            peer_runtime_instance_id=RUNTIME,
            call=_call(),
            subscribe=True,
        )
        second_call = validate_core_method(
            "SendStreamingMessage",
            {
                "message": {
                    "messageId": "2b91c538-e261-4a29-9036-3e5ce4fe060e",
                    "role": "ROLE_USER",
                    "parts": [{"text": "second"}],
                }
            },
        )
        second_task, second_stream = await dispatcher.submit(
            peer_device_id=PEER,
            peer_runtime_instance_id=RUNTIME,
            call=second_call,
            subscribe=True,
        )
        assert first_stream is not None and second_stream is not None
        assert first_task["id"] != second_task["id"]
        assert dispatcher.diagnostic_snapshot()["inflightMessageCount"] == 2

        assert await dispatcher.drain(
            asyncio.get_running_loop().time() + 1
        )
        for stream in (first_stream, second_stream):
            events = await _collect(stream)
            assert any(
                event.get("statusUpdate", {})
                .get("status", {})
                .get("state")
                == "TASK_STATE_CANCELED"
                for event in events
            )
        diagnostic = dispatcher.diagnostic_snapshot()
        for name in (
            "agentSessionTaskCount",
            "contextCount",
            "dispatchQueueBytes",
            "dispatchQueueCount",
            "inflightMessageCount",
            "localTurnCount",
            "responseCacheCount",
            "tokenReserved",
        ):
            assert diagnostic[name] == 0, name

    asyncio.run(scenario())


def test_per_peer_capacity_counts_dequeued_task_until_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(protocol, "PER_PEER_DISPATCH_PENDING_MAX", 1)
        executor = _Executor()
        dispatcher = DsoftbusTaskDispatcher(
            config=_config(),
            executor=executor,
            provider_runtime=object(),
            provider_ready=True,
            state_root=tmp_path / "dsoftbus",
        )
        dispatcher.local_turn_started("hold-local-turn")
        first, _ = await dispatcher.submit(
            peer_device_id=PEER,
            peer_runtime_instance_id=RUNTIME,
            call=_call(),
            subscribe=False,
        )
        for _ in range(10):
            if dispatcher.diagnostic_snapshot()["dispatchQueueCount"] == 0:
                break
            await asyncio.sleep(0)
        diagnostic = dispatcher.diagnostic_snapshot()
        assert diagnostic["dispatchQueueCount"] == 0
        assert diagnostic["inflightMessageCount"] == 1

        second_call = validate_core_method(
            "SendStreamingMessage",
            {
                "message": {
                    "messageId": "2b91c538-e261-4a29-9036-3e5ce4fe060e",
                    "role": "ROLE_USER",
                    "parts": [{"text": "second"}],
                }
            },
        )
        with pytest.raises(AgentMessageError, match="CAPACITY_BUSY"):
            await dispatcher.submit(
                peer_device_id=PEER,
                peer_runtime_instance_id=RUNTIME,
                call=second_call,
                subscribe=False,
            )
        assert dispatcher.diagnostic_snapshot()["remoteRejectedByCode"] == {
            "CAPACITY_BUSY": 1
        }

        canceled = await dispatcher.cancel_task(PEER, RUNTIME, first["id"])
        assert canceled["status"]["state"] == "TASK_STATE_CANCELED"
        replayed, _ = await dispatcher.submit(
            peer_device_id=PEER,
            peer_runtime_instance_id=RUNTIME,
            call=second_call,
            subscribe=False,
        )
        assert replayed["status"]["state"] == "TASK_STATE_SUBMITTED"
        assert await dispatcher.drain(asyncio.get_running_loop().time() + 1)

    asyncio.run(scenario())


def test_same_peer_runtime_reuses_context_history_without_retirement(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        executor = _Executor()
        executor.release.set()
        dispatcher = DsoftbusTaskDispatcher(
            config=_config(),
            executor=executor,
            provider_runtime=object(),
            provider_ready=True,
            state_root=tmp_path / "dsoftbus",
        )
        first, _ = await dispatcher.submit(
            peer_device_id=PEER,
            peer_runtime_instance_id=RUNTIME,
            call=_call(),
            subscribe=False,
        )
        await _wait_for_terminal(dispatcher, RUNTIME, first["id"])
        second, _ = await dispatcher.submit(
            peer_device_id=PEER,
            peer_runtime_instance_id=RUNTIME,
            call=_message_call(
                "4d72ad5f-0ef8-40ba-8980-a98d0ba8d3e4",
                "继续处理",
                context_id=first["contextId"],
            ),
            subscribe=False,
        )
        await _wait_for_terminal(dispatcher, RUNTIME, second["id"])
        assert len(executor.requests) == 2
        assert [dict(item) for item in executor.requests[1].history] == [
            {"role": "user", "content": "检查项目并返回结果"},
            {"role": "assistant", "content": "远端完成结果"},
        ]
        assert executor.forgotten == []
        assert dispatcher.diagnostic_snapshot()["contextCount"] == 1
        await dispatcher.drain(asyncio.get_running_loop().time() + 1)

    asyncio.run(scenario())


def test_peer_runtime_replacement_retires_old_idle_context_before_lookup(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        executor = _Executor()
        executor.release.set()
        dispatcher = DsoftbusTaskDispatcher(
            config=_config(),
            executor=executor,
            provider_runtime=object(),
            provider_ready=True,
            state_root=tmp_path / "dsoftbus",
        )
        old, _ = await dispatcher.submit(
            peer_device_id=PEER,
            peer_runtime_instance_id=RUNTIME,
            call=_call(),
            subscribe=False,
        )
        await _wait_for_terminal(dispatcher, RUNTIME, old["id"])
        old_session_id = executor.requests[0].conversation_key.session_id

        with pytest.raises(AgentMessageError, match="CONTEXT_NOT_FOUND"):
            await dispatcher.submit(
                peer_device_id=PEER,
                peer_runtime_instance_id=REPLACEMENT_RUNTIME,
                call=_message_call(
                    "4bfa1002-7de6-47c5-8230-0e145d7c56dd",
                    "不得复用旧上下文",
                    context_id=old["contextId"],
                ),
                subscribe=False,
            )
        assert executor.forgotten == [old_session_id]
        assert dispatcher.diagnostic_snapshot()["contextCount"] == 0

        fresh, _ = await dispatcher.submit(
            peer_device_id=PEER,
            peer_runtime_instance_id=REPLACEMENT_RUNTIME,
            call=_message_call(
                "c50fa195-a5fd-47b7-af39-8881df2aca84",
                "建立新上下文",
            ),
            subscribe=False,
        )
        await _wait_for_terminal(
            dispatcher, REPLACEMENT_RUNTIME, fresh["id"]
        )
        diagnostic = dispatcher.diagnostic_snapshot()
        assert diagnostic["contextCount"] == 1
        assert diagnostic["retiringContextCount"] == 0
        assert diagnostic["remoteRejectedByCode"] == {"CONTEXT_NOT_FOUND": 1}
        await dispatcher.drain(asyncio.get_running_loop().time() + 1)

    asyncio.run(scenario())


def test_context_capacity_evicts_oldest_idle_context_but_not_active(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def idle_scenario() -> None:
        monkeypatch.setattr(protocol, "PER_PEER_REMOTE_CONTEXT_MAX", 2)
        executor = _Executor()
        executor.release.set()
        dispatcher = DsoftbusTaskDispatcher(
            config=_config(),
            executor=executor,
            provider_runtime=object(),
            provider_ready=True,
            state_root=tmp_path / "idle",
        )
        tasks: list[Mapping[str, Any]] = []
        for message_id in (
            MESSAGE_ID,
            "ec9b2e2e-2f8d-4cfb-ab16-049173b45bb3",
            "5b75fd18-802b-4772-a904-9486d791021a",
        ):
            task, _ = await dispatcher.submit(
                peer_device_id=PEER,
                peer_runtime_instance_id=RUNTIME,
                call=_message_call(message_id, message_id),
                subscribe=False,
            )
            tasks.append(task)
            await _wait_for_terminal(dispatcher, RUNTIME, task["id"])
        assert dispatcher.diagnostic_snapshot()["contextCount"] == 2
        assert executor.forgotten == [
            executor.requests[0].conversation_key.session_id
        ]
        with pytest.raises(AgentMessageError, match="CONTEXT_NOT_FOUND"):
            await dispatcher.submit(
                peer_device_id=PEER,
                peer_runtime_instance_id=RUNTIME,
                call=_message_call(
                    "211ac7e3-bf9c-4174-93e3-c470958023af",
                    "已淘汰",
                    context_id=tasks[0]["contextId"],
                ),
                subscribe=False,
            )
        await dispatcher.drain(asyncio.get_running_loop().time() + 1)

    async def active_scenario() -> None:
        monkeypatch.setattr(protocol, "PER_PEER_REMOTE_CONTEXT_MAX", 1)
        executor = _Executor()
        dispatcher = DsoftbusTaskDispatcher(
            config=_config(),
            executor=executor,
            provider_runtime=object(),
            provider_ready=True,
            state_root=tmp_path / "active",
        )
        first, _ = await dispatcher.submit(
            peer_device_id=PEER,
            peer_runtime_instance_id=RUNTIME,
            call=_call(),
            subscribe=False,
        )
        await executor.started.wait()
        with pytest.raises(AgentMessageError, match="CAPACITY_BUSY"):
            await dispatcher.submit(
                peer_device_id=PEER,
                peer_runtime_instance_id=RUNTIME,
                call=_message_call(
                    "172d8a6f-fd6c-4d16-8997-4f60533aa931",
                    "不得淘汰活动上下文",
                ),
                subscribe=False,
            )
        assert executor.forgotten == []
        assert dispatcher.diagnostic_snapshot()["contextCount"] == 1
        await dispatcher.cancel_task(PEER, RUNTIME, first["id"])
        await dispatcher.drain(asyncio.get_running_loop().time() + 1)

    asyncio.run(idle_scenario())
    asyncio.run(active_scenario())


def test_context_history_drops_oldest_complete_turns_to_protocol_bound(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        executor = _Executor()
        executor.release.set()
        executor.messages = [
            {"role": role, "content": f"message-{index}"}
            for index in range(80)
            for role in ("user", "assistant")
        ]
        dispatcher = DsoftbusTaskDispatcher(
            config=_config(),
            executor=executor,
            provider_runtime=object(),
            provider_ready=True,
            state_root=tmp_path / "dsoftbus",
        )
        first, _ = await dispatcher.submit(
            peer_device_id=PEER,
            peer_runtime_instance_id=RUNTIME,
            call=_call(),
            subscribe=False,
        )
        await _wait_for_terminal(dispatcher, RUNTIME, first["id"])
        second, _ = await dispatcher.submit(
            peer_device_id=PEER,
            peer_runtime_instance_id=RUNTIME,
            call=_message_call(
                "55200e16-2288-4396-a08f-a5ab86b8e6ae",
                "验证裁剪历史",
                context_id=first["contextId"],
            ),
            subscribe=False,
        )
        await _wait_for_terminal(dispatcher, RUNTIME, second["id"])
        history = executor.requests[1].history
        assert len(history) == protocol.REMOTE_CONTEXT_MESSAGE_MAX
        assert len(
            json.dumps(
                [dict(item) for item in history],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ) <= protocol.REMOTE_CONTEXT_UTF8_MAX
        assert history[0]["content"] == "message-48"
        await dispatcher.drain(asyncio.get_running_loop().time() + 1)

    asyncio.run(scenario())


def test_task_response_cache_has_actual_bytes_ttl_and_hard_capacity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(protocol, "PER_PEER_RESPONSE_CACHE_CAP", 1)
        clock = _Clock()
        executor = _Executor()
        executor.release.set()
        dispatcher = DsoftbusTaskDispatcher(
            config=_config(),
            executor=executor,
            provider_runtime=object(),
            provider_ready=True,
            state_root=tmp_path / "dsoftbus",
            monotonic=clock,
        )
        first, _ = await dispatcher.submit(
            peer_device_id=PEER,
            peer_runtime_instance_id=RUNTIME,
            call=_call(),
            subscribe=False,
        )
        await _wait_for_terminal(dispatcher, RUNTIME, first["id"])
        cached = dispatcher.diagnostic_snapshot()
        assert cached["responseCacheCount"] == 1
        assert 0 < cached["responseCacheBytes"] <= protocol.REMOTE_FRAME_MAX

        second_call = _message_call(
            "dba63f3d-a307-4707-b815-6c33e17ca108",
            "等待缓存到期",
        )
        with pytest.raises(AgentMessageError, match="CAPACITY_BUSY"):
            await dispatcher.submit(
                peer_device_id=PEER,
                peer_runtime_instance_id=RUNTIME,
                call=second_call,
                subscribe=False,
            )
        assert dispatcher.diagnostic_snapshot()["contextCount"] == 1

        clock.value += float(protocol.RESPONSE_CACHE_TTL_S) + 0.001
        expired = dispatcher.diagnostic_snapshot()
        assert expired["responseCacheCount"] == 0
        assert expired["responseCacheBytes"] == 0
        assert expired["contextCount"] == 1
        # Cache expiry only releases memory.  The authoritative Task remains
        # available from the durable Task store.
        assert dispatcher.get_task(PEER, RUNTIME, first["id"])["id"] == first["id"]

        second, _ = await dispatcher.submit(
            peer_device_id=PEER,
            peer_runtime_instance_id=RUNTIME,
            call=second_call,
            subscribe=False,
        )
        await _wait_for_terminal(dispatcher, RUNTIME, second["id"])
        assert dispatcher.diagnostic_snapshot()["responseCacheCount"] == 1
        await dispatcher.drain(clock.value + 1)

    asyncio.run(scenario())


def test_subscription_overflow_is_explicit_and_task_can_be_resubscribed() -> None:
    async def scenario() -> None:
        subscription = TaskSubscription(
            "68eaf025-218f-49dc-ac78-b586b4695e5f",
            "5d75024f-d9da-46e1-bbb3-31f46232b827",
        )
        event = build_status_update(
            task_id=subscription.task_id,
            context_id=subscription.context_id,
            state="TASK_STATE_WORKING",
        )
        for _ in range(64):
            assert subscription._publish(event) is True
        assert subscription._publish(event) is False
        assert subscription.overflowed is True
        with pytest.raises(AgentMessageError, match="CAPACITY_BUSY"):
            while await subscription.next_event() is not None:
                pass

    asyncio.run(scenario())
