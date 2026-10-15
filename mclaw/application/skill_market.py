# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Trending caches and one-click installs backed by the shared Skill service."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote, urlencode, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from mclaw.application.sessions import ApplicationError
from mclaw.application.skills import SkillService
from mclaw.skills_hub import audits, catalog, skill_store
from mclaw.skills_hub.http_client import MarketplaceClient
from mclaw.system.lock import file_lock
from mclaw.utils import atomic_json_write

logger = logging.getLogger(__name__)
DETAIL_CACHE_LIMIT = 128
DETAIL_PREFETCH_WORKERS = 2


def refresh_boundary(now: datetime, zone: str) -> datetime:
    local = now.astimezone(ZoneInfo(zone))
    boundary = local.replace(hour=8, minute=0, second=0, microsecond=0)
    return boundary if local >= boundary else boundary - timedelta(days=1)


def _identity(url: str) -> str:
    parsed = urlparse(url)
    return (parsed.netloc.lower().removeprefix("www.") + unquote(parsed.path).rstrip("/"))


class SkillMarketService:
    def __init__(self, home: Path, *, skills=None, clock=None):
        self.home = Path(home)
        self.root = self.home / "web" / "skill-market"
        self.skills = skills or SkillService(home)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._locks = {market: threading.RLock() for market in catalog.MARKETS}
        self._guard = threading.RLock()
        self._retry_after: dict[str, datetime] = {}
        self._details: dict[str, tuple[datetime, dict]] = {}
        self._detail_requests: dict[str, Future[dict]] = {}
        self._detail_retry_after: dict[str, datetime] = {}
        self._prefetch_pools: dict[str, ThreadPoolExecutor] = {}
        self._http: MarketplaceClient | None = None
        self._audit_groups: dict[str, list[str]] = {}
        self._prefetch_jobs: dict[str, Future] = {}
        self._prefetched_catalogs: dict[str, str] = {}
        self._installing: set[str] = set()
        self._ticker: asyncio.Task | None = None
        self._timezone = ""
        try:
            value = json.loads((self.root / "settings.json").read_text(encoding="utf-8"))
            ZoneInfo(value["timezone"])
            self._timezone = value["timezone"]
        except (OSError, ValueError, KeyError, TypeError, ZoneInfoNotFoundError):
            pass
        self._load_detail_cache()

    @staticmethod
    def _detail_key(item: dict) -> str:
        return item["url"] + "?" + item["version"]

    def _load_detail_cache(self) -> None:
        path = self.root / "details.json"
        try:
            if path.stat().st_size > 64 * 1024 * 1024:
                return
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("schema_version") != 1 or not isinstance(data.get("entries"), dict):
                return
            for key, entry in list(data["entries"].items())[-DETAIL_CACHE_LIMIT:]:
                try:
                    url, _version = key.rsplit("?", 1)
                    if not (audits.catalog_identity(url) or audits.skillhub_identity(url)):
                        continue
                    expires = datetime.fromisoformat(entry["expires_at"])
                    detail = entry["detail"]
                    updated = datetime.fromisoformat(detail["detail_updated_at"])
                    if expires.tzinfo is None or updated.tzinfo is None or updated < self.clock() - timedelta(days=7):
                        continue
                    if (not isinstance(detail["content"], str) or len(detail["content"]) > 100000
                            or not isinstance(detail["audits"], dict)
                            or not isinstance(detail["audits"]["reports"], list)
                            or not isinstance(detail["detail_warning"], str)):
                        continue
                    self._details[key] = (expires, detail)
                except (ValueError, KeyError, TypeError, AttributeError):
                    continue
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    def _save_detail_cache(self) -> None:
        # Called under _guard so concurrent completions cannot overwrite a newer snapshot.
        try:
            atomic_json_write(self.root / "details.json", {
                "schema_version": 1,
                "entries": {key: {"expires_at": expires.isoformat(), "detail": detail}
                            for key, (expires, detail) in self._details.items()},
            })
        except OSError:
            logger.debug("Could not persist skill detail cache", exc_info=True)

    def _queue_detail(self, item: dict) -> None:
        key = self._detail_key(item)
        with self._guard:
            retry = self._detail_retry_after.get(key)
            cached = self._details.get(key)
            pool = self._prefetch_pools.get(item["market"])
            if (pool is None or key in self._prefetch_jobs or (retry and retry > self.clock())
                    or (cached and cached[0] > self.clock())):
                return
            future = pool.submit(self._cached_detail, dict(item))
            self._prefetch_jobs[key] = future
            def finished(done):
                with self._guard:
                    self._prefetch_jobs.pop(key, None)
                if not done.cancelled() and done.exception() is not None:
                    logger.debug("Skill detail prefetch failed: %s", item["id"])
            future.add_done_callback(finished)

    def _prefetch_catalog(self, market: str, cached: dict) -> None:
        with self._guard:
            stamp = cached["updated_at"]
            if not self._prefetch_pools or self._prefetched_catalogs.get(market) == stamp:
                return
            self._prefetched_catalogs[market] = stamp
            if market == "skills_sh":
                self._audit_groups = {}
                for item in cached["items"]:
                    self._audit_groups.setdefault(item["id"].rsplit("/", 1)[0], []).append(item["id"])
        for item in cached["items"]:
            self._queue_detail(item)

    def _rate_retry(self, market: str) -> datetime | None:
        url = "https://www.skills.sh" if market == "skills_sh" else catalog.SKILLHUB_API
        return self._http.retry_at(url) if self._http else None

    def _detail_cache_status(self, items: list[dict]) -> dict:
        with self._guard:
            keys = [self._detail_key(item) for item in items]
            ready = sum(bool(self._details.get(key) and self._details[key][1]["content"])
                        for key in keys)
            result = {"ready": ready, "total": len(keys),
                      "pending": sum(key in self._prefetch_jobs for key in keys)}
            retry = self._rate_retry(items[0]["market"]) if items else None
            if retry:
                result["retry_at"] = retry.isoformat()
            return result

    def _zone(self, value: str | None) -> str:
        value = value or self._timezone or "UTC"
        try:
            if not isinstance(value, str) or len(value) > 100:
                raise ValueError()
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ApplicationError("INVALID_TIMEZONE", "无法识别本地时区") from exc
        with self._guard:
            if self._timezone != value:
                self.root.mkdir(parents=True, exist_ok=True)
                atomic_json_write(self.root / "settings.json", {"timezone": value})
                self._timezone = value
        return value

    @staticmethod
    def _market(market: str) -> None:
        if market not in catalog.MARKETS:
            raise ApplicationError("UNKNOWN_SKILL_MARKET", "未知技能市场", 404)

    def _read(self, market: str) -> dict | None:
        try:
            data = json.loads((self.root / (market + ".json")).read_text(encoding="utf-8"))
            if data["schema_version"] != 1 or not isinstance(data["items"], list) or not data["items"]:
                return None
            updated = datetime.fromisoformat(data["updated_at"])
            if updated.tzinfo is None:
                return None
            for item in data["items"]:
                if item["market"] != market or item["url"] != catalog.source_url(market, item["id"]):
                    return None
            return data
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _snapshot(self, market: str, zone: str, *, force=False) -> dict:
        self._market(market)
        with self._locks[market]:
            now = self.clock()
            cached = self._read(market)
            limit = catalog.TRENDING_LIMITS[market]
            due = (not cached or cached.get("limit", 20) != limit
                   or datetime.fromisoformat(cached["updated_at"]) < refresh_boundary(now, zone))
            retry = self._retry_after.get(market)
            warning = ""
            if force or (due and (retry is None or now >= retry)):
                try:
                    if self._rate_retry(market):
                        raise httpx.RequestError("来源网站暂时限流")
                    items = catalog.fetch_trending(market, client=self._http)
                    cached = {"schema_version": 1, "limit": limit,
                              "updated_at": self.clock().isoformat(), "items": items}
                    self.root.mkdir(parents=True, exist_ok=True)
                    atomic_json_write(self.root / (market + ".json"), cached)
                    self._retry_after.pop(market, None)
                except (httpx.HTTPError, ValueError, OSError):
                    logger.debug("Skill marketplace refresh failed: %s", market, exc_info=True)
                    retry_at = self._rate_retry(market)
                    self._retry_after[market] = retry_at or now + timedelta(minutes=5)
                    warning = ("来源网站暂时限流，正在展示上次成功更新的榜单。" if retry_at else
                               "暂时无法刷新，正在展示上次成功更新的榜单。")
            elif retry:
                warning = "暂时无法刷新，正在展示上次成功更新的榜单。"
            if not cached:
                raise ApplicationError("SKILL_MARKET_UNAVAILABLE", f"暂时无法连接 {catalog.MARKETS[market]}，请稍后重试。", 503)
            self._prefetch_catalog(market, cached)
            boundary = refresh_boundary(now, zone)
            return {"market": market, "limit": limit, "items": [dict(item) for item in cached["items"]],
                    "updated_at": cached["updated_at"], "timezone": zone,
                    "next_refresh_at": (boundary + timedelta(days=1)).isoformat(),
                    "stale": bool(warning) or datetime.fromisoformat(cached["updated_at"]) < boundary,
                    "warning": warning}

    def _installed(self) -> dict[str, str]:
        return {_identity(record["source"].get("original", "")): record["name"]
                for record in skill_store.list_skills() if isinstance(record.get("source"), dict)}

    def trending(self, market: str, zone: str | None = None, force: bool = False) -> dict:
        self._market(market)
        result = self._snapshot(market, self._zone(zone), force=force)
        result["detail_cache"] = self._detail_cache_status(result["items"])
        installed = self._installed()
        with self._guard:
            for item in result["items"]:
                item["installed_name"] = installed.get(_identity(item["url"]))
                item["installing"] = item["url"] in self._installing
        return result

    def _item(self, market: str, identifier: str) -> dict:
        self._market(market)
        catalog.coordinate(market, identifier)
        # Accept only records from our catalog, never a client-provided fetch URL.
        data = self._read(market)
        if not data:
            data = self._snapshot(market, self._timezone or "UTC")
        item = next((row for row in data["items"] if row["id"] == identifier), None)
        if not item:
            raise ApplicationError("MARKET_SKILL_NOT_FOUND", "该技能已不在当前榜单中，请刷新榜单。", 404)
        return dict(item)

    @staticmethod
    def _description(item: dict, **kwargs) -> dict:
        try:
            return {**catalog.fetch_detail(item, **kwargs), "detail_warning": ""}
        except (httpx.HTTPError, ValueError, OSError):
            return {"content": "", "repository_url": None,
                    "detail_warning": "技能说明暂时无法读取，可前往来源网站查看。"}

    def _fetch_detail(self, item: dict) -> dict:
        client = self._http
        if item["market"] == "skillhub_cn":
            source = item["url"]
            if item["version"]:
                source += "?" + urlencode({"version": item["version"]})
            try:
                metadata = catalog.fetch_skillhub_metadata(item, client=client)
            except (httpx.HTTPError, ValueError, OSError):
                return {"content": "", "repository_url": None,
                        "detail_warning": "技能说明暂时无法读取，可前往来源网站查看。",
                        "audits": audits.fetch_audits(source, skillhub_payload={})}
            reports = audits.fetch_audits(source, skillhub_payload=metadata)
            detail = self._description(item, skillhub_metadata=metadata, client=client)
        else:
            # Independent remote reads should take the slower duration, not their sum.
            with self._guard:
                related = self._audit_groups.get(item["id"].rsplit("/", 1)[0])
            with ThreadPoolExecutor(max_workers=1) as pool:
                description = pool.submit(self._description, item, client=client)
                reports = audits.fetch_audits(item["url"], client=client, related_ids=related)
                detail = description.result()
        return {**detail, "audits": reports}

    def _cached_detail(self, item: dict) -> dict:
        key = self._detail_key(item)
        with self._guard:
            cached = self._details.get(key)
            if cached and cached[0] > self.clock():
                self._details.pop(key)
                self._details[key] = cached
                return cached[1]
            pending = self._detail_requests.get(key)
            owner = pending is None
            if owner:
                pending = Future()
                self._detail_requests[key] = pending
        if not owner:
            return pending.result()
        try:
            detail = self._fetch_detail(item)
            if detail["detail_warning"] and cached and cached[1]["content"]:
                # Keep a readable prior snapshot when the source is temporarily unavailable.
                detail = {**cached[1], "detail_warning": detail["detail_warning"],
                          "audits": detail["audits"] if detail["audits"]["available"] else cached[1]["audits"]}
            else:
                detail = {**detail, "detail_updated_at": self.clock().isoformat()}
            incomplete = detail["detail_warning"] or any(
                row["status"] == "unknown" for row in detail["audits"]["reports"])
            with self._guard:
                if key not in self._details and len(self._details) >= DETAIL_CACHE_LIMIT:
                    self._details.pop(next(iter(self._details)))
                self._details[key] = (self.clock() + timedelta(seconds=60 if incomplete else 900), detail)
                self._detail_retry_after.pop(key, None)
                self._save_detail_cache()
            pending.set_result(detail)
            return detail
        except BaseException as exc:
            with self._guard:
                self._detail_retry_after[key] = self.clock() + timedelta(seconds=60)
            pending.set_exception(exc)
            raise
        finally:
            with self._guard:
                self._detail_requests.pop(key, None)

    def detail(self, market: str, identifier: str) -> dict:
        item = self._item(market, identifier)
        key = self._detail_key(item)
        with self._guard:
            cached = self._details.get(key)
            background = bool(self._prefetch_pools)
        if cached and background:
            if cached[0] <= self.clock():
                self._queue_detail(item)
            detail = cached[1]
        elif background and self._rate_retry(market):
            self._queue_detail(item)
            reports = (audits.normalize_audits(identifier, {}) if market == "skills_sh"
                       else audits.normalize_skillhub_audits({}))
            detail = {"content": "", "repository_url": None, "audits": reports,
                      "detail_warning": "来源网站暂时限流，稍后自动补齐详情。", "detail_updated_at": ""}
        else:
            detail = self._cached_detail(item)
        with self._guard:
            cached = self._details.get(key)
            if cached:
                detail = cached[1]
                self._details.pop(key)
                self._details[key] = cached
            refreshing = key in self._prefetch_jobs or key in self._detail_requests
            stale = bool(detail["detail_warning"]) or bool(cached and cached[0] <= self.clock())
        return {**item, **detail, "detail_refreshing": refreshing, "detail_stale": stale,
                "installed_name": self._installed().get(_identity(item["url"]))}

    def install(self, market: str, identifier: str) -> dict:
        item = self._item(market, identifier)
        key = hashlib.sha256(item["url"].encode()).hexdigest()
        with file_lock(self.root / "install-locks" / (key + ".lock")):
            existing = self._installed().get(_identity(item["url"]))
            if existing:
                return {"success": True, "name": existing, "already_installed": True}
            with self._guard:
                self._installing.add(item["url"])
            draft = None
            try:
                source = item["url"]
                if market == "skillhub_cn" and item["version"]:
                    source += "?" + urlencode({"version": item["version"]})
                draft = self.skills.prepare(source, f"从技能市场安装 {item['name']}")
                # The authenticated install POST is the user's install action.
                result = self.skills.confirm(draft["drafting_id"], draft["confirmation_token"])
                return {**result, "audits": (draft.get("review") or {}).get("audits")}
            except BaseException:
                if draft:
                    with contextlib.suppress(OSError, ValueError, ApplicationError):
                        self.skills.cancel(draft["drafting_id"])
                raise
            finally:
                with self._guard:
                    self._installing.discard(item["url"])

    def refresh_due(self) -> None:
        if self._timezone:
            for market in catalog.MARKETS:
                try:
                    self._snapshot(market, self._timezone)
                except ApplicationError:
                    pass

    async def start(self) -> None:
        with self._guard:
            if self._prefetch_pools:
                return
            self._prefetched_catalogs.clear()
            self._http = MarketplaceClient()
            # Separate queues let the other origin continue while one is cooling down.
            self._prefetch_pools = {
                market: ThreadPoolExecutor(max_workers=DETAIL_PREFETCH_WORKERS,
                                           thread_name_prefix=f"skill-market-{market}")
                for market in catalog.MARKETS
            }
        async def run():
            while True:
                refresh = asyncio.create_task(asyncio.to_thread(self.refresh_due))
                try:
                    await asyncio.shield(refresh)
                except asyncio.CancelledError:
                    # Cancelling to_thread does not stop its HTTP read. Drain it before closing the client.
                    with contextlib.suppress(Exception):
                        await refresh
                    raise
                await asyncio.sleep(30)
        self._ticker = asyncio.create_task(run())

    async def close(self) -> None:
        with self._guard:
            pools, self._prefetch_pools = self._prefetch_pools, {}
        if self._http:
            self._http.stop_waiting()
        if self._ticker:
            self._ticker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._ticker
        # Cancel both queues before waiting for running requests, then release their connections.
        await asyncio.gather(*(asyncio.to_thread(pool.shutdown, wait=True, cancel_futures=True)
                               for pool in pools.values()))
        if self._http:
            await asyncio.to_thread(self._http.close)
            self._http = None
