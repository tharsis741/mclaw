# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import threading
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from mclaw.agent.context_compressor import (
    ContextCompressor,
    estimate_messages_tokens,
    estimate_tokens_rough,
)
from mclaw.agent.core import MClaw
from mclaw.agent.prompt_cache import PromptCachePlan
from mclaw.agent.token_budget import estimate_request_budget
from mclaw.agent.transports.base import (
    ModelCallError,
    ModelCallResult,
    ReasoningTrace,
)
from mclaw.agent.usage import (
    UsageRecord,
    parse_anthropic_usage,
    parse_openai_compatible_usage,
    reported_token,
)
from mclaw.providers.base import ModelTraits, RuntimeProviderProfile
from mclaw.providers.runtime import ProviderRuntimeContext
from mclaw.safety.context_rollback import ContextRollbackManager
from mclaw.state import SessionDB
from mclaw.tools.file_tools import write_file_tool


def _context(
    profile: RuntimeProviderProfile | None = None,
    *,
    model: str = "model-a",
) -> ProviderRuntimeContext:
    return ProviderRuntimeContext(
        profile=profile or RuntimeProviderProfile(name="test", display_name="Test"),
        model=model,
        api_key="secret",
        base_url="https://example.test/v1",
    )


def test_openai_usage_keeps_only_provider_reported_fields_and_provenance() -> None:
    context = _context(model="served/model")
    raw = SimpleNamespace(
        prompt_tokens=10,
        completion_tokens=0,
        prompt_tokens_details=SimpleNamespace(cached_tokens=3),
        completion_tokens_details=SimpleNamespace(reasoning_tokens=2),
    )

    usage = parse_openai_compatible_usage(raw, context, source="summary")

    assert usage == UsageRecord(
        provider="test",
        model="served/model",
        input_tokens=10,
        output_tokens=0,
        cache_read_tokens=3,
        reasoning_tokens=2,
        source="summary",
    )
    assert usage.total_tokens is None
    assert usage.available_fields == (
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "reasoning_tokens",
    )


def test_openai_usage_accepts_output_details_and_rejects_non_token_values() -> None:
    context = _context()
    raw = {
        "input_tokens": 7,
        "output_tokens": 4,
        "total_tokens": True,
        "input_tokens_details": {"cached_tokens": -1},
        "output_tokens_details": {"reasoning_tokens": 3},
    }

    usage = parse_openai_compatible_usage(raw, context, source="turn")

    assert usage is not None
    assert usage.total_tokens is None
    assert usage.cache_read_tokens is None
    assert usage.reasoning_tokens == 3
    assert reported_token({"first": 1.5, "second": 0}, "first", "second") == 0
    assert parse_openai_compatible_usage({}, context, source="turn") is None


def test_anthropic_usage_builds_full_input_total_from_reported_components() -> None:
    context = _context()
    usage = parse_anthropic_usage(
        {
            "input_tokens": 10,
            "cache_creation_input_tokens": 2,
            "cache_read_input_tokens": 3,
            "output_tokens": 4,
        },
        context,
        source="memory_flush",
    )

    assert usage == UsageRecord(
        provider="test",
        model="model-a",
        input_tokens=15,
        output_tokens=4,
        cache_read_tokens=3,
        cache_write_tokens=2,
        source="memory_flush",
    )
    assert usage.total_tokens is None


def test_counter_deltas_aggregate_calls_without_double_counting_breakdowns() -> None:
    records = [
        UsageRecord(input_tokens=10, output_tokens=4, cache_read_tokens=3),
        UsageRecord(input_tokens=8, output_tokens=2, cache_write_tokens=5),
        UsageRecord(reasoning_tokens=6, total_tokens=999),
    ]
    totals: dict[str, int] = {}
    for record in records:
        for name, value in record.to_counter_delta().items():
            totals[name] = totals.get(name, 0) + value

    assert totals == {
        "input_tokens": 18,
        "output_tokens": 6,
        "cache_read_tokens": 3,
        "cache_write_tokens": 5,
        "reasoning_tokens": 6,
    }
    assert totals["input_tokens"] + totals["output_tokens"] == 24


def test_compressor_update_contains_no_estimated_or_missing_values() -> None:
    assert UsageRecord(
        input_tokens=12,
        output_tokens=0,
        cache_read_tokens=4,
    ).to_compressor_update() == {
        "prompt_tokens": 12,
        "completion_tokens": 0,
    }


@dataclass(frozen=True)
class _CappedProfile(RuntimeProviderProfile):
    def model_traits(self, _model: str) -> ModelTraits:
        return ModelTraits(max_output_tokens=100)


def test_request_budget_adds_tools_dynamic_context_and_is_deterministic() -> None:
    context = _context()
    messages = [{"role": "user", "content": "hello world"}]
    tools = [{
        "type": "function",
        "function": {
            "parameters": {"properties": {"q": {"type": "string"}}, "type": "object"},
            "name": "lookup",
        },
    }]
    original_messages = deepcopy(messages)
    original_tools = deepcopy(tools)

    plain = estimate_request_budget(
        messages=messages,
        tools=[],
        dynamic_system_context="",
        context=context,
        context_window=128_000,
    )
    enriched = estimate_request_budget(
        messages=messages,
        tools=tools,
        dynamic_system_context="remember this",
        context=context,
        context_window=128_000,
    )
    reordered = estimate_request_budget(
        messages=messages,
        tools=[json.loads(json.dumps(tools[0], sort_keys=True))],
        dynamic_system_context="remember this",
        context=context,
        context_window=128_000,
    )

    assert plain.output_budget == 8_192
    assert plain.total_budget == plain.input_tokens + plain.output_budget
    assert enriched.input_tokens > plain.input_tokens
    assert reordered.input_tokens == enriched.input_tokens
    assert messages == original_messages
    assert tools == original_tools


def test_request_budget_clamps_only_to_model_output_limit() -> None:
    profile = _CappedProfile(name="capped", display_name="Capped")
    context = _context(profile)
    messages = [{"role": "user", "content": "x" * 100}]

    capped = estimate_request_budget(
        messages=messages,
        tools=[],
        dynamic_system_context="",
        context=context,
        context_window=10,
        max_output_tokens=500,
    )
    smaller = estimate_request_budget(
        messages=messages,
        tools=[],
        dynamic_system_context="",
        context=context,
        context_window=10,
        max_output_tokens=20,
    )

    assert capped.output_budget == 100
    assert smaller.output_budget == 20
    assert capped.total_budget > capped.context_window


@pytest.mark.parametrize("name,value", [("context_window", 0), ("context_window", True), ("max_output_tokens", -1)])
def test_request_budget_rejects_invalid_limits(name: str, value: object) -> None:
    kwargs = {
        "messages": [],
        "tools": [],
        "dynamic_system_context": "",
        "context": _context(),
        "context_window": 100,
        "max_output_tokens": None,
    }
    kwargs[name] = value

    with pytest.raises(ValueError, match="positive integer"):
        estimate_request_budget(**kwargs)  # type: ignore[arg-type]


def test_request_budget_rejects_noncanonical_request_inputs() -> None:
    context = _context()
    with pytest.raises(TypeError, match="messages"):
        estimate_request_budget(
            messages=["not-a-message"],  # type: ignore[list-item]
            tools=[],
            dynamic_system_context="",
            context=context,
            context_window=100,
        )
    with pytest.raises(TypeError, match="tools"):
        estimate_request_budget(
            messages=[],
            tools=["not-a-tool"],  # type: ignore[list-item]
            dynamic_system_context="",
            context=context,
            context_window=100,
        )


def test_request_budget_counts_matched_reasoning_once_and_skips_other_models() -> None:
    context = _context(model="model-a")
    direct = ReasoningTrace(
        text="same reasoning",
        provider="test",
        model="model-a",
        api_mode="chat_completions",
        format="reasoning_content",
        payload="same reasoning",
    )
    structured = ReasoningTrace(
        text="structured",
        provider="test",
        model="model-a",
        api_mode="chat_completions",
        format="reasoning_details",
        payload=[{"text": "structured", "type": "reasoning"}],
    )
    other_model = ReasoningTrace(
        text="must not count",
        provider="test",
        model="model-b",
        api_mode="chat_completions",
        format="reasoning_details",
        payload=[{"text": "must not count", "type": "reasoning"}],
    )
    messages = [
        {
            "role": "assistant",
            "content": "visible",
            "reasoning_content": "same reasoning",
            **direct.to_message_fields(),
        },
        {"role": "assistant", "content": "", **structured.to_message_fields()},
        {"role": "assistant", "content": "", **other_model.to_message_fields()},
    ]

    estimate = estimate_request_budget(
        messages=messages,
        tools=[],
        dynamic_system_context="",
        context=context,
        context_window=128_000,
    )
    expected = estimate_messages_tokens(messages) + estimate_tokens_rough(
        structured.to_budget_text()
    )

    assert estimate.input_tokens == expected


def test_request_budget_counts_gemini_display_and_signature_once_each() -> None:
    profile = RuntimeProviderProfile(name="google", display_name="Google")
    context = _context(profile, model="gemini-3.5-flash")
    trace = ReasoningTrace(
        text="visible thought",
        provider="google",
        model="gemini-3.5-flash",
        api_mode="chat_completions",
        format="gemini_thought_signature",
        payload=[{
            "field": "tool_calls.extra_content",
            "value": {"google": {"thought_signature": "signed-payload"}},
        }],
    )
    messages = [{"role": "assistant", "content": "", **trace.to_message_fields()}]
    signature_only = ReasoningTrace(
        format="gemini_thought_signature",
        payload=trace.payload,
    ).to_budget_text()

    estimate = estimate_request_budget(
        messages=messages,
        tools=[],
        dynamic_system_context="",
        context=context,
        context_window=128_000,
    )

    assert estimate.input_tokens == (
        estimate_messages_tokens(messages) + estimate_tokens_rough(signature_only)
    )


@dataclass(frozen=True)
class _FamilyProfile(RuntimeProviderProfile):
    @staticmethod
    def _same_family(origin: str, current: str) -> bool:
        return origin.split("-", 1)[0] == current.split("-", 1)[0]


def test_request_budget_reuses_profile_family_matcher() -> None:
    profile = _FamilyProfile(name="family", display_name="Family")
    context = _context(profile, model="series-new")
    trace = ReasoningTrace(
        text="family reasoning",
        provider="family",
        model="series-old",
        api_mode="chat_completions",
        format="reasoning_content",
        payload="family reasoning",
    )
    messages = [{"role": "assistant", "content": "", **trace.to_message_fields()}]

    estimate = estimate_request_budget(
        messages=messages,
        tools=[],
        dynamic_system_context="",
        context=context,
        context_window=100,
    )

    assert estimate.input_tokens == estimate_tokens_rough("family reasoning")


