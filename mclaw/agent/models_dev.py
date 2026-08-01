# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""models.dev registry integration for provider model metadata.

The module uses an offline-first lookup path: one-hour memory cache, disk cache,
then the public models.dev registry. It resolves provider metadata for context
length detection, setup model choices, and runtime model catalog views.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import requests

from mclaw.constants import get_mclaw_home
from mclaw.providers.registry import PROVIDER_REGISTRY

logger = logging.getLogger(__name__)

MODELS_DEV_URL = "https://models.dev/api.json"
MODELS_DEV_SNAPSHOT_URL = (
    "https://cdn.jsdelivr.net/npm/@opencode-ai/models@latest/dist/snapshot.js"
)
_MODELS_DEV_CACHE_TTL = 3600  # one-hour in-memory cache

# In-memory cache.
_models_dev_cache: dict[str, Any] = {}
_models_dev_cache_time: float = 0


def _providers_data(registry: dict[str, Any]) -> dict[str, Any]:
    """Return the current models.dev root-level provider mapping."""
    return {
        key: value
        for key, value in registry.items()
        if isinstance(value, dict) and isinstance(value.get("models"), dict)
    }


def _iter_models(provider_entry: Any) -> Iterator[dict[str, Any]]:
    """Yield model dictionaries from one current models.dev provider entry."""
    if not isinstance(provider_entry, dict):
        return
    models = provider_entry.get("models", {})
    if not isinstance(models, dict):
        return
    yield from (m for m in models.values() if isinstance(m, dict))


def _model_id(model: dict[str, Any]) -> str:
    return str(model.get("id") or "").lower()


def _coerce_positive_int(value: Any) -> int | None:
    try:
        length = int(value)
    except (TypeError, ValueError):
        return None
    return length if length > 0 else None


def _extract_context_length(model: dict[str, Any]) -> int | None:
    """Extract context length from the current models.dev limit metadata."""
    limit = model.get("limit")
    if isinstance(limit, dict):
        ctx = limit.get("context")
        if ctx:
            return _coerce_positive_int(ctx)
    return None


def _response_json_object(resp: requests.Response) -> dict[str, Any]:
    """Validate the registry protocol boundary before cache mutation."""
    data = resp.json()
    if not isinstance(data, dict):
        raise ValueError("models.dev registry response must be a JSON object")
    return data


def _response_snapshot_object(resp: requests.Response) -> dict[str, Any]:
    """Extract the provider registry from the official npm snapshot module."""
    marker = "JSON.parse("
    start = resp.text.find(marker)
    if start < 0:
        raise ValueError("models.dev npm snapshot is missing JSON.parse payload")
    try:
        encoded, _ = json.JSONDecoder().raw_decode(resp.text[start + len(marker):])
        data = json.loads(encoded)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("models.dev npm snapshot contains invalid JSON") from exc
    providers = data.get("providers") if isinstance(data, dict) else None
    if not isinstance(providers, dict):
        raise ValueError("models.dev npm snapshot is missing provider data")
    return providers


def _fetch_network_registry(timeout: int) -> dict[str, Any]:
    """Fetch the registry from the API, then its official npm snapshot."""
    errors: list[str] = []
    connect_timeout = min(3, timeout)
    for url, decoder in (
        (MODELS_DEV_URL, _response_json_object),
        (MODELS_DEV_SNAPSHOT_URL, _response_snapshot_object),
    ):
        try:
            response = requests.get(url, timeout=(connect_timeout, timeout))
            if response.status_code != 200:
                errors.append(f"{url}: HTTP {response.status_code}")
                continue
            return decoder(response)
        except (requests.RequestException, ValueError) as exc:
            errors.append(f"{url}: {exc}")
    raise ValueError("; ".join(errors))


def _get_cache_path() -> Path:
    """Return the user-scoped models.dev cache path."""
    return get_mclaw_home() / "models_dev_cache.json"


def _load_disk_cache() -> dict[str, Any]:
    """Read the offline registry cache, treating unreadable data as a miss."""
    try:
        path = _get_cache_path()
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("Failed to load models.dev disk cache: %s", exc)
    return {}


def _save_disk_cache(data: dict[str, Any]) -> None:
    """Persist fresh registry data without making lookup callers depend on disk I/O."""
    try:
        path = _get_cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except (OSError, TypeError) as exc:
        logger.debug("Failed to save models.dev disk cache: %s", exc)


