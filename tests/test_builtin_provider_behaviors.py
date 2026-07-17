# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from copy import deepcopy

import pytest

from mclaw.agent.prompt_cache import PromptCachePlan
from mclaw.agent.transports.base import ModelCallOptions, ReasoningTrace
from mclaw.providers.anthropic import AnthropicProfile
from mclaw.providers.deepseek import DeepSeekProfile
from mclaw.providers.gemini import GoogleGeminiProfile
from mclaw.providers.generic import (
    GenericAnthropicCompatibleProfile,
    GenericOpenAICompatibleProfile,
)
from mclaw.providers.minimax import MiniMaxProfile
from mclaw.providers.kimi import MoonshotKimiProfile
from mclaw.providers.openai import OpenAIProfile
from mclaw.providers.openrouter import OpenRouterProfile
from mclaw.providers.qwen import QwenProfile
from mclaw.providers.registry import PROVIDER_REGISTRY
from mclaw.providers.runtime import ProviderRuntimeContext
from mclaw.providers.xai import XAIProfile
from mclaw.providers.xiaomi import XiaomiMiMoProfile
from mclaw.providers.zhipu import ZhipuGLMProfile


def _context(profile, model: str, *, reasoning_config=None) -> ProviderRuntimeContext:
    return ProviderRuntimeContext(
        profile=profile,
        model=model,
        api_key="test-key",
        base_url="https://api.example.test/v1",
        reasoning_config=reasoning_config,
    )


def test_generic_openai_profile_uses_stable_protocol_request_shape() -> None:
    profile = GenericOpenAICompatibleProfile(
        name="generic",
        display_name="Generic",
        provider_kind="host",
    )
    context = _context(profile, "hosted/model")
    base = {
        "model": "wrong-model",
        "messages": [
            {"role": "system", "content": "stable"},
            {
                "role": "assistant",
                "content": "visible",
                "reasoning": "private",
                "reasoning_details": {"schema_version": 1, "payload": {"secret": False}},
            },
        ],
        "tools": [{"type": "function", "function": {"name": "lookup"}}],
    }
    original = deepcopy(base)

    prepared = profile.prepare_request(
        base,
        context,
        ModelCallOptions(
            max_output_tokens=321,
            temperature=0.25,
            dynamic_system_context="dynamic",
        ),
    )

    assert base == original
    assert prepared["model"] == "hosted/model"
    assert prepared["max_tokens"] == 321
    assert "max_completion_tokens" not in prepared
    assert prepared["temperature"] == 0.25
    assert prepared["messages"][0]["content"] == "stable\n\ndynamic"
    assert "reasoning" not in prepared["messages"][1]
    assert "reasoning_details" not in prepared["messages"][1]
    assert prepared["tools"] == base["tools"]


def test_openai_profile_selects_modern_output_field_and_stream_usage() -> None:
    profile = OpenAIProfile(name="openai", display_name="OpenAI")
    context = _context(profile, "gpt-5.6")

    traits = profile.model_traits(context.model)
    prepared = profile.prepare_request(
        {"messages": [{"role": "user", "content": "hello"}]},
        context,
        ModelCallOptions(max_output_tokens=1_024),
    )

    assert traits.output_token_param == "max_completion_tokens"
    assert traits.include_stream_usage_option is True
    assert traits.reasoning_modes == ("low", "medium", "high", "xhigh", "max")
    assert traits.max_output_tokens == 128_000
    assert prepared["max_completion_tokens"] == 1_024
    assert "max_tokens" not in prepared


def test_openai_profile_maps_enabled_disabled_and_legacy_reasoning() -> None:
    profile = OpenAIProfile(name="openai", display_name="OpenAI")
    enabled = profile.prepare_request(
        {"messages": [{"role": "user", "content": "hello"}]},
        _context(profile, "gpt-5.6", reasoning_config={"enabled": True, "effort": "high"}),
        ModelCallOptions(),
    )
    disabled = profile.prepare_request(
        {"messages": [{"role": "user", "content": "hello"}]},
        _context(profile, "gpt-5.6", reasoning_config={"enabled": False}),
        ModelCallOptions(),
    )
    legacy = profile.prepare_request(
        {"messages": [{"role": "user", "content": "hello"}], "reasoning_effort": "high"},
        _context(profile, "gpt-4o", reasoning_config={"enabled": False}),
        ModelCallOptions(),
    )

    assert enabled["reasoning_effort"] == "high"
    assert disabled["reasoning_effort"] == "none"
    assert "reasoning_effort" not in legacy
    assert profile.model_traits("gpt-4o").reasoning_modes == ()


