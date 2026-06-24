"""Configuration helpers for the vision tool."""

from __future__ import annotations

import os
from typing import Any

VISION_REQUIRED_FOR = "tool:vision_analyze"

DEFAULT_PROVIDER = "qwen"
DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
QWEN_DEFAULT_MODEL = "qwen-vl-max"

DEFAULT_VISION_TIMEOUT = 30.0
DEFAULT_DOWNLOAD_TIMEOUT = 30.0
MAX_IMAGE_SIZE_BYTES = 20 * 1024 * 1024


def env_value(key: str, default: str = "") -> str:
    """Read env values from process env or M-Claw home .env."""
    try:
        from mclaw.cli.config import get_env_value

        return get_env_value(key) or default
    except Exception:
        return os.getenv(key, default)


def authorized_env_value(key: str, default: str = "") -> str:
    try:
        from mclaw.runtime.features import authorized_env_value

        return authorized_env_value(VISION_REQUIRED_FOR, key, env_value, default)
    except Exception:
        return ""


def effective_config(parent_agent: Any = None, config: dict | None = None) -> dict:
    if isinstance(config, dict):
        return config
    if parent_agent is not None:
        cfg = getattr(parent_agent, "config", None)
        if isinstance(cfg, dict):
            return cfg
    try:
        from mclaw.cli.config import load_config

        return load_config()
    except Exception:
        return {}


def vision_config(parent_agent: Any = None, config: dict | None = None) -> dict:
    cfg = effective_config(parent_agent=parent_agent, config=config)
    vision = cfg.get("auxiliary", {}).get("vision", {}) if isinstance(cfg, dict) else {}
    return vision if isinstance(vision, dict) else {}


def resolve_timeout(parent_agent: Any = None) -> float:
    """Vision LLM call timeout: config -> env -> default."""
    try:
        val = vision_config(parent_agent=parent_agent).get("timeout")
        if val is not None:
            return float(val)
    except Exception:
        pass
    env_val = os.getenv("MCLAW_VISION_TIMEOUT", "").strip()
    if env_val:
        try:
            return float(env_val)
        except ValueError:
            pass
    return DEFAULT_VISION_TIMEOUT


def resolve_download_timeout(parent_agent: Any = None) -> float:
    """Image-download timeout: config -> env -> default."""
    try:
        val = vision_config(parent_agent=parent_agent).get("download_timeout")
        if val is not None:
            return float(val)
    except Exception:
        pass
    env_val = os.getenv("MCLAW_VISION_DOWNLOAD_TIMEOUT", "").strip()
    if env_val:
        try:
            return float(env_val)
        except ValueError:
            pass
    return DEFAULT_DOWNLOAD_TIMEOUT
