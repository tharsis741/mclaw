# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared third-party security reports for chat and marketplace installs."""

from __future__ import annotations

import re
import threading
import time
from concurrent.futures import Future
from datetime import datetime, timezone
from urllib.parse import parse_qs, quote, unquote, urlparse

import httpx

from mclaw.skills_hub.catalog import SKILLHUB_API, coordinate, get_json, source_url
from mclaw.tools.cancellation import cancellation_checkpoint

PROVIDERS = (
    ("ath", "综合安全评估", "Gen", "agent-trust-hub"),
    ("socket", "代码安全检查", "Socket", "socket"),
    ("snyk", "指令风险检查", "Snyk", "snyk"),
)
SKILLHUB_PROVIDERS = (
    ("keen", "科恩实验室", "tix.qq.com"),
    ("sanbu", "云鼎实验室", "static.cloudsec.tencent.com"),
)
SKILLHUB_STATUSES = {
    "benign": ("pass", "low", "安全，无风险"),
    "suspicious": ("warn", "medium", "存在可疑风险"),
    "malicious": ("warn", "high", "存在安全风险"),
    "scanning": ("unknown", "unknown", "扫描中"),
    "queued": ("unknown", "unknown", "等待扫描"),
    "failed": ("unknown", "unknown", "扫描失败"),
}
_cache: dict[str, tuple[float, dict]] = {}
_guard = threading.Lock()
_batch_requests: dict[tuple[str, ...], Future] = {}


def catalog_identity(url: str) -> str | None:
    parsed = urlparse(str(url or ""))
    if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() not in {"skills.sh", "www.skills.sh"}:
        return None
    identifier = unquote(parsed.path.strip("/"))
    try:
        coordinate("skills_sh", identifier)
    except ValueError:
        return None
    return identifier


def normalize_audits(identifier: str, data: dict) -> dict:
    rows = []
    root = source_url("skills_sh", identifier)
    for key, label, provider, path in PROVIDERS:
        raw = data.get(key) or {}
        if not isinstance(raw, dict):
            raw = {}
        risk = str(raw.get("risk") or "unknown").lower()
        alerts = raw.get("alerts") if type(raw.get("alerts")) is int else None
        status = "pass" if risk in {"safe", "low", "none"} else "warn" if risk in {"medium", "high", "critical"} else "unknown"
        if alerts is not None and alerts > 0:
            status = "warn"
        text = {"pass": "通过", "warn": "需留意", "unknown": "暂无报告"}[status]
        if key == "socket" and status != "unknown" and alerts is not None:
            text = f"{alerts} 项告警"
        rows.append({"provider": provider, "label": label, "status": status, "status_text": text,
                     "risk": risk, "alerts": alerts, "analyzed_at": str(raw.get("analyzedAt") or ""),
                     "report_url": f"{root}/security/{path}"})
    return _bundle("skills.sh", rows)


def _bundle(source: str, rows: list[dict]) -> dict:
    warnings = [row["label"] for row in rows if row["status"] == "warn"]
    available = any(row["status"] != "unknown" for row in rows)
    notice = ("、".join(warnings) + "提示需留意，请查看报告了解详情。") if warnings else (
        "第三方审核结果供安装时参考。" if available else "暂未获取到审核报告，可在来源网站查看。")
    return {"source": source, "available": available, "reports": rows, "notice": notice,
            "checked_at": datetime.now(timezone.utc).isoformat()}


def skillhub_identity(url: str) -> tuple[str, str, str] | None:
    parsed = urlparse(str(url or ""))
    if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() not in {"skillhub.cn", "www.skillhub.cn"}:
        return None
    parts = [unquote(part) for part in parsed.path.strip("/").split("/")]
    query = parse_qs(parsed.query)
    if len(parts) == 3 and parts[0] == "skills":
        namespace, slug = parts[1:]
    elif len(parts) == 2 and parts[0] == "skills":
        namespace, slug = query.get("namespace", [""])[0], parts[1]
    else:
        return None
    version = query.get("version", [""])[0]
    try:
        coordinate("skillhub_cn", f"{namespace or 'default'}/{slug}")
        if version and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,79}", version):
            return None
    except ValueError:
        return None
    return namespace, slug, version


def _report_url(value: object, host: str) -> str:
    if not isinstance(value, str) or any(char.isspace() or char == "\\" for char in value):
        return ""
    try:
        parsed = urlparse(value)
        if parsed.scheme == "https" and parsed.netloc.lower() == host:
            return value  # Preserve the full upstream signature and query encoding.
    except ValueError:
        pass
    return ""


def normalize_skillhub_audits(payload: dict, version: str = "") -> dict:
    data = payload.get("securityReports")
    latest = payload.get("latestVersion")
    # The public detail endpoint exposes the latest version's reports only.
    mismatch = bool(version and (not isinstance(latest, dict) or latest.get("version") != version))
    if not isinstance(data, dict) or mismatch:
        data = {}
    rows = []
    for key, label, host in SKILLHUB_PROVIDERS:
        raw = data.get(key)
        raw = raw if isinstance(raw, dict) else {}
        upstream_status = raw.get("status")
        upstream_status = upstream_status if isinstance(upstream_status, str) else ""
        status, risk, fallback = SKILLHUB_STATUSES.get(upstream_status, ("unknown", "unknown", "暂无报告"))
        text = raw.get("statusText")
        text = text.strip() if upstream_status in SKILLHUB_STATUSES and isinstance(text, str) else ""
        rows.append({"provider": label, "label": label, "status": status,
                     "status_text": text[:100] or fallback, "risk": risk, "alerts": None,
                     "analyzed_at": "", "report_url": _report_url(raw.get("reportUrl"), host)})
    result = _bundle("skillhub.cn", rows)
    if mismatch:
        result["notice"] = "当前版本暂无审核报告，可在来源网站查看。"
    elif all(row["status"] == "pass" for row in rows):
        result["notice"] = "科恩实验室、云鼎实验室均显示安全，无风险。"
    return result


