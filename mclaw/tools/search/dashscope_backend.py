"""DashScope Search Backend — web search via DashScope Qwen enable_search.

Provides a thin synchronous wrapper around DashScope's compatible-mode OpenAI
chat completions endpoint with enable_search.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "qwen3.5-plus"
_DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
_MAX_WEB_SEARCH_CHARS = 12_000


def _convert_html_images_to_markdown(text: str) -> str:
    """Convert HTML <img> tags to Markdown ![](url) format."""
    text = re.sub(r'<img\s+src="([^"]+)"\s+alt="([^"]*)"[^>]*>', r'![\2](\1)', text)
    text = re.sub(r'<p\s+align="center">\s*', '', text)
    text = re.sub(r'</p>', '', text)
    return text


def search(
    query: str,
    strategy: str,
    freshness: Optional[int],
    sites: Optional[str],
    images: bool,
    creds: dict,
    timeout: float,
) -> dict:
    """Search via DashScope Qwen enable_search.

    Args:
        query: Search query string.
        strategy: "turbo", "max", or "agent".
        freshness: Only results from last N days (7/30/180/365).
        sites: Comma-separated list of domains to restrict to.
        images: Include images in the response.
        creds: Dict with keys "api_key", "base_url", "model".
        timeout: Request timeout in seconds.

    Returns:
        {"success": bool, "results": str, "_backend": "dashscope", "_hint": str}
    """
    api_key = creds.get("api_key", "")
    base_url = creds.get("base_url", "") or _DEFAULT_BASE_URL
    model = creds.get("model", "") or _DEFAULT_MODEL

    # Non-positive timeout means "use the strategy default".
    if timeout <= 0:
        timeout = 120.0 if strategy in ("max", "agent") else 90.0

    if not api_key:
        logger.warning("DashScope search skipped: missing api_key in creds")
        return {
            "success": False,
            "results": (
                "No API key available for web search. "
                "Set DASHSCOPE_API_KEY or QWEN_API_KEY in the M-Claw home .env file."
            ),
            "_backend": "dashscope",
            "_hint": (
                "Set DASHSCOPE_API_KEY or QWEN_API_KEY in the M-Claw home .env file."
            ),
        }

    if not _is_dashscope_configured(creds):
        logger.warning(
            "DashScope search skipped: credentials do not point to DashScope "
            "(api_key_present=%s, base_url=%s)",
            bool(api_key),
            base_url,
        )
        return {
            "success": False,
            "results": (
                "web_search requires a DashScope API key (百炼). "
                "The current API key does not appear to be for DashScope, "
                "so enable_search (联网搜索) cannot work. "
                "Set DASHSCOPE_API_KEY or QWEN_API_KEY in the M-Claw home .env file."
            ),
            "_backend": "dashscope",
            "_hint": (
                "Set DASHSCOPE_API_KEY or QWEN_API_KEY in the M-Claw home .env file."
            ),
        }

    # 强制使用 Qwen 模型：DashScope 的 enable_search 仅对 Qwen 生效。
    if not model.lower().startswith("qwen"):
        logger.warning(
            "DashScope search model '%s' is not a Qwen model; forcing to %s for enable_search",
            model,
            _DEFAULT_MODEL,
        )
        model = _DEFAULT_MODEL

    try:
        import openai
    except ImportError as exc:
        raise RuntimeError("openai package is required for web search") from exc

    client = openai.OpenAI(api_key=api_key, base_url=base_url, max_retries=0, timeout=timeout)

    extra_body: Dict[str, Any] = {}
    search_options: Dict[str, Any] = {
        "search_strategy": strategy,
        "forced_search": True,
        "enable_source": True,
        "enable_citation": True,
        "enable_search_extension": True,
    }

    if freshness and strategy == "turbo":
        search_options["freshness"] = freshness
    if sites and strategy == "turbo":
        search_options["assigned_site_list"] = [s.strip() for s in sites.split(",") if s.strip()]

    extra_body["enable_search"] = True
    extra_body["search_options"] = search_options

    if images:
        extra_body["enable_text_image_mixed"] = True

    messages = [{"role": "user", "content": query}]

    try:
        completion = client.chat.completions.create(
            model=model,
            messages=messages,
            extra_body=extra_body,
        )
        result = completion.choices[0].message.content or ""

        if images:
            result = _convert_html_images_to_markdown(result)

        # 检测搜索是否真实触发。
        no_search_phrases = [
            "无法访问互联网",
            "无法联网",
            "不能访问网络",
            "没有联网",
            "知识库有截止",
            "knowledge cutoff",
            "training data",
            "无法提供实时",
            "无法获取最新",
            "我没有实时",
            "无法提供当前",
            "不能提供实时",
        ]
        if result and len(result) < 300 and any(p in result for p in no_search_phrases):
            logger.warning(
                "DashScope search response suggests search did NOT execute (model=%s, len=%d). "
                "Possible causes: (1) API key is not a DashScope key, "
                "(2) model does not support enable_search, "
                "(3) account lacks search quota. Response: %s",
                model,
                len(result),
                result[:200],
            )
            return {
                "success": False,
                "results": (
                    "Web search backend did not execute the query. "
                    "The model returned a fallback response instead of performing a live search. "
                    "Suggestion: retry the same query once, or check your DashScope search quota."
                ),
                "_backend": "dashscope",
                "_hint": "Retry the same query once, or check your DashScope search quota.",
            }

        logger.info("DashScope search completed (%d chars)", len(result))
        if result:
            preview = result[:2000] if len(result) <= 2000 else result[:2000] + " ... [truncated]"
            logger.info("DashScope search result:\n%s", preview)

        # 内部截断：防止过大的搜索结果撑爆上下文。
        hint = ""
        if len(result) > _MAX_WEB_SEARCH_CHARS:
            head = int(_MAX_WEB_SEARCH_CHARS * 0.4)
            tail = _MAX_WEB_SEARCH_CHARS - head
            omitted = len(result) - _MAX_WEB_SEARCH_CHARS
            result = (
                result[:head]
                + f"\n\n[... {omitted:,} characters truncated ...]\n\n"
                + result[-tail:]
            )
            hint = (
                f"Search result was truncated from {omitted + _MAX_WEB_SEARCH_CHARS:,} "
                f"to {_MAX_WEB_SEARCH_CHARS:,} characters. "
                "Use a more specific query if you need full details."
            )

        return {
            "success": True,
            "results": result,
            "_backend": "dashscope",
            "_hint": hint,
        }

    except openai.AuthenticationError as e:
        logger.error("DashScope search auth error: %s", e)
        return {
            "success": False,
            "results": f"Authentication failed. Check your DASHSCOPE_API_KEY. Error: {e}",
            "_backend": "dashscope",
            "_hint": "Check your DASHSCOPE_API_KEY.",
        }
    except openai.APIError as e:
        logger.error("DashScope search API error: %s", e)
        return {
            "success": False,
            "results": f"Search API error: {e}",
            "_backend": "dashscope",
            "_hint": "Check DashScope service status and your account quota.",
        }
    except Exception as e:
        logger.exception("DashScope search unexpected error: %s", e)
        return {
            "success": False,
            "results": f"Search failed: {e}",
            "_backend": "dashscope",
            "_hint": "Unexpected error during search.",
        }


def _is_dashscope_configured(creds: dict) -> bool:
    """Return True if the credentials point to DashScope (required for enable_search)."""
    api_key = creds.get("api_key", "")
    base_url = creds.get("base_url", "")
    if not api_key:
        return False
    # DashScope key 前缀或 base_url 域名识别。
    if api_key.startswith("sk-dashscope") or "dashscope" in base_url.lower():
        return True
    return False


def check_requirements(creds: dict) -> bool:
    """Return True if DashScope is available (required for web_search)."""
    return _is_dashscope_configured(creds)
