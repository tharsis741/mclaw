# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Provider selection and secret-safe runtime context restoration."""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass, replace
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

from mclaw.constants import parse_reasoning_effort
from mclaw.providers.base import RuntimeProviderProfile, default_models_url
from mclaw.providers.generic import (
    GenericAnthropicCompatibleProfile,
    GenericOpenAICompatibleProfile,
)
from mclaw.providers.normalization import normalize_provider_key, reserved_provider_collision
from mclaw.providers.registry import PROVIDER_REGISTRY
from mclaw.providers.runtime import ProviderRuntimeContext

_API_MODES = {"chat_completions", "anthropic_messages"}
_BASE_URL_SOURCES = {"profile", "provider_env", "provider_config", "explicit", "snapshot"}
_SNAPSHOT_KEYS = {
    "schema_version", "provider", "model", "safe_base_url", "base_url_source",
    "api_mode", "auth_source", "api_key_fingerprint", "reasoning_config",
}
_FINGERPRINT_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_DNS_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CUSTOM_ENV = {
    "custom": {
        "urls": ("MCLAW_BASE_URL", "OPENAI_BASE_URL"),
        "keys": ("MCLAW_API_KEY", "OPENAI_API_KEY"),
        "api_mode": "chat_completions",
    },
    "custom_anthropic": {
        "urls": ("MCLAW_ANTHROPIC_BASE_URL",),
        "keys": ("MCLAW_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"),
        "api_mode": "anthropic_messages",
    },
}


@dataclass
class ProviderResolutionError(ValueError):
    code: str
    message: str
    provider: str = ""
    key_env_var: str = ""
    key_url: str = ""

    def __post_init__(self) -> None:
        ValueError.__init__(self, self.message)

    def __str__(self) -> str:
        return self.message


def _fail(
    code: str,
    message: str,
    *,
    provider: str = "",
    key_env_var: str = "",
    key_url: str = "",
) -> ProviderResolutionError:
    return ProviderResolutionError(code, message, provider, key_env_var, key_url)


def _configured_providers(config: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    raw = config.get("providers", {}) if isinstance(config, dict) else {}
    if not isinstance(raw, dict):
        raise _fail("invalid_provider_config", "config.providers must be an object")
    providers: dict[str, dict[str, Any]] = {}
    folded_names: dict[str, str] = {}
    compact_names: dict[str, str] = {}
    for raw_name, raw_config in raw.items():
        name = str(raw_name or "").strip()
        if not name or not isinstance(raw_config, dict):
            raise _fail("invalid_provider_config", "Each configured provider must be a named object")
        if collision := reserved_provider_collision(name):
            raise _fail(
                "invalid_provider_config",
                f"Configured provider '{name}' collides with reserved provider '{collision}'",
                provider=name,
            )
        folded = name.casefold()
        if folded in folded_names and folded_names[folded] != name:
            raise _fail(
                "invalid_provider_config",
                f"Configured providers '{folded_names[folded]}' and '{name}' are case-insensitively ambiguous",
                provider=name,
            )
        folded_names[folded] = name
        key_env = raw_config.get("api_key_env")
        if not isinstance(key_env, str) or not _ENV_NAME_RE.fullmatch(key_env):
            raise _fail(
                "invalid_provider_config",
                f"Configured provider '{name}' requires a valid api_key_env",
                provider=name,
            )
        display_name = str(raw_config.get("display_name") or name)
        if display_collision := reserved_provider_collision(display_name):
            raise _fail(
                "invalid_provider_config",
                f"Configured provider display name '{display_name}' collides with reserved provider '{display_collision}'",
                provider=name,
            )
        for identity in (name, display_name):
            compact = "".join(char for char in identity.casefold() if char.isalnum())
            owner = compact_names.get(compact)
            if owner and owner != name:
                raise _fail(
                    "invalid_provider_config",
                    f"Configured provider identity '{identity}' is ambiguous with '{owner}'",
                    provider=name,
                )
            compact_names[compact] = name
        providers[name] = raw_config
    return providers


def _validate_api_mode(value: object, *, provider: str) -> str:
    api_mode = str(value or "chat_completions").strip()
    if api_mode not in _API_MODES:
        raise _fail(
            "invalid_api_mode",
            f"Provider '{provider}' has unsupported api_mode '{api_mode}'",
            provider=provider,
        )
    return api_mode


def _validate_base_url(value: object, *, provider: str) -> str:
    raw = str(value or "").strip()
    if (
        not raw
        or "?" in raw
        or "#" in raw
        or "\\" in raw
        or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in raw)
    ):
        raise _fail("invalid_base_url", f"Provider '{provider}' requires a valid base URL", provider=provider)
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise _fail("invalid_base_url", f"Invalid base URL for provider '{provider}'", provider=provider) from exc
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        raise _fail("invalid_base_url", f"Provider '{provider}' base URL must use http(s) and include a host", provider=provider)
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(char.isspace() for char in parsed.netloc)
    ):
        raise _fail("invalid_base_url", f"Provider '{provider}' base URL cannot contain credentials, query, or fragment", provider=provider)
    host = parsed.hostname.casefold()
    try:
        ipaddress.ip_address(host)
    except ValueError:
        try:
            ascii_host = host.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise _fail("invalid_base_url", f"Provider '{provider}' base URL host is invalid", provider=provider) from exc
        if not ascii_host or any(not _DNS_LABEL_RE.fullmatch(label) for label in ascii_host.split(".")):
            raise _fail("invalid_base_url", f"Provider '{provider}' base URL host is invalid", provider=provider)
        host = ascii_host
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = f"{host}:{port}" if port is not None else host
    return urlunsplit((parsed.scheme.casefold(), netloc, parsed.path.rstrip("/"), "", ""))


