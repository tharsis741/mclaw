# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import json
import socket
import threading
import zipfile
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.testclient import TestClient

from mclaw.application.sessions import ApplicationError
from mclaw.application import skill_market as market_module
from mclaw.application.skill_market import SkillMarketService, refresh_boundary
from mclaw.skills_hub import audits, catalog, install_service, skill_store, source_resolver
from mclaw.skills_hub.http_client import MarketplaceClient
from mclaw.skills_hub.paths import get_enabled_skills_dir, get_skill_drafting_dir
from mclaw.skills_hub.security import review_skill_package
from mclaw.web.app import LocalAccess
from mclaw.web.skill_market import build_skill_market_routes


def english_rows(count=40):
    return [{"source": "example/skills", "skillId": f"skill-{n}", "name": f"skill-{n}", "installs": 100 - n}
            for n in range(count)]


def chinese_rows():
    return [{"slug": "office", "name": "办公助手", "description_zh": "整理办公文档",
             "namespace": {"handle": "publisher", "publicSlug": "office"}, "downloads": 85000,
             "version": "1.2.0", "iconUrl": "https://cloudcache.tencent-cloud.com/icon.png",
             "labels": {"requires_api_key": "true", "pricing_type": "alipay"}}]


def chinese_ranked_rows(count=45):
    return [{**chinese_rows()[0], "slug": f"office-{n}",
             "namespace": {"handle": "publisher", "publicSlug": f"office-{n}"},
             "downloads": count - n} for n in range(count)]


def chinese_audit_payload(status="benign"):
    return {
        "namespace": {"handle": "publisher", "publicSlug": "office"},
        "latestVersion": {"version": "1.2.0"},
        "securityReports": {
            "keen": {"status": "benign", "statusText": "安全，无风险",
                     "reportUrl": "https://tix.qq.com/search/skill?keyword=example-hash"},
            "sanbu": {"status": status,
                      "reportUrl": "https://static.cloudsec.tencent.com/html-report-v2/example.html?q-sign-time=123%3B456&q-signature=example"},
        },
    }


@pytest.fixture(autouse=True)
def isolated_audits(monkeypatch):
    audits._cache.clear()
    monkeypatch.setattr(audits, "get_json", lambda *_a, **_k: {})
    yield
    audits._cache.clear()


@pytest.fixture
def market(tmp_path, monkeypatch):
    monkeypatch.setenv("MCLAW_HOME", str(tmp_path))
    calls = []

    def fetch(source, **_kwargs):
        calls.append(source)
        return catalog.normalize_trending(source, english_rows() if source == "skills_sh" else chinese_rows())

    monkeypatch.setattr(catalog, "fetch_trending", fetch)
    clock = SimpleNamespace(now=datetime(2026, 10, 14, 23, 59, tzinfo=timezone.utc))
    service = SkillMarketService(tmp_path, clock=lambda: clock.now)
    service.calls, service.test_clock = calls, clock
    return service


@pytest.mark.parametrize("path", ["/good", "/failed", "/too-large"])
def test_public_reads_reuse_borrowed_client_with_bounds_timeouts_and_response_cleanup(path):
    requests, responses = [], []
    def handle(request):
        requests.append(request)
        if request.url.path == "/redirect":
            response = httpx.Response(302, headers={"location": path})
        elif request.url.path == "/failed":
            response = httpx.Response(503)
        else:
            response = httpx.Response(200, content=b"x" * (4 * 1024 * 1024 + 1) if path == "/too-large" else b"ready")
        responses.append(response)
        return response
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        if path == "/good":
            assert catalog.get_text("https://example.test/redirect", client=client, timeout=6) == "ready"
            assert catalog.get_text("https://example.test/good", client=client) == "ready"
        else:
            with pytest.raises(httpx.HTTPStatusError if path == "/failed" else ValueError):
                catalog.get_text("https://example.test/redirect", client=client, timeout=6)
        assert not client.is_closed
        assert all(response.is_closed for response in responses)
        assert requests[0].extensions["timeout"] == dict.fromkeys(("connect", "read", "write", "pool"), 6)
    assert client.is_closed


def test_one_off_public_read_closes_its_owned_client(monkeypatch):
    client = httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={"ok": True})))
    monkeypatch.setattr(catalog.httpx, "Client", lambda: client)
    assert catalog.get_json("https://example.test/metadata") == {"ok": True}
    assert client.is_closed


def test_decode_next_stream_and_preserve_top_forty_upstream_order():
    rows = english_rows(45)
    rows[0]["installs"] = 2
    encoded = '4e:["$","$L",null,' + json.dumps({"initialSkills": rows}) + ']'
    chunks = [encoded[:30], encoded[30:]]
    html = ''.join('<script>self.__next_f.push(' + json.dumps([1, part]) + ')</script>' for part in chunks)
    items = catalog.normalize_trending("skills_sh", catalog.parse_skills_sh_trending(html))
    assert len(items) == 40
    assert items[0]["count"] == 2 and items[0]["rank"] == 1
    assert items[-1]["id"] == "example/skills/skill-39" and items[-1]["rank"] == 40
    assert all(item["icon_url"] is None for item in items)
    assert items[0]["count_label"] == "24h 安装量"


def test_changed_upstream_html_fails_instead_of_empty_success():
    with pytest.raises(ValueError, match="格式"):
        catalog.parse_skills_sh_trending('<html>sign in</html>')


def test_chinese_fetch_keeps_the_first_forty_upstream_ranks(monkeypatch):
    rows = chinese_ranked_rows()
    rows[0]["downloads"] = 1
    monkeypatch.setattr(catalog, "get_json", lambda *_a, **_k: {"skills": rows})
    items = catalog.fetch_trending("skillhub_cn")
    assert len(items) == 40
    assert items[0]["id"] == "publisher/office-0" and items[0]["count"] == 1
    assert items[-1]["id"] == "publisher/office-39" and items[-1]["rank"] == 40
    rows[3]["namespace"] = {"handle": "../invalid", "publicSlug": "office-3"}
    items = catalog.fetch_trending("skillhub_cn")
    assert len(items) == 39 and items[-1]["rank"] == 40


