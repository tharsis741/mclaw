# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

from mclaw.agent.transports import anthropic_messages as anthropic_module
from mclaw.agent.transports import openai_chat_completions as openai_module
from mclaw.agent.transports.anthropic_messages import AnthropicMessagesTransport
from mclaw.agent.transports.base import (
    BoundedNormalizedEventBuffer,
    ModelCallError,
    ModelCallOptions,
    ModelTransport,
    StreamBufferLimitError,
)
from mclaw.agent.transports.openai_chat_completions import (
    OpenAIChatCompletionsTransport,
)
from mclaw.dsoftbus.provider_readiness import resolve_provider_readiness
from mclaw.providers.generic import (
    GenericAnthropicCompatibleProfile,
    GenericOpenAICompatibleProfile,
)
from mclaw.providers.runtime import ProviderRuntimeContext


def _openai_context() -> ProviderRuntimeContext:
    return ProviderRuntimeContext(
        profile=GenericOpenAICompatibleProfile(
            name="openai-compatible",
            display_name="OpenAI compatible",
        ),
        model="model",
        api_key="secret",
        base_url="https://provider.test/api",
    )


def _anthropic_context() -> ProviderRuntimeContext:
    return ProviderRuntimeContext(
        profile=GenericAnthropicCompatibleProfile(
            name="anthropic-compatible",
            display_name="Anthropic compatible",
            api_mode="anthropic_messages",
            auth_scheme="anthropic_x_api_key",
        ),
        model="model",
        api_key="secret",
        base_url="https://provider.test",
    )


class _Stream:
    def __init__(self, values: list[Any] | None = None) -> None:
        self.values = values or []
        self.closed = threading.Event()

    def __iter__(self):
        return iter(self.values)

    def close(self) -> None:
        self.closed.set()


def test_normalized_buffer_releases_bytes_and_terminal_does_not_need_a_slot() -> None:
    buffer = BoundedNormalizedEventBuffer(max_items=1, max_bytes=64)
    stop = threading.Event()
    assert buffer.put({"text": "x"}, deadline_monotonic=None, stop_requested=stop)
    occupied = buffer.diagnostic_snapshot()
    assert occupied["eventCount"] == 1
    assert occupied["byteCount"] > 0

    error = StreamBufferLimitError("terminal")
    buffer.finish(error)
    assert buffer.finished is True
    assert buffer.terminal_error is error
    event = buffer.get(0.0)
    assert event is not None and event.value == {"text": "x"}
    assert buffer.diagnostic_snapshot()["byteCount"] == 0
    assert buffer.get(0.0) is None


def test_provider_readiness_uses_the_selected_transport_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert resolve_provider_readiness(None) == (False, "PROVIDER_MISSING")
    assert resolve_provider_readiness(_openai_context()) == (True, "")
    assert resolve_provider_readiness(_anthropic_context()) == (True, "")
    assert OpenAIChatCompletionsTransport.supports_dsoftbus_remote_fence is True
    assert AnthropicMessagesTransport.supports_dsoftbus_remote_fence is True

    class UnsupportedTransport(ModelTransport):
        def call(self, **_kwargs: Any):
            raise AssertionError("not called")

    monkeypatch.setattr(
        "mclaw.dsoftbus.provider_readiness.resolve_transport_class",
        lambda _context: UnsupportedTransport,
    )
    assert resolve_provider_readiness(_openai_context()) == (
        False,
        "TRANSPORT_FENCE_UNSUPPORTED",
    )


def test_openai_deadline_quarantines_and_closes_late_create_result() -> None:
    stream = _Stream()
    registered: list[Any] = []
    unregistered: list[Any] = []

    class Completions:
        @staticmethod
        def create(**_kwargs: Any) -> _Stream:
            time.sleep(0.04)
            return stream

    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    transport = OpenAIChatCompletionsTransport(_openai_context(), client)
    with pytest.raises(ModelCallError) as raised:
        transport.call(
            messages=[{"role": "user", "content": "hello"}],
            tools=[],
            options=ModelCallOptions(
                stream=True,
                timeout=1.0,
                deadline_monotonic=time.monotonic() + 0.01,
                stream_queue_max_items=2,
                stream_queue_max_bytes=1_024,
                stream_accumulator_max_bytes=1_024,
                response_utf8_max_bytes=1_024,
                register_worker=registered.append,
                unregister_worker=unregistered.append,
            ),
        )
    assert raised.value.code == "DEADLINE_EXCEEDED"
    time.sleep(0.08)
    assert len(registered) == 1
    assert unregistered == registered
    assert stream.closed.is_set()


def test_anthropic_deadline_quarantines_and_closes_late_create_result() -> None:
    registered: list[Any] = []
    unregistered: list[Any] = []
    stream = _Stream()

    class Manager:
        def __enter__(self) -> _Stream:
            return stream

        def __exit__(self, *_args: Any) -> bool:
            return False

    class Messages:
        @staticmethod
        def stream(**_kwargs: Any) -> Manager:
            time.sleep(0.04)
            return Manager()

    transport = AnthropicMessagesTransport(
        _anthropic_context(),
        SimpleNamespace(messages=Messages()),
    )
    with pytest.raises(ModelCallError) as raised:
        transport.call(
            messages=[{"role": "user", "content": "hello"}],
            tools=[],
            options=ModelCallOptions(
                stream=True,
                timeout=1.0,
                deadline_monotonic=time.monotonic() + 0.01,
                stream_queue_max_items=2,
                stream_queue_max_bytes=1_024,
                stream_accumulator_max_bytes=1_024,
                response_utf8_max_bytes=1_024,
                register_worker=registered.append,
                unregister_worker=unregistered.append,
            ),
        )
    assert raised.value.code == "DEADLINE_EXCEEDED"
    time.sleep(0.08)
    assert len(registered) == 1
    assert unregistered == registered
    assert stream.closed.is_set()


