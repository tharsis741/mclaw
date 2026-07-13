# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from copy import deepcopy
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from mclaw.agent.core import MClaw
from mclaw.agent.prompt_cache import PromptCachePlan, build_prompt_cache_plan
from mclaw.agent.transports.anthropic_messages import AnthropicMessagesTransport
from mclaw.agent.transports.base import ModelCallOptions, ModelCallResult, ReasoningTrace
from mclaw.agent.transports.openai_chat_completions import OpenAIChatCompletionsTransport
from mclaw.cli.config import DEFAULT_CONFIG
from mclaw.providers.base import (
    PROMPT_CACHE_LAYOUT_AUTOMATIC_PREFIX,
    PROMPT_CACHE_LAYOUT_ANTHROPIC_SYSTEM,
    PROMPT_CACHE_LAYOUT_GEMINI_USER_TAIL,
    PROMPT_CACHE_LAYOUT_OPENAI_SYSTEM,
    PROMPT_CACHE_LAYOUT_OPENROUTER_GEMINI,
    PROMPT_CACHE_LAYOUT_OPENROUTER_SYSTEM,
    PROMPT_CACHE_LAYOUT_QWEN_SYSTEM,
    PROMPT_CACHE_LAYOUT_XAI_CONVERSATION,
)
from mclaw.providers.registry import PROVIDER_REGISTRY
from mclaw.providers.runtime import ProviderRuntimeContext


def _context(
    provider: str,
    model: str,
    *,
    api_key: str = "test-secret",
    base_url: str | None = None,
) -> ProviderRuntimeContext:
    profile = PROVIDER_REGISTRY[provider]
    return ProviderRuntimeContext(
        profile=profile,
        model=profile.normalize_model(model),
        api_key=api_key,
        base_url=base_url if base_url is not None else profile.base_url,
    )