@pytest.mark.parametrize(
    ("model", "modes", "disabled_value"),
    [
        ("gpt-5.6", ("low", "medium", "high", "xhigh", "max"), "none"),
        ("gpt-5.4", ("low", "medium", "high", "xhigh"), "none"),
        ("gpt-5.1", ("low", "medium", "high"), "none"),
        ("gpt-5", ("minimal", "low", "medium", "high"), None),
        ("o4-mini", ("low", "medium", "high"), None),
    ],
)
def test_openai_profile_uses_exact_family_reasoning_policy(
    model: str,
    modes: tuple[str, ...],
    disabled_value: str | None,
) -> None:
    profile = OpenAIProfile(name="openai", display_name="OpenAI")
    traits = profile.model_traits(model)
    disabled = profile.prepare_request(
        {"messages": [{"role": "user", "content": "hello"}]},
        _context(profile, model, reasoning_config={"enabled": False}),
        ModelCallOptions(),
    )

    assert traits.reasoning_modes == modes
    if disabled_value is None:
        assert "reasoning_effort" not in disabled
    else:
        assert disabled["reasoning_effort"] == disabled_value


def test_openai_latest_family_accepts_max_effort() -> None:
    profile = OpenAIProfile(name="openai", display_name="OpenAI")
    context = _context(
        profile,
        "gpt-5.6",
        reasoning_config={"enabled": True, "effort": "max"},
    )

    prepared = profile.prepare_request(
        {"messages": [{"role": "user", "content": "hello"}]},
        context,
        ModelCallOptions(max_output_tokens=999_999),
    )

    assert prepared["reasoning_effort"] == "max"
    assert prepared["max_completion_tokens"] == 128_000


@pytest.mark.parametrize(
    ("model", "max_output"),
    [
        ("gpt-5.1-chat-latest", 16_384),
        ("gpt-5.2-chat-latest", 16_384),
        ("gpt-5.3-chat-latest", 16_384),
        ("gpt-5-chat-latest", 16_384),
    ],
)
def test_openai_nonstandard_variants_do_not_inherit_reasoning_family(
    model: str,
    max_output: int,
) -> None:
    profile = OpenAIProfile(name="openai", display_name="OpenAI")
    traits = profile.model_traits(model)
    prepared = profile.prepare_request(
        {"messages": [{"role": "user", "content": "hello"}]},
        _context(profile, model, reasoning_config={"enabled": False}),
        ModelCallOptions(),
    )

    assert traits.reasoning_modes == ()
    assert traits.output_token_param == "max_completion_tokens"
    assert traits.max_output_tokens == max_output
    assert prepared["max_completion_tokens"] == max_output
    assert "reasoning_effort" not in prepared


@pytest.mark.parametrize(
    "model",
    (
        "gpt-5-pro",
        "gpt-5.4-pro",
        "o1-pro",
        "o3-pro",
        "gpt-5.1-codex",
        "gpt-5.3-codex-spark",
        "codex-mini-latest",
    ),
)
def test_openai_responses_only_families_fail_closed_in_chat_profile(model: str) -> None:
    profile = OpenAIProfile(name="openai", display_name="OpenAI")
    with pytest.raises(ValueError, match="Responses API"):
        profile.prepare_request(
            {"messages": [{"role": "user", "content": "hello"}]},
            _context(profile, model),
            ModelCallOptions(),
        )


def test_generic_anthropic_profile_orders_system_replays_complete_thinking_and_usage() -> None:
    profile = GenericAnthropicCompatibleProfile(
        name="custom-anthropic",
        display_name="Custom Anthropic",
        api_mode="anthropic_messages",
        auth_scheme="anthropic_x_api_key",
    )
    context = _context(profile, "claude-custom")
    trace = ReasoningTrace(
        text="prior",
        provider=context.provider,
        model=context.model,
        api_mode=context.api_mode,
        format="anthropic_thinking_blocks",
        payload=[
            {"type": "thinking", "thinking": "prior", "signature": "signed"},
            {"type": "thinking", "thinking": "partial", "signature": ""},
            {"type": "redacted_thinking", "data": "opaque"},
        ],
    )
    base = {
        "system": "stable",
        "messages": [{"role": "assistant", "content": "answer", **trace.to_message_fields()}],
        "tools": [{"name": "lookup"}],
    }
    original = deepcopy(base)

    prepared = profile.prepare_request(
        base,
        context,
        ModelCallOptions(
            max_output_tokens=123,
            temperature=0.4,
            dynamic_system_context="dynamic",
        ),
    )
    usage = profile.parse_usage(
        {
            "input_tokens": 10,
            "cache_read_input_tokens": 3,
            "cache_creation_input_tokens": 2,
            "output_tokens": 4,
        },
        context,
        source="turn",
    )

    assert base == original
    assert prepared["system"] == [
        {"type": "text", "text": "stable"},
        {"type": "text", "text": "dynamic"},
    ]
    assert prepared["max_tokens"] == 123
    assert prepared["temperature"] == 0.4
    assert prepared["messages"][0]["content"] == "answer"
    assert "reasoning" not in prepared["messages"][0]
    assert "reasoning_details" not in prepared["messages"][0]
    assert usage is not None
    assert usage.input_tokens == 15
    assert usage.output_tokens == 4
    assert usage.cache_read_tokens == 3
    assert usage.cache_write_tokens == 2


