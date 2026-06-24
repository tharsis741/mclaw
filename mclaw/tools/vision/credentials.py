"""Credential resolution for the built-in Qwen vision provider."""

from __future__ import annotations

from typing import Any

from mclaw.tools.vision.config import (
    DASHSCOPE_BASE_URL,
    DEFAULT_PROVIDER,
    QWEN_DEFAULT_MODEL,
    VISION_REQUIRED_FOR,
    authorized_env_value,
    env_value,
    vision_config,
)
from mclaw.tools.vision.types import VisionCredentials

_SUPPORTED_PROVIDER_HINTS = {"", "auto", "qwen", "dashscope"}


def _is_qwen_model(model: str) -> bool:
    return str(model or "").strip().lower().startswith("qwen")


def _is_qwen_target(provider: str, base_url: str, model: str) -> bool:
    provider_hint = str(provider or "").strip().lower()
    return (
        provider_hint in {"qwen", "dashscope"}
        or "dashscope" in str(base_url or "").lower()
        or _is_qwen_model(model)
    )


def _unsupported_reason(provider: str, base_url: str, model: str) -> str:
    provider_hint = str(provider or "").strip().lower()
    if provider_hint not in _SUPPORTED_PROVIDER_HINTS and not _is_qwen_target(provider, base_url, model):
        return (
            f"vision provider '{provider_hint}' is not supported yet. "
            "Only Qwen vision is enabled in this build."
        )
    if model and not _is_qwen_model(model) and not _is_qwen_target(provider, base_url, model):
        return (
            f"vision model '{model}' does not look like a Qwen vision model. "
            "Only Qwen vision is enabled in this build."
        )
    return ""


def resolve_vision_credentials(parent_agent: Any = None, config: dict | None = None) -> VisionCredentials:
    """Resolve credentials for the currently supported vision provider.

    M-Claw currently enables only Qwen vision. The return type keeps a
    provider field so future provider clients can be added without changing the
    tool orchestration layer.
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

    dashscope_key = authorized_env_value("DASHSCOPE_API_KEY")
    qwen_key = authorized_env_value("QWEN_API_KEY")
    api_key = dashscope_key or qwen_key
    env_var = "DASHSCOPE_API_KEY" if dashscope_key else ("QWEN_API_KEY" if qwen_key else "")
    base_url = base_url_hint or env_value("DASHSCOPE_BASE_URL", DASHSCOPE_BASE_URL)
    model = model_hint or QWEN_DEFAULT_MODEL

    return VisionCredentials(
        provider=DEFAULT_PROVIDER,
        api_key=api_key,
        base_url=base_url,
        model=model,
        env_var=env_var,
    )


def feature_env_configured() -> bool:
    try:
        from mclaw.runtime.features import authorized_configured_env_vars, get_feature

        spec = get_feature("vision_analyze")
        return bool(spec and authorized_configured_env_vars(spec, env_value))
    except Exception:
        return bool(authorized_env_value("DASHSCOPE_API_KEY") or authorized_env_value("QWEN_API_KEY"))


def diagnose_vision_credentials(config: dict | None = None) -> dict:
    creds = resolve_vision_credentials(config=config)
    if creds.unsupported_reason:
        return {
            "available": False,
            "reason": creds.unsupported_reason,
            "fix": "Configure auxiliary.vision.provider as qwen or auto, then authorize DASHSCOPE_API_KEY/QWEN_API_KEY.",
        }
    if creds.api_key:
        return {"available": True, "reason": "Qwen vision key found in environment", "fix": ""}
    return {
        "available": False,
        "reason": "Qwen vision key is missing or not authorized (DASHSCOPE_API_KEY/QWEN_API_KEY)",
        "fix": f"Call secret_request_many(required_for='{VISION_REQUIRED_FOR}', ...) or rerun setup.",
    }
