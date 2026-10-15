# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Public marketplace adapters. Remote text is data, never executable code."""

from __future__ import annotations

import json
import re
from contextlib import nullcontext
from html.parser import HTMLParser
from urllib.parse import quote, urlparse

import httpx

SKILLHUB_API = "https://api.skillhub.cn/api/v1"
MARKETS = {"skills_sh": "skills.sh", "skillhub_cn": "SkillHub"}
TRENDING_LIMITS = {"skills_sh": 40, "skillhub_cn": 40}
ICON_HOSTS = frozenset({
    "cloudcache.tencent-cloud.com",
    "skillhub-1388575217.cos.accelerate.myqcloud.com",
})
_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_HOST = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$", re.I)


def coordinate(market: str, identifier: str) -> list[str]:
    """Validate a catalog identity before using any part in a URL."""
    if market not in MARKETS or not isinstance(identifier, str):
        raise ValueError("未知技能市场")
    parts = identifier.split("/")
    valid_length = len(parts) == (3 if market == "skills_sh" else 2)
    if market == "skills_sh" and len(parts) == 2:
        valid_length = bool(_HOST.fullmatch(parts[0]))
    if not valid_length or any(
        not _PART.fullmatch(part) or ".." in part for part in parts
    ):
        raise ValueError("技能来源地址无效")
    return parts


def source_url(market: str, identifier: str) -> str:
    parts = coordinate(market, identifier)
    path = "/".join(quote(part, safe="") for part in parts)
    return f"https://skills.sh/{path}" if market == "skills_sh" else f"https://skillhub.cn/skills/{path}"


def get_text(url: str, *, params: dict | None = None, timeout: float = 15.0,
             client: httpx.Client | None = None) -> str:
    """Read bounded public metadata without credentials or user model calls."""
    session = nullcontext(client) if client is not None else httpx.Client()
    with session as active_client:
        with active_client.stream("GET", url, params=params, timeout=timeout, follow_redirects=True) as response:
            response.raise_for_status()
            content = bytearray()
            for chunk in response.iter_bytes():
                content.extend(chunk)
                if len(content) > 4 * 1024 * 1024:
                    raise ValueError("技能市场返回的内容过大")
    return content.decode("utf-8")


def get_json(url: str, *, params: dict | None = None, timeout: float = 15.0,
             client: httpx.Client | None = None) -> dict:
    data = json.loads(get_text(url, params=params, timeout=timeout, client=client))
    if not isinstance(data, dict):
        raise ValueError("技能市场返回的格式有变化")
    return data


