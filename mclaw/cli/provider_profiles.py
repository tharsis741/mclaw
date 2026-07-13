# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Registry-derived setup and model-catalog presentation views."""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

from mclaw.providers.base import SetupProfileEntry
from mclaw.providers.registry import PROVIDER_REGISTRY


@dataclass(frozen=True)
class ProviderProfile:
    """One provider-facing setup or catalog option."""

    id: str
    label: str
    models_dev_provider: str
    runtime_provider: str = ""
    kind: str = "api"
    callable: bool = True
    credential_scope: str = "api"
    note: str = ""

    @property
    def status_label(self) -> str:
        return "可配置" if self.callable else "仅模型库"


def _view(entry: SetupProfileEntry) -> ProviderProfile:
    target = PROVIDER_REGISTRY.get(entry.runtime_provider) if entry.callable else None
    return ProviderProfile(
        id=entry.id,
        label=entry.label,
        models_dev_provider=target.models_dev_provider if target else entry.models_dev_provider,
        runtime_provider=entry.runtime_provider,
        kind=entry.kind,
        callable=entry.callable,
        credential_scope=entry.credential_scope,
        note=entry.note,
    )


PROVIDER_PROFILES: dict[str, list[ProviderProfile]] = {
    name: [_view(entry) for entry in profile.setup_profiles]
    for name, profile in PROVIDER_REGISTRY.items()
}

CORE_PROVIDER_KEYS: list[str] = [
    profile.name
    for profile in sorted(
        (item for item in PROVIDER_REGISTRY.values() if item.setup_order is not None),
        key=lambda item: item.setup_order,
    )
]


def get_provider_profiles(provider_key: str) -> list[ProviderProfile]:
    provider = str(provider_key or "").strip()
    if provider in PROVIDER_PROFILES and PROVIDER_PROFILES[provider]:
        return PROVIDER_PROFILES[provider]
    profile = PROVIDER_REGISTRY.get(provider)
    return [
        ProviderProfile(
            "api",
            f"{profile.display_name if profile else '官方'} API",
            profile.models_dev_provider if profile else provider,
            runtime_provider=provider,
        )
    ]


def get_provider_profile(provider_key: str, profile_id: str = "") -> ProviderProfile:
    return find_provider_profile(provider_key, profile_id) or get_default_provider_profile(provider_key)


def find_provider_profile(provider_key: str, profile_id: str = "") -> ProviderProfile | None:
    profiles = get_provider_profiles(provider_key)
    requested = str(profile_id or "").strip().casefold()
    if requested:
        return next((profile for profile in profiles if profile.id.casefold() == requested), None)
    return get_default_provider_profile(provider_key)


def get_default_provider_profile(provider_key: str) -> ProviderProfile:
    profiles = get_provider_profiles(provider_key)
    return (
        next((profile for profile in profiles if profile.callable and profile.kind == "api"), None)
        or next((profile for profile in profiles if profile.callable), None)
        or profiles[0]
    )


def resolve_models_dev_provider(provider_key: str, profile_id: str = "") -> str:
    return get_provider_profile(provider_key, profile_id).models_dev_provider


def profile_help_lines(provider_key: str) -> list[str]:
    lines: list[str] = []
    for profile in get_provider_profiles(provider_key):
        suffix = f"；{profile.note}" if profile.note else ""
        lines.append(f"{profile.id}: {profile.label} [{profile.status_label}]{suffix}")
    return lines


def search_models_dev_provider_ids(query: str, provider_ids: list[str], *, limit: int = 10) -> list[str]:
    raw = str(query or "").strip()
    if not raw:
        return []
    key = _compact(raw)
    scored: list[tuple[float, str]] = []
    for provider_id in provider_ids:
        candidate = _compact(provider_id)
        score = difflib.SequenceMatcher(None, key, candidate).ratio()
        if key and (key in candidate or candidate in key):
            score = max(score, 0.8)
        if score >= 0.55:
            scored.append((score, provider_id))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [provider_id for _score, provider_id in scored[:limit]]


def _compact(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())
