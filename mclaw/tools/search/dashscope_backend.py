# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""DashScope web search through the native protocol.

The OpenAI-compatible endpoint returns only generated text. The native
DashScope protocol also returns ``search_info.search_results``, which lets the
shared tool keep generated answers separate from supporting sources.
"""

from __future__ import annotations

import inspect
import logging
import re
import threading
from typing import Any

from mclaw.tools.cancellation import cancellation_checkpoint
from mclaw.tools.search.credentials import (
    DASHSCOPE_BASE_URL,
    QWEN_CREDENTIAL_HINT,
    is_dashscope_configured,
)
from mclaw.tools.search.profiles import get_search_backend_profile

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "qwen-plus"
_MAX_WEB_SEARCH_CHARS = 12_000


def _convert_html_images_to_markdown(text: str) -> str:
    text = re.sub(r'<img\s+src="([^"]+)"\s+alt="([^"]*)"[^>]*>', r'![\2](\1)', text)
    text = re.sub(r'<p\s+align="center">\s*', "", text)
    return re.sub(r"</p>", "", text)


def _native_base_address(base_url: str) -> str:
    base = str(base_url or DASHSCOPE_BASE_URL).rstrip("/")
    suffix = "/compatible-mode/v1"
    return base.removesuffix(suffix) + "/api/v1" if base.endswith(suffix) else base


def _value(obj: Any, key: str, default: Any = None) -> Any:
    getter = getattr(obj, "get", None)
    if callable(getter):
        return getter(key, default)
    return getattr(obj, key, default)


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(
        str(item.get("text") or "")
        for item in content
        if isinstance(item, dict)
    )


def _response_parts(response: Any) -> tuple[str, list[dict[str, Any]], bool, str]:
    status_code = _value(response, "status_code", 200)
    if status_code and int(status_code) != 200:
        code = str(_value(response, "code", "") or "")
        message = str(_value(response, "message", "") or "DashScope request failed")
        return "", [], False, f"{code}: {message}" if code else message

    output = _value(response, "output", {}) or {}
    search_info = _value(output, "search_info")
    search_seen = search_info is not None
    raw_sources = _value(search_info, "search_results", []) if search_seen else []
    sources = [dict(item) for item in raw_sources if isinstance(item, dict)] if isinstance(raw_sources, list) else []
    choices = _value(output, "choices", []) or []
    message = _value(choices[0], "message", {}) if choices else {}
    answer = _content_text(_value(message, "content", ""))
    return answer, sources, search_seen, ""


async def _call_generation(**kwargs):
    from dashscope import AioGeneration

    return await AioGeneration.call(**kwargs)


async def _call_multimodal(**kwargs):
    from dashscope import AioMultiModalConversation

    responses = await AioMultiModalConversation.call(**kwargs)
    if hasattr(responses, "__aiter__"):
        return [response async for response in responses]
    return responses


async def _invoke_call(call, kwargs: dict):
    value = call(**kwargs)
    return await value if inspect.isawaitable(value) else value


def _run_request(coro, *, parent_agent, timeout: float):
    from mclaw.tools.dispatch import _run_async

    return _run_async(
        coro,
        parent_agent=parent_agent,
        diagnostic_name="web_search_dashscope_http",
        timeout_seconds=timeout,
        raise_on_stop=True,
    )


def _uses_multimodal_api(model: str) -> bool:
    normalized = model.strip().lower()
    return normalized.startswith("qwen3.5-") or "omni" in normalized


def _normalize_sources(raw_sources: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    for fallback_index, item in enumerate(raw_sources[:limit], start=1):
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        try:
            index = int(item.get("index") or fallback_index)
        except (TypeError, ValueError):
            index = fallback_index
        sources.append({
            "index": index,
            "title": str(item.get("title") or url),
            "url": url,
            "snippet": "",
        })
    return sources


def _truncate_answer(answer: str) -> tuple[str, str]:
    if len(answer) <= _MAX_WEB_SEARCH_CHARS:
        return answer, ""
    omitted = len(answer) - _MAX_WEB_SEARCH_CHARS
    truncated = answer[:_MAX_WEB_SEARCH_CHARS] + f"\n\n[... {omitted:,} characters truncated ...]"
    return truncated, f"Generated answer was truncated by {omitted:,} characters."


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
    """Return a Qwen-generated answer plus native DashScope source links."""
    cancellation_checkpoint(cancel_event)
    profile = get_search_backend_profile("dashscope")
    profile_metadata = profile.tool_metadata()
    api_key = creds.get("api_key", "")
    base_url = creds.get("base_url", "") or DASHSCOPE_BASE_URL
    model = creds.get("model", "") or _DEFAULT_MODEL

    if timeout <= 0:
        timeout = 120.0 if strategy in ("max", "agent") else 90.0

    if not api_key:
        return {
            "success": False,
            "error": f"No DashScope API key is available. Set {QWEN_CREDENTIAL_HINT}.",
            "_backend": "dashscope",
            "_backend_profile": profile_metadata,
            "_hint": f"Set {QWEN_CREDENTIAL_HINT} in the M-Claw home .env file.",
        }
    if not is_dashscope_configured(creds):
        return {
            "success": False,
            "error": "web_search requires DashScope credentials and a DashScope endpoint.",
            "_backend": "dashscope",
            "_backend_profile": profile_metadata,
            "_hint": f"Set {QWEN_CREDENTIAL_HINT} in the M-Claw home .env file.",
        }
    if not model.lower().startswith("qwen"):
        logger.warning("DashScope search model '%s' is not Qwen; using %s", model, _DEFAULT_MODEL)
        model = _DEFAULT_MODEL

    max_sources = max(1, min(int(limit), 10))
    multimodal = _uses_multimodal_api(model)
    actual_strategy = "agent" if multimodal else strategy
    search_options: dict[str, Any] = {
        "search_strategy": actual_strategy,
        "forced_search": True,
        "enable_source": True,
        "enable_citation": True,
        "citation_format": "[<number>]",
    }
    if freshness and actual_strategy == "turbo":
        search_options["freshness"] = freshness
    if sites and actual_strategy == "turbo":
        search_options["assigned_site_list"] = [s.strip() for s in sites.split(",") if s.strip()]

    prompt = (
        f"请联网搜索并简要回答：{query}\n"
        f"最多引用前 {max_sources} 个来源，并使用 [序号] 标注引用。"
    )
    request = {
        "api_key": api_key,
        "model": model,
        "enable_search": True,
        "search_options": search_options,
        "base_address": _native_base_address(base_url),
        "request_timeout": timeout,
    }

    if multimodal:
        responses = _run_request(
            _invoke_call(
                _call_multimodal,
                {
                    **request,
                    "messages": [{"role": "user", "content": [{"text": prompt}]}],
                    "stream": True,
                    "incremental_output": True,
                },
            ),
            parent_agent=parent_agent,
            timeout=timeout,
        )
        cancellation_checkpoint(cancel_event)
        answer_parts: list[str] = []
        raw_sources: list[dict[str, Any]] = []
        search_seen = False
        for response in responses:
            cancellation_checkpoint(cancel_event)
            answer_part, response_sources, response_search_seen, error = _response_parts(response)
            if error:
                return {
                    "success": False,
                    "error": f"DashScope search failed: {error}",
                    "_backend": "dashscope",
                    "_backend_profile": profile_metadata,
                    "_hint": "Check the configured DashScope model and service status.",
                }
            answer_parts.append(answer_part)
            if response_sources:
                raw_sources = response_sources
            search_seen = search_seen or response_search_seen
        answer = "".join(answer_parts)
    else:
        response = _run_request(
            _invoke_call(
                _call_generation,
                {
                    **request,
                    "messages": [{"role": "user", "content": prompt}],
                    "result_format": "message",
                    "enable_text_image_mixed": bool(images),
                },
            ),
            parent_agent=parent_agent,
            timeout=timeout,
        )
        cancellation_checkpoint(cancel_event)
        answer, raw_sources, search_seen, error = _response_parts(response)
        if error:
            return {
                "success": False,
                "error": f"DashScope search failed: {error}",
                "_backend": "dashscope",
                "_backend_profile": profile_metadata,
                "_hint": "Check the configured DashScope model and service status.",
            }

    cancellation_checkpoint(cancel_event)

    if not search_seen:
        return {
            "success": False,
            "error": "DashScope did not return search_info, so live search could not be verified.",
            "_backend": "dashscope",
            "_backend_profile": profile_metadata,
            "_hint": "Retry once or check the model's web-search support and account quota.",
        }

    if images:
        answer = _convert_html_images_to_markdown(answer)
    answer, truncation_hint = _truncate_answer(answer.strip())
    sources = _normalize_sources(raw_sources, max_sources)
    hints = [truncation_hint] if truncation_hint else []
    if not sources:
        hints.append("DashScope returned no structured source links.")
    if not answer:
        hints.append("DashScope returned sources without a generated answer.")

    return {
        "success": bool(answer or sources),
        "answer": answer,
        "sources": sources,
        "_backend": "dashscope",
        "_backend_profile": {
            **profile_metadata,
            "model": model,
            "search_strategy": actual_strategy,
        },
        "_hint": " ".join(hints),
    }