def test_generic_anthropic_profile_replays_only_fully_complete_sequence() -> None:
    profile = GenericAnthropicCompatibleProfile(
        name="custom-anthropic",
        display_name="Custom Anthropic",
        api_mode="anthropic_messages",
        auth_scheme="anthropic_x_api_key",
    )
    context = _context(profile, "claude-custom")
    trace = ReasoningTrace(
        text="prior",
        provider=context.provider,
        model=context.model,
        api_mode=context.api_mode,
        format="anthropic_thinking_blocks",
        payload=[
            {
                "type": "thinking",
                "thinking": "prior",
                "signature": "signed",
                "opaque_future_field": {"version": 2},
            },
            {"type": "redacted_thinking", "data": "opaque"},
        ],
    )

    prepared = profile.prepare_request(
        {"messages": [{"role": "assistant", "content": "answer", **trace.to_message_fields()}]},
        context,
        ModelCallOptions(),
    )

    assert prepared["messages"][0]["content"] == [
        {
            "type": "thinking",
            "thinking": "prior",
            "signature": "signed",
            "opaque_future_field": {"version": 2},
        },
        {"type": "redacted_thinking", "data": "opaque"},
        {"type": "text", "text": "answer"},
    ]


@pytest.mark.parametrize("bad_schema", [True, 1.0, 2])
def test_anthropic_profile_drops_malformed_reasoning_schema(bad_schema: object) -> None:
    profile = AnthropicProfile(
        name="anthropic",
        display_name="Anthropic",
        api_mode="anthropic_messages",
        auth_scheme="anthropic_x_api_key",
    )
    context = _context(profile, "claude-sonnet-5")
    message = {
        "role": "assistant",
        "content": "answer",
        "reasoning": "partial",
        "reasoning_details": {
            "schema_version": bad_schema,
            "provider": "anthropic",
            "model": "claude-sonnet-5",
            "api_mode": "anthropic_messages",
            "format": "anthropic_thinking_blocks",
            "payload": [{"type": "thinking", "thinking": "partial", "signature": "sig"}],
        },
    }

    prepared = profile.prepare_request({"messages": [message]}, context, ModelCallOptions())

    assert prepared["messages"][0] == {"role": "assistant", "content": "answer"}


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("anthropic/claude-fable-5", 128_000),
        ("anthropic/claude-mythos-5", 128_000),
        ("anthropic/claude-mythos-preview", 128_000),
        ("claude-opus-4.8", 128_000),
        ("claude-sonnet-5", 128_000),
        ("claude-haiku-4-5-20251001", 64_000),
        ("claude-sonnet-4-6", 128_000),
    ],
)
def test_anthropic_profile_normalizes_current_models_and_caps_output(
    model: str,
    expected: int,
) -> None:
    profile = AnthropicProfile(name="anthropic", display_name="Anthropic")
    normalized = profile.normalize_model(model)
    context = _context(profile, normalized)

    prepared = profile.prepare_request(
        {"messages": [{"role": "user", "content": "hello"}]},
        context,
        ModelCallOptions(max_output_tokens=999_999),
    )

    assert profile.model_traits(normalized).max_output_tokens == expected
    assert prepared["max_tokens"] == expected


