# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import threading
import time
import traceback
from types import SimpleNamespace
from typing import Any

import pytest

from mclaw.agent.transports.anthropic_messages import AnthropicMessagesTransport
from mclaw.agent.transports.base import (
    ModelCallError,
    ModelCallOptions,
    ModelTransport,
    ReasoningTrace,
    json_safe_value,
    normalize_model_call_error,
)
from mclaw.agent.transports.factory import create_transport
from mclaw.agent.transports.openai_chat_completions import OpenAIChatCompletionsTransport
from mclaw.providers.anthropic import AnthropicProfile
from mclaw.providers.base import ModelTraits, RuntimeProviderProfile
from mclaw.providers.generic import (
    GenericAnthropicCompatibleProfile,
    GenericOpenAICompatibleProfile,
)
from mclaw.providers.gemini import GoogleGeminiProfile
from mclaw.providers.minimax import MiniMaxProfile
from mclaw.providers.kimi import MoonshotKimiProfile
from mclaw.providers.openai import OpenAIProfile
from mclaw.providers.openrouter import OpenRouterProfile
from mclaw.providers.runtime import ProviderRuntimeContext
from mclaw.state import SessionDB


def _context(
    *,
    profile: RuntimeProviderProfile | None = None,
    model: str = "test-model",
    api_key: str = "sk-super-secret",
    base_url: str = "https://api.example.test/v1",
) -> ProviderRuntimeContext:
    return ProviderRuntimeContext(
        profile=profile
        or RuntimeProviderProfile(
            name="test-provider",
            display_name="Test Provider",
            context_limit_markers=("provider-window-code",),
        ),
        model=model,
        api_key=api_key,
        base_url=base_url,
    )


class _FakeCompletions:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result() if callable(self.result) else self.result


class _FakeOpenAIClient:
    def __init__(self, result: Any) -> None:
        self.completions = _FakeCompletions(result)
        self.chat = SimpleNamespace(completions=self.completions)


class _FakeStream:
    def __init__(self, values: list[Any]) -> None:
        self.values = values
        self.closed = False

    def __iter__(self):
        return iter(self.values)

    def close(self) -> None:
        self.closed = True


class _DelayedFirstStream(_FakeStream):
    def __init__(self, delay: float, values: list[Any]) -> None:
        super().__init__(values)
        self.delay = delay

    def __iter__(self):
        time.sleep(self.delay)
        return super().__iter__()


class _BlockingStream:
    def __init__(self, first: Any) -> None:
        self.first = first
        self.closed = threading.Event()

    def __iter__(self):
        yield self.first
        self.closed.wait(1)

    def close(self) -> None:
        self.closed.set()


class _DelayedUsageStream:
    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.closed = False

    def __iter__(self):
        yield {"choices": [{"delta": {"content": "done"}, "finish_reason": "stop"}]}
        time.sleep(self.delay)
        yield {"choices": [], "usage": {"prompt_tokens": 6, "completion_tokens": 2}}

    def close(self) -> None:
        self.closed = True


class _FakeMessageAPI:
    def __init__(self, *, response: Any = None, events: list[Any] | None = None) -> None:
        self.response = response
        self.events = events or []
        self.create_calls: list[dict[str, Any]] = []
        self.stream_calls: list[dict[str, Any]] = []
        self.closed = False

    def create(self, **kwargs: Any) -> Any:
        self.create_calls.append(kwargs)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    def stream(self, **kwargs: Any):
        self.stream_calls.append(kwargs)
        events = self.events
        owner = self

        class Manager:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def __iter__(self):
                return iter(events)

            def close(self):
                owner.closed = True

        return Manager()


class _FakeAnthropicClient:
    def __init__(self, api: _FakeMessageAPI) -> None:
        self.messages = api


def test_protocols_share_json_safe_sdk_normalization() -> None:
    class SDKValue:
        def model_dump(self, *, mode: str):
            assert mode == "json"
            return {"finite": 1.5, "nonfinite": float("nan"), "nested": ("x",)}

    assert json_safe_value(SDKValue()) == {
        "finite": 1.5,
        "nonfinite": "nan",
        "nested": ["x"],
    }


def test_gemini_tool_signature_uses_canonical_reasoning_format() -> None:
    profile = GoogleGeminiProfile(name="google", display_name="Google")
    context = _context(profile=profile, model="gemini-3.5-flash")
    response = {
        "choices": [{
            "message": {
                "content": "",
                "tool_calls": [{
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": "{}"},
                    "extra_content": {
                        "google": {"thought_signature": "opaque-signature"}
                    },
                }],
            },
            "finish_reason": "tool_calls",
        }],
    }

    result = OpenAIChatCompletionsTransport(
        context,
        _FakeOpenAIClient(response),
    ).call(messages=[{"role": "user", "content": "hello"}], tools=[], options=ModelCallOptions())

    assert result.reasoning is not None
    assert result.reasoning.format == "gemini_thought_signature"
    assert result.reasoning.payload == [{
        "field": "tool_calls.extra_content",
        "index": 0,
        "value": {"google": {"thought_signature": "opaque-signature"}},
    }]


def test_openai_stream_merges_usage_fragments_by_reported_field() -> None:
    profile = GenericOpenAICompatibleProfile(name="generic", display_name="Generic")
    context = _context(profile=profile)
    stream = _FakeStream([
        {
            "choices": [{"delta": {"content": "ok"}, "finish_reason": None}],
            "usage": {
                "prompt_tokens": 7,
                "prompt_tokens_details": {"cached_tokens": 3},
            },
        },
        {
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {
                "completion_tokens": 2,
                "prompt_tokens_details": {"cache_write_tokens": 2},
                "completion_tokens_details": {"reasoning_tokens": 1},
            },
        },
    ])

    result = OpenAIChatCompletionsTransport(
        context,
        _FakeOpenAIClient(stream),
    ).call(
        messages=[{"role": "user", "content": "hello"}],
        tools=[],
        options=ModelCallOptions(stream=True),
    )

    assert result.usage is not None
    assert result.usage.to_counter_delta() == {
        "input_tokens": 7,
        "output_tokens": 2,
        "cache_read_tokens": 3,
        "cache_write_tokens": 2,
        "reasoning_tokens": 1,
    }


