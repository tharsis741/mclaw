# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Extract readable content from known public web pages."""

from __future__ import annotations

import json
import math
import re
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

from mclaw.cli.config import ConfigError, get_env_value, load_config
from mclaw.tools.extract.profiles import (
    DEFAULT_FIRECRAWL_API_URL,
    VALID_EXTRACT_BACKENDS,
    get_extract_backend_profile,
)
from mclaw.tools.registry import registry, tool_error
from mclaw.tools.vision.image_io import _is_safe_url

_EXTRACT_SCOPE = "tool:web_extract"
_TAVILY_EXTRACT_URL = "https://api.tavily.com/extract"
_MAX_URLS = 5
_MAX_DOWNLOAD_BYTES = 5 * 1024 * 1024
_MAX_CONTENT_CHARS = 20_000
_CONTINUATION_TTL_SECONDS = 15 * 60
_MAX_CONTINUATION_ENTRIES = 16
_MAX_CONTINUATION_CACHE_CHARS = 5_000_000
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


@dataclass
class _ContinuationEntry:
    url: str
    result: dict[str, Any]
    backend: str
    backend_profile: dict[str, Any]
    expires_at: float


_continuation_cache: OrderedDict[str, _ContinuationEntry] = OrderedDict()
_continuation_lock = threading.Lock()


def _config(parent_agent=None, config: dict | None = None) -> dict[str, Any]:
    if isinstance(config, dict):
        cfg = config
    elif parent_agent is not None and isinstance(getattr(parent_agent, "config", None), dict):
        cfg = parent_agent.config
    else:
        cfg = load_config(strict=True)

    raw = cfg.get("auxiliary", {}).get("web_extract", {}) if isinstance(cfg, dict) else {}
    if not isinstance(raw, dict):
        raw = {}

    backend = str(raw.get("backend") or "trafilatura").strip().lower()
    if backend not in VALID_EXTRACT_BACKENDS:
        raise ConfigError(
            f"Unknown auxiliary.web_extract.backend {backend!r}; "
            f"use {', '.join(VALID_EXTRACT_BACKENDS)}."
        )
    try:
        timeout = float(raw.get("timeout", 30))
    except (TypeError, ValueError) as exc:
        raise ConfigError("auxiliary.web_extract.timeout must be a number") from exc
    if not math.isfinite(timeout):
        raise ConfigError("auxiliary.web_extract.timeout must be finite")

    firecrawl_api_url = str(
        raw.get("firecrawl_api_url") or DEFAULT_FIRECRAWL_API_URL
    ).rstrip("/")
    if backend == "firecrawl":
        parsed_api_url = urlparse(firecrawl_api_url)
        if (
            parsed_api_url.scheme not in {"http", "https"}
            or not parsed_api_url.hostname
            or parsed_api_url.username
            or parsed_api_url.password
        ):
            raise ConfigError(
                "auxiliary.web_extract.firecrawl_api_url must be an http/https URL "
                "without embedded credentials"
            )

    return {
        "backend": backend,
        "timeout": max(1.0, min(timeout, 60.0)),
        "firecrawl_api_url": firecrawl_api_url,
    }


def _authorized_env_value(name: str) -> str:
    try:
        from mclaw.runtime.features import authorized_env_value

        return authorized_env_value(_EXTRACT_SCOPE, name, get_env_value, "")
    except Exception:
        return ""


def diagnose_web_extract_requirements(config: dict | None = None) -> dict[str, Any]:
    """Return provider diagnostics without exposing credential values."""
    try:
        cfg = _config(config=config)
    except ConfigError as exc:
        return {
            "available": False,
            "reason": str(exc),
            "fix": "Check auxiliary.web_extract config.",
        }

    backend = cfg["backend"]
    if backend == "trafilatura":
        try:
            import trafilatura  # noqa: F401
        except ImportError:
            return {
                "available": False,
                "reason": "Trafilatura is not installed",
                "fix": "Install the project dependencies (trafilatura>=2.1,<3).",
            }
        return {
            "available": True,
            "reason": "local Trafilatura extraction is available",
            "fix": "",
        }

    if backend == "tavily":
        if _authorized_env_value("TAVILY_API_KEY"):
            return {"available": True, "reason": "Tavily key configured", "fix": ""}
        return {
            "available": False,
            "reason": "web_extract backend=tavily but TAVILY_API_KEY is missing or not authorized",
            "fix": "Authorize TAVILY_API_KEY for required_for='tool:web_extract'.",
        }

    if _authorized_env_value("FIRECRAWL_API_KEY"):
        return {
            "available": True,
            "reason": "Firecrawl key configured",
            "fix": "",
        }
    return {
        "available": False,
        "reason": "cloud Firecrawl requires FIRECRAWL_API_KEY authorized for web_extract",
        "fix": "Authorize FIRECRAWL_API_KEY for required_for='tool:web_extract'.",
    }


