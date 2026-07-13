# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Standard protocol provider profiles."""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Any, Callable, Mapping

from mclaw.agent.transports.base import ReasoningTrace
from mclaw.providers.base import (
    PROMPT_CACHE_LAYOUT_AUTOMATIC_PREFIX,
    PROMPT_CACHE_LAYOUT_ANTHROPIC_SYSTEM,
    PROMPT_CACHE_LAYOUT_GEMINI_USER_TAIL,
    PROMPT_CACHE_LAYOUT_OPENAI_SYSTEM,
    PROMPT_CACHE_LAYOUT_OPENROUTER_GEMINI,
    PROMPT_CACHE_LAYOUT_OPENROUTER_SYSTEM,
    PROMPT_CACHE_LAYOUT_QWEN_SYSTEM,
    PROMPT_CACHE_LAYOUT_XAI_CONVERSATION,
    RuntimeProviderProfile,
)

if TYPE_CHECKING:
    from mclaw.agent.transports.base import ModelCallOptions
    from mclaw.providers.runtime import ProviderRuntimeContext


def _append_dynamic_system(messages: list[dict[str, Any]], text: str) -> None:
    if not text:
        return
    if messages and messages[0].get("role") == "system":
        content = messages[0].get("content")
        if isinstance(content, list):
            content.append({"type": "text", "text": text})
        else:
            current = str(content or "")
            messages[0]["content"] = f"{current}\n\n{text}" if current else text
        return
    messages.insert(0, {"role": "system", "content": text})


def _insert_dynamic_after_stable_system(
    messages: list[dict[str, Any]],
    text: str,
    options: ModelCallOptions,
    *,
    role: str,
) -> None:
    if not text:
        return
    plan = options.cache_plan
    index = plan.system_message_index if plan is not None else None
    if (
        type(index) is not int
        or not 0 <= index < len(messages)
        or messages[index].get("role") != "system"
    ):
        index = next(
            (i for i, message in enumerate(messages) if message.get("role") == "system"),
            None,
        )
    content = (
        f"[M-Claw dynamic system context]\n{text}"
        if role == "user"
        else text
    )
    if type(index) is int:
        messages.insert(index + 1, {
            "role": role,
            "content": content,
            "_mclaw_dynamic_system_context": True,
        })
    else:
        messages.insert(0, {
            "role": role,
            "content": content,
            "_mclaw_dynamic_system_context": True,
        })


def reasoning_replay_messages(
    messages: object,
    traces: list[ReasoningTrace | None],
) -> list[dict[str, Any]]:
    """Align prepared messages with pre-layout traces after dynamic insertion."""
    if not isinstance(messages, list):
        return []
    candidates = [
        message
        for message in messages
        if isinstance(message, dict)
        and not message.get("_mclaw_dynamic_system_context")
    ]
    offset = max(0, len(candidates) - len(traces))
    return candidates[offset:offset + len(traces)]


def _cache_plan_enabled(options: ModelCallOptions) -> bool:
    plan = options.cache_plan
    return bool(plan is not None and plan.enabled)


def _mark_last_text_block(
    blocks: list[Any],
    *,
    marker_name: str,
    marker_value: dict[str, str],
) -> bool:
    """Mark the last stable text block before per-call dynamic context is appended."""
    for index in range(len(blocks) - 1, -1, -1):
        raw = blocks[index]
        if isinstance(raw, Mapping):
            block = deepcopy(dict(raw))
            if not str(block.get("text") or ""):
                continue
        elif str(raw):
            block = {"type": "text", "text": str(raw)}
        else:
            continue
        block[marker_name] = deepcopy(marker_value)
        blocks[index] = block
        return True
    return False