def _is_local_url(base_url: str) -> bool:
    host = (urlsplit(base_url).hostname or "").casefold()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _custom_endpoint_from_env(custom: Mapping[str, object]) -> str:
    """Read the first approved custom-endpoint environment candidate."""
    candidates = custom.get("urls", ())
    if not isinstance(candidates, tuple):
        return ""
    return next(
        (value for name in candidates if isinstance(name, str) and (value := os.environ.get(name, ""))),
        "",
    )


def _dynamic_profile(
    *,
    name: str,
    display_name: str,
    api_mode: str,
    base_url: str,
    env_vars: tuple[str, ...],
    credential_required: bool,
    models_dev_provider: str = "",
) -> RuntimeProviderProfile:
    profile_type = (
        GenericAnthropicCompatibleProfile
        if api_mode == "anthropic_messages"
        else GenericOpenAICompatibleProfile
    )
    return profile_type(
        name=name,
        display_name=display_name,
        provider_kind="host",
        api_mode=api_mode,
        auth_scheme="anthropic_x_api_key" if api_mode == "anthropic_messages" else "bearer",
        credential_required=credential_required,
        env_vars=env_vars,
        base_url=base_url,
        base_url_required=True,
        models_url=default_models_url(base_url, api_mode),
        models_dev_provider=models_dev_provider,
    )


def _credential(
    profile: RuntimeProviderProfile,
    explicit_key: str,
    *,
    preferred_source: str = "",
    snapshot: bool = False,
) -> tuple[str, str]:
    if explicit_key:
        return explicit_key, "explicit"
    candidates = profile.env_vars
    if preferred_source and preferred_source != "explicit" and preferred_source not in candidates:
        code = "invalid_snapshot" if snapshot else "invalid_provider_config"
        raise _fail(code, f"Credential source '{preferred_source}' is not allowed for provider '{profile.name}'", provider=profile.name)
    ordered = ([preferred_source] if preferred_source in candidates else []) + [
        name for name in candidates if name != preferred_source
    ]
    for env_var in ordered:
        if value := os.environ.get(env_var, ""):
            return value, env_var
    if profile.credential_required:
        key_env_var = candidates[0] if candidates else ""
        raise _fail(
            "missing_credential",
            f"Missing credential for provider '{profile.name}'",
            provider=profile.name,
            key_env_var=key_env_var,
            key_url=profile.key_url,
        )
    return "", ""


