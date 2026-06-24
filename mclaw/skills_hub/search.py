# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Search layer for external Skill discovery.

The module wraps remote Skill search responses into internal result objects so
the CLI and agent can present consistent Skill metadata.
"""
from __future__ import annotations

import logging
from typing import List

import httpx

from mclaw.skills_hub.models import ExternalSkill

logger = logging.getLogger(__name__)

CLAW_HUB_API_BASE = "https://clawhub.ai/api/v1"
CLAW_HUB_DETAIL_STATS_LIMIT = 3


def _as_int(value, default: int = 0) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return default


def _extract_stats(item: dict) -> tuple[int, int]:
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
    def get_skill_detail(self, slug: str, *, raise_on_error: bool = False) -> dict:
        if not slug:
            return {}
        try:
            response = httpx.get(
                f"{CLAW_HUB_API_BASE}/skills/{slug}",
                timeout=10.0,
            )
            response.raise_for_status()
            data = response.json()
            if isinstance(data, dict):
                return data
        except Exception:
            logger.debug("ClawHub detail fetch failed slug=%s", slug, exc_info=True)
            if raise_on_error:
                raise
        return {}

    def search(self, query: str, limit: int = 10) -> List[ExternalSkill]:
        try:
            response = httpx.get(
                f"{CLAW_HUB_API_BASE}/search",
                params={"q": query},
                timeout=10.0,
            )
            response.raise_for_status()
            data = response.json()
            items = data.get("results", [])
            results: List[ExternalSkill] = []
            for index, item in enumerate(items[:limit]):
                slug = item.get("slug", "")
                owner_item = item.get("owner")
                owner_payload = owner_item if isinstance(owner_item, dict) else {}
                owner_handle = str(
                    item.get("ownerHandle")
                    or owner_payload.get("handle")
                    or ""
                ).strip()
                downloads, stars = _extract_stats(item)
                if slug and not (downloads or stars) and index < CLAW_HUB_DETAIL_STATS_LIMIT:
                    detail = self.get_skill_detail(slug)
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
            return results
        except Exception:
            logger.debug("ClawHub search failed", exc_info=True)
            return []


class WebSearcher:
    """Placeholder — previous endpoint (sorane.search.skywork.ai) no longer exists.

    Web search for skills is disabled until a reliable structured source
    (e.g. Tavily with domain filter, GitHub topics API) is integrated.
    """

    def search(self, query: str, limit: int = 5) -> List[ExternalSkill]:
        return []


def search_all(query: str, limit: int = 10) -> List[ExternalSkill]:
    return ClawHubSearcher().search(query, limit=limit)