def _mark_ordered_system_prefix(
    messages: list[dict[str, Any]],
    options: ModelCallOptions,
    layout: str,
) -> bool:
    if not _cache_plan_enabled(options) or layout not in {
        PROMPT_CACHE_LAYOUT_OPENAI_SYSTEM,
        PROMPT_CACHE_LAYOUT_OPENROUTER_SYSTEM,
        PROMPT_CACHE_LAYOUT_OPENROUTER_GEMINI,
        PROMPT_CACHE_LAYOUT_QWEN_SYSTEM,
    }:
        return False
    plan = options.cache_plan
    index = plan.system_message_index if plan is not None else None
    if type(index) is not int or not 0 <= index < len(messages):
        return False
    message = messages[index]
    if message.get("role") != "system" or not isinstance(message.get("content"), list):
        return False
    marker_name = (
        "prompt_cache_breakpoint"
        if layout == PROMPT_CACHE_LAYOUT_OPENAI_SYSTEM
        else "cache_control"
    )
    return _mark_last_text_block(
        message["content"],
        marker_name=marker_name,
        marker_value={"mode": "explicit"}
        if marker_name == "prompt_cache_breakpoint"
        else {"type": "ephemeral"},
    )


def _place_dynamic_system_context(
    messages: list[dict[str, Any]],
    options: ModelCallOptions,
    layout: str,
    *,
    breakpoint_marked: bool,
) -> None:
    text = options.dynamic_system_context
    if not text:
        return
    if layout in {
        PROMPT_CACHE_LAYOUT_GEMINI_USER_TAIL,
        PROMPT_CACHE_LAYOUT_OPENROUTER_GEMINI,
    }:
        _insert_dynamic_after_stable_system(messages, text, options, role="user")
        return
    if layout in {
        PROMPT_CACHE_LAYOUT_AUTOMATIC_PREFIX,
        PROMPT_CACHE_LAYOUT_XAI_CONVERSATION,
    } or (layout and not breakpoint_marked):
        _insert_dynamic_after_stable_system(messages, text, options, role="system")
        return
    _append_dynamic_system(messages, text)


def reasoning_trace_matches(
    trace: ReasoningTrace | None,
    context: ProviderRuntimeContext,
    same_family: Callable[[str, str], bool],
) -> bool:
    """Check the shared provenance envelope before provider-native replay."""
    return bool(
        trace is not None
        and trace.provider == context.provider
        and trace.api_mode == context.api_mode
        and same_family(trace.model, context.model)
    )


def prepare_openai_compatible_request(
    profile: RuntimeProviderProfile,
    base_kwargs: dict[str, Any],
    context: ProviderRuntimeContext,
    options: ModelCallOptions,
) -> dict[str, Any]:
    """Apply the protocol defaults shared by OpenAI-compatible profiles."""
    kwargs = deepcopy(base_kwargs)
    kwargs["model"] = context.model
    messages = kwargs.get("messages")
    if not isinstance(messages, list):
        messages = []
    for message in messages:
        if isinstance(message, dict):
            message.pop("reasoning", None)
            message.pop("reasoning_details", None)
    traits = profile.model_traits(context.model)
    breakpoint_marked = _mark_ordered_system_prefix(
        messages,
        options,
        traits.prompt_cache_layout,
    )
    _place_dynamic_system_context(
        messages,
        options,
        traits.prompt_cache_layout,
        breakpoint_marked=breakpoint_marked,
    )
    kwargs["messages"] = messages

    budget = options.max_output_tokens
    if traits.max_output_tokens is not None:
        budget = traits.max_output_tokens if budget is None else min(budget, traits.max_output_tokens)
    for name in ("max_tokens", "max_completion_tokens"):
        if name != traits.output_token_param:
            kwargs.pop(name, None)
    if budget is not None:
        kwargs[traits.output_token_param] = budget
    if options.temperature is not None:
        kwargs["temperature"] = options.temperature
    if not traits.supports_tools:
        kwargs.pop("tools", None)
    return kwargs


