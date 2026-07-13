# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Zhipu GLM request and usage behavior."""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Any, Mapping

from mclaw.agent.transports.base import ReasoningTrace
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


def _family(model: str) -> str:
    lowered = model.strip().casefold()
    for name in (
        "glm-5.2",
        "glm-5.1",
        "glm-5v",
        "glm-4.7",
        "glm-4.6v",
        "glm-4.6",
        "glm-4.5v",
        "glm-4.5",
        "glm-4.1v-thinking",
    ):
        if lowered.startswith(name):
            return name
    if lowered == "glm-5" or lowered.startswith("glm-5-"):
        return "glm-5"
    return lowered


def _reasoning_modes(model: str) -> tuple[str, ...]:
    family = _family(model)
    if family == "glm-5.2":
        return ("minimal", "low", "medium", "high", "xhigh", "max")
    if family in {
        "glm-5.1",
        "glm-5",
        "glm-5v",
        "glm-4.7",
        "glm-4.6v",
        "glm-4.6",
        "glm-4.5v",
        "glm-4.5",
    }:
        return ("high",)
    return ()


def _max_output_tokens(model: str) -> int | None:
    family = _family(model)
    if family in {"glm-5.2", "glm-5.1", "glm-5", "glm-5v", "glm-4.7", "glm-4.6"}:
        return 131_072
    if family in {"glm-4.6v", "glm-4.1v-thinking"}:
        return 32_768
    if family == "glm-4.5":
        return 98_304
    if family == "glm-4.5v":
        return 16_384
    return None


class ZhipuGLMProfile(RuntimeProviderProfile):
    """Zhipu GLM request and usage behavior."""

    def normalize_model(self, model: str) -> str:
        model = model.strip()
        for prefix in ("zhipu/", "zhipuai/", "zai/"):
            if model.casefold().startswith(prefix):
                return model[len(prefix):]
        return model

    def model_traits(self, model: str) -> ModelTraits:
        model = self.normalize_model(model)
        return ModelTraits(
            max_output_tokens=_max_output_tokens(model),
            reasoning_modes=_reasoning_modes(model),
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

        modes = _reasoning_modes(context.model)
        config = context.reasoning_config
        thinking = bool(modes) and (config is None or config.get("enabled") is True)
        if (
            _family(context.model) == "glm-5.2"
            and config is not None
            and config.get("effort") == "minimal"
        ):
            thinking = False
        matching = [
            reasoning_trace_matches(trace, context, self._same_family)
            for trace in traces
        ]
        extra_body = deepcopy(kwargs.get("extra_body")) if isinstance(kwargs.get("extra_body"), Mapping) else {}
        extra_body.pop("thinking", None)
        if modes:
            native: dict[str, Any] = {"type": "enabled" if thinking else "disabled"}
            if thinking and any(matching):
                native["clear_thinking"] = False
            extra_body["thinking"] = native
        if extra_body:
            kwargs["extra_body"] = extra_body
        else:
            kwargs.pop("extra_body", None)

        if (
            config is not None
            and config.get("enabled") is True
            and _family(context.model) == "glm-5.2"
        ):
            effort = str(config.get("effort") or "")
            mapped = {
                "minimal": "minimal",
                "low": "high",
                "medium": "high",
                "high": "high",
                "xhigh": "max",
                "max": "max",
            }.get(effort)
            if mapped is None:
                raise ValueError(f"Unsupported GLM reasoning effort: {effort}")
            kwargs["reasoning_effort"] = mapped

        for message, trace, matches in zip(
            reasoning_replay_messages(kwargs["messages"], traces),
            traces,
            matching,
        ):
            if (
                thinking
                and matches
                and isinstance(message, dict)
                and trace is not None
                and trace.text
            ):
                message["reasoning_content"] = trace.text
        return kwargs

    @staticmethod
    def _same_family(origin: str, current: str) -> bool:
        return bool(origin) and _family(origin) == _family(current)
