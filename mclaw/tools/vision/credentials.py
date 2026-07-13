# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Credential resolution for the built-in Qwen vision provider."""

from __future__ import annotations

from typing import Any

from mclaw.cli.config import ConfigError
from mclaw.providers.normalization import normalize_provider_key
from mclaw.providers.registry import get_runtime_profile
from mclaw.tools.vision.config import (
    DEFAULT_PROVIDER,
    QWEN_DEFAULT_MODEL,
    VISION_REQUIRED_FOR,
    authorized_env_value,
    env_value,
    vision_config,
)
from mclaw.tools.vision.types import VisionCredentials

_QWEN_PROVIDER_KEYS = {"qwen", "qwen-intl"}


def _is_qwen_model(model: str) -> bool:
    """Return whether a model name belongs to the supported Qwen family."""
    return str(model or "").strip().lower().startswith("qwen")


def _is_qwen_target(provider: str, base_url: str, model: str) -> bool:
    """Infer Qwen compatibility from provider hint, base URL, or model name."""
    provider_hint = str(provider or "").strip().lower()
    return (
        normalize_provider_key(provider_hint) in _QWEN_PROVIDER_KEYS
        or "dashscope" in str(base_url or "").lower()
        or _is_qwen_model(model)
    )


def _qwen_profile(provider: str, base_url: str):
    """Select the canonical regional Qwen metadata for this tool target."""
    endpoint = str(base_url or "").lower()
    if "dashscope-intl" in endpoint:
        return get_runtime_profile("qwen-intl")
    if "dashscope.aliyuncs.com" in endpoint:
        return get_runtime_profile("qwen")
    normalized = normalize_provider_key(str(provider or "").strip())
    return get_runtime_profile(normalized if normalized in _QWEN_PROVIDER_KEYS else DEFAULT_PROVIDER)


def _unsupported_reason(provider: str, base_url: str, model: str) -> str:
    """Explain unsupported provider/model combinations without probing secrets."""
    provider_hint = str(provider or "").strip().lower()
    if provider_hint not in {"", "auto"} and not _is_qwen_target(provider, base_url, model):
        return (
            f"vision provider '{provider_hint}' is not supported. "
            "Configure auxiliary.vision.provider as qwen or dashscope."
        )
    if model and not _is_qwen_model(model) and not _is_qwen_target(provider, base_url, model):
        return (
            f"vision model '{model}' is outside the supported Qwen vision configuration. "
            "Configure a Qwen vision model for auxiliary.vision.model."
        )
    return ""


def resolve_vision_credentials(parent_agent: Any = None, config: dict | None = None) -> VisionCredentials:
    """Resolve credentials for the currently supported vision provider.

    M-Claw enables Qwen vision through DashScope-compatible credentials. The
    provider field keeps client selection separate from tool orchestration.
    """
    cfg = vision_config(parent_agent=parent_agent, config=config)
    provider_hint = str(cfg.get("provider") or "auto").strip().lower()
    base_url_hint = str(cfg.get("base_url") or "").strip()
    model_hint = str(cfg.get("model") or "").strip()

    unsupported = _unsupported_reason(provider_hint, base_url_hint, model_hint)
    if unsupported:
        return VisionCredentials(
            provider=provider_hint or "unsupported",
            api_key="",
            base_url=base_url_hint,
            model=model_hint,
            unsupported_reason=unsupported,
        )

    profile = _qwen_profile(provider_hint, base_url_hint)
    env_var, api_key = next(
        ((name, value) for name in profile.env_vars if (value := authorized_env_value(name))),
        ("", ""),
    )
    base_url = base_url_hint or env_value(profile.base_url_env_var, profile.base_url)
    model = model_hint or QWEN_DEFAULT_MODEL

    return VisionCredentials(
        provider=profile.name,
        api_key=api_key,
        base_url=base_url,
        model=model,
        env_var=env_var,
    )


def diagnose_vision_credentials(config: dict | None = None) -> dict:
    """Return availability diagnostics without exposing credential values."""
    try:
        creds = resolve_vision_credentials(config=config)
    except ConfigError as exc:
        return {
            "available": False,
            "reason": f"vision config check failed: {type(exc).__name__}: {exc}",
            "fix": "Check auxiliary.vision config.",
        }
    if creds.unsupported_reason:
        return {
            "available": False,
            "reason": creds.unsupported_reason,
            "fix": "Configure auxiliary.vision.provider as qwen or auto, then authorize a registry-declared Qwen key.",
        }
    if creds.api_key:
        return {"available": True, "reason": "Qwen vision key found in environment", "fix": ""}
    profile = get_runtime_profile(creds.provider if creds.provider in _QWEN_PROVIDER_KEYS else DEFAULT_PROVIDER)
    credential_names = "/".join(profile.env_vars)
    return {
        "available": False,
        "reason": f"{profile.display_name} key is missing or not authorized ({credential_names})",
        "fix": f"Call secret_request_many(required_for='{VISION_REQUIRED_FOR}', ...) or rerun setup.",
    }
