# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Browser contracts exercised against real, local Chromium pages."""

from __future__ import annotations

import re
import struct
from pathlib import Path

import pytest

from mclaw.tools.browser_backend import BrowserBackend
from mclaw.tools.browser_requirements import check_browser_requirements


pytestmark = pytest.mark.skipif(
    not check_browser_requirements(), reason="Playwright Chromium is not installed",
)


@pytest.fixture(scope="module")
def backend():
    instance = BrowserBackend()
    yield instance
    instance.stop()


@pytest.fixture
def browser(backend, request):
    session_id = request.node.name
    yield backend, session_id
    backend.close_session(session_id)


def load(browser, tmp_path, html: str):
    backend, session_id = browser
    path = tmp_path / "page.html"
    path.write_text(html, encoding="utf-8")
    nav, snap = backend.navigate_and_snapshot(session_id, path.as_uri())
    assert "error" not in nav
    assert snap["success"], snap
    return snap


def ref(snap, label: str, role: str = r"\S+") -> str:
    match = re.search(rf"\[(e\d+)\] {role} '{re.escape(label)}'", snap["snapshot"])
    assert match, snap
    return match[1]


def evaluate(browser, script):
    backend, session_id = browser
    return backend._run_on_worker(lambda: backend._sessions[session_id].page.evaluate(script))


def test_old_and_cloned_element_refs_never_click_replacement(browser, tmp_path):
    backend, sid = browser
    first = load(browser, tmp_path, '<button id="run" onclick="window.count=(window.count||0)+1">Run</button>')
    old_ref = ref(first, "Run")
    second = backend.snapshot(sid)
    assert old_ref != ref(second, "Run")
    stale = backend.click(sid, old_ref)
    assert stale["error_code"] == "STALE_REF"
    assert stale["action_completed"] is False
    live_ref = ref(stale, "Run")
    evaluate(browser, "run.replaceWith(run.cloneNode(true))")
    replaced = backend.click(sid, live_ref)
    assert replaced["error_code"] == "STALE_REF"
    assert evaluate(browser, "window.count || 0") == 0
    assert backend.click(sid, ref(replaced, "Run"))["success"]
    assert evaluate(browser, "window.count") == 1


def test_node_replaced_during_snapshot_cannot_inherit_issued_reference(browser, tmp_path):
    backend, sid = browser
    snap = load(browser, tmp_path, """
        <button id="run" onclick="window.clicked='original'">Original</button>
        <script>
        const observer = new MutationObserver(() => {
            observer.disconnect();
            const replacement = run.cloneNode(true);
            replacement.textContent = 'Replacement';
            replacement.onclick = () => window.clicked = 'replacement';
            run.replaceWith(replacement);
        });
        observer.observe(document, {subtree:true, attributes:true, attributeFilter:['data-mclaw-ref']});
        </script>
    """)
    result = backend.click(sid, ref(snap, "Original"))
    assert result.get("error_code") == "STALE_REF", result
    assert evaluate(browser, "window.clicked || ''") == ""


def test_snapshot_reads_iframe_and_open_shadow_root(browser, tmp_path):
    backend, sid = browser
    child = tmp_path / "child.html"
    child.write_text('<button onclick="this.textContent=\'Frame done\'">Frame action</button>', encoding="utf-8")
    snap = load(browser, tmp_path, f"""
        <iframe src="{child.as_uri()}"></iframe><div id="host"></div>
        <input type="password" aria-label="Password" value="never-show-this">
        <script>
        const button = document.createElement('button');
        button.textContent = 'Shadow action';
        button.onclick = () => button.textContent = 'Shadow done';
        host.attachShadow({{mode:'open'}}).append(button);
        </script>
    """)
    assert "never-show-this" not in snap["snapshot"]
    assert "frame: 1" in snap["snapshot"]
    result = backend.click(sid, ref(snap, "Frame action"))
    assert result["success"] and "Frame done" in result["snapshot"]
    result = backend.click(sid, ref(result, "Shadow action"))
    assert result["success"] and "Shadow done" in result["snapshot"]


def test_select_check_and_hover_report_actual_form_state(browser, tmp_path):
    backend, sid = browser
    snap = load(browser, tmp_path, """
        <select aria-label="Color" multiple>
          <option value="red">Red</option><option value="blue">Blue</option>
        </select>
        <input aria-label="Agree" type="checkbox" onchange="window.changes=(window.changes||0)+1">
        <button disabled>Unavailable</button>
        <style>#menu {display:none} #trigger:hover + #menu {display:block}</style>
        <button id="trigger">Menu</button><button id="menu">Revealed</button>
    """)
    assert "[disabled]" in snap["snapshot"]
    assert "Revealed" not in snap["snapshot"]
    selected = backend.select(sid, ref(snap, "Color"), ["red", "blue"])
    assert selected["success"] and "'blue'='Blue'*" in selected["snapshot"]
    checked = backend.check(sid, ref(selected, "Agree"), True)
    assert checked["success"] and "[checked=true]" in checked["snapshot"]
    checked_again = backend.check(sid, ref(checked, "Agree"), True)
    assert checked_again["success"] and evaluate(browser, "window.changes") == 1
    hovered = backend.hover(sid, ref(checked_again, "Menu"))
    assert hovered["success"] and "Revealed" in hovered["snapshot"]