def _configured_model(config: dict[str, Any] | None, provider: str, user_providers: dict[str, Any]) -> str:
    if not isinstance(config, dict):
        return ""
    active = normalize_provider_key(str(config.get("active_provider") or ""), user_providers)
    if active == provider and config.get("model"):
        return str(config["model"]).strip()
    for raw in config.get("fallback_providers", []) if isinstance(config.get("fallback_providers"), list) else []:
        if not isinstance(raw, dict):
            continue
        if normalize_provider_key(str(raw.get("provider") or ""), user_providers) == provider:
            return str(raw.get("model") or "").strip()
    provider_config = user_providers.get(provider)
    return str(provider_config.get("model") or "").strip() if isinstance(provider_config, dict) else ""


def default_model_for_provider(
    provider: str,
    *,
    config: dict[str, Any] | None = None,
) -> str:
    """Return the canonical built-in profile's first fallback model."""
    user_providers = _configured_providers(config)
    canonical = normalize_provider_key(str(provider or ""), user_providers)
    profile = PROVIDER_REGISTRY.get(canonical)
    return profile.fallback_models[0] if profile and profile.fallback_models else ""


def _reasoning_config(
    value: Mapping[str, Any] | None,
    *,
    config: dict[str, Any] | None,
    profile: RuntimeProviderProfile,
    model: str,
) -> dict[str, Any] | None:
    if value is None:
        reasoning = config.get("reasoning", {}) if isinstance(config, dict) else {}
        if not isinstance(reasoning, dict) or set(reasoning) - {"effort"}:
            raise _fail("invalid_reasoning_config", "config.reasoning must contain only effort", provider=profile.name)
        effort = reasoning.get("effort", "")
        if effort:
            parsed = parse_reasoning_effort(str(effort))
            if parsed is None:
                raise _fail("invalid_reasoning_config", f"Invalid reasoning effort '{effort}'", provider=profile.name)
            value = parsed
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) - {"enabled", "effort"}:
        raise _fail("invalid_reasoning_config", "reasoning_config must contain only enabled and effort", provider=profile.name)
    enabled = value.get("enabled")
    effort = value.get("effort", "")
    if not isinstance(enabled, bool) or not isinstance(effort, str):
        raise _fail("invalid_reasoning_config", "reasoning_config has invalid field types", provider=profile.name)
    normalized_effort = effort.strip().casefold()
    if not enabled:
        if normalized_effort:
            raise _fail("invalid_reasoning_config", "Disabled reasoning cannot include effort", provider=profile.name)
        return {"enabled": False}
    parsed = parse_reasoning_effort(normalized_effort)
    modes = profile.model_traits(model).reasoning_modes
    canonical_effort = str(parsed.get("effort") or "") if parsed else ""
    if not parsed or not parsed.get("enabled") or canonical_effort not in modes:
        raise _fail(
            "invalid_reasoning_config",
            f"Reasoning effort '{effort}' is unsupported for {profile.name}/{model}",
            provider=profile.name,
        )
    return {"enabled": True, "effort": canonical_effort}


