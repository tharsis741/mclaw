# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Response deadlines must survive SDK timeouts, early headers, and heartbeats."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import httpx
import openai
import pytest

from mclaw.agent.transports.anthropic_messages import AnthropicMessagesTransport
from mclaw.agent.transports.base import ModelCallError, ModelCallOptions, normalize_model_call_error
from mclaw.agent.transports.openai_chat_completions import OpenAIChatCompletionsTransport
from mclaw.providers.generic import GenericAnthropicCompatibleProfile, GenericOpenAICompatibleProfile
from mclaw.providers.runtime import ProviderRuntimeContext


def _context(protocol: str = "openai", base_url: str = "https://example.test/v1") -> ProviderRuntimeContext:
    profile = (
        GenericOpenAICompatibleProfile(name="test", display_name="Test")
        if protocol == "openai" else GenericAnthropicCompatibleProfile(
            name="test", display_name="Test", api_mode="anthropic_messages", auth_scheme="anthropic_x_api_key",
        )
    )
    return ProviderRuntimeContext(profile=profile, model="test-model", api_key="test-placeholder", base_url=base_url)


def _event(protocol: str, kind: str, text: str = "OK") -> dict:
    if protocol == "openai":
        delta = {"content": text} if kind == "text" else {"reasoning_content": text} if kind == "thinking" else {}
        return {"choices": [{"delta": delta, "finish_reason": "stop" if kind == "stop" else None}]}
    if kind == "heartbeat":
        return {"type": "ping"}
    if kind == "stop":
        return {"type": "message_stop", "message": {"stop_reason": "end_turn"}}
    field = "text" if kind == "text" else "thinking"
    return {"type": "content_block_delta", "index": 0, "delta": {"type": f"{field}_delta", field: text}}


class _TimedStream:
    def __init__(self, schedule: list[tuple[float, dict]]) -> None:
        self.schedule = schedule
        self.closed = threading.Event()

    def __iter__(self):
        for delay, event in self.schedule:
            if self.closed.wait(delay):
                return
            yield event

    def close(self) -> None:
        self.closed.set()


def _transport(protocol: str, schedule: list[tuple[float, dict]], create_delay: float = 0):
    stream = _TimedStream(schedule)

    def create(**_kwargs):
        time.sleep(create_delay)
        return stream

    if protocol == "openai":
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        return OpenAIChatCompletionsTransport(_context(protocol), client), stream
    client = SimpleNamespace(messages=SimpleNamespace(stream=create))
    return AnthropicMessagesTransport(_context(protocol), client), stream


@pytest.fixture(autouse=True)
def _fast_watchdog(monkeypatch):
    monkeypatch.setattr("mclaw.agent.transports.openai_chat_completions.POLL_INTERVAL", 0.002)
    monkeypatch.setattr("mclaw.agent.transports.anthropic_messages.POLL_INTERVAL", 0.002)


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
def test_headers_do_not_switch_to_shorter_idle_timeout(protocol):
    transport, stream = _transport(protocol, [
        (0.12, _event(protocol, "text")), (0, _event(protocol, "stop")),
    ])
    result = transport.call(
        messages=[{"role": "user", "content": "hi"}], tools=[],
        options=ModelCallOptions(stream=True, timeout=0.02, first_response_timeout=0.5, stream_idle_timeout=0.04),
    )
    assert result.content == "OK"
    assert stream.closed.is_set()


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
def test_first_response_budget_includes_header_wait(protocol):
    transport, stream = _transport(protocol, [(0.15, _event(protocol, "text"))], create_delay=0.15)
    with pytest.raises(ModelCallError) as raised:
        transport.call(
            messages=[{"role": "user", "content": "hi"}], tools=[],
            options=ModelCallOptions(stream=True, first_response_timeout=0.24, stream_idle_timeout=1),
        )
    assert raised.value.code == "PROVIDER_TIMEOUT"
    assert stream.closed.is_set()


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
def test_heartbeats_do_not_extend_first_response_deadline(protocol):
    transport, stream = _transport(protocol, [(0.01, _event(protocol, "heartbeat"))] * 40)
    started = time.monotonic()
    with pytest.raises(ModelCallError) as raised:
        transport.call(
            messages=[{"role": "user", "content": "hi"}], tools=[],
            options=ModelCallOptions(stream=True, first_response_timeout=0.08, stream_idle_timeout=1),
        )
    assert raised.value.code == "PROVIDER_TIMEOUT"
    assert time.monotonic() - started < 0.3
    assert stream.closed.is_set()