def _anthropic_system_blocks(value: object) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [
            deepcopy(dict(block))
            if isinstance(block, Mapping)
            else {"type": "text", "text": str(block)}
            for block in value
            if isinstance(block, Mapping) or str(block)
        ]
    return [{"type": "text", "text": str(value)}] if value else []


def _anthropic_replay_blocks(
    profile: RuntimeProviderProfile,
    message: dict[str, Any],
    context: ProviderRuntimeContext,
) -> list[dict[str, Any]]:
    trace = ReasoningTrace.from_message(message)
    if (
        trace is None
        or trace.provider != context.provider
        or trace.api_mode != context.api_mode
        or trace.format != "anthropic_thinking_blocks"
        or profile.normalize_model(trace.model) != context.model
        or not isinstance(trace.payload, list)
    ):
        return []

    blocks: list[dict[str, Any]] = []
    for raw in trace.payload:
        if not isinstance(raw, Mapping):
            return []
        block_type = raw.get("type")
        if block_type == "thinking" and isinstance(raw.get("signature"), str) and raw["signature"]:
            blocks.append(deepcopy(dict(raw)))
        elif block_type == "redacted_thinking" and isinstance(raw.get("data"), str) and raw["data"]:
            blocks.append(deepcopy(dict(raw)))
        else:
            return []
    return blocks


def prepare_anthropic_compatible_request(
    profile: RuntimeProviderProfile,
    base_kwargs: dict[str, Any],
    context: ProviderRuntimeContext,
    options: ModelCallOptions,
) -> dict[str, Any]:
    """Apply Anthropic Messages defaults and replay complete native thinking blocks."""
    kwargs = deepcopy(base_kwargs)
    kwargs["model"] = context.model
    traits = profile.model_traits(context.model)
    budget = options.max_output_tokens
    if traits.max_output_tokens is not None:
        budget = traits.max_output_tokens if budget is None else min(budget, traits.max_output_tokens)
    if budget is not None:
        kwargs["max_tokens"] = budget
    if options.temperature is not None:
        kwargs["temperature"] = options.temperature
    if not traits.supports_tools:
        kwargs.pop("tools", None)

    system = _anthropic_system_blocks(kwargs.get("system"))
    if (
        _cache_plan_enabled(options)
        and traits.prompt_cache_layout == PROMPT_CACHE_LAYOUT_ANTHROPIC_SYSTEM
    ):
        _mark_last_text_block(
            system,
            marker_name="cache_control",
            marker_value={"type": "ephemeral"},
        )
    if options.dynamic_system_context:
        system.append({"type": "text", "text": options.dynamic_system_context})
    if system:
        kwargs["system"] = system
    else:
        kwargs.pop("system", None)

    messages = kwargs.get("messages")
    if not isinstance(messages, list):
        messages = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        replay = _anthropic_replay_blocks(profile, message, context)
        if replay:
            content = message.get("content")
            if isinstance(content, list):
                message["content"] = replay + content
            elif content:
                message["content"] = replay + [{"type": "text", "text": str(content)}]
            else:
                message["content"] = replay
        message.pop("reasoning", None)
        message.pop("reasoning_details", None)
    kwargs["messages"] = messages
    return kwargs


class GenericOpenAICompatibleProfile(RuntimeProviderProfile):
    """Provider using the standard OpenAI Chat Completions shape."""

    def prepare_request(
        self,
        base_kwargs: dict[str, Any],
        context: ProviderRuntimeContext,
        options: ModelCallOptions,
    ) -> dict[str, Any]:
        return prepare_openai_compatible_request(self, base_kwargs, context, options)

class GenericAnthropicCompatibleProfile(RuntimeProviderProfile):
    """Dynamic provider using the standard Anthropic Messages shape."""

    def prepare_request(
        self,
        base_kwargs: dict[str, Any],
        context: ProviderRuntimeContext,
        options: ModelCallOptions,
    ) -> dict[str, Any]:
        return prepare_anthropic_compatible_request(self, base_kwargs, context, options)
