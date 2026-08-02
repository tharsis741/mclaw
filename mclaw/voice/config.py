# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Configuration resolution for voice ASR backends."""

from __future__ import annotations

from typing import Any, Dict

from mclaw.cli.config import get_env_value, load_config, mask_api_key
from mclaw.providers.normalization import normalize_provider_key
from mclaw.providers.registry import get_runtime_profile
from mclaw.voice.codecs import SILK_SAMPLE_RATES
from mclaw.voice.transcription import validate_audio_service_url

CN_REALTIME_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"
INTL_REALTIME_URL = "wss://dashscope-intl.aliyuncs.com/api-ws/v1/realtime"

DEFAULT_ASR_CONFIG: Dict[str, Any] = {
    "enabled": "auto",
    "provider": "qwen",
    "backend": "qwen_realtime",
    "model": "qwen3-asr-flash-realtime",
    "websocket_url": "",
    "force_ipv4": "auto",
    "ca_bundle": "auto",
    "recorder_backend": "auto",
    "arecord_device": "auto",
    "region": "cn",
    "language": "zh",
    "sample_rate": 16000,
    "input_audio_format": "pcm",
    "channels": 1,
    "enable_server_vad": True,
    "vad_threshold": 0.0,
    "silence_duration_ms": 400,
    "listen_mode": "wake_word",
    "require_wake_word": True,
    "wake_words": ["小爪", "老麦"],
    "dedupe_seconds": 1.5,
    "allow_voice_slash_commands": False,
    "min_audio_rms": 1,
    "min_voice_chunks": 1,
    "push_to_talk_key": "f8",
    "push_to_talk_behavior": "tap_once",
}
DEFAULT_INBOUND_AUDIO_CONFIG: Dict[str, Any] = {
    "enabled": "auto",
    "auto_transcribe_voice_messages": True,
    "provider": "qwen",
    "model": "qwen3-asr-flash",
    "base_url": "",
    "language": "auto",
    "enable_itn": False,
    # Qwen's OpenAI-compatible endpoint applies its 10 MiB limit after
    # Base64 expansion.  Seven MiB leaves safe room for the data-URI wrapper.
    "max_audio_bytes": 7 * 1024 * 1024,
    "max_duration_seconds": 120,
    "transcription_timeout_seconds": 60.0,
    "decode_timeout_seconds": 15.0,
    "max_concurrency": 2,
    "max_retries": 1,
    "retry_backoff_seconds": 0.5,
    "retain_source_seconds": 0,
    "retain_decoded_seconds": 0,
    "silk_sample_rate": 24000,
}
_ASR_REQUIRED_FOR = "runtime:asr"
_QWEN_PROVIDER_KEYS = {"qwen", "qwen-intl"}


def _authorized_env_value(name: str) -> str:
    """Read ASR credentials only through the runtime scoped-secret gate."""
    try:
        from mclaw.runtime.features import authorized_env_value

        return authorized_env_value(_ASR_REQUIRED_FOR, name, get_env_value)
    except Exception:
        return ""


def _resolve_enabled(value: Any, config: dict | None = None) -> tuple[bool, str]:
    """Resolve the user-facing enabled mode, probing input hardware for auto."""
    if isinstance(value, str) and value.strip().lower() == "auto":
        try:
            from mclaw.voice.recorder import AudioRecorder

            return AudioRecorder.input_available(config or {}), "auto"
        except Exception:
            return False, "auto"
    if isinstance(value, bool):
        return value, "on" if value else "off"
    if isinstance(value, (int, float)):
        return bool(value), "on" if value else "off"
    if isinstance(value, str):
        enabled = value.strip().lower() in {"1", "true", "yes", "on", "y"}
        return enabled, "on" if enabled else "off"
    return False, "off"


def _deep_merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def effective_config(parent_agent=None, config: dict | None = None) -> dict:
    """Select the config source in the same order as interactive runtime callers."""
    if isinstance(config, dict) and config:
        return config
    if parent_agent is not None:
        cfg = getattr(parent_agent, "config", None)
        if isinstance(cfg, dict) and cfg:
            return cfg
    return load_config(strict=True)