def _profile_and_endpoint(
    provider: str,
    *,
    user_providers: dict[str, dict[str, Any]],
    base_url: str,
) -> tuple[RuntimeProviderProfile, str, str]:
    if provider in PROVIDER_REGISTRY:
        profile = PROVIDER_REGISTRY[provider]
        if base_url:
            return profile, _validate_base_url(base_url, provider=provider), "explicit"
        if profile.base_url_env_var and (override := os.environ.get(profile.base_url_env_var, "")):
            return profile, _validate_base_url(override, provider=provider), "provider_env"
        if profile.base_url:
            return profile, _validate_base_url(profile.base_url, provider=provider), "profile"
        raise _fail("invalid_base_url", f"Provider '{provider}' requires a base URL", provider=provider)

    if provider in user_providers:
        raw = user_providers[provider]
        api_mode = _validate_api_mode(raw.get("api_mode"), provider=provider)
        base_env = str(raw.get("base_url_env") or "").strip()
        if base_url:
            endpoint, source = base_url, "explicit"
        elif base_env and os.environ.get(base_env):
            endpoint, source = os.environ[base_env], "provider_env"
        else:
            endpoint, source = raw.get("base_url", ""), "provider_config"
        endpoint = _validate_base_url(endpoint, provider=provider)
        key_env = str(raw.get("api_key_env") or "").strip()
        profile = _dynamic_profile(
            name=provider,
            display_name=str(raw.get("display_name") or provider),
            api_mode=api_mode,
            base_url=endpoint,
            env_vars=(key_env,) if key_env else (),
            credential_required=True,
            models_dev_provider=str(raw.get("models_dev_provider") or provider),
        )
        return profile, endpoint, source

    if provider in _CUSTOM_ENV:
        custom = _CUSTOM_ENV[provider]
        endpoint_source = "explicit" if base_url else "provider_env"
        endpoint = _validate_base_url(
            base_url or _custom_endpoint_from_env(custom),
            provider=provider,
        )
        profile = _dynamic_profile(
            name=provider,
            display_name="Custom Anthropic" if provider.endswith("anthropic") else "Custom OpenAI-compatible",
            api_mode=str(custom["api_mode"]),
            base_url=endpoint,
            env_vars=tuple(custom["keys"]),
            credential_required=not _is_local_url(endpoint),
        )
        return profile, endpoint, endpoint_source

    raise _fail("unknown_provider", f"Unknown provider '{provider}'", provider=provider)


def _bind_setup_profile(provider: str, setup_profile_id: str) -> str:
    if not setup_profile_id:
        return provider
    root = PROVIDER_REGISTRY.get(provider)
    if not root:
        raise _fail("unknown_provider", f"Unknown setup provider '{provider}'", provider=provider)
    requested = setup_profile_id.strip().casefold()
    entry = next((item for item in root.setup_profiles if item.id.casefold() == requested), None)
    if not entry or not entry.callable or entry.runtime_provider not in PROVIDER_REGISTRY:
        raise _fail(
            "invalid_provider_config",
            f"Setup profile '{setup_profile_id}' is not callable for provider '{provider}'",
            provider=provider,
        )
    return entry.runtime_provider


