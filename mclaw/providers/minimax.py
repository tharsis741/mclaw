# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""MiniMax behavior profile declaration."""

from __future__ import annotations

import math
from copy import deepcopy
from typing import TYPE_CHECKING, Any, Mapping

from mclaw.agent.transports.base import ReasoningTrace
from mclaw.agent.usage import UsageRecord, parse_openai_compatible_usage
from mclaw.providers.base import (
    PROMPT_CACHE_LAYOUT_AUTOMATIC_PREFIX,
    ModelTraits,
    RuntimeProviderProfile,
)
from mclaw.providers.generic import (
    prepare_openai_compatible_request,
    reasoning_replay_messages,
)

if TYPE_CHECKING:
    from mclaw.agent.transports.base import ModelCallOptions
    from mclaw.providers.runtime import ProviderRuntimeContext


class MiniMaxProfile(RuntimeProviderProfile):
    """MiniMax request and usage behavior."""

    def model_traits(self, model: str) -> ModelTraits:
        cumulative = self._uses_cumulative_split(model)
        m3 = model.casefold().startswith("minimax-m3")
        return ModelTraits(
            output_token_param="max_completion_tokens",
            max_output_tokens=524_288 if m3 else (204_800 if cumulative else None),
            include_stream_usage_option=True,
            content_stream_mode="cumulative" if cumulative else "delta",
            reasoning_stream_mode="cumulative" if cumulative else "delta",
            reasoning_modes=("minimal", "low", "medium", "high", "xhigh", "max") if m3 else (),
            prompt_cache_layout=PROMPT_CACHE_LAYOUT_AUTOMATIC_PREFIX,
        )

    def prepare_request(
        self,
        base_kwargs: dict[str, Any],
        context: ProviderRuntimeContext,
        options: ModelCallOptions,
    ) -> dict[str, Any]:
        if options.temperature is not None and (
            isinstance(options.temperature, bool)
            or not isinstance(options.temperature, (int, float))
            or not math.isfinite(options.temperature)
            or not 0 <= options.temperature <= 2
        ):
            raise ValueError("MiniMax temperature must be in the range [0, 2]")

        traces: list[ReasoningTrace | None] = []
        for message in base_kwargs.get("messages", []):
            trace = ReasoningTrace.from_message(message) if isinstance(message, dict) else None
            if trace is None:
                traces.append(None)
                continue
            if (
                trace.provider != context.provider
                or trace.api_mode != context.api_mode
                or not self._same_family(trace.model, context.model)
            ):
                traces.append(None)
                continue
            traces.append(trace)

        kwargs = prepare_openai_compatible_request(self, base_kwargs, context, options)
        for message, trace in zip(reasoning_replay_messages(kwargs["messages"], traces), traces):
            if trace is None or not isinstance(message, dict):
                continue
            if trace.text and trace.format != "reasoning_details":
                message["reasoning_content"] = trace.text
            if trace.format == "reasoning_details" and trace.payload is not None:
                message["reasoning_details"] = self._native_reasoning_details(trace.payload)

        # MiniMax accepts these as JSON-root extensions, but the OpenAI SDK does
        # not expose them as named Chat Completions arguments. ``extra_body``
        # preserves the provider wire shape without failing SDK validation.
        kwargs.pop("thinking", None)
        extra_body = deepcopy(kwargs.get("extra_body")) if isinstance(kwargs.get("extra_body"), Mapping) else {}
        extra_body.pop("thinking", None)
        if self._is_m3_family(context.model) and context.reasoning_config is not None:
            extra_body["thinking"] = {
                "type": (
                    "adaptive"
                    if context.reasoning_config.get("enabled") is True
                    else "disabled"
                )
            }
        extra_body["reasoning_split"] = True
        kwargs["extra_body"] = extra_body
        return kwargs

    def parse_usage(
        self,
        raw_usage: object,
        context: ProviderRuntimeContext,
        *,
        source: str,
    ) -> UsageRecord | None:
        return parse_openai_compatible_usage(raw_usage, context, source=source)

    @staticmethod
    def _uses_cumulative_split(model: str) -> bool:
        return model.casefold().startswith(("minimax-m2", "minimax-m3"))

    @staticmethod
    def _is_m3_family(model: str) -> bool:
        return model.casefold().startswith("minimax-m3")

    @staticmethod
    def _same_family(origin: str, current: str) -> bool:
        def family(model: str) -> str:
            lowered = model.casefold()
            if lowered.startswith("minimax-m3"):
                return "minimax-m3"
            if lowered.startswith("minimax-m2"):
                return "minimax-m2"
            if lowered.startswith("minimax"):
                return lowered.split("-", 2)[0]
            if lowered.startswith("abab"):
                return "abab"
            return lowered

        return bool(origin) and family(origin) == family(current)

    @staticmethod
    def _native_reasoning_details(payload: Any) -> Any:
        if not isinstance(payload, list):
            return deepcopy(payload)
        values = [
            item.get("value")
            for item in payload
            if isinstance(item, Mapping) and item.get("field") == "reasoning_details"
        ]
        if not values:
            return deepcopy(payload)
        flattened: list[Any] = []
        for value in values:
            if isinstance(value, list):
                flattened.extend(deepcopy(value))
            else:
                flattened.append(deepcopy(value))
        return flattened