class _SequenceTransport:
    def __init__(self, *results: object) -> None:
        self.results = list(results)
        self.calls: list[dict[str, object]] = []

    def call(self, **kwargs):
        self.calls.append(kwargs)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _runtime_agent(
    monkeypatch,
    context: ProviderRuntimeContext,
    transport: _SequenceTransport,
    *,
    session_db: SessionDB | None = None,
    session_id: str = "runtime-session",
) -> MClaw:
    monkeypatch.setattr("mclaw.agent.core.create_transport", lambda _context: transport)
    monkeypatch.setattr(MClaw, "_discover_tools", lambda self: None)
    return MClaw(
        provider_runtime=context,
        session_db=session_db,
        session_id=session_id,
        system_prompt="system",
        skip_memory=True,
        config={"compression": {"enabled": False}},
    )


def test_configured_iteration_limit_returns_incomplete_stop_reason(monkeypatch) -> None:
    context = _context()

    def tool_result(call_id: str) -> ModelCallResult:
        return ModelCallResult(
            content="",
            tool_calls=[{
                "id": call_id,
                "type": "function",
                "function": {"name": "lookup", "arguments": "{}"},
            }],
            finish_reason="tool_calls",
            reasoning=None,
            usage=None,
            was_streamed=False,
            provider=context.provider,
            model=context.model,
        )

    transport = _SequenceTransport(tool_result("call-1"), tool_result("call-2"))
    monkeypatch.setattr("mclaw.agent.core.create_transport", lambda _context: transport)
    monkeypatch.setattr(MClaw, "_discover_tools", lambda self: None)

    unlimited = MClaw(
        provider_runtime=context,
        skip_memory=True,
        config={"compression": {"enabled": False}},
    )
    assert unlimited.max_iterations is None

    agent = MClaw(
        provider_runtime=context,
        skip_memory=True,
        config={
            "agent": {"max_turns": 2},
            "compression": {"enabled": False},
        },
    )

    def execute(tool_calls, messages, assistant_content="", reasoning=None):
        messages.append(agent._build_assistant_msg(assistant_content, tool_calls, reasoning))
        for call in tool_calls:
            messages.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "content": "ok",
            })

    agent._execute_tool_calls = execute  # type: ignore[method-assign]

    result = agent.run_conversation("keep working", advance_background_review=False)

    assert result["stop_reason"] == "max_iterations"
    assert result["completed"] is False
    assert result["api_calls"] == 2


@pytest.mark.parametrize("retry_first", [False, True])
def test_delegated_child_reserves_final_call_for_tool_free_summary(
    monkeypatch,
    retry_first: bool,
) -> None:
    from mclaw.tools.delegate_tool import _run_single_child

    context = _context()

    def model_result(*, content: str = "", call_id: str | None = None) -> ModelCallResult:
        tool_calls = None
        if call_id:
            tool_calls = [{
                "id": call_id,
                "type": "function",
                "function": {"name": "lookup", "arguments": json.dumps({"step": call_id})},
            }]
        return ModelCallResult(
            content=content,
            tool_calls=tool_calls,
            finish_reason="tool_calls" if tool_calls else "stop",
            reasoning=None,
            usage=None,
            was_streamed=False,
            provider=context.provider,
            model=context.model,
        )

    transport_results: list[object] = []
    if retry_first:
        transport_results.append(ModelCallError(
            message="retry",
            provider=context.provider,
            model=context.model,
            retryable=True,
            retry_after=0,
        ))
    transport_results.append(model_result(call_id="call-1"))
    if not retry_first:
        transport_results.append(model_result(call_id="call-2"))
    final_summary = "final summary " + "s" * 1_200
    transport_results.append(model_result(content=final_summary))
    transport = _SequenceTransport(*transport_results)
    child = _runtime_agent(monkeypatch, context, transport)
    child.max_iterations = 3
    child._delegate_depth = 1
    child.tools = [{
        "type": "function",
        "function": {"name": "lookup", "parameters": {"type": "object"}},
    }]
    child.valid_tool_names = {"lookup"}

    def execute(tool_calls, messages, assistant_content="", reasoning=None):
        messages.append(child._build_assistant_msg(assistant_content, tool_calls, reasoning))
        for call in tool_calls:
            messages.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "content": f"result for {call['id']}",
            })

    child._execute_tool_calls = execute  # type: ignore[method-assign]

    entry = _run_single_child(0, "research", child, parent_agent=None)

    assert entry["status"] == "completed"
    assert entry["exit_reason"] == "completed"
    assert entry["summary"] == final_summary
    assert entry["api_calls"] == 3
    assert len(transport.calls) == 3
    assert transport.calls[0]["tools"]
    assert transport.calls[1]["tools"]
    assert transport.calls[2]["tools"] == []
    last_call_id = "call-1" if retry_first else "call-2"
    assert any(
        message.get("tool_call_id") == last_call_id
        and message.get("content") == f"result for {last_call_id}"
        for message in transport.calls[2]["messages"]
    )
    assert child.max_iterations == 3
    assert child.tools


def test_delegated_timeout_summarizes_completed_tool_results(monkeypatch) -> None:
    from mclaw.tools.delegate_tool import _run_single_child

    context = _context()
    large_arguments = json.dumps({
        "path": "report.md",
        "content": "x" * 2_000,
    })
    tool_call = {
        "id": "write-before-timeout",
        "type": "function",
        "function": {"name": "write_file", "arguments": large_arguments},
    }
    transport = _SequenceTransport(
        ModelCallResult(
            content="",
            tool_calls=[tool_call],
            finish_reason="tool_calls",
            reasoning=None,
            usage=None,
            was_streamed=False,
            provider=context.provider,
            model=context.model,
        ),
        ModelCallResult(
            content="timeout summary",
            tool_calls=None,
            finish_reason="stop",
            reasoning=None,
            usage=None,
            was_streamed=False,
            provider=context.provider,
            model=context.model,
        ),
    )
    child = _runtime_agent(monkeypatch, context, transport)
    child.context_compressor = ContextCompressor(
        provider_runtime=context,
        context_window=128_000,
        quiet_mode=True,
    )
    child.max_iterations = 5
    child._delegate_depth = 1
    child.tools = [{
        "type": "function",
        "function": {"name": "write_file", "parameters": {"type": "object"}},
    }]
    child.valid_tool_names = {"write_file"}

    def execute(tool_calls, messages, assistant_content="", reasoning=None):
        messages.append(child._build_assistant_msg(assistant_content, tool_calls, reasoning))
        messages.append({
            "role": "tool",
            "tool_call_id": tool_calls[0]["id"],
            "content": json.dumps({
                "path": "report.md",
                "bytes_written": 2_000,
            }),
        })
        threading.Event().wait(0.12)

    child._execute_tool_calls = execute  # type: ignore[method-assign]

    events = []
    entry = _run_single_child(
        0,
        "research",
        child,
        parent_agent=None,
        progress_callback=events.append,
        timeout_seconds=0.1,
    )

    assert entry["status"] == "timed_out"
    assert entry["exit_reason"] == "timeout"
    assert entry["summary"] == "timeout summary"
    assert entry["api_calls"] == 2
    assert len(transport.calls) == 2
    assert transport.calls[0]["tools"]
    assert transport.calls[1]["tools"] == []
    assert any(event.event_type == "finalizing" for event in events)
    summary_calls = {
        call["id"]: call
        for message in transport.calls[1]["messages"]
        for call in message.get("tool_calls") or []
    }
    assert summary_calls["write-before-timeout"]["function"]["arguments"] == large_arguments
    assert any(
        message.get("tool_call_id") == "write-before-timeout"
        and json.loads(message.get("content") or "{}").get("bytes_written") == 2_000
        for message in transport.calls[1]["messages"]
    )


def test_oversized_child_summary_is_persisted_for_parent_handoff(tmp_path) -> None:
    from mclaw.tools.delegate_tool import _run_single_child

    full_summary = "result-" + "x" * 2_000

    class Child:
        max_iterations = 1
        tools = ["tool"]
        model = "test-model"
        _delegate_depth = 1
        _delegation_dir = tmp_path

        def run_conversation(self, **_kwargs):
            return {
                "final_response": full_summary,
                "completed": True,
                "interrupted": False,
                "api_calls": 1,
            }

    entry = _run_single_child(0, "research", Child(), parent_agent=None)

    assert len(entry["summary"]) < len(full_summary)
    assert "完整结果已保存" in entry["summary"]
    assert Path(entry["summary_path"]).read_text(encoding="utf-8") == full_summary


def test_delegation_publishes_only_after_every_child_finishes(monkeypatch) -> None:
    from mclaw.tools import delegate_tool

    release = threading.Event()
    second_started = threading.Event()
    task_id = "test-complete-handoff"

    def fake_run_single_child(task_index, goal, child, parent_agent, **kwargs):
        if task_index == 1:
            second_started.set()
            assert release.wait(timeout=1)
        return {
            "task_index": task_index,
            "goal": goal,
            "status": "completed",
            "summary": f"summary-{task_index}",
            "api_calls": 1,
            "duration_seconds": 0.01,
        }

    monkeypatch.setattr(delegate_tool, "_run_single_child", fake_run_single_child)
    tasks = [{"goal": "first"}, {"goal": "second"}]
    children = [(0, tasks[0], object()), (1, tasks[1], object())]
    coordinator = threading.Thread(
        target=delegate_tool._run_all_children_background,
        args=(tasks, children, None, None, task_id, 0.0, 600.0),
        daemon=True,
    )
    coordinator.start()
    try:
        assert second_started.wait(timeout=1)
        assert delegate_tool.get_pending_result_for_task(task_id, timeout=0.01) is None
    finally:
        release.set()
        coordinator.join(timeout=1)
    assert not coordinator.is_alive()
    result = delegate_tool.get_pending_result_for_task(task_id, timeout=0.1)
    assert result is not None
    assert [entry["summary"] for entry in result["results"]] == [
        "summary-0",
        "summary-1",
    ]


def test_delegate_task_passes_configured_timeout_to_child(monkeypatch) -> None:
    from mclaw.tools import delegate_tool

    child = SimpleNamespace()
    captured: dict[str, float] = {}
    monkeypatch.setattr(delegate_tool, "_build_child_agent", lambda **kwargs: child)

    def fake_run_single_child(task_index, goal, built_child, parent_agent, **kwargs):
        captured["timeout_seconds"] = kwargs["timeout_seconds"]
        return {
            "task_index": task_index,
            "goal": goal,
            "status": "completed",
            "summary": "done",
            "api_calls": 1,
            "duration_seconds": 0.01,
        }

    monkeypatch.setattr(delegate_tool, "_run_single_child", fake_run_single_child)
    parent = SimpleNamespace(
        _delegate_depth=0,
        config={"delegation": {"max_iterations": 50, "timeout_seconds": 900}},
    )

    result = json.loads(delegate_tool.delegate_task(
        tasks=[{"goal": "research"}],
        parent_agent=parent,
    ))

    assert result["success"] is True
    assert captured["timeout_seconds"] == 900.0