def test_kimi_stream_reads_choice_level_usage() -> None:
    profile = MoonshotKimiProfile(name="moonshot", display_name="Moonshot")
    context = _context(profile=profile, model="kimi-k2.6")
    stream = _FakeStream([{
        "choices": [{
            "delta": {"content": "ok"},
            "finish_reason": "stop",
            "usage": {
                "prompt_tokens": 7,
                "completion_tokens": 2,
                "cached_tokens": 3,
            },
        }],
    }])

    result = OpenAIChatCompletionsTransport(
        context,
        _FakeOpenAIClient(stream),
    ).call(
        messages=[{"role": "user", "content": "hello"}],
        tools=[],
        options=ModelCallOptions(stream=True),
    )

    assert result.usage is not None
    assert result.usage.input_tokens == 7
    assert result.usage.output_tokens == 2
    assert result.usage.cache_read_tokens == 3


def test_factory_builds_zero_retry_protocol_clients(monkeypatch) -> None:
    import anthropic
    import openai

    captured: dict[str, dict[str, Any]] = {}

    class OpenAIClient:
        def __init__(self, **kwargs: Any) -> None:
            captured["openai"] = kwargs

    class AnthropicClient:
        def __init__(self, **kwargs: Any) -> None:
            captured["anthropic"] = kwargs

    monkeypatch.setattr(openai, "OpenAI", OpenAIClient)
    monkeypatch.setattr(anthropic, "Anthropic", AnthropicClient)

    local_profile = GenericOpenAICompatibleProfile(
        name="local",
        display_name="Local",
        provider_kind="host",
        credential_required=False,
    )
    openai_context = _context(
        profile=local_profile,
        api_key="",
        base_url="http://127.0.0.1:11434/v1",
    )
    anthropic_profile = GenericAnthropicCompatibleProfile(
        name="anthropic-host",
        display_name="Anthropic Host",
        provider_kind="host",
        api_mode="anthropic_messages",
        auth_scheme="anthropic_x_api_key",
    )
    anthropic_context = _context(
        profile=anthropic_profile,
        api_key="anthropic-key",
        base_url="https://anthropic.example.test",
    )

    openai_transport = create_transport(openai_context)
    anthropic_transport = create_transport(anthropic_context)

    assert isinstance(openai_transport, OpenAIChatCompletionsTransport)
    assert isinstance(anthropic_transport, AnthropicMessagesTransport)
    assert isinstance(openai_transport, ModelTransport)
    assert isinstance(anthropic_transport, ModelTransport)
    assert captured["openai"]["api_key"]
    assert captured["openai"]["api_key"] != openai_context.api_key
    assert captured["openai"]["base_url"] == openai_context.base_url
    assert captured["openai"]["max_retries"] == 0
    assert captured["openai"]["timeout"] == 60.0
    assert captured["anthropic"]["api_key"] == "anthropic-key"
    assert captured["anthropic"]["base_url"] == anthropic_context.base_url
    assert captured["anthropic"]["max_retries"] == 0
    assert captured["anthropic"]["timeout"] == 60.0


def test_minimax_extensions_serialize_at_json_root_through_openai_sdk() -> None:
    import httpx
    import openai

    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 0,
                "model": "MiniMax-M3",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": "OK"},
                    "finish_reason": "stop",
                }],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            },
        )

    profile = MiniMaxProfile(name="minimax", display_name="MiniMax")
    context = ProviderRuntimeContext(
        profile=profile,
        model="MiniMax-M3",
        api_key="test-key",
        base_url="https://minimax.example.test/v1",
        reasoning_config={"enabled": False},
    )
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = openai.OpenAI(
        api_key="test-key",
        base_url=context.base_url,
        http_client=http_client,
        max_retries=0,
    )
    try:
        result = OpenAIChatCompletionsTransport(context, client).call(
            messages=[{"role": "user", "content": "hello"}],
            tools=[],
            options=ModelCallOptions(max_output_tokens=8),
        )
    finally:
        client.close()

    assert result.content == "OK"
    assert bodies[0]["thinking"] == {"type": "disabled"}
    assert bodies[0]["reasoning_split"] is True
    assert "extra_body" not in bodies[0]


@pytest.mark.parametrize(
    ("profile", "api_key", "message"),
    [
        (
            GenericOpenAICompatibleProfile(name="required", display_name="Required"),
            "",
            "requires an API credential",
        ),
        (
            GenericOpenAICompatibleProfile(
                name="bad-chat-auth",
                display_name="Bad Chat Auth",
                auth_scheme="anthropic_x_api_key",
            ),
            "key",
            "incompatible auth scheme",
        ),
        (
            GenericAnthropicCompatibleProfile(
                name="bad-anthropic-auth",
                display_name="Bad Anthropic Auth",
                api_mode="anthropic_messages",
                auth_scheme="bearer",
            ),
            "key",
            "incompatible auth scheme",
        ),
        (
            RuntimeProviderProfile(
                name="unsupported",
                display_name="Unsupported",
                api_mode="responses",
            ),
            "key",
            "unsupported api_mode",
        ),
    ],
)
def test_factory_rejects_invalid_runtime_protocol_metadata(
    profile: RuntimeProviderProfile,
    api_key: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        create_transport(_context(profile=profile, api_key=api_key))


def test_openai_nonstream_conversion_and_result_normalization() -> None:
    profile = GenericOpenAICompatibleProfile(name="generic", display_name="Generic")
    context = _context(profile=profile, model="hosted/model")
    response = {
        "choices": [{
            "message": {
                "content": "answer",
                "reasoning_content": "thought",
                "reasoning_details": [{"type": "reasoning", "text": "thought"}],
                "tool_calls": [{
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": '{"q":"x"}'},
                    "extra_content": {"signature": "sig"},
                }],
            },
            "finish_reason": "tool_calls",
        }],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 4,
            "total_tokens": 14,
            "prompt_tokens_details": {"cached_tokens": 3},
            "completion_tokens_details": {"reasoning_tokens": 2},
        },
    }
    client = _FakeOpenAIClient(response)
    transport = OpenAIChatCompletionsTransport(context, client)
    messages = [{"role": "user", "content": "hello", "_private": True}]

    result = transport.call(
        messages=messages,
        tools=[{"type": "function", "function": {"name": "lookup", "parameters": {}}}],
        options=ModelCallOptions(source="turn", dynamic_system_context="dynamic"),
    )

    assert len(client.completions.calls) == 1
    request = client.completions.calls[0]
    assert request["model"] == "hosted/model"
    assert request["messages"][0] == {"role": "system", "content": "dynamic"}
    assert request["messages"][1] == {"role": "user", "content": "hello"}
    assert messages == [{"role": "user", "content": "hello", "_private": True}]
    assert result.content == "answer"
    assert result.tool_calls == [{
        "id": "call-1",
        "type": "function",
        "function": {"name": "lookup", "arguments": '{"q":"x"}'},
    }]
    assert result.finish_reason == "tool_calls"
    assert result.reasoning is not None
    assert result.reasoning.text == "thought"
    assert result.reasoning.format == "reasoning_details"
    assert result.usage is not None
    assert result.usage.to_counter_delta() == {
        "input_tokens": 10,
        "output_tokens": 4,
        "cache_read_tokens": 3,
        "reasoning_tokens": 2,
    }
    assert result.was_streamed is False