def test_popup_tabs_switch_and_close_without_reusing_refs(browser, tmp_path):
    backend, sid = browser
    child = tmp_path / "popup.html"
    child.write_text('<title>Popup</title><button>Child</button>', encoding="utf-8")
    snap = load(browser, tmp_path, f'<a href="{child.as_uri()}" target="_blank">Open popup</a>')
    opened = backend.click(sid, ref(snap, "Open popup"))
    assert opened["success"] and opened["tab_id"] == "t1"
    assert len(opened["tabs"]) == 2
    original_ref = ref(opened, "Open popup")
    assert backend.tabs(sid)["tabs"] == opened["tabs"]
    selected = backend.tabs(sid, "select", "t2")
    assert selected["success"] and selected["title"] == "Popup"
    assert backend.click(sid, original_ref)["error_code"] == "STALE_REF"
    restored = backend.tabs(sid, "close")
    assert restored["success"] and restored["tab_id"] == "t1"
    assert len(restored["tabs"]) == 1
    blank = backend.tabs(sid, "close")
    assert blank["success"] and blank["url"] == "about:blank"
    assert blank["tab_id"] != "t1"
    assert backend.tabs(sid, "select", "t2")["error_code"] == "TAB_NOT_FOUND"


def test_new_tab_and_back_preserve_session_history(browser, tmp_path):
    backend, sid = browser
    first = load(browser, tmp_path, '<title>First</title><p>First page</p>')
    assert backend.tabs(sid, "new")["tab_id"] == "t2"
    second = tmp_path / "second.html"
    second.write_text('<title>Second</title>', encoding="utf-8")
    backend.navigate(sid, first["url"])
    backend.navigate(sid, second.as_uri())
    back = backend.back(sid)
    assert back["success"] and back["title"] == "First" and back["tab_id"] == "t2"
    assert backend.tabs(sid, "select", "t1")["title"] == "First"


def test_dialog_policy_is_one_shot_and_preserves_refs(browser, tmp_path):
    backend, sid = browser
    snap = load(browser, tmp_path, """
        <button onclick="answer.textContent=confirm('Continue?')">Confirm</button>
        <button onclick="answer.textContent=prompt('Name?')">Prompt</button>
        <p id="answer">Waiting</p>
    """)
    dismissed = backend.click(sid, ref(snap, "Confirm"))
    assert dismissed["success"] and evaluate(browser, "answer.textContent") == "false"
    assert dismissed["dialogs"][0]["accepted"] is False
    assert backend.handle_dialog(sid, True)["armed"]
    accepted = backend.click(sid, ref(dismissed, "Confirm"))
    assert accepted["success"] and evaluate(browser, "answer.textContent") == "true"
    assert accepted["dialogs"][0]["configured"] is True
    default_again = backend.click(sid, ref(accepted, "Confirm"))
    assert default_again["success"] and evaluate(browser, "answer.textContent") == "false"
    backend.handle_dialog(sid, True, "Alice")
    answered = backend.click(sid, ref(default_again, "Prompt"))
    assert answered["success"] and evaluate(browser, "answer.textContent") == "Alice"


def test_dialog_policy_does_not_leak_between_tabs(browser, tmp_path):
    backend, sid = browser
    first = load(browser, tmp_path, '<button onclick="window.answer=confirm(\'Confirm\')">Confirm</button>')
    backend.handle_dialog(sid, True)
    backend.tabs(sid, "new")
    _, second = backend.navigate_and_snapshot(sid, first["url"])
    result = backend.click(sid, ref(second, "Confirm"))
    assert result["success"] and evaluate(browser, "window.answer") is False
    restored = backend.tabs(sid, "select", "t1")
    result = backend.click(sid, ref(restored, "Confirm"))
    assert result["success"] and evaluate(browser, "window.answer") is True


