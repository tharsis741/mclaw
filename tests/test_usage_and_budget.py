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
                pruned = json.loads(calls["write-once"]["function"]["arguments"])
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


def test_restored_history_prunes_completed_large_tool_arguments_before_first_call(
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
    pruned_arguments = json.loads(calls["large-call"]["function"]["arguments"])
    assert pruned_arguments["path"] == "report.py"
    assert pruned_arguments["encoding"] == "utf-8"
    assert pruned_arguments["content"] == (
        "[MCLAW_INTERNAL_WRITE_CONTENT_PRUNED: original 2000 chars were already "
        "written successfully and removed from history; never use this marker as "
        "new write_file content; use read_file(path) to inspect the file]"
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
        _compressed_this_turn = False

        @staticmethod
        def compress(messages, **_kwargs):
            return [messages[0], *messages[2:]]

        @staticmethod
        def prune(messages, **_kwargs):
            return messages, 0

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

    compressor.reconfigure_model(next_context, context_window=2_000)
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