def _messages(system: str = "stable system", user: str = "question") -> list[dict]:
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _tools(description: str = "stable tool") -> list[dict]:
    return [{
        "type": "function",
        "function": {
            "name": "lookup",
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    }]


def _plan(
    context: ProviderRuntimeContext,
    *,
    messages: list[dict] | None = None,
    tools: list[dict] | None = None,
    session_id: str = "session-a",
    enabled: bool = True,
) -> PromptCachePlan:
    return build_prompt_cache_plan(
        messages=messages if messages is not None else _messages(),
        tools=tools if tools is not None else _tools(),
        context=context,
        session_id=session_id,
        enabled=enabled,
    )


class _FakeStream:
    def __init__(self, values: list[dict]) -> None:
        self.values = values
        self.closed = False

    def __iter__(self):
        return iter(self.values)

    def close(self) -> None:
        self.closed = True


class _CapturingCompletions:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("stream"):
            return _FakeStream([{
                "choices": [{
                    "delta": {"content": "ok"},
                    "finish_reason": "stop",
                }],
                "usage": {
                    "prompt_tokens": 5,
                    "completion_tokens": 1,
                    "prompt_tokens_details": {"cached_tokens": 0},
                },
            }])
        return {
            "choices": [{
                "message": {"content": "ok"},
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": 5,
                "completion_tokens": 1,
                "prompt_tokens_details": {"cached_tokens": 0},
            },
        }


class _CapturingOpenAIClient:
    def __init__(self) -> None:
        self.completions = _CapturingCompletions()
        self.chat = SimpleNamespace(completions=self.completions)


class _CapturingAnthropicAPI:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
            "usage": {
                "input_tokens": 2,
                "cache_creation_input_tokens": 3,
                "cache_read_input_tokens": 4,
                "output_tokens": 1,
            },
        }


def _openai_request(
    provider: str,
    model: str,
    *,
    enabled: bool,
    dynamic: str = "volatile context",
) -> tuple[dict, PromptCachePlan]:
    context = _context(provider, model)
    messages = _messages()
    tools = _tools()
    plan = _plan(context, messages=messages, tools=tools, enabled=enabled)
    client = _CapturingOpenAIClient()
    OpenAIChatCompletionsTransport(context, client).call(
        messages=messages,
        tools=tools,
        options=ModelCallOptions(
            dynamic_system_context=dynamic,
            cache_plan=plan,
        ),
    )
    return client.completions.calls[0], plan


def test_minimal_prompt_cache_plan_defaults_are_frozen() -> None:
    plan = PromptCachePlan()
    assert plan == PromptCachePlan(
        enabled=False,
        system_message_index=None,
        prefix_hash="",
        conversation_key="",
    )
    with pytest.raises(FrozenInstanceError):
        plan.enabled = True  # type: ignore[misc]


def test_prefix_hash_uses_only_first_system_and_deterministic_tool_schemas() -> None:
    context = _context("openai", "gpt-5.6")
    first = _plan(context, messages=_messages(user="first"))
    suffix_changed = _plan(context, messages=_messages(user="second"))
    field_order_changed = _plan(
        context,
        tools=[{
            "function": {
                "parameters": {
                    "required": ["query"],
                    "properties": {"query": {"type": "string"}},
                    "type": "object",
                },
                "description": "stable tool",
                "name": "lookup",
            },
            "type": "function",
        }],
    )
    system_changed = _plan(context, messages=_messages(system="new epoch"))
    tool_changed = _plan(context, tools=_tools(description="changed tool"))

    assert first.system_message_index == 0
    assert len(first.prefix_hash) == 64
    assert first.prefix_hash == suffix_changed.prefix_hash
    assert first.prefix_hash == field_order_changed.prefix_hash
    assert first.prefix_hash != system_changed.prefix_hash
    assert first.prefix_hash != tool_changed.prefix_hash


def test_conversation_key_is_stable_secret_free_and_runtime_separated() -> None:
    current = _context(
        "openai",
        "gpt-5.6",
        api_key="first-secret",
        base_url="https://User:Password@EXAMPLE.test/v1/?token=hidden#fragment",
    )
    same_safe_runtime = _context(
        "openai",
        "gpt-5.6",
        api_key="second-secret",
        base_url="https://example.test/v1",
    )
    base = _plan(current)

    assert base.conversation_key == _plan(current).conversation_key
    assert base.conversation_key == _plan(same_safe_runtime).conversation_key
    assert "secret" not in repr(base)
    assert "Password" not in repr(base)
    assert base.conversation_key != _plan(current, session_id="session-b").conversation_key
    assert base.conversation_key != _plan(_context("openai", "gpt-5.4")).conversation_key
    assert base.conversation_key != _plan(_context("xai", "grok-4.5")).conversation_key
    assert base.conversation_key != _plan(
        _context("openai", "gpt-5.6", base_url="https://other.test/v1")
    ).conversation_key


def test_disabled_plan_still_describes_prefix_and_config_defaults_enabled() -> None:
    plan = _plan(_context("openai", "gpt-5.6"), enabled=False)
    assert plan.enabled is False
    assert plan.system_message_index == 0
    assert plan.prefix_hash and plan.conversation_key
    assert DEFAULT_CONFIG["prompt_cache"] == {"enabled": True}


@pytest.mark.parametrize(
    ("provider", "model", "expected"),
    [
        ("openai", "gpt-5.6", PROMPT_CACHE_LAYOUT_OPENAI_SYSTEM),
        ("openai", "gpt-5.4", PROMPT_CACHE_LAYOUT_AUTOMATIC_PREFIX),
        ("anthropic", "claude-sonnet-5", PROMPT_CACHE_LAYOUT_ANTHROPIC_SYSTEM),
        (
            "openrouter",
            "anthropic/claude-sonnet-5",
            PROMPT_CACHE_LAYOUT_OPENROUTER_SYSTEM,
        ),
        (
            "openrouter",
            "qwen/qwen3-max",
            PROMPT_CACHE_LAYOUT_OPENROUTER_SYSTEM,
        ),
        (
            "openrouter",
            "google/gemini-2.5-flash",
            PROMPT_CACHE_LAYOUT_OPENROUTER_GEMINI,
        ),
        (
            "openrouter",
            "google/gemini-3.1-pro-preview-customtools",
            PROMPT_CACHE_LAYOUT_OPENROUTER_GEMINI,
        ),
        ("openrouter", "openai/gpt-5.6", PROMPT_CACHE_LAYOUT_AUTOMATIC_PREFIX),
        ("qwen", "qwen3.7-max", PROMPT_CACHE_LAYOUT_QWEN_SYSTEM),
        ("qwen", "qwen3.7-max-2026-06-08", PROMPT_CACHE_LAYOUT_QWEN_SYSTEM),
        ("qwen-intl", "qwen3.7-plus-us", PROMPT_CACHE_LAYOUT_QWEN_SYSTEM),
        ("qwen", "qwen3.6-35b-a3b", PROMPT_CACHE_LAYOUT_AUTOMATIC_PREFIX),
        ("qwen", "qwen3.5-27b", PROMPT_CACHE_LAYOUT_AUTOMATIC_PREFIX),
        ("qwen", "qwen3.5-plus-2026-02-15", PROMPT_CACHE_LAYOUT_AUTOMATIC_PREFIX),
        ("qwen", "qwen3.5-flash-2026-02-23", PROMPT_CACHE_LAYOUT_AUTOMATIC_PREFIX),
        ("qwen", "qwen-max", PROMPT_CACHE_LAYOUT_AUTOMATIC_PREFIX),
        ("qwen", "qwen-turbo", PROMPT_CACHE_LAYOUT_AUTOMATIC_PREFIX),
        ("qwen", "unlisted-hosted-model", PROMPT_CACHE_LAYOUT_AUTOMATIC_PREFIX),
        ("google", "gemini-3.5-flash", PROMPT_CACHE_LAYOUT_GEMINI_USER_TAIL),
        ("xai", "grok-4.5", PROMPT_CACHE_LAYOUT_XAI_CONVERSATION),
    ],
)
def test_provider_model_traits_are_the_only_layout_source(
    provider: str,
    model: str,
    expected: str,
) -> None:
    context = _context(provider, model)
    assert context.profile.model_traits(context.model).prompt_cache_layout == expected


def test_openai_eligible_request_maps_key_options_and_ordered_breakpoint() -> None:
    request, plan = _openai_request("openai", "gpt-5.6", enabled=True)
    blocks = request["messages"][0]["content"]

    assert request["prompt_cache_key"] == plan.prefix_hash
    assert request["extra_body"]["prompt_cache_options"] == {"mode": "explicit"}
    assert blocks == [
        {
            "type": "text",
            "text": "stable system",
            "prompt_cache_breakpoint": {"mode": "explicit"},
        },
        {"type": "text", "text": "volatile context"},
    ]
    assert "volatile context" not in repr(plan)


def test_openai_automatic_family_maps_key_without_unsupported_breakpoint_fields() -> None:
    request, plan = _openai_request("openai", "gpt-5.4", enabled=True)
    assert request["prompt_cache_key"] == plan.prefix_hash
    assert request["messages"][:2] == [
        {"role": "system", "content": "stable system"},
        {"role": "system", "content": "volatile context"},
    ]
    assert "prompt_cache_options" not in request.get("extra_body", {})
    assert "prompt_cache_breakpoint" not in repr(request["messages"])


@pytest.mark.parametrize(
    ("provider", "model"),
    [
        ("openai", "gpt-5.6"),
        ("openrouter", "anthropic/claude-sonnet-5"),
        ("qwen", "qwen3.7-max"),
        ("xai", "grok-4.5"),
    ],
)
def test_disabled_flag_emits_no_explicit_cache_or_routing_fields(
    provider: str,
    model: str,
) -> None:
    request, _plan_value = _openai_request(provider, model, enabled=False)
    assert request["messages"][:2] == [
        {"role": "system", "content": "stable system"},
        {"role": "system", "content": "volatile context"},
    ]
    assert "prompt_cache_key" not in request
    assert "prompt_cache_options" not in request.get("extra_body", {})
    assert "session_id" not in request.get("extra_body", {})
    assert "x-grok-conv-id" not in {
        str(name).casefold() for name in request.get("extra_headers", {})
    }
    assert "cache_control" not in repr(request["messages"])
    assert "prompt_cache_breakpoint" not in repr(request["messages"])


@pytest.mark.parametrize(
    ("provider", "model", "layout"),
    [
        ("openrouter", "anthropic/claude-sonnet-5", "openrouter"),
        ("openrouter", "qwen/qwen3-max", "openrouter"),
        ("qwen", "qwen3.7-max", "qwen"),
    ],
)
def test_openrouter_and_qwen_map_system_cache_control_after_stable_prefix(
    provider: str,
    model: str,
    layout: str,
) -> None:
    request, plan = _openai_request(provider, model, enabled=True)
    blocks = request["messages"][0]["content"]
    assert blocks == [
        {
            "type": "text",
            "text": "stable system",
            "cache_control": {"type": "ephemeral"},
        },
        {"type": "text", "text": "volatile context"},
    ]
    if layout == "openrouter":
        assert request["extra_body"]["session_id"] == plan.conversation_key
    else:
        assert "session_id" not in request.get("extra_body", {})


def test_openrouter_non_anthropic_target_keeps_automatic_shape_but_routes_session() -> None:
    request, plan = _openai_request("openrouter", "openai/gpt-5.6", enabled=True)
    assert request["messages"][:2] == [
        {"role": "system", "content": "stable system"},
        {"role": "system", "content": "volatile context"},
    ]
    assert request["extra_body"]["session_id"] == plan.conversation_key
    assert "cache_control" not in repr(request["messages"])


@pytest.mark.parametrize(
    "model",
    [
        "google/gemini-2.5-flash",
        "google/gemini-3.1-pro-preview-customtools",
    ],
)
def test_openrouter_gemini_marks_stable_system_and_moves_dynamic_to_user_tail(
    model: str,
) -> None:
    request, plan = _openai_request(
        "openrouter",
        model,
        enabled=True,
    )
    assert request["messages"][:3] == [
        {
            "role": "system",
            "content": [{
                "type": "text",
                "text": "stable system",
                "cache_control": {"type": "ephemeral"},
            }],
        },
        {
            "role": "user",
            "content": "[M-Claw dynamic system context]\nvolatile context",
        },
        {"role": "user", "content": "question"},
    ]
    assert request["extra_body"]["session_id"] == plan.conversation_key


def test_gemini_implicit_cache_moves_dynamic_out_of_immutable_system_instruction() -> None:
    request, _plan_value = _openai_request(
        "google",
        "gemini-3.5-flash",
        enabled=False,
    )
    assert request["messages"][:3] == [
        {"role": "system", "content": "stable system"},
        {
            "role": "user",
            "content": "[M-Claw dynamic system context]\nvolatile context",
        },
        {"role": "user", "content": "question"},
    ]


def test_xai_maps_stable_conversation_header_without_changing_automatic_prefix() -> None:
    request, plan = _openai_request("xai", "grok-4.5", enabled=True)
    assert request["extra_headers"]["x-grok-conv-id"] == plan.conversation_key
    assert request["messages"][:2] == [
        {"role": "system", "content": "stable system"},
        {"role": "system", "content": "volatile context"},
    ]


def test_anthropic_orders_stable_cache_block_before_dynamic_context_and_parses_usage() -> None:
    context = _context("anthropic", "claude-3-5-sonnet")
    messages = _messages()
    tools = _tools()
    plan = _plan(context, messages=messages, tools=tools)
    api = _CapturingAnthropicAPI()
    result = AnthropicMessagesTransport(
        context,
        SimpleNamespace(messages=api),
    ).call(
        messages=messages,
        tools=tools,
        options=ModelCallOptions(
            dynamic_system_context="volatile context",
            cache_plan=plan,
        ),
    )

    assert api.calls[0]["system"] == [
        {
            "type": "text",
            "text": "stable system",
            "cache_control": {"type": "ephemeral"},
        },
        {"type": "text", "text": "volatile context"},
    ]
    assert result.usage is not None
    assert result.usage.input_tokens == 9
    assert result.usage.cache_write_tokens == 3
    assert result.usage.cache_read_tokens == 4


def test_anthropic_disabled_flag_preserves_order_without_cache_control() -> None:
    context = _context("anthropic", "claude-3-5-sonnet")
    messages = _messages()
    api = _CapturingAnthropicAPI()
    AnthropicMessagesTransport(context, SimpleNamespace(messages=api)).call(
        messages=messages,
        tools=[],
        options=ModelCallOptions(
            dynamic_system_context="volatile context",
            cache_plan=_plan(context, messages=messages, tools=[], enabled=False),
        ),
    )
    assert api.calls[0]["system"] == [
        {"type": "text", "text": "stable system"},
        {"type": "text", "text": "volatile context"},
    ]


@pytest.mark.parametrize(
    ("provider", "model"),
    [
        ("deepseek", "deepseek-v4-pro"),
        ("minimax", "MiniMax-M3"),
        ("zhipu", "glm-5.2"),
        ("google", "gemini-3.5-flash"),
    ],
)
def test_automatic_cache_profiles_do_not_gain_explicit_request_fields(
    provider: str,
    model: str,
) -> None:
    context = _context(provider, model)
    profile = context.profile
    base = {"messages": _messages(), "tools": _tools()}
    original = deepcopy(base)
    without_plan = profile.prepare_request(base, context, ModelCallOptions())
    with_plan = profile.prepare_request(
        base,
        context,
        ModelCallOptions(cache_plan=_plan(context)),
    )
    assert base == original
    assert with_plan == without_plan


@pytest.mark.parametrize(
    ("provider", "model"),
    [
        ("openai", "gpt-5.4"),
        ("deepseek", "deepseek-v4-pro"),
        ("moonshot", "kimi-k2.6"),
        ("minimax", "MiniMax-M3"),
        ("zhipu", "glm-5.2"),
        ("qwen", "qwen3.6-35b-a3b"),
        ("openrouter", "openai/gpt-5.6"),
        ("xai", "grok-4.5"),
    ],
)
def test_known_automatic_cache_profiles_keep_stable_system_message(
    provider: str,
    model: str,
) -> None:
    request, _plan_value = _openai_request(provider, model, enabled=False)
    assert request["messages"][:2] == [
        {"role": "system", "content": "stable system"},
        {"role": "system", "content": "volatile context"},
    ]


def test_dynamic_system_insertion_keeps_reasoning_replay_on_original_message() -> None:
    context = _context("deepseek", "deepseek-v4-pro")
    trace = ReasoningTrace(
        text="private chain",
        provider="deepseek",
        model="deepseek-v4-pro",
        api_mode=context.api_mode,
        format="reasoning_content",
        payload="private chain",
    )
    messages = [
        {"role": "system", "content": "stable system"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "call-1",
                "type": "function",
                "function": {"name": "lookup", "arguments": "{}"},
            }],
            **trace.to_message_fields(),
        },
    ]
    client = _CapturingOpenAIClient()
    OpenAIChatCompletionsTransport(context, client).call(
        messages=messages,
        tools=_tools(),
        options=ModelCallOptions(
            dynamic_system_context="volatile context",
            cache_plan=_plan(context, messages=messages),
        ),
    )

    prepared = client.completions.calls[0]["messages"]
    assert prepared[0] == {"role": "system", "content": "stable system"}
    assert prepared[1] == {"role": "system", "content": "volatile context"}
    assert prepared[2]["role"] == "assistant"
    assert prepared[2]["reasoning_content"] == "private chain"
    assert "reasoning_content" not in prepared[1]


