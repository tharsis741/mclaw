# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""OpenRouter request and usage behavior."""

from __future__ import annotations

import re
from copy import deepcopy
from typing import TYPE_CHECKING, Any, Mapping

from mclaw.agent.transports.base import ReasoningTrace
from mclaw.providers.base import (
    PROMPT_CACHE_LAYOUT_AUTOMATIC_PREFIX,
    PROMPT_CACHE_LAYOUT_GEMINI_USER_TAIL,
    PROMPT_CACHE_LAYOUT_OPENROUTER_GEMINI,
    PROMPT_CACHE_LAYOUT_OPENROUTER_SYSTEM,
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


_ALIBABA_EXPLICIT_CACHE_MODELS = frozenset({
    "deepseek/deepseek-v3.2",
    "qwen/qwen3-max",
    "qwen/qwen-plus",
    "qwen/qwen3.6-plus",
    "qwen/qwen3-coder-plus",
    "qwen/qwen3-coder-flash",
})


def _router_model(model: str) -> str:
    return model.strip().casefold().lstrip("~")


def _uses_standard_cache_control(model: str) -> bool:
    value = _router_model(model)
    return value.startswith("anthropic/") or value in _ALIBABA_EXPLICIT_CACHE_MODELS


def _known_gemini_cache_model(model: str) -> bool:
    value = _router_model(model)
    if not value.startswith("google/"):
        return False
    value = value[len("google/"):]
    if value == "gemini-3.1-pro-preview-customtools":
        return True
    dated = r"(?:-\d{2}-(?:\d{2}|\d{4}))?"
    return bool(
        re.fullmatch(
            rf"gemini-(?:"
            rf"3\.5-flash(?:-(?:latest|\d{{3}}|preview{dated}|exp{dated}))?|"
            rf"3\.1-(?:pro|flash-lite)(?:-preview{dated})?|"
            rf"3-flash(?:-(?:latest|\d{{3}}|preview{dated}))?|"
            rf"2\.5-(?:pro|flash|flash-lite)(?:-(?:latest|\d{{3}}|preview{dated}))?"
            rf")",
            value,
        )
    )


def _native_reasoning_details(payload: object) -> list[dict[str, Any]] | None:
    if not isinstance(payload, list) or not payload:
        return None
    if all(isinstance(item, Mapping) and "field" not in item for item in payload):
        return deepcopy(payload)

    details: list[dict[str, Any]] = []
    for event in payload:
        if not isinstance(event, Mapping):
            return None
        if event.get("field") != "reasoning_details":
            continue
        value = event.get("value")
        if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
            return None
        details.extend(deepcopy(value))
    return details or None


class OpenRouterProfile(RuntimeProviderProfile):
    """OpenRouter request and usage behavior."""

    def model_traits(self, model: str) -> ModelTraits:
        modes = () if model.strip().casefold() in {"openrouter/auto", "openrouter/free"} else _REASONING_MODES
        router_model = _router_model(model)
        if _uses_standard_cache_control(model):
            cache_layout = PROMPT_CACHE_LAYOUT_OPENROUTER_SYSTEM
        elif _known_gemini_cache_model(model):
            cache_layout = PROMPT_CACHE_LAYOUT_OPENROUTER_GEMINI
        elif router_model.startswith("google/gemini-"):
            cache_layout = PROMPT_CACHE_LAYOUT_GEMINI_USER_TAIL
        else:
            cache_layout = PROMPT_CACHE_LAYOUT_AUTOMATIC_PREFIX
        return ModelTraits(
            output_token_param="max_completion_tokens",
            include_stream_usage_option=True,
            reasoning_modes=modes,
            prompt_cache_layout=cache_layout,
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

        extra_body = deepcopy(kwargs.get("extra_body")) if isinstance(kwargs.get("extra_body"), Mapping) else {}
        extra_body.pop("reasoning", None)
        kwargs.pop("reasoning_effort", None)
        config = context.reasoning_config
        if config is not None and self.model_traits(context.model).reasoning_modes:
            if config.get("enabled") is False:
                extra_body["reasoning"] = {"effort": "none"}
            else:
                effort = str(config.get("effort") or "")
                if effort not in _REASONING_MODES:
                    raise ValueError(f"Unsupported OpenRouter reasoning effort: {effort}")
                extra_body["reasoning"] = {"effort": effort}

        kwargs.pop("session_id", None)
        extra_body.pop("session_id", None)
        plan = options.cache_plan
        conversation_key = getattr(plan, "conversation_key", "") if plan else ""
        if plan is not None and plan.enabled and conversation_key:
            if len(conversation_key) > 256:
                raise ValueError("OpenRouter session_id must not exceed 256 characters")
            # OpenAI SDK sends provider-specific top-level JSON via extra_body;
            # ``session_id`` is not a named Chat Completions SDK argument.
            extra_body["session_id"] = conversation_key
        if extra_body:
            kwargs["extra_body"] = extra_body
        else:
            kwargs.pop("extra_body", None)

        for message, trace in zip(reasoning_replay_messages(kwargs["messages"], traces), traces):
            if (
                not isinstance(message, dict)
                or not reasoning_trace_matches(trace, context, self._same_family)
            ):
                continue
            if trace.format == "reasoning_details":
                if details := _native_reasoning_details(trace.payload):
                    message["reasoning_details"] = details
            elif trace.text:
                message["reasoning"] = trace.text
        return kwargs

    @staticmethod
    def _same_family(origin: str, current: str) -> bool:
        return bool(origin) and origin.strip().casefold() == current.strip().casefold()