def test_anthropic_current_families_map_effort_thinking_and_sampling_policy() -> None:
    profile = AnthropicProfile(
        name="anthropic",
        display_name="Anthropic",
        api_mode="anthropic_messages",
        auth_scheme="anthropic_x_api_key",
    )
    sonnet_disabled = profile.prepare_request(
        {"messages": [{"role": "user", "content": "hello"}]},
        _context(profile, "claude-sonnet-5", reasoning_config={"enabled": False}),
        ModelCallOptions(temperature=0.2),
    )
    sonnet_enabled = profile.prepare_request(
        {"messages": [{"role": "user", "content": "hello"}]},
        _context(
            profile,
            "claude-sonnet-5",
            reasoning_config={"enabled": True, "effort": "max"},
        ),
        ModelCallOptions(temperature=0.2),
    )
    opus_enabled = profile.prepare_request(
        {"messages": [{"role": "user", "content": "hello"}]},
        _context(
            profile,
            "claude-opus-4-8",
            reasoning_config={"enabled": True, "effort": "xhigh"},
        ),
        ModelCallOptions(temperature=0.2),
    )
    sonnet_46_enabled = profile.prepare_request(
        {"messages": [{"role": "user", "content": "hello"}]},
        _context(
            profile,
            "claude-sonnet-4-6",
            reasoning_config={"enabled": True, "effort": "max"},
        ),
        ModelCallOptions(temperature=0.2),
    )
    mythos_preview = profile.prepare_request(
        {"messages": [{"role": "user", "content": "hello"}]},
        _context(
            profile,
            "claude-mythos-preview",
            reasoning_config={"enabled": True, "effort": "max"},
        ),
        ModelCallOptions(temperature=0.2),
    )

    assert sonnet_disabled["thinking"] == {"type": "disabled"}
    assert "temperature" not in sonnet_disabled
    assert sonnet_enabled["output_config"] == {"effort": "max"}
    assert "thinking" not in sonnet_enabled
    assert "temperature" not in sonnet_enabled
    assert opus_enabled["thinking"] == {"type": "adaptive"}
    assert opus_enabled["output_config"] == {"effort": "xhigh"}
    assert "temperature" not in opus_enabled
    assert sonnet_46_enabled["thinking"] == {"type": "adaptive"}
    assert "temperature" not in sonnet_46_enabled
    assert mythos_preview["output_config"] == {"effort": "max"}
    assert "thinking" not in mythos_preview
    assert "temperature" not in mythos_preview
    assert profile.model_traits("claude-mythos-preview").max_output_tokens == 128_000
    assert profile.model_traits("claude-mythos-preview").reasoning_modes == (
        "low", "medium", "high", "max"
    )
    assert profile.model_traits("claude-sonnet-5").requires_stream is True
    assert profile.model_traits("claude-sonnet-5").reasoning_modes == (
        "low", "medium", "high", "xhigh", "max"
    )
    assert profile.model_traits("claude-sonnet-4-6").reasoning_modes == (
        "low", "medium", "high", "max"
    )


def test_minimax_profile_replays_matching_reasoning_without_mutating_history() -> None:
    profile = MiniMaxProfile(name="minimax", display_name="MiniMax")
    context = _context(profile, "MiniMax-M2.7")
    trace = ReasoningTrace(
        text="thinking",
        provider="minimax",
        model="MiniMax-M2",
        api_mode="chat_completions",
        format="reasoning_details",
        payload=[{"type": "reasoning", "text": "thinking"}],
    )
    message = {"role": "assistant", "content": "answer", **trace.to_message_fields()}
    base = {"messages": [message]}
    original = deepcopy(base)

    prepared = profile.prepare_request(base, context, ModelCallOptions())

    assert base == original
    assert "reasoning_content" not in prepared["messages"][0]
    assert prepared["messages"][0]["reasoning_details"] == trace.payload
    assert prepared["extra_body"] == {"reasoning_split": True}
    assert profile.model_traits(context.model).content_stream_mode == "cumulative"
    assert profile.model_traits(context.model).reasoning_stream_mode == "cumulative"


def test_minimax_profile_drops_foreign_reasoning_envelope() -> None:
    profile = MiniMaxProfile(name="minimax", display_name="MiniMax")
    context = _context(profile, "MiniMax-M2.7")
    trace = ReasoningTrace(
        text="foreign",
        provider="openrouter",
        model="minimax/minimax-m2",
        api_mode="chat_completions",
        format="reasoning_details",
        payload=[{"text": "foreign"}],
    )

    prepared = profile.prepare_request(
        {"messages": [{"role": "assistant", "content": "visible", **trace.to_message_fields()}]},
        context,
        ModelCallOptions(),
    )

    assert "reasoning_content" not in prepared["messages"][0]
    assert "reasoning_details" not in prepared["messages"][0]


@pytest.mark.parametrize("temperature", [0.0, 0.5, 1.0, 2.0])
def test_minimax_profile_accepts_documented_temperature_range(temperature: float) -> None:
    profile = MiniMaxProfile(name="minimax", display_name="MiniMax")
    context = _context(profile, "MiniMax-M2.7")

    prepared = profile.prepare_request(
        {"messages": [{"role": "user", "content": "hello"}]},
        context,
        ModelCallOptions(temperature=temperature),
    )

    assert prepared["temperature"] == temperature


@pytest.mark.parametrize("temperature", [-0.1, 2.0001, float("inf"), float("nan")])
def test_minimax_profile_rejects_out_of_range_temperature(temperature: float) -> None:
    profile = MiniMaxProfile(name="minimax", display_name="MiniMax")
    context = _context(profile, "MiniMax-M2.7")

    with pytest.raises(ValueError, match="range"):
        profile.prepare_request(
            {"messages": [{"role": "user", "content": "hello"}]},
            context,
            ModelCallOptions(temperature=temperature),
        )