def check_web_extract_requirements(config: dict | None = None) -> bool:
    return bool(diagnose_web_extract_requirements(config=config).get("available"))


def _url_error(url: str) -> str:
    if not isinstance(url, str) or not url.strip():
        return "URL must be a non-empty string."
    parsed = urlparse(url.strip())
    if parsed.username or parsed.password:
        return "URLs containing embedded credentials are not allowed."
    if not _is_safe_url(url.strip()):
        return "Only public http/https URLs are allowed; private or internal addresses are blocked."
    return ""


def _safe_error(value: object) -> str:
    text = str(value or "request failed")
    text = re.sub(
        r"(?i)\b(api[_-]?key|token|secret|password)\s*[:=]\s*['\"]?[^'\"\s,;]+",
        r"\1=<redacted>",
        text,
    )
    text = re.sub(r"\b(?:fc|tvly|sk)-[A-Za-z0-9_-]{8,}\b", "<redacted>", text)
    return text[:500] + ("...[truncated]" if len(text) > 500 else "")


def _compact_mapping(values: dict[str, Any] | None) -> dict[str, Any]:
    """Drop absent provider metadata while preserving useful false/zero values."""
    if not isinstance(values, dict):
        return {}
    return {
        str(key): value
        for key, value in values.items()
        if value is not None and value != "" and value != [] and value != {}
    }


def _first_provider_value(
    containers: tuple[dict[str, Any], ...],
    *keys: str,
) -> Any:
    """Return the first present provider field across response nesting levels."""
    for container in containers:
        for key in keys:
            value = container.get(key)
            if value is not None and value != "":
                return value
    return None


def _normalize_warnings(values: object) -> list[str]:
    """Normalize provider and local quality warnings without leaking secrets."""
    if values is None:
        return []
    candidates = values if isinstance(values, (list, tuple, set)) else [values]
    warnings: list[str] = []
    for candidate in candidates:
        if candidate is None or (isinstance(candidate, str) and not candidate.strip()):
            continue
        warning = _safe_error(candidate).strip()
        if warning and warning not in warnings:
            warnings.append(warning)
    return warnings


def _error_result(
    url: str,
    message: object,
    *,
    warnings: object = None,
    diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "url": url,
        "title": "",
        "content": "",
        "error": _safe_error(message),
        "truncated": False,
        "status": "error",
        "warnings": _normalize_warnings(warnings),
        "_diagnostics": _compact_mapping(diagnostics),
        "original_chars": 0,
        "content_start": 0,
        "content_end": 0,
        "has_more": False,
        "next_cursor": None,
    }


def _title_from_markdown(content: str) -> str:
    match = re.search(r"(?m)^#\s+(.+?)\s*$", content)
    return match.group(1).strip()[:300] if match else ""


def _content_quality_warnings(content: str) -> list[str]:
    """Flag deterministic incompleteness signals without guessing article facts.

    A non-empty string is not proof of a successful extraction. In particular,
    provider output can contain only navigation links and a page shell. These
    checks are intentionally non-definitive: they add warnings but never claim
    that usable content is incomplete.
    """
    warnings: list[str] = []
    if len(content) < 200:
        warnings.append(
            "Extracted content is unusually short; verify that the main body is present."
        )

    markdown_links = list(re.finditer(r"\[[^\]\n]+\]\([^\)\n]+\)", content))
    link_chars = sum(len(match.group(0)) for match in markdown_links)
    link_ratio = link_chars / max(len(content), 1)
    prose_view = re.sub(
        r"\[([^\]\n]+)\]\([^\)\n]+\)",
        lambda match: match.group(1),
        content,
    )
    prose_view = re.sub(r"https?://\S+", "", prose_view)
    sentence_marks = len(re.findall(r"[。！？!?]|\.(?=\s|$)", prose_view))
    prose_lines = [
        line
        for raw_line in prose_view.splitlines()
        if len((line := raw_line.strip())) >= 80
        and not line.startswith(("#", "- ", "* ", ">", "|"))
    ]
    navigation_heavy = (
        link_ratio >= 0.75 and not prose_lines
    ) or (
        link_ratio >= 0.45 and sentence_marks < 3 and len(prose_lines) < 2
    )
    if len(content) >= 300 and navigation_heavy:
        warnings.append(
            "Content appears navigation-heavy and may be missing the main article body."
        )
    return warnings