@pytest.mark.parametrize("offline", [False, True])
@pytest.mark.parametrize("source", ["skills_sh", "skillhub_cn"])
@pytest.mark.parametrize("stored_limit", [None, 20])
def test_legacy_cache_refreshes_for_top_forty_and_survives_failure(market, monkeypatch, offline, source, stored_limit):
    rows = english_rows if source == "skills_sh" else chinese_ranked_rows
    old = {"schema_version": 1, "updated_at": market.test_clock.now.isoformat(),
           "items": catalog.normalize_trending(source, rows(20))}
    if stored_limit is not None:
        old["limit"] = stored_limit
    market.root.mkdir(parents=True, exist_ok=True)
    path = market.root / f"{source}.json"
    path.write_text(json.dumps(old), encoding="utf-8")
    before = path.read_bytes()
    calls = []
    def fetch(source, **_kwargs):
        calls.append(source)
        if offline:
            raise httpx.ConnectError("offline")
        return catalog.normalize_trending(source, rows(45))
    monkeypatch.setattr(catalog, "fetch_trending", fetch)
    result = market.trending(source, "Asia/Shanghai")
    assert result["limit"] == 40
    assert len(result["items"]) == (20 if offline else 40)
    assert result["stale"] is offline
    if offline:
        assert result["warning"] and path.read_bytes() == before
    else:
        assert json.loads(path.read_text(encoding="utf-8"))["limit"] == 40
    market.trending(source, "Asia/Shanghai")
    assert calls == [source]


def test_chinese_fields_requirements_and_icon_allowlist():
    rows = chinese_rows()
    item = catalog.normalize_trending("skillhub_cn", rows)[0]
    assert item["name"] == "办公助手"
    assert item["url"] == "https://skillhub.cn/skills/publisher/office"
    assert item["requires_api_key"] and item["paid"]
    assert item["count_label"] == "下载量"
    rows[0]["labels"] = {"requires_api_key": "false", "pricing_type": "free"}
    rows[0]["iconUrl"] = "https://cloudcache.tencent-cloud.com.evil.test/icon.png"
    item = catalog.normalize_trending("skillhub_cn", rows)[0]
    assert not item["requires_api_key"] and not item["paid"] and item["icon_url"] is None


def test_invalid_catalog_entries_cannot_redirect_install():
    rows = english_rows(3)
    rows[0]["source"] = "https://evil.example/"
    rows[1]["skillId"] = "../secret"
    item, = catalog.normalize_trending("skills_sh", rows)
    assert item["rank"] == 3
    assert item["url"] == "https://skills.sh/example/skills/skill-2"


def test_refresh_at_local_eight_and_cached_before_boundary(market):
    before = market.trending("skills_sh", "Asia/Shanghai")
    assert before["next_refresh_at"] == "2026-10-15T08:00:00+08:00"
    market.trending("skills_sh", "Asia/Shanghai")
    assert market.calls == ["skills_sh"]
    market.test_clock.now += timedelta(minutes=1)
    after = market.trending("skills_sh", "Asia/Shanghai")
    assert market.calls == ["skills_sh", "skills_sh"]
    assert after["next_refresh_at"] == "2026-10-16T08:00:00+08:00"


@pytest.mark.parametrize("at,expected", [
    ("2026-03-08T11:59:00+00:00", "2026-03-07T08:00:00-05:00"),
    ("2026-03-08T12:00:00+00:00", "2026-03-08T08:00:00-04:00"),
    ("2026-11-01T13:00:00+00:00", "2026-11-01T08:00:00-05:00"),
])
def test_refresh_boundary_tracks_user_daylight_saving_time(at, expected):
    assert refresh_boundary(datetime.fromisoformat(at), "America/New_York").isoformat() == expected


def test_manual_refresh_and_startup_catch_up_both_sources(market):
    market.trending("skills_sh", "Asia/Shanghai")
    market.trending("skills_sh", "Asia/Shanghai", True)
    assert market.calls == ["skills_sh", "skills_sh"]
    market.test_clock.now += timedelta(days=2)
    restarted = SkillMarketService(market.home, clock=lambda: market.test_clock.now)
    restarted.refresh_due()
    assert market.calls[-2:] == ["skills_sh", "skillhub_cn"]
    assert restarted.trending("skillhub_cn")["timezone"] == "Asia/Shanghai"


def test_failed_refresh_retains_last_good_data_with_notice(market, monkeypatch):
    first = market.trending("skills_sh", "Asia/Shanghai")
    path = market.root / "skills_sh.json"
    original = path.read_bytes()
    monkeypatch.setattr(catalog, "fetch_trending", lambda *_a, **_k: (_ for _ in ()).throw(ValueError("changed HTML")))
    market.test_clock.now += timedelta(days=1)
    stale = market.trending("skills_sh")
    assert stale["stale"] and stale["warning"]
    assert stale["items"] == first["items"] and stale["updated_at"] == first["updated_at"]
    assert path.read_bytes() == original


def test_no_cache_failure_returns_retryable_error_and_backoff(market, monkeypatch):
    calls = []
    def fail(*_args, **_kwargs):
        calls.append(1)
        raise httpx.ConnectError("offline")
    monkeypatch.setattr(catalog, "fetch_trending", fail)
    for _ in range(2):
        with pytest.raises(ApplicationError) as error:
            market.trending("skills_sh", "UTC")
        assert error.value.status == 503
    assert len(calls) == 1


@pytest.mark.parametrize("zone", ["../../secret", "Invalid/Timezone", {"zone": "UTC"}])
def test_invalid_timezone_is_rejected(market, zone):
    with pytest.raises(ApplicationError, match="时区"):
        market.trending("skills_sh", zone)


def test_skillhub_resolution_qualifies_owner_and_pins_version(monkeypatch):
    calls = []
    async def get(url, **kwargs):
        calls.append(url)
        return {"namespace": {"handle": "publisher", "publicSlug": "office"}, "latestVersion": {"version": "2.0.0"}}
    monkeypatch.setattr(source_resolver, "_get_json_async", get)
    result = source_resolver.resolve_source("https://skillhub.cn/skills/publisher/office?version=1.2.0")
    assert result.type == "skillhub_cn"
    assert parse_qs(urlparse(result.fetch_url).query) == {"slug": ["office"], "namespace": ["publisher"], "version": ["1.2.0"]}
    assert "namespace=publisher" in calls[0]