def test_minimax_profile_scopes_cumulative_streaming_to_m2_family() -> None:
    profile = MiniMaxProfile(name="minimax", display_name="MiniMax")

    current = profile.model_traits("MiniMax-M2.7")
    latest = profile.model_traits("MiniMax-M3")
    legacy = profile.model_traits("abab6.5-chat")

    assert current.include_stream_usage_option is True
    assert current.content_stream_mode == "cumulative"
    assert current.reasoning_stream_mode == "cumulative"
    assert latest.content_stream_mode == "cumulative"
    assert latest.max_output_tokens == 524_288
    assert latest.reasoning_modes == ("minimal", "low", "medium", "high", "xhigh", "max")
    assert legacy.content_stream_mode == "delta"
    assert legacy.reasoning_stream_mode == "delta"


def test_minimax_m3_maps_reasoning_intent() -> None:
    profile = MiniMaxProfile(name="minimax", display_name="MiniMax")
    enabled = profile.prepare_request(
        {"messages": [{"role": "user", "content": "hello"}]},
        _context(
            profile,
            "MiniMax-M3",
            reasoning_config={"enabled": True, "effort": "max"},
        ),
        ModelCallOptions(max_output_tokens=999_999),
    )
    disabled = profile.prepare_request(
        {"messages": [{"role": "user", "content": "hello"}]},
        _context(profile, "MiniMax-M3", reasoning_config={"enabled": False}),
        ModelCallOptions(),
    )

    assert "thinking" not in enabled
    assert enabled["extra_body"]["thinking"] == {"type": "adaptive"}
    assert enabled["extra_body"]["reasoning_split"] is True
    assert enabled["max_completion_tokens"] == 524_288
    assert "thinking" not in disabled
    assert disabled["extra_body"]["thinking"] == {"type": "disabled"}
    assert disabled["extra_body"]["reasoning_split"] is True


def test_minimax_profile_drops_boolean_schema_envelope() -> None:
    profile = MiniMaxProfile(name="minimax", display_name="MiniMax")
    context = _context(profile, "MiniMax-M2.7")
    message = {
        "role": "assistant",
        "content": "visible",
        "reasoning": "invalid",
        "reasoning_details": {
            "schema_version": True,
            "provider": "minimax",
            "model": "MiniMax-M2.7",
            "api_mode": "chat_completions",
            "format": "reasoning_details",
            "payload": [{"text": "invalid"}],
        },
    }

    prepared = profile.prepare_request({"messages": [message]}, context, ModelCallOptions())

    assert "reasoning_content" not in prepared["messages"][0]
    assert "reasoning_details" not in prepared["messages"][0]


def test_minimax_profile_drops_cross_family_reasoning_replay() -> None:
    profile = MiniMaxProfile(name="minimax", display_name="MiniMax")
    context = _context(profile, "MiniMax-M3")
    trace = ReasoningTrace(
        text="m2 thought",
        provider="minimax",
        model="MiniMax-M2.7",
        api_mode="chat_completions",
        format="reasoning_details",
        payload=[{"text": "m2 thought"}],
    )

    prepared = profile.prepare_request(
        {"messages": [{"role": "assistant", "content": "visible", **trace.to_message_fields()}]},
        context,
        ModelCallOptions(),
    )

    assert "reasoning_details" not in prepared["messages"][0]


def test_openrouter_maps_unified_reasoning_sticky_session_replay_and_usage() -> None:
    profile = OpenRouterProfile(name="openrouter", display_name="OpenRouter")
    context = _context(
        profile,
        "anthropic/claude-sonnet-5",
        reasoning_config={"enabled": True, "effort": "high"},
    )
    native_details = [
        {
            "type": "reasoning.text",
            "text": "prior",
            "signature": "opaque",
            "index": 0,
        }
    ]
    trace = ReasoningTrace(
        text="prior",
        provider="openrouter",
        model="anthropic/claude-sonnet-5",
        api_mode="chat_completions",
        format="reasoning_details",
        payload=[{"field": "reasoning_details", "value": native_details}],
    )
    prepared = profile.prepare_request(
        {
            "messages": [{
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}],
                **trace.to_message_fields(),
            }],
        },
        context,
        ModelCallOptions(
            cache_plan=PromptCachePlan(
                enabled=True,
                conversation_key="session-key",
            )
        ),
    )
    usage = profile.parse_usage(
        {
            "prompt_tokens": 20,
            "completion_tokens": 10,
            "prompt_tokens_details": {"cached_tokens": 8, "cache_write_tokens": 4},
            "completion_tokens_details": {"reasoning_tokens": 6},
        },
        context,
        source="turn",
    )

    assert prepared["model"] == "anthropic/claude-sonnet-5"
    assert prepared["extra_body"]["reasoning"] == {"effort": "high"}
    assert prepared["extra_body"]["session_id"] == "session-key"
    assert prepared["messages"][0]["reasoning_details"] == native_details
    assert usage is not None
    assert usage.to_counter_delta() == {
        "input_tokens": 20,
        "output_tokens": 10,
        "cache_read_tokens": 8,
        "cache_write_tokens": 4,
        "reasoning_tokens": 6,
    }