def test_openai_nonstream_watchdog_respects_longer_call_timeout(monkeypatch) -> None:
    from mclaw.agent.transports import openai_chat_completions as module

    monkeypatch.setattr(module, "NONSTREAM_TIMEOUT", 0.01)
    monkeypatch.setattr(module, "POLL_INTERVAL", 0.001)
    profile = GenericOpenAICompatibleProfile(name="generic", display_name="Generic")
    context = _context(profile=profile)

    def delayed_response() -> dict[str, object]:
        time.sleep(0.03)
        return {
            "choices": [{
                "message": {"content": "answer"},
                "finish_reason": "stop",
            }],
        }

    result = OpenAIChatCompletionsTransport(
        context,
        _FakeOpenAIClient(delayed_response),
    ).call(
        messages=[{"role": "user", "content": "hello"}],
        tools=[],
        options=ModelCallOptions(timeout=0.05),
    )

    assert result.content == "answer"


def test_openrouter_plaintext_reasoning_reaches_wire_request() -> None:
    profile = OpenRouterProfile(name="openrouter", display_name="OpenRouter")
    model = "openai/gpt-oss-120b"
    context = _context(profile=profile, model=model)
    trace = ReasoningTrace(
        text="prior thought",
        provider="openrouter",
        model=model,
        api_mode="chat_completions",
        format="reasoning_content",
        payload="prior thought",
    )
    client = _FakeOpenAIClient({
        "choices": [{
            "message": {"content": "answer"},
            "finish_reason": "stop",
        }],
    })

    OpenAIChatCompletionsTransport(context, client).call(
        messages=[{
            "role": "assistant",
            "content": "working",
            **trace.to_message_fields(),
        }],
        tools=[],
        options=ModelCallOptions(),
    )

    wire_message = client.completions.calls[0]["messages"][0]
    assert wire_message["reasoning"] == "prior thought"
    assert "reasoning_details" not in wire_message


def test_openai_surfaces_refusal_when_visible_content_is_empty() -> None:
    profile = GenericOpenAICompatibleProfile(name="generic", display_name="Generic")
    context = _context(profile=profile)
    response = {
        "choices": [{
            "message": {"content": None, "refusal": "I cannot help with that."},
            "finish_reason": "stop",
        }]
    }

    result = OpenAIChatCompletionsTransport(context, _FakeOpenAIClient(response)).call(
        messages=[{"role": "user", "content": "hello"}],
        tools=[],
        options=ModelCallOptions(),
    )

    assert result.content == "I cannot help with that."


def test_openai_delta_stream_assembles_content_reasoning_tools_and_usage() -> None:
    profile = GenericOpenAICompatibleProfile(name="generic", display_name="Generic")
    context = _context(profile=profile)
    stream = _FakeStream([
        {
            "choices": [{
                "delta": {
                    "content": "Hel",
                    "reasoning_content": "thi",
                    "tool_calls": [{
                        "index": 0,
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "look", "arguments": '{"q":"'},
                    }],
                },
                "finish_reason": None,
            }],
        },
        {
            "choices": [{
                "delta": {
                    "content": "lo",
                    "reasoning_content": "nk",
                    "tool_calls": [{
                        "index": 0,
                        "function": {"arguments": 'x"}'},
                        "extra_content": {"signature": "sig"},
                    }],
                },
                "finish_reason": "tool_calls",
            }],
        },
        {"choices": [], "usage": {"prompt_tokens": 8, "completion_tokens": 5}},
    ])
    client = _FakeOpenAIClient(stream)
    deltas: list[str] = []

    result = OpenAIChatCompletionsTransport(context, client).call(
        messages=[{"role": "user", "content": "hello"}],
        tools=[],
        options=ModelCallOptions(stream=True),
        stream_callback=deltas.append,
    )

    assert stream.closed is True
    assert "stream_options" not in client.completions.calls[0]
    assert deltas == ["Hel", "lo"]
    assert result.content == "Hello"
    assert result.reasoning is not None
    assert result.reasoning.text == "think"
    assert result.reasoning.format == "reasoning_details"
    assert result.tool_calls == [{
        "id": "call-1",
        "type": "function",
        "function": {"name": "look", "arguments": '{"q":"x"}'},
    }]
    assert result.usage is not None
    assert result.usage.input_tokens == 8
    assert result.usage.output_tokens == 5
    assert result.was_streamed is True


