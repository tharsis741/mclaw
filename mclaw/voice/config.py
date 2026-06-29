# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Configuration resolution for voice ASR backends."""

from __future__ import annotations

from typing import Any, Dict

from mclaw.cli.config import get_env_value, load_config, mask_api_key

CN_REALTIME_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"
INTL_REALTIME_URL = "wss://dashscope-intl.aliyuncs.com/api-ws/v1/realtime"

DEFAULT_ASR_CONFIG: Dict[str, Any] = {
    "enabled": "auto",
    "provider": "dashscope",
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
_ASR_REQUIRED_FOR = "runtime:asr"


def _authorized_env_value(name: str) -> str:
    try:
        from mclaw.runtime.features import authorized_env_value

        return authorized_env_value(_ASR_REQUIRED_FOR, name, get_env_value)
    except Exception:
        return ""


def _resolve_enabled(value: Any, config: dict | None = None) -> tuple[bool, str]:
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
    if isinstance(config, dict) and config:
        return config
    if parent_agent is not None:
        cfg = getattr(parent_agent, "config", None)
        if isinstance(cfg, dict) and cfg:
            return cfg
    try:
        return load_config()
    except Exception:
        return {}


def _is_dashscope_target(section: dict) -> bool:
    provider = str(section.get("provider") or "").lower()
    backend = str(section.get("backend") or "").lower()
    model = str(section.get("model") or "").lower()
    base_url = str(section.get("base_url") or "").lower()
    websocket_url = str(section.get("websocket_url") or "").lower()
    return (
        provider in ("dashscope", "qwen")
        or backend in ("qwen_realtime", "dashscope_realtime")
        or model.startswith("qwen")
        or "dashscope" in base_url
        or "dashscope" in websocket_url
    )


def _default_realtime_url(region: str) -> str:
    return INTL_REALTIME_URL if str(region).lower() in ("intl", "international", "global") else CN_REALTIME_URL


def resolve_asr_config(parent_agent=None, config: dict | None = None) -> Dict[str, Any]:
    cfg = effective_config(parent_agent=parent_agent, config=config)
    auxiliary = cfg.get("auxiliary", {}) if isinstance(cfg, dict) else {}
    if not isinstance(auxiliary, dict):
        auxiliary = {}

    raw_asr = auxiliary.get("asr", {})
    asr_cfg = raw_asr if isinstance(raw_asr, dict) else {}
    result = _deep_merge(DEFAULT_ASR_CONFIG, asr_cfg)
    enabled, enabled_mode = _resolve_enabled(result.get("enabled"), result)
    result["enabled"] = enabled
    result["enabled_mode"] = enabled_mode

    wants_dashscope = _is_dashscope_target(result)
    result["credential_provider"] = "qwen" if wants_dashscope else str(result.get("provider") or "")
    result["dedicated_target"] = bool(wants_dashscope)
    result["key_source"] = ""

    result["api_key"] = ""
    if wants_dashscope:
        dashscope_key = _authorized_env_value("DASHSCOPE_API_KEY")
        qwen_key = _authorized_env_value("QWEN_API_KEY")
        if dashscope_key:
            result["api_key"] = dashscope_key
            result["key_source"] = "DASHSCOPE_API_KEY"
        elif qwen_key:
            result["api_key"] = qwen_key
            result["key_source"] = "QWEN_API_KEY"

    if not result.get("websocket_url"):
        env_url = get_env_value("DASHSCOPE_REALTIME_URL")
        result["websocket_url"] = env_url or _default_realtime_url(str(result.get("region") or "cn"))

    if not isinstance(result.get("wake_words"), list):
        result["wake_words"] = list(DEFAULT_ASR_CONFIG["wake_words"])

    return result


def mask_asr_status(config: dict) -> dict:
    status = dict(config)
    status["api_key"] = mask_api_key(str(config.get("api_key") or ""))
    return status