def test_delegated_summary_exception_restores_tools_and_reports_calls() -> None:
    from mclaw.tools.delegate_tool import _run_single_child

    class Child:
        max_iterations = 2
        tools = ["tool"]
        session_api_calls = 0
        _delegate_depth = 1

        def run_conversation(self, **kwargs):
            self.session_api_calls += 1
            if kwargs.get("disable_tools"):
                self.tools = []
                raise RuntimeError("summary failed")
            return {
                "final_response": "",
                "interrupted": False,
                "messages": [],
                "api_calls": 1,
            }

    child = Child()
    entry = _run_single_child(0, "research", child, parent_agent=None)

    assert entry["status"] == "error"
    assert entry["api_calls"] == 2
    assert child.max_iterations == 2
    assert child.tools == ["tool"]


@pytest.mark.parametrize("retryable", [False, True])
def test_delegated_api_error_is_not_completed(monkeypatch, retryable: bool) -> None:
    from mclaw.tools.delegate_tool import _run_single_child

    context = _context()
    child = _runtime_agent(
        monkeypatch,
        context,
        _SequenceTransport(ModelCallError(
            message="bad request",
            provider=context.provider,
            model=context.model,
            retryable=retryable,
            retry_after=0,
        )),
    )
    child.max_iterations = 1
    child._delegate_depth = 1

    entry = _run_single_child(0, "research", child, parent_agent=None)

    assert entry["status"] == "failed"
    assert entry["exit_reason"] == "error"
    assert entry["api_calls"] == 1


def test_successful_large_write_is_visible_once_before_pruning(monkeypatch) -> None:
    context = _context()
    large_arguments = json.dumps({
        "path": "report.md",
        "content": "x" * 2_000,
    })
    write_call = {
        "id": "write-once",
        "type": "function",
        "function": {"name": "write_file", "arguments": large_arguments},
    }
    lookup_call = {
        "id": "lookup-after-write",
        "type": "function",
        "function": {"name": "lookup", "arguments": "{}"},
    }

    class Transport:
        def __init__(self) -> None:
            self.call_index = 0

        def call(self, **kwargs):
            calls = {
                call["id"]: call
                for message in kwargs["messages"]
                for call in message.get("tool_calls") or []
            }
            if self.call_index == 0:
                result = ModelCallResult(
                    content="",
                    tool_calls=[write_call],
                    finish_reason="tool_calls",
                    reasoning=None,
                    usage=None,
                    was_streamed=False,
                    provider=context.provider,
                    model=context.model,
                )
            elif self.call_index == 1:
                assert calls["write-once"]["function"]["arguments"] == large_arguments
                result = ModelCallResult(
                    content="",
                    tool_calls=[lookup_call],
                    finish_reason="tool_calls",
                    reasoning=None,
                    usage=None,
                    was_streamed=False,
                    provider=context.provider,
                    model=context.model,
                )
            else:
                pruned = json.loads(
                    calls["write-once"]["function"]["arguments"]
                )
                assert pruned["path"] == "report.md"
                assert pruned["content"].startswith(
                    "[MCLAW_INTERNAL_WRITE_CONTENT_PRUNED:"
                )
                result = ModelCallResult(
                    content="done",
                    tool_calls=None,
                    finish_reason="stop",
                    reasoning=None,
                    usage=None,
                    was_streamed=False,
                    provider=context.provider,
                    model=context.model,
                )
            self.call_index += 1
            return result

    transport = Transport()
    monkeypatch.setattr("mclaw.agent.core.create_transport", lambda _context: transport)
    monkeypatch.setattr(MClaw, "_discover_tools", lambda self: None)
    agent = MClaw(
        provider_runtime=context,
        system_prompt="system",
        skip_memory=True,
        config={"compression": {"enabled": True}},
    )

    def execute(tool_calls, messages, assistant_content="", reasoning=None):
        messages.append(agent._build_assistant_msg(
            assistant_content,
            tool_calls,
            reasoning,
        ))
        for call in tool_calls:
            content = (
                {"path": "report.md", "bytes_written": 2_000}
                if call["id"] == "write-once"
                else {"success": True, "value": "ok"}
            )
            messages.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "content": json.dumps(content),
            })

    agent._execute_tool_calls = execute  # type: ignore[method-assign]

    result = agent.run_conversation("create report", advance_background_review=False)

    assert result["final_response"] == "done"
    assert transport.call_index == 3


def test_non_write_tool_results_remain_raw_after_seen_once(
    monkeypatch,
) -> None:
    context = _context()
    tool_calls = [
        {
            "id": f"batch-{i}",
            "type": "function",
            "function": {"name": "lookup", "arguments": "{}"},
        }
        for i in range(21)
    ]
    tool_results = {
        call["id"]: json.dumps({
            "success": True,
            "value": f"result-{i}-" + "x" * 500,
        })
        for i, call in enumerate(tool_calls)
    }
    lookup_call = {
        "id": "lookup-after-batch",
        "type": "function",
        "function": {"name": "lookup", "arguments": "{}"},
    }

    class Transport:
        def __init__(self) -> None:
            self.call_index = 0

        def call(self, **kwargs):
            if self.call_index == 0:
                result = ModelCallResult(
                    content="",
                    tool_calls=tool_calls,
                    finish_reason="tool_calls",
                    reasoning=None,
                    usage=None,
                    was_streamed=False,
                    provider=context.provider,
                    model=context.model,
                )
            elif self.call_index == 1:
                visible_results = {
                    message.get("tool_call_id"): message.get("content")
                    for message in kwargs["messages"]
                    if message.get("tool_call_id") in tool_results
                }
                assert visible_results == tool_results
                result = ModelCallResult(
                    content="",
                    tool_calls=[lookup_call],
                    finish_reason="tool_calls",
                    reasoning=None,
                    usage=None,
                    was_streamed=False,
                    provider=context.provider,
                    model=context.model,
                )
            else:
                visible_results = {
                    message.get("tool_call_id"): message.get("content")
                    for message in kwargs["messages"]
                    if message.get("tool_call_id") in tool_results
                }
                assert visible_results == tool_results
                result = ModelCallResult(
                    content="done",
                    tool_calls=None,
                    finish_reason="stop",
                    reasoning=None,
                    usage=None,
                    was_streamed=False,
                    provider=context.provider,
                    model=context.model,
                )
            self.call_index += 1
            return result

    transport = Transport()
    monkeypatch.setattr("mclaw.agent.core.create_transport", lambda _context: transport)
    monkeypatch.setattr(MClaw, "_discover_tools", lambda self: None)
    agent = MClaw(
        provider_runtime=context,
        system_prompt="system",
        skip_memory=True,
        config={"compression": {"enabled": True}},
    )

    def execute(calls, messages, assistant_content="", reasoning=None):
        messages.append(agent._build_assistant_msg(
            assistant_content,
            calls,
            reasoning,
        ))
        for call in calls:
            messages.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "content": tool_results.get(
                    call["id"],
                    json.dumps({"success": True, "value": "ok"}),
                ),
            })

    agent._execute_tool_calls = execute  # type: ignore[method-assign]

    result = agent.run_conversation("run the batch", advance_background_review=False)

    assert result["final_response"] == "done"
    assert transport.call_index == 3


def test_failed_follow_up_keeps_pending_write_raw(monkeypatch) -> None:
    context = _context()
    large_arguments = json.dumps({
        "path": "report.md",
        "content": "x" * 2_000,
    })
    write_call = {
        "id": "write-fail",
        "type": "function",
        "function": {"name": "write_file", "arguments": large_arguments},
    }

    class Transport:
        call_index = 0

        def call(self, **kwargs):
            if self.call_index == 0:
                result = ModelCallResult(
                    content="",
                    tool_calls=[write_call],
                    finish_reason="tool_calls",
                    reasoning=None,
                    usage=None,
                    was_streamed=False,
                    provider=context.provider,
                    model=context.model,
                )
            else:
                sent_call = next(
                    call
                    for message in kwargs["messages"]
                    for call in message.get("tool_calls") or []
                    if call.get("id") == "write-fail"
                )
                assert sent_call["function"]["arguments"] == large_arguments
                raise ModelCallError(
                    message="provider failed",
                    provider=context.provider,
                    model=context.model,
                )
            self.call_index += 1
            return result

    transport = Transport()
    monkeypatch.setattr("mclaw.agent.core.create_transport", lambda _context: transport)
    monkeypatch.setattr(MClaw, "_discover_tools", lambda self: None)
    agent = MClaw(
        provider_runtime=context,
        system_prompt="system",
        skip_memory=True,
        config={"compression": {"enabled": True}},
    )

    def execute(calls, messages, assistant_content="", reasoning=None):
        messages.append(agent._build_assistant_msg(
            assistant_content,
            calls,
            reasoning,
        ))
        messages.append({
            "role": "tool",
            "tool_call_id": calls[0]["id"],
            "content": json.dumps({"path": "report.md", "bytes_written": 2_000}),
        })

    agent._execute_tool_calls = execute  # type: ignore[method-assign]

    result = agent.run_conversation("create report", advance_background_review=False)

    assert result["completed"] is False
    assert agent._tool_call_ids_pending_visibility == {"write-fail"}


