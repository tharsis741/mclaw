# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Anthropic Messages request and usage behavior."""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Any, Mapping

from mclaw.providers.base import PROMPT_CACHE_LAYOUT_ANTHROPIC_SYSTEM, ModelTraits
from mclaw.providers.generic import GenericAnthropicCompatibleProfile

if TYPE_CHECKING:
    from mclaw.agent.transports.base import ModelCallOptions
    from mclaw.providers.runtime import ProviderRuntimeContext


_MAX_OUTPUT_TOKENS = {
    "claude-fable-5": 128_000,
    "claude-mythos-5": 128_000,
    "claude-mythos-preview": 128_000,
    "claude-opus-4-8": 128_000,
    "claude-sonnet-5": 128_000,
    "claude-haiku-4-5": 64_000,
    "claude-opus-4-7": 128_000,
    "claude-opus-4-6": 128_000,
    "claude-sonnet-4-6": 128_000,
    "claude-opus-4-5": 64_000,
    "claude-sonnet-4-5": 64_000,
    "claude-opus-4-1": 32_000,
    "claude-sonnet-4-0": 16_384,
    "claude-sonnet-4": 16_384,
    "claude-3-5-sonnet": 8_192,
    "claude-3-5-haiku": 8_192,
    "claude-3-opus": 4_096,
    "claude-3-haiku": 4_096,
}

_EFFORT_BASE = ("low", "medium", "high")
_EFFORT_XHIGH_MAX = (*_EFFORT_BASE, "xhigh", "max")
_EFFORT_MAX = (*_EFFORT_BASE, "max")


def _family_policy(model: str) -> tuple[tuple[str, ...], str, bool]:
    lowered = model.casefold()
    if "claude-mythos-preview" in lowered:
        return _EFFORT_MAX, "always", True
    if any(name in lowered for name in ("claude-fable-5", "claude-mythos-5")):
        return _EFFORT_XHIGH_MAX, "always", True
    if "claude-sonnet-5" in lowered:
        return _EFFORT_XHIGH_MAX, "default", True
    if any(name in lowered for name in ("claude-opus-4-8", "claude-opus-4-7")):
        return _EFFORT_XHIGH_MAX, "opt_in", True
    if any(name in lowered for name in ("claude-opus-4-6", "claude-sonnet-4-6")):
        return _EFFORT_MAX, "opt_in", False
    if "claude-opus-4-5" in lowered:
        return _EFFORT_BASE, "manual", False
    return (), "none", False


class AnthropicProfile(GenericAnthropicCompatibleProfile):
    """Anthropic Messages request and usage behavior."""

    def normalize_model(self, model: str) -> str:
        model = model.strip()
        if model.casefold().startswith("anthropic/"):
            model = model[len("anthropic/"):]
        return model.replace(".", "-")

    def model_traits(self, model: str) -> ModelTraits:
        lowered = model.casefold()
        matched = max(
            (name for name in _MAX_OUTPUT_TOKENS if name in lowered),
            key=len,
            default="",
        )
        max_output = _MAX_OUTPUT_TOKENS.get(matched, 8_192)
        modes, _thinking_policy, _sampling_restricted = _family_policy(model)
        return ModelTraits(
            max_output_tokens=max_output,
            requires_stream=max_output > 21_333,
            reasoning_modes=modes,
            prompt_cache_layout=PROMPT_CACHE_LAYOUT_ANTHROPIC_SYSTEM,
        )

    def prepare_request(
        self,
        base_kwargs: dict[str, Any],
        context: ProviderRuntimeContext,
        options: ModelCallOptions,
    ) -> dict[str, Any]:
        kwargs = super().prepare_request(base_kwargs, context, options)
        modes, thinking_policy, sampling_restricted = _family_policy(context.model)
        config = context.reasoning_config
        if sampling_restricted or (
            config is not None
            and config.get("enabled") is True
            and thinking_policy == "opt_in"
        ):
            kwargs.pop("temperature", None)

        if config is None or not modes:
            return kwargs
        output_config = (
            deepcopy(dict(kwargs.get("output_config")))
            if isinstance(kwargs.get("output_config"), Mapping)
            else {}
        )
        if config.get("enabled") is True:
            effort = str(config.get("effort") or "")
            if effort not in modes:
                raise ValueError(f"Unsupported Anthropic reasoning effort: {effort}")
            output_config["effort"] = effort
            kwargs["output_config"] = output_config
            if thinking_policy == "opt_in":
                kwargs["thinking"] = {"type": "adaptive"}
            elif thinking_policy in {"always", "default"}:
                kwargs.pop("thinking", None)
        elif config.get("enabled") is False:
            output_config.pop("effort", None)
            if output_config:
                kwargs["output_config"] = output_config
            else:
                kwargs.pop("output_config", None)
            if thinking_policy == "default":
                kwargs["thinking"] = {"type": "disabled"}
            else:
                kwargs.pop("thinking", None)
        return kwargs