def test_nonstream_absolute_deadline_quarantines_both_provider_results() -> None:
    openai_response = _Stream()
    anthropic_response = _Stream()

    class OpenAICompletions:
        @staticmethod
        def create(**_kwargs: Any) -> _Stream:
            time.sleep(0.04)
            return openai_response

    class AnthropicMessages:
        @staticmethod
        def create(**_kwargs: Any) -> _Stream:
            time.sleep(0.04)
            return anthropic_response

    cases = (
        OpenAIChatCompletionsTransport(
            _openai_context(),
            SimpleNamespace(
                chat=SimpleNamespace(completions=OpenAICompletions())
            ),
        ),
        AnthropicMessagesTransport(
            _anthropic_context(),
            SimpleNamespace(messages=AnthropicMessages()),
        ),
    )
    for transport in cases:
        registered: list[Any] = []
        unregistered: list[Any] = []
        with pytest.raises(ModelCallError) as raised:
            transport.call(
                messages=[{"role": "user", "content": "hello"}],
                tools=[],
                options=ModelCallOptions(
                    stream=False,
                    timeout=1.0,
                    deadline_monotonic=time.monotonic() + 0.01,
                    register_worker=registered.append,
                    unregister_worker=unregistered.append,
                ),
            )
        assert raised.value.code == "DEADLINE_EXCEEDED"
        time.sleep(0.08)
        assert len(registered) == 1
        assert unregistered == registered

    assert openai_response.closed.is_set()
    assert anthropic_response.closed.is_set()


def test_transport_worker_start_failure_unregisters_before_return(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StartFailureThread:
        def __init__(self, **_kwargs: Any) -> None:
            return None

        @staticmethod
        def start() -> None:
            raise RuntimeError("thread start failed")

    cases = (
        (
            openai_module,
            OpenAIChatCompletionsTransport(
                _openai_context(),
                SimpleNamespace(
                    chat=SimpleNamespace(completions=SimpleNamespace())
                ),
            ),
        ),
        (
            anthropic_module,
            AnthropicMessagesTransport(
                _anthropic_context(),
                SimpleNamespace(messages=SimpleNamespace()),
            ),
        ),
    )
    for module, transport in cases:
        registered: list[Any] = []
        unregistered: list[Any] = []
        with monkeypatch.context() as scoped:
            scoped.setattr(module.threading, "Thread", StartFailureThread)
            with pytest.raises(ModelCallError):
                transport.call(
                    messages=[{"role": "user", "content": "hello"}],
                    tools=[],
                    options=ModelCallOptions(
                        stream=True,
                        register_worker=registered.append,
                        unregister_worker=unregistered.append,
                    ),
                )
        assert len(registered) == 1
        assert unregistered == registered


def test_openai_accumulator_limit_closes_stream_and_returns_stable_error() -> None:
    stream = _Stream(
        [
            {
                "choices": [
                    {
                        "delta": {"content": "x" * 128},
                        "finish_reason": "stop",
                    }
                ]
            }
        ]
    )
    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=lambda **_kwargs: stream)
        )
    )
    with pytest.raises(ModelCallError) as raised:
        OpenAIChatCompletionsTransport(_openai_context(), client).call(
            messages=[{"role": "user", "content": "hello"}],
            tools=[],
            options=ModelCallOptions(
                stream=True,
                response_utf8_max_bytes=64,
                stream_queue_max_items=2,
                stream_queue_max_bytes=1_024,
                stream_accumulator_max_bytes=256,
            ),
        )
    assert raised.value.code == "INVALID_AGENT_RESPONSE"
    assert stream.closed.is_set()


def test_anthropic_accumulator_limit_closes_stream_and_returns_stable_error() -> None:
    stream = _Stream(
        [
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "x" * 128},
            },
            {
                "type": "message_stop",
                "message": {"stop_reason": "end_turn", "usage": {}},
            },
        ]
    )

    class Manager:
        def __enter__(self) -> _Stream:
            return stream

        def __exit__(self, *_args: Any) -> bool:
            return False

    client = SimpleNamespace(
        messages=SimpleNamespace(stream=lambda **_kwargs: Manager())
    )
    with pytest.raises(ModelCallError) as raised:
        AnthropicMessagesTransport(_anthropic_context(), client).call(
            messages=[{"role": "user", "content": "hello"}],
            tools=[],
            options=ModelCallOptions(
                stream=True,
                response_utf8_max_bytes=64,
                stream_queue_max_items=4,
                stream_queue_max_bytes=2_048,
                stream_accumulator_max_bytes=256,
            ),
        )
    assert raised.value.code == "INVALID_AGENT_RESPONSE"
    assert stream.closed.is_set()