def test_tui_context_uses_first_estimate_then_provider_input_usage(
    monkeypatch,
) -> None:
    context = _context()
    tool_call = {
        "id": "lookup-usage",
        "type": "function",
        "function": {"name": "lookup", "arguments": "{}"},
    }

    class Transport:
        call_index = 0
        agent = None

        def call(self, **kwargs):
            compressor = self.agent.context_compressor
            if self.call_index == 0:
                expected = estimate_request_budget(
                    messages=kwargs["messages"],
                    tools=kwargs["tools"],
                    dynamic_system_context=kwargs["options"].dynamic_system_context,
                    context=context,
                    context_window=compressor.context_length,
                ).input_tokens
                assert compressor.display_context_estimated is True
                assert compressor.display_context_tokens == expected
                result = ModelCallResult(
                    content="",
                    tool_calls=[tool_call],
                    finish_reason="tool_calls",
                    reasoning=None,
                    usage=UsageRecord(
                        input_tokens=100,
                        output_tokens=10,
                        total_tokens=110,
                    ),
                    was_streamed=False,
                    provider=context.provider,
                    model=context.model,
                )
            else:
                # Tool execution must not replace the last API report with an
                # estimate of the enlarged message list.
                assert compressor.display_context_estimated is False
                assert compressor.display_context_tokens == 100
                result = ModelCallResult(
                    content="done",
                    tool_calls=None,
                    finish_reason="stop",
                    reasoning=None,
                    usage=UsageRecord(input_tokens=150, output_tokens=20),
                    was_streamed=False,
                    provider=context.provider,
                    model=context.model,
                )
            self.call_index += 1
            return result

    transport = Transport()
    monkeypatch.setattr("mclaw.agent.core.create_transport", lambda _context: transport)
    monkeypatch.setattr(MClaw, "_discover_tools", lambda self: None)
    agent = MClaw(
        provider_runtime=context,
        system_prompt="system",
        skip_memory=True,
        config={"compression": {"enabled": True}},
    )
    transport.agent = agent

    def execute(calls, messages, assistant_content="", reasoning=None):
        messages.append(agent._build_assistant_msg(
            assistant_content,
            calls,
            reasoning,
        ))
        messages.append({
            "role": "tool",
            "tool_call_id": calls[0]["id"],
            "content": json.dumps({"success": True, "value": "ok"}),
        })

    agent._execute_tool_calls = execute  # type: ignore[method-assign]

    result = agent.run_conversation("look it up", advance_background_review=False)

    assert result["final_response"] == "done"
    assert agent.context_compressor.display_context_tokens == 150
    assert agent.context_compressor.display_context_estimated is False


def test_input_only_usage_replaces_first_request_estimate(monkeypatch) -> None:
    context = _context()
    tool_call = {
        "id": "lookup-partial",
        "type": "function",
        "function": {"name": "lookup", "arguments": "{}"},
    }

    class Transport:
        call_index = 0
        agent = None
        first_estimate = 0

        def call(self, **_kwargs):
            compressor = self.agent.context_compressor
            if self.call_index == 0:
                self.first_estimate = compressor.display_context_tokens
                result = ModelCallResult(
                    content="",
                    tool_calls=[tool_call],
                    finish_reason="tool_calls",
                    reasoning=None,
                    usage=UsageRecord(input_tokens=100),
                    was_streamed=False,
                    provider=context.provider,
                    model=context.model,
                )
            else:
                assert compressor.display_context_estimated is False
                assert compressor.display_context_tokens == 100
                result = ModelCallResult(
                    content="done",
                    tool_calls=None,
                    finish_reason="stop",
                    reasoning=None,
                    usage=None,
                    was_streamed=False,
                    provider=context.provider,
                    model=context.model,
                )
            self.call_index += 1
            return result

    transport = Transport()
    monkeypatch.setattr("mclaw.agent.core.create_transport", lambda _context: transport)
    monkeypatch.setattr(MClaw, "_discover_tools", lambda self: None)
    agent = MClaw(
        provider_runtime=context,
        system_prompt="system",
        skip_memory=True,
        config={"compression": {"enabled": True}},
    )
    transport.agent = agent
    agent._execute_tool_calls = lambda calls, messages, **_kwargs: messages.extend([  # type: ignore[method-assign]
        agent._build_assistant_msg("", calls),
        {
            "role": "tool",
            "tool_call_id": calls[0]["id"],
            "content": json.dumps({"success": True, "value": "ok"}),
        },
    ])

    agent.run_conversation("look it up", advance_background_review=False)

    assert agent.context_compressor.display_context_estimated is False
    assert agent.context_compressor.display_context_tokens == 100


def test_later_user_turn_keeps_last_actual_while_backend_still_estimates(
    monkeypatch,
    caplog,
) -> None:
    context = _context()

    class Transport:
        call_index = 0
        agent = None

        def call(self, **_kwargs):
            compressor = self.agent.context_compressor
            if self.call_index == 0:
                assert compressor.display_context_estimated is True
                assert compressor.display_context_tokens is not None
                usage = UsageRecord(input_tokens=100, output_tokens=10, total_tokens=110)
                content = "first"
            else:
                assert compressor.display_context_estimated is False
                assert compressor.display_context_tokens == 100
                usage = UsageRecord(input_tokens=150, output_tokens=20, total_tokens=170)
                content = "second"
            self.call_index += 1
            return ModelCallResult(
                content=content,
                tool_calls=None,
                finish_reason="stop",
                reasoning=None,
                usage=usage,
                was_streamed=False,
                provider=context.provider,
                model=context.model,
            )

    transport = Transport()
    monkeypatch.setattr("mclaw.agent.core.create_transport", lambda _context: transport)
    monkeypatch.setattr(MClaw, "_discover_tools", lambda self: None)
    agent = MClaw(
        provider_runtime=context,
        system_prompt="system",
        skip_memory=True,
        config={"compression": {"enabled": True}},
    )
    transport.agent = agent
    caplog.set_level(20, logger="mclaw.agent.core")

    first = agent.run_conversation("first user", advance_background_review=False)
    second = agent.run_conversation(
        "second user",
        conversation_history=first["messages"],
        advance_background_review=False,
    )

    logs = [record.getMessage() for record in caplog.records]
    assert second["final_response"] == "second"
    assert agent.context_compressor.display_context_tokens == 150
    assert agent.context_compressor.display_context_estimated is False
    assert sum("[TUI CONTEXT] source=estimate" in message for message in logs) == 1
    assert sum(
        "[LOOP] estimating tokens for preventive compression" in message
        for message in logs
    ) == 2


def test_missing_input_usage_clears_first_estimate_without_reestimating(
    monkeypatch,
    caplog,
) -> None:
    context = _context()

    class Transport:
        call_index = 0
        agent = None

        def call(self, **_kwargs):
            compressor = self.agent.context_compressor
            if self.call_index == 0:
                assert compressor.display_context_estimated is True
                assert compressor.display_context_tokens is not None
                usage = UsageRecord(output_tokens=10, total_tokens=10)
            else:
                assert compressor._display_estimate_emitted is True
                assert compressor.display_context_estimated is False
                assert compressor.display_context_tokens is None
                usage = None
            self.call_index += 1
            return ModelCallResult(
                content=f"answer-{self.call_index}",
                tool_calls=None,
                finish_reason="stop",
                reasoning=None,
                usage=usage,
                was_streamed=False,
                provider=context.provider,
                model=context.model,
            )

    transport = Transport()
    monkeypatch.setattr("mclaw.agent.core.create_transport", lambda _context: transport)
    monkeypatch.setattr(MClaw, "_discover_tools", lambda self: None)
    agent = MClaw(
        provider_runtime=context,
        system_prompt="system",
        skip_memory=True,
        config={"compression": {"enabled": True}},
    )
    transport.agent = agent
    caplog.set_level(20, logger="mclaw.agent.core")

    first = agent.run_conversation("first user", advance_background_review=False)
    agent.run_conversation(
        "second user",
        conversation_history=first["messages"],
        advance_background_review=False,
    )

    logs = [record.getMessage() for record in caplog.records]
    assert agent.context_compressor.display_context_tokens is None
    assert agent.context_compressor.display_context_estimated is False
    assert sum("[TUI CONTEXT] source=estimate" in message for message in logs) == 1


def test_unexpected_transport_error_clears_first_context_estimate(
    monkeypatch,
) -> None:
    context = _context()

    class Transport:
        def call(self, **_kwargs):
            raise RuntimeError("transport crashed")

    monkeypatch.setattr("mclaw.agent.core.create_transport", lambda _context: Transport())
    monkeypatch.setattr(MClaw, "_discover_tools", lambda self: None)
    agent = MClaw(
        provider_runtime=context,
        system_prompt="system",
        skip_memory=True,
        config={"compression": {"enabled": True}},
    )

    with pytest.raises(RuntimeError, match="transport crashed"):
        agent.run_conversation("first user", advance_background_review=False)

    compressor = agent.context_compressor
    assert compressor.display_context_tokens is None
    assert compressor.display_context_estimated is False
    assert compressor._display_estimate_emitted is True


@pytest.mark.parametrize("record_actual", [False, True])
def test_post_transport_error_keeps_only_confirmed_context_usage(
    monkeypatch,
    record_actual,
) -> None:
    context = _context()
    transport = _SequenceTransport(ModelCallResult(
        content="done",
        tool_calls=None,
        finish_reason="stop",
        reasoning=None,
        usage=UsageRecord(
            provider=context.provider,
            model=context.model,
            input_tokens=321,
            source="turn",
        ),
        was_streamed=False,
        provider=context.provider,
        model=context.model,
    ))
    agent = _runtime_agent(monkeypatch, context, transport)
    agent.context_compressor = ContextCompressor(
        provider_runtime=context,
        context_window=128_000,
        quiet_mode=True,
    )

    original_record_usage = agent._record_usage

    def fail_usage(usage):
        if record_actual:
            original_record_usage(usage)
        raise RuntimeError("usage processing crashed")

    monkeypatch.setattr(agent, "_record_usage", fail_usage)

    with pytest.raises(RuntimeError, match="usage processing crashed"):
        agent.run_conversation("first user", advance_background_review=False)

    compressor = agent.context_compressor
    assert compressor.display_context_tokens == (321 if record_actual else None)
    assert compressor.display_context_estimated is False
    assert compressor._display_estimate_emitted is True


def test_summary_api_receives_ten_thousand_content_chars() -> None:
    compressor = ContextCompressor(
        provider_runtime=_context(),
        context_window=128_000,
        quiet_mode=True,
    )
    content = "H" * 7_000 + "M" * 2_000 + "T" * 3_000

    message = {
        "role": "tool",
        "tool_call_id": "large-result",
        "content": content,
    }
    serialized = compressor._serialize_for_summary([message])

    assert serialized == (
        "[TOOL RESULT large-result]: "
        + "H" * 7_000
        + "\n...[truncated]...\n"
        + "T" * 3_000
    )
    calls: list[dict] = []

    class Transport:
        def call(self, **kwargs):
            calls.append(kwargs)
            return ModelCallResult(
                content="summary",
                tool_calls=None,
                finish_reason="stop",
                reasoning=None,
                usage=None,
                was_streamed=False,
                provider=compressor.provider_runtime.provider,
                model=compressor.provider_runtime.model,
            )

    compressor._summary_runtime = compressor.provider_runtime
    compressor._summary_transport = Transport()

    summary = compressor._generate_summary([message])

    assert summary is not None and summary.endswith("summary")
    assert len(calls) == 1
    assert serialized in calls[0]["messages"][0]["content"]
    assert calls[0]["options"].source == "summary"


