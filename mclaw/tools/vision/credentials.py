# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Resolve independent vision targets through the shared provider registry."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

from mclaw.providers.capabilities import resolve_image_capability
from mclaw.providers.normalization import normalize_provider_key
from mclaw.providers.registry import PROVIDER_REGISTRY
from mclaw.providers.resolver import resolve_provider_runtime_context
from mclaw.tools.vision.config import (
    DEFAULT_PROVIDER, QWEN_DEFAULT_MODEL, VISION_REQUIRED_FOR,
    authorized_env_value, effective_config, env_value, resolve_mode, vision_config,
)
from mclaw.tools.vision.types import VisionCredentials


def resolve_vision_credentials(parent_agent: Any = None, config: dict | None = None) -> VisionCredentials:
    config = effective_config(parent_agent, config)
    cfg = vision_config(config=config)
    provider = str(cfg.get("provider") or DEFAULT_PROVIDER).strip()
    if provider == "auto":
        provider = DEFAULT_PROVIDER
    provider = normalize_provider_key(provider, config.get("providers", {}))
    endpoint = str(cfg.get("base_url") or "").strip()
    if provider in {"qwen", "qwen-intl"} and "dashscope-intl" in endpoint:
        provider = "qwen-intl"
    profile = PROVIDER_REGISTRY.get(provider)
    if profile and not endpoint:
        endpoint = env_value(profile.base_url_env_var, profile.base_url) if profile.base_url_env_var else profile.base_url
    model = str(cfg.get("model") or (QWEN_DEFAULT_MODEL if provider in {"qwen", "qwen-intl"} else "")).strip()
    try:
        if not model:
            raise ValueError("Configure a dedicated vision model name.")
        # Resolve metadata without borrowing an unrelated or unauthorized credential.
        runtime = resolve_provider_runtime_context(
            provider=provider, model=model, base_url=endpoint,
            api_key="vision-metadata-only", config=config, reasoning_config={"enabled": False},
        )
        selected = cfg.get("credential_env")
        names = runtime.profile.env_vars
        if selected:
            if selected not in names:
                raise ValueError("The selected credential is not declared by this vision provider.")
            names = (selected,)
        env_var, key = next(((name, value) for name in names if (value := authorized_env_value(name))), ("", ""))
        runtime = replace(runtime, api_key=key, auth_source=env_var)
        capability = resolve_image_capability(
            runtime, config, image_input=cfg.get("image_input", "auto"),
            catalog_provider=cfg.get("catalog_provider", ""), catalog_model=cfg.get("catalog_model", ""),
        )
        reason = capability.reason if capability.state == "unsupported" else ""
        return VisionCredentials(runtime.provider, key, runtime.base_url, runtime.model, env_var, reason, runtime)
    except (ValueError, TypeError) as exc:
        return VisionCredentials(provider, "", endpoint, model, unsupported_reason=str(exc))


def diagnose_vision_credentials(config: dict | None = None) -> dict:
    """Return a route diagnosis without exposing secrets or making model calls."""
    try:
        config = effective_config(config=config)
        mode = resolve_mode(config=config)
        from mclaw.tools.vision.routing import choose_vision_route

        main = None
        if mode != "auxiliary":
            try:
                main = resolve_provider_runtime_context(config=config)
            except ValueError:
                pass
        native, creds = choose_vision_route(SimpleNamespace(config=config, provider_runtime=main))
        if native:
            capability = resolve_image_capability(main, config)
            return {"available": True, "reason": f"当前模型：{capability.reason}", "fix": "",
                    "mode": mode, "route": "main", "main_capability": capability.to_dict()}
        return {"available": creds.available, "mode": mode,
                "route": "auxiliary",
                "reason": creds.unsupported_reason or (f"专用视觉模型：{creds.provider}/{creds.model}" if creds.available else "专用视觉模型的凭据缺失或尚未授权。"),
                "fix": "" if creds.available else f"Configure vision or authorize a provider credential for {VISION_REQUIRED_FOR}."}
    except (ValueError, TypeError) as exc:
        return {"available": False, "reason": str(exc), "fix": "Check auxiliary.vision configuration."}
