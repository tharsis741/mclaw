# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""CLI credential helpers and registry-derived provider views."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from mclaw.cli.config import get_env_value
from mclaw.providers.normalization import normalize_provider_key
from mclaw.providers.registry import PROVIDER_REGISTRY as _RUNTIME_PROVIDER_REGISTRY
from mclaw.providers.resolver import ProviderResolutionError, resolve_provider_runtime_context

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProviderConfig:
    """CLI presentation view of one runtime provider profile."""

    name: str
    display_name: str
    api_key_env_vars: list[str]
    base_url: str
    base_url_required: bool = False
    base_url_env_var: str = ""
    api_mode: str = "chat_completions"
    key_url: str = ""
    model_prefixes: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)


def _provider_config(name: str) -> ProviderConfig:
    profile = _RUNTIME_PROVIDER_REGISTRY[name]
    return ProviderConfig(
        name=profile.name,
        display_name=profile.display_name,
        api_key_env_vars=list(profile.env_vars),
        base_url=profile.base_url,
        base_url_required=profile.base_url_required,
        base_url_env_var=profile.base_url_env_var,
        api_mode=profile.api_mode,
        key_url=profile.key_url,
        model_prefixes=list(profile.model_prefixes),
        aliases=list(profile.aliases),
    )


PROVIDER_CONFIGS: dict[str, ProviderConfig] = {
    name: _provider_config(name) for name in _RUNTIME_PROVIDER_REGISTRY
}

# Compatibility alias for existing CLI consumers. This is a derived view, not
# a second metadata source.
PROVIDER_REGISTRY = PROVIDER_CONFIGS

DEFAULT_PROVIDER_MODELS: dict[str, list[str]] = {
    name: list(profile.fallback_models)
    for name, profile in _RUNTIME_PROVIDER_REGISTRY.items()
    if profile.fallback_models
}


def detect_provider_for_model(model_name: str) -> str | None:
    """Return the unique direct provider matching a model prefix."""
    model = str(model_name or "").casefold()
    if not model:
        return None
    matches = {
        name
        for name, profile in _RUNTIME_PROVIDER_REGISTRY.items()
        if profile.provider_kind == "direct"
        and any(model.startswith(prefix.casefold()) for prefix in profile.model_prefixes)
    }
    return next(iter(matches)) if len(matches) == 1 else None


def resolve_api_key(provider_name: str) -> str | None:
    """Resolve a built-in provider key from its ordered environment aliases."""
    profile = _RUNTIME_PROVIDER_REGISTRY.get(provider_name)
    if not profile:
        return None
    return next((value for env in profile.env_vars if (value := get_env_value(env))), None)


def resolve_base_url(provider_name: str) -> str:
    """Resolve a built-in endpoint, honoring its approved environment override."""
    profile = _RUNTIME_PROVIDER_REGISTRY.get(provider_name)
    if not profile:
        return ""
    return (get_env_value(profile.base_url_env_var) if profile.base_url_env_var else "") or profile.base_url


def _configured_providers(config: dict | None = None) -> dict[str, dict]:
    if not isinstance(config, dict):
        return {}
    providers = config.get("providers", {})
    return providers if isinstance(providers, dict) else {}


def _resolve_user_provider(provider_name: str, config: dict | None = None) -> dict[str, str] | None:
    provider_config = _configured_providers(config).get(provider_name)
    if not isinstance(provider_config, dict):
        return None
    env_var = str(provider_config.get("api_key_env") or "")
    return {
        "provider": provider_name,
        "model": str(provider_config.get("model") or ""),
        "api_key": (get_env_value(env_var) if env_var else "") or "",
        "base_url": str(provider_config.get("base_url") or "").rstrip("/"),
        "api_mode": str(provider_config.get("api_mode") or "chat_completions"),
    }


def _resolve_config_profile(provider_name: str, config: dict | None = None) -> str:
    if provider_name not in PROVIDER_CONFIGS or not isinstance(config, dict):
        return ""
    active_provider = normalize_provider_key(str(config.get("active_provider") or ""), _configured_providers(config))
    if active_provider and active_provider != provider_name:
        return ""
    profile_id = str(config.get("active_provider_profile") or "").strip()
    if not profile_id:
        return ""
    try:
        from mclaw.cli.provider_profiles import find_provider_profile

        profile = find_provider_profile(provider_name, profile_id)
        return profile.id if profile and profile.callable else ""
    except (ImportError, AttributeError, TypeError, ValueError) as exc:
        logger.debug("Provider profile resolution failed for %s: %s", provider_name, exc)
        return ""


