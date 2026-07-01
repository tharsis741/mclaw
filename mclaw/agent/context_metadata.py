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

logger = logging.getLogger(__name__)

DEFAULT_FALLBACK_CONTEXT = 128_000

DEFAULT_CONTEXT_LENGTHS = {
    # Anthropic models.
    "claude-opus-4-6": 1_000_000,
    "claude-sonnet-4-6": 1_000_000,
    "claude": 200_000,
    # OpenAI models.
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-4-turbo": 128_000,
    "gpt-4": 128_000,
    "gpt-3.5-turbo": 16_384,
    # Google models.
    "gemini": 1_048_576,
    "gemma": 8_192,
    # DeepSeek models.
    "deepseek": 128_000,
    # Meta models.
    "llama": 131_072,
    # Qwen models.
    "qwen": 131_072,
    # MiniMax built-ins are treated as 204,800-token contexts.
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
    key = f"{base_url or ''}:{model}".lower()
    entry = cache.get(key, {})
    if isinstance(entry, dict):
        return _coerce_context_length(entry.get("context_length"))
    return None


def save_context_length(model: str, base_url: str, length: int) -> None:
    """Write to persistent cache."""
    cache = _load_length_cache()
    key = f"{base_url or ''}:{model}".lower()
    cache[key] = {"context_length": length, "updated_at": time.time()}
    _save_length_cache(cache)


def get_model_context_length(
    model: str,
    base_url: str = "",
    api_key: str = "",
    provider: str = "",
) -> int:
    """Resolve context length for a model.

    Resolution order:
      1. persistent cache in M-Claw home context_length_cache.yaml
      2. custom endpoint /models for non-built-in endpoints
      3. Anthropic /v1/models API
      4. models.dev registry
      5. DEFAULT_CONTEXT_LENGTHS fuzzy match
      6. default 128K fallback
    """
    model_lower = model.lower()

    # Persistent cache.
    cached = get_cached_context_length(model, base_url)
    if cached:
        return cached

    # Custom endpoint /models, skipped for built-in provider hosts.
    if base_url:
        known_hostnames = [
            "openai.com", "anthropic.com", "openrouter.ai",
            "generativelanguage.googleapis", "deepseek.com",
            "moonshot.ai", "minimax", "dashscope", "bigmodel.cn",
            "api.minimax.chat", "api.minimaxi.com",
        ]
        if not any(known in base_url.lower() for known in known_hostnames):
            try:
                resp = requests.get(
                    f"{base_url.rstrip('/')}/models",
                    timeout=5,
                    headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    entries = data.get("data", []) if isinstance(data, dict) else []
                    for m in entries:
                        if not isinstance(m, dict):
                            continue
                        mid = m.get("id", "").lower()
                        if mid == model_lower or mid.endswith(f"/{model_lower}"):
                            ctx = (
                                m.get("context_length") or m.get("context_window")
                                or m.get("max_tokens") or m.get("max_position_embeddings")
                            )
                            length = _coerce_context_length(ctx)
                            if length:
                                save_context_length(model, base_url, length)
                                return length
            except (requests.RequestException, ValueError) as exc:
                logger.debug("Custom endpoint context probe failed for %s: %s", model, exc)

    # Anthropic /v1/models API.
    if provider == "anthropic" or "claude" in model_lower:
        try:
            resp = requests.get(
                "https://api.anthropic.com/v1/models",
                timeout=5,
                headers={"x-api-key": api_key} if api_key else {},
            )
            if resp.status_code == 200:
                data = resp.json()
                entries = data.get("data", []) if isinstance(data, dict) else []
                for m in entries:
                    if not isinstance(m, dict):
                        continue
                    if m.get("id", "").lower() == model_lower:
                        ctx = m.get("context_length") or m.get("max_tokens")
                        length = _coerce_context_length(ctx)
                        if length:
                            save_context_length(model, base_url, length)
                            return length
        except (requests.RequestException, ValueError) as exc:
            logger.debug("Anthropic context probe failed for %s: %s", model, exc)

    # models.dev registry.
    if provider:
        from mclaw.agent.models_dev import lookup_models_dev_context
        ctx = lookup_models_dev_context(provider, model)
        if ctx:
            save_context_length(model, base_url, ctx)
            return ctx

    # Match the longest keys first so specific variants win.
    for key, length in sorted(DEFAULT_CONTEXT_LENGTHS.items(), key=lambda x: -len(x[0])):
        if key in model_lower:
            save_context_length(model, base_url, length)
            return length

    # Default fallback.
    save_context_length(model, base_url, DEFAULT_FALLBACK_CONTEXT)
    return DEFAULT_FALLBACK_CONTEXT


def _coerce_context_length(value: object) -> int | None:
    try:
        length = int(value)
    except (TypeError, ValueError):
        return None
    return length if length > 0 else None
