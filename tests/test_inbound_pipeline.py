# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import replace
from datetime import datetime

import pytest

from mclaw.channels.base import (
    AgentTurnResult,
    AttachmentKind,
    AttachmentOrigin,
    ChannelAttachment,
    ChannelMessage,
    ChannelMessageType,
    ChannelSource,
)
from mclaw.channels.inbound_pipeline import (
    InboundCapabilityError,
    InboundCapabilityPipeline,
)
from mclaw.channels.runner import AgentRunner, IngressQueueFullError


def _message(text: str = "hello", *, attachments: tuple[ChannelAttachment, ...] = ()) -> ChannelMessage:
    return ChannelMessage(
        text=text,
        source=ChannelSource(channel="test", chat_id="chat", user_id="user"),
        attachments=attachments,
    )


def _bare_runner(
    pipeline: InboundCapabilityPipeline | None = None,
    *,
    max_pending_messages: int = 8,
) -> AgentRunner:
    runner = AgentRunner.__new__(AgentRunner)
    runner._locks = {}
    runner._pending = {}
    runner._cancelled_sessions = set()
    runner._active_agents = {}
    runner._agents = OrderedDict()
    runner._ingress_queues = {}
    runner._ingress_generations = {}
    runner._session_tasks = {}
    runner._closing_sessions = {}
    runner.inbound_pipeline = pipeline or InboundCapabilityPipeline()
    runner.max_pending_messages = max_pending_messages
    return runner


def test_channel_message_keeps_legacy_constructor_and_adds_normalized_attachments() -> None:
    source = ChannelSource(channel="test", chat_id="chat")
    timestamp = datetime(2026, 1, 2, 3, 4, 5)

    legacy = ChannelMessage("legacy", source, ChannelMessageType.TEXT, {"raw": True}, timestamp)

    assert legacy.text == "legacy"
    assert legacy.attachments == ()
    assert legacy.capability_results == {}

    attachment = ChannelAttachment(
        kind=AttachmentKind.AUDIO,
        origin=AttachmentOrigin.VOICE_MESSAGE,
        path="C:/cache/voice.silk",
        filename="voice.silk",
        mime_type="audio/silk",
        size_bytes=123,
        duration_ms=456,
        codec="silk",
        metadata={"channel": "test"},
    )
    enriched = replace(
        legacy,
        attachments=(attachment,),
        capability_results={"asr": {"text": "transcript"}},
    )

    assert enriched.attachments == (attachment,)
    assert attachment.to_dict() == {
        "kind": "audio",
        "origin": "voice_message",
        "path": "C:/cache/voice.silk",
        "filename": "voice.silk",
        "mime_type": "audio/silk",
        "size_bytes": 123,
        "duration_ms": 456,
        "codec": "silk",
        "error": "",
        "metadata": {"channel": "test"},
    }


@pytest.mark.asyncio
async def test_empty_pipeline_is_identity_and_capabilities_run_in_order() -> None:
    original = _message()
    assert await InboundCapabilityPipeline().process(original) is original

    observed: list[tuple[str, str]] = []

    async def first(message: ChannelMessage) -> ChannelMessage:
        observed.append(("first", message.text))
        return replace(
            message,
            text=f"{message.text}:one",
            capability_results={**message.capability_results, "first": True},
        )

    class Second:
        name = "second"

        async def process(self, message: ChannelMessage) -> ChannelMessage:
            observed.append(("second", message.text))
            return replace(
                message,
                text=f"{message.text}:two",
                capability_results={**message.capability_results, "second": True},
            )

    result = await InboundCapabilityPipeline([first, Second()]).process(original)

    assert observed == [("first", "hello"), ("second", "hello:one")]
    assert result.text == "hello:one:two"
    assert result.capability_results == {"first": True, "second": True}


