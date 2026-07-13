# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Xiaomi MiMo OpenAI-compatible behavior."""

from __future__ import annotations

import math
from copy import deepcopy
from typing import TYPE_CHECKING, Any, Mapping

from mclaw.agent.transports.base import ReasoningTrace
from mclaw.providers.base import ModelTraits, RuntimeProviderProfile
from mclaw.providers.generic import (
    prepare_openai_compatible_request,
    reasoning_replay_messages,
    reasoning_trace_matches,
)

if TYPE_CHECKING:
    from mclaw.agent.transports.base import ModelCallOptions
    from mclaw.providers.runtime import ProviderRuntimeContext


_CHAT_MODELS = {
    "mimo-v2.5-pro": (131_072, "mimo-v2.5-pro"),
    "mimo-v2.5": (131_072, "mimo-v2.5"),
}
_TTS_MODELS = {
    "mimo-v2.5-tts",
    "mimo-v2.5-tts-voiceclone",
    "mimo-v2.5-tts-voicedesign",
}
_REASONING_MODES = ("high",)


class XiaomiMiMoProfile(RuntimeProviderProfile):
    """MiMo V2.5 thinking, tool-call replay, and usage behavior."""

    def model_traits(self, model: str) -> ModelTraits:
        value = model.strip().casefold()
        if value in _CHAT_MODELS:
            return ModelTraits(
                output_token_param="max_completion_tokens",
                max_output_tokens=_CHAT_MODELS[value][0],
                reasoning_modes=_REASONING_MODES,
            )
        if value in _TTS_MODELS:
            return ModelTraits(
                output_token_param="max_completion_tokens",
                max_output_tokens=8_192,
                supports_tools=False,
            )
        return ModelTraits()

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
        value = context.model.strip().casefold()
        extra = kwargs.get("extra_body")
        extra = deepcopy(dict(extra)) if isinstance(extra, Mapping) else {}
        raw_thinking = extra.get("thinking", kwargs.pop("thinking", None))
        configured = context.reasoning_config

        if value in _CHAT_MODELS:
            if raw_thinking is not None and (
                not isinstance(raw_thinking, Mapping)
                or raw_thinking.get("type") not in {"enabled", "disabled"}
            ):
                raise ValueError("MiMo thinking.type must be enabled or disabled")
            effective_thinking = (
                raw_thinking is None or raw_thinking.get("type") == "enabled"
            )
            if configured is not None:
                effective_thinking = configured.get("enabled") is True
                extra["thinking"] = {
                    "type": "enabled" if effective_thinking else "disabled"
                }
            elif raw_thinking is not None:
                extra["thinking"] = deepcopy(dict(raw_thinking))
        else:
            effective_thinking = False
            extra.pop("thinking", None)

        if effective_thinking:
            kwargs.pop("temperature", None)
            kwargs.pop("top_p", None)
        elif options.temperature is not None and (
            isinstance(options.temperature, bool)
            or not isinstance(options.temperature, (int, float))
            or not math.isfinite(options.temperature)
            or not 0 <= options.temperature <= 1.5
        ):
            raise ValueError("MiMo temperature must be in the range [0, 1.5]")

        if kwargs.get("tools") and value in _CHAT_MODELS:
            kwargs["tool_choice"] = "auto"
        elif value not in _CHAT_MODELS:
            kwargs.pop("tool_choice", None)
        if extra:
            kwargs["extra_body"] = extra
        else:
            kwargs.pop("extra_body", None)
        if not effective_thinking:
            return kwargs

        for message, trace in zip(reasoning_replay_messages(kwargs["messages"], traces), traces):
            if (
                not isinstance(message, dict)
                or not message.get("tool_calls")
                or not reasoning_trace_matches(trace, context, self._same_family)
            ):
                continue
            if reasoning := trace.to_replay_text():
                message["reasoning_content"] = reasoning
        return kwargs

    @staticmethod
    def _same_family(origin: str, current: str) -> bool:
        origin_value = origin.strip().casefold()
        current_value = current.strip().casefold()
        return (
            origin_value in _CHAT_MODELS
            and current_value in _CHAT_MODELS
            and _CHAT_MODELS[origin_value][1] == _CHAT_MODELS[current_value][1]
        )
