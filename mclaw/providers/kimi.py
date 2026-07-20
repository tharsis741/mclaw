# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Moonshot Kimi request and usage behavior."""

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


def _family(model: str) -> str:
    lowered = model.strip().casefold()
    if lowered == "kimi-k3":
        return "kimi-k3"
    if lowered.startswith("kimi-k2.7-code"):
        return "kimi-k2.7-code"
    for name in ("kimi-k2.6", "kimi-k2.5"):
        if lowered.startswith(name):
            return name
    if lowered.startswith("kimi-k2-thinking"):
        return "kimi-k2-thinking"
    return lowered


def _thinking_policy(model: str) -> str:
    family = _family(model)
    if family == "kimi-k3":
        return "always_preserved"
    if family == "kimi-k2.7-code":
        return "always_preserved"
    if family == "kimi-k2.6":
        return "toggle_preserved"
    if family == "kimi-k2.5":
        return "toggle"
    if family == "kimi-k2-thinking":
        return "always"
    return "none"


class MoonshotKimiProfile(RuntimeProviderProfile):
    """Moonshot/Kimi request and usage behavior."""

    def normalize_model(self, model: str) -> str:
        model = model.strip()
        for prefix in ("moonshot/", "moonshotai/"):
            if model.casefold().startswith(prefix):
                return model[len(prefix):]
        return model

    def model_traits(self, model: str) -> ModelTraits:
        family = _family(self.normalize_model(model))
        policy = _thinking_policy(family)
        current = policy != "none"
        return ModelTraits(
            output_token_param="max_completion_tokens" if current else "max_tokens",
            # Use provider defaults as request budgets; the advertised hard
            # limits can consume the entire context window.
            max_output_tokens=(
                131_072 if family == "kimi-k3" else 32_768 if current else None
            ),
            include_stream_usage_option=True,
            reasoning_modes=(
                ("low", "high", "max")
                if family == "kimi-k3"
                else ("high",) if current else ()
            ),
            prompt_cache_layout=PROMPT_CACHE_LAYOUT_AUTOMATIC_PREFIX,
            stream_safety_timeout=7200.0 if family == "kimi-k3" else None,
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
        kwargs.pop("prompt_cache_key", None)
        plan = options.cache_plan
        if plan is not None and plan.enabled and plan.conversation_key:
            kwargs["prompt_cache_key"] = plan.conversation_key
        family = _family(context.model)
        policy = _thinking_policy(family)
        config = context.reasoning_config
        thinking = policy.startswith("always") or (
            policy.startswith("toggle")
            and (config is None or config.get("enabled") is True)
        )

        kwargs.pop("reasoning_effort", None)
        if family == "kimi-k3" and config is not None:
            if config.get("enabled") is not True:
                raise ValueError("Kimi K3 reasoning cannot be disabled")
            effort = str(config.get("effort") or "")
            if effort not in {"low", "high", "max"}:
                raise ValueError(f"Unsupported Kimi K3 reasoning effort: {effort}")
            kwargs["reasoning_effort"] = effort

        if policy != "none":
            for name in ("temperature", "top_p", "presence_penalty", "frequency_penalty", "n"):
                kwargs.pop(name, None)
            if kwargs.get("tools"):
                kwargs["tool_choice"] = "auto"

        matching = [
            reasoning_trace_matches(trace, context, self._same_family)
            for trace in traces
        ]
        extra_body = deepcopy(kwargs.get("extra_body")) if isinstance(kwargs.get("extra_body"), Mapping) else {}
        extra_body.pop("thinking", None)
        if policy in {"toggle", "toggle_preserved"}:
            native = {"type": "enabled" if thinking else "disabled"}
            if policy == "toggle_preserved" and thinking and any(matching):
                native["keep"] = "all"
            extra_body["thinking"] = native
        if extra_body:
            kwargs["extra_body"] = extra_body
        else:
            kwargs.pop("extra_body", None)

        preserve_all = policy in {"always_preserved", "toggle_preserved"}
        for message, trace, matches in zip(
            reasoning_replay_messages(kwargs["messages"], traces),
            traces,
            matching,
        ):
            if (
                thinking
                and matches
                and isinstance(message, dict)
                and (preserve_all or message.get("tool_calls"))
                and trace is not None
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
        cache_read = reported_token(raw_usage, "cached_tokens")
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
