# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Google Gemini OpenAI-compatible behavior."""

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
)
from mclaw.providers.base import (
    PROMPT_CACHE_LAYOUT_GEMINI_USER_TAIL,
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


def _canonical_model(model: str) -> str:
    value = model.strip()
    for prefix in ("google/", "models/"):
        if value.casefold().startswith(prefix):
            return value[len(prefix):]
    return value


def _policy(model: str) -> tuple[str, tuple[str, ...], bool]:
    value = _canonical_model(model).casefold()
    dated = r"(?:-\d{2}-(?:\d{2}|\d{4}))?"
    if re.fullmatch(rf"gemini-3\.5-flash(?:-(?:latest|\d{{3}}|preview{dated}|exp{dated}))?", value):
        return "gemini-3.5-flash", ("minimal", "low", "medium", "high"), False
    if re.fullmatch(rf"gemini-3\.1-pro(?:-preview{dated})?", value):
        return "gemini-3.1-pro", ("minimal", "low", "medium", "high"), False
    if re.fullmatch(rf"gemini-3\.1-flash-lite(?:-preview{dated})?", value):
        return "gemini-3.1-flash-lite", ("minimal", "low", "medium", "high"), False
    if re.fullmatch(rf"gemini-3-flash(?:-(?:latest|\d{{3}}|preview{dated}))?", value):
        return "gemini-3-flash", ("minimal", "low", "medium", "high"), False
    match = re.fullmatch(
        rf"gemini-2\.5-(pro|flash|flash-lite)(?:-(?:latest|\d{{3}}|preview{dated}))?",
        value,
    )
    if match:
        family = f"gemini-2.5-{match.group(1)}"
        return family, ("minimal", "low", "medium", "high"), family != "gemini-2.5-pro"
    return "", (), False


def _valid_signature(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    google = value.get("google")
    return (
        isinstance(google, Mapping)
        and isinstance(google.get("thought_signature"), str)
        and bool(google["thought_signature"])
    )


def _signature_events(trace: ReasoningTrace) -> dict[int, dict[str, Any]]:
    if trace.format not in {"reasoning_details", "gemini_thought_signature"}:
        return {}
    if _valid_signature(trace.payload):
        return {0: deepcopy(dict(trace.payload))}
    if not isinstance(trace.payload, list):
        return {}
    result: dict[int, dict[str, Any]] = {}
    for event in trace.payload:
        if (
            isinstance(event, Mapping)
            and event.get("field") == "tool_calls.extra_content"
            and type(event.get("index")) is int
            and _valid_signature(event.get("value"))
        ):
            result[event["index"]] = deepcopy(dict(event["value"]))
    return result


class GoogleGeminiProfile(RuntimeProviderProfile):
    """Gemini OpenAI-compatible request, signature, and usage behavior."""

    def normalize_model(self, model: str) -> str:
        return _canonical_model(model)

    def model_traits(self, model: str) -> ModelTraits:
        family, modes, _can_disable = _policy(model)
        return ModelTraits(
            max_output_tokens=65_536 if modes else None,
            reasoning_modes=modes,
            prompt_cache_layout=(
                PROMPT_CACHE_LAYOUT_GEMINI_USER_TAIL if family else ""
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
        family, modes, can_disable = _policy(context.model)

        extra = kwargs.get("extra_body")
        extra = deepcopy(dict(extra)) if isinstance(extra, Mapping) else {}
        google = extra.get("google")
        google = deepcopy(dict(google)) if isinstance(google, Mapping) else {}
        config = context.reasoning_config
        if not modes:
            kwargs.pop("reasoning_effort", None)
            google.pop("thinking_config", None)
        elif config is not None:
            google.pop("thinking_config", None)
            kwargs.pop("reasoning_effort", None)
            if config.get("enabled") is True:
                kwargs["reasoning_effort"] = config["effort"]
            elif can_disable:
                kwargs["reasoning_effort"] = "none"
        elif google.get("thinking_config") is not None:
            kwargs.pop("reasoning_effort", None)
        if google:
            extra["google"] = google
        else:
            extra.pop("google", None)
        if extra:
            kwargs["extra_body"] = extra
        else:
            kwargs.pop("extra_body", None)

        if not family.startswith("gemini-3"):
            return kwargs
        for message, trace in zip(reasoning_replay_messages(kwargs["messages"], traces), traces):
            if (
                not isinstance(message, dict)
                or not reasoning_trace_matches(trace, context, self._same_family)
            ):
                continue
            tool_calls = message.get("tool_calls")
            if not isinstance(tool_calls, list):
                continue
            for index, value in _signature_events(trace).items():
                if 0 <= index < len(tool_calls) and isinstance(tool_calls[index], dict):
                    tool_calls[index]["extra_content"] = value
        return kwargs

    def parse_usage(
        self,
        raw_usage: object,
        context: ProviderRuntimeContext,
        *,
        source: str,
    ) -> UsageRecord | None:
        record = parse_openai_compatible_usage(raw_usage, context, source=source)
        local = {
            "input_tokens": reported_token(
                raw_usage, "prompt_token_count", "promptTokenCount"
            ),
            "output_tokens": reported_token(
                raw_usage, "candidates_token_count", "candidatesTokenCount"
            ),
            "total_tokens": reported_token(
                raw_usage, "total_token_count", "totalTokenCount"
            ),
            "cache_read_tokens": reported_token(
                raw_usage,
                "cached_content_token_count",
                "cachedContentTokenCount",
                "total_cached_tokens",
                "totalCachedTokens",
            ),
            "reasoning_tokens": reported_token(
                raw_usage, "thoughts_token_count", "thoughtsTokenCount"
            ),
        }
        if record is None:
            if all(value is None for value in local.values()):
                return None
            return UsageRecord(
                provider=context.provider,
                model=context.model,
                source=source,
                **local,
            )
        return replace(
            record,
            **{
                name: value
                for name, value in local.items()
                if getattr(record, name) is None and value is not None
            },
        )

    @staticmethod
    def _same_family(origin: str, current: str) -> bool:
        origin_family, _modes, _disabled = _policy(origin)
        current_family, _modes, _disabled = _policy(current)
        return bool(origin_family) and origin_family == current_family
