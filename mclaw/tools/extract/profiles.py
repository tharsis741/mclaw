# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Maintained behavior profiles for each ``web_extract`` backend.

The public tool deliberately exposes only a list of URLs. Provider-specific
accuracy, freshness, cost, and rendering choices live here so the adapters,
slash command, tests, and model-facing diagnostics cannot silently drift.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


DEFAULT_FIRECRAWL_API_URL = "https://api.firecrawl.dev/v2/scrape"


@dataclass(frozen=True)
class ExtractBackendProfile:
    """Stable provider behavior hidden behind the small public tool schema."""

    name: str
    display_name: str
    mode: str
    javascript_rendering: bool
    freshness: str
    pricing: str
    status_description_zh: str
    request_defaults: tuple[tuple[str, Any], ...]

    def request_options(self) -> dict[str, Any]:
        """Return a mutable request-options copy for one adapter invocation."""
        options: dict[str, Any] = {}
        for key, value in self.request_defaults:
            # Tuples keep the frozen profile immutable but JSON arrays should
            # still be emitted as lists in captured requests and wire payloads.
            options[key] = list(value) if isinstance(value, tuple) else value
        return options

    def tool_metadata(self) -> dict[str, Any]:
        """Return compact, provider-neutral behavior metadata for the agent."""
        return {
            "mode": self.mode,
            "javascript_rendering": self.javascript_rendering,
            "freshness": self.freshness,
            "pricing": self.pricing,
        }


EXTRACT_BACKEND_PROFILES: dict[str, ExtractBackendProfile] = {
    "trafilatura": ExtractBackendProfile(
        name="trafilatura",
        display_name="Trafilatura",
        mode="local",
        javascript_rendering=False,
        freshness="direct_fetch",
        pricing="free_local",
        status_description_zh="本地直连静态 HTML；免费、无 JS 渲染、无供应商缓存",
        request_defaults=(
            ("output_format", "markdown"),
            ("fast", False),
            ("favor_precision", False),
            ("favor_recall", False),
            ("include_comments", False),
            ("include_links", True),
            ("include_tables", True),
            ("include_images", False),
            ("include_formatting", True),
        ),
    ),
    "firecrawl": ExtractBackendProfile(
        name="firecrawl",
        display_name="Firecrawl",
        mode="cloud",
        javascript_rendering=True,
        freshness="forced_fresh",
        pricing="provider_credits",
        status_description_zh="云端渲染；强制新鲜抓取、正文过滤开启、LLM 清洗关闭",
        request_defaults=(
            ("formats", ("markdown",)),
            ("onlyMainContent", True),
            ("onlyCleanContent", False),
            ("maxAge", 0),
            ("proxy", "auto"),
            ("blockAds", True),
            ("removeBase64Images", True),
        ),
    ),
    "tavily": ExtractBackendProfile(
        name="tavily",
        display_name="Tavily",
        mode="cloud",
        # Tavily documents higher success, tables, and embedded content for
        # advanced mode, but does not promise general browser-style JS rendering.
        javascript_rendering=False,
        freshness="provider_managed",
        pricing="provider_credits_advanced",
        status_description_zh="云端 advanced 提取；完整正文模式、返回用量、不做 query 分块",
        request_defaults=(
            ("extract_depth", "advanced"),
            ("include_images", False),
            ("include_usage", True),
            ("format", "markdown"),
        ),
    ),
}

VALID_EXTRACT_BACKENDS = tuple(EXTRACT_BACKEND_PROFILES)


def get_extract_backend_profile(name: str) -> ExtractBackendProfile:
    """Return a known backend profile or raise a descriptive ``KeyError``."""
    normalized = str(name or "").strip().lower()
    try:
        return EXTRACT_BACKEND_PROFILES[normalized]
    except KeyError as exc:
        available = ", ".join(VALID_EXTRACT_BACKENDS)
        raise KeyError(f"unknown extraction backend {name!r}; available: {available}") from exc
