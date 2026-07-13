# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Qwen OpenAI-compatible behavior."""

from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Mapping

from mclaw.agent.transports.base import ReasoningTrace
from mclaw.agent.usage import (
    UsageRecord,
    parse_openai_compatible_usage,
    reported_token,
    usage_field,
)
from mclaw.providers.base import (
    PROMPT_CACHE_LAYOUT_AUTOMATIC_PREFIX,
    PROMPT_CACHE_LAYOUT_QWEN_SYSTEM,
    ModelTraits,
    RuntimeProviderProfile,
)
from mclaw.providers.generic import (
    prepare_openai_compatible_request,
    reasoning_replay_messages,
    reasoning_trace_matches,
)

if TYPE_CHECKING:
    from mclaw.agent.transports.base import ModelCallOptions
    from mclaw.providers.runtime import ProviderRuntimeContext


_REASONING_MODES = ("minimal", "low", "medium", "high", "xhigh", "max")
_HYBRID_PRESERVE = {
    "qwen3.7-max",
    "qwen3.7-max-2026-05-20",
    "qwen3.7-max-2026-06-08",
    "qwen3.7-plus",
    "qwen3.7-plus-2026-05-26",
    "qwen3.6-max-preview",
    "qwen3.6-plus",
    "qwen3.6-plus-2026-04-02",
    "qwen3.6-flash",
    "qwen3.6-flash-2026-04-16",
}
_THINKING_ONLY = {
    "qwen3.7-max-preview",
    "qwen3.7-max-2026-05-17",
    "qwen3-next-80b-a3b-thinking",
    "qwen3-235b-a22b-thinking-2507",
    "qwen3-30b-a3b-thinking-2507",
}
_HYBRID_DEFAULT_ON = _HYBRID_PRESERVE | {
    "qwen3.6-35b-a3b",
    "qwen3.5-plus",
    "qwen3.5-plus-2026-02-15",
    "qwen3.5-flash",
    "qwen3.5-flash-2026-02-23",
    "qwen3.5-397b-a17b",
    "qwen3.5-122b-a10b",
    "qwen3.5-27b",
    "qwen3.5-35b-a3b",
    "qwen3-235b-a22b",
    "qwen3-32b",
    "qwen3-30b-a3b",
    "qwen3-14b",
    "qwen3-8b",
}
_MAX_COMPLETION_MODELS = {
    "qwen3.7-max",
    "qwen3.7-max-preview",
    "qwen3.7-max-2026-05-17",
    "qwen3.7-max-2026-05-20",
    "qwen3.7-max-2026-06-08",
    "qwen3.7-plus",
    "qwen3.7-plus-2026-05-26",
    "qwen3.6-plus",
    "qwen3.6-plus-2026-04-02",
    "qwen3.6-flash",
    "qwen3.6-flash-2026-04-16",
    "qwen3.5-plus",
    "qwen3.5-plus-2026-02-15",
    "qwen3.5-flash",
    "qwen3.5-flash-2026-02-23",
}
_OPEN_64K_MODELS = {"qwen3.6-35b-a3b"}
_OPEN_8K_MODELS = {
    "qwen3.5-397b-a17b",
    "qwen3.5-122b-a10b",
    "qwen3.5-27b",
    "qwen3.5-35b-a3b",
}
_EXPLICIT_CACHE_MODELS = frozenset({
    "qwen3.7-max",
    "qwen3.7-max-2026-05-20",
    "qwen3.7-max-2026-06-08",
    "qwen3.7-plus",
    "qwen3.7-plus-2026-05-26",
    "qwen3.7-max-us",
    "qwen3.7-plus-us",
    "qwen3.6-max-preview",
    "qwen3.6-plus",
    "qwen3.6-flash",
    "qwen3.5-plus",
    "qwen3.5-plus-2026-04-20",
    "qwen3.5-flash",
    "qwen3-max",
    "qwen-plus",
    "qwen-flash",
    "qwen3-coder-plus",
    "qwen3-coder-flash",
    "qwen3-vl-plus",
    "qwen3-vl-flash",
})


def _supports_explicit_cache(model: str) -> bool:
    return model.strip().casefold() in _EXPLICIT_CACHE_MODELS


def _legacy_hybrid(model: str) -> bool:
    return bool(
        re.fullmatch(
            r"qwen(?:3-max(?:-preview|-2026-01-23)?|"
            r"-(?:plus|flash|turbo)(?:-latest|-\d{4}-\d{2}-\d{2})?)",
            model,
        )
    )


def _family(model: str) -> str:
    value = model.strip().casefold()
    if (
        value not in _HYBRID_DEFAULT_ON
        and value not in _THINKING_ONLY
        and not _legacy_hybrid(value)
    ):
        return ""
    if value.startswith("qwen3.7-max"):
        return "qwen3.7-max"
    if value.startswith("qwen3.7-plus"):
        return "qwen3.7-plus"
    if value.startswith("qwen3.6-max"):
        return "qwen3.6-max"
    if value.startswith("qwen3.6-plus"):
        return "qwen3.6-plus"
    if value.startswith("qwen3.6-flash"):
        return "qwen3.6-flash"
    if value in _HYBRID_DEFAULT_ON or value in _THINKING_ONLY:
        return value.rsplit("-thinking-", 1)[0]
    if _legacy_hybrid(value):
        return re.sub(r"(?:-latest|-\d{4}-\d{2}-\d{2})$", "", value)
    return ""


