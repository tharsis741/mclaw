# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tavily web-search backend.

Provides a thin synchronous wrapper around https://api.tavily.com/search and
formats the answer plus sources into lightweight Markdown for the agent.
"""

from __future__ import annotations

import logging
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_TAVILY_API_URL = "https://api.tavily.com/search"


def search(
    query: str,
    strategy: str,
    freshness: Optional[int],
    sites: Optional[str],
    images: bool,
    creds: dict,
    timeout: float,
) -> dict:
    """Search the web using the Tavily API.

    Args:
        query: Search query string.
        strategy: "turbo", "max", or "agent".
        freshness: Only results from last N days (7/30/180/365).
        sites: Comma-separated list of domains to restrict to.
        images: Ignored for Tavily (no effect).
        creds: Dict with "api_key" key.
        timeout: Request timeout in seconds.

    Returns:
        {"success": bool, "results": str, "_backend": "tavily", "_hint": str}
    """
    api_key = creds.get("api_key", "")
    if not api_key:
        logger.warning("Tavily search skipped: missing api_key in creds")
        return {
            "success": False,
            "results": "Tavily API key is missing. Set it in creds['api_key'].",
            "_backend": "tavily",
            "_hint": "Set TAVILY_API_KEY in the M-Claw home .env file.",
        }

    # Map search strategy to Tavily search_depth and max_results.
    if strategy == "turbo":
        search_depth = "basic"
        max_results = 5
    else:
        # max / agent strategies use deeper search and more results.
        search_depth = "advanced"
        max_results = 10

    payload: dict = {
        "api_key": api_key,
        "query": query,
        "search_depth": search_depth,
        "max_results": max_results,
        "include_answer": True,
        "include_raw_content": False,
    }

    # Map freshness to Tavily time_range.
    _FRESHNESS_MAP = {7: "week", 30: "month", 180: "month", 365: "year"}
    if freshness in _FRESHNESS_MAP:
        payload["time_range"] = _FRESHNESS_MAP[freshness]

    # Map sites to include_domains.
    if sites:
        domains = [s.strip() for s in sites.split(",") if s.strip()]
        if domains:
            payload["include_domains"] = domains

    try:
        response = requests.post(_TAVILY_API_URL, json=payload, timeout=timeout)
        response.raise_for_status()
        data = response.json()
    except requests.Timeout:
        logger.warning("Tavily search timed out after %.1fs", timeout)
        return {
            "success": False,
            "results": f"Tavily search timed out after {timeout:.1f}s.",
            "_backend": "tavily",
            "_hint": "Retry or increase timeout in auxiliary.web_search.timeout config.",
        }
    except requests.HTTPError as exc:
        logger.warning("Tavily search HTTP error: %s", exc)
        msg = f"Tavily search HTTP error: {exc}"
        try:
            if exc.response is not None:
                err_body = exc.response.json()
                detail = err_body.get("detail") or err_body.get("message") or err_body.get("error")
                if detail:
                    msg = f"Tavily search error: {detail}"
        except Exception:
            pass
        return {
            "success": False,
            "results": msg,
            "_backend": "tavily",
            "_hint": "Check your Tavily API key and account status.",
        }
    except requests.RequestException as exc:
        logger.warning("Tavily search request failed: %s", exc)
        return {
            "success": False,
            "results": f"Tavily search request failed: {exc}",
            "_backend": "tavily",
            "_hint": "Check network connectivity and Tavily service status.",
        }

    answer = data.get("answer", "")
    sources = data.get("results", [])

    # Build lightweight Markdown output.
    lines: list[str] = []
    if answer:
        lines.append(answer)
    else:
        lines.append("No answer returned by Tavily.")

    if sources:
        lines.append("")
        lines.append("### 来源")
        for idx, src in enumerate(sources, start=1):
            title = src.get("title", "Untitled")
            url = src.get("url", "")
            lines.append(f"[{idx}] {title} — {url}")

    results_text = "\n".join(lines)

    return {
        "success": True,
        "results": results_text,
        "_backend": "tavily",
        "_hint": "",
    }