def test_empty_anthropic_thinking_block_does_not_hide_first_response_timeout():
    transport, stream = _transport("anthropic", [
        (0, {"type": "message_start", "message": {"usage": {"input_tokens": 10}}}),
        (0, {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}}),
        (0.2, _event("anthropic", "text")),
    ])
    with pytest.raises(ModelCallError) as raised:
        transport.call(
            messages=[{"role": "user", "content": "hi"}], tools=[],
            options=ModelCallOptions(stream=True, first_response_timeout=0.04, stream_idle_timeout=1),
        )
    assert raised.value.code == "PROVIDER_TIMEOUT"
    assert stream.closed.is_set()


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
def test_reasoning_starts_idle_budget_and_later_stalls_preserve_partial_output(protocol):
    transport, stream = _transport(protocol, [
        (0, _event(protocol, "thinking", "working")),
        (0.08, _event(protocol, "text", "first")),
        (0.3, _event(protocol, "text", "late")),
    ])
    result = transport.call(
        messages=[{"role": "user", "content": "hi"}], tools=[],
        options=ModelCallOptions(stream=True, first_response_timeout=0.04, stream_idle_timeout=0.15),
    )
    assert result.content == "first"
    assert result.finish_reason == "stream_stalled"
    assert result.reasoning.text == "working"
    assert stream.closed.is_set()


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
def test_cancellation_preempts_long_first_response_wait_and_closes_late_stream(protocol):
    transport, stream = _transport(protocol, [], create_delay=0.2)
    started = time.monotonic()
    result = transport.call(
        messages=[{"role": "user", "content": "hi"}], tools=[],
        options=ModelCallOptions(stream=True, first_response_timeout=180, stream_idle_timeout=60),
        interrupted=lambda: time.monotonic() - started > 0.03,
    )
    assert result.interrupted
    assert time.monotonic() - started < 0.15
    assert stream.closed.wait(1)


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
def test_absolute_task_deadline_preempts_configured_response_wait(protocol):
    transport, stream = _transport(protocol, [(0.3, _event(protocol, "text"))])
    with pytest.raises(ModelCallError) as raised:
        transport.call(
            messages=[{"role": "user", "content": "hi"}], tools=[],
            options=ModelCallOptions(
                stream=True, first_response_timeout=180, stream_idle_timeout=60,
                deadline_monotonic=time.monotonic() + 0.04,
            ),
        )
    assert raised.value.code == "DEADLINE_EXCEEDED"
    assert stream.closed.is_set()


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("first,idle,read", [(90, 60, 90), (180, 45, 180), (10, 60, 60)])
def test_sdk_receives_phase_aware_timeouts_without_changing_request_body(streaming, first, idle, read):
    captured = []

    def handle(request):
        captured.append(request)
        if streaming:
            body = 'data: {"choices":[{"delta":{"content":"OK"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}]})

    context = _context()
    client = openai.OpenAI(
        api_key=context.api_key, base_url=context.base_url, max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handle)),
    )
    transport = OpenAIChatCompletionsTransport(context, client)
    try:
        result = transport.call(
            messages=[{"role": "user", "content": "hi"}], tools=[],
            options=ModelCallOptions(stream=streaming, timeout=30, first_response_timeout=first, stream_idle_timeout=idle),
        )
    finally:
        transport.close()
    assert result.content == "OK"
    assert captured[0].extensions["timeout"] == {
        "connect": 30, "write": 30, "pool": 30, "read": read if streaming else first,
    }
    assert not {"timeout", "first_response_timeout", "stream_idle_timeout"} & json.loads(captured[0].content).keys()


@pytest.mark.parametrize("phase", ["headers", "body"])
def test_real_sdk_accepts_delayed_local_server_response_beyond_connection_timeout(phase):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            if phase == "headers":
                time.sleep(0.2)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.flush()
            if phase == "body":
                time.sleep(0.2)
            try:
                self.wfile.write(b'data: {"choices":[{"delta":{"content":"OK"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
                self.wfile.flush()
            except OSError:
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    context = _context(base_url=f"http://127.0.0.1:{server.server_port}/v1")
    client = openai.OpenAI(
        api_key=context.api_key, base_url=context.base_url, max_retries=0,
        http_client=httpx.Client(trust_env=False),
    )
    transport = OpenAIChatCompletionsTransport(context, client)
    try:
        result = transport.call(
            messages=[{"role": "user", "content": "hi"}], tools=[],
            options=ModelCallOptions(stream=True, timeout=0.05, first_response_timeout=0.8, stream_idle_timeout=0.05),
        )
        assert result.content == "OK"
    finally:
        transport.close()
        server.shutdown()
        server.server_close()
        thread.join(1)


@pytest.mark.parametrize("error", [TimeoutError("slow"), httpx.ReadTimeout("slow"), openai.APITimeoutError(request=httpx.Request("POST", "https://example.test"))])
def test_timeout_errors_are_identified_for_retry_status(error):
    result = normalize_model_call_error(error, _context())
    assert result.code == "PROVIDER_TIMEOUT"
    assert result.retryable


def test_agent_passes_saved_timeouts_and_explains_timeout_retries(monkeypatch, tmp_path):
    from mclaw.agent.core import MClaw
    from mclaw.agent.transports.base import ModelCallResult

    monkeypatch.setenv("MCLAW_HOME", str(tmp_path))
    calls = []
    statuses = []

    class Transport:
        def call(self, **kwargs):
            calls.append(kwargs["options"])
            if len(calls) == 1:
                raise ModelCallError("slow", "test", "test-model", retryable=True, retry_after=0, code="PROVIDER_TIMEOUT")
            return ModelCallResult("OK", None, "stop", None, None, True, "test", "test-model")

        def close(self):
            return True

    monkeypatch.setattr("mclaw.agent.core.create_transport", lambda _context: Transport())
    monkeypatch.setattr(MClaw, "_discover_tools", lambda self: None)
    agent = MClaw(
        provider_runtime=_context(), system_prompt="test", skip_memory=True, persist_session=False,
        workspace=str(tmp_path), status_callback=statuses.append, stream_callback=lambda _text: None,
        config={"compression": {"enabled": False}, "checkpoints": {"enabled": False},
                "agent": {"first_response_timeout": 180, "stream_idle_timeout": 45}},
    )
    try:
        result = agent.run_conversation("hi", advance_background_review=False)
    finally:
        agent.close()
    assert result["final_response"] == "OK"
    assert len(calls) == 2
    assert all(call.first_response_timeout == 180 and call.stream_idle_timeout == 45 for call in calls)
    assert "Model response timed out; retrying in 0s..." in statuses
