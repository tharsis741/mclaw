# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tool-facing web search registration and availability diagnostics.

The router owns backend selection. This layer keeps the model-facing schema,
credential diagnostics, and result envelope stable for the tool registry.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from mclaw.cli.config import ConfigError
from mclaw.tools.registry import registry, tool_error
from mclaw.tools.search.config import load_search_config
from mclaw.tools.search.credentials import dashscope_creds_ok, tavily_creds_ok

logger = logging.getLogger(__name__)


def diagnose_web_search_requirements(config: dict | None = None) -> dict:
    """Return registry diagnostics without exposing credential values."""
    try:
        search_config = load_search_config(config=config)
    except ConfigError as exc:
        return {"available": False, "reason": f"web search config check failed: {type(exc).__name__}: {exc}", "fix": "Check auxiliary.web_search config."}

    backend = search_config.backend
    fallback = search_config.fallback
    tavily_ok = tavily_creds_ok()
    dashscope_ok = dashscope_creds_ok(config=config)

    if backend == "tavily":
        if tavily_ok:
            return {"available": True, "reason": "Tavily key configured", "fix": ""}
        if fallback and dashscope_ok:
            return {"available": True, "reason": "Tavily missing but DashScope fallback is configured", "fix": ""}
        return {"available": False, "reason": "web_search backend=tavily but TAVILY_API_KEY is missing or not authorized", "fix": "Call secret_request_many(required_for='tool:web_search', ...) or rerun setup."}
    if backend == "dashscope":
        if dashscope_ok:
            return {"available": True, "reason": "DashScope web search credentials configured", "fix": ""}
        return {"available": False, "reason": "web_search backend=dashscope but DASHSCOPE_API_KEY/QWEN_API_KEY is missing or not authorized", "fix": "Call secret_request_many(required_for='tool:web_search', ...) or rerun setup."}
    if backend == "auto":
        if tavily_ok:
            return {"available": True, "reason": "auto backend will use Tavily", "fix": ""}
        if dashscope_ok:
            return {"available": True, "reason": "auto backend will use DashScope fallback", "fix": ""}
        return {
            "available": False,
            "reason": "web_search backend=auto but no Tavily or DashScope key was found",
            "fix": "Set TAVILY_API_KEY, DASHSCOPE_API_KEY, or QWEN_API_KEY in the M-Claw home .env file.",
        }
    return {"available": False, "reason": f"unknown web_search backend: {backend}", "fix": "Use backend tavily, dashscope, or auto."}


def check_web_search_requirements(config: dict | None = None) -> bool:
    """Return True if the configured search backend can run."""
    try:
        search_config = load_search_config(config=config)
    except ConfigError:
        return False

    backend = search_config.backend
    fallback = search_config.fallback
    tavily_ok = tavily_creds_ok()
    dashscope_ok = dashscope_creds_ok(config=config)

    if backend == "tavily":
        return tavily_ok or (fallback and dashscope_ok)
    if backend == "dashscope":
        return dashscope_ok
    if backend == "auto":
        return tavily_ok or dashscope_ok
    return False


# ── Thin wrapper around router.execute_search ──────────────────────────────────────

