# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Import-safe provider-reported usage contract."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:
    from mclaw.providers.runtime import ProviderRuntimeContext


_TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
)
_COUNTER_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
)


@dataclass
class UsageRecord:
    """Token fields reported by one provider response; missing means unknown."""

    provider: str = ""
    model: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None
    source: str = "turn"

    @property
    def available_fields(self) -> tuple[str, ...]:
        return tuple(name for name in _TOKEN_FIELDS if getattr(self, name) is not None)

    def to_counter_delta(self) -> dict[str, int]:
        return {
            name: value
            for name in _COUNTER_FIELDS
            if (value := getattr(self, name)) is not None
        }

    def to_compressor_update(self) -> dict[str, int]:
        mapping = {
            "prompt_tokens": self.input_tokens,
            "completion_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
        }
        return {name: value for name, value in mapping.items() if value is not None}


def usage_field(value: object, name: str) -> Any:
    """Read one provider usage field from either a mapping or SDK object."""
    return value.get(name) if isinstance(value, Mapping) else getattr(value, name, None)


def reported_token(value: object, *names: str) -> int | None:
    """Return the first non-negative integer token value actually reported."""
    for name in names:
        raw = usage_field(value, name)
        if type(raw) is int and raw >= 0:
            return raw
    return None


def parse_openai_compatible_usage(
    raw_usage: object,
    context: ProviderRuntimeContext,
    *,
    source: str,
) -> UsageRecord | None:
    """Normalize only token values present in an OpenAI-compatible response."""
    if raw_usage is None:
        return None
    prompt_details = usage_field(raw_usage, "prompt_tokens_details")
    if prompt_details is None:
        prompt_details = usage_field(raw_usage, "input_tokens_details")
    completion_details = usage_field(raw_usage, "completion_tokens_details")
    if completion_details is None:
        completion_details = usage_field(raw_usage, "output_tokens_details")
    values = {
        "input_tokens": reported_token(raw_usage, "prompt_tokens", "input_tokens"),
        "output_tokens": reported_token(raw_usage, "completion_tokens", "output_tokens"),
        "total_tokens": reported_token(raw_usage, "total_tokens"),
        "cache_read_tokens": reported_token(prompt_details, "cached_tokens"),
        "cache_write_tokens": reported_token(prompt_details, "cache_write_tokens"),
        "reasoning_tokens": reported_token(completion_details, "reasoning_tokens"),
    }
    if all(value is None for value in values.values()):
        return None
    return UsageRecord(
        provider=context.provider,
        model=context.model,
        source=source,
        **values,
    )


def parse_anthropic_usage(
    raw_usage: object,
    context: ProviderRuntimeContext,
    *,
    source: str,
) -> UsageRecord | None:
    """Normalize Anthropic input/cache components without estimating omissions."""
    if raw_usage is None:
        return None
    base_input = reported_token(raw_usage, "input_tokens")
    cache_read = reported_token(raw_usage, "cache_read_input_tokens")
    cache_write = reported_token(raw_usage, "cache_creation_input_tokens")
    input_parts = [
        value
        for value in (base_input, cache_read, cache_write)
        if value is not None
    ]
    values = {
        "input_tokens": sum(input_parts) if input_parts else None,
        "output_tokens": reported_token(raw_usage, "output_tokens"),
        "total_tokens": reported_token(raw_usage, "total_tokens"),
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "reasoning_tokens": reported_token(raw_usage, "reasoning_tokens"),
    }
    if all(value is None for value in values.values()):
        return None
    return UsageRecord(
        provider=context.provider,
        model=context.model,
        source=source,
        **values,
    )