def _fetch_skillhub_audits(identity: tuple[str, str, str], payload: dict | None = None,
                          *, client: httpx.Client | None = None) -> dict:
    namespace, slug, version = identity
    if payload is None:
        payload = get_json(f"{SKILLHUB_API}/skills/{quote(slug, safe='')}",
                           params={"namespace": namespace} if namespace else None, timeout=6, client=client)
    returned = payload.get("namespace")
    if not isinstance(returned, dict) or (namespace and returned.get("handle") != namespace) or returned.get("publicSlug") != slug:
        payload = {}
    return normalize_skillhub_audits(payload, version)


def _remember(key: str, result: dict) -> None:
    ttl = 60 if any(row["status"] == "unknown" for row in result["reports"]) else 900
    if key not in _cache and len(_cache) >= 256:
        _cache.pop(next(iter(_cache)))
    _cache[key] = (time.monotonic() + ttl, result)


def _english_batch(identifier: str, related_ids: list[str] | None, client: httpx.Client | None) -> dict:
    """Use the upstream comma-separated skills parameter, retaining per-skill cache keys."""
    source = identifier.rsplit("/", 1)[0]
    identifiers = {identifier}
    for related in (related_ids or [])[:40]:
        if catalog_identity(source_url("skills_sh", related)) and related.rsplit("/", 1)[0] == source:
            identifiers.add(related)
    key = tuple(sorted(identifiers))
    with _guard:
        cached = {value: _cache.get("skills.sh/" + value) for value in key}
        missing = [value for value in key if not cached[value] or cached[value][0] <= time.monotonic()]
        if not missing:
            return cached[identifier][1]
        pending = _batch_requests.get(key)
        owner = pending is None
        if owner:
            pending = Future()
            _batch_requests[key] = pending
    if not owner:
        return pending.result()[identifier]
    try:
        try:
            payload = get_json("https://www.skills.sh/tele/audit", params={
                "source": source, "skills": ",".join(value.rsplit("/", 1)[1] for value in missing),
            }, timeout=6, client=client)
        except (httpx.HTTPError, ValueError, OSError):
            payload = {}
        results = {value: entry[1] for value, entry in cached.items() if value not in missing}
        with _guard:
            for value in missing:
                data = payload.get(value.rsplit("/", 1)[1]) or {}
                results[value] = normalize_audits(value, data if isinstance(data, dict) else {})
                _remember("skills.sh/" + value, results[value])
        pending.set_result(results)
        return results[identifier]
    except BaseException as exc:
        pending.set_exception(exc)
        raise
    finally:
        with _guard:
            _batch_requests.pop(key, None)


def fetch_audits(url: str, *, cancel_event=None, skillhub_payload: dict | None = None,
                 client: httpx.Client | None = None, related_ids: list[str] | None = None) -> dict:
    """Read reports, reusing already fetched SkillHub metadata when provided."""
    cancellation_checkpoint(cancel_event)
    identifier = catalog_identity(url)
    chinese = skillhub_identity(url) if not identifier else None
    if not identifier and not chinese:
        return {"source": "", "available": False, "reports": [], "notice": "暂无审核报告。", "checked_at": ""}
    cache_key = "skills.sh/" + identifier if identifier else "skillhub.cn/" + "/".join(chinese)
    with _guard:
        cached = _cache.get(cache_key)
        if cached and cached[0] > time.monotonic() and skillhub_payload is None:
            return cached[1]
    try:
        if chinese:
            result = _fetch_skillhub_audits(chinese, skillhub_payload, client=client)
        else:
            result = _english_batch(identifier, related_ids, client)
    except (httpx.HTTPError, ValueError, OSError):
        result = normalize_skillhub_audits({}) if chinese else normalize_audits(identifier, {})
    cancellation_checkpoint(cancel_event)
    with _guard:
        _remember(cache_key, result)
    return result


def informational_review(url: str, *, cancel_event=None) -> dict:
    audits = fetch_audits(url, cancel_event=cancel_event)
    reports = audits["reports"]
    risks = [row["risk"] for row in reports]
    warnings = any(row["status"] == "warn" for row in reports)
    complete = bool(reports) and all(row["status"] == "pass" for row in reports)
    risk = "high" if any(value in {"high", "critical"} for value in risks) else "medium" if warnings else "low" if complete else "unknown"
    return {"risk_level": risk, "verdict": "warning" if warnings else "safe" if complete else "unknown",
            "install_allowed": True, "install_policy_reason": "由用户自主选择安装。",
            "summary": audits["notice"], "scan_report": audits["notice"], "findings": [],
            "audits": audits}
