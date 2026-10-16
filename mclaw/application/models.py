# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Secret-free model choices derived from existing configured providers."""

from __future__ import annotations


def configured_models(status: dict) -> list[dict[str, str]]:
    from mclaw.cli.config import ConfigError, load_config
    from mclaw.providers.registry import PROVIDER_REGISTRY
    from mclaw.providers.resolver import ProviderResolutionError, resolve_provider_runtime_context

    try:
        config = load_config(strict=True)
    except (ConfigError, OSError):
        # Settings can change between the status read and this optional list.
        # Keep the application reachable so the user can repair configuration.
        return []
    candidates = [(status.get("provider", ""), status.get("model", ""))]
    candidates.extend((entry.get("provider", ""), entry.get("model", ""))
                      for entry in config.get("fallback_providers", []) if isinstance(entry, dict))
    providers = config.get("providers") or {}
    if isinstance(providers, dict):
        candidates.extend((name, entry.get("model", "")) for name, entry in providers.items() if isinstance(entry, dict))
    # Registry candidates are returned only when their existing credential resolves.
    for provider, profile in PROVIDER_REGISTRY.items():
        candidates.extend((provider, model) for model in profile.fallback_models)
    choices = []
    seen = set()
    for provider, model in candidates:
        if not provider or not model or (provider, model) in seen:
            continue
        seen.add((provider, model))
        try:
            context = resolve_provider_runtime_context(provider=provider, model=model, config=config)
        except ProviderResolutionError:
            continue
        choices.append({"id": f"{context.provider}::{context.model}", "label": context.model,
                        "provider": context.provider, "provider_label": context.profile.display_name})
    return choices