def test_confirmed_tool_fallback_prunes_oldest_but_protects_pending() -> None:
    compressor = ContextCompressor(
        provider_runtime=_context(),
        context_window=128_000,
        quiet_mode=True,
    )
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "pending-oldest",
                "type": "function",
                "function": {"name": "lookup", "arguments": "{}"},
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "pending-oldest",
            "content": "P" * 6_000,
        },
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "confirmed-old",
                "type": "function",
                "function": {"name": "lookup", "arguments": "{}"},
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "confirmed-old",
            "content": "O" * 4_000,
        },
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "confirmed-new-large",
                "type": "function",
                "function": {"name": "lookup", "arguments": "{}"},
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "confirmed-new-large",
            "content": "N" * 8_000,
        },
    ]
    original = deepcopy(messages)

    pruned_messages, pruned, saved = compressor.prune_confirmed_tool_results(
        messages,
        tokens_to_save=500,
        protected_tool_call_ids={"pending-oldest"},
    )

    contents = {
        message.get("tool_call_id"): message.get("content")
        for message in pruned_messages
        if message.get("role") == "tool"
    }
    assert pruned == 1
    assert saved >= 500
    assert contents["pending-oldest"] == "P" * 6_000
    assert contents["confirmed-old"].startswith(
        "[MCLAW_CONTEXT_FALLBACK_TOOL_RESULT_PRUNED:"
    )
    assert contents["confirmed-new-large"] == "N" * 8_000
    assert [message.get("tool_call_id") for message in pruned_messages] == [
        message.get("tool_call_id") for message in messages
    ]
    assert messages == original


def test_confirmed_tool_fallback_keeps_raw_session_result(tmp_path) -> None:
    db = SessionDB(tmp_path / "fallback-raw.db")
    session_id = "fallback-raw"
    raw_content = "R" * 4_000
    call = {
        "id": "confirmed-db",
        "type": "function",
        "function": {"name": "lookup", "arguments": "{}"},
    }
    try:
        db.create_session(session_id, "cli", model="model-a")
        db.append_message(
            session_id,
            "assistant",
            content="",
            tool_calls=[call],
            turn_id="tool-turn",
        )
        db.append_message(
            session_id,
            "tool",
            content=raw_content,
            tool_call_id="confirmed-db",
            turn_id="tool-turn",
        )
        history = db.get_messages_as_conversation(session_id)
        compressor = ContextCompressor(
            provider_runtime=_context(),
            context_window=128_000,
            quiet_mode=True,
        )

        projection, pruned, _saved = compressor.prune_confirmed_tool_results(
            history,
            tokens_to_save=1,
        )

        projected_result = next(
            message["content"]
            for message in projection
            if message.get("tool_call_id") == "confirmed-db"
        )
        persisted_result = next(
            message["content"]
            for message in db.get_messages_as_conversation(session_id)
            if message.get("tool_call_id") == "confirmed-db"
        )
        assert pruned == 1
        assert projected_result.startswith(
            "[MCLAW_CONTEXT_FALLBACK_TOOL_RESULT_PRUNED:"
        )
        assert persisted_result == raw_content
    finally:
        db.close()


def test_full_compression_keeps_entire_latest_user_turn_raw(monkeypatch) -> None:
    context = _context()
    compressor = ContextCompressor(
        provider_runtime=context,
        context_window=1_000,
        protect_first_n=1,
        quiet_mode=True,
    )
    latest_user = {
        "role": "user",
        "content": "LATEST USER REQUEST: preserve this exact text",
    }
    tool_calls = [
        {
            "id": f"current-{i}",
            "type": "function",
            "function": {"name": "lookup", "arguments": "{}"},
        }
        for i in range(6)
    ]
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "old request"},
        {"role": "assistant", "content": "old response"},
        latest_user,
        {"role": "assistant", "content": "", "tool_calls": tool_calls},
        *[
            {
                "role": "tool",
                "tool_call_id": call["id"],
                "content": json.dumps({
                    "success": True,
                    "value": "x" * 2_000,
                }),
            }
            for call in tool_calls
        ],
    ]
    summarized: list[dict] = []

    def generate_summary(turns):
        summarized.extend(deepcopy(turns))
        return "old turns summary"

    monkeypatch.setattr(compressor, "_generate_summary", generate_summary)

    compressed = compressor.compress(messages)

    assert summarized == messages[1:3]
    assert latest_user not in summarized
    latest_user_idx = compressed.index(latest_user)
    assert compressed[latest_user_idx:] == messages[3:]


def test_full_compression_keeps_resumed_pending_batch_raw(monkeypatch) -> None:
    context = _context()
    compressor = ContextCompressor(
        provider_runtime=context,
        context_window=1_000,
        protect_first_n=1,
        quiet_mode=True,
    )
    pending_calls = [
        {
            "id": f"pending-{i}",
            "type": "function",
            "function": {"name": "lookup", "arguments": "{}"},
        }
        for i in range(2)
    ]
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "old request"},
        {"role": "assistant", "content": "old response"},
        {"role": "assistant", "content": "", "tool_calls": pending_calls},
        *[
            {
                "role": "tool",
                "tool_call_id": call["id"],
                "content": json.dumps({"success": True, "value": "x" * 2_000}),
            }
            for call in pending_calls
        ],
        {"role": "user", "content": "resume and continue"},
    ]
    summarized: list[dict] = []

    def generate_summary(turns):
        summarized.extend(deepcopy(turns))
        return "old turns summary"

    monkeypatch.setattr(compressor, "_generate_summary", generate_summary)

    compressed = compressor.compress(
        messages,
        protected_tool_call_ids={"pending-0", "pending-1"},
    )

    assert summarized == messages[1:3]
    pending_start = compressed.index(messages[3])
    assert compressed[pending_start:] == messages[3:]


def test_completed_restored_write_is_pruned_at_pre_api_boundary(
    monkeypatch,
) -> None:
    context = _context()
    transport = _SequenceTransport(ModelCallResult(
        content="done",
        tool_calls=None,
        finish_reason="stop",
        reasoning=None,
        usage=None,
        was_streamed=False,
        provider=context.provider,
        model=context.model,
    ))
    agent = _runtime_agent(monkeypatch, context, transport)
    agent.context_compressor = ContextCompressor(
        provider_runtime=context,
        context_window=128_000,
        quiet_mode=True,
    )
    large_arguments = json.dumps({
        "path": "report.py",
        "content": "x" * 2_000,
        "encoding": "utf-8",
    })
    small_arguments = json.dumps({"path": "notes.txt", "content": "small"})
    failed_arguments = json.dumps({"path": "blocked.py", "content": "y" * 2_000})
    terminal_arguments = json.dumps({"command": "z" * 2_000})

    def tool_pair(call_id, name, arguments, result, **extra):
        call = {
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": arguments},
            **extra,
        }
        return [
            {"role": "assistant", "content": "", "tool_calls": [call]},
            {
                "role": "tool",
                "tool_call_id": call_id,
                "content": json.dumps(result),
            },
        ]

    history = [
        {"role": "system", "content": "system"},
        *tool_pair(
            "large-call",
            "write_file",
            large_arguments,
            {"path": "report.py", "bytes_written": 2_000},
            extra_content={"provider": "signature"},
        ),
        *tool_pair(
            "small-call",
            "write_file",
            small_arguments,
            {"path": "notes.txt", "bytes_written": 5},
        ),
        *tool_pair(
            "failed-call",
            "write_file",
            failed_arguments,
            {"error": "blocked"},
        ),
        *tool_pair(
            "terminal-call",
            "terminal",
            terminal_arguments,
            {"output": "ok", "returncode": 0, "error": ""},
        ),
    ]
    original_history = deepcopy(history)

    agent.run_conversation(
        "continue",
        conversation_history=history,
        advance_background_review=False,
    )

    sent = transport.calls[0]["messages"]
    calls = {
        call["id"]: call
        for message in sent
        for call in message.get("tool_calls") or []
    }
    pruned_large = json.loads(calls["large-call"]["function"]["arguments"])
    assert pruned_large["path"] == "report.py"
    assert pruned_large["encoding"] == "utf-8"
    assert pruned_large["content"].startswith(
        "[MCLAW_INTERNAL_WRITE_CONTENT_PRUNED:"
    )
    assert calls["large-call"]["id"] == "large-call"
    assert calls["large-call"]["function"]["name"] == "write_file"
    assert calls["large-call"]["extra_content"] == {"provider": "signature"}
    assert calls["small-call"]["function"]["arguments"] == small_arguments
    assert calls["failed-call"]["function"]["arguments"] == failed_arguments
    assert calls["terminal-call"]["function"]["arguments"] == terminal_arguments
    assert {
        call["id"] for call in calls.values()
    } == {
        message["tool_call_id"] for message in sent if message.get("role") == "tool"
    }
    assert history == original_history


def test_resume_rebuilds_pending_batch_before_pre_api_prune(
    monkeypatch,
    tmp_path,
) -> None:
    context = _context()
    db = SessionDB(tmp_path / "resume.db")
    session_id = "resume-pending"
    pending_calls = [
        {
            "id": f"pending-{i}",
            "type": "function",
            "function": {
                "name": "write_file",
                "arguments": json.dumps({
                    "path": f"pending-{i}.txt",
                    "content": "x" * 2_000,
                }),
            },
        }
        for i in range(2)
    ]
    raw_results = {
        call["id"]: json.dumps({"success": True, "value": "x" * 500})
        for call in pending_calls
    }
    final = ModelCallResult(
        content="done",
        tool_calls=None,
        finish_reason="stop",
        reasoning=None,
        usage=None,
        was_streamed=False,
        provider=context.provider,
        model=context.model,
    )
    transport = _SequenceTransport(final)

    try:
        db.create_session(session_id, "cli", model=context.model)
        db.append_message(
            session_id,
            "assistant",
            content="",
            tool_calls=pending_calls,
            turn_id="turn-tools",
        )
        for call in pending_calls:
            db.append_message(
                session_id,
                "tool",
                content=raw_results[call["id"]],
                tool_call_id=call["id"],
                turn_id="turn-tools",
            )
        # Runtime notices and later user input do not prove model visibility.
        db.append_message(session_id, "assistant", content="runtime notice")
        for i in range(21):
            db.append_message(
                session_id,
                "user",
                content=f"queued user message {i}",
                turn_id=f"queued-{i}",
            )

        history = db.get_messages_as_conversation(session_id)
        agent = _runtime_agent(
            monkeypatch,
            context,
            transport,
            session_db=db,
            session_id=session_id,
        )
        agent.context_compressor = ContextCompressor(
            provider_runtime=context,
            context_window=128_000,
            quiet_mode=True,
        )

        assert agent._tool_call_ids_pending_visibility == {
            "pending-0",
            "pending-1",
        }

        agent.run_conversation(
            "continue",
            conversation_history=history,
            advance_background_review=False,
        )

        sent_results = {
            message.get("tool_call_id"): message.get("content")
            for message in transport.calls[0]["messages"]
            if message.get("tool_call_id") in raw_results
        }
        assert sent_results == raw_results
        sent_calls = {
            call["id"]: call
            for message in transport.calls[0]["messages"]
            for call in message.get("tool_calls") or []
            if call.get("id") in raw_results
        }
        assert all(
            json.loads(sent_calls[call["id"]]["function"]["arguments"])["content"]
            == "x" * 2_000
            for call in pending_calls
        )
        assert agent._tool_call_ids_pending_visibility == set()

        # The persisted final model assistant now proves the batch was seen.
        resumed_again = _runtime_agent(
            monkeypatch,
            context,
            _SequenceTransport(final),
            session_db=db,
            session_id=session_id,
        )
        assert resumed_again._tool_call_ids_pending_visibility == set()
    finally:
        db.close()