@pytest.mark.asyncio
async def test_runner_short_circuits_capability_failure_without_calling_agent() -> None:
    secret_path = "C:/private/messages/voice.silk"
    attachment = ChannelAttachment(
        kind=AttachmentKind.AUDIO,
        origin=AttachmentOrigin.VOICE_MESSAGE,
        path=secret_path,
    )

    class FailingCapability:
        name = "audio_transcription"

        async def process(self, _message: ChannelMessage) -> ChannelMessage:
            raise InboundCapabilityError(
                code="audio_decode_failed",
                detail=f"decoder rejected {secret_path}",
                safe_message="I could not process that voice message.",
            )

    runner = _bare_runner(InboundCapabilityPipeline([FailingCapability()]))
    agent_calls: list[str] = []
    replies: list[AgentTurnResult] = []

    async def run_single_turn(**kwargs) -> AgentTurnResult:
        agent_calls.append(kwargs["message"].text)
        return AgentTurnResult(session_id=kwargs["session_id"])

    async def reply(_message: ChannelMessage, result: AgentTurnResult) -> None:
        replies.append(result)

    runner._run_single_turn = run_single_turn
    result = await runner.handle_message(
        message=_message("", attachments=(attachment,)),
        session_id="session",
        conversation_history=None,
        reply_callback=reply,
    )

    assert agent_calls == []
    assert replies == [result]
    assert result.final_response == "I could not process that voice message."
    assert result.error == "audio_decode_failed"
    assert result.raw_result == {
        "capability_error": {
            "code": "audio_decode_failed",
            "capability": "audio_transcription",
        }
    }
    assert secret_path not in result.final_response
    assert secret_path not in result.error
    assert secret_path not in str(result.raw_result)


@pytest.mark.asyncio
async def test_runner_uses_bounded_fifo_and_rejects_without_dropping_accepted_messages() -> None:
    runner = _bare_runner(max_pending_messages=2)
    started = asyncio.Event()
    release = asyncio.Event()
    agent_order: list[str] = []
    replies: list[tuple[str, AgentTurnResult]] = []

    async def run_single_turn(**kwargs) -> AgentTurnResult:
        message = kwargs["message"]
        agent_order.append(message.text)
        if message.text == "one":
            started.set()
            await release.wait()
        return AgentTurnResult(
            session_id=kwargs["session_id"],
            final_response=message.text,
            raw_result={"messages": [{"role": "user", "content": message.text}]},
        )

    async def reply(message: ChannelMessage, result: AgentTurnResult) -> None:
        replies.append((message.text, result))

    runner._run_single_turn = run_single_turn
    first = asyncio.create_task(
        runner.handle_message(
            message=_message("one"),
            session_id="session",
            conversation_history=None,
            reply_callback=reply,
        )
    )
    await started.wait()

    second = await runner.handle_message(
        message=_message("two"),
        session_id="session",
        conversation_history=None,
        reply_callback=reply,
    )
    third = await runner.handle_message(
        message=_message("three"),
        session_id="session",
        conversation_history=None,
        reply_callback=reply,
    )
    overflow = await runner.handle_message(
        message=_message("four"),
        session_id="session",
        conversation_history=None,
        reply_callback=reply,
    )

    assert second.queued is True
    assert third.queued is True
    assert overflow.queued is False
    assert overflow.error
    assert overflow.raw_result == {"queue_full": True}
    assert agent_order == ["one"]

    release.set()
    final = await first

    assert final.final_response == "three"
    assert agent_order == ["one", "two", "three"]
    assert "four" not in agent_order
    assert [text for text, result in replies if not result.error] == ["one", "two", "three"]
    assert replies[0] == ("four", overflow)
    assert "session" not in runner._pending


@pytest.mark.asyncio
async def test_ingress_reservation_preserves_arrival_order_when_second_download_finishes_first() -> None:
    runner = _bare_runner()
    first_reservation = runner.reserve_ingress("session")
    second_reservation = runner.reserve_ingress("session")
    first_started = asyncio.Event()
    finish_first = asyncio.Event()
    agent_order: list[str] = []

    async def run_single_turn(**kwargs) -> AgentTurnResult:
        text = kwargs["message"].text
        agent_order.append(text)
        if text == "one":
            first_started.set()
            await finish_first.wait()
        return AgentTurnResult(
            session_id=kwargs["session_id"],
            final_response=text,
            raw_result={"messages": []},
        )

    async def reply(_message: ChannelMessage, _result: AgentTurnResult) -> None:
        return None

    runner._run_single_turn = run_single_turn
    # Simulate the second (smaller) media download completing first.
    second_task = asyncio.create_task(runner.handle_message(
        message=_message("two"),
        session_id="session",
        conversation_history=None,
        reply_callback=reply,
        ingress_reservation=second_reservation,
    ))
    await asyncio.sleep(0)
    assert agent_order == []

    first_task = asyncio.create_task(runner.handle_message(
        message=_message("one"),
        session_id="session",
        conversation_history=None,
        reply_callback=reply,
        ingress_reservation=first_reservation,
    ))
    await first_started.wait()
    second_result = await second_task
    assert second_result.queued is True

    finish_first.set()
    await first_task
    assert agent_order == ["one", "two"]
    assert runner._ingress_queues == {}


