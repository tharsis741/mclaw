# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Offline model capabilities, scoped overrides and endpoint observations."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any

from mclaw.providers.runtime import ProviderRuntimeContext


@dataclass(frozen=True)
class ImageCapability:
    state: str = "unknown"
    source: str = "unknown"
    reason: str = "Image input capability is not listed."
    updated_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_observations: dict[tuple[str, ...], tuple[float, str]] = {}
_lock = threading.Lock()
_OBSERVATION_TTL = 3600


def capability_key(context: ProviderRuntimeContext) -> str:
    """Scope a user declaration to a model, protocol and endpoint, not a key."""
    identity = (context.provider, context.model, context.api_mode, context.safe_base_url)
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


def _declaration_revision(context: ProviderRuntimeContext, config: dict | None) -> str:
    overrides = (config or {}).get("model_capabilities", {})
    entry = overrides.get(capability_key(context), {}) if isinstance(overrides, dict) else {}
    return str(entry.get("revision", "")) if isinstance(entry, dict) else ""


def observe_image_rejection(context: ProviderRuntimeContext, config: dict | None = None) -> None:
    """Remember only an explicit image-input rejection at this runtime target."""
    with _lock:
        now = time.monotonic()
        for key, (recorded, _) in list(_observations.items()):
            if now - recorded >= _OBSERVATION_TTL:
                del _observations[key]
        if len(_observations) >= 256:
            del _observations[next(iter(_observations))]
        _observations[context.fingerprint()] = (now, _declaration_revision(context, config))


def clear_image_observation(context: ProviderRuntimeContext) -> None:
    with _lock:
        _observations.pop(context.fingerprint(), None)


def is_image_rejection(error: BaseException) -> bool:
    """Do not confuse auth, transport, image validation or policy errors with capability."""
    status = getattr(error, "status_code", None)
    if status not in (None, 400, 422):
        return False
    text = str(error).casefold()
    if any(word in text for word in ("timeout", "rate limit", "api key", "unauthorized",
                                    "too large", "invalid image", "image format", "content policy")):
        return False
    return any(marker in text for marker in (
        "does not support image", "doesn't support image", "do not support image",
        "image input is not supported", "image inputs are not supported",
        "image input not supported", "image_url is not supported",
        "image_url content is not supported", "image content blocks are not supported",
        "only supports text", "only text input", "does not support vision",
    ))


def resolve_image_capability(
    context: ProviderRuntimeContext,
    config: dict | None = None,
    *,
    registry: dict | None = None,
    image_input: str = "auto",
    catalog_provider: str = "",
    catalog_model: str = "",
) -> ImageCapability:
    """Resolve a tri-state declaration without networking or guessing model names."""
    if context.api_mode not in {"chat_completions", "anthropic_messages"}:
        return ImageCapability("unsupported", "adapter", "Image transport is not implemented for this protocol.")
    overrides = (config or {}).get("model_capabilities", {})
    override = overrides.get(capability_key(context), {}) if isinstance(overrides, dict) else {}
    if isinstance(override, dict):
        if image_input == "auto":
            image_input = override.get("image_input", image_input)
        catalog_provider = catalog_provider or override.get("catalog_provider", "")
        catalog_model = catalog_model or override.get("catalog_model", "")
    if not isinstance(catalog_provider, str) or not isinstance(catalog_model, str):
        return ImageCapability(reason="Invalid models.dev mapping; configure a provider/model name.")
    if image_input == "disabled":
        return ImageCapability("unsupported", "config", "Image input is disabled for this model configuration.")
    with _lock:
        observed = _observations.get(context.fingerprint())
    if observed and observed[1] == _declaration_revision(context, config) and time.monotonic() - observed[0] < _OBSERVATION_TTL:
        return ImageCapability("unsupported", "runtime", "This endpoint explicitly rejected image input.")
    if image_input == "enabled":
        return ImageCapability("supported", "config", "Image input is enabled by the user; not a verification result.")

    from mclaw.agent import models_dev

    if registry is None:
        registry = models_dev.cached_models_dev()
    provider = catalog_provider or context.profile.models_dev_provider or context.provider
    model = catalog_model or context.model
    entry = models_dev.lookup_model_metadata(provider, model, registry=registry)
    modalities = entry.get("modalities") if isinstance(entry, dict) else None
    inputs = modalities.get("input") if isinstance(modalities, dict) else None
    if not isinstance(inputs, list) or not inputs or any(not isinstance(item, str) or not item for item in inputs):
        return ImageCapability()
    supported = "image" in inputs
    return ImageCapability(
        "supported" if supported else "unsupported", "models.dev",
        f"models.dev declares {'image' if supported else 'no image'} input for {provider}/{model}.",
        models_dev.cached_models_dev_status().get("updated_at"),
    )
