# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Import-safe provider metadata contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mclaw.agent.transports.base import ModelCallOptions
    from mclaw.agent.usage import UsageRecord
    from mclaw.providers.runtime import ProviderRuntimeContext


PROMPT_CACHE_LAYOUT_AUTOMATIC_PREFIX = "automatic_prefix"
PROMPT_CACHE_LAYOUT_OPENAI_SYSTEM = "openai_system"
PROMPT_CACHE_LAYOUT_ANTHROPIC_SYSTEM = "anthropic_system"
PROMPT_CACHE_LAYOUT_OPENROUTER_SYSTEM = "openrouter_system"
PROMPT_CACHE_LAYOUT_GEMINI_USER_TAIL = "gemini_user_tail"
PROMPT_CACHE_LAYOUT_OPENROUTER_GEMINI = "openrouter_gemini"
PROMPT_CACHE_LAYOUT_QWEN_SYSTEM = "qwen_system"
PROMPT_CACHE_LAYOUT_XAI_CONVERSATION = "xai_conversation"


def default_models_url(base_url: str, api_mode: str) -> str:
    """Derive the standard model-catalog endpoint for one protocol root."""
    normalized = str(base_url or "").rstrip("/")
    if not normalized:
        return ""
    if api_mode == "anthropic_messages" and not normalized.casefold().endswith("/v1"):
        normalized = f"{normalized}/v1"
    return f"{normalized}/models"


@dataclass(frozen=True)
class SetupProfileEntry:
    """One setup/catalog presentation entry bound to a runtime provider."""

    id: str
    label: str
    runtime_provider: str = ""
    models_dev_provider: str = ""
    kind: str = "api"
    callable: bool = True
    credential_scope: str = "api"
    note: str = ""


@dataclass(frozen=True)
class ModelTraits:
    """Protocol-level traits for one provider model family."""

    output_token_param: str = "max_tokens"
    max_output_tokens: int | None = None
    supports_tools: bool = True
    include_stream_usage_option: bool = False
    requires_stream: bool = False
    content_stream_mode: str = "delta"
    reasoning_stream_mode: str = "delta"
    reasoning_modes: tuple[str, ...] = ()
    prompt_cache_layout: str = ""
    stream_safety_timeout: float | None = None


@dataclass(frozen=True)
class RuntimeProviderProfile:
    """Frozen, process-wide provider identity and behavior declaration."""

    name: str
    display_name: str
    provider_kind: str = "direct"
    api_mode: str = "chat_completions"
    auth_scheme: str = "bearer"
    credential_required: bool = True
    aliases: tuple[str, ...] = ()
    env_vars: tuple[str, ...] = ()
    base_url: str = ""
    base_url_required: bool = False
    base_url_env_var: str = ""
    key_url: str = ""
    models_url: str = ""
    models_dev_provider: str = ""
    setup_profiles: tuple[SetupProfileEntry, ...] = ()
    setup_order: int | None = None
    model_prefixes: tuple[str, ...] = ()
    fallback_models: tuple[str, ...] = ()
    context_limit_markers: tuple[str, ...] = ()

    def normalize_model(self, model: str) -> str:
        """Return the canonical API model id for profiles without a special rule."""
        return model.strip()

    def model_traits(self, model: str) -> ModelTraits:
        """Return default protocol traits for profiles without a family rule."""
        return ModelTraits()

    def prepare_request(
        self,
        base_kwargs: dict[str, Any],
        context: ProviderRuntimeContext,
        options: ModelCallOptions,
    ) -> dict[str, Any]:
        """Apply provider request behavior implemented by concrete profiles."""
        raise NotImplementedError

    def parse_usage(
        self,
        raw_usage: object,
        context: ProviderRuntimeContext,
        *,
        source: str,
    ) -> UsageRecord | None:
        """Normalize protocol-standard usage without importing provider SDKs."""
        from mclaw.agent.usage import (
            parse_anthropic_usage,
            parse_openai_compatible_usage,
        )

        if self.api_mode == "anthropic_messages":
            return parse_anthropic_usage(raw_usage, context, source=source)
        if self.api_mode == "chat_completions":
            return parse_openai_compatible_usage(raw_usage, context, source=source)
        raise ValueError(f"Unsupported provider api_mode: {self.api_mode}")
