# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""OpenAI behavior profile declaration."""

from __future__ import annotations

import re
from copy import deepcopy
from typing import TYPE_CHECKING, Any, Mapping

from mclaw.agent.usage import UsageRecord, parse_openai_compatible_usage
from mclaw.providers.base import (
    PROMPT_CACHE_LAYOUT_AUTOMATIC_PREFIX,
    PROMPT_CACHE_LAYOUT_OPENAI_SYSTEM,
    ModelTraits,
    RuntimeProviderProfile,
)
from mclaw.providers.generic import prepare_openai_compatible_request

if TYPE_CHECKING:
    from mclaw.agent.transports.base import ModelCallOptions
    from mclaw.providers.runtime import ProviderRuntimeContext


_STANDARD_EFFORTS = ("low", "medium", "high")


def _requires_responses_api(model: str) -> bool:
    lowered = model.strip().casefold()
    return bool(
        "-codex" in lowered
        or lowered.startswith("codex-")
        or re.match(r"^gpt-5(?:\.\d+)?-pro(?:-|$)", lowered)
        or re.match(r"^o\d+-pro(?:-|$)", lowered)
    )


def _supports_explicit_prompt_cache(model: str) -> bool:
    match = re.match(r"^gpt-(\d+)(?:\.(\d+))?(?:-|$)", model.strip().casefold())
    if not match:
        return False
    major = int(match.group(1))
    minor = int(match.group(2) or 0)
    return major > 5 or (major == 5 and minor >= 6)


def _has_explicit_system_breakpoint(kwargs: Mapping[str, Any]) -> bool:
    messages = kwargs.get("messages")
    if not isinstance(messages, list):
        return False
    for message in messages:
        if not isinstance(message, Mapping) or message.get("role") != "system":
            continue
        content = message.get("content")
        return bool(
            isinstance(content, list)
            and any(
                isinstance(block, Mapping)
                and block.get("prompt_cache_breakpoint") == {"mode": "explicit"}
                for block in content
            )
        )
    return False


def _model_policy(model: str) -> tuple[tuple[str, ...], bool, int | None, str]:
    lowered = model.casefold()
    if lowered in {
        "gpt-5-chat-latest",
        "gpt-5.1-chat-latest",
        "gpt-5.2-chat-latest",
        "gpt-5.3-chat-latest",
    }:
        return (), False, 16_384, "max_completion_tokens"
    if lowered == "chat-latest":
        return (), False, 128_000, "max_completion_tokens"
    if "-codex" in lowered:
        return (), False, 128_000, "max_completion_tokens"
    if lowered.startswith("gpt-5.6"):
        return (*_STANDARD_EFFORTS, "xhigh", "max"), True, 128_000, "max_completion_tokens"
    if lowered.startswith(("gpt-5.5-pro", "gpt-5.4-pro", "gpt-5.2-pro")):
        return ("medium", "high", "xhigh"), False, 128_000, "max_completion_tokens"
    if lowered.startswith("gpt-5-pro"):
        return ("high",), False, 272_000, "max_completion_tokens"
    if lowered.startswith(("gpt-5.5", "gpt-5.4", "gpt-5.3", "gpt-5.2")):
        return (*_STANDARD_EFFORTS, "xhigh"), True, 128_000, "max_completion_tokens"
    if lowered.startswith("gpt-5.1"):
        return _STANDARD_EFFORTS, True, 128_000, "max_completion_tokens"
    if lowered.startswith("gpt-5"):
        return ("minimal", *_STANDARD_EFFORTS), False, 128_000, "max_completion_tokens"
    if lowered.startswith("o3-pro"):
        return ("high",), False, 100_000, "max_completion_tokens"
    if lowered.startswith(("o1", "o3", "o4", "o5")):
        return _STANDARD_EFFORTS, False, 100_000, "max_completion_tokens"
    return (), False, None, "max_tokens"


class OpenAIProfile(RuntimeProviderProfile):
    """OpenAI request and usage behavior."""

    def model_traits(self, model: str) -> ModelTraits:
        reasoning_modes, _supports_none, max_output, output_token_param = _model_policy(model)
        return ModelTraits(
            output_token_param=output_token_param,
            max_output_tokens=max_output,
            include_stream_usage_option=True,
            reasoning_modes=reasoning_modes,
            prompt_cache_layout=(
                PROMPT_CACHE_LAYOUT_OPENAI_SYSTEM
                if _supports_explicit_prompt_cache(model)
                else PROMPT_CACHE_LAYOUT_AUTOMATIC_PREFIX
            ),
        )

    def prepare_request(
        self,
        base_kwargs: dict[str, Any],
        context: ProviderRuntimeContext,
        options: ModelCallOptions,
    ) -> dict[str, Any]:
        if _requires_responses_api(context.model):
            raise ValueError(
                f"{context.model} requires the OpenAI Responses API; "
                "Chat Completions is unsupported"
            )
        kwargs = prepare_openai_compatible_request(self, base_kwargs, context, options)
        kwargs.pop("prompt_cache_key", None)
        kwargs.pop("prompt_cache_options", None)
        extra_body = (
            deepcopy(dict(kwargs.get("extra_body")))
            if isinstance(kwargs.get("extra_body"), Mapping)
            else {}
        )
        extra_body.pop("prompt_cache_options", None)
        plan = options.cache_plan
        if plan is not None and plan.enabled and plan.prefix_hash:
            kwargs["prompt_cache_key"] = plan.prefix_hash
            if (
                self.model_traits(context.model).prompt_cache_layout
                == PROMPT_CACHE_LAYOUT_OPENAI_SYSTEM
                and _has_explicit_system_breakpoint(kwargs)
            ):
                extra_body["prompt_cache_options"] = {"mode": "explicit"}
        if extra_body:
            kwargs["extra_body"] = extra_body
        else:
            kwargs.pop("extra_body", None)
        kwargs.pop("reasoning_effort", None)
        config = context.reasoning_config
        modes, supports_none, _max_output, _output_token_param = _model_policy(context.model)
        if config is not None and modes:
            if config.get("enabled") is False and supports_none:
                kwargs["reasoning_effort"] = "none"
            elif config.get("enabled") is True:
                effort = str(config.get("effort") or "")
                if effort not in modes:
                    raise ValueError(f"Unsupported OpenAI reasoning effort: {effort}")
                kwargs["reasoning_effort"] = effort
        return kwargs

    def parse_usage(
        self,
        raw_usage: object,
        context: ProviderRuntimeContext,
        *,
        source: str,
    ) -> UsageRecord | None:
        return parse_openai_compatible_usage(raw_usage, context, source=source)