def test_deepseek_v4_maps_effort_sampling_tool_replay_and_actual_cache_hit() -> None:
    profile = DeepSeekProfile(name="deepseek", display_name="DeepSeek")
    context = _context(
        profile,
        "deepseek-v4-pro",
        reasoning_config={"enabled": True, "effort": "low"},
    )
    trace = ReasoningTrace(
        text="prior thought",
        provider="deepseek",
        model="deepseek-v4-pro",
        api_mode="chat_completions",
        format="reasoning_content",
        payload="prior thought",
    )
    prepared = profile.prepare_request(
        {
            "messages": [{
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}],
                **trace.to_message_fields(),
            }],
            "top_p": 0.7,
        },
        context,
        ModelCallOptions(temperature=0.2),
    )
    usage = profile.parse_usage(
        {
            "prompt_tokens": 12,
            "completion_tokens": 5,
            "prompt_cache_hit_tokens": 7,
            "prompt_cache_miss_tokens": 5,
        },
        context,
        source="turn",
    )

    assert profile.model_traits("deepseek-v4-pro").max_output_tokens == 384_000
    assert prepared["max_tokens"] == 384_000
    assert prepared["extra_body"]["thinking"] == {"type": "enabled"}
    assert prepared["reasoning_effort"] == "high"
    assert "temperature" not in prepared and "top_p" not in prepared
    assert prepared["messages"][0]["reasoning_content"] == "prior thought"
    assert usage is not None
    assert usage.cache_read_tokens == 7
    assert usage.cache_write_tokens is None


def test_deepseek_legacy_aliases_keep_thinking_and_non_thinking_distinct() -> None:
    profile = DeepSeekProfile(name="deepseek", display_name="DeepSeek")
    assert profile.model_traits("deepseek-reasoner").reasoning_modes
    assert profile.model_traits("deepseek-chat").reasoning_modes == ()
    prepared = profile.prepare_request(
        {"messages": [{"role": "user", "content": "hello"}]},
        _context(profile, "deepseek-chat", reasoning_config={"enabled": False}),
        ModelCallOptions(temperature=0.4),
    )
    assert prepared["extra_body"]["thinking"] == {"type": "disabled"}
    assert prepared["temperature"] == 0.4


def test_kimi_current_families_apply_stream_thinking_and_preserved_replay() -> None:
    profile = MoonshotKimiProfile(name="moonshot", display_name="Moonshot")
    trace = ReasoningTrace(
        text="preserved",
        provider="moonshot",
        model="kimi-k2.7-code",
        api_mode="chat_completions",
        format="reasoning_content",
        payload="preserved",
    )
    code = profile.prepare_request(
        {"messages": [{"role": "assistant", "content": "", **trace.to_message_fields()}]},
        _context(profile, "kimi-k2.7-code", reasoning_config={"enabled": False}),
        ModelCallOptions(temperature=0.1),
    )
    disabled = profile.prepare_request(
        {"messages": [{"role": "user", "content": "hello"}]},
        _context(profile, "kimi-k2.6", reasoning_config={"enabled": False}),
        ModelCallOptions(temperature=0.1),
    )
    usage = profile.parse_usage(
        {"prompt_tokens": 9, "completion_tokens": 3, "cached_tokens": 4},
        _context(profile, "kimi-k2.6"),
        source="turn",
    )

    traits = profile.model_traits("kimi-k2.7-code")
    assert traits.requires_stream is False
    assert traits.include_stream_usage_option is True
    assert traits.output_token_param == "max_completion_tokens"
    assert traits.max_output_tokens == 32_768
    assert "temperature" not in code
    assert "thinking" not in code.get("extra_body", {})
    assert code["messages"][0]["reasoning_content"] == "preserved"
    assert disabled["extra_body"]["thinking"] == {"type": "disabled"}
    assert usage is not None and usage.cache_read_tokens == 4


