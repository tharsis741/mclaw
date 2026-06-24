"""models.dev 注册表集成：查询模型上下文长度和模型信息。

本模块采用离线优先策略：内存缓存（1 小时）→ 磁盘缓存 → 网络请求。
数据来源为 https://models.dev/api.json。模块只负责按 (provider, model)
查询 context_length，不负责模型目录展示或供应商识别；后者由 auth.py 处理。
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional

from mclaw.constants import get_mclaw_home

logger = logging.getLogger(__name__)

MODELS_DEV_URL = "https://models.dev/api.json"
_MODELS_DEV_CACHE_TTL = 3600  # 内存缓存 1 小时。

# 内存缓存。
_models_dev_cache: Dict[str, Any] = {}
_models_dev_cache_time: float = 0

# M-Claw 供应商名称到默认 models.dev 供应商 ID 的映射。
# 多接口供应商（例如 Kimi API vs Kimi Coding Plan）由
# mclaw.cli.provider_profiles 提供更细的 profile 映射。
PROVIDER_TO_MODELS_DEV: Dict[str, str] = {
    "openrouter": "openrouter",
    "anthropic": "anthropic",
    "deepseek": "deepseek",
    "baidu": "qianfan",
    "tencent": "hunyuan",
    "xiaomi": "xiaomi",
    "groq": "groq",
    "meta": "llama",
    "mistral": "mistral",
    "microsoft": "azure",
    "cohere": "cohere",
    "amazon": "amazon-bedrock",
    "together": "togetherai",
    "perplexity": "perplexity",
    "fireworks": "fireworks-ai",
    "deepinfra": "deepinfra",
    "moonshot": "moonshotai",
    "minimax": "minimax",
    "minimax-cn": "minimax-cn",
    "zhipu": "zhipuai",
    "qwen": "alibaba",
    "google": "google",
    "openai": "openai",
    "azure": "azure",
    "xai": "xai",
    "gemini": "google",
    "yi": "yi",
    "stepfun": "stepfun",
    "baichuan": "baichuan",
    "doubao": "bytedance",
    "siliconflow": "siliconflow",
    "ollama": "ollama",
}


def _providers_data(registry: Dict[str, Any]) -> Dict[str, Any]:
    """Return provider mapping for both old and current models.dev shapes."""
    merged: Dict[str, Any] = {}
    for key, value in registry.items():
        if key == "providers":
            continue
        if isinstance(value, list) or (isinstance(value, dict) and "models" in value):
            merged[key] = value

    providers = registry.get("providers")
    if isinstance(providers, dict):
        merged.update(providers)
    return merged


def _iter_models(provider_entry: Any):
    """Yield model dicts from old list shape or current dict shape."""
    if isinstance(provider_entry, dict):
        models = provider_entry.get("models", [])
        if isinstance(models, dict):
            yield from (m for m in models.values() if isinstance(m, dict))
        elif isinstance(models, list):
            yield from (m for m in models if isinstance(m, dict))
    elif isinstance(provider_entry, list):
        yield from (m for m in provider_entry if isinstance(m, dict))


def _model_id(model: Dict[str, Any]) -> str:
    return str(model.get("id") or model.get("model_id") or "").lower()


def _extract_context_length(model: Dict[str, Any]) -> Optional[int]:
    """Extract context length from historical and current metadata shapes."""
    direct = (
        model.get("context_length")
        or model.get("context_window")
        or model.get("max_tokens")
        or model.get("max_position_embeddings")
        or model.get("max_input_tokens")
    )
    if direct:
        return int(direct)

    limit = model.get("limit")
    if isinstance(limit, dict):
        ctx = limit.get("context") or limit.get("context_length") or limit.get("input")
        if ctx:
            return int(ctx)

    limits = model.get("limits")
    if isinstance(limits, dict):
        ctx = limits.get("context") or limits.get("context_length") or limits.get("input")
        if ctx:
            return int(ctx)

    return None


def _get_cache_path() -> Path:
    return get_mclaw_home() / "models_dev_cache.json"


def _load_disk_cache() -> Dict[str, Any]:
    try:
        path = _get_cache_path()
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.debug("Failed to load models.dev disk cache: %s", e)
    return {}


def _save_disk_cache(data: Dict[str, Any]) -> None:
    try:
        path = _get_cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.debug("Failed to save models.dev disk cache: %s", e)


def fetch_models_dev(force_refresh: bool = False) -> Dict[str, Any]:
    """Fetch models.dev registry.

    Resolution order:
      1. In-memory cache (1hr TTL)
      2. Disk cache in M-Claw home models_dev_cache.json
      3. Network fetch (https://models.dev/api.json)
    """
    global _models_dev_cache, _models_dev_cache_time

    if not force_refresh and _models_dev_cache and (time.time() - _models_dev_cache_time) < _MODELS_DEV_CACHE_TTL:
        return _models_dev_cache

    # 优先尝试磁盘缓存。
    disk = _load_disk_cache()
    if disk:
        _models_dev_cache = disk
        _models_dev_cache_time = time.time()
        logger.debug("models.dev loaded from disk cache")

    # 再尝试网络请求。
    try:
        import requests
        resp = requests.get(MODELS_DEV_URL, timeout=3)
        if resp.status_code == 200:
            data = resp.json()
            _models_dev_cache = data
            _models_dev_cache_time = time.time()
            _save_disk_cache(data)
            logger.debug("models.dev fetched from network, cached")
            return data
    except Exception as e:
        logger.debug("models.dev network fetch failed: %s", e)

    return _models_dev_cache or {}


def refresh_models_dev_cache(*, timeout: int = 10) -> Dict[str, Any]:
    """Force-refresh the models.dev disk cache from the network.

    Returns a small status dict suitable for CLI rendering.  Unlike
    ``fetch_models_dev(force_refresh=True)``, this function reports whether the
    network refresh actually succeeded instead of silently falling back to stale
    cache data.
    """
    global _models_dev_cache, _models_dev_cache_time

    path = _get_cache_path()
    try:
        import requests
        resp = requests.get(MODELS_DEV_URL, timeout=timeout)
        if resp.status_code != 200:
            return {
                "ok": False,
                "error": f"HTTP {resp.status_code}",
                "cache_path": str(path),
                **_registry_stats(_models_dev_cache or _load_disk_cache()),
            }
        data = resp.json()
        _models_dev_cache = data
        _models_dev_cache_time = time.time()
        _save_disk_cache(data)
        return {
            "ok": True,
            "cache_path": str(path),
            **_registry_stats(data),
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
            "cache_path": str(path),
            **_registry_stats(_models_dev_cache or _load_disk_cache()),
        }


def _registry_stats(registry: Dict[str, Any]) -> Dict[str, int]:
    providers_data = _providers_data(registry or {})
    model_count = 0
    for provider_data in providers_data.values():
        model_count += sum(1 for _ in _iter_models(provider_data))
    return {
        "providers": len(providers_data),
        "models": model_count,
    }


def lookup_models_dev_context(provider: str, model: str) -> Optional[int]:
    """Look up context_length for a specific (provider, model) pair.

    Returns None if not found in models.dev registry.
    """
    dev_provider = resolve_models_dev_provider(provider)
    registry = fetch_models_dev()
    providers_data = _providers_data(registry)

    # models.dev structure: {"providers": {"openai": {"models": [...]}}}
    model_lower = model.lower()
    for m in _iter_models(providers_data.get(dev_provider, {})):
        if _model_id(m) == model_lower:
            return _extract_context_length(m)
    return None


def get_model_info_any_provider(model_id: str) -> Optional[Dict[str, Any]]:
    """Search all providers in models.dev for a model by ID.

    Returns the raw model dict or None.
    """
    registry = fetch_models_dev()
    providers_data = _providers_data(registry)
    model_lower = model_id.lower()
    for provider_data in providers_data.values():
        for m in _iter_models(provider_data):
            if _model_id(m) == model_lower:
                return m
    return None


def resolve_models_dev_provider(provider: str, profile_id: str = "") -> str:
    """Resolve M-Claw provider/profile to a raw models.dev provider id."""
    try:
        from mclaw.cli.provider_profiles import resolve_models_dev_provider as _resolve_profile_provider

        return _resolve_profile_provider(provider, profile_id)
    except Exception:
        return PROVIDER_TO_MODELS_DEV.get(provider, provider)


def list_models_dev_provider(provider_id: str, *, limit: int = 20) -> list[str]:
    """Return model IDs for one raw models.dev provider ID."""
    registry = fetch_models_dev()
    providers_data = _providers_data(registry)
    provider_data = providers_data.get(provider_id)
    if not provider_data:
        return []

    models: list[str] = []
    seen = set()
    for m in _iter_models(provider_data):
        model_id = str(m.get("id") or m.get("model_id") or "").strip()
        if not model_id:
            continue
        key = model_id.lower()
        if key in seen:
            continue
        seen.add(key)
        models.append(model_id)
        if len(models) >= limit:
            break
    return models


def list_provider_models(provider: str, *, limit: int = 20, profile_id: str = "") -> list[str]:
    """Return model IDs for one provider from the current models.dev registry.

    ``provider`` may be an M-Claw provider key or a raw models.dev provider ID.
    The function is cache-backed through ``fetch_models_dev`` and returns an
    empty list when the provider is not present in the registry.
    """
    dev_provider = resolve_models_dev_provider(provider, profile_id)
    return list_models_dev_provider(dev_provider, limit=limit)


def list_models_dev_provider_ids() -> list[str]:
    """Return all provider IDs currently known by the cached models.dev registry."""
    registry = fetch_models_dev()
    providers_data = _providers_data(registry)
    return sorted(providers_data.keys())