def _policy(model: str) -> tuple[str, bool, bool]:
    value = model.strip().casefold()
    if value in _THINKING_ONLY:
        return "required", True, value.startswith("qwen3.7-max")
    if value in _HYBRID_DEFAULT_ON:
        return "hybrid", True, value in _HYBRID_PRESERVE
    if _legacy_hybrid(value):
        return "hybrid", False, False
    return "", False, False


class QwenProfile(RuntimeProviderProfile):
    """Qwen thinking, preserved-reasoning, and usage behavior."""

    def model_traits(self, model: str) -> ModelTraits:
        value = model.strip().casefold()
        mode, _default_on, _preserve = _policy(model)
        return ModelTraits(
            output_token_param=(
                "max_completion_tokens" if value in _MAX_COMPLETION_MODELS else "max_tokens"
            ),
            max_output_tokens=(
                65_536
                if value in _MAX_COMPLETION_MODELS or value in _OPEN_64K_MODELS
                else (8_192 if value in _OPEN_8K_MODELS else None)
            ),
            include_stream_usage_option=bool(mode),
            requires_stream=bool(mode),
            reasoning_modes=_REASONING_MODES if mode else (),
            prompt_cache_layout=(
                PROMPT_CACHE_LAYOUT_QWEN_SYSTEM
                if _supports_explicit_cache(model)
                else PROMPT_CACHE_LAYOUT_AUTOMATIC_PREFIX
            ),
        )

    def prepare_request(
        self,
        base_kwargs: dict[str, Any],
        context: ProviderRuntimeContext,
        options: ModelCallOptions,
    ) -> dict[str, Any]:
        traces = [
            ReasoningTrace.from_message(message) if isinstance(message, dict) else None
            for message in base_kwargs.get("messages", [])
        ]
        kwargs = prepare_openai_compatible_request(self, base_kwargs, context, options)
        mode, default_on, supports_preserve = _policy(context.model)
        extra = kwargs.get("extra_body")
        extra = deepcopy(dict(extra)) if isinstance(extra, Mapping) else {}

        configured = context.reasoning_config
        if not mode:
            extra.pop("enable_thinking", None)
            extra.pop("thinking_budget", None)
            extra.pop("preserve_thinking", None)
            effective_thinking = False
        else:
            requested = extra.get("enable_thinking")
            if requested is not None and type(requested) is not bool:
                raise ValueError("Qwen enable_thinking must be a boolean")
            if configured is not None:
                requested = configured.get("enabled") is True
            if mode == "required":
                extra.pop("enable_thinking", None)
                effective_thinking = True
            else:
                if requested is not None:
                    extra["enable_thinking"] = requested
                effective_thinking = default_on if requested is None else requested

            budget = extra.get("thinking_budget")
            if budget is not None and (type(budget) is not int or budget <= 0):
                raise ValueError("Qwen thinking_budget must be a positive integer")
            if not effective_thinking:
                extra.pop("thinking_budget", None)
            if supports_preserve and effective_thinking:
                extra["preserve_thinking"] = True
            else:
                extra.pop("preserve_thinking", None)

        if extra:
            kwargs["extra_body"] = extra
        else:
            kwargs.pop("extra_body", None)
        if not (supports_preserve and effective_thinking):
            return kwargs

        for message, trace in zip(reasoning_replay_messages(kwargs["messages"], traces), traces):
            if (
                not isinstance(message, dict)
                or not reasoning_trace_matches(trace, context, self._same_family)
            ):
                continue
            if reasoning := trace.to_replay_text():
                message["reasoning_content"] = reasoning
        return kwargs

    def parse_usage(
        self,
        raw_usage: object,
        context: ProviderRuntimeContext,
        *,
        source: str,
    ) -> UsageRecord | None:
        record = parse_openai_compatible_usage(raw_usage, context, source=source)
        prompt_details = usage_field(raw_usage, "prompt_tokens_details")
        cache_write = reported_token(
            prompt_details,
            "cache_creation_input_tokens",
            "ephemeral_5m_input_tokens",
        )
        if cache_write is None:
            cache_creation = usage_field(prompt_details, "cache_creation")
            cache_write = reported_token(
                cache_creation,
                "cache_creation_input_tokens",
                "ephemeral_5m_input_tokens",
            )
        if cache_write is None:
            return record
        if record is None:
            return UsageRecord(
                provider=context.provider,
                model=context.model,
                cache_write_tokens=cache_write,
                source=source,
            )
        return replace(record, cache_write_tokens=cache_write)

    @staticmethod
    def _same_family(origin: str, current: str) -> bool:
        origin_family = _family(origin)
        return bool(origin_family) and origin_family == _family(current)