@pytest.mark.asyncio
async def test_abandoned_middle_reservation_cannot_overtake_the_head() -> None:
    runner = _bare_runner()
    first = runner.reserve_ingress("session")
    middle = runner.reserve_ingress("session")
    third = runner.reserve_ingress("session")
    runner.release_ingress(middle)

    assert await runner.await_ingress(first) is True
    third_wait = asyncio.create_task(runner.await_ingress(third))
    await asyncio.sleep(0)
    assert third_wait.done() is False

    runner.release_ingress(first)
    assert await third_wait is True
    runner.release_ingress(third)
    assert runner._ingress_queues == {}


def test_ingress_capacity_is_bounded_before_media_download() -> None:
    runner = _bare_runner(max_pending_messages=2)
    reservations = [runner.reserve_ingress("session") for _ in range(3)]

    with pytest.raises(IngressQueueFullError):
        runner.reserve_ingress("session")

    for reservation in reservations:
        runner.release_ingress(reservation)
    assert runner._ingress_queues == {}


@pytest.mark.asyncio
async def test_interrupt_cancels_reserved_message_and_cleans_managed_cache(tmp_path) -> None:
    runner = _bare_runner()
    reservation = runner.reserve_ingress("session")
    cached = tmp_path / "voice.wav"
    cached.write_bytes(b"voice")
    message = _message("", attachments=(ChannelAttachment(
        kind=AttachmentKind.AUDIO,
        origin=AttachmentOrigin.VOICE_MESSAGE,
        path=str(cached),
        metadata={"managed_cache": True},
    ),))

    assert runner.get_status("session") == "queued"
    assert runner.interrupt("session") is True
    result = await runner.handle_message(
        message=message,
        session_id="session",
        conversation_history=None,
        reply_callback=lambda *_args: None,
        ingress_reservation=reservation,
    )

    assert result.interrupted is True
    assert not cached.exists()
    assert runner.get_status("session") == "idle"


@pytest.mark.asyncio
async def test_cancelling_pre_admission_wait_cleans_managed_cache(tmp_path) -> None:
    runner = _bare_runner()
    head = runner.reserve_ingress("session")
    waiting = runner.reserve_ingress("session")
    cached = tmp_path / "waiting.wav"
    cached.write_bytes(b"voice")
    message = _message("", attachments=(ChannelAttachment(
        kind=AttachmentKind.AUDIO,
        origin=AttachmentOrigin.VOICE_MESSAGE,
        path=str(cached),
        metadata={"managed_cache": True},
    ),))

    task = asyncio.create_task(runner.handle_message(
        message=message,
        session_id="session",
        conversation_history=None,
        reply_callback=lambda *_args: None,
        ingress_reservation=waiting,
    ))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not cached.exists()
    runner.release_ingress(head)
    assert runner._ingress_queues == {}


def test_legacy_single_pending_slot_is_promoted_to_fifo() -> None:
    runner = _bare_runner(max_pending_messages=2)
    runner._pending["session"] = _message("legacy")

    assert runner._enqueue_pending("session", _message("new")) is True
    assert runner._enqueue_pending("session", _message("overflow")) is False
    assert runner._dequeue_pending("session").text == "legacy"
    assert runner._dequeue_pending("session").text == "new"
    assert runner._dequeue_pending("session") is None


def test_interrupt_and_reset_discard_the_entire_pending_fifo() -> None:
    runner = _bare_runner()
    assert runner._pending_capacity() == 8
    assert runner._enqueue_pending("session", _message("one")) is True
    assert runner._enqueue_pending("session", _message("two")) is True

    assert runner.interrupt("session") is True
    assert "session" not in runner._pending

    assert runner._enqueue_pending("session", _message("three")) is True
    assert runner._enqueue_pending("session", _message("four")) is True
    assert runner.reset_session("session") is True
    assert "session" not in runner._pending