def test_openai_cumulative_stream_emits_only_new_visible_text() -> None:
    profile = MiniMaxProfile(name="minimax", display_name="MiniMax")
    context = _context(profile=profile, model="MiniMax-M2.7")
    stream = _FakeStream([
        {"choices": [{"delta": {"content": "H", "reasoning_details": [{"text": "T"}]}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": "Hello", "reasoning_details": [{"text": "Think"}]}, "finish_reason": "stop"}]},
    ])
    deltas: list[str] = []

    client = _FakeOpenAIClient(stream)
    result = OpenAIChatCompletionsTransport(context, client).call(
        messages=[{"role": "user", "content": "hello"}],
        tools=[],
        options=ModelCallOptions(stream=True),
        stream_callback=deltas.append,
    )

    assert deltas == ["H", "ello"]
    assert client.completions.calls[0]["stream_options"] == {"include_usage": True}
    assert result.content == "Hello"
    assert result.reasoning is not None
    assert result.reasoning.text == "Think"
    assert result.reasoning.payload == [{
        "field": "reasoning_details",
        "value": [{"text": "Think"}],
    }]


def test_openai_waits_for_delayed_final_usage_chunk(monkeypatch) -> None:
    from mclaw.agent.transports import openai_chat_completions as module

    monkeypatch.setattr(module, "STREAM_STALL_TIMEOUT", 0.2)
    monkeypatch.setattr(module, "POLL_INTERVAL", 0.001)
    profile = OpenAIProfile(name="openai", display_name="OpenAI")
    context = _context(profile=profile, model="gpt-5.6")
    stream = _DelayedUsageStream(0.03)
    client = _FakeOpenAIClient(stream)

    result = OpenAIChatCompletionsTransport(context, client).call(
        messages=[{"role": "user", "content": "hello"}],
        tools=[],
        options=ModelCallOptions(stream=True),
    )

    assert client.completions.calls[0]["stream_options"] == {"include_usage": True}
    assert stream.closed is True
    assert result.content == "done"
    assert result.usage is not None
    assert result.usage.input_tokens == 6
    assert result.usage.output_tokens == 2


def test_generic_openai_keeps_unsolicited_delayed_usage(monkeypatch) -> None:
    from mclaw.agent.transports import openai_chat_completions as module

    monkeypatch.setattr(module, "STREAM_STALL_TIMEOUT", 0.2)
    monkeypatch.setattr(module, "POLL_INTERVAL", 0.001)
    profile = GenericOpenAICompatibleProfile(name="generic", display_name="Generic")
    context = _context(profile=profile)
    stream = _DelayedUsageStream(0.03)

    result = OpenAIChatCompletionsTransport(context, _FakeOpenAIClient(stream)).call(
        messages=[{"role": "user", "content": "hello"}],
        tools=[],
        options=ModelCallOptions(stream=True),
    )

    assert result.usage is not None
    assert result.usage.input_tokens == 6


def test_required_stream_is_consumed_internally_without_callback() -> None:
    class RequiredStreamProfile(GenericOpenAICompatibleProfile):
        def model_traits(self, _model: str) -> ModelTraits:
            return ModelTraits(requires_stream=True)

    profile = RequiredStreamProfile(name="required", display_name="Required")
    context = _context(profile=profile)
    client = _FakeOpenAIClient(_FakeStream([
        {"choices": [{"delta": {"content": "internal"}, "finish_reason": "stop"}]},
    ]))

    result = OpenAIChatCompletionsTransport(context, client).call(
        messages=[{"role": "user", "content": "hello"}],
        tools=[],
        options=ModelCallOptions(),
    )

    assert client.completions.calls[0]["stream"] is True
    assert result.content == "internal"
    assert result.was_streamed is True


def test_openai_interrupted_stream_keeps_partial_usage() -> None:
    profile = GenericOpenAICompatibleProfile(name="generic", display_name="Generic")
    context = _context(profile=profile)
    state = {"interrupted": False}
    stream = _FakeStream([
        {
            "choices": [{"delta": {"content": "partial"}, "finish_reason": None}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 1},
        },
        {"choices": [{"delta": {"content": "ignored"}, "finish_reason": "stop"}]},
    ])

    def callback(text: str) -> None:
        assert text == "partial"
        state["interrupted"] = True

    result = OpenAIChatCompletionsTransport(context, _FakeOpenAIClient(stream)).call(
        messages=[{"role": "user", "content": "hello"}],
        tools=[],
        options=ModelCallOptions(stream=True),
        stream_callback=callback,
        interrupted=lambda: state["interrupted"],
    )

    assert result.interrupted is True
    assert result.finish_reason == "interrupted"
    assert result.content == "partial"
    assert result.usage is not None
    assert result.usage.input_tokens == 7


def test_openai_interrupt_drains_already_queued_final_usage() -> None:
    profile = GenericOpenAICompatibleProfile(name="generic", display_name="Generic")
    context = _context(profile=profile)
    state = {"interrupted": False}
    stream = _FakeStream([
        {"choices": [{"delta": {"content": "partial"}, "finish_reason": None}]},
        {"choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 2}},
    ])

    def callback(_text: str) -> None:
        state["interrupted"] = True

    result = OpenAIChatCompletionsTransport(context, _FakeOpenAIClient(stream)).call(
        messages=[{"role": "user", "content": "hello"}],
        tools=[],
        options=ModelCallOptions(stream=True),
        stream_callback=callback,
        interrupted=lambda: state["interrupted"],
    )

    assert result.interrupted is True
    assert result.usage is not None
    assert result.usage.input_tokens == 9
    assert result.usage.output_tokens == 2


def test_openai_partial_stream_stall_returns_partial_result(monkeypatch) -> None:
    from mclaw.agent.transports import openai_chat_completions as module

    monkeypatch.setattr(module, "STREAM_STALL_TIMEOUT", 0.01)
    monkeypatch.setattr(module, "POLL_INTERVAL", 0.001)
    profile = GenericOpenAICompatibleProfile(name="generic", display_name="Generic")
    context = _context(profile=profile)
    blocking = _BlockingStream({
        "choices": [{"delta": {"content": "partial"}, "finish_reason": None}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1},
    })

    result = OpenAIChatCompletionsTransport(context, _FakeOpenAIClient(blocking)).call(
        messages=[{"role": "user", "content": "hello"}],
        tools=[],
        options=ModelCallOptions(stream=True, timeout=0.005),
    )

    assert result.content == "partial"
    assert result.finish_reason == "stream_stalled"
    assert result.usage is not None
    assert blocking.closed.is_set()


def test_openai_partial_tool_call_is_not_dispatchable(monkeypatch) -> None:
    from mclaw.agent.transports import openai_chat_completions as module

    monkeypatch.setattr(module, "STREAM_STALL_TIMEOUT", 0.01)
    monkeypatch.setattr(module, "POLL_INTERVAL", 0.001)
    profile = GenericOpenAICompatibleProfile(name="generic", display_name="Generic")
    context = _context(profile=profile)
    blocking = _BlockingStream({
        "choices": [{
            "delta": {
                "content": "partial answer",
                "tool_calls": [{
                    "index": 0,
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": '{"path":"'},
                    "extra_content": {"signature": "dangling"},
                }],
            },
            "finish_reason": None,
        }],
    })

    result = OpenAIChatCompletionsTransport(context, _FakeOpenAIClient(blocking)).call(
        messages=[{"role": "user", "content": "hello"}],
        tools=[],
        options=ModelCallOptions(stream=True, timeout=0.005),
    )

    assert result.content == "partial answer"
    assert result.finish_reason == "stream_stalled"
    assert result.tool_calls is None
    assert result.reasoning is None
    assert blocking.closed.is_set()


def test_openai_interrupted_during_create_closes_late_stream() -> None:
    profile = GenericOpenAICompatibleProfile(name="generic", display_name="Generic")
    context = _context(profile=profile)
    stream = _FakeStream([])

    def delayed_stream() -> _FakeStream:
        time.sleep(0.03)
        return stream

    result = OpenAIChatCompletionsTransport(
        context,
        _FakeOpenAIClient(delayed_stream),
    ).call(
        messages=[{"role": "user", "content": "hello"}],
        tools=[],
        options=ModelCallOptions(stream=True),
        interrupted=lambda: True,
    )
    time.sleep(0.06)

    assert result.interrupted is True
    assert stream.closed is True


def test_openai_transport_performs_one_sdk_attempt_on_error() -> None:
    profile = GenericOpenAICompatibleProfile(name="generic", display_name="Generic")
    context = _context(profile=profile)
    client = _FakeOpenAIClient(Exception("api_key=sk-super-secret failed"))

    try:
        OpenAIChatCompletionsTransport(context, client).call(
            messages=[{"role": "user", "content": "hello"}],
            tools=[],
            options=ModelCallOptions(),
        )
    except ModelCallError as error:
        safe_message = str(error)
        formatted = traceback.format_exc()
    else:
        pytest.fail("expected ModelCallError")

    assert len(client.completions.calls) == 1
    assert "sk-super-secret" not in safe_message
    assert "sk-super-secret" not in formatted


def test_openrouter_midstream_error_is_retryable_failure() -> None:
    profile = OpenRouterProfile(name="openrouter", display_name="OpenRouter")
    context = _context(profile=profile, model="openai/gpt-oss-120b")
    stream = _FakeStream([
        {
            "choices": [{
                "delta": {"content": "partial"},
                "finish_reason": None,
            }],
        },
        {
            "error": {
                "code": 429,
                "message": "Rate limit exceeded",
                "metadata": {"error_type": "rate_limit_exceeded"},
            },
            "choices": [{"delta": {"content": ""}, "finish_reason": "error"}],
        },
    ])

    with pytest.raises(ModelCallError, match="Rate limit exceeded") as raised:
        OpenAIChatCompletionsTransport(context, _FakeOpenAIClient(stream)).call(
            messages=[{"role": "user", "content": "hello"}],
            tools=[],
            options=ModelCallOptions(stream=True),
        )

    assert raised.value.status_code == 429
    assert raised.value.retryable is True
    assert raised.value.rate_limited is True
    assert stream.closed is True


def test_openrouter_sdk_stream_error_body_is_not_hidden_by_partial_content() -> None:
    import httpx
    import openai

    def handler(_request: httpx.Request) -> httpx.Response:
        partial = json.dumps({
            "id": "chatcmpl-partial",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "openai/gpt-oss-120b",
            "choices": [{
                "index": 0,
                "delta": {"content": "partial"},
                "finish_reason": None,
            }],
        })
        error = json.dumps({
            "error": {
                "code": 429,
                "message": "Rate limit exceeded",
                "metadata": {"error_type": "rate_limit_exceeded"},
            },
            "choices": [{"index": 0, "delta": {}, "finish_reason": "error"}],
        })
        return httpx.Response(
            200,
            content=f"data: {partial}\n\ndata: {error}\n\n",
            headers={"content-type": "text/event-stream"},
        )

    profile = OpenRouterProfile(name="openrouter", display_name="OpenRouter")
    context = _context(
        profile=profile,
        model="openai/gpt-oss-120b",
        api_key="test-key",
        base_url="https://openrouter.example.test/api/v1",
    )
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = openai.OpenAI(
        api_key="test-key",
        base_url=context.base_url,
        http_client=http_client,
        max_retries=0,
    )
    try:
        with pytest.raises(ModelCallError, match="Rate limit exceeded") as raised:
            OpenAIChatCompletionsTransport(context, client).call(
                messages=[{"role": "user", "content": "hello"}],
                tools=[],
                options=ModelCallOptions(stream=True),
            )
    finally:
        client.close()

    assert raised.value.status_code == 429
    assert raised.value.retryable is True
    assert raised.value.rate_limited is True


def test_openai_stream_watchdogs_respect_longer_call_timeout(monkeypatch) -> None:
    from mclaw.agent.transports import openai_chat_completions as module

    monkeypatch.setattr(module, "CREATE_TIMEOUT", 0.01)
    monkeypatch.setattr(module, "STREAM_SAFETY_TIMEOUT", 0.01)
    monkeypatch.setattr(module, "STREAM_STALL_TIMEOUT", 0.01)
    monkeypatch.setattr(module, "POLL_INTERVAL", 0.001)
    profile = GenericOpenAICompatibleProfile(name="generic", display_name="Generic")
    context = _context(profile=profile)
    stream = _DelayedFirstStream(0.02, [{
        "choices": [{"delta": {"content": "answer"}, "finish_reason": "stop"}],
    }])

    def delayed_create() -> _DelayedFirstStream:
        time.sleep(0.02)
        return stream

    result = OpenAIChatCompletionsTransport(
        context,
        _FakeOpenAIClient(delayed_create),
    ).call(
        messages=[{"role": "user", "content": "hello"}],
        tools=[],
        options=ModelCallOptions(stream=True, timeout=0.05),
    )

    assert result.content == "answer"
    assert stream.closed is True


def test_openai_zero_payload_stream_is_normalized() -> None:
    profile = GenericOpenAICompatibleProfile(name="generic", display_name="Generic")
    context = _context(profile=profile)

    with pytest.raises(ModelCallError, match="ended before returning a payload") as raised:
        OpenAIChatCompletionsTransport(context, _FakeOpenAIClient(_FakeStream([]))).call(
            messages=[{"role": "user", "content": "hello"}],
            tools=[],
            options=ModelCallOptions(stream=True),
        )

    assert raised.value.retryable is False


def test_anthropic_nonstream_converts_grouped_tools_reasoning_and_usage() -> None:
    profile = AnthropicProfile(
        name="anthropic",
        display_name="Anthropic",
        api_mode="anthropic_messages",
        auth_scheme="anthropic_x_api_key",
    )
    context = _context(profile=profile, model="claude-3-5-sonnet")
    trace = ReasoningTrace(
        text="prior thought",
        provider="anthropic",
        model="claude-3-5-sonnet",
        api_mode="anthropic_messages",
        format="anthropic_thinking_blocks",
        payload=[{"type": "thinking", "thinking": "prior thought", "signature": "signed"}],
    )
    messages = [
        {"role": "system", "content": "stable"},
        {
            "role": "assistant",
            "content": "working",
            "tool_calls": [
                {"id": "call-1", "type": "function", "function": {"name": "one", "arguments": '{"x":1}'}},
                {"id": "call-2", "type": "function", "function": {"name": "two", "arguments": '{"y":2}'}},
            ],
            **trace.to_message_fields(),
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "first"},
        {"role": "tool", "tool_call_id": "call-2", "content": "second"},
        {"role": "user", "content": "continue"},
    ]
    response = {
        "content": [
            {"type": "thinking", "thinking": "new thought", "signature": "new-sig"},
            {"type": "text", "text": "answer"},
            {"type": "tool_use", "id": "call-3", "name": "three", "input": {"z": 3}},
        ],
        "stop_reason": "tool_use",
        "usage": {
            "input_tokens": 10,
            "cache_creation_input_tokens": 2,
            "cache_read_input_tokens": 3,
            "output_tokens": 4,
        },
    }
    api = _FakeMessageAPI(response=response)

    result = AnthropicMessagesTransport(context, _FakeAnthropicClient(api)).call(
        messages=messages,
        tools=[{
            "type": "function",
            "function": {
                "name": "three",
                "description": "third",
                "parameters": {"type": "object", "properties": {"z": {"type": "integer"}}},
            },
        }],
        options=ModelCallOptions(
            max_output_tokens=999_999,
            temperature=0.2,
            timeout=12,
            dynamic_system_context="dynamic",
        ),
    )

    assert len(api.create_calls) == 1
    request = api.create_calls[0]
    assert request["model"] == "claude-3-5-sonnet"
    assert request["max_tokens"] == 8_192
    assert request["temperature"] == 0.2
    assert request["timeout"] == 12
    assert request["system"] == [
        {"type": "text", "text": "stable"},
        {"type": "text", "text": "dynamic"},
    ]
    assert request["system"].count({"type": "text", "text": "dynamic"}) == 1
    assistant_blocks = request["messages"][0]["content"]
    assert assistant_blocks[0] == {
        "type": "thinking",
        "thinking": "prior thought",
        "signature": "signed",
    }
    assert [block["type"] for block in assistant_blocks[1:]] == ["text", "tool_use", "tool_use"]
    grouped_results = request["messages"][1]
    assert grouped_results["role"] == "user"
    assert [block["tool_use_id"] for block in grouped_results["content"]] == ["call-1", "call-2"]
    assert request["messages"][2] == {"role": "user", "content": "continue"}
    assert request["tools"][0]["input_schema"]["properties"]["z"]["type"] == "integer"
    assert result.content == "answer"
    assert result.tool_calls == [{
        "id": "call-3",
        "type": "function",
        "function": {"name": "three", "arguments": '{"z":3}'},
    }]
    assert result.reasoning is not None
    assert result.reasoning.text == "new thought"
    assert result.reasoning.payload == [
        {"type": "thinking", "thinking": "new thought", "signature": "new-sig"}
    ]
    assert result.usage is not None
    assert result.usage.input_tokens == 15
    assert result.usage.output_tokens == 4
    assert result.usage.cache_read_tokens == 3
    assert result.usage.cache_write_tokens == 2


def test_anthropic_nonstream_watchdog_respects_longer_call_timeout(monkeypatch) -> None:
    from mclaw.agent.transports import anthropic_messages as module

    class DelayedMessageAPI(_FakeMessageAPI):
        def create(self, **kwargs: Any) -> Any:
            time.sleep(0.03)
            return super().create(**kwargs)

    monkeypatch.setattr(module, "NONSTREAM_WATCHDOG_TIMEOUT", 0.01)
    monkeypatch.setattr(module, "POLL_INTERVAL", 0.001)
    profile = AnthropicProfile(
        name="anthropic",
        display_name="Anthropic",
        api_mode="anthropic_messages",
        auth_scheme="anthropic_x_api_key",
    )
    context = _context(profile=profile, model="claude-3-5-sonnet")
    api = DelayedMessageAPI(response={
        "content": [{"type": "text", "text": "answer"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    })

    result = AnthropicMessagesTransport(context, _FakeAnthropicClient(api)).call(
        messages=[{"role": "user", "content": "hello"}],
        tools=[],
        options=ModelCallOptions(timeout=0.05),
    )

    assert result.content == "answer"


def test_anthropic_stream_aggregates_thinking_tools_and_cumulative_usage() -> None:
    profile = AnthropicProfile(
        name="anthropic",
        display_name="Anthropic",
        api_mode="anthropic_messages",
        auth_scheme="anthropic_x_api_key",
    )
    context = _context(profile=profile, model="claude-sonnet-4-6")
    events = [
        {
            "type": "message_start",
            "message": {
                "usage": {
                    "input_tokens": 10,
                    "cache_creation_input_tokens": 2,
                    "cache_read_input_tokens": 3,
                }
            },
        },
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "thought"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig-"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "part"}},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Hello"}},
        {"type": "content_block_start", "index": 2, "content_block": {"type": "tool_use", "id": "call-1", "name": "lookup", "input": {}}},
        {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": '{"q":'}},
        {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": '"x"}'}},
        {"type": "content_block_stop", "index": 2},
        {"type": "message_delta", "delta": {"stop_reason": None}, "usage": {"output_tokens": 2}},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 4}},
        {"type": "message_stop"},
    ]
    api = _FakeMessageAPI(events=events)
    deltas: list[str] = []

    result = AnthropicMessagesTransport(context, _FakeAnthropicClient(api)).call(
        messages=[{"role": "user", "content": "hello"}],
        tools=[],
        options=ModelCallOptions(stream=True, timeout=17),
        stream_callback=deltas.append,
    )

    assert len(api.stream_calls) == 1
    assert api.stream_calls[0]["timeout"] == 17
    assert deltas == ["Hello"]
    assert result.content == "Hello"
    assert result.finish_reason == "tool_use"
    assert result.reasoning is not None
    assert result.reasoning.text == "thought"
    assert result.reasoning.payload == [
        {"type": "thinking", "thinking": "thought", "signature": "sig-part"}
    ]
    assert result.tool_calls == [{
        "id": "call-1",
        "type": "function",
        "function": {"name": "lookup", "arguments": '{"q":"x"}'},
    }]
    assert result.usage is not None
    assert result.usage.input_tokens == 15
    assert result.usage.output_tokens == 4


def test_anthropic_current_large_output_requires_internal_stream() -> None:
    profile = AnthropicProfile(
        name="anthropic",
        display_name="Anthropic",
        api_mode="anthropic_messages",
        auth_scheme="anthropic_x_api_key",
    )
    context = _context(profile=profile, model="claude-sonnet-5")
    api = _FakeMessageAPI(events=[
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "answer"}},
        {"type": "message_stop", "message": {"stop_reason": "end_turn", "usage": {"output_tokens": 1}}},
    ])

    result = AnthropicMessagesTransport(context, _FakeAnthropicClient(api)).call(
        messages=[{"role": "user", "content": "hello"}],
        tools=[],
        options=ModelCallOptions(),
    )

    assert len(api.stream_calls) == 1
    assert not api.create_calls
    assert result.content == "answer"
    assert result.was_streamed is True


def test_anthropic_stream_watchdogs_respect_longer_call_timeout(monkeypatch) -> None:
    from mclaw.agent.transports import anthropic_messages as module

    class DelayedMessageAPI(_FakeMessageAPI):
        def stream(self, **kwargs: Any):
            time.sleep(0.02)
            return super().stream(**kwargs)

    monkeypatch.setattr(module, "STREAM_CREATE_TIMEOUT", 0.01)
    monkeypatch.setattr(module, "STREAM_SAFETY_TIMEOUT", 0.01)
    monkeypatch.setattr(module, "STREAM_STALL_TIMEOUT", 0.01)
    monkeypatch.setattr(module, "POLL_INTERVAL", 0.001)
    profile = AnthropicProfile(
        name="anthropic",
        display_name="Anthropic",
        api_mode="anthropic_messages",
        auth_scheme="anthropic_x_api_key",
    )
    context = _context(profile=profile, model="claude-sonnet-5")
    api = DelayedMessageAPI(events=_DelayedFirstStream(0.02, [
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "answer"},
        },
        {"type": "message_stop", "message": {"stop_reason": "end_turn"}},
    ]))

    result = AnthropicMessagesTransport(context, _FakeAnthropicClient(api)).call(
        messages=[{"role": "user", "content": "hello"}],
        tools=[],
        options=ModelCallOptions(timeout=0.05),
    )

    assert result.content == "answer"
    assert api.closed is True


