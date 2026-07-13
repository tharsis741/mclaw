# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""DeepSeek request and usage behavior."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Mapping

from mclaw.agent.transports.base import ReasoningTrace
from mclaw.agent.usage import (
    UsageRecord,
    parse_openai_compatible_usage,
    reported_token,
)
from mclaw.providers.base import (
    PROMPT_CACHE_LAYOUT_AUTOMATIC_PREFIX,
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


_REASONING_MODES = ("low", "medium", "high", "xhigh", "max")


def _family(model: str) -> str:
    lowered = model.strip().casefold()
    if lowered == "deepseek-reasoner":
        return "deepseek-v4-flash"
    if lowered == "deepseek-chat":
        return "deepseek-v4-flash-nonthinking"
    for name in ("deepseek-v4-pro", "deepseek-v4-flash"):
        if lowered.startswith(name):
            return name
    return lowered


def _is_thinking_family(model: str) -> bool:
    family = _family(model)
    return family in {"deepseek-v4-pro", "deepseek-v4-flash"}


class DeepSeekProfile(RuntimeProviderProfile):
    """DeepSeek request and usage behavior."""

    def normalize_model(self, model: str) -> str:
        model = model.strip()
        return model[len("deepseek/"):] if model.casefold().startswith("deepseek/") else model

    def model_traits(self, model: str) -> ModelTraits:
        normalized = self.normalize_model(model)
        current = _family(normalized).startswith("deepseek-v4-")
        return ModelTraits(
            max_output_tokens=384_000 if current else None,
            include_stream_usage_option=True,
            reasoning_modes=_REASONING_MODES if _is_thinking_family(normalized) else (),
            prompt_cache_layout=PROMPT_CACHE_LAYOUT_AUTOMATIC_PREFIX,
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
        kwargs.pop("reasoning_effort", None)

        known_family = _family(context.model).startswith("deepseek-v4-")
        config = context.reasoning_config
        thinking = _is_thinking_family(context.model)
        if config is not None and known_family:
            thinking = config.get("enabled") is True

        extra_body = deepcopy(kwargs.get("extra_body")) if isinstance(kwargs.get("extra_body"), Mapping) else {}
        extra_body.pop("thinking", None)
        if known_family:
            extra_body["thinking"] = {"type": "enabled" if thinking else "disabled"}
        if extra_body:
            kwargs["extra_body"] = extra_body
        else:
            kwargs.pop("extra_body", None)

        if thinking:
            for name in ("temperature", "top_p", "presence_penalty", "frequency_penalty"):
                kwargs.pop(name, None)
            if config is not None and config.get("enabled") is True:
                effort = str(config.get("effort") or "")
                mapped = {
                    "low": "high",
                    "medium": "high",
                    "high": "high",
                    "xhigh": "max",
                    "max": "max",
                }.get(effort)
                if mapped is None:
                    raise ValueError(f"Unsupported DeepSeek reasoning effort: {effort}")
                kwargs["reasoning_effort"] = mapped

        for message, trace in zip(reasoning_replay_messages(kwargs["messages"], traces), traces):
            if (
                thinking
                and isinstance(message, dict)
                and message.get("tool_calls")
                and reasoning_trace_matches(trace, context, self._same_family)
                and trace.text
            ):
                message["reasoning_content"] = trace.text
        return kwargs

    def parse_usage(
        self,
        raw_usage: object,
        context: ProviderRuntimeContext,
        *,
        source: str,
    ) -> UsageRecord | None:
        record = parse_openai_compatible_usage(raw_usage, context, source=source)
        cache_read = reported_token(raw_usage, "prompt_cache_hit_tokens")
        if cache_read is None:
            return record
        if record is None:
            return UsageRecord(
                provider=context.provider,
                model=context.model,
                cache_read_tokens=cache_read,
                source=source,
            )
        return replace(record, cache_read_tokens=cache_read)

    @staticmethod
    def _same_family(origin: str, current: str) -> bool:
        return bool(origin) and _family(origin) == _family(current)