def test_skillhub_wrong_owner_is_rejected(monkeypatch):
    async def get(*_args, **_kwargs):
        return {"namespace": {"handle": "another", "publicSlug": "office"}, "latestVersion": {"version": "1.0"}}
    monkeypatch.setattr(source_resolver, "_get_json_async", get)
    with pytest.raises(ValueError, match="不匹配"):
        source_resolver.resolve_source("https://skillhub.cn/skills/publisher/office")


@pytest.mark.parametrize("url", ["https://skillhub.cn/skills/a/%2e%2e", "https://skillhub.cn/skills/a/b/c", "https://skillhub.cn/skills/a%2fb/office"])
def test_skillhub_rejects_invalid_coordinates_before_network(url, monkeypatch):
    monkeypatch.setattr(source_resolver, "_run_http", lambda *_a, **_k: pytest.fail("invalid URL reached network"))
    with pytest.raises(ValueError):
        source_resolver.resolve_source(url)


def test_audits_use_chinese_labels_and_unknown_is_not_pass():
    result = audits.normalize_audits("example/skills/demo", {
        "ath": {"risk": "safe"}, "socket": {"risk": "safe", "alerts": 0}, "snyk": {"risk": "medium"},
    })
    assert [row["label"] for row in result["reports"]] == ["综合安全评估", "代码安全检查", "指令风险检查"]
    assert [row["status_text"] for row in result["reports"]] == ["通过", "0 项告警", "需留意"]
    assert result["reports"][2]["report_url"].endswith("/security/snyk")
    missing = audits.normalize_audits("example/skills/demo", {"socket": {"alerts": 0}})
    assert not missing["available"]
    assert all(row["status"] == "unknown" for row in missing["reports"])


@pytest.mark.parametrize("source,url", [
    ("skills_sh", "https://skills.sh/example/skills/demo"),
    ("skillhub_cn", "https://skillhub.cn/skills/publisher/office"),
])
def test_audit_failure_is_informational_not_green_or_blocked(monkeypatch, tmp_path, source, url):
    monkeypatch.setattr(audits, "get_json", lambda *_a, **_k: (_ for _ in ()).throw(httpx.ConnectError("offline")))
    result = review_skill_package(tmp_path, source=source, source_original=url)
    assert result["verdict"] == "unknown" and result["install_allowed"]
    assert result["audits"]["available"] is False


def test_skillhub_reports_keep_laboratory_names_source_status_and_signed_urls():
    payload = chinese_audit_payload()
    payload["securityReports"]["sanbu"]["statusText"] = "安全，无风险"
    result = audits.normalize_skillhub_audits(payload, "1.2.0")
    assert result["source"] == "skillhub.cn" and result["available"]
    assert [row["label"] for row in result["reports"]] == ["科恩实验室", "云鼎实验室"]
    assert all(row["status"] == "pass" and row["status_text"] == "安全，无风险" for row in result["reports"])
    assert result["reports"][1]["report_url"] == payload["securityReports"]["sanbu"]["reportUrl"]
    assert all(not row["analyzed_at"] for row in result["reports"])  # No report timestamp in the source.


@pytest.mark.parametrize("upstream,status,risk,text", [
    ("suspicious", "warn", "medium", "存在可疑风险"),
    ("malicious", "warn", "high", "存在安全风险"),
    ("scanning", "unknown", "unknown", "扫描中"),
    ("queued", "unknown", "unknown", "等待扫描"),
    ("failed", "unknown", "unknown", "扫描失败"),
    ("new-status", "unknown", "unknown", "暂无报告"),
])
def test_skillhub_statuses_do_not_turn_pending_or_failed_reports_green(upstream, status, risk, text):
    result = audits.normalize_skillhub_audits(chinese_audit_payload(upstream))
    row = result["reports"][1]
    assert (row["status"], row["risk"], row["status_text"]) == (status, risk, text)
    if status == "warn":
        assert "云鼎实验室提示需留意" in result["notice"]


@pytest.mark.parametrize("value", [None, [], "changed-format", {"keen": [], "sanbu": {"status": []}}])
def test_skillhub_missing_or_changed_reports_are_neutral(value):
    result = audits.normalize_skillhub_audits({"securityReports": value})
    assert not result["available"]
    assert all(row["status"] == "unknown" and not row["report_url"] for row in result["reports"])


@pytest.mark.parametrize("url", [
    "javascript:alert(1)", "http://tix.qq.com/report", "https://tix.qq.com.evil.test/report",
    "https://tix.qq.com@evil.test/report", "https://tix.qq.com/\\evil.test/report",
    "https://[invalid", "https://tix.qq.com/report\nnext",
])
def test_skillhub_only_links_to_the_provider_report_host(url):
    payload = chinese_audit_payload()
    payload["securityReports"]["keen"]["reportUrl"] = url
    row = audits.normalize_skillhub_audits(payload)["reports"][0]
    assert row["report_url"] == ""
    assert row["status"] == "pass"


@pytest.mark.parametrize("url", [
    "https://skillhub.cn/skills/publisher/office?version=1.2.0",
    "https://www.skillhub.cn/skills/office?namespace=publisher&version=1.2.0",
])
def test_skillhub_fetches_public_reports_and_caches_matching_version(url, monkeypatch):
    calls = []
    def get(endpoint, **kwargs):
        calls.append((endpoint, kwargs))
        return chinese_audit_payload()
    monkeypatch.setattr(audits, "get_json", get)
    result = audits.fetch_audits(url)
    assert audits.fetch_audits(url) == result and result["available"]
    assert len(calls) == 1
    assert calls[0][0] == "https://api.skillhub.cn/api/v1/skills/office"
    assert calls[0][1]["params"] == {"namespace": "publisher"}
    older = audits.fetch_audits(url.replace("1.2.0", "1.1.0"))
    assert len(calls) == 2 and not older["available"]
    assert "当前版本暂无审核报告" in older["notice"]
    assert all(not row["report_url"] for row in older["reports"])


