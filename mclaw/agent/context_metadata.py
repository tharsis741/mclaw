# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Resolve model context windows from cache, providers, and fallbacks.

The agent needs a conservative context length before it can decide when to
compact history. This module resolves that value from persistent cache,
provider APIs, the models.dev registry, fuzzy built-in defaults, and finally
a 128K fallback.
"""

from __future__ import annotations

import logging
import time

import requests
import yaml

from mclaw.constants import get_mclaw_home
from mclaw.providers.base import RuntimeProviderProfile, default_models_url
from mclaw.providers.registry import PROVIDER_REGISTRY
from mclaw.providers.runtime import ProviderRuntimeContext

logger = logging.getLogger(__name__)

DEFAULT_FALLBACK_CONTEXT = 128_000

DEFAULT_CONTEXT_LENGTHS = {
    # Anthropic models.
    "claude-fable-5": 1_000_000,
    "claude-mythos-5": 1_000_000,
    "claude-mythos-preview": 1_000_000,
    "claude-opus-4-8": 1_000_000,
    "claude-sonnet-5": 1_000_000,
    "claude-haiku-4-5": 200_000,
    "claude-opus-4-6": 1_000_000,
    "claude-sonnet-4-6": 1_000_000,
    "claude": 200_000,
    # OpenAI models.
    "gpt-5.6": 1_050_000,
    "gpt-5.5": 1_050_000,
    "gpt-5.4": 1_050_000,
    "gpt-5.2": 400_000,
    "gpt-5.1": 400_000,
    "gpt-5": 400_000,
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-4-turbo": 128_000,
    "gpt-4": 128_000,
    "gpt-3.5-turbo": 16_384,
    "o3": 200_000,
    "o4": 200_000,
    "o1": 200_000,
    # Google models.
    "gemini": 1_048_576,
    "gemma": 8_192,
    # DeepSeek models.
    "deepseek-v4-pro": 1_000_000,
    "deepseek-v4-flash": 1_000_000,
    "deepseek-chat": 1_000_000,
    "deepseek-reasoner": 1_000_000,
    "deepseek": 128_000,
    # Meta models.
    "llama": 131_072,
    # Qwen models.
    "qwen3.7": 1_000_000,
    "qwen3.6": 1_000_000,
    "qwen3.5": 1_000_000,
    "qwen": 131_072,
    # Xiaomi MiMo V2.5 text generation models.
    "mimo-v2.5-pro": 1_048_576,
    "mimo-v2.5": 1_048_576,
    # MiniMax current family context windows.
    "minimax-m3": 1_000_000,
    "minimax-m1-256k": 204_800,
    "minimax-m1-128k": 204_800,
    "minimax-m1-80k": 204_800,
    "minimax-m1-40k": 204_800,
    "minimax-m1": 204_800,
    "minimax-m2": 204_800,
    "minimax-m2.1": 204_800,
    "minimax-m2.1-highspeed": 204_800,
    "minimax-m2.5": 204_800,
    "minimax-m2.5-highspeed": 204_800,
    "minimax-m2.7": 204_800,
    "minimax-m2.7-highspeed": 204_800,
    "minimax": 204_800,
    # GLM models.
    "glm-5.1": 200_000,
    "glm-5": 200_000,
    "glm-4.7": 200_000,
    "glm-4.6": 200_000,
    "glm": 202_752,
    # Kimi models.
    "kimi": 262_144,
    # Local/Ollama models.
    "llama3.1:8b": 131_072,
    "llama3.1:70b": 131_072,
    "qwen2.5:14b": 131_072,
    "mistral:7b": 32_768,
}

_CACHE_PATH = get_mclaw_home() / "context_length_cache.yaml"
_CACHE_KEY_VERSION = "v2"


def _load_length_cache() -> dict[str, object]:
    try:
        if _CACHE_PATH.exists():
            data = yaml.safe_load(_CACHE_PATH.read_text(encoding="utf-8")) or {}
            return data if isinstance(data, dict) else {}
    except (OSError, yaml.YAMLError) as exc:
        logger.debug("Failed to load context length cache: %s", exc)
    return {}


def _save_length_cache(cache: dict[str, object]) -> None:
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(yaml.dump(cache, allow_unicode=True), encoding="utf-8")
    except (OSError, TypeError, yaml.YAMLError) as exc:
        logger.debug("Failed to save context length cache: %s", exc)


def get_cached_context_length(model: str, base_url: str) -> int | None:
    """Read from persistent cache. Key = (base_url, model)."""
    cache = _load_length_cache()
    key = f"{_CACHE_KEY_VERSION}:{base_url or ''}:{model}".lower()
    entry = cache.get(key, {})
    if isinstance(entry, dict):
        return _coerce_context_length(entry.get("context_length"))
    return None


def save_context_length(model: str, base_url: str, length: int) -> None:
    """Write to persistent cache."""
    cache = _load_length_cache()
    key = f"{_CACHE_KEY_VERSION}:{base_url or ''}:{model}".lower()
    cache[key] = {"context_length": length, "updated_at": time.time()}
    _save_length_cache(cache)


def _probe_provider_model_entries(
    profile: RuntimeProviderProfile,
    *,
    base_url: str,
    api_key: str,
) -> list[dict[str, object]] | None:
    runtime_base_url = base_url.rstrip("/")
    profile_base_url = profile.base_url.rstrip("/")
    if (
        profile.models_url
        and (
            not runtime_base_url
            or not profile_base_url
            or runtime_base_url.casefold() == profile_base_url.casefold()
        )
    ):
        url = profile.models_url
    else:
        url = default_models_url(
            runtime_base_url or profile_base_url,
            profile.api_mode,
        )
    if not url:
        return None
    if profile.auth_scheme == "bearer":
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    elif profile.auth_scheme == "anthropic_x_api_key":
        headers = {"anthropic-version": "2023-06-01"}
        if api_key:
            headers["x-api-key"] = api_key
    else:
        logger.debug("Unsupported model probe auth scheme: %s", profile.auth_scheme)
        return None
    try:
        response = requests.get(url, timeout=10, headers=headers)
        if response.status_code != 200:
            return None
        payload = response.json()
        entries = payload.get("data") if isinstance(payload, dict) else None
        return [entry for entry in entries if isinstance(entry, dict)] if isinstance(entries, list) else None
    except (requests.RequestException, ValueError) as exc:
        logger.debug("Provider model probe failed for %s: %s", profile.name, exc)
        return None


def probe_provider_models(
    profile: RuntimeProviderProfile,
    *,
    base_url: str,
    api_key: str,
) -> list[str] | None:
    """Return provider model IDs, or ``None`` when the endpoint is unreachable."""
    entries = _probe_provider_model_entries(profile, base_url=base_url, api_key=api_key)
    if entries is None:
        return None
    return [
        model_id.strip()
        for entry in entries
        if isinstance((model_id := entry.get("id")), str) and model_id.strip()
    ]


def resolve_context_length(context: ProviderRuntimeContext) -> int:
    """Resolve context length from one fully bound provider runtime."""
    model = context.model
    cache_base_url = context.safe_base_url
    if cached := get_cached_context_length(model, cache_base_url):
        return cached

    entries = _probe_provider_model_entries(
        context.profile,
        base_url=context.base_url,
        api_key=context.api_key,
    )
    model_lower = model.casefold()
    for entry in entries or ():
        raw_model_id = entry.get("id")
        if not isinstance(raw_model_id, str):
            continue
        model_id = raw_model_id.casefold()
        if model_id != model_lower and not model_id.endswith(f"/{model_lower}"):
            continue
        raw_length = (
            entry.get("context_length")
            or entry.get("context_window")
            or entry.get("max_input_tokens")
            or entry.get("max_position_embeddings")
        )
        length = _coerce_context_length(raw_length)
        if length is None and context.api_mode != "anthropic_messages":
            length = _coerce_context_length(entry.get("max_tokens"))
        if length:
            save_context_length(model, cache_base_url, length)
            return length

    if context.profile.models_dev_provider:
        from mclaw.agent.models_dev import lookup_models_dev_context

        if length := lookup_models_dev_context(context.profile.models_dev_provider, model):
            save_context_length(model, cache_base_url, length)
            return length

    for key, length in sorted(DEFAULT_CONTEXT_LENGTHS.items(), key=lambda item: -len(item[0])):
        if key in model_lower:
            save_context_length(model, cache_base_url, length)
            return length

    save_context_length(model, cache_base_url, DEFAULT_FALLBACK_CONTEXT)
    return DEFAULT_FALLBACK_CONTEXT


def _coerce_context_length(value: object) -> int | None:
    try:
        length = int(value)
    except (TypeError, ValueError):
        return None
    return length if length > 0 else None