def _is_dashscope_target(section: dict) -> bool:
    """Detect whether this ASR section needs DashScope/Qwen credentials."""
    provider = str(section.get("provider") or "").lower()
    backend = str(section.get("backend") or "").lower()
    model = str(section.get("model") or "").lower()
    base_url = str(section.get("base_url") or "").lower()
    websocket_url = str(section.get("websocket_url") or "").lower()
    return (
        normalize_provider_key(provider) in _QWEN_PROVIDER_KEYS
        or backend in ("qwen_realtime", "dashscope_realtime")
        or model.startswith("qwen")
        or "dashscope" in base_url
        or "dashscope" in websocket_url
    )


def _qwen_profile(section: dict, *, region_explicit: bool):
    """Select canonical Qwen metadata from explicit endpoint, region, or provider."""
    endpoint = f"{section.get('base_url') or ''} {section.get('websocket_url') or ''}".lower()
    if "dashscope-intl" in endpoint:
        return get_runtime_profile("qwen-intl")
    if "dashscope.aliyuncs.com" in endpoint:
        return get_runtime_profile("qwen")
    if region_explicit:
        region = str(section.get("region") or "").lower()
        return get_runtime_profile("qwen-intl" if region in {"intl", "international", "global"} else "qwen")
    provider = normalize_provider_key(str(section.get("provider") or ""))
    return get_runtime_profile(provider if provider in _QWEN_PROVIDER_KEYS else "qwen")


def _default_realtime_url(region: str) -> str:
    return INTL_REALTIME_URL if str(region).lower() in ("intl", "international", "global") else CN_REALTIME_URL


def resolve_asr_config(parent_agent=None, config: dict | None = None) -> Dict[str, Any]:
    """Build the effective ASR config with defaults, hardware gating, and secrets."""
    cfg = effective_config(parent_agent=parent_agent, config=config)
    auxiliary = cfg.get("auxiliary", {}) if isinstance(cfg, dict) else {}
    if not isinstance(auxiliary, dict):
        auxiliary = {}

    raw_asr = auxiliary.get("asr", {})
    asr_cfg = raw_asr if isinstance(raw_asr, dict) else {}
    result = _deep_merge(DEFAULT_ASR_CONFIG, asr_cfg)
    if result.get("listen_mode") not in {"wake_word", "push_to_talk"}:
        result["listen_mode"] = "wake_word"
        result["require_wake_word"] = True
    enabled, enabled_mode = _resolve_enabled(result.get("enabled"), result)
    result["enabled"] = enabled
    result["enabled_mode"] = enabled_mode

    wants_dashscope = _is_dashscope_target(result)
    profile = _qwen_profile(result, region_explicit="region" in asr_cfg) if wants_dashscope else None
    if profile:
        result["provider"] = profile.name
    result["credential_provider"] = profile.name if profile else str(result.get("provider") or "")
    result["dedicated_target"] = bool(wants_dashscope)
    result["key_source"] = ""

    result["api_key"] = ""
    if profile:
        result["key_source"], result["api_key"] = next(
            ((name, value) for name in profile.env_vars if (value := _authorized_env_value(name))),
            ("", ""),
        )

    if not result.get("websocket_url"):
        env_url = get_env_value("DASHSCOPE_REALTIME_URL")
        default_url = (
            INTL_REALTIME_URL
            if profile and profile.name == "qwen-intl"
            else _default_realtime_url(str(result.get("region") or "cn"))
        )
        result["websocket_url"] = env_url or default_url

    if not isinstance(result.get("wake_words"), list):
        result["wake_words"] = list(DEFAULT_ASR_CONFIG["wake_words"])

    return result