def test_skillhub_rejects_wrong_publisher_and_retries_pending_reports_soon(monkeypatch):
    clock = SimpleNamespace(value=10)
    monkeypatch.setattr(audits, "time", SimpleNamespace(monotonic=lambda: clock.value))
    payload = chinese_audit_payload("scanning")
    monkeypatch.setattr(audits, "get_json", lambda *_a, **_k: payload)
    url = "https://skillhub.cn/skills/publisher/office"
    assert audits.fetch_audits(url)["reports"][1]["status_text"] == "扫描中"
    payload["securityReports"]["sanbu"]["status"] = "benign"
    clock.value += 61
    assert all(row["status"] == "pass" for row in audits.fetch_audits(url)["reports"])
    assert not audits.fetch_audits(url.replace("publisher", "another"))["available"]


def fake_archive(market, monkeypatch, *, chinese=False):
    archive = market.home / "download.zip"
    name = "办公助手" if chinese else "skill-0"
    folder = "" if chinese else "repo-main/skills/skill-0/"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr(folder + "SKILL.md", f"---\nname: {name}\ndescription: 文档处理助手\n---\n\n你是一个文档助手。请整理用户提供的文件。\n")
        output.writestr(folder + "scripts/example.py", "print('example')\n")
    monkeypatch.setattr(source_resolver, "_download_archive", lambda *_a, **_k: archive)
    async def detail(*_a, **_k):
        return {"namespace": {"handle": "publisher", "publicSlug": "office"}, "latestVersion": {"version": "1.2.0"}}
    monkeypatch.setattr(source_resolver, "_get_json_async", detail)


@pytest.mark.parametrize("status", ["benign", "malicious"])
def test_one_click_chinese_install_uses_shared_store_and_is_idempotent(market, monkeypatch, status):
    fake_archive(market, monkeypatch, chinese=True)
    monkeypatch.setattr(audits, "get_json", lambda *_a, **_k: chinese_audit_payload(status))
    monkeypatch.setattr(catalog, "fetch_skillhub_metadata", lambda *_a, **_k: chinese_audit_payload(status))
    monkeypatch.setattr(catalog, "fetch_detail", lambda *_a, **_k: {"content": "办公助手", "repository_url": None})
    market.trending("skillhub_cn", "Asia/Shanghai")
    detail = market.detail("skillhub_cn", "publisher/office")
    result = market.install("skillhub_cn", "publisher/office")
    assert result["success"] and result["name"] == "office"
    assert result["audits"] == detail["audits"]
    assert result["audits"]["reports"][1]["status"] == ("pass" if status == "benign" else "warn")
    root = get_enabled_skills_dir() / "office"
    assert "name: office" in (root / "SKILL.md").read_text(encoding="utf-8")
    assert (root / "scripts/example.py").is_file()
    assert (root / "install_audit.json").is_file()
    assert market.install("skillhub_cn", "publisher/office")["already_installed"]
    assert market.trending("skillhub_cn")["items"][0]["installed_name"] == "office"
    assert not list(get_skill_drafting_dir().glob("skill_drafting_*"))


def test_market_name_collision_preserves_existing_skill_and_cleans_draft(market, monkeypatch):
    original = "---\nname: office\ndescription: 本地办公技能\n---\n\n整理用户指定的本地文档。\n"
    skill_store.create_skill(name="office", skill_md=original, short_description="本地办公技能")
    root = get_enabled_skills_dir() / "office"
    before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    fake_archive(market, monkeypatch, chinese=True)
    market.trending("skillhub_cn", "UTC")
    with pytest.raises(ValueError, match="exists|存在"):
        market.install("skillhub_cn", "publisher/office")
    assert {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()} == before
    assert not list(get_skill_drafting_dir().glob("skill_drafting_*"))
    assert market.trending("skillhub_cn")["items"][0]["installed_name"] is None


def test_chat_and_market_preparation_share_external_audits(market, monkeypatch):
    fake_archive(market, monkeypatch)
    monkeypatch.setattr(audits, "get_json", lambda *_a, **_k: {
        "skill-0": {"ath": {"risk": "safe"}, "socket": {"risk": "safe", "alerts": 0}, "snyk": {"risk": "high"}},
    })
    market.trending("skills_sh", "UTC")
    prepared = install_service.install_prepare("https://skills.sh/example/skills/skill-0")
    assert prepared["requires_confirmation"] and prepared["verdict"] == "warning"
    web_detail = market.skills.draft_detail(prepared["drafting_id"])
    assert not web_detail["blocked"]
    assert prepared["audits"] == web_detail["review"]["audits"]
    result = market.skills.confirm(web_detail["drafting_id"], web_detail["confirmation_token"])
    assert result["success"]


def test_chat_skillhub_install_displays_the_same_laboratory_reports(market, monkeypatch):
    fake_archive(market, monkeypatch, chinese=True)
    monkeypatch.setattr(audits, "get_json", lambda *_a, **_k: chinese_audit_payload("suspicious"))
    prepared = install_service.install_prepare("https://skillhub.cn/skills/publisher/office?version=1.2.0")
    assert prepared["requires_confirmation"] and prepared["verdict"] == "warning"
    web_detail = market.skills.draft_detail(prepared["drafting_id"])
    assert not web_detail["blocked"]
    assert prepared["audits"] == web_detail["review"]["audits"]
    assert prepared["audits"]["reports"][1]["label"] == "云鼎实验室"
    market.skills.cancel(prepared["drafting_id"])


def test_unlisted_skill_never_reaches_installer(market, monkeypatch):
    market.trending("skills_sh", "UTC")
    monkeypatch.setattr(market.skills, "prepare", lambda *_a: pytest.fail("unlisted source installed"))
    with pytest.raises(ApplicationError, match="榜单"):
        market.install("skills_sh", "other/repo/demo")


def test_detail_source_failure_keeps_installable_metadata_and_audits(market, monkeypatch):
    market.trending("skills_sh", "UTC")
    monkeypatch.setattr(catalog, "fetch_detail", lambda *_a, **_k: (_ for _ in ()).throw(httpx.ConnectError("offline")))
    value = market.detail("skills_sh", "example/skills/skill-0")
    assert value["id"] == "example/skills/skill-0" and value["detail_warning"]
    assert not value["audits"]["available"]


