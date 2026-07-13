# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Import-safe contracts shared by model transports and the agent core."""

from __future__ import annotations

import json
import math
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Mapping

if TYPE_CHECKING:
    from mclaw.agent.prompt_cache import PromptCachePlan
    from mclaw.agent.usage import UsageRecord
    from mclaw.providers.runtime import ProviderRuntimeContext


_REASONING_SCHEMA_VERSION = 1
_DEFAULT_CONTEXT_LIMIT_MARKERS = (
    "context_length_exceeded",
    "maximum context length",
    "context window",
    "too many tokens",
    "prompt is too long",
    "input is too long",
    "request too large",
    "entity too large",
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[^\s,;\}\]]+")
_SECRET_NAME = (
    r"(?:authorization|proxy-authorization|x-api-key|api[_-]?key|apikey|"
    r"access[_-]?token|token|client[_-]?secret|password|secret)"
)
_QUOTED_SECRET_ASSIGNMENT_RE = re.compile(
    rf"(?i)([\"']?\b{_SECRET_NAME}\b[\"']?\s*[:=]\s*)([\"'])(.*?)(\2)"
)
_SECRET_ASSIGNMENT_RE = re.compile(
    rf"(?i)([\"']?\b{_SECRET_NAME}\b[\"']?\s*[:=]\s*)([^\s,;\}}\]]+)"
)


def _stable_json(value: Any) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def json_safe_value(value: Any) -> Any:
    """Normalize SDK/Pydantic values to deterministic JSON-compatible data."""
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Mapping):
        return {str(key): json_safe_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe_value(item) for item in value]
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            return json_safe_value(dump(mode="json"))
        except TypeError:
            return json_safe_value(dump())
    dump = getattr(value, "to_dict", None)
    if callable(dump):
        return json_safe_value(dump())
    values = getattr(value, "__dict__", None)
    if isinstance(values, dict):
        return {
            str(key): json_safe_value(item)
            for key, item in values.items()
            if not str(key).startswith("_")
        }
    return str(value)


def _json_safe_copy(value: Any) -> Any:
    return json.loads(_stable_json(value))


@dataclass(frozen=True)
class ModelCallOptions:
    stream: bool = False
    timeout: float = 30.0
    max_output_tokens: int | None = None
    temperature: float | None = None
    source: str = "turn"
    dynamic_system_context: str = ""
    cache_plan: PromptCachePlan | None = None


@dataclass(frozen=True)
class ReasoningTrace:
    text: str | None = None
    provider: str = ""
    model: str = ""
    api_mode: str = ""
    format: str = ""
    payload: Any | None = None

    def to_message_fields(self) -> dict[str, Any]:
        return {
            "reasoning": self.text,
            "reasoning_details": {
                "schema_version": _REASONING_SCHEMA_VERSION,
                "provider": self.provider,
                "model": self.model,
                "api_mode": self.api_mode,
                "format": self.format,
                "payload": _json_safe_copy(self.payload),
            },
        }

    def to_budget_text(self) -> str:
        if self.format == "reasoning_content":
            if self.text is not None:
                return self.text
            return self.payload if isinstance(self.payload, str) else ""
        payload_text = "" if self.payload is None else _stable_json(self.payload)
        if self.format == "gemini_thought_signature":
            return "\n".join(part for part in (self.text or "", payload_text) if part)
        return payload_text or (self.text or "")

    def to_replay_text(self) -> str:
        """Return canonical OpenAI-compatible reasoning text when available."""
        if self.format not in {"reasoning_content", "reasoning_details"}:
            return ""
        if self.text:
            return self.text
        if isinstance(self.payload, str):
            return self.payload
        if isinstance(self.payload, list):
            for event in self.payload:
                if (
                    isinstance(event, Mapping)
                    and event.get("field") == "reasoning_content"
                    and isinstance(event.get("value"), str)
                ):
                    return event["value"]
        return ""

    @classmethod
    def from_message(cls, message: dict[str, Any]) -> ReasoningTrace | None:
        raw_text = message.get("reasoning")
        text = None if raw_text is None else str(raw_text)
        details = message.get("reasoning_details")
        if details is None:
            return cls(text=text, format="reasoning_content") if text is not None else None

        if isinstance(details, Mapping) and "schema_version" in details:
            if type(details.get("schema_version")) is not int or details["schema_version"] != 1:
                return cls(text=text, format="reasoning_content") if text is not None else None
            origin_fields = ("provider", "model", "api_mode", "format")
            if any(not isinstance(details.get(name, ""), str) for name in origin_fields):
                return cls(text=text, format="reasoning_content") if text is not None else None
            return cls(
                text=text,
                provider=details.get("provider", ""),
                model=details.get("model", ""),
                api_mode=details.get("api_mode", ""),
                format=details.get("format", ""),
                payload=_json_safe_copy(details.get("payload")),
            )

        return cls(
            text=text,
            format="reasoning_details",
            payload=_json_safe_copy(details),
        )


