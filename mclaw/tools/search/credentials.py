"""Credential resolution for web-search backends."""

from __future__ import annotations

import os
from typing import Callable

from mclaw.tools.search.config import effective_config

WEB_SEARCH_REQUIRED_FOR = "tool:web_search"
_DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"


def env_value(key: str, default: str = "") -> str:
    """Read env values from process env or M-Claw home .env."""
    try:
        from mclaw.cli.config import get_env_value

        return get_env_value(key) or default
    except Exception:
        return os.getenv(key, default)


def authorized_env_value(key: str, default: str = "") -> str:
    try:
        from mclaw.runtime.features import authorized_env_value as _authorized_env_value

        return _authorized_env_value(WEB_SEARCH_REQUIRED_FOR, key, env_value, default)
    except Exception:
        return ""


def is_dashscope_configured(creds: dict) -> bool:
    api_key = creds.get("api_key", "")
    base_url = creds.get("base_url", "")
    if not api_key:
        return False
    return api_key.startswith("sk-dashscope") or "dashscope" in base_url.lower()


def resolve_dashscope_creds(
    parent_agent=None,
    config: dict | None = None,
    load_config_fn: Callable[[], dict] | None = None,
) -> dict[str, str]:
    """Resolve DashScope credentials for Qwen enable_search."""
    cfg = effective_config(parent_agent=parent_agent, config=config, load_config_fn=load_config_fn)
    result: dict[str, str] = {"api_key": "", "base_url": "", "model": ""}

    try:
        web_cfg = cfg.get("auxiliary", {}).get("web_search", {})
        if isinstance(web_cfg, dict):
            if web_cfg.get("base_url"):
                result["base_url"] = str(web_cfg["base_url"])
            if web_cfg.get("model"):
                result["model"] = str(web_cfg["model"])
    except Exception:
        pass

    result["api_key"] = authorized_env_value("DASHSCOPE_API_KEY") or authorized_env_value("QWEN_API_KEY")
    if not result["base_url"]:
        result["base_url"] = env_value("DASHSCOPE_BASE_URL", _DASHSCOPE_BASE_URL)
    return result


def get_tavily_creds() -> dict[str, str]:
    return {"api_key": authorized_env_value("TAVILY_API_KEY")}


def tavily_creds_ok() -> bool:
    return bool(authorized_env_value("TAVILY_API_KEY"))


def dashscope_creds_ok(
    parent_agent=None,
    config: dict | None = None,
    load_config_fn: Callable[[], dict] | None = None,
) -> bool:
    return is_dashscope_configured(
        resolve_dashscope_creds(parent_agent=parent_agent, config=config, load_config_fn=load_config_fn)
    )


def feature_env_configured() -> bool:
    try:
        from mclaw.runtime.features import authorized_configured_env_vars, get_feature

        spec = get_feature("web_search")
        return bool(spec and authorized_configured_env_vars(spec, env_value))
    except Exception:
        return bool(tavily_creds_ok() or authorized_env_value("DASHSCOPE_API_KEY") or authorized_env_value("QWEN_API_KEY"))
