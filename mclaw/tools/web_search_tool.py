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
from mclaw.tools.search.credentials import (
    QWEN_CREDENTIAL_HINT,
    dashscope_creds_ok,
    tavily_creds_ok,
)

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
        return {"available": False, "reason": f"web_search backend=dashscope but {QWEN_CREDENTIAL_HINT} is missing or not authorized", "fix": "Call secret_request_many(required_for='tool:web_search', ...) or rerun setup."}
    if backend == "auto":
        if tavily_ok:
            return {"available": True, "reason": "auto backend will use Tavily", "fix": ""}
        if dashscope_ok:
            return {"available": True, "reason": "auto backend will use DashScope fallback", "fix": ""}
        return {
            "available": False,
            "reason": "web_search backend=auto but no Tavily or DashScope key was found",
            "fix": f"Set TAVILY_API_KEY or one of {QWEN_CREDENTIAL_HINT} in the M-Claw home .env file.",
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
    limit: int = 5,
    parent_agent=None,
) -> str:
    """Search the web through the configured backend router.

    Args:
        query: Search query string.
        limit: Maximum number of source candidates to return (1-10).
        parent_agent: Optional parent agent for config and credential scoping.

    Returns:
        JSON string with {success, answer, sources, _backend_profile, _hint?}.
    """
    if not query or not isinstance(query, str):
        return tool_error("query is required", success=False)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10:
        return tool_error("limit must be an integer between 1 and 10", success=False)

    logger.info("Web search: %s (limit=%d)", query[:80], limit)

    from mclaw.tools.search.router import execute_search

    try:
        result = execute_search(
            query=query,
            limit=limit,
            parent_agent=parent_agent,
        )
    except ConfigError as exc:
        return tool_error(f"Configuration error: {exc}", success=False)

    if not result.get("success"):
        return tool_error(
            result.get("error", "Search failed"),
            success=False,
            _backend=result.get("_backend", ""),
            _backend_profile=result.get("_backend_profile", {}),
            _hint=result.get("_hint", ""),
            _fallback_from=result.get("_fallback_from", ""),
            _fallback_error=result.get("_fallback_error", ""),
        )

    payload: dict[str, Any] = {
        "success": True,
        "answer": str(result.get("answer") or ""),
        "sources": result.get("sources") if isinstance(result.get("sources"), list) else [],
    }
    for src_key, dst_key in (
        ("_backend", "_backend"),
        ("_backend_profile", "_backend_profile"),
        ("_hint", "_hint"),
        ("_fallback_from", "_fallback_from"),
        ("_fallback_error", "_fallback_error"),
    ):
        value = result.get(src_key)
        if value:
            payload[dst_key] = value

    logger.info(
        "Web search completed (%d answer chars, %d sources)",
        len(payload["answer"]),
        len(payload["sources"]),
    )
    return json.dumps(payload, ensure_ascii=False)


# ── Registry ────────────────────────────────────────────────────────────────

WEB_SEARCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "Search the internet and return an answer with sources",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "default": 5,
                    "description": "Maximum number of sources to return.",
                },
            },
            "required": ["query"],
        },
    },
}


def _handle_web_search(args: dict, **kw) -> str:
    return web_search(
        query=args.get("query", ""),
        limit=args.get("limit", 5),
        parent_agent=kw.get("parent_agent"),
    )


registry.register(
    name="web_search",
    toolset="web",
    schema=WEB_SEARCH_SCHEMA,
    handler=_handle_web_search,
    check_fn=check_web_search_requirements,
    diagnose_fn=diagnose_web_search_requirements,
    description="Search the internet and return an answer with sources",
    emoji="🌐",
    max_result_size_chars=15_000,
)