def test_anthropic_interrupted_stream_keeps_reported_input_usage() -> None:
    profile = AnthropicProfile(
        name="anthropic",
        display_name="Anthropic",
        api_mode="anthropic_messages",
        auth_scheme="anthropic_x_api_key",
    )
    context = _context(profile=profile, model="claude-sonnet-4-6")
    state = {"interrupted": False}
    api = _FakeMessageAPI(events=[
        {"type": "message_start", "message": {"usage": {"input_tokens": 5}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": "partial"}},
        {"type": "message_delta", "delta": {"stop_reason": None}, "usage": {"output_tokens": 2}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "ignored"}},
    ])

    def callback(text: str) -> None:
        assert text == "partial"
        state["interrupted"] = True

    result = AnthropicMessagesTransport(context, _FakeAnthropicClient(api)).call(
        messages=[{"role": "user", "content": "hello"}],
        tools=[],
        options=ModelCallOptions(stream=True),
        stream_callback=callback,
        interrupted=lambda: state["interrupted"],
    )

    assert result.interrupted is True
    assert result.finish_reason == "interrupted"
    assert result.content == "partial"
    assert result.usage is not None
    assert result.usage.input_tokens == 5
    assert result.usage.output_tokens == 2


def test_anthropic_interrupted_during_create_closes_late_stream() -> None:
    class DelayedMessageAPI(_FakeMessageAPI):
        def stream(self, **kwargs: Any):
            time.sleep(0.03)
            return super().stream(**kwargs)

    profile = AnthropicProfile(
        name="anthropic",
        display_name="Anthropic",
        api_mode="anthropic_messages",
        auth_scheme="anthropic_x_api_key",
    )
    context = _context(profile=profile, model="claude-sonnet-5")
    api = DelayedMessageAPI(events=[])

    result = AnthropicMessagesTransport(context, _FakeAnthropicClient(api)).call(
        messages=[{"role": "user", "content": "hello"}],
        tools=[],
        options=ModelCallOptions(stream=True),
        interrupted=lambda: True,
    )
    time.sleep(0.06)

    assert result.interrupted is True
    assert api.closed is True


def test_anthropic_transport_performs_one_sdk_attempt_on_error() -> None:
    profile = AnthropicProfile(
        name="anthropic",
        display_name="Anthropic",
        api_mode="anthropic_messages",
        auth_scheme="anthropic_x_api_key",
    )
    context = _context(profile=profile, model="claude-3-5-sonnet")
    api = _FakeMessageAPI(response=Exception("x-api-key: sk-super-secret"))

    try:
        AnthropicMessagesTransport(context, _FakeAnthropicClient(api)).call(
            messages=[{"role": "user", "content": "hello"}],
            tools=[],
            options=ModelCallOptions(),
        )
    except ModelCallError as error:
        safe_message = str(error)
        formatted = traceback.format_exc()
    else:
        pytest.fail("expected ModelCallError")

    assert len(api.create_calls) == 1
    assert "sk-super-secret" not in safe_message
    assert "sk-super-secret" not in formatted


def test_anthropic_partial_tool_block_is_not_dispatchable() -> None:
    profile = AnthropicProfile(
        name="anthropic",
        display_name="Anthropic",
        api_mode="anthropic_messages",
        auth_scheme="anthropic_x_api_key",
    )
    context = _context(profile=profile, model="claude-sonnet-5")
    api = _FakeMessageAPI(events=[
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "text_delta", "text": "partial answer"},
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "tool_use", "id": "call-1", "name": "lookup", "input": {}},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": '{"path":"'},
        },
        {"type": "content_block_stop", "index": 0},
        {"type": "message_stop"},
    ])

    result = AnthropicMessagesTransport(context, _FakeAnthropicClient(api)).call(
        messages=[{"role": "user", "content": "hello"}],
        tools=[],
        options=ModelCallOptions(stream=True),
    )

    assert result.tool_calls is None


