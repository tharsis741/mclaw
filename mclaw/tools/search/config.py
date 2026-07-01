# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Configuration helpers for web-search backend selection and timeouts."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from mclaw.cli.config import ConfigError


_DEFAULT_BACKEND = "auto"
_DEFAULT_TAVILY_TIMEOUT = 30.0
_DEFAULT_DASHSCOPE_TURBO_TIMEOUT = 90.0
_DEFAULT_DASHSCOPE_DEEP_TIMEOUT = 120.0


def _to_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_bool_config(value: Any, *, default: bool = True) -> bool:
    """Parse booleans from config values without treating every string as true."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if not normalized:
            return default
        if normalized in {"1", "true", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "no", "n", "off"}:
            return False
    raise ConfigError(f"Invalid boolean value for auxiliary.web_search.fallback: {value!r}")


def effective_config(
    parent_agent=None,
    config: dict | None = None,
    load_config_fn: Callable[[], dict] | None = None,
) -> dict:
    """Resolve config from explicit args, parent agent state, or global loader."""
    if isinstance(config, dict) and config:
        return config
    if parent_agent is not None:
        cfg = getattr(parent_agent, "config", None)
        if isinstance(cfg, dict) and cfg:
            return cfg
    if load_config_fn is not None:
        return load_config_fn()
    from mclaw.cli.config import load_config

    return load_config(strict=True)


@dataclass(frozen=True)
class SearchConfig:
    """Normalized web-search settings consumed by router and diagnostics."""
    backend: str = _DEFAULT_BACKEND
    tavily_timeout: float = _DEFAULT_TAVILY_TIMEOUT
    dashscope_timeout: float = _DEFAULT_DASHSCOPE_TURBO_TIMEOUT
    dashscope_deep_timeout: float = _DEFAULT_DASHSCOPE_DEEP_TIMEOUT
    fallback: bool = True
    model: str = ""
    base_url: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def timeout_for_backend(self, backend: str, strategy: str = "turbo") -> float:
        """Return the timeout aligned with backend and retrieval strategy depth."""
        if backend == "tavily":
            return self.tavily_timeout
        if backend == "dashscope":
            return self.dashscope_deep_timeout if strategy in ("max", "agent") else self.dashscope_timeout
        return self.tavily_timeout


def load_search_config(
    parent_agent=None,
    config: dict | None = None,
    load_config_fn: Callable[[], dict] | None = None,
) -> SearchConfig:
    """Load and normalize auxiliary.web_search config with validated fallback."""
    cfg = effective_config(parent_agent=parent_agent, config=config, load_config_fn=load_config_fn)
    web_cfg = cfg.get("auxiliary", {}).get("web_search", {}) if isinstance(cfg, dict) else {}
    if not isinstance(web_cfg, dict):
        web_cfg = {}

    tavily_timeout = _to_float(web_cfg.get("tavily_timeout"), _DEFAULT_TAVILY_TIMEOUT)
    dashscope_timeout = _to_float(web_cfg.get("dashscope_timeout"), _DEFAULT_DASHSCOPE_TURBO_TIMEOUT)
    dashscope_deep_timeout = _to_float(
        web_cfg.get("dashscope_deep_timeout"),
        _DEFAULT_DASHSCOPE_DEEP_TIMEOUT,
    )

    return SearchConfig(
        backend=str(web_cfg.get("backend") or _DEFAULT_BACKEND).strip().lower(),
        tavily_timeout=tavily_timeout,
        dashscope_timeout=dashscope_timeout,
        dashscope_deep_timeout=dashscope_deep_timeout,
        fallback=parse_bool_config(web_cfg.get("fallback"), default=True),
        model=str(web_cfg.get("model") or ""),
        base_url=str(web_cfg.get("base_url") or ""),
        raw=dict(web_cfg),
    )