def _fallback_provider_entries(config: dict | None = None) -> list[dict[str, str]]:
    if not isinstance(config, dict) or not isinstance(config.get("fallback_providers"), list):
        return []
    entries: list[dict[str, str]] = []
    for raw in config["fallback_providers"]:
        if not isinstance(raw, dict):
            continue
        provider = str(raw.get("provider") or "").strip()
        model = str(raw.get("model") or "").strip()
        if provider and model:
            entries.append({"provider": provider, "model": model})
    return entries


def _configured_model_for_provider(config: dict | None, provider_name: str) -> str:
    if not isinstance(config, dict):
        return ""
    active = normalize_provider_key(str(config.get("active_provider") or ""), _configured_providers(config))
    if active == provider_name and config.get("model"):
        return str(config["model"]).strip()
    for entry in _fallback_provider_entries(config):
        if normalize_provider_key(entry["provider"], _configured_providers(config)) == provider_name:
            return entry["model"]
    return ""


def list_configured_providers() -> list[dict[str, Any]]:
    """Return custom and built-in providers that currently have credentials."""
    from mclaw.cli.config import load_config

    config = load_config(strict=True)
    user_providers = _configured_providers(config)
    result: list[dict[str, Any]] = []
    for name, raw in user_providers.items():
        resolved = _resolve_user_provider(name, config) or {}
        if resolved.get("api_key"):
            result.append({
                "name": name,
                "display_name": str(raw.get("display_name") or name),
                "has_key": True,
                "model": _configured_model_for_provider(config, name) or resolved.get("model", ""),
                "api_mode": str(raw.get("api_mode") or "chat_completions"),
            })
    for name, profile in _RUNTIME_PROVIDER_REGISTRY.items():
        if name not in user_providers and resolve_api_key(name):
            result.append({
                "name": name,
                "display_name": profile.display_name,
                "has_key": True,
                "model": _configured_model_for_provider(config, name),
                "api_mode": profile.api_mode,
            })
    return result


def _result(
    provider: str,
    model: str,
    api_key: str,
    base_url: str,
    api_mode: str,
    provider_profile: str = "",
) -> dict[str, str]:
    return {
        "provider": provider,
        "model": model,
        "api_key": api_key,
        "base_url": base_url.rstrip("/"),
        "api_mode": api_mode,
        "provider_profile": provider_profile,
    }


def resolve_provider(
    model: str = "",
    provider: str = "",
    base_url: str = "",
    api_key: str = "",
    config: dict | None = None,
) -> dict[str, str]:
    """Compatibility dict view over the provider-layer runtime resolver."""
    user_providers = _configured_providers(config)
    normalized_provider = normalize_provider_key(provider, user_providers)
    setup_profile_id = _resolve_config_profile(normalized_provider, config)
    try:
        context = resolve_provider_runtime_context(
            model=model,
            provider=normalized_provider,
            setup_profile_id=setup_profile_id,
            base_url=base_url,
            api_key=api_key,
            config=config,
        )
    except ProviderResolutionError as exc:
        logger.debug("Provider runtime resolution failed (%s): %s", exc.code, exc)
        if exc.code in {
            "invalid_provider_config",
            "invalid_base_url",
            "invalid_api_mode",
            "invalid_reasoning_config",
            "invalid_snapshot",
        }:
            raise
        selected = exc.provider or normalized_provider
        profile = _RUNTIME_PROVIDER_REGISTRY.get(selected)
        user = _resolve_user_provider(selected, config) or {}
        return _result(
            selected if exc.code in {"missing_credential", "missing_model"} else "",
            model or _configured_model_for_provider(config, selected) or user.get("model", ""),
            api_key or (resolve_api_key(selected) if profile else user.get("api_key", "")) or "",
            base_url or (resolve_base_url(selected) if profile else user.get("base_url", "")),
            profile.api_mode if profile else user.get("api_mode", "chat_completions"),
        )
    return _result(
        context.provider,
        context.model,
        context.api_key,
        context.base_url,
        context.api_mode,
    )
