# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import threading

from mclaw.agent.core import MClaw
from mclaw.agent.prompt_cache import PromptCachePlan
from mclaw.agent.transports.base import ModelCallResult
from mclaw.agent.usage import UsageRecord
from mclaw.providers.base import RuntimeProviderProfile
from mclaw.providers.runtime import ProviderRuntimeContext


def _context() -> ProviderRuntimeContext:
    return ProviderRuntimeContext(
        profile=RuntimeProviderProfile(name="test", display_name="Test"),
        model="model-a",
        api_key="secret",
        base_url="https://example.test/v1",
    )


class _MemoryManager:
    def __init__(self) -> None:
        self.schemas = [{
            "type": "function",
            "function": {
                "name": "memory_add",
                "description": "save memory",
                "parameters": {"type": "object", "properties": {}},
            },
        }]
        self.handled: list[tuple[str, dict]] = []

    def get_all_tool_schemas(self):
        return self.schemas

    def handle_tool_call(self, name: str, args: dict) -> str:
        self.handled.append((name, args))
        return json.dumps({"success": True})


class _FlushTransport:
    def __init__(self, result: ModelCallResult) -> None:
        self.result = result
        self.calls: list[dict] = []

    def call(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


def _agent(monkeypatch, transport) -> MClaw:
    monkeypatch.setattr("mclaw.agent.core.create_transport", lambda _context: transport)
    monkeypatch.setattr(MClaw, "_discover_tools", lambda self: None)
    return MClaw(
        provider_runtime=_context(),
        system_prompt="system",
        skip_memory=True,
        config={"compression": {"enabled": False}},
    )


def test_memory_flush_uses_explicit_tools_and_direct_dispatch(monkeypatch) -> None:
    context = _context()
    usage = UsageRecord(
        provider=context.provider,
        model=context.model,
        input_tokens=8,
        output_tokens=3,
        source="memory_flush",
    )
    transport = _FlushTransport(ModelCallResult(
        content="",
        tool_calls=[{
            "id": "memory-1",
            "type": "function",
            "function": {
                "name": "memory_add",
                "arguments": json.dumps({"target": "memory", "content": "remember"}),
            },
        }],
        finish_reason="tool_calls",
        reasoning=None,
        usage=usage,
        was_streamed=False,
        provider=context.provider,
        model=context.model,
    ))
    agent = _agent(monkeypatch, transport)
    manager = _MemoryManager()
    agent._memory_manager = manager
    agent._memory_store = object()
    original_tools = [{"type": "function", "function": {"name": "terminal"}}]
    original_names = {"terminal"}
    agent.tools = original_tools
    agent.valid_tool_names = original_names

    agent.flush_memories([
        {"role": "system", "content": "system"},
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "two"},
    ])

    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call["tools"] is manager.schemas
    assert call["options"].source == "memory_flush"
    cache_plan = call["options"].cache_plan
    assert isinstance(cache_plan, PromptCachePlan)
    assert cache_plan.enabled is True
    assert cache_plan.system_message_index == 0
    assert cache_plan.prefix_hash
    assert cache_plan.conversation_key
    assert call["messages"][0]["role"] == "system"
    assert manager.handled == [
        ("memory_add", {"target": "memory", "content": "remember"})
    ]
    assert agent.tools is original_tools
    assert agent.valid_tool_names is original_names
    assert agent.session_input_tokens == 8
    assert agent.session_output_tokens == 3
    assert agent.session_api_calls == 1


def test_memory_flush_timeout_never_mutates_agent_tool_state(monkeypatch) -> None:
    release = threading.Event()
    started = threading.Event()
    finished = threading.Event()

    class _BlockingTransport:
        def call(self, **_kwargs):
            started.set()
            release.wait(1)
            result = ModelCallResult(
                content="",
                tool_calls=[{
                    "id": "late",
                    "type": "function",
                    "function": {"name": "memory_add", "arguments": "{}"},
                }],
                finish_reason="stop",
                reasoning=None,
                usage=None,
                was_streamed=False,
                provider="test",
                model="model-a",
            )
            finished.set()
            return result

    agent = _agent(monkeypatch, _BlockingTransport())
    manager = _MemoryManager()
    agent._memory_manager = manager
    agent._memory_store = object()
    original_tools = [{"type": "function", "function": {"name": "terminal"}}]
    original_names = {"terminal"}
    agent.tools = original_tools
    agent.valid_tool_names = original_names

    agent.flush_memories(
        [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two"},
        ],
        timeout=0.01,
    )
    assert started.is_set()
    assert agent.tools is original_tools
    assert agent.valid_tool_names is original_names
    release.set()
    assert finished.wait(1)
    assert manager.handled == []


def test_memory_flush_timeout_late_usage_does_not_leak_into_next_turn(monkeypatch) -> None:
    release = threading.Event()
    recorded = threading.Event()

    class _BlockingTransport:
        def call(self, **_kwargs):
            release.wait(1)
            return ModelCallResult(
                content="",
                tool_calls=[],
                finish_reason="stop",
                reasoning=None,
                usage=UsageRecord(
                    provider="test",
                    model="model-a",
                    input_tokens=7,
                    output_tokens=2,
                    source="memory_flush",
                ),
                was_streamed=False,
                provider="test",
                model="model-a",
            )

    agent = _agent(monkeypatch, _BlockingTransport())
    agent._memory_manager = _MemoryManager()
    agent._memory_store = object()
    agent._usage_sink = lambda _record: recorded.set()

    agent.flush_memories(
        [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two"},
        ],
        timeout=0.01,
    )
    with agent._usage_lock:
        agent._turn_usage = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "reasoning_tokens": 0,
            "api_calls": 0,
        }

    release.set()
    assert recorded.wait(1)
    assert agent.session_input_tokens == 7
    assert agent.session_output_tokens == 2
    assert agent._turn_usage["input_tokens"] == 0
    assert agent._turn_usage["output_tokens"] == 0


def test_memory_flush_timeout_late_attempt_does_not_leak_into_next_turn(monkeypatch) -> None:
    pending: list[object] = []

    class _DeferredThread:
        def __init__(self, *, target, **_kwargs) -> None:
            pending.append(target)

        def start(self) -> None:
            pass

        def join(self, timeout=None) -> None:
            pass

        def is_alive(self) -> bool:
            return True

    class _InterruptedTransport:
        def call(self, **kwargs):
            assert kwargs["interrupted"]() is True
            return ModelCallResult(
                content="",
                tool_calls=None,
                finish_reason="interrupted",
                reasoning=None,
                usage=None,
                was_streamed=False,
                provider="test",
                model="model-a",
                interrupted=True,
            )

    monkeypatch.setattr("mclaw.agent.core.threading.Thread", _DeferredThread)
    agent = _agent(monkeypatch, _InterruptedTransport())
    agent._memory_manager = _MemoryManager()
    agent._memory_store = object()

    agent.flush_memories(
        [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two"},
        ],
        timeout=0.01,
    )
    with agent._usage_lock:
        agent._turn_usage = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "reasoning_tokens": 0,
            "api_calls": 0,
        }

    pending[0]()

    assert agent.session_api_calls == 1
    assert agent._turn_usage["api_calls"] == 0