def test_glm_52_maps_effort_and_preserved_thinking_without_guessing_context() -> None:
    profile = ZhipuGLMProfile(name="zhipu", display_name="Zhipu")
    trace = ReasoningTrace(
        text="prior",
        provider="zhipu",
        model="glm-5.2",
        api_mode="chat_completions",
        format="reasoning_content",
        payload="prior",
    )
    high = profile.prepare_request(
        {"messages": [{"role": "assistant", "content": "", **trace.to_message_fields()}]},
        _context(profile, "glm-5.2", reasoning_config={"enabled": True, "effort": "xhigh"}),
        ModelCallOptions(),
    )
    minimal = profile.prepare_request(
        {"messages": [{"role": "assistant", "content": "", **trace.to_message_fields()}]},
        _context(profile, "glm-5.2", reasoning_config={"enabled": True, "effort": "minimal"}),
        ModelCallOptions(),
    )
    usage = profile.parse_usage(
        {
            "prompt_tokens": 11,
            "completion_tokens": 2,
            "prompt_tokens_details": {"cached_tokens": 5},
        },
        _context(profile, "glm-5.2"),
        source="turn",
    )

    assert profile.model_traits("glm-5.2").max_output_tokens == 131_072
    assert high["reasoning_effort"] == "max"
    assert high["extra_body"]["thinking"] == {"type": "enabled", "clear_thinking": False}
    assert high["messages"][0]["reasoning_content"] == "prior"
    assert minimal["reasoning_effort"] == "minimal"
    assert minimal["extra_body"]["thinking"] == {"type": "disabled"}
    assert "reasoning_content" not in minimal["messages"][0]
    assert usage is not None and usage.cache_read_tokens == 5


def test_gemini_current_effort_disable_signature_replay_and_local_usage() -> None:
    profile = GoogleGeminiProfile(name="google", display_name="Google")
    signature = {"google": {"thought_signature": "opaque-signature"}}
    trace = ReasoningTrace(
        text="summary",
        provider="google",
        model="gemini-3.5-flash",
        api_mode="chat_completions",
        format="gemini_thought_signature",
        payload=[{
            "field": "tool_calls.extra_content",
            "index": 0,
            "value": signature,
        }],
    )
    prepared = profile.prepare_request(
        {"messages": [{
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}],
            **trace.to_message_fields(),
        }]},
        _context(profile, "gemini-3.5-flash", reasoning_config={"enabled": True, "effort": "high"}),
        ModelCallOptions(),
    )
    disabled = profile.prepare_request(
        {"messages": [{"role": "user", "content": "hello"}]},
        _context(profile, "gemini-2.5-flash", reasoning_config={"enabled": False}),
        ModelCallOptions(),
    )
    usage = profile.parse_usage(
        {
            "promptTokenCount": 10,
            "candidatesTokenCount": 4,
            "totalTokenCount": 17,
            "cachedContentTokenCount": 3,
            "thoughtsTokenCount": 3,
        },
        _context(profile, "gemini-3.5-flash"),
        source="turn",
    )

    assert prepared["reasoning_effort"] == "high"
    assert prepared["messages"][0]["tool_calls"][0]["extra_content"] == signature
    assert disabled["reasoning_effort"] == "none"
    assert usage is not None
    assert usage.to_counter_delta() == {
        "input_tokens": 10,
        "output_tokens": 7,
        "cache_read_tokens": 3,
        "reasoning_tokens": 3,
    }
    assert usage.total_tokens == 17
    assert usage.input_tokens + usage.output_tokens == usage.total_tokens


def test_qwen_37_preserves_explicit_budget_reasoning_and_required_stream() -> None:
    profile = QwenProfile(name="qwen", display_name="Qwen")
    trace = ReasoningTrace(
        text="prior",
        provider="qwen",
        model="qwen3.7-plus",
        api_mode="chat_completions",
        format="reasoning_content",
        payload="prior",
    )
    prepared = profile.prepare_request(
        {
            "messages": [{"role": "assistant", "content": "", **trace.to_message_fields()}],
            "extra_body": {"thinking_budget": 2048},
        },
        _context(profile, "qwen3.7-plus", reasoning_config={"enabled": True, "effort": "high"}),
        ModelCallOptions(max_output_tokens=100_000),
    )
    required = profile.prepare_request(
        {"messages": [{"role": "user", "content": "hello"}]},
        _context(
            profile,
            "qwen3.7-max-preview",
            reasoning_config={"enabled": False},
        ),
        ModelCallOptions(),
    )
    usage = profile.parse_usage(
        {
            "prompt_tokens": 30,
            "completion_tokens": 9,
            "prompt_tokens_details": {
                "cached_tokens": 12,
                "cache_creation": {"cache_creation_input_tokens": 7},
            },
        },
        _context(profile, "qwen3.7-plus"),
        source="turn",
    )

    traits = profile.model_traits("qwen3.7-plus")
    assert traits.requires_stream is True and traits.include_stream_usage_option is True
    assert traits.output_token_param == "max_completion_tokens"
    assert prepared["max_completion_tokens"] == 65_536
    assert prepared["extra_body"] == {
        "thinking_budget": 2048,
        "enable_thinking": True,
        "preserve_thinking": True,
    }
    assert prepared["messages"][0]["reasoning_content"] == "prior"
    assert "enable_thinking" not in required.get("extra_body", {})
    assert required.get("extra_body", {}).get("preserve_thinking") is True
    assert usage is not None
    assert usage.cache_read_tokens == 12 and usage.cache_write_tokens == 7