@pytest.mark.parametrize(
    "finish_reason",
    [
        "stream_stalled",
        "stream_timeout",
        "stream_error",
        "stream_create_timeout",
        "stream_incomplete",
    ],
)
def test_incomplete_stream_keeps_pending_in_memory_and_after_resume(
    monkeypatch,
    tmp_path,
    finish_reason,
) -> None:
    context = _context()
    db = SessionDB(tmp_path / f"{finish_reason}.db")
    session_id = f"pending-{finish_reason}"
    pending_call = {
        "id": "pending-stream",
        "type": "function",
        "function": {"name": "lookup", "arguments": "{}"},
    }
    partial = ModelCallResult(
        content="partial response",
        tool_calls=None,
        finish_reason=finish_reason,
        reasoning=None,
        usage=UsageRecord(input_tokens=123),
        was_streamed=True,
        provider=context.provider,
        model=context.model,
        interrupted=False,
    )

    try:
        db.create_session(session_id, "cli", model=context.model)
        db.append_message(
            session_id,
            "assistant",
            content="",
            tool_calls=[pending_call],
            finish_reason="tool_calls",
            turn_id="tool-turn",
        )
        db.append_message(
            session_id,
            "tool",
            content="raw tool result",
            tool_call_id="pending-stream",
            turn_id="tool-turn",
        )
        history = db.get_messages_as_conversation(session_id)
        agent = _runtime_agent(
            monkeypatch,
            context,
            _SequenceTransport(partial),
            session_db=db,
            session_id=session_id,
        )

        result = agent.run_conversation(
            "continue",
            conversation_history=history,
            advance_background_review=False,
        )

        assert result["final_response"] == "partial response"
        assert result["completed"] is False
        assert result["stop_reason"] == finish_reason
        assert result["assistant_rounds"][-1]["is_final"] is False
        assert agent._tool_call_ids_pending_visibility == {"pending-stream"}
        assert db.get_pending_tool_call_ids(session_id) == {"pending-stream"}

        resumed = _runtime_agent(
            monkeypatch,
            context,
            _SequenceTransport(partial),
            session_db=db,
            session_id=session_id,
        )
        assert resumed._tool_call_ids_pending_visibility == {"pending-stream"}
    finally:
        db.close()


def test_pending_recovery_is_conservative_for_legacy_tool_rows(tmp_path) -> None:
    db = SessionDB(tmp_path / "legacy-resume.db")
    session_id = "legacy-pending"
    legacy_call = {
        "id": "legacy-tool",
        "type": "function",
        "function": {"name": "lookup", "arguments": "{}"},
    }

    try:
        db.create_session(session_id, "cli", model="legacy-model")
        db.append_message(
            session_id,
            "assistant",
            content="",
            tool_calls=[legacy_call],
        )
        db.append_message(
            session_id,
            "tool",
            content=json.dumps({"success": True, "value": "raw"}),
            tool_call_id="legacy-tool",
        )
        db.append_message(session_id, "assistant", content="runtime notice")

        assert db.get_pending_tool_call_ids(session_id) == {"legacy-tool"}

        db.append_message(
            session_id,
            "assistant",
            content="current model reply",
            turn_id="current-turn",
        )
        assert db.get_pending_tool_call_ids(session_id) == set()
    finally:
        db.close()


def test_context_reload_rebuilds_live_pending_visibility(tmp_path) -> None:
    db = SessionDB(tmp_path / "rollback-pending.db")
    session_id = "rollback-pending"
    call = {
        "id": "rollback-tool",
        "type": "function",
        "function": {"name": "lookup", "arguments": "{}"},
    }
    agent = SimpleNamespace(
        session_id=session_id,
        messages=[],
        session_user_messages=0,
        _tool_call_ids_pending_visibility=set(),
    )

    try:
        db.create_session(session_id, "cli", model="model-a")
        db.append_message(
            session_id,
            "assistant",
            content="",
            tool_calls=[call],
            turn_id="tool-turn",
        )
        db.append_message(
            session_id,
            "tool",
            content=json.dumps({"success": True, "value": "raw"}),
            tool_call_id="rollback-tool",
            turn_id="tool-turn",
        )
        manager = ContextRollbackManager(session_db=db, agent=agent)

        manager.reload_agent_messages(session_id)
        assert agent._tool_call_ids_pending_visibility == {"rollback-tool"}

        db.append_message(
            session_id,
            "assistant",
            content="seen",
            turn_id="response-turn",
        )
        manager.reload_agent_messages(session_id)
        assert agent._tool_call_ids_pending_visibility == set()
    finally:
        db.close()


def test_write_pruning_invalidates_stale_provider_prompt_for_compression_check(
    monkeypatch,
) -> None:
    context = _context()
    old_call = {
        "id": "old-write",
        "type": "function",
        "function": {
            "name": "write_file",
            "arguments": json.dumps({
                "path": "old.txt",
                "content": "x" * 2_000,
            }),
        },
    }
    history = [
        {"role": "system", "content": "system"},
        {"role": "assistant", "content": "", "tool_calls": [old_call]},
        {
            "role": "tool",
            "tool_call_id": "old-write",
            "content": json.dumps({"success": True, "bytes_written": 2_000}),
        },
        *[
            {"role": "user", "content": f"filler {i}"}
            for i in range(21)
        ],
    ]
    transport = _SequenceTransport(ModelCallResult(
        content="done",
        tool_calls=None,
        finish_reason="stop",
        reasoning=None,
        usage=None,
        was_streamed=False,
        provider=context.provider,
        model=context.model,
    ))
    monkeypatch.setattr("mclaw.agent.core.create_transport", lambda _context: transport)
    monkeypatch.setattr(MClaw, "_discover_tools", lambda self: None)
    agent = MClaw(
        provider_runtime=context,
        system_prompt="system",
        skip_memory=True,
        config={"compression": {"enabled": True}},
    )
    agent.context_compressor.last_prompt_tokens = 100_000
    agent.context_compressor.threshold_tokens = 50_000
    compress_calls = []

    def unexpected_compress(messages, **_kwargs):
        compress_calls.append(messages)
        return messages

    monkeypatch.setattr(agent.context_compressor, "compress", unexpected_compress)

    agent.run_conversation(
        "continue",
        conversation_history=history,
        advance_background_review=False,
    )

    assert compress_calls == []
    old_write = next(
        call
        for message in transport.calls[0]["messages"]
        for call in message.get("tool_calls") or []
        if call.get("id") == "old-write"
    )
    old_arguments = json.loads(old_write["function"]["arguments"])
    assert old_arguments["path"] == "old.txt"
    assert old_arguments["content"].startswith(
        "[MCLAW_INTERNAL_WRITE_CONTENT_PRUNED:"
    )


@pytest.mark.parametrize("marker", [
    "[Earlier content argument cleared after successful write; 19436 chars]",
    (
        "[MCLAW_INTERNAL_WRITE_CONTENT_PRUNED: original 19436 chars were already "
        "written successfully and removed from history; never use this marker as "
        "new write_file content; use read_file(path) to inspect the file]"
    ),
])
def test_write_file_rejects_internal_pruning_marker(tmp_path, marker: str) -> None:
    target = tmp_path / "report.md"
    target.write_text("existing content", encoding="utf-8")

    result = json.loads(write_file_tool(str(target), marker))

    assert result["success"] is False
    assert "context-pruning marker" in result["error"]
    assert target.read_text(encoding="utf-8") == "existing content"


@pytest.mark.parametrize("trigger", ["preventive", "context_overflow"])
def test_compression_flush_receives_current_working_history(
    monkeypatch,
    trigger: str,
) -> None:
    context = _context()
    final = ModelCallResult(
        content="answer",
        tool_calls=None,
        finish_reason="stop",
        reasoning=None,
        usage=None,
        was_streamed=False,
        provider=context.provider,
        model=context.model,
    )
    results: list[object] = [final]
    if trigger == "context_overflow":
        results.insert(0, ModelCallError(
            message="context limit",
            provider=context.provider,
            model=context.model,
            context_limit=True,
        ))
    agent = _runtime_agent(monkeypatch, context, _SequenceTransport(*results))

    class Compressor:
        context_length = 128_000
        threshold_tokens = 1 if trigger == "preventive" else 127_000
        last_prompt_tokens = 128_000 if trigger == "preventive" else 0
        last_completion_tokens = 0
        display_context_tokens = 0
        display_context_estimated = False
        _compressed_this_turn = False

        @staticmethod
        def prune(messages, **_kwargs):
            return messages, 0

        @classmethod
        def compress(cls, messages, **_kwargs):
            cls._compressed_this_turn = True
            return [messages[0], *messages[2:]]

    agent.context_compressor = Compressor()
    flushed: list[list[dict] | None] = []

    def capture_flush(messages=None, **_kwargs):
        flushed.append(deepcopy(messages))

    agent.flush_memories = capture_flush  # type: ignore[method-assign]
    result = agent.run_conversation(
        "current user",
        conversation_history=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": "old user"},
            {"role": "assistant", "content": "old answer"},
        ],
        advance_background_review=False,
    )

    assert result["final_response"] == "answer"
    assert len(flushed) == 1
    assert flushed[0] is not None
    assert flushed[0][-1] == {"role": "user", "content": "current user"}


