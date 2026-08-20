# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Configuration helpers for the vision tool."""

from __future__ import annotations

import os
from typing import Any

from mclaw.providers.registry import get_runtime_profile

VISION_REQUIRED_FOR = "tool:vision_analyze"

DEFAULT_PROVIDER = "qwen"
_QWEN_PROFILE = get_runtime_profile(DEFAULT_PROVIDER)
DASHSCOPE_BASE_URL = _QWEN_PROFILE.base_url
QWEN_BASE_URL_ENV_VAR = _QWEN_PROFILE.base_url_env_var
QWEN_CREDENTIAL_ENV_VARS = _QWEN_PROFILE.env_vars
QWEN_DEFAULT_MODEL = "qwen3-vl-flash"

DEFAULT_VISION_TIMEOUT = 30.0
DEFAULT_DOWNLOAD_TIMEOUT = 30.0
DEFAULT_MAX_PIXELS = 1_310_720
MIN_MAX_PIXELS = 65_536
MAX_MAX_PIXELS = 16_777_216
MAX_IMAGE_SIZE_BYTES = 20 * 1024 * 1024


def env_value(key: str, default: str = "") -> str:
    """Read env values from process env or M-Claw home .env."""
    try:
        from mclaw.cli.config import get_env_value

        return get_env_value(key) or default
    except Exception:
        return os.getenv(key, default)


def authorized_env_value(key: str, default: str = "") -> str:
    """Read an env value only when authorized for the vision tool scope."""
    try:
        from mclaw.runtime.features import authorized_env_value as _authorized_env_value

        return _authorized_env_value(VISION_REQUIRED_FOR, key, env_value, default)
    except Exception:
        return ""


def effective_config(parent_agent: Any = None, config: dict | None = None) -> dict:
    """Resolve config from explicit args, parent agent state, or global loader."""
    if isinstance(config, dict):
        return config
    if parent_agent is not None:
        cfg = getattr(parent_agent, "config", None)
        if isinstance(cfg, dict):
            return cfg
    from mclaw.cli.config import load_config

    return load_config(strict=True)


def vision_config(parent_agent: Any = None, config: dict | None = None) -> dict:
    """Return the auxiliary.vision section as a normalized dictionary."""
    cfg = effective_config(parent_agent=parent_agent, config=config)
    vision = cfg.get("auxiliary", {}).get("vision", {}) if isinstance(cfg, dict) else {}
    return vision if isinstance(vision, dict) else {}


def resolve_timeout(parent_agent: Any = None) -> float:
    """Vision LLM call timeout: config -> env -> default."""
    val = vision_config(parent_agent=parent_agent).get("timeout")
    if val is not None:
        try:
            return float(val)
        except (TypeError, ValueError):
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
    val = vision_config(parent_agent=parent_agent).get("download_timeout")
    if val is not None:
        try:
            return float(val)
        except (TypeError, ValueError):
            pass
    env_val = os.getenv("MCLAW_VISION_DOWNLOAD_TIMEOUT", "").strip()
    if env_val:
        try:
            return float(env_val)
        except ValueError:
            pass
    return DEFAULT_DOWNLOAD_TIMEOUT


def resolve_max_pixels(parent_agent: Any = None) -> int:
    """Return a bounded pixel budget for vision image preprocessing."""
    val = vision_config(parent_agent=parent_agent).get("max_pixels")
    if type(val) is int and MIN_MAX_PIXELS <= val <= MAX_MAX_PIXELS:
        return val
    return DEFAULT_MAX_PIXELS