def test_kimi_maps_stable_session_cache_key_only_when_enabled() -> None:
    enabled, plan = _openai_request("moonshot", "kimi-k2.7-code", enabled=True)
    disabled, _disabled_plan = _openai_request(
        "moonshot",
        "kimi-k2.7-code",
        enabled=False,
    )
    assert enabled["prompt_cache_key"] == plan.conversation_key
    assert "prompt_cache_key" not in disabled


def test_cache_usage_fields_are_actual_only_even_when_request_cache_is_disabled() -> None:
    openai_context = _context("openai", "gpt-5.6")
    reported = openai_context.profile.parse_usage(
        {
            "prompt_tokens": 12,
            "completion_tokens": 4,
            "prompt_tokens_details": {
                "cached_tokens": 7,
                "cache_write_tokens": 3,
            },
        },
        openai_context,
        source="turn",
    )
    absent = openai_context.profile.parse_usage(
        {"prompt_tokens": 12, "completion_tokens": 4},
        openai_context,
        source="turn",
    )
    assert reported is not None
    assert reported.cache_read_tokens == 7
    assert reported.cache_write_tokens == 3
    assert absent is not None
    assert absent.cache_read_tokens is None
    assert absent.cache_write_tokens is None

    qwen_context = _context("qwen", "qwen3.7-max")
    qwen_usage = qwen_context.profile.parse_usage(
        {
            "prompt_tokens": 20,
            "completion_tokens": 2,
            "prompt_tokens_details": {
                "cached_tokens": 8,
                "cache_creation_input_tokens": 11,
            },
        },
        qwen_context,
        source="turn",
    )
    assert qwen_usage is not None
    assert qwen_usage.cache_read_tokens == 8
    assert qwen_usage.cache_write_tokens == 11


