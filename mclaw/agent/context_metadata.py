"""Context length resolution with 8-level fallback + runtime error probe.

Resolution order:
  0. config explicit override
  1. persistent cache in M-Claw home context_length_cache.yaml
  2. custom endpoint /models metadata (only for truly custom endpoints)
  3. Anthropic /v1/models API
  4. models.dev registry (core — 4000+ models)
  5. hardcoded DEFAULT_CONTEXT_LENGTHS fuzzy match
  6. Error probe (runtime parse from API error) — caller invokes this after overflow error
  7. default 128K fallback
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Optional

import yaml

from mclaw.constants import get_mclaw_home

logger = logging.getLogger(__name__)

CONTEXT_PROBE_TIERS = [128_000, 64_000, 32_000, 16_000, 8_000]
DEFAULT_FALLBACK_CONTEXT = 128_000

DEFAULT_CONTEXT_LENGTHS = {
    # Anthropic 模型。
    "claude-opus-4-6": 1_000_000,
    "claude-sonnet-4-6": 1_000_000,
    "claude": 200_000,
    # OpenAI 模型。
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-4-turbo": 128_000,
    "gpt-4": 128_000,
    "gpt-3.5-turbo": 16_384,
    # Google 模型。
    "gemini": 1_048_576,
    "gemma": 8_192,
    # DeepSeek 模型。
    "deepseek": 128_000,
    # Meta 模型。
    "llama": 131_072,
    # Qwen 模型。
    "qwen": 131_072,
    # MiniMax：当前内置模型上下文长度按 204,800 处理。
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
    # GLM 模型。
    "glm": 202_752,
    # Kimi 模型。
    "kimi": 262_144,
    # 本地/Ollama 模型。
    "llama3.1:8b": 131_072,
    "llama3.1:70b": 131_072,
    "qwen2.5:14b": 131_072,
    "mistral:7b": 32_768,
}

_CACHE_PATH = get_mclaw_home() / "context_length_cache.yaml"


def _load_length_cache() -> dict:
    try:
        if _CACHE_PATH.exists():
            return yaml.safe_load(_CACHE_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        pass
    return {}


def _save_length_cache(cache: dict) -> None:
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(yaml.dump(cache, allow_unicode=True), encoding="utf-8")
    except Exception:
        pass


def get_cached_context_length(model: str, base_url: str) -> Optional[int]:
    """Read from persistent cache. Key = (base_url, model)."""
    cache = _load_length_cache()
    key = f"{base_url or ''}:{model}".lower()
    entry = cache.get(key, {})
    if isinstance(entry, dict):
        return entry.get("context_length")
    return None


def save_context_length(model: str, base_url: str, length: int) -> None:
    """Write to persistent cache."""
    cache = _load_length_cache()
    key = f"{base_url or ''}:{model}".lower()
    cache[key] = {"context_length": length, "updated_at": time.time()}
    _save_length_cache(cache)


def parse_context_limit_from_error(error_msg: str) -> Optional[int]:
    """Extract context limit from API error.

    Examples:
      "maximum context length is 32768 tokens" → 32768
      "context limit: 200000 tokens" → 200000
    """
    patterns = [
        r"maximum context.*?(\d{3,7})\s*tokens?",
        r"context.*?limit.*?(\d{3,7})\s*tokens?",
        r"(\d{5,7})\s*tokens?\s*(?:maximum|limit)",
    ]
    for pat in patterns:
        m = re.search(pat, error_msg, re.IGNORECASE)
        if m:
            val = int(m.group(1))
            if 1000 <= val <= 10_000_000:
                return val
    return None


def get_next_probe_tier(current: int) -> Optional[int]:
    """Return the next lower tier from CONTEXT_PROBE_TIERS."""
    for tier in sorted(CONTEXT_PROBE_TIERS, reverse=True):
        if tier < current:
            return tier
    return None


def get_model_context_length(
    model: str,
    base_url: str = "",
    api_key: str = "",
    config_context_length: Optional[int] = None,
    provider: str = "",
) -> int:
    """Resolve context length for a model.

    Resolution order:
      0. config explicit override
      1. persistent cache in M-Claw home context_length_cache.yaml
      2. custom endpoint /models (only for truly custom endpoints)
      3. Anthropic /v1/models API
      4. models.dev registry (core — 4000+ models)
      5. hardcoded DEFAULT_CONTEXT_LENGTHS fuzzy match
      6. Error probe (caller should invoke after API overflow error)
      7. default 128K fallback
    """
    # Level 0: explicit config override
    if config_context_length and config_context_length > 0:
        return config_context_length

    model_lower = model.lower()

    # Level 1: persistent cache
    cached = get_cached_context_length(model, base_url)
    if cached:
        return cached

    # Level 2: custom endpoint /models (only when base_url looks custom)
    # 跳过已知供应商，避免不必要的 API 调用。
    if base_url:
        known_hostnames = [
            "openai.com", "anthropic.com", "openrouter.ai",
            "generativelanguage.googleapis", "deepseek.com",
            "moonshot.ai", "minimax", "dashscope", "bigmodel.cn",
            "api.minimax.chat", "api.minimaxi.com",
        ]
        if not any(known in base_url.lower() for known in known_hostnames):
            try:
                import requests
                resp = requests.get(
                    f"{base_url.rstrip('/')}/models",
                    timeout=5,
                    headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
                )
                if resp.status_code == 200:
                    for m in resp.json().get("data", []):
                        mid = m.get("id", "").lower()
                        if mid == model_lower or mid.endswith(f"/{model_lower}"):
                            ctx = (
                                m.get("context_length") or m.get("context_window")
                                or m.get("max_tokens") or m.get("max_position_embeddings")
                            )
                            if ctx:
                                save_context_length(model, base_url, int(ctx))
                                return int(ctx)
            except Exception:
                pass

    # Level 3: Anthropic /v1/models API
    if provider == "anthropic" or "claude" in model_lower:
        try:
            import requests
            resp = requests.get(
                "https://api.anthropic.com/v1/models",
                timeout=5,
                headers={"x-api-key": api_key} if api_key else {},
            )
            if resp.status_code == 200:
                for m in resp.json().get("data", []):
                    if m.get("id", "").lower() == model_lower:
                        ctx = m.get("context_length") or m.get("max_tokens")
                        if ctx:
                            save_context_length(model, base_url, int(ctx))
                            return int(ctx)
        except Exception:
            pass

    # Level 4: models.dev (core)
    if provider:
        from mclaw.agent.models_dev import lookup_models_dev_context
        ctx = lookup_models_dev_context(provider, model)
        if ctx:
            save_context_length(model, base_url, ctx)
            return ctx

    # Level 5: hardcoded DEFAULT_CONTEXT_LENGTHS fuzzy match
    # 按 key 长度倒序匹配，优先命中最具体的 key。
    for key, length in sorted(DEFAULT_CONTEXT_LENGTHS.items(), key=lambda x: -len(x[0])):
        if key in model_lower:
            save_context_length(model, base_url, length)
            return length

    # 第 6 层：错误探测，调用方应在溢出后调用 parse_context_limit_from_error。
    # 第 7 层：默认降级值。
    save_context_length(model, base_url, DEFAULT_FALLBACK_CONTEXT)
    return DEFAULT_FALLBACK_CONTEXT