def resolve_inbound_audio_config(parent_agent=None, config: dict | None = None) -> Dict[str, Any]:
    """Resolve complete-file ASR without probing microphone hardware.

    Gateway voice messages have already been recorded by the remote user, so
    their availability must not depend on a local sound card or PortAudio.
    Credentials still pass through the existing ``runtime:asr`` secret gate.
    """

    cfg = effective_config(parent_agent=parent_agent, config=config)
    capabilities = cfg.get("capabilities", {}) if isinstance(cfg, dict) else {}
    if not isinstance(capabilities, dict):
        capabilities = {}
    raw = capabilities.get("inbound_audio", {})
    inbound_cfg = raw if isinstance(raw, dict) else {}
    result = _deep_merge(DEFAULT_INBOUND_AUDIO_CONFIG, inbound_cfg)

    provider = normalize_provider_key(str(result.get("provider") or "qwen"))
    result["provider"] = provider
    result["credential_provider"] = provider
    result["configuration_error"] = ""
    result["key_source"] = ""
    result["api_key"] = ""

    if provider not in _QWEN_PROVIDER_KEYS:
        # Never reinterpret an unknown/local provider as a cloud provider.  A
        # typo must not cause private audio to be sent using an unrelated key.
        result["base_url"] = ""
        result["configuration_error"] = f"unsupported inbound ASR provider: {provider or '<empty>'}"
    else:
        profile = get_runtime_profile(provider)
        result["provider"] = profile.name
        result["credential_provider"] = profile.name
        result["key_source"], result["api_key"] = next(
            ((name, value) for name in profile.env_vars if (value := _authorized_env_value(name))),
            ("", ""),
        )

        configured_base_url = str(result.get("base_url") or "").strip()
        env_base_url = get_env_value(profile.base_url_env_var) if profile.base_url_env_var else ""
        candidate_base_url = configured_base_url or env_base_url or profile.base_url
        try:
            result["base_url"] = validate_audio_service_url(candidate_base_url)
        except ValueError as exc:
            result["base_url"] = str(candidate_base_url or "").strip().rstrip("/")
            result["configuration_error"] = f"invalid inbound ASR base URL: {exc}"
            # Do not leave a usable credential beside an unsafe endpoint.
            result["key_source"] = ""
            result["api_key"] = ""

    language = str(result.get("language") or "auto").strip().casefold()
    result["language"] = language or "auto"

    for key, minimum in (
        ("max_audio_bytes", 1),
        ("max_duration_seconds", 1),
        ("max_concurrency", 1),
    ):
        try:
            value = int(result.get(key) or 0)
        except (TypeError, ValueError):
            value = int(DEFAULT_INBOUND_AUDIO_CONFIG[key])
        result[key] = max(minimum, value)
    try:
        silk_sample_rate = int(result.get("silk_sample_rate") or 0)
    except (TypeError, ValueError):
        silk_sample_rate = int(DEFAULT_INBOUND_AUDIO_CONFIG["silk_sample_rate"])
    result["silk_sample_rate"] = silk_sample_rate
    if silk_sample_rate not in SILK_SAMPLE_RATES:
        supported = ", ".join(str(rate) for rate in sorted(SILK_SAMPLE_RATES))
        rate_error = (
            f"unsupported SILK sample rate: {silk_sample_rate}; "
            f"expected one of {supported}"
        )
        existing_error = str(result.get("configuration_error") or "")
        result["configuration_error"] = (
            f"{existing_error}; {rate_error}" if existing_error else rate_error
        )
    for key in ("retain_source_seconds", "retain_decoded_seconds"):
        try:
            value = int(result.get(key) or 0)
        except (TypeError, ValueError):
            value = int(DEFAULT_INBOUND_AUDIO_CONFIG[key])
        result[key] = max(0, value)
    for key, minimum in (
        ("transcription_timeout_seconds", 1.0),
        ("decode_timeout_seconds", 1.0),
        ("retry_backoff_seconds", 0.0),
    ):
        try:
            value = float(result.get(key) or 0)
        except (TypeError, ValueError):
            value = float(DEFAULT_INBOUND_AUDIO_CONFIG[key])
        result[key] = max(minimum, value)
    try:
        result["max_retries"] = max(0, int(result.get("max_retries") or 0))
    except (TypeError, ValueError):
        result["max_retries"] = int(DEFAULT_INBOUND_AUDIO_CONFIG["max_retries"])
    for key in (
        "auto_transcribe_voice_messages",
        "enable_itn",
    ):
        result[key] = _coerce_enabled(result.get(key))

    enabled_raw = result.get("enabled")
    if isinstance(enabled_raw, str) and enabled_raw.strip().casefold() == "auto":
        result["enabled"] = bool(result["api_key"]) and not result["configuration_error"]
        result["enabled_mode"] = "auto"
    else:
        requested_enabled = _coerce_enabled(enabled_raw)
        result["enabled"] = requested_enabled and not result["configuration_error"]
        result["enabled_mode"] = "on" if requested_enabled else "off"
    return result


def _coerce_enabled(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "on", "y"}
    return False


def mask_asr_status(config: dict) -> dict:
    """Return a status-safe copy that never exposes the plaintext API key."""
    status = dict(config)
    status["api_key"] = mask_api_key(str(config.get("api_key") or ""))
    return status
