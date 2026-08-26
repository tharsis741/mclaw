# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Pure host-admission decisions for the DSoftBus interactive entrypoint."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


_CLI_SELECTOR_KEYS = ("model", "provider", "api_key", "base_url")
_PROJECT_SCALAR_SELECTORS = ("model", "active_provider", "active_provider_profile")


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def is_discovery_only_candidate(
    config: Mapping[str, Any],
) -> bool:
    """Return whether the current Kaihong runtime and user config enable DSoftBus."""
    dsoftbus = config.get("dsoftbus")
    if not isinstance(dsoftbus, Mapping) or dsoftbus.get("enabled") != "auto":
        return False
    from mclaw.runtime.manager import RuntimeManager

    return RuntimeManager.current(dict(config)).features.is_enabled("dsoftbus")


def has_persisted_provider_selection(
    user_config: Mapping[str, Any],
    resume_snapshot: Mapping[str, Any] | None,
) -> bool:
    """Recognize only user/session provider evidence that survived a prior launch."""
    if isinstance(resume_snapshot, Mapping):
        schema = resume_snapshot.get("schema_version")
        if (
            type(schema) is int
            and schema == 1
            and _nonempty_string(resume_snapshot.get("provider"))
            and _nonempty_string(resume_snapshot.get("model"))
        ):
            return True

    if _nonempty_string(user_config.get("model")):
        return True
    if _nonempty_string(user_config.get("active_provider")) and _nonempty_string(
        user_config.get("active_provider_profile")
    ):
        return True

    fallback = user_config.get("fallback_providers")
    if isinstance(fallback, list):
        for item in fallback:
            if (
                isinstance(item, Mapping)
                and _nonempty_string(item.get("provider"))
                and _nonempty_string(item.get("model"))
            ):
                return True

    providers = user_config.get("providers")
    if isinstance(providers, Mapping):
        for item in providers.values():
            if isinstance(item, Mapping) and _nonempty_string(item.get("model")):
                return True
    return False


def _project_has_selector(raw_project_config: Mapping[str, Any]) -> bool:
    if any(_nonempty_string(raw_project_config.get(key)) for key in _PROJECT_SCALAR_SELECTORS):
        return True
    providers = raw_project_config.get("providers")
    if providers not in (None, {}, []):
        return True
    fallback = raw_project_config.get("fallback_providers")
    return fallback not in (None, {}, [])


def can_enter_discovery_only(
    *,
    error_code: str,
    resume_snapshot: Mapping[str, Any] | None,
    cli_values: Mapping[str, str],
    raw_user_config: Mapping[str, Any],
    raw_project_config: Mapping[str, Any],
    merged_config: Mapping[str, Any],
) -> bool:
    """Apply the only allowed missing-Provider downgrade decision."""
    if not is_discovery_only_candidate(merged_config):
        return False
    if any(str(cli_values.get(key) or "").strip() for key in _CLI_SELECTOR_KEYS):
        return False
    if _project_has_selector(raw_project_config):
        return False

    persisted = has_persisted_provider_selection(raw_user_config, resume_snapshot)
    dsoftbus = raw_user_config.get("dsoftbus")
    collaboration_selected = (
        isinstance(dsoftbus, Mapping) and dsoftbus.get("enabled") == "auto"
    )
    if error_code == "provider_required":
        return collaboration_selected or persisted
    if error_code in {"missing_model", "missing_credential"}:
        return persisted
    return False


__all__ = [
    "can_enter_discovery_only",
    "has_persisted_provider_selection",
    "is_discovery_only_candidate",
]
