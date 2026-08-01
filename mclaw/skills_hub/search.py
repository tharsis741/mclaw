# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""External Skill catalog adapters.

skills.sh is the default discovery provider.  The ClawHub adapter remains in
this module because existing ClawHub URLs still use its detail endpoint during
manual installation.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import List
from urllib.parse import quote

import httpx

from mclaw.skills_hub.models import ExternalSkill
from mclaw.tools.cancellation import cancellation_checkpoint

logger = logging.getLogger(__name__)

SKILLS_SH_BASE_URL = "https://skills.sh"
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
        raise ValueError("Skill catalog returned a non-object response.")
    return data


def _run_request(
    coro,
    *,
    parent_agent,
    diagnostic_name: str,
    timeout: float = 10.0,
):
    from mclaw.tools.dispatch import _run_async

    return _run_async(
        coro,
        parent_agent=parent_agent,
        diagnostic_name=diagnostic_name,
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


def _skills_sh_catalog_parts(item: dict) -> tuple[str, str, str]:
    """Return (catalog_id, repository, skill_slug) for a GitHub-backed result."""

    catalog_id = str(item.get("id") or "").strip().strip("/")
    repository = str(item.get("source") or "").strip().strip("/")
    skill_slug = str(item.get("skillId") or "").strip()
    if not repository and catalog_id.count("/") >= 2:
        repository = "/".join(catalog_id.split("/")[:2])
    if not skill_slug and catalog_id:
        skill_slug = catalog_id.rsplit("/", 1)[-1]
    if not skill_slug:
        skill_slug = str(item.get("name") or "").strip()
    if not catalog_id and repository and skill_slug:
        catalog_id = f"{repository}/{skill_slug}"
    return catalog_id, repository, skill_slug


def _skills_sh_url(catalog_id: str) -> str:
    parts = [quote(part, safe="") for part in catalog_id.split("/") if part]
    return f"{SKILLS_SH_BASE_URL}/{'/'.join(parts)}" if parts else ""


class SkillsShSearcher:
    """Adapter for the unauthenticated search endpoint used by skills.sh CLI."""

    def search(
        self,
        query: str,
        limit: int = 10,
        *,
        cancel_event: threading.Event | None = None,
        parent_agent=None,
    ) -> List[ExternalSkill]:
        """Search skills.sh and return installable GitHub-backed records."""

        cancellation_checkpoint(cancel_event)
        query = str(query or "").strip()
        if not query:
            return []
        bounded_limit = max(1, min(_as_int(limit, 10), 50))
        try:
            data = _run_request(
                _get_json_async(
                    f"{SKILLS_SH_BASE_URL}/api/search",
                    params={"q": query, "limit": str(bounded_limit)},
                    timeout=10.0,
                ),
                parent_agent=parent_agent,
                diagnostic_name="skills_sh_http",
            )
            cancellation_checkpoint(cancel_event)
        except (httpx.HTTPError, TimeoutError, ValueError):
            cancellation_checkpoint(cancel_event)
            logger.debug("skills.sh search failed", exc_info=True)
            return []

        items = data.get("skills", []) if isinstance(data, dict) else []
        if not isinstance(items, list):
            return []

        results: List[ExternalSkill] = []
        for item in items:
            cancellation_checkpoint(cancel_event)
            if not isinstance(item, dict):
                continue
            catalog_id, repository, skill_slug = _skills_sh_catalog_parts(item)
            repo_parts = [part for part in repository.split("/") if part]
            # The no-key install path downloads a public GitHub repository.  A
            # well-known/non-GitHub source cannot be materialized by that path.
            if len(repo_parts) != 2 or not catalog_id or not skill_slug:
                continue
            # Build the canonical ID from the independently returned source and
            # selector so an inconsistent upstream id cannot redirect install.
            catalog_id = f"{repository}/{skill_slug}"
            url = _skills_sh_url(catalog_id)
            installs = _as_int(item.get("installs"))
            results.append(
                ExternalSkill(
                    source="skills_sh",
                    name=str(item.get("name") or skill_slug).strip(),
                    description=str(item.get("description") or "").strip(),
                    slug=skill_slug,
                    url=url,
                    author=repo_parts[0],
                    installs=installs,
                    catalog_id=catalog_id,
                    repository=repository,
                    install_source=url,
                    platforms=None,
                    frontmatter={},
                )
            )
            if len(results) >= bounded_limit:
                break
        cancellation_checkpoint(cancel_event)
        return results


class ClawHubSearcher:
    """Small adapter around ClawHub search and detail endpoints."""

    def get_skill_detail(
        self,
        slug: str,
        *,
        owner_handle: str = "",
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
                    params={"owner": owner_handle} if owner_handle else None,
                    timeout=10.0,
                ),
                parent_agent=parent_agent,
                diagnostic_name="clawhub_http",
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
                diagnostic_name="clawhub_http",
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
                    owner_handle=owner_handle,
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
            canonical_path = str(item.get("canonicalUrl") or "").strip()
            if canonical_path.startswith("/"):
                url = f"https://clawhub.ai{canonical_path}"
            else:
                url = (
                    f"https://clawhub.ai/{owner_handle}/skills/{slug}"
                    if owner_handle and slug
                    else ""
                )
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

    return SkillsShSearcher().search(
        query,
        limit=limit,
        cancel_event=cancel_event,
        parent_agent=parent_agent,
    )