def web_search(
    query: str,
    strategy: str = "turbo",
    freshness: int | None = None,
    sites: str | None = None,
    images: bool = False,
    parent_agent=None,
) -> str:
    """Search the web through the configured backend router.

    Args:
        query: Search query string.
        strategy: "turbo" (fast), "max" (thorough), or "agent" (multi-round).
        freshness: Only results from last N days (7/30/180/365).
        sites: Comma-separated list of domains to restrict to.
        images: Include images in the response.
        parent_agent: Optional parent agent for config and credential scoping.

    Returns:
        JSON string with {success, results, _hint?}.
    """
    if not query or not isinstance(query, str):
        return tool_error("query is required", success=False)

    logger.info(
        "Web search: %s (strategy=%s, images=%s)",
        query[:80],
        strategy,
        images,
    )

    from mclaw.tools.search.router import execute_search

    try:
        result = execute_search(
            query=query,
            strategy=strategy,
            freshness=freshness,
            sites=sites,
            images=images,
            parent_agent=parent_agent,
        )
    except ConfigError as exc:
        return tool_error(f"Configuration error: {exc}", success=False)

    if not result.get("success"):
        return tool_error(
            result.get("results", "Search failed"),
            success=False,
            backend=result.get("_backend", ""),
            hint=result.get("_hint", ""),
            fallback_from=result.get("_fallback_from", ""),
            fallback_error=result.get("_fallback_error", ""),
        )

    payload: dict[str, Any] = {"success": True, "results": result.get("results", "")}
    for src_key, dst_key in (
        ("_backend", "_backend"),
        ("_hint", "_hint"),
        ("_fallback_from", "_fallback_from"),
        ("_fallback_error", "_fallback_error"),
    ):
        value = result.get(src_key)
        if value:
            payload[dst_key] = value

    logger.info("Web search completed (%d chars)", len(payload["results"]))
    return json.dumps(payload, ensure_ascii=False)


# ── Registry ────────────────────────────────────────────────────────────────

WEB_SEARCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Search the web for real-time information using the configured backend "
            "(DashScope/Qwen or Tavily). Returns a comprehensive answer with source citations - this result "
            "is usually sufficient to answer the user's question.\n\n"
            "DO NOT call browser_navigate or browser_snapshot after using this tool "
            "to fetch the same information again. The search result already aggregates "
            "multiple sources and includes current data. Only use browser tools when "
            "the user explicitly asks you to visit a specific website or perform actions "
            "on a web page (clicking, filling forms, downloading).\n\n"
            "Use this tool when: (1) the user asks about current events, news, weather, "
            "stock prices, or anything requiring up-to-date information; (2) the user asks "
            "you to 'search', 'look up', 'find out', or 'check' something online; (3) the "
            "user asks a factual question your training data may not cover.\n\n"
            "Strategies:\n"
            "- turbo (default): fast, good for quick facts\n"
            "- max: thorough multi-source verification\n"
            "- agent: multi-round retrieval for complex questions\n\n"
            "Results include citation markers like [1], [2] - preserve these in your response."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query. Be specific for better results.",
                },
                "strategy": {
                    "type": "string",
                    "enum": ["turbo", "max", "agent"],
                    "description": (
                        "Search strategy. "
                        "turbo=fast general search (~60-90s). "
                        "max=thorough multi-source verification (~60-120s), use for broad/hot topics or when turbo times out. "
                        "agent=multi-round deep research."
                    ),
                },
                "freshness": {
                    "type": "integer",
                    "enum": [7, 30, 180, 365],
                    "description": "Only return results from the last N days. Only works with turbo strategy.",
                },
                "sites": {
                    "type": "string",
                    "description": "Comma-separated domain list to restrict search to (e.g. 'github.com,arxiv.org'). Only works with turbo strategy.",
                },
                "images": {
                    "type": "boolean",
                    "description": "Include images in the response. Results may contain Markdown image links.",
                },
            },
            "required": ["query"],
        },
    },
}


def _handle_web_search(args: dict, **kw) -> str:
    return web_search(
        query=args.get("query", ""),
        strategy=args.get("strategy", "turbo"),
        freshness=args.get("freshness"),
        sites=args.get("sites"),
        images=args.get("images", False),
        parent_agent=kw.get("parent_agent"),
    )


registry.register(
    name="web_search",
    toolset="web",
    schema=WEB_SEARCH_SCHEMA,
    handler=_handle_web_search,
    check_fn=check_web_search_requirements,
    diagnose_fn=diagnose_web_search_requirements,
    description="Search the web for current information",
    emoji="🌐",
    max_result_size_chars=15_000,
)