def test_preventive_compression_does_not_report_noop_as_done(
    monkeypatch,
    caplog,
) -> None:
    context = _context()
    transport = _SequenceTransport(ModelCallResult(
        content="answer",
        tool_calls=None,
        finish_reason="stop",
        reasoning=None,
        usage=None,
        was_streamed=False,
        provider=context.provider,
        model=context.model,
    ))
    agent = _runtime_agent(monkeypatch, context, transport)

    class Compressor:
        context_length = 128_000
        threshold_tokens = 1
        last_prompt_tokens = 128_000
        last_completion_tokens = 0
        display_context_tokens = 0
        display_context_estimated = False
        _compressed_this_turn = False

        @staticmethod
        def prune(messages, **_kwargs):
            return messages, 0

        @staticmethod
        def compress(messages, **_kwargs):
            return messages

    compressor = Compressor()
    agent.context_compressor = compressor
    caplog.set_level(20, logger="mclaw.agent.core")

    result = agent.run_conversation(
        "current user",
        conversation_history=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": "old user"},
            {"role": "assistant", "content": "old answer"},
        ],
        advance_background_review=False,
    )

    log_messages = [record.getMessage() for record in caplog.records]
    assert result["final_response"] == "answer"
    assert compressor._compressed_this_turn is False
    assert "[LOOP] compression done" not in log_messages
    assert (
        "[LOOP] compression unavailable; continuing without compaction"
        in log_messages
    )


def test_summary_failure_prunes_confirmed_tools_to_eighty_percent_then_calls_model(
    monkeypatch,
    caplog,
) -> None:
    context = _context(_CappedProfile(name="capped", display_name="Capped"))
    transport = _SequenceTransport(ModelCallResult(
        content="done",
        tool_calls=None,
        finish_reason="stop",
        reasoning=None,
        usage=None,
        was_streamed=False,
        provider=context.provider,
        model=context.model,
    ))
    agent = _runtime_agent(monkeypatch, context, transport)
    agent.context_compressor = ContextCompressor(
        provider_runtime=context,
        context_window=2_000,
        threshold_percent=0.50,
        protect_first_n=1,
        quiet_mode=True,
    )
    monkeypatch.setattr(
        agent.context_compressor,
        "_generate_summary",
        lambda _turns: None,
    )
    agent._tool_visibility_state_reliable = True
    agent._tool_call_ids_pending_visibility = {"pending-current"}
    statuses: list[str] = []
    agent._status_callback = statuses.append
    history = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "old request"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "confirmed-1",
                "type": "function",
                "function": {"name": "lookup", "arguments": "{}"},
            }],
        },
        {"role": "tool", "tool_call_id": "confirmed-1", "content": "A" * 4_000},
        {"role": "assistant", "content": "first result seen"},
        {"role": "user", "content": "continue"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "confirmed-2",
                "type": "function",
                "function": {"name": "lookup", "arguments": "{}"},
            }],
        },
        {"role": "tool", "tool_call_id": "confirmed-2", "content": "B" * 4_000},
        {"role": "assistant", "content": "second result seen"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "pending-current",
                "type": "function",
                "function": {"name": "lookup", "arguments": "{}"},
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "pending-current",
            "content": "P" * 400,
        },
    ]
    original = deepcopy(history)
    caplog.set_level(20, logger="mclaw.agent.core")

    result = agent.run_conversation(
        "current request",
        conversation_history=history,
        advance_background_review=False,
    )

    sent = transport.calls[0]["messages"]
    sent_budget = estimate_request_budget(
        messages=sent,
        tools=[],
        dynamic_system_context="",
        context=context,
        context_window=2_000,
    )
    sent_results = {
        message.get("tool_call_id"): message.get("content")
        for message in sent
        if message.get("role") == "tool"
    }
    logs = [record.getMessage() for record in caplog.records]
    assert result["final_response"] == "done"
    assert sent_budget.input_tokens <= 800
    assert sent_results["confirmed-1"].startswith(
        "[MCLAW_CONTEXT_FALLBACK_TOOL_RESULT_PRUNED:"
    )
    assert sent_results["confirmed-2"].startswith(
        "[MCLAW_CONTEXT_FALLBACK_TOOL_RESULT_PRUNED:"
    )
    assert sent_results["pending-current"] == "P" * 400
    assert any("Pruning tool results" in status for status in statuses)
    assert any("[CONTEXT FALLBACK START]" in message for message in logs)
    assert any("target=800" in message for message in logs)
    assert "[LOOP] compression done" not in logs
    assert history == original


def test_successful_preventive_summary_above_target_adds_tool_fallback(
    monkeypatch,
) -> None:
    context = _context(_CappedProfile(name="capped", display_name="Capped"))
    transport = _SequenceTransport(ModelCallResult(
        content="done",
        tool_calls=None,
        finish_reason="stop",
        reasoning=None,
        usage=None,
        was_streamed=False,
        provider=context.provider,
        model=context.model,
    ))
    agent = _runtime_agent(monkeypatch, context, transport)
    agent.context_compressor = ContextCompressor(
        provider_runtime=context,
        context_window=2_000,
        threshold_percent=0.50,
        protect_first_n=4,
        quiet_mode=True,
    )
    summary_calls = 0

    def generate_summary(_turns):
        nonlocal summary_calls
        summary_calls += 1
        return "successful summary"

    monkeypatch.setattr(agent.context_compressor, "_generate_summary", generate_summary)
    real_compress = agent.context_compressor.compress
    post_summary_inputs: list[int] = []

    def capture_compress(messages, **kwargs):
        projection = real_compress(messages, **kwargs)
        post_summary_inputs.append(estimate_request_budget(
            messages=projection,
            tools=[],
            dynamic_system_context="",
            context=context,
            context_window=2_000,
        ).input_tokens)
        return projection

    monkeypatch.setattr(agent.context_compressor, "compress", capture_compress)
    agent._tool_visibility_state_reliable = True
    history = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "old request"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "confirmed-head",
                "type": "function",
                "function": {"name": "lookup", "arguments": "{}"},
            }],
        },
        {"role": "tool", "tool_call_id": "confirmed-head", "content": "T" * 2_400},
        *[
            {
                "role": "user" if i % 2 == 0 else "assistant",
                "content": f"filler-{i}-" + "F" * 400,
            }
            for i in range(12)
        ],
    ]
    original = deepcopy(history)

    result = agent.run_conversation(
        "current request",
        conversation_history=history,
        advance_background_review=False,
    )

    sent = transport.calls[0]["messages"]
    sent_result = next(
        message["content"]
        for message in sent
        if message.get("tool_call_id") == "confirmed-head"
    )
    sent_budget = estimate_request_budget(
        messages=sent,
        tools=[],
        dynamic_system_context="",
        context=context,
        context_window=2_000,
    )
    assert result["final_response"] == "done"
    assert summary_calls == 1
    assert len(post_summary_inputs) == 1
    assert 800 < post_summary_inputs[0] < 1_000
    assert len(transport.calls) == 1
    assert any(message.get("content") == "successful summary" for message in sent)
    assert sent_result.startswith("[MCLAW_CONTEXT_FALLBACK_TOOL_RESULT_PRUNED:")
    assert sent_budget.input_tokens <= 800
    assert history == original


def test_context_overflow_uses_summary_then_fallback_in_finite_stages(
    monkeypatch,
) -> None:
    context = _context(_CappedProfile(name="capped", display_name="Capped"))
    transport = _SequenceTransport(
        ModelCallError(
            message="first context limit",
            provider=context.provider,
            model=context.model,
            context_limit=True,
        ),
        ModelCallError(
            message="second context limit",
            provider=context.provider,
            model=context.model,
            context_limit=True,
            retryable=True,
            retry_after=0,
        ),
        ModelCallError(
            message="third context limit",
            provider=context.provider,
            model=context.model,
            context_limit=True,
            retryable=True,
            retry_after=0,
        ),
    )
    agent = _runtime_agent(monkeypatch, context, transport)
    agent.context_compressor = ContextCompressor(
        provider_runtime=context,
        context_window=2_000,
        threshold_percent=0.50,
        protect_first_n=1,
        quiet_mode=True,
    )
    compressor = agent.context_compressor
    summary_calls = 0

    def compress(messages, **_kwargs):
        nonlocal summary_calls
        summary_calls += 1
        projection = deepcopy(messages)
        old_user = next(
            message
            for message in projection
            if message.get("role") == "user" and len(message.get("content", "")) == 200
        )
        old_user["content"] = old_user["content"][:100]
        compressor._compressed_this_turn = True
        compressor.last_compression_outcome = "compressed"
        return projection

    monkeypatch.setattr(compressor, "compress", compress)
    agent._tool_visibility_state_reliable = True
    history = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "U" * 200},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "confirmed-1",
                "type": "function",
                "function": {"name": "lookup", "arguments": "{}"},
            }],
        },
        {"role": "tool", "tool_call_id": "confirmed-1", "content": "A" * 1_600},
        {"role": "assistant", "content": "first result seen"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "confirmed-2",
                "type": "function",
                "function": {"name": "lookup", "arguments": "{}"},
            }],
        },
        {"role": "tool", "tool_call_id": "confirmed-2", "content": "B" * 1_600},
        {"role": "assistant", "content": "second result seen"},
    ]

    result = agent.run_conversation(
        "current request",
        conversation_history=history,
        advance_background_review=False,
    )

    def sent_results(call_index):
        return {
            message.get("tool_call_id"): message.get("content")
            for message in transport.calls[call_index]["messages"]
            if message.get("role") == "tool"
        }

    first = sent_results(0)
    second = sent_results(1)
    third = sent_results(2)
    assert result["completed"] is False
    assert result["error"] == "third context limit"
    assert len(transport.calls) == 3
    assert summary_calls == 1
    assert first["confirmed-1"] == "A" * 1_600
    assert first["confirmed-2"] == "B" * 1_600
    assert second["confirmed-1"].startswith(
        "[MCLAW_CONTEXT_FALLBACK_TOOL_RESULT_PRUNED:"
    )
    assert second["confirmed-2"] == "B" * 1_600
    assert third["confirmed-1"].startswith(
        "[MCLAW_CONTEXT_FALLBACK_TOOL_RESULT_PRUNED:"
    )
    assert third["confirmed-2"].startswith(
        "[MCLAW_CONTEXT_FALLBACK_TOOL_RESULT_PRUNED:"
    )


