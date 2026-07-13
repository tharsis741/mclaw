# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Request-time token budget contract."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mclaw.agent.context_compressor import (
    estimate_messages_tokens,
    estimate_tokens_rough,
)
from mclaw.agent.transports.base import ReasoningTrace

if TYPE_CHECKING:
    from mclaw.providers.runtime import ProviderRuntimeContext


_PROTOCOL_DEFAULT_OUTPUT_TOKENS = 8_192


@dataclass(frozen=True)
class TokenBudgetEstimate:
    input_tokens: int
    output_budget: int
    total_budget: int
    context_window: int
    source: str = "estimate"


def _positive_int(value: object, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _model_matches(context: ProviderRuntimeContext, origin: str) -> bool:
    if not origin:
        return False
    profile = context.profile
    normalized_origin = profile.normalize_model(origin)
    normalized_current = profile.normalize_model(context.model)
    if normalized_origin == normalized_current:
        return True
    same_family = getattr(profile, "_same_family", None)
    return bool(
        callable(same_family)
        and same_family(normalized_origin, normalized_current)
    )


def _trace_matches(
    message: dict[str, Any],
    trace: ReasoningTrace,
    context: ProviderRuntimeContext,
) -> bool:
    details = message.get("reasoning_details")
    if not (isinstance(details, Mapping) and "schema_version" in details):
        return True
    return (
        trace.provider == context.provider
        and trace.api_mode == context.api_mode
        and _model_matches(context, trace.model)
    )


def _reasoning_tokens(
    messages: list[dict[str, Any]],
    context: ProviderRuntimeContext,
) -> int:
    total = 0
    for message in messages:
        trace = ReasoningTrace.from_message(message)
        if trace is None or not _trace_matches(message, trace, context):
            continue
        budget_text = trace.to_budget_text()
        if trace.format == "gemini_thought_signature" and trace.text:
            budget_text = budget_text.removeprefix(f"{trace.text}\n")
        if (
            not budget_text
            or budget_text == message.get("reasoning")
            or budget_text == message.get("reasoning_content")
        ):
            continue
        total += estimate_tokens_rough(budget_text)
    return total


def estimate_request_budget(
    *,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    dynamic_system_context: str,
    context: ProviderRuntimeContext,
    context_window: int,
    max_output_tokens: int | None = None,
) -> TokenBudgetEstimate:
    """Estimate one request without mixing estimates into provider usage."""
    if not isinstance(messages, list) or any(not isinstance(item, dict) for item in messages):
        raise TypeError("messages must be a list of dictionaries")
    if not isinstance(tools, list) or any(not isinstance(item, dict) for item in tools):
        raise TypeError("tools must be a list of dictionaries")
    if not isinstance(dynamic_system_context, str):
        raise TypeError("dynamic_system_context must be a string")
    window = _positive_int(context_window, "context_window")

    requested = (
        None
        if max_output_tokens is None
        else _positive_int(max_output_tokens, "max_output_tokens")
    )
    trait_limit = context.profile.model_traits(context.model).max_output_tokens
    if trait_limit is not None:
        trait_limit = _positive_int(trait_limit, "model max_output_tokens")
    if requested is not None and trait_limit is not None:
        output_budget = min(requested, trait_limit)
    else:
        output_budget = requested or trait_limit or _PROTOCOL_DEFAULT_OUTPUT_TOKENS

    input_tokens = estimate_messages_tokens(messages)
    input_tokens += _reasoning_tokens(messages, context)
    if tools:
        serialized_tools = json.dumps(
            tools,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        input_tokens += estimate_tokens_rough(serialized_tools)
    input_tokens += estimate_tokens_rough(dynamic_system_context)
    return TokenBudgetEstimate(
        input_tokens=input_tokens,
        output_budget=output_budget,
        total_budget=input_tokens + output_budget,
        context_window=window,
    )