def test_core_keeps_extra_and_recalled_memory_dynamic_and_outside_prompt_epoch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context("openai", "gpt-5.6")

    class CapturingTransport:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def call(self, **kwargs):
            self.calls.append(kwargs)
            return ModelCallResult(
                content="done",
                tool_calls=None,
                finish_reason="stop",
                reasoning=None,
                usage=None,
                was_streamed=False,
                provider=context.provider,
                model=context.model,
            )

    class RecalledMemory:
        def prefetch_all(self, *_args, **_kwargs):
            return "recalled memory"

    transport = CapturingTransport()
    monkeypatch.setattr("mclaw.agent.core.create_transport", lambda _context: transport)
    monkeypatch.setattr(MClaw, "_discover_tools", lambda self: None)
    agent = MClaw(
        provider_runtime=context,
        system_prompt="stable system",
        skip_memory=True,
        config={
            "compression": {"enabled": False},
            "prompt_cache": {"enabled": True},
        },
    )
    agent._memory_manager = RecalledMemory()
    agent.tools = _tools()

    agent.run_conversation(
        "question",
        extra_system="extra system",
        advance_background_review=False,
    )

    call = transport.calls[0]
    options = call["options"]
    assert call["messages"][0] == {"role": "system", "content": "stable system"}
    assert options.dynamic_system_context == "extra system\n\nrecalled memory"
    assert options.cache_plan == _plan(
        context,
        messages=call["messages"],
        tools=call["tools"],
        session_id=agent.session_id,
    )
    assert "extra system" not in repr(options.cache_plan)
    assert "recalled memory" not in repr(options.cache_plan)


