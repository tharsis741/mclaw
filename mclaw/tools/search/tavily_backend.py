# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tavily web-search backend returning an answer and structured sources."""

from __future__ import annotations

import asyncio
import logging
import threading

import httpx

from mclaw.tools.cancellation import cancellation_checkpoint
from mclaw.tools.search.profiles import get_search_backend_profile

logger = logging.getLogger(__name__)

_TAVILY_API_URL = "https://api.tavily.com/search"


async def _request_tavily(
    *,
    headers: dict,
    payload: dict,
    timeout: float,
) -> dict:
    """Issue one natively cancellable Tavily request with an absolute deadline."""
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with asyncio.timeout(max(0.001, timeout)):
            response = await client.post(
                _TAVILY_API_URL,
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
    if not isinstance(data, dict):
        raise ValueError("Tavily returned a non-object response.")
    return data


def _run_request(coro, *, parent_agent, timeout: float):
    from mclaw.tools.dispatch import _run_async

    return _run_async(
        coro,
        parent_agent=parent_agent,
        diagnostic_name="web_search_tavily_http",
        timeout_seconds=timeout,
        raise_on_stop=True,
    )


def search(
    query: str,
    strategy: str,
    freshness: int | None,
    sites: str | None,
    images: bool,
    creds: dict,
    timeout: float,
    limit: int = 5,
    parent_agent=None,
    cancel_event: threading.Event | None = None,
) -> dict:
    """Search the web using the Tavily API.

    This adapter converts the router's common SearchRequest fields into Tavily
    payload options and returns the same response envelope as other backends.

    Args:
        query: Search query string.
        strategy: "turbo", "max", or "agent".
        freshness: Only results from last N days (7/30/180/365).
        sites: Comma-separated list of domains to restrict to.
        images: Ignored for Tavily (no effect).
        creds: Dict with "api_key" key.
        timeout: Request timeout in seconds.

    Returns:
        A provider-neutral mapping with ``answer`` and structured ``sources``.
    """
    cancellation_checkpoint(cancel_event)
    profile = get_search_backend_profile("tavily")
    api_key = creds.get("api_key", "")
    if not api_key:
        logger.warning("Tavily search skipped: missing api_key in creds")
        return {
            "success": False,
            "error": "Tavily API key is missing. Set it in creds['api_key'].",
            "_backend": "tavily",
            "_backend_profile": profile.tool_metadata(),
            "_hint": "Set TAVILY_API_KEY in the M-Claw home .env file.",
        }

    # Keep legacy strategy support inside the backend while the model-facing
    # tool exposes only query + result limit.
    if strategy == "turbo":
        search_depth = "basic"
        answer_depth = "basic"
    else:
        search_depth = "advanced"
        answer_depth = "advanced"
    max_results = max(1, min(int(limit), 10))

    payload: dict = {
        "query": query,
        "search_depth": search_depth,
        "max_results": max_results,
        "include_answer": answer_depth,
        "include_raw_content": False,
    }

    # Tavily has coarse time windows, so several requested freshness values
    # intentionally collapse to the nearest supported range.
    _FRESHNESS_MAP = {7: "week", 30: "month", 180: "month", 365: "year"}
    if freshness in _FRESHNESS_MAP:
        payload["time_range"] = _FRESHNESS_MAP[freshness]

    # Domain filters are provider-side allowlists, not post-filtered results.
    if sites:
        domains = [s.strip() for s in sites.split(",") if s.strip()]
        if domains:
            payload["include_domains"] = domains

    try:
        data = _run_request(
            _request_tavily(
                headers={"Authorization": f"Bearer {api_key}"},
                payload=payload,
                timeout=timeout,
            ),
            parent_agent=parent_agent,
            timeout=timeout,
        )
        cancellation_checkpoint(cancel_event)
    except (TimeoutError, httpx.TimeoutException):
        logger.warning("Tavily search timed out after %.1fs", timeout)
        return {
            "success": False,
            "error": f"Tavily search timed out after {timeout:.1f}s.",
            "_backend": "tavily",
            "_backend_profile": profile.tool_metadata(),
            "_hint": "Retry or increase timeout in auxiliary.web_search.tavily_timeout config.",
        }
    except httpx.HTTPStatusError as exc:
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
            "error": msg,
            "_backend": "tavily",
            "_backend_profile": profile.tool_metadata(),
            "_hint": "Check your Tavily API key and account status.",
        }
    except httpx.HTTPError as exc:
        logger.warning("Tavily search request failed: %s", exc)
        return {
            "success": False,
            "error": f"Tavily search request failed: {exc}",
            "_backend": "tavily",
            "_backend_profile": profile.tool_metadata(),
            "_hint": "Check network connectivity and Tavily service status.",
        }

    raw_sources = data.get("results")
    sources = [
        {
            "index": idx,
            "title": str(src.get("title") or "Untitled"),
            "url": str(src.get("url") or ""),
            "snippet": str(src.get("content") or "").strip(),
        }
        for idx, src in enumerate(raw_sources, start=1)
        if isinstance(src, dict) and src.get("url")
    ] if isinstance(raw_sources, list) else []
    answer = str(data.get("answer") or "").strip()
    hint = "" if answer else "Tavily returned sources without a generated answer."

    return {
        "success": True,
        "answer": answer,
        "sources": sources,
        "_backend": "tavily",
        "_backend_profile": {
            **profile.tool_metadata(),
            "search_depth": search_depth,
            "answer_depth": answer_depth,
        },
        "_hint": hint,
    }