def test_english_description_and_audits_are_requested_in_parallel(market, monkeypatch):
    market.trending("skills_sh", "UTC")
    barrier = threading.Barrier(2)
    def description(item, **_kwargs):
        barrier.wait(timeout=3)
        return {"content": "# Skill", "repository_url": None}
    def reports(url, **_kwargs):
        barrier.wait(timeout=3)
        return audits.normalize_audits("example/skills/skill-0", {"ath": {"risk": "safe"}})
    monkeypatch.setattr(catalog, "fetch_detail", description)
    monkeypatch.setattr(audits, "fetch_audits", reports)
    value = market.detail("skills_sh", "example/skills/skill-0")
    assert value["content"] == "# Skill" and value["audits"]["available"]


def test_english_reports_batch_and_coalesce_by_source_with_individual_cache_entries(monkeypatch):
    started, waiting, release = threading.Event(), threading.Event(), threading.Event()
    calls = []
    class ObservedFuture(Future):
        def result(self, timeout=None):
            waiting.set()
            return super().result(timeout)
    monkeypatch.setattr(audits, "Future", ObservedFuture)
    def get(url, **kwargs):
        calls.append(kwargs["params"])
        started.set()
        assert release.wait(timeout=3)
        return {"one": {"ath": {"risk": "safe"}}}
    monkeypatch.setattr(audits, "get_json", get)
    related = ["example/repo/one", "example/repo/two", "different/repo/one"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(audits.fetch_audits, "https://skills.sh/example/repo/one", related_ids=related)
        try:
            assert started.wait(timeout=2)
            second = pool.submit(audits.fetch_audits, "https://skills.sh/example/repo/two", related_ids=related)
            assert waiting.wait(timeout=2)
        finally:
            release.set()
        assert first.result()["available"]
        assert not second.result()["available"]
    assert calls == [{"source": "example/repo", "skills": "one,two"}]
    assert audits.fetch_audits("https://skills.sh/example/repo/one")["available"]
    assert not audits.fetch_audits("https://skills.sh/example/repo/two")["available"]
    assert len(calls) == 1 and not audits._batch_requests


@pytest.mark.parametrize("version", ["1.2.0", "2.0.0"])
def test_chinese_detail_reuses_one_metadata_request_for_content_and_versioned_reports(market, monkeypatch, version):
    market.trending("skillhub_cn", "UTC")
    calls = []
    payload = {**chinese_audit_payload(), "skill": {"summary_zh": "技能简介"}}
    payload["latestVersion"]["version"] = version
    def metadata(url, **kwargs):
        calls.append((url, kwargs))
        return payload
    def content(url, **kwargs):
        calls.append((url, kwargs))
        return "# 办公助手"
    monkeypatch.setattr(catalog, "get_json", metadata)
    monkeypatch.setattr(catalog, "get_text", content)
    monkeypatch.setattr(audits, "get_json", lambda *_a, **_k: pytest.fail("metadata was fetched twice"))
    value = market.detail("skillhub_cn", "publisher/office")
    assert value["content"] == "# 办公助手"
    assert value["description"] == "技能简介"
    assert len(calls) == 2 and calls[0][0].endswith("/office") and calls[1][0].endswith("/office/file")
    assert calls[1][1]["params"] == {"namespace": "publisher", "path": "SKILL.md", "version": "1.2.0"}
    assert value["audits"]["available"] is (version == "1.2.0")
    assert audits.fetch_audits("https://skillhub.cn/skills/publisher/office?version=1.2.0") == value["audits"]
    assert "securityReports" not in value


@pytest.mark.parametrize("failure", ["offline", "wrong_publisher"])
def test_chinese_metadata_failure_keeps_catalog_fields_and_neutral_reports(market, monkeypatch, failure):
    market.trending("skillhub_cn", "UTC")
    def metadata(*args, **kwargs):
        if failure == "offline":
            raise httpx.ConnectError("offline")
        payload = chinese_audit_payload()
        payload["namespace"]["handle"] = "another"
        return payload
    monkeypatch.setattr(catalog, "get_json", metadata)
    monkeypatch.setattr(catalog, "get_text", lambda *_a, **_k: pytest.fail("unvalidated content requested"))
    monkeypatch.setattr(audits, "get_json", lambda *_a, **_k: pytest.fail("failed metadata retried immediately"))
    value = market.detail("skillhub_cn", "publisher/office")
    assert value["id"] == "publisher/office" and value["detail_warning"]
    assert not value["audits"]["available"] and not value["content"]


@pytest.mark.parametrize("fails", [False, True])
def test_concurrent_detail_requests_share_work_and_release_waiters_on_failure(market, monkeypatch, fails):
    market.trending("skills_sh", "UTC")
    started, waiting, release = threading.Event(), threading.Event(), threading.Event()
    calls = []
    class ObservedFuture(Future):
        def result(self, timeout=None):
            waiting.set()
            return super().result(timeout)
    monkeypatch.setattr(market_module, "Future", ObservedFuture)
    value = {"content": "# Skill", "repository_url": None, "detail_warning": "",
             "audits": audits.normalize_audits("example/skills/skill-0", {})}
    def fetch(item):
        calls.append(item["id"])
        started.set()
        assert release.wait(timeout=3)
        if fails:
            raise RuntimeError("unexpected failure")
        return value
    monkeypatch.setattr(market, "_fetch_detail", fetch)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(market.detail, "skills_sh", "example/skills/skill-0")
        try:
            assert started.wait(timeout=3)
            second = pool.submit(market.detail, "skills_sh", "example/skills/skill-0")
            assert waiting.wait(timeout=3)
        finally:
            release.set()
        for result in (first, second):
            if fails:
                with pytest.raises(RuntimeError, match="unexpected failure"):
                    result.result(timeout=3)
            else:
                assert result.result(timeout=3)["content"] == "# Skill"
    assert len(calls) == 1 and not market._detail_requests
    if fails:
        monkeypatch.setattr(market, "_fetch_detail", lambda item: value)
    else:
        monkeypatch.setattr(market, "_fetch_detail", lambda item: pytest.fail("cached detail fetched again"))
    monkeypatch.setattr(market, "_installed", lambda: {"skills.sh/example/skills/skill-0": "local-skill"})
    assert market.detail("skills_sh", "example/skills/skill-0")["installed_name"] == "local-skill"


@pytest.mark.parametrize("pending,seconds", [(True, 60), (False, 900)])
def test_detail_cache_expires_promptly_for_unknown_reports(market, monkeypatch, pending, seconds):
    market.trending("skills_sh", "UTC")
    calls = []
    def fetch(item):
        calls.append(item["id"])
        reports = audits.normalize_audits(item["id"], {} if pending else {
            "ath": {"risk": "safe"}, "socket": {"risk": "safe"}, "snyk": {"risk": "safe"}})
        return {"content": "# Skill", "repository_url": None, "detail_warning": "", "audits": reports}
    monkeypatch.setattr(market, "_fetch_detail", fetch)
    market.detail("skills_sh", "example/skills/skill-0")
    market.test_clock.now += timedelta(seconds=seconds - 1)
    market.detail("skills_sh", "example/skills/skill-0")
    assert len(calls) == 1
    market.test_clock.now += timedelta(seconds=1)
    market.detail("skills_sh", "example/skills/skill-0")
    assert len(calls) == 2


def test_market_routes_require_auth_and_validate_install_identity(market):
    async def error(_request, exc):
        return JSONResponse({"error": {"code": exc.code, "message": str(exc)}}, status_code=exc.status)
    app = LocalAccess(Starlette(routes=build_skill_market_routes(market), exception_handlers={ApplicationError: error}), "test-token")
    with TestClient(app, base_url="http://127.0.0.1") as client:
        url = "/api/v1/skill-market/skills_sh/trending"
        assert client.get(url).status_code == 403
        headers = {"Authorization": "Bearer test-token"}
        response = client.get(url, headers=headers).json()
        assert response["limit"] == 40 and len(response["items"]) == 40
        assert client.post("/api/v1/skill-market/skills_sh/install", headers=headers, json={"id": {}}).status_code == 400
        assert client.get(url, headers={**headers, "Origin": "https://evil.test"}).status_code == 403
        assert client.get("/api/v1/skill-market/unknown/trending", headers=headers).status_code == 404


@pytest.mark.asyncio
async def test_background_refresh_lifecycle_stays_idle_until_market_used(market):
    await market.start()
    await asyncio.sleep(0)
    await market.close()
    assert market._ticker.done()
    assert market.calls == []


@pytest.mark.asyncio
async def test_market_uses_one_client_for_both_sources_and_closes_it_after_prefetch(market, monkeypatch):
    clients, requests, feed_clients = [], [], []
    def respond(request):
        requests.append(request)
        if request.url.path.endswith("/file"):
            return httpx.Response(200, text="# 办公助手")
        if request.url.host == "api.skillhub.cn":
            return httpx.Response(200, json=chinese_audit_payload())
        if request.url.path == "/tele/audit":
            return httpx.Response(200, json={"skill-0": {
                "ath": {"risk": "safe"}, "socket": {"risk": "safe"}, "snyk": {"risk": "safe"}}})
        return httpx.Response(200, text='<div class="prose"><h1>Ready</h1><p>Skill instructions</p></div>')
    def create(**kwargs):
        client = MarketplaceClient(transport=httpx.MockTransport(respond), **kwargs)
        client._before_request = lambda _request: None
        client.event_hooks["request"] = []
        clients.append(client)
        return client
    def trending(source, *, client):
        feed_clients.append(client)
        return catalog.normalize_trending(source, english_rows(1) if source == "skills_sh" else chinese_rows())
    monkeypatch.setattr(market_module, "MarketplaceClient", create)
    monkeypatch.setattr(catalog, "fetch_trending", trending)
    monkeypatch.setattr(audits, "get_json", catalog.get_json)
    await market.start()
    ticker = market._ticker
    try:
        await market.start()
        assert market._ticker is ticker
        for source in catalog.MARKETS:
            market.trending(source, "UTC")
        await finish_prefetch(market)
        assert len(clients) == 1 and all(client is clients[0] for client in feed_clients)
        assert not clients[0].is_closed
        for source, identifier in (("skills_sh", "example/skills/skill-0"), ("skillhub_cn", "publisher/office")):
            detail = market.detail(source, identifier)
            assert detail["content"] and detail["audits"]["available"]
        assert len(requests) == 4  # English page + audits; Chinese metadata + content.
        audit_request = next(request for request in requests if request.url.path == "/tele/audit")
        assert audit_request.extensions["timeout"]["read"] == 6
    finally:
        await market.close()
    assert clients[0].is_closed and market._http is None
    await market.close()


@pytest.mark.asyncio
async def test_shutdown_waits_for_running_catalog_read_before_closing_connections(market, monkeypatch):
    started, release = threading.Event(), threading.Event()
    def trending(source, *, client):
        started.set()
        assert release.wait(timeout=5)
        assert not client.is_closed
        return catalog.normalize_trending(source, english_rows(1) if source == "skills_sh" else chinese_rows())
    monkeypatch.setattr(catalog, "fetch_trending", trending)
    market._timezone = "UTC"
    await market.start()
    client = market._http
    try:
        assert await asyncio.to_thread(started.wait, 2)
        closing = asyncio.create_task(market.close())
        async with asyncio.timeout(2):
            while market._prefetch_pools:
                await asyncio.sleep(0.005)
        assert not closing.done() and not client.is_closed
        release.set()
        await closing
        assert client.is_closed and not market._prefetch_jobs
    finally:
        release.set()
        await market.close()


def cached_detail(item, content="# Ready"):
    reports = (audits.normalize_skillhub_audits(chinese_audit_payload()) if item["market"] == "skillhub_cn"
               else audits.normalize_audits(item["id"], {
                   "ath": {"risk": "safe"}, "socket": {"risk": "safe"}, "snyk": {"risk": "safe"}}))
    return {"content": content, "repository_url": None, "detail_warning": "", "audits": reports}


async def finish_prefetch(service):
    async def wait():
        while service._prefetch_jobs:
            await asyncio.sleep(0.005)
    await asyncio.wait_for(wait(), timeout=8)


@pytest.mark.asyncio
async def test_prefetch_is_nonblocking_bounded_and_keeps_both_top_forty_after_restart(market, monkeypatch):
    def trending(source, **_kwargs):
        return catalog.normalize_trending(source, english_rows() if source == "skills_sh" else chinese_ranked_rows(40))
    monkeypatch.setattr(catalog, "fetch_trending", trending)
    release = threading.Event()
    started = {source: threading.Event() for source in catalog.MARKETS}
    guard = threading.Lock()
    calls = []
    active = {source: 0 for source in catalog.MARKETS}
    def fetch(item):
        with guard:
            calls.append(item["id"])
            active[item["market"]] += 1
            if active[item["market"]] == market_module.DETAIL_PREFETCH_WORKERS:
                started[item["market"]].set()
        try:
            # A user opening an item still in the queue must not wait for earlier items.
            if item["id"] != "example/skills/skill-39":
                assert release.wait(timeout=5)
            return cached_detail(item)
        finally:
            with guard:
                active[item["market"]] -= 1
    monkeypatch.setattr(market, "_fetch_detail", fetch)
    await market.start()
    try:
        first = await asyncio.wait_for(asyncio.to_thread(market.trending, "skills_sh", "UTC"), timeout=1)
        assert await asyncio.to_thread(started["skills_sh"].wait, 3)
        assert len(first["items"]) == 40 and first["detail_cache"]["pending"] == 40
        second = market.trending("skillhub_cn", "UTC")
        assert second["detail_cache"]["pending"] == 40
        assert await asyncio.to_thread(started["skillhub_cn"].wait, 3)
        assert len(calls) == 4 and all(count == 2 for count in active.values())
        selected = await asyncio.wait_for(
            asyncio.to_thread(market.detail, "skills_sh", "example/skills/skill-39"), timeout=1)
        assert selected["content"] == "# Ready"
        release.set()
        await finish_prefetch(market)
        assert len(calls) == 80 and calls.count("example/skills/skill-39") == 1
        for source in catalog.MARKETS:
            assert market.trending(source)["detail_cache"] == {"ready": 40, "total": 40, "pending": 0}
        assert len(market._details) == 80
        persisted = json.loads((market.root / "details.json").read_text(encoding="utf-8"))
        assert len(persisted["entries"]) == 80
    finally:
        release.set()
        await market.close()
    restarted = SkillMarketService(market.home, clock=market.clock)
    monkeypatch.setattr(restarted, "_fetch_detail", lambda *_a: pytest.fail("valid persisted detail fetched again"))
    await restarted.start()
    try:
        for source in catalog.MARKETS:
            feed = restarted.trending(source, "UTC")
            assert feed["detail_cache"]["ready"] == 40
            assert restarted.detail(source, feed["items"][-1]["id"])["content"] == "# Ready"
        await finish_prefetch(restarted)
    finally:
        await restarted.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True])
