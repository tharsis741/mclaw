# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Choose the configured vision route without making network calls."""

from __future__ import annotations

from mclaw.providers.capabilities import resolve_image_capability
from mclaw.tools.vision.config import resolve_mode
from mclaw.tools.vision.types import VisionInputError


def ensure_vision_enabled(parent_agent) -> None:
    """Honor explicit disablement for tools, attachments and automatic screenshots."""
    config = getattr(parent_agent, "config", {}) or {}
    disabled = (config.get("tools") or {}).get("disabled", [])
    if isinstance(disabled, (list, tuple, set)) and "vision_analyze" in disabled:
        raise VisionInputError("视觉工具已关闭，请在工具设置中启用。", "VISION_DISABLED")


def choose_vision_route(parent_agent, mode=None, *, credentials_resolver=None):
    from mclaw.tools.vision.credentials import resolve_vision_credentials

    ensure_vision_enabled(parent_agent)
    selected = mode or resolve_mode(parent_agent)
    if not isinstance(selected, str) or selected not in {"auto", "main", "auxiliary"}:
        raise ValueError("Invalid vision mode")
    runtime = getattr(parent_agent, "provider_runtime", None)
    capability = resolve_image_capability(runtime, getattr(parent_agent, "config", {})) if runtime else None
    if selected == "main":
        if runtime is None or capability.state == "unsupported":
            raise ValueError(capability.reason if capability else "Main vision requires an active agent session.")
        return True, None
    if selected == "auto" and runtime and capability.state == "supported":
        return True, None
    credentials = (credentials_resolver or resolve_vision_credentials)(parent_agent=parent_agent)
    ready = getattr(credentials, "available", bool(credentials.api_key and not credentials.unsupported_reason))
    if selected == "auto" and runtime and capability.state == "unknown" and not ready:
        return True, None
    return False, credentials