def resolve_provider_runtime_context(
    *,
    model: str = "",
    provider: str = "",
    setup_profile_id: str = "",
    base_url: str = "",
    api_key: str = "",
    reasoning_config: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
) -> ProviderRuntimeContext:
    """Resolve provider/model intent to one immutable, live runtime context."""
    user_providers = _configured_providers(config)
    raw_provider = str(provider or "").strip()
    provider = normalize_provider_key(raw_provider, user_providers)
    endpoint_from_openai_env = False
    if not provider and isinstance(config, dict) and config.get("active_provider"):
        raw_provider = str(config["active_provider"]).strip()
        provider = normalize_provider_key(raw_provider, user_providers)
    if setup_profile_id:
        if not provider:
            raise _fail("provider_required", "A provider is required for setup_profile_id")
        provider = _bind_setup_profile(provider, setup_profile_id)

    if not provider:
        if base_url:
            provider = "custom"
        elif os.environ.get("MCLAW_ANTHROPIC_BASE_URL"):
            provider = "custom_anthropic"
        elif os.environ.get("MCLAW_BASE_URL"):
            provider = "custom"
        elif os.environ.get("OPENAI_BASE_URL"):
            provider = "custom"
            base_url = os.environ["OPENAI_BASE_URL"]
            endpoint_from_openai_env = True
        elif model:
            lowered = model.casefold()
            matches = {
                name
                for name, profile in PROVIDER_REGISTRY.items()
                if profile.provider_kind == "direct"
                and any(lowered.startswith(prefix.casefold()) for prefix in profile.model_prefixes)
            }
            if len(matches) == 1:
                provider = next(iter(matches))
            else:
                raise _fail(
                    "provider_required",
                    f"Provider is required for model '{model}'",
                )
        else:
            candidates: list[tuple[str, str]] = []
            fallback = config.get("fallback_providers", []) if isinstance(config, dict) else []
            if isinstance(fallback, list):
                for raw in fallback:
                    if isinstance(raw, dict):
                        candidates.append((str(raw.get("provider") or ""), str(raw.get("model") or "")))
            candidates.extend(
                (name, str(raw.get("model") or "")) for name, raw in user_providers.items()
            )
            candidates.extend(
                (profile.name, profile.fallback_models[0])
                for profile in sorted(
                    (item for item in PROVIDER_REGISTRY.values() if item.setup_order is not None and item.fallback_models),
                    key=lambda item: item.setup_order,
                )
            )
            for candidate_provider, candidate_model in candidates:
                if not candidate_provider or not candidate_model:
                    continue
                try:
                    return resolve_provider_runtime_context(
                        provider=candidate_provider,
                        model=candidate_model,
                        config=config,
                    )
                except ProviderResolutionError as exc:
                    if exc.code in {"missing_credential", "invalid_base_url"}:
                        continue
                    raise
            raise _fail("provider_required", "No configured provider with a model and credential is available")

    if raw_provider and provider not in user_providers and provider not in PROVIDER_REGISTRY and provider not in _CUSTOM_ENV:
        raise _fail("unknown_provider", f"Unknown provider '{raw_provider}'", provider=raw_provider)

    profile, endpoint, endpoint_source = _profile_and_endpoint(
        provider,
        user_providers=user_providers,
        base_url=base_url,
    )
    if endpoint_from_openai_env:
        endpoint_source = "provider_env"
    model = str(model or "").strip() or _configured_model(config, provider, user_providers)
    if not model and raw_provider and setup_profile_id:
        model = _configured_model(config, raw_provider, user_providers)
    model = profile.normalize_model(model)
    if not model:
        raise _fail("missing_model", f"Missing model for provider '{provider}'", provider=provider)
    key, auth_source = _credential(profile, str(api_key or ""))
    reasoning = _reasoning_config(
        reasoning_config,
        config=config,
        profile=profile,
        model=model,
    )
    return ProviderRuntimeContext(
        profile=profile,
        model=model,
        api_key=key,
        base_url=endpoint,
        base_url_source=endpoint_source,
        auth_source=auth_source,
        reasoning_config=reasoning,
    )


def _validated_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        raise _fail("invalid_snapshot", "Provider runtime snapshot must be an object")
    if not snapshot:
        return {}
    if set(snapshot) != _SNAPSHOT_KEYS or type(snapshot.get("schema_version")) is not int or snapshot["schema_version"] != 1:
        raise _fail("invalid_snapshot", "Provider runtime snapshot has an invalid schema")
    for name in _SNAPSHOT_KEYS - {"schema_version", "reasoning_config"}:
        if not isinstance(snapshot.get(name), str):
            raise _fail("invalid_snapshot", f"Snapshot field '{name}' must be a string")
    if not snapshot["provider"].strip() or not snapshot["model"].strip():
        raise _fail("invalid_snapshot", "Snapshot provider and model are required")
    if snapshot["base_url_source"] not in _BASE_URL_SOURCES:
        raise _fail("invalid_snapshot", "Snapshot base_url_source is invalid")
    try:
        _validate_api_mode(snapshot["api_mode"], provider=snapshot["provider"])
    except ProviderResolutionError as exc:
        raise _fail("invalid_snapshot", "Snapshot api_mode is invalid", provider=snapshot["provider"]) from exc
    fingerprint = snapshot["api_key_fingerprint"]
    if fingerprint and not _FINGERPRINT_RE.fullmatch(fingerprint):
        raise _fail("invalid_snapshot", "Snapshot API key fingerprint is invalid")
    if snapshot["safe_base_url"]:
        try:
            _validate_base_url(snapshot["safe_base_url"], provider=snapshot["provider"])
        except ProviderResolutionError as exc:
            raise _fail("invalid_snapshot", "Snapshot safe_base_url is invalid", provider=snapshot["provider"]) from exc
    if snapshot["reasoning_config"] is not None and not isinstance(snapshot["reasoning_config"], dict):
        raise _fail("invalid_snapshot", "Snapshot reasoning_config must be an object or null")
    return snapshot