def _content_result(
    url: str,
    content: object,
    *,
    title: object = "",
    final_url: object = "",
    warnings: object = None,
    diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    text = str(content or "").strip()
    if not text:
        return _error_result(
            url,
            "No readable main content was extracted.",
            warnings=warnings,
            diagnostics=diagnostics,
        )

    original_chars = len(text)
    result_warnings = _normalize_warnings(warnings)
    for warning in _content_quality_warnings(text):
        if warning not in result_warnings:
            result_warnings.append(warning)

    result: dict[str, Any] = {
        "url": url,
        "title": str(title or _title_from_markdown(text)).strip()[:300],
        "content": text,
        "error": None,
        "truncated": False,
        "status": "ok",
        "warnings": result_warnings,
        "_diagnostics": _compact_mapping(diagnostics),
        "original_chars": original_chars,
        "content_start": 0,
        "content_end": original_chars,
        "has_more": False,
        "next_cursor": None,
    }
    if final_url and str(final_url) != url:
        result["final_url"] = str(final_url)
    return result


def _prune_continuation_cache(now: float) -> None:
    expired = [
        token
        for token, entry in _continuation_cache.items()
        if entry.expires_at <= now
    ]
    for token in expired:
        _continuation_cache.pop(token, None)


def _store_continuation(
    url: str,
    result: dict[str, Any],
    backend: str,
    backend_profile: dict[str, Any],
) -> str:
    """Cache one full result and return an opaque process-local cache token."""
    now = time.monotonic()
    content_chars = len(str(result.get("content") or ""))
    with _continuation_lock:
        _prune_continuation_cache(now)
        cached_chars = sum(
            len(str(entry.result.get("content") or ""))
            for entry in _continuation_cache.values()
        )
        # ponytail: process-local bounded cache; use shared storage only if
        # continuation must survive restarts or span multiple M-Claw workers.
        while _continuation_cache and (
            len(_continuation_cache) >= _MAX_CONTINUATION_ENTRIES
            or cached_chars + content_chars > _MAX_CONTINUATION_CACHE_CHARS
        ):
            _, removed = _continuation_cache.popitem(last=False)
            cached_chars -= len(str(removed.result.get("content") or ""))

        token = secrets.token_urlsafe(24)
        _continuation_cache[token] = _ContinuationEntry(
            url=url,
            result=result,
            backend=backend,
            backend_profile=dict(backend_profile),
            expires_at=now + _CONTINUATION_TTL_SECONDS,
        )
        return token


def _chunk_result(
    result: dict[str, Any],
    *,
    start: int = 0,
    cache_token: str = "",
) -> dict[str, Any]:
    """Return one deterministic content window and its next cursor."""
    content = str(result.get("content") or "")
    end = min(start + _MAX_CONTENT_CHARS, len(content))
    has_more = end < len(content)
    if has_more and not cache_token:
        raise ValueError("cache token is required when content has another chunk")
    chunk = dict(result)
    chunk.update({
        "content": content[start:end],
        "truncated": has_more,
        "status": "partial" if has_more else "ok",
        "original_chars": len(content),
        "content_start": start,
        "content_end": end,
        "has_more": has_more,
        "next_cursor": f"{cache_token}:{end}" if has_more else None,
    })
    return chunk


def _load_continuation(
    url: str,
    cursor: str,
) -> tuple[_ContinuationEntry, str, int]:
    """Validate a cursor and return its cached full result plus chunk offset."""
    if not isinstance(cursor, str) or not cursor.strip() or len(cursor) > 256:
        raise ValueError("cursor must be a non-empty continuation token")
    try:
        token, raw_offset = cursor.rsplit(":", 1)
        offset = int(raw_offset)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid continuation cursor") from exc
    if not token or offset <= 0 or offset % _MAX_CONTENT_CHARS:
        raise ValueError("invalid continuation cursor")

    now = time.monotonic()
    with _continuation_lock:
        _prune_continuation_cache(now)
        entry = _continuation_cache.get(token)
        if entry is None:
            raise ValueError(
                "continuation cursor expired or is unknown; extract the URL again without cursor"
            )
        if entry.url != url:
            raise ValueError("continuation cursor does not belong to this URL")
        content_length = len(str(entry.result.get("content") or ""))
        if offset >= content_length:
            raise ValueError("continuation cursor points beyond the cached content")
        entry.expires_at = now + _CONTINUATION_TTL_SECONDS
        _continuation_cache.move_to_end(token)
        return entry, token, offset


def _fetch_public_page(url: str, timeout: float) -> tuple[bytes, str]:
    """Download one page while validating every redirect before following it."""
    import httpx

    current = url
    headers = {
        "User-Agent": "M-Claw-WebExtract/1.0",
        "Accept": "text/html,application/xhtml+xml,text/plain;q=0.8,*/*;q=0.1",
    }
    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        for _ in range(6):
            error = _url_error(current)
            if error:
                raise ValueError(error)

            with client.stream("GET", current, headers=headers) as response:
                if response.status_code in _REDIRECT_STATUSES:
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("Redirect response did not include a Location header.")
                    current = urljoin(current, location)
                    continue

                response.raise_for_status()
                content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                if content_type and content_type not in {
                    "text/html",
                    "application/xhtml+xml",
                    "text/plain",
                }:
                    raise ValueError(
                        f"Unsupported content type {content_type!r}; local extraction accepts HTML or text pages."
                    )

                content_length = response.headers.get("content-length")
                if content_length:
                    try:
                        declared_size = int(content_length)
                    except ValueError:
                        declared_size = 0
                    if declared_size > _MAX_DOWNLOAD_BYTES:
                        raise ValueError("Page exceeds the 5 MB download limit.")

                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > _MAX_DOWNLOAD_BYTES:
                        raise ValueError("Page exceeds the 5 MB download limit.")
                    chunks.append(chunk)
                return b"".join(chunks), str(response.url)

    raise ValueError("Too many redirects (maximum 5).")


def _extract_with_trafilatura(urls: list[str], timeout: float) -> list[dict[str, Any]]:
    try:
        import trafilatura
    except ImportError:
        return [_error_result(url, "Trafilatura is not installed.") for url in urls]

    profile = get_extract_backend_profile("trafilatura")
    results: list[dict[str, Any]] = []
    for url in urls:
        started = time.perf_counter()
        try:
            html, final_url = _fetch_public_page(url, timeout)
            content = trafilatura.extract(
                html,
                url=final_url,
                **profile.request_options(),
            )
            metadata_warning = ""
            try:
                metadata = trafilatura.extract_metadata(html, default_url=final_url)
            except Exception as exc:
                metadata = None
                metadata_warning = f"Page metadata extraction failed: {_safe_error(exc)}"
            title = getattr(metadata, "title", "") if metadata else ""
            results.append(
                _content_result(
                    url,
                    content,
                    title=title,
                    final_url=final_url,
                    warnings=metadata_warning,
                    diagnostics={
                        "backend": "trafilatura",
                        "freshness": "direct_fetch",
                        "renderer": "none",
                        "download_bytes": len(html),
                        "elapsed_ms": round((time.perf_counter() - started) * 1000),
                        "final_url": final_url,
                    },
                )
            )
        except Exception as exc:
            results.append(
                _error_result(
                    url,
                    exc,
                    diagnostics={
                        "backend": "trafilatura",
                        "freshness": "direct_fetch",
                        "renderer": "none",
                        "elapsed_ms": round((time.perf_counter() - started) * 1000),
                    },
                )
            )
    return results


def _request_error_results(
    urls: list[str],
    provider: str,
    exc: object,
    *,
    diagnostics: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    message = f"{provider} extraction failed: {_safe_error(exc)}"
    return [_error_result(url, message, diagnostics=diagnostics) for url in urls]


def _http_failure_context(
    provider: str,
    started: float,
    exc: requests.RequestException,
) -> tuple[object, dict[str, Any]]:
    """Preserve safe HTTP/provider failure details for actionable diagnostics."""
    response = getattr(exc, "response", None)
    body: dict[str, Any] = {}
    if response is not None:
        try:
            candidate = response.json()
            if isinstance(candidate, dict):
                body = candidate
        except (TypeError, ValueError):
            pass

    detail: object = body.get("error") or body.get("detail") or exc
    if isinstance(detail, dict):
        detail = detail.get("error") or detail.get("message") or detail
    headers = getattr(response, "headers", {}) if response is not None else {}
    if not hasattr(headers, "get"):
        headers = {}
    diagnostics = _compact_mapping({
        "backend": provider.lower(),
        "status_code": getattr(response, "status_code", None),
        "request_id": (
            body.get("request_id")
            or headers.get("x-request-id")
            or headers.get("x-firecrawl-request-id")
        ),
        "elapsed_ms": round((time.perf_counter() - started) * 1000),
    })
    return detail, diagnostics


def _extract_with_tavily(urls: list[str], timeout: float) -> list[dict[str, Any]]:
    profile = get_extract_backend_profile("tavily")
    api_key = _authorized_env_value("TAVILY_API_KEY")
    if not api_key:
        return _request_error_results(
            urls,
            "Tavily",
            "TAVILY_API_KEY is missing or not authorized for tool:web_extract.",
            diagnostics={"backend": "tavily"},
        )

    started = time.perf_counter()
    try:
        request_payload = profile.request_options()
        request_payload.update({"urls": urls, "timeout": timeout})
        response = requests.post(
            _TAVILY_EXTRACT_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=request_payload,
            timeout=timeout + 5,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("Tavily returned a non-object response.")
    except requests.RequestException as exc:
        detail, diagnostics = _http_failure_context("Tavily", started, exc)
        return _request_error_results(
            urls,
            "Tavily",
            detail,
            diagnostics=diagnostics,
        )
    except ValueError as exc:
        return _request_error_results(
            urls,
            "Tavily",
            exc,
            diagnostics={
                "backend": "tavily",
                "elapsed_ms": round((time.perf_counter() - started) * 1000),
            },
        )

    shared_diagnostics = _compact_mapping({
        "backend": "tavily",
        "extract_depth": "advanced",
        "response_time": data.get("response_time"),
        "usage": data.get("usage"),
        "request_id": data.get("request_id"),
        "elapsed_ms": round((time.perf_counter() - started) * 1000),
    })

    extracted: dict[str, dict[str, Any]] = {}
    for item in data.get("results") or []:
        if not isinstance(item, dict):
            continue
        item_url = str(item.get("url") or "")
        extracted[item_url.rstrip("/")] = _content_result(
            item_url,
            item.get("raw_content"),
            title=item.get("title"),
            warnings=item.get("warning") or item.get("warnings"),
            diagnostics=shared_diagnostics,
        )

    failed: dict[str, object] = {}
    for item in data.get("failed_results") or []:
        if not isinstance(item, dict):
            continue
        item_url = str(item.get("url") or "")
        failed[item_url.rstrip("/")] = item.get("error") or "Tavily could not extract this URL."

    results: list[dict[str, Any]] = []
    for url in urls:
        key = url.rstrip("/")
        if key in extracted:
            result = dict(extracted[key])
            result["url"] = url
            results.append(result)
        elif key in failed:
            results.append(_error_result(url, failed[key], diagnostics=shared_diagnostics))
        else:
            results.append(
                _error_result(
                    url,
                    "Tavily returned no result for this URL.",
                    diagnostics=shared_diagnostics,
                )
            )
    return results


def _extract_with_firecrawl(
    urls: list[str],
    timeout: float,
    api_url: str,
) -> list[dict[str, Any]]:
    profile = get_extract_backend_profile("firecrawl")
    api_key = _authorized_env_value("FIRECRAWL_API_KEY")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    results: list[dict[str, Any]] = []
    for url in urls:
        started = time.perf_counter()
        try:
            request_payload = profile.request_options()
            request_payload.update({
                "url": url,
                "timeout": int(timeout * 1000),
            })
            response = requests.post(
                api_url,
                headers=headers,
                json=request_payload,
                timeout=timeout + 5,
            )
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("Firecrawl returned a non-object response.")
            if data.get("success") is False:
                results.append(
                    _error_result(
                        url,
                        "Firecrawl extraction failed: "
                        f"{_safe_error(data.get('error') or 'Firecrawl returned success=false.')}",
                        diagnostics={
                            "backend": "firecrawl",
                            "status_code": getattr(response, "status_code", None),
                            "request_id": data.get("request_id"),
                            "scrape_id": data.get("scrapeId"),
                            "elapsed_ms": round((time.perf_counter() - started) * 1000),
                        },
                    )
                )
                continue
            document = data.get("data") if isinstance(data.get("data"), dict) else data
            metadata = document.get("metadata") if isinstance(document.get("metadata"), dict) else {}
            final_url = metadata.get("sourceURL") or metadata.get("url") or ""
            if final_url and _url_error(str(final_url)):
                raise ValueError("Firecrawl reported a private or unsafe final URL.")

            containers = (metadata, document, data)
            cache_state = _first_provider_value(containers, "cacheState", "cache_state")
            diagnostics = _compact_mapping({
                "backend": "firecrawl",
                "freshness": "forced_fresh",
                "cache_state": cache_state,
                "cached_at": _first_provider_value(containers, "cachedAt", "cached_at"),
                "scrape_id": _first_provider_value(containers, "scrapeId", "scrape_id"),
                "renderer": _first_provider_value(containers, "renderer"),
                "proxy_used": _first_provider_value(containers, "proxyUsed", "proxy_used"),
                "credits_used": _first_provider_value(containers, "creditsUsed", "credits_used"),
                "status_code": _first_provider_value(containers, "statusCode", "status_code")
                    or getattr(response, "status_code", None),
                "content_type": _first_provider_value(containers, "contentType", "content_type"),
                "concurrency_limited": _first_provider_value(
                    containers,
                    "concurrencyLimited",
                    "concurrency_limited",
                ),
                "elapsed_ms": round((time.perf_counter() - started) * 1000),
            })
            provider_warnings: list[object] = [
                value
                for value in (
                    data.get("warning"),
                    document.get("warning"),
                    metadata.get("warning"),
                )
                if value
            ]
            if str(cache_state or "").strip().lower() == "hit":
                provider_warnings.append(
                    "Firecrawl reported a cache hit although this profile requested maxAge=0; "
                    "verify the main body before relying on the result."
                )
            results.append(
                _content_result(
                    url,
                    document.get("markdown"),
                    title=metadata.get("title"),
                    final_url=final_url,
                    warnings=provider_warnings,
                    diagnostics=diagnostics,
                )
            )
        except requests.RequestException as exc:
            detail, diagnostics = _http_failure_context("Firecrawl", started, exc)
            results.append(
                _error_result(
                    url,
                    f"Firecrawl extraction failed: {_safe_error(detail)}",
                    diagnostics=diagnostics,
                )
            )
        except (ValueError, TypeError) as exc:
            results.append(
                _error_result(
                    url,
                    f"Firecrawl extraction failed: {_safe_error(exc)}",
                    diagnostics={
                        "backend": "firecrawl",
                        "freshness": "forced_fresh",
                        "elapsed_ms": round((time.perf_counter() - started) * 1000),
                    },
                )
            )
    return results


def _extract_response(
    results: list[dict[str, Any]],
    *,
    backend: str,
    backend_profile: dict[str, Any],
) -> str:
    """Serialize initial and continuation calls through one response contract."""
    status_counts = {
        status: sum(item.get("status") == status for item in results)
        for status in ("ok", "partial", "error")
    }
    warned_results = sum(bool(item.get("warnings")) for item in results)
    success = status_counts["ok"] + status_counts["partial"] > 0
    hint_parts: list[str] = []
    if status_counts["error"]:
        hint_parts.append(
            f"{status_counts['error']} of {len(results)} URL(s) could not be extracted."
        )
    if status_counts["partial"]:
        hint_parts.append(
            f"{status_counts['partial']} URL(s) have more cached content. Call web_extract "
            "again with that result's single URL and next_cursor until has_more is false; "
            "the cursor expires after 15 minutes."
        )
    if warned_results:
        hint_parts.append(
            f"{warned_results} URL(s) include non-definitive quality warnings; inspect them "
            "without assuming the content is incomplete."
        )
    if status_counts["error"] or warned_results:
        hint_parts.append(
            "Quality warnings are advisory; inspect the returned content before treating it as "
            "incomplete."
        )

    return json.dumps(
        {
            "success": success,
            "results": results,
            "_backend": backend,
            "_backend_profile": backend_profile,
            "_summary": status_counts,
            "_hint": " ".join(hint_parts),
        },
        ensure_ascii=False,
    )


def web_extract(
    urls: list[str],
    parent_agent=None,
    *,
    cursor: str | None = None,
) -> str:
    """Extract or continue main content from known public URLs."""
    if not isinstance(urls, list):
        return tool_error("urls must be a list of 1 to 5 URLs", success=False)
    if not 1 <= len(urls) <= _MAX_URLS:
        return tool_error("urls must contain between 1 and 5 URLs", success=False)
    if any(not isinstance(url, str) or not url.strip() for url in urls):
        return tool_error("each URL must be a non-empty string", success=False)

    normalized = [url.strip() for url in urls]
    if cursor is not None:
        if len(normalized) != 1:
            return tool_error(
                "continuation calls require exactly one URL",
                success=False,
            )
        try:
            entry, cache_token, offset = _load_continuation(normalized[0], cursor)
            result = _chunk_result(
                entry.result,
                start=offset,
                cache_token=cache_token,
            )
        except ValueError as exc:
            return tool_error(str(exc), success=False)
        return _extract_response(
            [result],
            backend=entry.backend,
            backend_profile=entry.backend_profile,
        )

    try:
        cfg = _config(parent_agent=parent_agent)
    except ConfigError as exc:
        return tool_error(f"Configuration error: {exc}", success=False)

    safe_urls: list[str] = []
    validation: list[str] = []
    for url in normalized:
        error = _url_error(url)
        validation.append(error)
        if not error:
            safe_urls.append(url)

    backend = cfg["backend"]
    profile = get_extract_backend_profile(backend)
    if not safe_urls:
        provider_results = []
    elif backend == "trafilatura":
        provider_results = _extract_with_trafilatura(safe_urls, cfg["timeout"])
    elif backend == "tavily":
        provider_results = _extract_with_tavily(safe_urls, cfg["timeout"])
    else:
        provider_results = _extract_with_firecrawl(
            safe_urls,
            cfg["timeout"],
            cfg["firecrawl_api_url"],
        )

    provider_iter = iter(provider_results)
    results: list[dict[str, Any]] = []
    for url, error in zip(normalized, validation, strict=True):
        if error:
            results.append(
                _error_result(url, error, diagnostics={"backend": "url_validation"})
            )
            continue
        try:
            results.append(next(provider_iter))
        except StopIteration:
            results.append(
                _error_result(
                    url,
                    f"{backend} returned no result for this URL.",
                    diagnostics={"backend": backend},
                )
            )

    backend_profile = profile.tool_metadata()
    chunks: list[dict[str, Any]] = []
    for result in results:
        if result.get("status") == "error":
            chunks.append(result)
            continue
        content = str(result.get("content") or "")
        cache_token = ""
        if len(content) > _MAX_CONTENT_CHARS:
            cache_token = _store_continuation(
                str(result.get("url") or ""),
                result,
                backend,
                backend_profile,
            )
        chunks.append(_chunk_result(result, cache_token=cache_token))

    return _extract_response(
        chunks,
        backend=backend,
        backend_profile=backend_profile,
    )


WEB_EXTRACT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "web_extract",
        "description": (
            "Extract content from 1–5 known public URLs using the configured extraction backend"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "urls": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 5,
                    "items": {"type": "string", "format": "uri"},
                    "description": "Public HTTP(S) page URLs.",
                },
                "cursor": {
                    "type": "string",
                    "maxLength": 256,
                    "description": (
                        "next_cursor from a partial result; urls must contain only that result's URL."
                    ),
                },
            },
            "required": ["urls"],
        },
    },
}


def _handle_web_extract(args: dict, **kw) -> str:
    return web_extract(
        args.get("urls"),
        cursor=args.get("cursor"),
        parent_agent=kw.get("parent_agent"),
    )


registry.register(
    name="web_extract",
    toolset="web",
    schema=WEB_EXTRACT_SCHEMA,
    handler=_handle_web_extract,
    check_fn=check_web_extract_requirements,
    diagnose_fn=diagnose_web_extract_requirements,
    description="Extract content from known public URLs",
    emoji="📄",
    max_result_size_chars=110_000,
)