def test_context_overflow_uses_confirmed_tool_fallback_before_single_retry(
    monkeypatch,
    caplog,
) -> None:
    context = _context(_CappedProfile(name="capped", display_name="Capped"))
    transport = _SequenceTransport(
        ModelCallError(
            message="context limit",
            provider=context.provider,
            model=context.model,
            context_limit=True,
        ),
        ModelCallResult(
            content="done",
            tool_calls=None,
            finish_reason="stop",
            reasoning=None,
            usage=None,
            was_streamed=False,
            provider=context.provider,
            model=context.model,
        ),
    )
    agent = _runtime_agent(monkeypatch, context, transport)
    agent.context_compressor = ContextCompressor(
        provider_runtime=context,
        context_window=10_000,
        protect_first_n=1,
        quiet_mode=True,
    )
    agent.context_compressor.threshold_tokens = 100_000
    monkeypatch.setattr(
        agent.context_compressor,
        "_generate_summary",
        lambda _turns: None,
    )
    agent._tool_visibility_state_reliable = True
    caplog.set_level(20, logger="mclaw.agent.core")
    history = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "old request"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "confirmed-overflow",
                "type": "function",
                "function": {"name": "lookup", "arguments": "{}"},
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "confirmed-overflow",
            "content": "X" * 4_000,
        },
        {"role": "assistant", "content": "result seen"},
        {"role": "user", "content": "more work"},
        {"role": "assistant", "content": "ready"},
    ]

    result = agent.run_conversation(
        "current request",
        conversation_history=history,
        advance_background_review=False,
    )

    first_result = next(
        message["content"]
        for message in transport.calls[0]["messages"]
        if message.get("tool_call_id") == "confirmed-overflow"
    )
    retried_result = next(
        message["content"]
        for message in transport.calls[1]["messages"]
        if message.get("tool_call_id") == "confirmed-overflow"
    )
    assert result["final_response"] == "done"
    assert len(transport.calls) == 2
    assert first_result == "X" * 4_000
    assert retried_result.startswith(
        "[MCLAW_CONTEXT_FALLBACK_TOOL_RESULT_PRUNED:"
    )
    assert sum(
        "[TUI CONTEXT] source=estimate" in record.getMessage()
        for record in caplog.records
    ) == 1


def test_core_transport_attempt_usage_hydration_and_reasoning_persistence(
    monkeypatch,
    tmp_path,
) -> None:
    context = _context(model="served/model")
    trace = ReasoningTrace(
        text="private reasoning",
        provider=context.provider,
        model=context.model,
        api_mode=context.api_mode,
        format="reasoning_content",
        payload="private reasoning",
    )
    usage = UsageRecord(
        provider=context.provider,
        model=context.model,
        input_tokens=3,
        output_tokens=2,
        cache_read_tokens=1,
        cache_write_tokens=4,
        reasoning_tokens=5,
        source="turn",
    )
    transport = _SequenceTransport(
        ModelCallError(
            message="retry",
            provider=context.provider,
            model=context.model,
            retryable=True,
            retry_after=0,
        ),
        ModelCallResult(
            content="answer",
            tool_calls=None,
            finish_reason="stop",
            reasoning=trace,
            usage=usage,
            was_streamed=False,
            provider=context.provider,
            model=context.model,
        ),
    )
    db = SessionDB(tmp_path / "runtime.db")
    try:
        db.create_session("runtime-session", "cli", model="old")
        db.update_token_counts(
            "runtime-session",
            input_tokens=10,
            output_tokens=20,
            cache_read_tokens=30,
            cache_write_tokens=40,
            reasoning_tokens=50,
        )
        agent = _runtime_agent(
            monkeypatch,
            context,
            transport,
            session_db=db,
        )

        assert agent.session_input_tokens == 10
        assert agent.session_output_tokens == 20
        with pytest.raises(AttributeError):
            agent.model = "mutated"  # type: ignore[misc]

        result = agent.run_conversation(
            "hello",
            extra_system="temporary context",
            advance_background_review=False,
        )

        assert result["api_calls"] == 2
        assert result["token_usage"] == {
            "input_tokens": 3,
            "output_tokens": 2,
            "cache_read_tokens": 1,
            "cache_write_tokens": 4,
            "reasoning_tokens": 5,
            "api_calls": 2,
        }
        assert agent.session_input_tokens == 13
        assert agent.session_output_tokens == 22
        assert agent.session_cache_read_tokens == 31
        assert agent.session_cache_write_tokens == 44
        assert agent.session_reasoning_tokens == 55
        assert agent.session_api_calls == 2
        assert len(transport.calls) == 2
        retry_plans = []
        for call in transport.calls:
            assert call["options"].dynamic_system_context == "temporary context"
            cache_plan = call["options"].cache_plan
            assert isinstance(cache_plan, PromptCachePlan)
            assert cache_plan.enabled is True
            assert cache_plan.system_message_index == 0
            assert cache_plan.prefix_hash
            assert cache_plan.conversation_key
            retry_plans.append(cache_plan)
        assert retry_plans[0] == retry_plans[1]

        restored = db.get_messages_as_conversation("runtime-session")[-1]
        assert ReasoningTrace.from_message(restored) == trace
        row = db.get_session("runtime-session")
        assert row is not None
        assert row["model"] == context.model
        assert row["input_tokens"] == 13
        assert row["reasoning_tokens"] == 55
        assert db.get_model_config("runtime-session") == context.snapshot()
    finally:
        db.close()


def test_pending_return_finalizes_usage_without_leaking_into_next_turn(monkeypatch) -> None:
    context = _context()

    def result(*, content="", tool_calls=None, input_tokens: int, output_tokens: int):
        return ModelCallResult(
            content=content,
            tool_calls=tool_calls,
            finish_reason="tool_calls" if tool_calls else "stop",
            reasoning=None,
            usage=UsageRecord(
                provider=context.provider,
                model=context.model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                source="turn",
            ),
            was_streamed=False,
            provider=context.provider,
            model=context.model,
        )

    transport = _SequenceTransport(
        result(
            tool_calls=[{
                "id": "delegate-1",
                "type": "function",
                "function": {"name": "delegate_task", "arguments": "{}"},
            }],
            input_tokens=3,
            output_tokens=2,
        ),
        result(content="done", input_tokens=5, output_tokens=1),
    )
    agent = _runtime_agent(monkeypatch, context, transport)
    agent._execute_tool_calls = lambda *_args, **_kwargs: {  # type: ignore[method-assign]
        "pending": True,
        "api_calls": 999,
        "token_usage": {"input_tokens": 999},
    }

    first = agent.run_conversation("start", advance_background_review=False)
    second = agent.run_conversation("continue", advance_background_review=False)

    assert first["pending"] is True
    assert first["api_calls"] == 1
    assert first["token_usage"] == {
        "input_tokens": 3,
        "output_tokens": 2,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "reasoning_tokens": 0,
        "api_calls": 1,
    }
    assert second["final_response"] == "done"
    assert second["token_usage"] == {
        "input_tokens": 5,
        "output_tokens": 1,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "reasoning_tokens": 0,
        "api_calls": 1,
    }
    assert agent.session_input_tokens == 8
    assert agent.session_output_tokens == 3
    assert agent.session_api_calls == 2


def test_background_usage_updates_are_locked_and_token_only(
    monkeypatch,
    tmp_path,
) -> None:
    context = _context()
    transport = _SequenceTransport()
    db = SessionDB(tmp_path / "concurrent.db")
    try:
        agent = _runtime_agent(
            monkeypatch,
            context,
            transport,
            session_db=db,
            session_id="concurrent-session",
        )
        snapshot = db.get_model_config("concurrent-session")
        record = UsageRecord(
            provider=context.provider,
            model=context.model,
            input_tokens=2,
            output_tokens=1,
            source="background_review",
        )
        workers = [
            threading.Thread(
                target=agent._record_usage,
                args=(record,),
                kwargs={"include_in_turn": False},
            )
            for _ in range(12)
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()

        assert agent.session_input_tokens == 24
        assert agent.session_output_tokens == 12
        row = db.get_session("concurrent-session")
        assert row is not None
        assert row["input_tokens"] == 24
        assert row["output_tokens"] == 12
        assert db.get_model_config("concurrent-session") == snapshot
    finally:
        db.close()


def test_compressor_reconfigure_is_in_memory_and_clears_summary_cache() -> None:
    current = _context(model="model-a")
    next_context = _context(model="model-b")
    compressor = ContextCompressor(
        provider_runtime=current,
        context_window=1_000,
        quiet_mode=True,
    )
    compressor._summary_runtime = current
    compressor._summary_transport = object()
    compressor.display_context_tokens = 999
    compressor.display_context_estimated = True
    compressor._display_estimate_emitted = True
    compressor.update_from_response(
        {"prompt_tokens": 800, "completion_tokens": 100, "total_tokens": 900}
    )

    compressor.reconfigure_model(next_context, context_window=2_000)

    assert compressor.last_prompt_tokens == 0
    assert compressor.last_completion_tokens == 0
    assert compressor.last_total_tokens == 0
    assert compressor.display_context_tokens is None
    assert compressor.display_context_estimated is False
    assert compressor._display_estimate_emitted is False

    compressor.update_from_response(
        {"prompt_tokens": 0, "completion_tokens": 7, "total_tokens": 7}
    )

    assert compressor.provider_runtime is next_context
    assert compressor.provider_runtime.model == "model-b"
    assert compressor.context_length == 2_000
    assert compressor.threshold_tokens == 1_000
    assert compressor._summary_runtime is None
    assert compressor._summary_transport is None
    assert compressor.last_prompt_tokens == 0
    assert compressor.last_completion_tokens == 7
    assert compressor.last_total_tokens == 7


def test_compressor_summary_lazily_inherits_runtime_and_reports_usage(
    monkeypatch,
) -> None:
    current = _context(model="model-a")
    usage = UsageRecord(
        provider=current.provider,
        model="model-summary",
        input_tokens=4,
        output_tokens=2,
        source="summary",
    )
    created: list[ProviderRuntimeContext] = []
    calls: list[dict] = []

    class _SummaryTransport:
        def __init__(self, context: ProviderRuntimeContext) -> None:
            self.context = context

        def call(self, **kwargs):
            calls.append(kwargs)
            return ModelCallResult(
                content="short summary",
                tool_calls=None,
                finish_reason="stop",
                reasoning=None,
                usage=usage,
                was_streamed=False,
                provider=self.context.provider,
                model=self.context.model,
            )

    def _factory(context: ProviderRuntimeContext):
        created.append(context)
        return _SummaryTransport(context)

    monkeypatch.setattr("mclaw.agent.context_compressor.create_transport", _factory)
    recorded: list[UsageRecord] = []
    compressor = ContextCompressor(
        provider_runtime=current,
        context_window=1_000,
        summary_model_override="model-summary",
        summary_timeout=600,
        usage_callback=recorded.append,
        quiet_mode=True,
    )

    first = compressor._summarize("prompt", 100)
    second = compressor._summarize("prompt", 100)

    assert first.endswith("short summary")
    assert second.endswith("short summary")
    assert len(created) == 1
    assert created[0].profile is current.profile
    assert created[0].api_key == current.api_key
    assert created[0].base_url == current.base_url
    assert created[0].model == "model-summary"
    assert calls[0]["options"].source == "summary"
    assert calls[0]["options"].timeout == 600.0
    assert calls[0]["options"].max_output_tokens == 200
    assert calls[0]["options"].cache_plan is None
    assert recorded == [usage, usage]
