# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""xAI Grok OpenAI-compatible behavior."""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Any, Mapping

from mclaw.providers.base import (
    PROMPT_CACHE_LAYOUT_XAI_CONVERSATION,
    ModelTraits,
    RuntimeProviderProfile,
)
from mclaw.providers.generic import prepare_openai_compatible_request

if TYPE_CHECKING:
    from mclaw.agent.transports.base import ModelCallOptions
    from mclaw.providers.runtime import ProviderRuntimeContext


_GROK_45 = {"grok-4.5"}
_GROK_43 = {"grok-4.3"}
_GROK_420_REASONING = {
    "grok-4.20-0309-reasoning",
    "grok-4.20-reasoning-latest",
    "grok-4.20",
    "grok-4.20-reasoning",
    "grok-4.20-0309",
}
_GROK_420_NON_REASONING = {
    "grok-4.20-0309-non-reasoning",
    "grok-4.20-non-reasoning",
    "grok-4.20-non-reasoning-latest",
}
_GROK_420_MULTI_AGENT = {
    "grok-4.20-multi-agent",
    "grok-4.20-multi-agent-latest",
    "grok-4.20-multi-agent-0309",
    "grok-4.20-multi-agent-beta-0309",
    "grok-4.20-multi-agent-beta-latest",
}
_UNSUPPORTED_REASONING_PARAMETERS = (
    "presence_penalty",
    "frequency_penalty",
    "stop",
    "presencePenalty",
    "frequencyPenalty",
)


def _policy(model: str) -> tuple[str, tuple[str, ...], bool, bool, bool]:
    value = model.strip().casefold()
    if value in _GROK_45:
        return "grok-4.5", ("low", "medium", "high"), False, True, False
    if value in _GROK_43:
        return "grok-4.3", ("low", "medium", "high"), True, True, False
    if value in _GROK_420_REASONING:
        return "grok-4.20-reasoning", (), False, True, False
    if value in _GROK_420_NON_REASONING:
        return "grok-4.20-non-reasoning", (), False, False, False
    if value in _GROK_420_MULTI_AGENT:
        return "grok-4.20-multi-agent", (), False, True, True
    return "", (), False, False, False


class XAIProfile(RuntimeProviderProfile):
    """Grok reasoning parameter filtering and usage behavior."""

    def model_traits(self, model: str) -> ModelTraits:
        _family, modes, _can_disable, _reasoning, _responses_only = _policy(model)
        return ModelTraits(
            reasoning_modes=modes,
            prompt_cache_layout=PROMPT_CACHE_LAYOUT_XAI_CONVERSATION,
        )

    def prepare_request(
        self,
        base_kwargs: dict[str, Any],
        context: ProviderRuntimeContext,
        options: ModelCallOptions,
    ) -> dict[str, Any]:
        _family, modes, can_disable, reasoning_model, responses_only = _policy(
            context.model
        )
        if responses_only:
            raise ValueError(
                f"{context.model} requires the xAI Responses API; Chat Completions is unsupported"
            )

        kwargs = prepare_openai_compatible_request(self, base_kwargs, context, options)
        config = context.reasoning_config
        if not modes:
            kwargs.pop("reasoning_effort", None)
        elif config is not None:
            kwargs.pop("reasoning_effort", None)
            if config.get("enabled") is True:
                kwargs["reasoning_effort"] = config["effort"]
            elif can_disable:
                kwargs["reasoning_effort"] = "none"
        elif kwargs.get("reasoning_effort") not in (*modes, "none" if can_disable else ""):
            kwargs.pop("reasoning_effort", None)

        if reasoning_model:
            for name in _UNSUPPORTED_REASONING_PARAMETERS:
                kwargs.pop(name, None)
        headers = kwargs.get("extra_headers")
        headers = deepcopy(dict(headers)) if isinstance(headers, Mapping) else {}
        for name in tuple(headers):
            if str(name).casefold() == "x-grok-conv-id":
                headers.pop(name)
        plan = options.cache_plan
        conversation_key = getattr(plan, "conversation_key", "") if plan else ""
        if plan is not None and plan.enabled and conversation_key:
            headers["x-grok-conv-id"] = conversation_key
        if headers:
            kwargs["extra_headers"] = headers
        else:
            kwargs.pop("extra_headers", None)
        return kwargs

    @staticmethod
    def _same_family(origin: str, current: str) -> bool:
        origin_family, _modes, _disabled, _reasoning, _responses = _policy(origin)
        current_family, _modes, _disabled, _reasoning, _responses = _policy(current)
        return bool(origin_family) and origin_family == current_family
