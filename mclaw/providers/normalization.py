# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Canonical provider name normalization."""

from __future__ import annotations

from typing import Any

from mclaw.providers.registry import PROVIDER_REGISTRY

_DYNAMIC_PROVIDER_SELECTORS = {"custom", "custom_anthropic"}


def _compact(value: str) -> str:
    return "".join(ch for ch in str(value or "").casefold() if ch.isalnum())


def normalize_provider_key(
    provider_input: str,
    user_providers: dict[str, Any] | None = None,
) -> str:
    """Resolve user input to a canonical built-in or configured provider key."""
    raw = str(provider_input or "").strip()
    if not raw:
        return ""
    user_providers = user_providers or {}

    if raw in PROVIDER_REGISTRY:
        return raw
    if raw in user_providers:
        return raw

    folded = raw.casefold()
    for name in user_providers:
        if name.casefold() == folded:
            return name

    for name, profile in PROVIDER_REGISTRY.items():
        if folded == name.casefold() or folded in {alias.casefold() for alias in profile.aliases}:
            return name

    compact = _compact(raw)
    for name, config in user_providers.items():
        display_name = str(config.get("display_name") or name) if isinstance(config, dict) else name
        if compact in {_compact(name), _compact(display_name)}:
            return name
    for name, profile in PROVIDER_REGISTRY.items():
        if compact == _compact(profile.display_name):
            return name

    return raw


def reserved_provider_collision(provider_name: str) -> str:
    """Return the built-in key reserved by a configured provider name, if any."""
    raw = str(provider_name or "").strip()
    if not raw:
        return ""
    folded = raw.casefold()
    if folded in _DYNAMIC_PROVIDER_SELECTORS:
        return folded
    compact = _compact(raw)
    for name, profile in PROVIDER_REGISTRY.items():
        if folded == name.casefold():
            return name
        if folded in {alias.casefold() for alias in profile.aliases}:
            return name
        if compact == _compact(profile.display_name):
            return name
    return ""