def test_upload_by_ref_and_hidden_file_chooser(browser, tmp_path):
    backend, sid = browser
    snap = load(browser, tmp_path, """
        <input id="visible" type="file" aria-label="File">
        <input id="hidden" type="file" multiple style="display:none">
        <button onclick="document.getElementById('hidden').click()">Choose files</button>
    """)
    upload = tmp_path / "upload.txt"
    upload.write_text("hello upload", encoding="utf-8")
    result = backend.upload(sid, [str(upload)], ref(snap, "File"))
    assert result["success"]
    assert evaluate(browser, "visible.files[0].text()") == "hello upload"
    chooser = backend.click(sid, ref(result, "Choose files"))
    assert chooser["success"] and chooser["file_chooser"]["multiple"] is True
    result = backend.upload(sid, [str(upload)])
    assert result["success"] and "file_chooser" not in result
    assert evaluate(browser, "hidden.files[0].name") == "upload.txt"
    assert backend.upload(sid, [str(upload)])["error_code"] == "NO_FILE_CHOOSER"
    assert backend.upload(sid, [], ref(result, "File"))["success"]
    assert evaluate(browser, "visible.files.length") == 0


def test_wait_for_text_disappearance_and_timeout(browser, tmp_path):
    backend, sid = browser
    load(browser, tmp_path, '<button id="loading">Loading</button><p id="status">Waiting</p>')
    evaluate(browser, "setTimeout(() => { document.getElementById('status').textContent='Ready'; }, 250)")
    ready = backend.wait_for(sid, text="Ready", timeout_ms=2000)
    assert ready["success"] and ready["condition_met"]
    loading_ref = ref(ready, "Loading")
    evaluate(browser, "setTimeout(() => loading.remove(), 250)")
    hidden = backend.wait_for(sid, ref=loading_ref, state="hidden", timeout_ms=2000)
    assert hidden["success"] and "Loading" not in hidden["snapshot"]
    timeout = backend.wait_for(sid, text="Never rendered", timeout_ms=100)
    assert not timeout["success"] and timeout["error_code"] == "WAIT_TIMEOUT"
    assert "Ready" in timeout["snapshot"]


def test_hidden_wait_accepts_node_removed_since_latest_snapshot(browser, tmp_path):
    backend, sid = browser
    snap = load(browser, tmp_path, '<button id="loading">Loading</button>')
    evaluate(browser, "loading.remove()")
    result = backend.wait_for(sid, ref=ref(snap, "Loading"), state="hidden", timeout_ms=100)
    assert result["success"] and result["condition_met"]


def test_text_wait_sees_visible_match_after_many_hidden_matches(browser, tmp_path):
    backend, sid = browser
    load(browser, tmp_path, '<p style="display:none">Ready</p>' * 120 + '<p>Ready</p>')
    result = backend.wait_for(sid, text="Ready", timeout_ms=100)
    assert result["success"] and result["condition_met"]


def test_action_completed_snapshot_failure_is_not_reported_as_success(browser, tmp_path):
    backend, sid = browser
    snap = load(browser, tmp_path, """
        <button onclick="window.count=(window.count||0)+1;
            Document.prototype.querySelectorAll=()=>{throw Error('snapshot unavailable')}">
            Run</button>
    """)
    result = backend.click(sid, ref(snap, "Run"))
    assert result["success"] is False
    assert result["action_completed"] is True
    assert result["error_code"] == "SNAPSHOT_FAILED" and result["status"] == "partial"
    assert evaluate(browser, "window.count") == 1
    assert "do not repeat" in result["suggestion"]


def test_large_snapshot_only_counts_references_that_are_returned(browser, tmp_path):
    backend, sid = browser
    buttons = "".join(f'<button>Action {index:03} {"x" * 90}</button>' for index in range(100))
    snap = load(browser, tmp_path, '<style>button{width:60px;height:30px;overflow:hidden}</style>' + buttons)
    refs = re.findall(r"^\[(e\d+)\]", snap["snapshot"], flags=re.MULTILINE)
    assert "Snapshot truncated" in snap["snapshot"]
    assert 0 < snap["ref_count"] == len(refs) < 100
    assert set(backend._sessions[sid].ref_map) == set(refs)
    assert len(snap["snapshot"]) < 8100


def test_screenshot_paths_are_unique_and_viewport_size_is_respected(browser, tmp_path, monkeypatch):
    backend, sid = browser
    load(browser, tmp_path, '<body style="height:2000px"><h1>Tall page</h1></body>')
    backend._sessions[sid].downloads_dir = tmp_path
    monkeypatch.setattr("mclaw.tools.browser_backend.time.time", lambda: 1000)
    full = backend.screenshot(sid)
    viewport = backend.screenshot(sid, full_page=False)
    assert full["success"] and viewport["success"] and full["path"] != viewport["path"]
    assert full["mime_type"] == viewport["mime_type"] == "image/png"
    height = lambda result: struct.unpack(">II", Path(result["path"]).read_bytes()[16:24])[1]
    assert height(full) > 2000
    assert height(viewport) == 720
    jpeg = backend.screenshot(sid, str(tmp_path / "page.jpg"), full_page=False)
    assert jpeg["success"] and jpeg["mime_type"] == "image/jpeg"
    assert Path(jpeg["path"]).read_bytes().startswith(b"\xff\xd8")