def restore_provider_runtime_context(
    snapshot: dict[str, Any],
    *,
    config: dict[str, Any] | None = None,
    model: str = "",
    provider: str = "",
    base_url: str = "",
    api_key: str = "",
) -> ProviderRuntimeContext:
    """Restore a persisted selector using current metadata and credentials."""
    snapshot = _validated_snapshot(snapshot)
    if not snapshot:
        return resolve_provider_runtime_context(
            model=model,
            provider=provider,
            base_url=base_url,
            api_key=api_key,
            config=config,
        )

    user_providers = _configured_providers(config)
    snapshot_provider = normalize_provider_key(snapshot["provider"], user_providers)
    overlay_provider = normalize_provider_key(provider, user_providers) if provider else ""
    if overlay_provider and overlay_provider != snapshot_provider:
        if not model:
            raise _fail("missing_model", "Changing provider during restore requires a model", provider=overlay_provider)
        return resolve_provider_runtime_context(
            model=model,
            provider=overlay_provider,
            base_url=base_url,
            api_key=api_key,
            reasoning_config=snapshot["reasoning_config"],
            config=config,
        )
    effective_provider = overlay_provider or snapshot_provider
    effective_model = model or snapshot["model"]

    static_profile = PROVIDER_REGISTRY.get(effective_provider)
    user_config = user_providers.get(effective_provider)
    custom = _CUSTOM_ENV.get(effective_provider)
    snapshot_only_dynamic = not static_profile and not user_config and not custom

    if static_profile:
        profile = static_profile
        allowed_auth = profile.env_vars
    elif user_config:
        api_mode = _validate_api_mode(user_config.get("api_mode"), provider=effective_provider)
        key_env = str(user_config.get("api_key_env") or "").strip()
        provisional_url = str(base_url or snapshot["safe_base_url"] or user_config.get("base_url"))
        profile = _dynamic_profile(
            name=effective_provider,
            display_name=str(user_config.get("display_name") or effective_provider),
            api_mode=api_mode,
            base_url=_validate_base_url(provisional_url, provider=effective_provider),
            env_vars=(key_env,) if key_env else (),
            credential_required=True,
            models_dev_provider=str(user_config.get("models_dev_provider") or effective_provider),
        )
        allowed_auth = profile.env_vars
    elif custom:
        provisional_url = str(snapshot["safe_base_url"] or _custom_endpoint_from_env(custom))
        provisional_url = _validate_base_url(provisional_url, provider=effective_provider)
        profile = _dynamic_profile(
            name=effective_provider,
            display_name="Custom Anthropic" if effective_provider.endswith("anthropic") else "Custom OpenAI-compatible",
            api_mode=str(custom["api_mode"]),
            base_url=provisional_url,
            env_vars=tuple(custom["keys"]),
            credential_required=not _is_local_url(provisional_url),
        )
        allowed_auth = profile.env_vars
    else:
        api_mode = _validate_api_mode(snapshot["api_mode"], provider=effective_provider)
        provisional_url = _validate_base_url(snapshot["safe_base_url"], provider=effective_provider)
        profile = _dynamic_profile(
            name=effective_provider,
            display_name=effective_provider,
            api_mode=api_mode,
            base_url=provisional_url,
            env_vars=(),
            credential_required=True,
        )
        allowed_auth = ()

    snapshot_auth = snapshot["auth_source"]
    if not api_key and snapshot_auth and snapshot_auth != "explicit" and snapshot_auth not in allowed_auth:
        raise _fail("invalid_snapshot", f"Snapshot credential source '{snapshot_auth}' is not allowed", provider=effective_provider)
    if snapshot_only_dynamic and not api_key:
        raise _fail("missing_credential", f"Snapshot-only provider '{effective_provider}' requires an explicit credential", provider=effective_provider)

    source = "explicit" if base_url else snapshot["base_url_source"]
    if base_url:
        endpoint = _validate_base_url(base_url, provider=effective_provider)
    elif source == "profile":
        if not static_profile or not profile.base_url:
            raise _fail("invalid_snapshot", "Snapshot profile endpoint source is unavailable", provider=effective_provider)
        endpoint = _validate_base_url(profile.base_url, provider=effective_provider)
    elif source == "provider_env":
        env_name = ""
        if static_profile:
            env_name = profile.base_url_env_var
        elif user_config:
            env_name = str(user_config.get("base_url_env") or "")
        elif custom:
            env_names = custom.get("urls", ())
            env_name = next(
                (name for name in env_names if isinstance(name, str) and os.environ.get(name)),
                str(env_names[0]) if isinstance(env_names, tuple) and env_names else "",
            )
        if not env_name:
            raise _fail("invalid_snapshot", "Snapshot endpoint environment source is not allowed", provider=effective_provider)
        current_endpoint = (
            _custom_endpoint_from_env(custom)
            if custom
            else os.environ.get(env_name, "")
        )
        endpoint = _validate_base_url(current_endpoint or snapshot["safe_base_url"], provider=effective_provider)
    elif source == "provider_config":
        if user_config:
            endpoint = _validate_base_url(user_config.get("base_url") or snapshot["safe_base_url"], provider=effective_provider)
        elif snapshot_only_dynamic:
            endpoint = _validate_base_url(snapshot["safe_base_url"], provider=effective_provider)
        else:
            raise _fail("invalid_snapshot", "Snapshot provider_config endpoint source is not allowed", provider=effective_provider)
    else:
        endpoint = _validate_base_url(snapshot["safe_base_url"], provider=effective_provider)

    if not static_profile:
        credential_required = profile.credential_required
        if custom:
            credential_required = not _is_local_url(endpoint)
        profile = _dynamic_profile(
            name=profile.name,
            display_name=profile.display_name,
            api_mode=profile.api_mode,
            base_url=endpoint,
            env_vars=profile.env_vars,
            credential_required=credential_required,
            models_dev_provider=profile.models_dev_provider,
        )
    key, auth_source = _credential(
        profile,
        str(api_key or ""),
        preferred_source=snapshot_auth,
        snapshot=True,
    )
    normalized_model = profile.normalize_model(str(effective_model).strip())
    if not normalized_model:
        raise _fail("missing_model", f"Missing model for provider '{effective_provider}'", provider=effective_provider)
    reasoning = _reasoning_config(
        snapshot["reasoning_config"],
        config=config,
        profile=profile,
        model=normalized_model,
    )
    return ProviderRuntimeContext(
        profile=profile,
        model=normalized_model,
        api_key=key,
        base_url=endpoint,
        base_url_source=source,
        auth_source=auth_source,
        reasoning_config=reasoning,
    )