def test_anthropic_callback_failure_closes_stream() -> None:
    profile = AnthropicProfile(
        name="anthropic",
        display_name="Anthropic",
        api_mode="anthropic_messages",
        auth_scheme="anthropic_x_api_key",
    )
    context = _context(profile=profile, model="claude-sonnet-5")
    api = _FakeMessageAPI(events=[
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "x"}},
    ])

    with pytest.raises(ModelCallError, match="callback failed"):
        AnthropicMessagesTransport(context, _FakeAnthropicClient(api)).call(
            messages=[{"role": "user", "content": "hello"}],
            tools=[],
            options=ModelCallOptions(stream=True),
            stream_callback=lambda _text: (_ for _ in ()).throw(RuntimeError("callback failed")),
        )

    assert api.closed is True


def test_reasoning_envelope_roundtrips_through_session_db(tmp_path) -> None:
    trace = ReasoningTrace(
        text="thought",
        provider="anthropic",
        model="claude-sonnet-4-6",
        api_mode="anthropic_messages",
        format="anthropic_thinking_blocks",
        payload=[{"type": "thinking", "thinking": "thought", "signature": "sig"}],
    )
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("session-1", "test")
        db.append_message("session-1", "assistant", "answer", **trace.to_message_fields())
        restored = db.get_messages_as_conversation("session-1")[-1]
    finally:
        db.close()

    assert ReasoningTrace.from_message(restored) == trace


