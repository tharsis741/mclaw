"""Search backend router — selects and executes search backend based on config."""

from __future__ import annotations

import logging
from typing import Optional

from mclaw.cli.config import load_config
from mclaw.tools.search.config import load_search_config
from mclaw.tools.search.credentials import (
    dashscope_creds_ok,
    get_tavily_creds,
    resolve_dashscope_creds,
    tavily_creds_ok,
)
from mclaw.tools.search.dashscope_backend import search as dashscope_search
from mclaw.tools.search.tavily_backend import search as tavily_search
from mclaw.tools.search.types import SearchRequest, SearchResponse

logger = logging.getLogger(__name__)

BACKENDS = {
    "dashscope": dashscope_search,
    "tavily": tavily_search,
}


def _tavily_creds_ok(parent_agent=None) -> bool:
    """Return True if a web_search-scoped Tavily API key is available."""
    return tavily_creds_ok()


def _dashscope_creds_ok(parent_agent=None) -> bool:
    """Return True if web_search-scoped DashScope credentials are available."""
    return dashscope_creds_ok(parent_agent=parent_agent, load_config_fn=load_config)


def _effective_backend(backend_name: str, parent_agent=None) -> str:
    if backend_name == "auto":
        return "tavily" if _tavily_creds_ok(parent_agent=parent_agent) else "dashscope"
    return backend_name


def _invoke_backend(backend_name: str, request: SearchRequest, creds: dict, timeout: float) -> SearchResponse:
    backend_fn = BACKENDS[backend_name]
    result = backend_fn(
        query=request.query,
        strategy=request.strategy,
        freshness=request.freshness,
        sites=request.sites,
        images=request.images,
        creds=creds,
        timeout=timeout,
    )
    return SearchResponse.from_mapping(result, default_backend=backend_name)


def execute_search(
    query: str,
    strategy: str = "turbo",
    freshness: Optional[int] = None,
    sites: Optional[str] = None,
    images: bool = False,
    parent_agent=None,
) -> dict:
    """Route search to the configured backend."""
    request = SearchRequest(
        query=query,
        strategy=strategy,
        freshness=freshness,
        sites=sites,
        images=images,
    )
    search_config = load_search_config(parent_agent=parent_agent, load_config_fn=load_config)
    backend_name = _effective_backend(search_config.backend, parent_agent=parent_agent)

    if backend_name not in BACKENDS:
        available = ", ".join(BACKENDS.keys())
        return SearchResponse(
            success=False,
            results=f"未知搜索后端 '{backend_name}'。可用: {available}",
        ).to_dict()

    if backend_name == "tavily":
        creds = get_tavily_creds()
    else:
        creds = resolve_dashscope_creds(parent_agent=parent_agent, load_config_fn=load_config)

    result = _invoke_backend(
        backend_name,
        request,
        creds,
        search_config.timeout_for_backend(backend_name, strategy),
    )

    if (
        not result.success
        and backend_name == "tavily"
        and search_config.fallback
        and _dashscope_creds_ok(parent_agent=parent_agent)
    ):
        logger.warning("Tavily failed (%s), falling back to DashScope", result.results)
        ds_result = _invoke_backend(
            "dashscope",
            request,
            resolve_dashscope_creds(parent_agent=parent_agent, load_config_fn=load_config),
            search_config.timeout_for_backend("dashscope", strategy),
        )
        if ds_result.success:
            fallback_hint = "Tavily 失败，已自动降级至 DashScope"
            hint = ds_result.hint
            ds_result = SearchResponse(
                success=True,
                results=ds_result.results,
                backend=ds_result.backend,
                hint=f"{hint} [{fallback_hint}]" if hint else fallback_hint,
            )
        result = ds_result

    return result.to_dict()
