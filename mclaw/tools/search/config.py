# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Configuration helpers for web-search backends."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


_DEFAULT_BACKEND = "auto"
_DEFAULT_TAVILY_TIMEOUT = 30.0
_DEFAULT_DASHSCOPE_TURBO_TIMEOUT = 90.0
_DEFAULT_DASHSCOPE_DEEP_TIMEOUT = 120.0


def _to_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def effective_config(
    parent_agent=None,
    config: dict | None = None,
    load_config_fn: Callable[[], dict] | None = None,
) -> dict:
    if isinstance(config, dict) and config:
        return config
    if parent_agent is not None:
        cfg = getattr(parent_agent, "config", None)
        if isinstance(cfg, dict) and cfg:
            return cfg
    try:
        if load_config_fn is not None:
            return load_config_fn()
        from mclaw.cli.config import load_config

        return load_config()
    except Exception:
        return {}


@dataclass(frozen=True)
class SearchConfig:
    backend: str = _DEFAULT_BACKEND
    timeout: float = _DEFAULT_TAVILY_TIMEOUT
    tavily_timeout: float = _DEFAULT_TAVILY_TIMEOUT
    dashscope_timeout: float = _DEFAULT_DASHSCOPE_TURBO_TIMEOUT
    dashscope_deep_timeout: float = _DEFAULT_DASHSCOPE_DEEP_TIMEOUT
    fallback: bool = True
    model: str = ""
    base_url: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def timeout_for_backend(self, backend: str, strategy: str = "turbo") -> float:
        if backend == "tavily":
            return self.tavily_timeout
        if backend == "dashscope":
            return self.dashscope_deep_timeout if strategy in ("max", "agent") else self.dashscope_timeout
        return self.timeout


def load_search_config(
    parent_agent=None,
    config: dict | None = None,
    load_config_fn: Callable[[], dict] | None = None,
) -> SearchConfig:
    cfg = effective_config(parent_agent=parent_agent, config=config, load_config_fn=load_config_fn)
    web_cfg = cfg.get("auxiliary", {}).get("web_search", {}) if isinstance(cfg, dict) else {}
    if not isinstance(web_cfg, dict):
        web_cfg = {}

    timeout = _to_float(web_cfg.get("timeout"), _DEFAULT_TAVILY_TIMEOUT)
    tavily_timeout = _to_float(web_cfg.get("tavily_timeout"), timeout)
    dashscope_timeout = _to_float(web_cfg.get("dashscope_timeout"), _DEFAULT_DASHSCOPE_TURBO_TIMEOUT)
    dashscope_deep_timeout = _to_float(
        web_cfg.get("dashscope_deep_timeout"),
        _to_float(web_cfg.get("dashscope_max_timeout"), _DEFAULT_DASHSCOPE_DEEP_TIMEOUT),
    )

    return SearchConfig(
        backend=str(web_cfg.get("backend") or _DEFAULT_BACKEND).strip().lower(),
        timeout=timeout,
        tavily_timeout=tavily_timeout,
        dashscope_timeout=dashscope_timeout,
        dashscope_deep_timeout=dashscope_deep_timeout,
        fallback=bool(web_cfg.get("fallback", True)),
        model=str(web_cfg.get("model") or ""),
        base_url=str(web_cfg.get("base_url") or ""),
        raw=dict(web_cfg),
    )
