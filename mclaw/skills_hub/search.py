# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""ClawHub-backed search layer for external Skills."""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import List

import httpx

from mclaw.skills_hub.models import ExternalSkill
from mclaw.tools.cancellation import cancellation_checkpoint

logger = logging.getLogger(__name__)

CLAW_HUB_API_BASE = "https://clawhub.ai/api/v1"
CLAW_HUB_DETAIL_STATS_LIMIT = 3


async def _get_json_async(
    url: str,
    *,
    params: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> dict:
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with asyncio.timeout(max(0.001, timeout)):
            response = await client.get(url, params=params)
            response.raise_for_status()
            data = response.json()
    if not isinstance(data, dict):
        raise ValueError("ClawHub returned a non-object response.")
    return data


def _run_request(coro, *, parent_agent, timeout: float = 10.0):
    from mclaw.tools.dispatch import _run_async

    return _run_async(
        coro,
        parent_agent=parent_agent,
        diagnostic_name="clawhub_http",
        timeout_seconds=timeout,
        raise_on_stop=True,
    )


class ClawHubSearchError(RuntimeError):
    """Raised when a required ClawHub lookup fails."""


def _as_int(value, default: int = 0) -> int:
    """Coerce loosely typed API count fields into stable integers."""

    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return default


def _extract_stats(item: dict) -> tuple[int, int]:
    """Normalize download and star counts across ClawHub response shapes."""

    stats = item.get("stats") if isinstance(item, dict) else {}
    if not isinstance(stats, dict):
        stats = {}
    downloads = (
        stats.get("downloads")
        or item.get("downloads")
        or item.get("downloadCount")
        or item.get("download_count")
    )
    stars = (
        stats.get("stars")
        or item.get("stars")
        or item.get("starCount")
        or item.get("star_count")
    )
    return _as_int(downloads), _as_int(stars)


class ClawHubSearcher:
    """Small adapter around ClawHub search and detail endpoints."""

    def get_skill_detail(
        self,
        slug: str,
        *,
        raise_on_error: bool = False,
        cancel_event: threading.Event | None = None,
        parent_agent=None,
    ) -> dict:
        """Fetch detail metadata for one slug, optionally surfacing network failures."""

        cancellation_checkpoint(cancel_event)
        if not slug:
            return {}
        try:
            data = _run_request(
                _get_json_async(
                f"{CLAW_HUB_API_BASE}/skills/{slug}",
                timeout=10.0,
                ),
                parent_agent=parent_agent,
            )
            cancellation_checkpoint(cancel_event)
            return data
        except (httpx.HTTPError, TimeoutError, ValueError) as exc:
            cancellation_checkpoint(cancel_event)
            logger.debug("ClawHub detail fetch failed slug=%s", slug, exc_info=True)
            if raise_on_error:
                raise ClawHubSearchError(f"ClawHub detail fetch failed for slug '{slug}'.") from exc
        return {}

    def search(
        self,
        query: str,
        limit: int = 10,
        *,
        cancel_event: threading.Event | None = None,
        parent_agent=None,
    ) -> List[ExternalSkill]:
        """Search ClawHub and return provider-neutral external Skill records."""

        cancellation_checkpoint(cancel_event)
        try:
            data = _run_request(
                _get_json_async(
                    f"{CLAW_HUB_API_BASE}/search",
                    params={"q": query},
                    timeout=10.0,
                ),
                parent_agent=parent_agent,
            )
            cancellation_checkpoint(cancel_event)
        except (httpx.HTTPError, TimeoutError, ValueError):
            cancellation_checkpoint(cancel_event)
            logger.debug("ClawHub search failed", exc_info=True)
            return []
        if not isinstance(data, dict):
            return []
        items = data.get("results", [])
        if not isinstance(items, list):
            return []
        results: List[ExternalSkill] = []
        for index, item in enumerate(items[:limit]):
            cancellation_checkpoint(cancel_event)
            if not isinstance(item, dict):
                continue
            slug = str(item.get("slug") or "").strip()
            owner_item = item.get("owner")
            owner_payload = owner_item if isinstance(owner_item, dict) else {}
            owner_handle = str(
                item.get("ownerHandle")
                or owner_payload.get("handle")
                or ""
            ).strip()
            downloads, stars = _extract_stats(item)
            if slug and not (downloads or stars) and index < CLAW_HUB_DETAIL_STATS_LIMIT:
                # Search results can omit counters, so enrich only a small prefix with
                # detail calls to keep interactive search latency bounded.
                detail = self.get_skill_detail(
                    slug,
                    cancel_event=cancel_event,
                    parent_agent=parent_agent,
                )
                skill_detail = detail.get("skill") if isinstance(detail, dict) else {}
                if isinstance(skill_detail, dict):
                    detail_downloads, detail_stars = _extract_stats(skill_detail)
                    downloads = detail_downloads or downloads
                    stars = detail_stars or stars
                owner_detail = detail.get("owner") if isinstance(detail, dict) else {}
                if not owner_handle and isinstance(owner_detail, dict):
                    owner_handle = str(owner_detail.get("handle") or "").strip()
            url = f"https://clawhub.ai/{owner_handle}/{slug}" if owner_handle and slug else ""
            results.append(
                ExternalSkill(
                    source="clawhub",
                    name=item.get("displayName", "") or item.get("name", ""),
                    description=item.get("summary", "") or item.get("description", ""),
                    slug=slug,
                    url=url,
                    author=owner_handle,
                    downloads=downloads,
                    stars=stars,
                    platforms=None,
                    frontmatter={},
                )
            )
        cancellation_checkpoint(cancel_event)
        return results


def search_all(
    query: str,
    limit: int = 10,
    *,
    cancel_event: threading.Event | None = None,
    parent_agent=None,
) -> List[ExternalSkill]:
    """Convenience entry point used by tools and slash commands."""

    return ClawHubSearcher().search(
        query,
        limit=limit,
        cancel_event=cancel_event,
        parent_agent=parent_agent,
    )