def test_mimo_v25_enforces_thinking_temperature_tool_and_replay_policy() -> None:
    profile = XiaomiMiMoProfile(name="xiaomi", display_name="MiMo")
    trace = ReasoningTrace(
        text="prior",
        provider="xiaomi",
        model="mimo-v2.5-pro",
        api_mode="chat_completions",
        format="reasoning_content",
        payload="prior",
    )
    prepared = profile.prepare_request(
        {
            "messages": [{
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}],
                **trace.to_message_fields(),
            }],
            "tools": [{"type": "function", "function": {"name": "lookup"}}],
        },
        _context(profile, "mimo-v2.5-pro", reasoning_config={"enabled": True, "effort": "high"}),
        ModelCallOptions(temperature=0.2),
    )
    disabled = profile.prepare_request(
        {"messages": [{"role": "user", "content": "hello"}]},
        _context(profile, "mimo-v2.5", reasoning_config={"enabled": False}),
        ModelCallOptions(temperature=0.4),
    )
    usage = profile.parse_usage(
        {
            "prompt_tokens": 7,
            "completion_tokens": 5,
            "completion_tokens_details": {"reasoning_tokens": 3},
        },
        _context(profile, "mimo-v2.5-pro"),
        source="turn",
    )

    assert prepared["max_completion_tokens"] == 131_072
    assert profile.model_traits("mimo-v2.5").max_output_tokens == 131_072
    assert prepared["extra_body"]["thinking"] == {"type": "enabled"}
    assert "temperature" not in prepared
    assert prepared["tool_choice"] == "auto"
    assert prepared["messages"][0]["reasoning_content"] == "prior"
    assert disabled["temperature"] == 0.4
    assert disabled["extra_body"]["thinking"] == {"type": "disabled"}
    assert usage is not None and usage.reasoning_tokens == 3


def test_xai_current_chat_families_filter_parameters_route_session_and_usage() -> None:
    profile = XAIProfile(name="xai", display_name="xAI")
    context = _context(
        profile,
        "grok-4.5",
        reasoning_config={"enabled": True, "effort": "high"},
    )
    prepared = profile.prepare_request(
        {
            "messages": [{"role": "user", "content": "hello"}],
            "presence_penalty": 0.5,
            "frequency_penalty": 0.5,
            "stop": ["done"],
        },
        context,
        ModelCallOptions(
            cache_plan=PromptCachePlan(enabled=True, conversation_key="conversation-key")
        ),
    )
    legacy_disabled = profile.prepare_request(
        {"messages": [{"role": "user", "content": "hello"}]},
        _context(profile, "grok-4.3", reasoning_config={"enabled": False}),
        ModelCallOptions(),
    )
    usage = profile.parse_usage(
        {
            "prompt_tokens": 13,
            "completion_tokens": 7,
            "prompt_tokens_details": {"cached_tokens": 6},
            "completion_tokens_details": {"reasoning_tokens": 4},
        },
        context,
        source="turn",
    )

    assert prepared["reasoning_effort"] == "high"
    assert all(name not in prepared for name in ("presence_penalty", "frequency_penalty", "stop"))
    assert prepared["extra_headers"]["x-grok-conv-id"] == "conversation-key"
    assert legacy_disabled["reasoning_effort"] == "none"
    assert usage is not None
    assert usage.cache_read_tokens == 6 and usage.reasoning_tokens == 4
    with pytest.raises(ValueError, match="Responses API"):
        profile.prepare_request(
            {"messages": [{"role": "user", "content": "hello"}]},
            _context(profile, "grok-4.20-multi-agent"),
            ModelCallOptions(),
        )


@pytest.mark.parametrize(
    "provider",
    (
        "baidu", "tencent", "groq", "fireworks", "deepinfra", "mistral",
        "microsoft", "cohere", "amazon", "together", "perplexity", "meta",
        "yi", "stepfun", "baichuan", "doubao", "siliconflow",
    ),
)
def test_every_generic_provider_uses_protocol_request_and_usage_shape(provider: str) -> None:
    profile = PROVIDER_REGISTRY[provider]
    assert isinstance(profile, GenericOpenAICompatibleProfile)
    model = profile.fallback_models[0] if profile.fallback_models else f"{provider}-model"
    context = _context(profile, model)
    prepared = profile.prepare_request(
        {"messages": [{"role": "user", "content": "hello"}]},
        context,
        ModelCallOptions(max_output_tokens=321, temperature=0.3),
    )
    usage = profile.parse_usage(
        {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
        context,
        source="turn",
    )

    assert prepared["model"] == model
    assert prepared["max_tokens"] == 321
    assert prepared["temperature"] == 0.3
    assert usage is not None
    assert (usage.provider, usage.model, usage.source) == (provider, model, "turn")
    assert usage.to_counter_delta() == {"input_tokens": 5, "output_tokens": 2}