def test_minimax_transport_reasoning_roundtrips_to_native_replay(tmp_path) -> None:
    profile = MiniMaxProfile(name="minimax", display_name="MiniMax")
    context = _context(profile=profile, model="MiniMax-M2.7")
    response = {
        "choices": [{
            "message": {
                "content": "answer",
                "reasoning_details": [{"type": "reasoning", "text": "thought"}],
                "tool_calls": [{
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": '{"q":"x"}'},
                }],
            },
            "finish_reason": "tool_calls",
        }],
    }
    result = OpenAIChatCompletionsTransport(context, _FakeOpenAIClient(response)).call(
        messages=[{"role": "user", "content": "hello"}],
        tools=[],
        options=ModelCallOptions(),
    )
    assert result.reasoning is not None
    db = SessionDB(tmp_path / "minimax.db")
    try:
        db.create_session("session-1", "test")
        db.append_message(
            "session-1",
            "assistant",
            result.content,
            tool_calls=result.tool_calls,
            **result.reasoning.to_message_fields(),
        )
        restored = db.get_messages_as_conversation("session-1")[-1]
    finally:
        db.close()

    prepared = profile.prepare_request({"messages": [restored]}, context, ModelCallOptions())

    assert prepared["messages"][0]["reasoning_details"] == [
        {"type": "reasoning", "text": "thought"}
    ]
    assert prepared["messages"][0]["tool_calls"] == result.tool_calls