async def test_expired_details_display_immediately_while_background_refreshes(market, monkeypatch, fails):
    market.trending("skills_sh", "UTC")
    monkeypatch.setattr(market, "_fetch_detail", lambda item: cached_detail(item, "# Saved"))
    original = market.detail("skills_sh", "example/skills/skill-0")
    market.test_clock.now += timedelta(minutes=16)
    market._timezone = ""  # Keep this test focused on an on-demand stale read.
    started, release = threading.Event(), threading.Event()
    def update(item):
        started.set()
        assert release.wait(timeout=3)
        if fails:
            return {**cached_detail(item, ""), "detail_warning": "来源暂不可用"}
        return cached_detail(item, "# Updated")
    monkeypatch.setattr(market, "_fetch_detail", update)
    await market.start()
    try:
        result = market.detail("skills_sh", "example/skills/skill-0")
        assert result["content"] == "# Saved" and result["detail_stale"] and result["detail_refreshing"]
        assert await asyncio.to_thread(started.wait, 2)
        release.set()
        await finish_prefetch(market)
        after = market.detail("skills_sh", "example/skills/skill-0")
        assert after["content"] == ("# Saved" if fails else "# Updated")
        assert not after["detail_refreshing"]
        assert after["detail_stale"] is fails
        assert (after["detail_updated_at"] == original["detail_updated_at"]) is fails
    finally:
        release.set()
        await market.close()