def fetch_models_dev(force_refresh: bool = False) -> dict[str, Any]:
    """Fetch models.dev registry.

    Resolution order:
      1. In-memory cache (1hr TTL)
      2. Disk cache in M-Claw home models_dev_cache.json
      3. Network fetch (https://models.dev/api.json)
    """
    global _models_dev_cache, _models_dev_cache_time

    if not force_refresh and _models_dev_cache and (time.time() - _models_dev_cache_time) < _MODELS_DEV_CACHE_TTL:
        return _models_dev_cache

    # Try disk cache before network.
    disk = _load_disk_cache()
    if disk:
        _models_dev_cache = disk
        _models_dev_cache_time = time.time()
        logger.debug("models.dev loaded from disk cache")
        if not force_refresh:
            return disk

    # Then try a short network fetch.
    try:
        data = _fetch_network_registry(timeout=5)
        _models_dev_cache = data
        _models_dev_cache_time = time.time()
        _save_disk_cache(data)
        logger.debug("models.dev fetched from network, cached")
        return data
    except (requests.RequestException, ValueError) as exc:
        logger.debug("models.dev network fetch failed: %s", exc)

    return _models_dev_cache or {}


def refresh_models_dev_cache(*, timeout: int = 10) -> dict[str, Any]:
    """Force-refresh the models.dev disk cache from the network.

    Returns a small status dict suitable for CLI rendering, including whether
    fresh network data replaced the existing cache.
    """
    global _models_dev_cache, _models_dev_cache_time

    path = _get_cache_path()
    try:
        data = _fetch_network_registry(timeout=timeout)
        _models_dev_cache = data
        _models_dev_cache_time = time.time()
        _save_disk_cache(data)
        return {
            "ok": True,
            "cache_path": str(path),
            **_registry_stats(data),
        }
    except (requests.RequestException, ValueError) as exc:
        return {
            "ok": False,
            "error": str(exc),
            "cache_path": str(path),
            **_registry_stats(_models_dev_cache or _load_disk_cache()),
        }


def _registry_stats(registry: dict[str, Any]) -> dict[str, int]:
    """Summarize a registry snapshot for CLI refresh status payloads."""
    providers_data = _providers_data(registry or {})
    model_count = 0
    for provider_data in providers_data.values():
        model_count += sum(1 for _ in _iter_models(provider_data))
    return {
        "providers": len(providers_data),
        "models": model_count,
    }


def lookup_models_dev_context(provider: str, model: str) -> int | None:
    """Look up context_length for a specific (provider, model) pair.

    Returns None if not found in models.dev registry.
    """
    dev_provider = resolve_models_dev_provider(provider)
    registry = fetch_models_dev()
    providers_data = _providers_data(registry)

    model_lower = model.lower()
    for m in _iter_models(providers_data.get(dev_provider, {})):
        if _model_id(m) == model_lower:
            return _extract_context_length(m)
    return None


def resolve_models_dev_provider(provider: str, profile_id: str = "") -> str:
    """Resolve M-Claw provider/profile to a raw models.dev provider id."""
    profile = PROVIDER_REGISTRY.get(provider)
    if not profile:
        return provider
    requested = str(profile_id or "").strip().casefold()
    if requested:
        entry = next(
            (item for item in profile.setup_profiles if item.id.casefold() == requested),
            None,
        )
        if entry:
            if entry.callable and entry.runtime_provider in PROVIDER_REGISTRY:
                return PROVIDER_REGISTRY[entry.runtime_provider].models_dev_provider
            return entry.models_dev_provider
    return profile.models_dev_provider


def list_models_dev_provider(provider_id: str, *, limit: int | None = 20) -> list[str]:
    """Return model IDs for one raw models.dev provider ID."""
    registry = fetch_models_dev()
    providers_data = _providers_data(registry)
    provider_data = providers_data.get(provider_id)
    if not provider_data:
        return []

    models: list[str] = []
    seen = set()
    for m in sorted(
        _iter_models(provider_data),
        key=lambda item: str(item.get("release_date") or ""),
        reverse=True,
    ):
        model_id = str(m.get("id") or "").strip()
        if not model_id:
            continue
        key = model_id.lower()
        if key in seen:
            continue
        seen.add(key)
        models.append(model_id)
        if limit is not None and len(models) >= limit:
            break
    return models


def list_provider_models(provider: str, *, limit: int | None = 20, profile_id: str = "") -> list[str]:
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