def test_model_call_error_is_secret_safe_and_retry_ready() -> None:
    context = _context()

    class ProviderError(Exception):
        status_code = 429
        response = SimpleNamespace(headers={"Retry-After": "2.5"}, status_code=429)

    raw = ProviderError(
        "provider-window-code: Authorization: Bearer sk-super-secret; "
        "api_key='sk-other-secret'; password=hunter2"
    )
    error = normalize_model_call_error(raw, context)

    assert isinstance(error, ModelCallError)
    assert error.provider == "test-provider"
    assert error.model == "test-model"
    assert error.retryable is True
    assert error.rate_limited is True
    assert error.context_limit is True
    assert error.retry_after == 2.5
    assert error.status_code == 429
    assert error.raw_exception is raw
    assert "sk-super-secret" not in error.message
    assert "sk-other-secret" not in error.message
    assert "hunter2" not in error.message
    assert error.message.count("<redacted>") >= 3


def test_transport_timeout_is_retryable() -> None:
    error = normalize_model_call_error(TimeoutError("provider watchdog expired"), _context())

    assert error.retryable is True


@pytest.mark.parametrize("retry_after", ["-1", "nan", "inf", "not-a-number"])
def test_model_call_error_rejects_invalid_retry_after(retry_after: str) -> None:
    class ProviderError(Exception):
        status_code = 503
        response = SimpleNamespace(headers={"retry-after": retry_after}, status_code=503)

    error = normalize_model_call_error(ProviderError("temporarily unavailable"), _context())

    assert error.retryable is True
    assert error.retry_after is None


def test_unrelated_numeric_error_is_not_a_context_limit() -> None:
    error = normalize_model_call_error(Exception("archive record from 2013"), _context())

    assert error.context_limit is False