@dataclass
class ModelCallResult:
    content: str
    tool_calls: list[dict[str, Any]] | None
    finish_reason: str | None
    reasoning: ReasoningTrace | None
    usage: UsageRecord | None
    was_streamed: bool
    provider: str
    model: str
    interrupted: bool = False


@dataclass
class ModelCallError(Exception):
    message: str
    provider: str
    model: str
    retryable: bool = False
    context_limit: bool = False
    rate_limited: bool = False
    retry_after: float | None = None
    status_code: int | None = None
    raw_exception: Exception | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        Exception.__init__(self, self.message)

    def __str__(self) -> str:
        return self.message


class ModelTransport(ABC):
    """One provider-bound protocol adapter; each call performs one SDK attempt."""

    def __init__(self, context: ProviderRuntimeContext, client: Any) -> None:
        self.context = context
        self.client = client

    @abstractmethod
    def call(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        options: ModelCallOptions,
        stream_callback: Callable[[str], None] | None = None,
        interrupted: Callable[[], bool] | None = None,
    ) -> ModelCallResult:
        """Execute exactly one SDK request and return its normalized result."""
        raise NotImplementedError


def _redact_error_message(message: str, api_key: str) -> str:
    redacted = str(message or "")
    if api_key:
        redacted = redacted.replace(api_key, "<redacted>")
    redacted = _BEARER_RE.sub("Bearer <redacted>", redacted)
    redacted = _QUOTED_SECRET_ASSIGNMENT_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}<redacted>{match.group(2)}",
        redacted,
    )
    return _SECRET_ASSIGNMENT_RE.sub(r"\1<redacted>", redacted)


def _status_code(exc: Exception) -> int | None:
    value = getattr(exc, "status_code", None)
    if value is None:
        value = getattr(getattr(exc, "response", None), "status_code", None)
    try:
        status = int(value)
    except (TypeError, ValueError):
        return None
    return status if 100 <= status <= 599 else None


def normalize_model_call_error(
    exc: Exception,
    context: ProviderRuntimeContext,
) -> ModelCallError:
    """Convert one SDK failure into secret-safe, retry-ready transport metadata."""
    from mclaw.agent.retry_utils import (
        RETRYABLE_STATUS_CODES,
        get_retry_after,
        is_retryable_error,
    )

    raw_message = str(exc) or type(exc).__name__
    lowered = raw_message.casefold()
    markers = (*_DEFAULT_CONTEXT_LIMIT_MARKERS, *context.profile.context_limit_markers)
    context_limit = (
        ("context" in lowered and "limit" in lowered)
        or any(str(marker).casefold() in lowered for marker in markers if marker)
    )
    status_code = _status_code(exc)
    try:
        retryable = is_retryable_error(exc)
    except (ImportError, AttributeError, TypeError):
        retryable = False
    retryable = (
        retryable
        or isinstance(exc, TimeoutError)
        or type(exc).__name__.casefold().endswith("timeouterror")
        or status_code in RETRYABLE_STATUS_CODES
    )
    try:
        retry_after = get_retry_after(exc)
    except (AttributeError, TypeError, ValueError):
        retry_after = None
    if retry_after is not None and (retry_after < 0 or not math.isfinite(retry_after)):
        retry_after = None
    rate_limited = status_code == 429 or type(exc).__name__.casefold() == "ratelimiterror"
    return ModelCallError(
        message=_redact_error_message(raw_message, context.api_key),
        provider=context.provider,
        model=context.model,
        retryable=retryable,
        context_limit=context_limit,
        rate_limited=rate_limited,
        retry_after=retry_after,
        status_code=status_code,
        raw_exception=exc,
    )
