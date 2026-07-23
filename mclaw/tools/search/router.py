# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Select and execute web-search backends behind one tool contract.

The router normalizes configuration, credential selection, backend exceptions,
and Tavily-to-DashScope fallback so tool callers receive one stable response
shape regardless of provider.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import replace

from mclaw.cli.config import load_config
from mclaw.tools.search.config import load_search_config
from mclaw.tools.search.credentials import (
    dashscope_creds_ok,
    get_tavily_creds,
    resolve_dashscope_creds,
    tavily_creds_ok,
)
from mclaw.tools.search.dashscope_backend import search as dashscope_search
from mclaw.tools.search.profiles import get_search_backend_profile
from mclaw.tools.search.tavily_backend import search as tavily_search
from mclaw.tools.search.types import SearchRequest, SearchResponse

logger = logging.getLogger(__name__)

BACKENDS = {
    "dashscope": dashscope_search,
    "tavily": tavily_search,
}
_ERROR_DETAIL_MAX_CHARS = 500


def _cancelled_response() -> dict:
    return {
        "success": False,
        "error": "Web search interrupted by user",
        "interrupted": True,
        "status": "cancelled",
    }


def _is_cancelled(event: threading.Event | None) -> bool:
    return event is not None and event.is_set()


def _load_config_strict() -> dict:
    return load_config(strict=True)


def _tavily_creds_ok(parent_agent=None) -> bool:
    """Return True if a web_search-scoped Tavily API key is available."""
    return tavily_creds_ok()


def _dashscope_creds_ok(parent_agent=None) -> bool:
    """Return True if web_search-scoped DashScope credentials are available."""
    return dashscope_creds_ok(parent_agent=parent_agent, load_config_fn=_load_config_strict)


def _effective_backend(backend_name: str, parent_agent=None) -> str:
    """Resolve auto mode to the first credentialed backend preference."""
    if backend_name == "auto":
        return "tavily" if _tavily_creds_ok(parent_agent=parent_agent) else "dashscope"
    return backend_name


def _safe_error_detail(exc: BaseException | str) -> str:
    """Redact likely secret material before returning backend failures."""
    detail = str(exc)
    if not isinstance(exc, str):
        detail = f"{type(exc).__name__}: {detail or type(exc).__name__}"
    detail = re.sub(
        r"(?i)\b(api[_-]?key|token|secret|password)\s*[:=]\s*['\"]?[^'\"\s,;]+",
        r"\1=<redacted>",
        detail,
    )
    detail = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "<redacted>", detail)
    if len(detail) > _ERROR_DETAIL_MAX_CHARS:
        detail = detail[:_ERROR_DETAIL_MAX_CHARS] + "...[truncated]"
    return detail


def _invoke_backend(
    backend_name: str,
    request: SearchRequest,
    creds: dict,
    timeout: float,
    *,
    parent_agent=None,
    cancel_event: threading.Event | None = None,
) -> SearchResponse:
    """Call one backend adapter and convert exceptions into SearchResponse."""
    backend_fn = BACKENDS[backend_name]
    try:
        result = backend_fn(
            query=request.query,
            limit=request.limit,
            strategy=request.strategy,
            freshness=request.freshness,
            sites=request.sites,
            images=request.images,
            creds=creds,
            timeout=timeout,
            parent_agent=parent_agent,
            cancel_event=cancel_event,
        )
        return SearchResponse.from_mapping(result, default_backend=backend_name)
    except InterruptedError:
        raise
    except Exception as exc:
        detail = _safe_error_detail(exc)
        logger.warning("%s search backend failed: %s", backend_name, detail, exc_info=True)
        return SearchResponse(
            success=False,
            error=f"{backend_name} search backend failed: {detail}",
            backend=backend_name,
            backend_profile=get_search_backend_profile(backend_name).tool_metadata(),
            hint=f"Check {backend_name} configuration, dependencies, and service status.",
        )


def execute_search(
    query: str,
    strategy: str = "turbo",
    freshness: int | None = None,
    sites: str | None = None,
    images: bool = False,
    parent_agent=None,
    limit: int = 5,
    cancel_event: threading.Event | None = None,
) -> dict:
    """Route search to the configured backend and apply configured fallback."""
    if cancel_event is None:
        from mclaw.tools.interrupt import get_interrupt_event

        cancel_event = get_interrupt_event()
    if _is_cancelled(cancel_event):
        return _cancelled_response()

    request = SearchRequest(
        query=query,
        limit=limit,
        strategy=strategy,
        freshness=freshness,
        sites=sites,
        images=images,
    )
    search_config = load_search_config(parent_agent=parent_agent, load_config_fn=_load_config_strict)
    if _is_cancelled(cancel_event):
        return _cancelled_response()
    backend_name = _effective_backend(search_config.backend, parent_agent=parent_agent)
    if _is_cancelled(cancel_event):
        return _cancelled_response()

    if backend_name not in BACKENDS:
        available = ", ".join(BACKENDS.keys())
        return SearchResponse(
            success=False,
            error=f"未知搜索后端 '{backend_name}'。可用: {available}",
        ).to_dict()

    if backend_name == "tavily":
        creds = get_tavily_creds()
    else:
        creds = resolve_dashscope_creds(parent_agent=parent_agent, load_config_fn=_load_config_strict)

    if _is_cancelled(cancel_event):
        return _cancelled_response()
    result = _invoke_backend(
        backend_name,
        request,
        creds,
        search_config.timeout_for_backend(backend_name, strategy),
        parent_agent=parent_agent,
        cancel_event=cancel_event,
    )

    if _is_cancelled(cancel_event):
        return _cancelled_response()

    if (
        not result.success
        and backend_name == "tavily"
        and search_config.fallback
        and _dashscope_creds_ok(parent_agent=parent_agent)
    ):
        # Fallback is deliberately one-way: DashScope can replace Tavily when
        # Tavily fails, but DashScope failures surface directly to the caller.
        if _is_cancelled(cancel_event):
            return _cancelled_response()
        logger.warning("Tavily failed (%s), falling back to DashScope", result.error)
        fallback_creds = resolve_dashscope_creds(
            parent_agent=parent_agent,
            load_config_fn=_load_config_strict,
        )
        if _is_cancelled(cancel_event):
            return _cancelled_response()
        ds_result = _invoke_backend(
            "dashscope",
            request,
            fallback_creds,
            search_config.timeout_for_backend("dashscope", strategy),
            parent_agent=parent_agent,
            cancel_event=cancel_event,
        )
        if _is_cancelled(cancel_event):
            return _cancelled_response()
        if ds_result.success:
            fallback_hint = "Tavily 失败，已自动降级至 DashScope"
            hint = ds_result.hint
            ds_result = replace(
                ds_result,
                hint=f"{hint} [{fallback_hint}]" if hint else fallback_hint,
                fallback_from="tavily",
                fallback_error=result.error,
            )
        else:
            hint = ds_result.hint
            fallback_hint = f"DashScope fallback also failed after Tavily failure: {result.error}"
            ds_result = replace(
                ds_result,
                hint=f"{hint} [{fallback_hint}]" if hint else fallback_hint,
                fallback_from="tavily",
                fallback_error=result.error,
            )
        result = ds_result

    if _is_cancelled(cancel_event):
        return _cancelled_response()
    return result.to_dict()