def test_core_config_disables_runtime_cache_mapping_intent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context("openai", "gpt-5.6")

    class CapturingTransport:
        def __init__(self) -> None:
            self.options: list[ModelCallOptions] = []

        def call(self, **kwargs):
            self.options.append(kwargs["options"])
            return ModelCallResult(
                content="done",
                tool_calls=None,
                finish_reason="stop",
                reasoning=None,
                usage=None,
                was_streamed=False,
                provider=context.provider,
                model=context.model,
            )

    transport = CapturingTransport()
    monkeypatch.setattr("mclaw.agent.core.create_transport", lambda _context: transport)
    monkeypatch.setattr(MClaw, "_discover_tools", lambda self: None)
    agent = MClaw(
        provider_runtime=context,
        system_prompt="stable system",
        skip_memory=True,
        config={
            "compression": {"enabled": False},
            "prompt_cache": {"enabled": False},
        },
    )

    agent.run_conversation("question", advance_background_review=False)

    assert len(transport.options) == 1
    assert transport.options[0].cache_plan is not None
    assert transport.options[0].cache_plan.enabled is False
    assert transport.options[0].cache_plan.prefix_hash
    assert transport.options[0].cache_plan.conversation_key


def test_successful_skill_write_rebuilds_system_and_starts_new_prompt_epoch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context("openai", "gpt-5.6")
    monkeypatch.setattr("mclaw.agent.core.create_transport", lambda _context: object())
    monkeypatch.setattr(MClaw, "_discover_tools", lambda self: None)
    monkeypatch.setattr(
        "mclaw.tools.dispatch.handle_function_calls",
        lambda **_kwargs: [
            '{"success":true,"action":"create_scaffold","name":"new-skill"}'
        ],
    )
    agent = MClaw(
        provider_runtime=context,
        system_prompt="",
        skip_memory=True,
        config={
            "compression": {"enabled": False},
            "checkpoints": {"enabled": False},
        },
    )
    agent.valid_tool_names = {"skill_manage"}
    agent.tools = _tools()
    agent._build_system_prompt = lambda **_kwargs: "system with new skill"  # type: ignore[method-assign]
    messages = _messages(system="system before skill")
    before = _plan(context, messages=messages, tools=agent.tools)

    agent._execute_tool_calls(
        [{
            "id": "skill-1",
            "type": "function",
            "function": {
                "name": "skill_manage",
                "arguments": '{"action":"create_scaffold","name":"new-skill"}',
            },
        }],
        messages,
    )

    after = _plan(context, messages=messages, tools=agent.tools)
    assert messages[0] == {"role": "system", "content": "system with new skill"}
    assert agent._skills_changed_in_turn is True
    assert before.prefix_hash != after.prefix_hash