@pytest.mark.asyncio
async def test_manual_ranking_refresh_keeps_valid_detail_cache(market, monkeypatch):
    monkeypatch.setattr(market, "refresh_due", lambda: None)
    calls = []
    def fetch(item):
        calls.append(item["id"])
        return cached_detail(item)
    monkeypatch.setattr(market, "_fetch_detail", fetch)
    await market.start()
    try:
        market.trending("skills_sh", "UTC")
        await finish_prefetch(market)
        assert len(calls) == 40
        market.test_clock.now += timedelta(seconds=60)
        market.trending("skills_sh", "UTC", True)
        await finish_prefetch(market)
        assert len(calls) == 40
    finally:
        await market.close()


@pytest.mark.asyncio
async def test_cooldown_serves_cached_feed_and_returns_pending_details_without_foreground_wait(market, monkeypatch):
    market.trending("skills_sh", "UTC")
    monkeypatch.setattr(market, "_fetch_detail", cached_detail)
    saved = market.detail("skills_sh", "example/skills/skill-0")
    release = threading.Event()
    def blocked(item):
        assert release.wait(timeout=5)
        return cached_detail(item)
    monkeypatch.setattr(market, "_fetch_detail", blocked)
    await market.start()
    retry = market.clock() + timedelta(seconds=60)
    monkeypatch.setattr(market._http, "retry_at", lambda url: retry if "skills.sh" in url else None)
    try:
        feed = market.trending("skills_sh", "UTC", True)
        assert market.calls.count("skills_sh") == 1
        assert "限流" in feed["warning"] and feed["detail_cache"]["retry_at"] == retry.isoformat()
        assert market.detail("skills_sh", "example/skills/skill-0")["content"] == saved["content"]
        pending = await asyncio.wait_for(
            asyncio.to_thread(market.detail, "skills_sh", "example/skills/skill-1"), timeout=1)
        assert pending["detail_refreshing"] and "限流" in pending["detail_warning"]
        assert not pending["audits"]["available"]
    finally:
        release.set()
        await market.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("scheduled", [False, True])