def _count(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def safe_icon(value) -> str | None:
    url = str(value or "")
    parsed = urlparse(url)
    if parsed.scheme == "https" and parsed.netloc in ICON_HOSTS:
        return url
    return None


def parse_skills_sh_trending(html: str) -> list[dict]:
    """Decode the site's server-rendered leaderboard; never eval Next scripts."""
    chunks = []
    for match in re.finditer(r'self\.__next_f\.push\((\[.*?\])\)</script>', html, re.S):
        try:
            value = json.loads(match[1])
        except ValueError:
            continue
        if isinstance(value, list) and len(value) > 1 and isinstance(value[1], str):
            chunks.append(value[1])
    stream = "".join(chunks)
    for match in re.finditer(r'"initialSkills"\s*:\s*', stream):
        try:
            rows, _ = json.JSONDecoder().raw_decode(stream[match.end():])
        except ValueError:
            continue
        if isinstance(rows, list):
            return rows
    raise ValueError("skills.sh 榜单格式有变化，请稍后重试")


def normalize_trending(market: str, rows: list) -> list[dict]:
    result, seen = [], set()
    limit = TRENDING_LIMITS[market]
    # Keep upstream ranks. Counts measure different things on the two sources.
    for rank, row in enumerate(rows[:limit], 1):
        if not isinstance(row, dict):
            continue
        if market == "skills_sh":
            slug = str(row.get("skillId") or row.get("name") or "")
            owner = str(row.get("source") or "")
            identifier = f"{owner}/{slug}"
            name = str(row.get("name") or slug)
            description = str(row.get("description") or "")
            labels = {}
        else:
            namespace = row.get("namespace") or {}
            if not isinstance(namespace, dict):
                continue
            owner = str(namespace.get("handle") or "")
            slug = str(namespace.get("publicSlug") or row.get("slug") or "")
            identifier = f"{owner}/{slug}"
            name = str(row.get("name") or slug)
            description = str(row.get("description_zh") or row.get("description") or "")
            labels = row.get("labels") or {}
            if not isinstance(labels, dict):
                labels = {}
        try:
            url = source_url(market, identifier)
        except ValueError:
            continue
        if identifier in seen:
            continue
        seen.add(identifier)
        pricing = str(labels.get("pricing_type") or "").lower()
        result.append({
            "id": identifier, "market": market, "rank": rank, "slug": slug,
            "name": name[:256], "description": description[:12000], "publisher": owner,
            "url": url, "icon_url": safe_icon(row.get("iconUrl")) if market == "skillhub_cn" else None,
            "count": _count(row.get("installs" if market == "skills_sh" else "downloads")),
            "count_label": "24h 安装量" if market == "skills_sh" else "下载量",
            "version": str(row.get("version") or "")[:80],
            "requires_api_key": str(labels.get("requires_api_key") or "").lower() == "true",
            "paid": pricing not in {"", "free", "none", "false"} or "付费" in name,
        })
    if not result:
        raise ValueError("暂时没有可读取的 Trending 榜单")
    return result


def fetch_trending(market: str, *, client: httpx.Client | None = None) -> list[dict]:
    if market == "skills_sh":
        rows = parse_skills_sh_trending(get_text("https://www.skills.sh/trending", client=client))
    elif market == "skillhub_cn":
        rows = get_json(f"{SKILLHUB_API}/showcase/trending", client=client).get("skills")
    else:
        raise ValueError("未知技能市场")
    if not isinstance(rows, list):
        raise ValueError("技能市场榜单格式有变化")
    return normalize_trending(market, rows)


class _SkillPage(HTMLParser):
    """Extract readable content from skills.sh's prose containers as Markdown."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.description = ""
        self.depth = 0
        self.parts: list[str] = []
        self.blocks: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta" and attrs.get("name") == "description":
            self.description = attrs.get("content", "")
        if self.depth:
            if tag == "div":
                self.depth += 1
            if tag in {"p", "pre", "ul", "ol", "br"}:
                self.parts.append("\n\n")
            elif tag in {"h1", "h2", "h3", "h4"}:
                self.parts.append("\n\n" + "#" * int(tag[1]) + " ")
            elif tag == "li":
                self.parts.append("\n- ")
            elif tag == "code":
                self.parts.append("`")
        elif tag == "div" and "prose" in attrs.get("class", "").split():
            self.depth = 1
            self.parts = []

    def handle_endtag(self, tag):
        if not self.depth:
            return
        if tag == "div":
            self.depth -= 1
            if not self.depth:
                self.blocks.append("".join(self.parts).strip())
        elif tag in {"p", "pre", "h1", "h2", "h3", "h4"}:
            self.parts.append("\n\n")
        elif tag == "code":
            self.parts.append("`")

    def handle_data(self, data):
        if self.depth:
            self.parts.append(data)


def fetch_skillhub_metadata(item: dict, *, client: httpx.Client | None = None) -> dict:
    namespace, slug = coordinate("skillhub_cn", item["id"])
    data = get_json(f"{SKILLHUB_API}/skills/{quote(slug, safe='')}", params={"namespace": namespace}, client=client)
    returned = data.get("namespace")
    if not isinstance(returned, dict) or returned.get("handle") != namespace or returned.get("publicSlug") != slug:
        raise ValueError("技能详情来源不匹配")
    return data


def fetch_detail(item: dict, *, skillhub_metadata: dict | None = None,
                 client: httpx.Client | None = None) -> dict:
    market, identifier = item["market"], item["id"]
    parts = coordinate(market, identifier)
    if market == "skills_sh":
        page = _SkillPage()
        page.feed(get_text(source_url(market, identifier).replace("https://skills.sh/", "https://www.skills.sh/"),
                           client=client))
        return {"description": page.description or item["description"],
                "content": max(page.blocks, key=len, default="")[:100000],
                "repository_url": "https://github.com/" + "/".join(parts[:2]) if len(parts) == 3 else None}
    namespace, slug = parts
    params = {"namespace": namespace}
    data = skillhub_metadata if skillhub_metadata is not None else fetch_skillhub_metadata(item, client=client)
    skill = data.get("skill") or {}
    content = str(skill.get("overviewMd") or "")
    if not content:
        try:
            content = get_text(f"{SKILLHUB_API}/skills/{quote(slug, safe='')}/file", params={
                **params, "path": "SKILL.md", **({"version": item["version"]} if item["version"] else {}),
            }, client=client)
        except (httpx.HTTPError, ValueError):
            content = str(skill.get("summary_zh") or skill.get("summary") or item["description"])
    return {"description": str(skill.get("summary_zh") or skill.get("summary") or item["description"]),
            "content": content[:100000], "repository_url": None}
