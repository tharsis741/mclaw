# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Production Web UI, real custom-provider tasks, isolated application data."""

import copy
import json
import os
import time
from pathlib import Path

import httpx
import pytest

from mclaw.application.task_notifications import first_reply_line
from mclaw.cli.config import save_config
from mclaw.web.launcher import ensure_service, running_service
from test_skill_notifications_live import report
from test_skill_review_live import live

pytestmark = pytest.mark.skipif(os.environ.get("MCLAW_RUN_LIVE_AGENT_TESTS") != "1",
                               reason="Requires real configured-provider requests and Chrome")


def test_live_browser_global_banner_refresh_focus_and_result(live):
    playwright = pytest.importorskip("playwright.sync_api")
    _, trace, home = live
    config = copy.deepcopy(trace.config)
    config.update(active_provider="custom", model=trace.context.model,
                  channels={"weixin": {"enabled": False}, "dingtalk": {"enabled": False}})
    config["skills"] = {**config.get("skills", {}), "evolution_review_round": 0}
    config["memory"] = {"memory_enabled": False, "user_profile_enabled": False, "memory_review_round": 0}
    config["checkpoints"] = {"enabled": False}
    config["compression"] = {"enabled": False}
    workspace = home / "_temp"
    workspace.mkdir()
    config["security"] = {**config.get("security", {}), "trusted_workspaces": [str(workspace)]}
    save_config(config)
    server = ensure_service(home)
    base = f"http://127.0.0.1:{server['port']}"
    client = httpx.Client(base_url=base, headers={"Authorization": f"Bearer {server['token']}"}, trust_env=False)
    directory = Path(os.environ.get("MCLAW_LIVE_REPORT_DIR", home))
    directory.mkdir(parents=True, exist_ok=True)
    errors = []
    try:
        with playwright.sync_playwright() as p:
            headed = os.environ.get("MCLAW_BROWSER_HEADED") == "1"
            browser = p.chromium.launch(channel="chrome", headless=not headed)
            context = browser.new_context(viewport={"width": 1280, "height": 820})
            # Keep connection credentials out of navigation URLs, screenshots and reports.
            context.add_init_script("sessionStorage.setItem('mclaw.connection.token', " + json.dumps(server["token"]) + ");")
            page = context.new_page()
            if headed:
                # Playwright enables focus emulation for every page, including
                # headed Chrome. Disable it to exercise actual tab visibility.
                context.new_cdp_session(page).send("Emulation.setFocusEmulationEnabled", {"enabled": False})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(base)
            page.bring_to_front()
            def wait_until(predicate, timeout=30):
                deadline = time.monotonic() + timeout
                while not predicate():
                    if time.monotonic() >= deadline:
                        pytest.fail("Browser completion condition was not reached")
                    page.wait_for_timeout(100)

            def feed():
                response = client.get("/api/v1/task-notifications")
                response.raise_for_status()
                return response.json()
            draft = page.get_by_role("textbox", name="描述你的任务")
            draft.wait_for(timeout=30000)
            prompt = "这是一次性界面验收，不调用工具，不保存技能或经验。只回复两行：\n**浏览器验收完成：结果已核对。**\n这是完整回复的第二行。"
            draft.fill(prompt)
            with page.expect_response(lambda response: response.url.endswith("/messages") and response.request.method == "POST") as submitted:
                page.get_by_role("button", name="发送", exact=True).click()
            session_id = submitted.value.json()["id"]
            # The UI allocates a per-session child directory. Trusting its parent
            # in fixture configuration does not authorize this exact workspace.
            page.get_by_role("button", name="信任并继续", exact=True).click()
            print("browser: workspace authorized, waiting for real completion", flush=True)
            page.locator(".main-nav").get_by_role("button", name="技能", exact=True).click()
            banner = page.get_by_role("region", name="任务完成通知")
            banner.wait_for(timeout=120000)
            record = client.get("/api/v1/task-notifications").json()["notifications"][0]
            controller = page.evaluate("sessionStorage.getItem('mclaw.connection.client')")
            snapshot = client.get(f"/api/v1/sessions/{session_id}", headers={"X-MClaw-Client": controller}).json()
            final = next(item for item in snapshot["messages"] if item["id"] == record["response_id"])
            assert record["preview"] == first_reply_line(final["content"])
            assert banner.locator(".task-completion-preview").inner_text() == record["preview"]
            # Wait for the short entrance animation before asserting placement.
            playwright.expect(banner).to_have_css("transform", "matrix(1, 0, 0, 1, -260, 0)")
            desktop = banner.bounding_box()
            assert desktop["width"] == pytest.approx(520, abs=0.05)
            assert desktop["height"] == pytest.approx(60, abs=0.05)
            assert abs(banner.bounding_box()["y"] - 88) < 1
            page.screenshot(path=str(directory / "banner-on-skills.png"))
            page.set_viewport_size({"width": 390, "height": 844})
            mobile = banner.bounding_box()
            assert mobile["width"] <= 366.05 and mobile["height"] == pytest.approx(60, abs=0.05)
            page.screenshot(path=str(directory / "banner-mobile.png"))
            page.set_viewport_size({"width": 1280, "height": 820})
            page.reload()
            banner.wait_for(timeout=30000)
            assert banner.locator(".task-completion-preview").inner_text() == record["preview"]
            page.screenshot(path=str(directory / "banner-after-refresh.png"))
            banner.get_by_role("button", name="关闭任务完成通知").click()
            playwright.expect(banner).to_have_count(0)
            closed = client.get("/api/v1/task-notifications").json()
            assert not closed["notifications"] and session_id in closed["unread_session_ids"]
            with page.expect_response(lambda response: response.url.endswith("/task-notifications")):
                page.reload()
            playwright.expect(page.get_by_role("region", name="任务完成通知")).to_have_count(0)
            page.locator(".session-link").filter(has_text=snapshot["title"]).click()
            draft.wait_for()
            wait_until(lambda: not feed()["unread_session_ids"])
            # Inject a delivery failure only for read receipts. The task still
            # uses the production Agent and the real configured model.
            block_reads = True
            failed_reads = []
            def receipts(route):
                if route.request.method == "POST" and block_reads and route.request.post_data_json.get("action") == "read":
                    failed_reads.append(route.request.post_data_json)
                    route.abort("failed")
                else:
                    route.continue_()
            page.route("**/api/v1/task-notifications", receipts)
            page.evaluate("""() => {
                window.completionBannerFlashes = 0;
                new MutationObserver(() => {
                    if (document.querySelector('.task-completion-banner')) window.completionBannerFlashes++;
                }).observe(document.body, {childList: true, subtree: true});
            }""")
            draft.fill(prompt.replace("浏览器验收", "当前会话验收"))
            with page.expect_response(lambda response: response.url.endswith("/messages") and response.request.method == "POST"):
                page.get_by_role("button", name="发送", exact=True).click()
            draft.fill("这段草稿应保留")
            draft.focus()
            page.get_by_text("当前会话验收完成：结果已核对。", exact=True).wait_for(timeout=120000)
            wait_until(lambda: bool(failed_reads))
            playwright.expect(banner).to_have_count(0)
            assert page.evaluate("window.completionBannerFlashes") == 0
            playwright.expect(draft).to_be_focused()
            playwright.expect(draft).to_have_value("这段草稿应保留")
            second_record = feed()["notifications"][0]
            page.screenshot(path=str(directory / "current-session-no-banner.png"))
            page.locator(".sidebar-bottom").get_by_role("button", name="设置", exact=True).click()
            page.wait_for_timeout(1700)
            playwright.expect(banner).to_have_count(0)
            assert page.evaluate("window.completionBannerFlashes") == 0
            assert second_record["id"] in page.evaluate("JSON.parse(sessionStorage.getItem('mclaw.task-notification-reads.v1'))")
            attempts_before_reload = len(failed_reads)
            page.reload()
            draft.wait_for()
            wait_until(lambda: len(failed_reads) > attempts_before_reload)
            playwright.expect(banner).to_have_count(0)
            assert feed()["pending_count"] == 1
            block_reads = False
            page.evaluate("window.dispatchEvent(new Event('online'))")
            wait_until(lambda: not feed()["unread_session_ids"])
            assert feed()["pending_count"] == 0
            page.unroute("**/api/v1/task-notifications", receipts)
            print("browser: current completion stays quiet across failed receipt, navigation and reload", flush=True)

            # A real idle session lets us verify completion while viewing a
            # different conversation; no assistant reply is manufactured.
            other_response = client.post("/api/v1/sessions", json={"workspace": str(workspace), "model": f"custom::{trace.context.model}"}, headers={"X-MClaw-Client": controller})
            other_response.raise_for_status()
            other = other_response.json()
            renamed = client.patch(f"/api/v1/sessions/{other['id']}", json={"title": "通知验收：另一会话"}, headers={"X-MClaw-Client": controller})
            renamed.raise_for_status()
            page.reload()
            page.locator(".session-link").filter(has_text=snapshot["title"]).click()
            draft.wait_for()
            draft.fill(prompt.replace("浏览器验收", "其他会话验收"))
            page.get_by_role("button", name="发送", exact=True).click()
            page.locator(".session-link").filter(has_text="通知验收：另一会话").click()
            banner.wait_for(timeout=120000)
            playwright.expect(page.get_by_role("heading", name="通知验收：另一会话", exact=True)).to_be_visible()
            third_record = feed()["notifications"][0]
            assert third_record["session_id"] == session_id
            assert "其他会话验收" in third_record["preview"]
            page.screenshot(path=str(directory / "other-session-banner.png"))
            second = context.new_page()
            if headed:
                context.new_cdp_session(second).send("Emulation.setFocusEmulationEnabled", {"enabled": False})
            second.goto(base)
            second.bring_to_front()
            second_banner = second.get_by_role("region", name="任务完成通知")
            second_banner.wait_for(timeout=30000)
            # Actual OS tab focus/visibility is checked in headed mode.
            if headed:
                playwright.expect(banner).to_have_count(0)
            second_banner.get_by_role("button", name="关闭任务完成通知").click()
            playwright.expect(second_banner).to_have_count(0)
            playwright.expect(banner).to_have_count(0)
            second.close()
            page.bring_to_front()
            with page.expect_response(lambda response: response.url.endswith("/task-notifications")):
                page.evaluate("window.dispatchEvent(new Event('focus'))")
            playwright.expect(banner).to_have_count(0)
            # One more real completion exercises the actual jump to its reply.
            page.locator(".session-link").filter(has_text=snapshot["title"]).click()
            draft.wait_for()
            wait_until(lambda: not feed()["unread_session_ids"])
            draft.fill(prompt.replace("浏览器验收", "结果定位验收"))
            page.get_by_role("button", name="发送", exact=True).click()
            page.locator(".sidebar-bottom").get_by_role("button", name="设置", exact=True).click()
            banner.wait_for(timeout=120000)
            fourth_record = feed()["notifications"][0]
            banner.get_by_role("button", name="查看结果").click()
            target = page.locator(f'[data-message-id="{fourth_record["response_id"]}"]')
            playwright.expect(target).to_be_in_viewport()
            playwright.expect(banner).to_have_count(0)
            wait_until(lambda: not feed()["unread_session_ids"])
            page.screenshot(path=str(directory / "banner-view-result.png"))
            assert not errors
            count = sum("[MODEL REQUEST]" in line for path in (home / "logs" / "sessions").glob("*.log")
                        for line in path.read_text(encoding="utf-8").splitlines())
            assert count >= 4
            report("browser-task-notifications.json", {"browser": browser.version, "desktop": desktop, "mobile": mobile,
                "notifications": [record, second_record, third_record, fourth_record], "worker_model_requests": count,
                "model": trace.context.model, "refresh_pending": True, "dismiss_persisted": True,
                "unread_persisted": True, "focus_preserved": True, "cross_window_dismiss": True,
                "current_session_suppressed": True, "no_banner_flash": True, "no_reannouncement_after_leaving": True,
                "failed_read_receipt_reload_recovered": True, "controlled_failed_receipt_count": len(failed_reads),
                "other_conversation_notified": True, "result_located": True, "headed_foreground_checked": headed, "page_errors": errors})
            browser.close()
    finally:
        client.post("/api/v1/service/stop", json={})
        client.close()
        deadline = time.monotonic() + 25
        while running_service(home) and time.monotonic() < deadline:
            time.sleep(0.1)