def restore_session_runtime_context(
    snapshot: dict[str, Any],
    *,
    row_model: str = "",
    fallback_context: ProviderRuntimeContext,
    config: dict[str, Any] | None = None,
) -> ProviderRuntimeContext:
    """Restore a session snapshot or bootstrap it from one live fallback context."""
    if not isinstance(fallback_context, ProviderRuntimeContext):
        raise TypeError("fallback_context must be a ProviderRuntimeContext")
    if snapshot:
        same_provider = False
        if isinstance(snapshot, dict):
            user_providers = _configured_providers(config)
            snapshot_provider = normalize_provider_key(
                str(snapshot.get("provider") or ""),
                user_providers,
            )
            same_provider = snapshot_provider == fallback_context.provider
        try:
            return restore_provider_runtime_context(snapshot, config=config)
        except ProviderResolutionError as exc:
            if exc.code != "missing_credential" or not same_provider or not fallback_context.api_key:
                raise
            context = restore_provider_runtime_context(
                snapshot,
                config=config,
                api_key=fallback_context.api_key,
            )
            return replace(context, auth_source=fallback_context.auth_source)
    context = restore_provider_runtime_context(
        fallback_context.snapshot(),
        config=config,
        model=str(row_model or fallback_context.model),
        api_key=fallback_context.api_key,
    )
    return replace(context, auth_source=fallback_context.auth_source)