def test_successful_memory_write_rebuilds_system_and_starts_new_prompt_epoch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context("openai", "gpt-5.6")
    monkeypatch.setattr("mclaw.agent.core.create_transport", lambda _context: object())
    monkeypatch.setattr(MClaw, "_discover_tools", lambda self: None)
    monkeypatch.setattr(
        "mclaw.tools.dispatch.handle_function_calls",
        lambda **_kwargs: ['{"success":true}'],
    )
    agent = MClaw(
        provider_runtime=context,
        system_prompt="",
        skip_memory=True,
        config={
            "compression": {"enabled": False},
            "checkpoints": {"enabled": False},
        },
    )
    agent._memory_review_round = 0
    agent.valid_tool_names = {"memory_add"}
    agent.tools = _tools()
    agent._build_system_prompt = lambda **_kwargs: "system with new memory"  # type: ignore[method-assign]
    messages = _messages(system="system before memory")
    before = _plan(context, messages=messages, tools=agent.tools)

    agent._execute_tool_calls(
        [{
            "id": "memory-1",
            "type": "function",
            "function": {
                "name": "memory_add",
                "arguments": '{"target":"memory","content":"remember"}',
            },
        }],
        messages,
    )

    after = _plan(context, messages=messages, tools=agent.tools)
    assert messages[0] == {"role": "system", "content": "system with new memory"}
    assert agent._memory_changed_in_turn is True
    assert before.prefix_hash != after.prefix_hash


def test_out_of_band_skill_change_refreshes_epoch_before_next_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context("openai", "gpt-5.6")

    class CapturingTransport:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def call(self, **kwargs):
            self.calls.append(kwargs)
            return ModelCallResult(
                content="done",
                tool_calls=None,
                finish_reason="stop",
                reasoning=None,
                usage=None,
                was_streamed=False,
                provider=context.provider,
                model=context.model,
            )

    transport = CapturingTransport()
    monkeypatch.setattr("mclaw.agent.core.create_transport", lambda _context: transport)
    monkeypatch.setattr(MClaw, "_discover_tools", lambda self: None)
    agent = MClaw(
        provider_runtime=context,
        system_prompt="",
        skip_memory=True,
        config={"compression": {"enabled": False}},
    )
    agent.messages = _messages(system="stale skill prompt")
    agent._build_system_prompt = lambda **_kwargs: "fresh skill prompt"  # type: ignore[method-assign]
    agent.mark_prompt_epoch_dirty()

    agent.run_conversation(
        "next question",
        conversation_history=agent.messages,
        advance_background_review=False,
    )

    assert transport.calls[0]["messages"][0]["content"] == "fresh skill prompt"
    assert agent._prompt_epoch_dirty is False