async def test_refresh_warms_new_catalog_once_and_shutdown_cancels_queued_items(market, monkeypatch, scheduled):
    monkeypatch.setattr(market, "_fetch_detail", cached_detail)
    await market.start()
    try:
        market.trending("skills_sh", "UTC")
        await finish_prefetch(market)
        # A normal read reuses the completed batch; a newly refreshed catalog starts a new one.
        market.trending("skills_sh", "UTC")
        assert not market._prefetch_jobs
        market.test_clock.now += timedelta(days=1) if scheduled else timedelta(minutes=16)
        release, four_started = threading.Event(), threading.Event()
        calls = []
        expected_running = 3 if scheduled else 2
        def blocked(item):
            calls.append(item["id"])
            if len(calls) == expected_running:
                four_started.set()
            assert release.wait(timeout=5)
            return cached_detail(item)
        monkeypatch.setattr(market, "_fetch_detail", blocked)
        if scheduled:
            market.refresh_due()
        else:
            market.trending("skills_sh", "UTC", True)
        assert await asyncio.to_thread(four_started.wait, 3)
        jobs = list(market._prefetch_jobs.values())
        closing = asyncio.create_task(market.close())
        try:
            async def cancelled():
                while not any(job.cancelled() for job in jobs):
                    await asyncio.sleep(0.005)
            await asyncio.wait_for(cancelled(), timeout=3)
            assert len(calls) == expected_running
        finally:
            release.set()
            await closing
        assert not market._prefetch_jobs and not market._prefetch_pools
    finally:
        await market.close()


def test_persisted_cache_is_version_specific_and_recovers_from_bad_files(market, monkeypatch):
    market.trending("skillhub_cn", "UTC")
    calls = []
    def fetch(item):
        calls.append(item["version"])
        return cached_detail(item, item["version"])
    monkeypatch.setattr(market, "_fetch_detail", fetch)
    item = market._item("skillhub_cn", "publisher/office")
    assert market._cached_detail(item)["content"] == "1.2.0"
    assert market._cached_detail({**item, "version": "2.0.0"})["content"] == "2.0.0"
    assert calls == ["1.2.0", "2.0.0"]
    path = market.root / "details.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["entries"]["https://skills.sh/example/skills/broken?"] = {"expires_at": "bad"}
    path.write_text(json.dumps(data), encoding="utf-8")
    assert len(SkillMarketService(market.home, clock=market.clock)._details) == 2
    path.write_text("{broken", encoding="utf-8")
    assert SkillMarketService(market.home, clock=market.clock)._details == {}


def test_failed_detail_cache_write_keeps_in_memory_result(market, monkeypatch):
    market.trending("skills_sh", "UTC")
    monkeypatch.setattr(market, "_fetch_detail", cached_detail)
    monkeypatch.setattr(market_module, "atomic_json_write", lambda *_a, **_k: (_ for _ in ()).throw(OSError("disk full")))
    assert market.detail("skills_sh", "example/skills/skill-0")["content"] == "# Ready"
    assert len(market._details) == 1


def test_detail_cache_capacity_keeps_recently_used_current_skills(market, monkeypatch):
    market.trending("skills_sh", "UTC")
    item = market._item("skills_sh", "example/skills/skill-0")
    monkeypatch.setattr(market, "_fetch_detail", cached_detail)
    monkeypatch.setattr(market, "_save_detail_cache", lambda: None)
    for index in range(market_module.DETAIL_CACHE_LIMIT):
        market._cached_detail({**item, "version": str(index)})
    first = {**item, "version": "0"}
    market._cached_detail(first)
    market._cached_detail({**item, "version": "new"})
    assert len(market._details) == market_module.DETAIL_CACHE_LIMIT
    assert market._detail_key(first) in market._details
    assert market._detail_key({**item, "version": "1"}) not in market._details


def test_well_known_skill_keeps_its_place_in_the_top_forty():
    rows = english_rows(45)
    rows[12] = {"source": "uizze.sh", "skillId": "ui-taste", "name": "ui-taste", "installs": 20000}
    items = catalog.normalize_trending("skills_sh", rows)
    assert len(items) == 40 and items[-1]["rank"] == 40
    assert items[12]["url"] == "https://skills.sh/uizze.sh/ui-taste"


@pytest.mark.parametrize("invalid", [False, True])
def test_well_known_materialization_includes_support_files_and_rejects_traversal(tmp_path, monkeypatch, invalid):
    calls = []
    def respond(request):
        calls.append(str(request.url))
        if '/agent-skills/' in request.url.path:
            return httpx.Response(404)
        if request.url.path.endswith('/index.json'):
            return httpx.Response(200, json={"skills": [{"name": "demo", "files": ["SKILL.md", "../outside" if invalid else "references/guide.md"]}]})
        return httpx.Response(200, text="example content")
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(**kwargs, transport=httpx.MockTransport(respond)))
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_a, **_k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))])
    resolved = source_resolver.resolve_source("https://skills.sh/public.example/demo")
    target = tmp_path / "package"
    if invalid:
        with pytest.raises(ValueError, match="路径"):
            source_resolver.materialize_source(resolved, target)
        assert not (tmp_path / "outside").exists()
        assert len(calls) == 2
    else:
        source_resolver.materialize_source(resolved, target)
        assert (target / "SKILL.md").is_file()
        assert (target / "references/guide.md").read_text() == "example content"
        assert all(url.startswith("https://public.example/.well-known/") for url in calls)


def test_well_known_rejects_private_network_hosts(tmp_path, monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_a, **_k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))])
    resolved = source_resolver.resolve_source("https://skills.sh/private.example/demo")
    with pytest.raises(ValueError, match="公开网站"):
        source_resolver.materialize_source(resolved, tmp_path / "package")
