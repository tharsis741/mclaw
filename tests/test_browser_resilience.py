# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Failure and recovery contracts for browser automation on local pages."""

from __future__ import annotations

import re
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from mclaw.tools.browser_backend import BrowserBackend, BrowserOperationTimedOut
from mclaw.tools.browser_requirements import check_browser_requirements


pytestmark = pytest.mark.skipif(not check_browser_requirements(), reason="Chromium is not installed")


@pytest.fixture(scope="module")
def backend():
    instance = BrowserBackend()
    yield instance
    instance.stop()


@pytest.fixture
def browser(backend, request, tmp_path):
    sid = request.node.name

    def load(html):
        path = tmp_path / "page.html"
        path.write_text(html, encoding="utf-8")
        nav, snapshot = backend.navigate_and_snapshot(sid, path.as_uri())
        assert "error" not in nav and snapshot["success"], (nav, snapshot)
        return snapshot

    yield backend, sid, load
    backend.close_session(sid)


def ref(snapshot, label):
    match = re.search(rf"\[(e\d+)\] \S+ '{re.escape(label)}'", snapshot["snapshot"])
    assert match, snapshot
    return match[1]


def evaluate(browser, script):
    backend, sid, _ = browser
    return backend._run_on_worker(lambda: backend._sessions[sid].page.evaluate(script))


def test_generic_spa_text_is_observable_and_hidden_text_is_excluded(browser):
    backend, sid, load = browser
    snap = load('<button onclick="result.innerHTML=\'<span>Submitted successfully</span>\'">Submit</button>'
                '<div id="result"><span>Waiting</span><span hidden>Hidden text</span></div>')
    assert "text: Waiting" in snap["snapshot"]
    assert "Hidden text" not in snap["snapshot"]
    result = backend.click(sid, ref(snap, "Submit"))
    assert "text: Submitted successfully" in result["snapshot"]


def test_snapshot_excludes_inert_controls_text_and_frames(browser):
    _, _, load = browser
    snap = load('''<button>Available</button><section inert>
        <button>Inactive button</button><div>Inactive text</div>
        <iframe srcdoc="<button>Inactive frame</button>"></iframe></section>
        <div id="host" inert></div><script>
        host.attachShadow({mode:'open'}).innerHTML='<button>Inactive shadow</button>';
        </script>''')
    assert "Available" in snap["snapshot"]
    assert "Inactive" not in snap["snapshot"]


def test_snapshot_follows_top_modal_and_restores_background_after_close(browser):
    backend, sid, load = browser
    snap = load('''<button>Background</button><div>Background text</div>
        <iframe srcdoc="<button>Background frame</button>"></iframe>
        <dialog id="first"><button onclick="first.close()">Close first</button>
        <iframe srcdoc="<button>Modal frame</button>"></iframe></dialog>
        <dialog id="second"><button onclick="second.close()">Close second</button></dialog>
        <script>second.showModal();first.showModal();</script>''')
    assert "Close first" in snap["snapshot"] and "Modal frame" in snap["snapshot"]
    assert "Background" not in snap["snapshot"] and "Close second" not in snap["snapshot"]
    snap = backend.click(sid, ref(snap, "Close first"))
    assert "Close second" in snap["snapshot"] and "Background" not in snap["snapshot"]
    snap = backend.click(sid, ref(snap, "Close second"))
    assert "Background frame" in snap["snapshot"] and "Background text" in snap["snapshot"]


def test_modal_in_shadow_root_escapes_inert_ancestor(browser):
    _, _, load = browser
    snap = load('''<button>Background</button><div inert><div id="host"></div></div>
        <script>const root=host.attachShadow({mode:'open'});
        root.innerHTML='<dialog><button>Inside modal</button></dialog>';
        root.querySelector('dialog').showModal();</script>''')
    assert "Inside modal" in snap["snapshot"]
    assert "Background" not in snap["snapshot"]


@pytest.mark.parametrize("editable", ["", "plaintext-only", "TRUE"])
def test_snapshot_and_type_support_contenteditable_variants(browser, editable):
    backend, sid, load = browser
    snap = load(f'<div contenteditable="{editable}" aria-label="Editor" style="min-height:30px"></div>'
                '<div contenteditable="false">Read only</div>')
    result = backend.type_text(sid, ref(snap, "Editor"), "Hello 编辑器")
    assert result["success"], result
    assert evaluate(browser, "document.querySelector('[contenteditable]').textContent") == "Hello 编辑器"


def test_snapshot_can_page_through_controls_in_one_viewport(browser):
    backend, sid, load = browser
    snap = load('<style>button{width:45px;height:25px;overflow:hidden}</style>' +
                ''.join(f'<button>Action {index}</button>' for index in range(150)))
    assert snap.get("next_offset") is not None, snap
    second = backend.snapshot(sid, offset=snap["next_offset"])
    assert "Action 149" in second["snapshot"]
    assert backend.click(sid, ref(second, "Action 149"))["success"]


def test_scroll_can_target_an_iframe_and_shadow_scroller(browser, tmp_path):
    backend, sid, load = browser
    child = tmp_path / "frame.html"
    child.write_text('<button>Frame top</button><button style="position:absolute;top:700px">Frame bottom</button>'
                     '<div style="height:1800px"></div>', encoding="utf-8")
    snap = load(f'<iframe style="height:250px" src="{child.as_uri()}"></iframe><div id="host"></div>' + """
        <script>host.attachShadow({mode:'open'}).innerHTML = `<div style="height:160px;overflow:auto">
          <button>Shadow top</button><div style="height:550px"></div><button>Shadow bottom</button>
          <div style="height:100px"></div></div>`;</script>
    """)
    result = backend.scroll(sid, ref=ref(snap, "Frame top"))
    assert result["success"] and "Frame bottom" in result["snapshot"]
    result = backend.scroll(sid, ref=ref(result, "Shadow top"))
    assert result["success"] and "Shadow bottom" in result["snapshot"]


def test_delayed_download_is_captured_after_click_returns(browser, tmp_path):
    backend, sid, load = browser
    snap = load('<a id="save" download="delayed.txt" href="data:text/plain,delayed">Save</a>'
                '<button onclick="setTimeout(() => save.click(), 1500)">Prepare</button>')
    assert backend.click(sid, ref(snap, "Prepare"))["success"]
    backend._run_on_worker(lambda: backend._sessions[sid].page.wait_for_timeout(1700))
    result = backend.download(sid, path=str(tmp_path / "received.txt"))
    assert result["success"], result
    assert Path(result["path"]).read_text() == "delayed"


def test_failed_download_destination_preserves_capture_for_retry(browser, tmp_path):
    backend, sid, load = browser
    snap = load('<a download="sample.txt" href="data:text/plain,content">Save</a>')
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("keep", encoding="utf-8")
    failed = backend.download(sid, ref=ref(snap, "Save"), path=str(blocker / "file.txt"))
    assert not failed["success"]
    retried = backend.download(sid, path=str(tmp_path / "retry.txt"))
    assert retried["success"], retried
    assert Path(retried["path"]).read_text() == "content"


def test_final_download_filename_is_checked_before_writing(browser, tmp_path):
    backend, sid, load = browser
    load('<p>Download fixture</p>')
    # Chromium sanitizes some names on Windows; test the final path boundary
    # directly so coverage is independent of platform filename sanitization.
    download = SimpleNamespace(suggested_filename=".env", save_as=lambda path: pytest.fail("must deny before write"))
    with pytest.raises(PermissionError):
        backend._run_on_worker(lambda: backend._save_download(backend._sessions[sid], download, str(tmp_path) + "/"))
    assert not (tmp_path / ".env").exists()


def test_browser_disconnect_recovers_on_next_navigation(browser):
    backend, sid, load = browser
    original = load('<title>Before disconnect</title>')
    backend._run_on_worker(lambda: backend._browser.close())
    nav, result = backend.navigate_and_snapshot(sid, original["url"])
    assert "error" not in nav and result["success"]
    assert result["session_reset"] is True


def test_unused_dialog_response_expires_after_trigger_action(browser):
    backend, sid, load = browser
    snap = load('<button>No dialog</button><button onclick="window.answer=confirm(\'Later\')">Confirm</button>')
    backend.handle_dialog(sid, True)
    result = backend.click(sid, ref(snap, "No dialog"))
    result = backend.click(sid, ref(result, "Confirm"))
    assert result["success"] and evaluate(browser, "window.answer") is False


def test_file_chooser_is_cleared_when_page_navigates_by_click(browser, tmp_path):
    backend, sid, load = browser
    next_page = tmp_path / "next.html"
    next_page.write_text('<h1>Next page</h1>', encoding="utf-8")
    snap = load('<input type="file" id="upload" hidden><button onclick="upload.click()">Choose</button>'
                f'<a href="{next_page.as_uri()}">Next</a>')
    chosen = backend.click(sid, ref(snap, "Choose"))
    assert "file_chooser" in chosen
    result = backend.click(sid, ref(chosen, "Next"))
    assert result["success"] and "file_chooser" not in result


def test_hung_page_script_times_out_without_destroying_another_session(browser):
    backend, sid, load = browser
    load('<p>Before timeout</p>')
    sentinel = sid + "-sentinel"
    backend.navigate_and_snapshot(sentinel, 'data:text/html,<p>Still alive</p>')
    started = time.monotonic()
    try:
        with pytest.raises(BrowserOperationTimedOut) as raised:
            backend._run_on_worker(lambda: backend._sessions[sid].page.evaluate("() => new Promise(() => {})"),
                                   cancel_session_id=sid, operation_timeout=0.2)
        assert raised.value.fence is None
        assert time.monotonic() - started < 2
        assert "Still alive" in backend.snapshot(sentinel)["snapshot"]
        recovered = backend.snapshot(sid)
        assert recovered["success"] and recovered["session_reset"]
    finally:
        backend.close_session(sentinel)


def test_closed_context_is_recovered_without_replaying_actions(browser):
    backend, sid, load = browser
    load('<p>Original context</p>')
    backend._run_on_worker(lambda: backend._sessions[sid].context.close())
    snap = backend.snapshot(sid)
    assert snap["success"] and snap["session_reset"] and snap["url"] == "about:blank"


def test_screenshot_failure_preserves_existing_file_and_removes_partial_output(browser, tmp_path):
    backend, sid, load = browser
    load('<p>Screenshot fixture</p>')
    target = tmp_path / "existing.png"
    target.write_bytes(b"original")

    def fail(**kwargs):
        Path(kwargs["path"]).write_bytes(b"partial")
        raise RuntimeError("Screenshot transfer failed")

    page = backend._sessions[sid].page
    original = page.screenshot
    backend._run_on_worker(setattr, page, "screenshot", fail)
    try:
        result = backend.screenshot(sid, str(target))
        assert not result["success"]
        assert target.read_bytes() == b"original"
        assert not list(tmp_path.glob(".mclaw-browser-*.part"))
    finally:
        backend._run_on_worker(setattr, page, "screenshot", original)


def test_saved_download_can_be_retrieved_at_the_same_path(browser, tmp_path):
    backend, sid, load = browser
    snap = load('<a download="test.txt" href="data:text/plain,saved">Save</a>')
    captured = backend.click(sid, ref(snap, "Save"))["download"]
    # Explicitly requesting a managed runtime path is forbidden, so exercise
    # an ordinary workspace destination after moving the saved fixture there.
    source = Path(captured["path"])
    target = tmp_path / source.name
    source.replace(target)
    backend._sessions[sid].downloads[-1] = target
    result = backend.download(sid, path=str(target))
    assert result["success"] and result["path"] == str(target.resolve())
    assert target.read_text() == "saved"


def test_large_escaped_snapshot_stays_valid_and_all_controls_can_be_reached(browser):
    backend, sid, load = browser
    buttons = ''.join(f'<button>Action {index:03} {"&quot;" * 85}</button>' for index in range(130))
    snap = load('<title>' + '"' * 3000 + '</title><style>button{width:45px;height:25px;overflow:hidden}</style>' + buttons)
    seen = set()
    for _ in range(20):
        assert len(json.dumps(snap, ensure_ascii=False)) < 12000
        seen.update(re.findall(r"Action (\d{3})", snap["snapshot"]))
        shown_refs = set(re.findall(r"^\[(e\d+)\]", snap["snapshot"], re.MULTILINE))
        assert shown_refs == set(backend._sessions[sid].ref_map)
        if "next_offset" not in snap:
            break
        snap = backend.snapshot(sid, offset=snap["next_offset"])
    assert len(seen) == 130


def test_tabs_list_is_bounded_and_paginated(browser):
    backend, sid, load = browser
    load('<p>Tabs</p>')
    backend._run_on_worker(lambda: [backend._sessions[sid].context.new_page() for _ in range(21)])
    first = backend.tabs(sid)
    second = backend.tabs(sid, offset=first["next_offset"])
    assert len(first["tabs"]) == 20 and len(second["tabs"]) == 2
    assert len({tab["id"] for tab in first["tabs"] + second["tabs"]}) == 22
    snap = backend.snapshot(sid)
    assert snap["tabs_truncated"] and snap["tab_count"] == 22 and len(snap["tabs"]) == 5


def test_prose_heavy_frames_cannot_hide_controls_in_later_frames(browser, tmp_path):
    backend, sid, load = browser
    prose = tmp_path / "prose.html"
    prose.write_text(''.join(f'<p style="margin:0">{index} {"word " * 45}</p>' for index in range(12)), encoding="utf-8")
    controls = tmp_path / "controls.html"
    controls.write_text('<button>Reachable</button>', encoding="utf-8")
    snap = load(''.join(f'<iframe style="height:550px;width:180px" src="{prose.as_uri()}"></iframe>'
                        for _ in range(5)) + f'<iframe style="height:550px;width:180px" src="{controls.as_uri()}"></iframe>')
    assert ref(snap, "Reachable")
    assert backend.click(sid, ref(snap, "Reachable"))["success"]


def test_form_actions_reject_unachievable_states_before_mutation(browser):
    backend, sid, load = browser
    snap = load('<select aria-label="Single"><option value="a">A</option><option value="b">B</option></select>'
                '<input type="radio" aria-label="Radio" name="r" checked>')
    result = backend.select(sid, ref(snap, "Single"), ["a", "b"])
    assert not result["success"] and result["action_completed"] is False
    assert evaluate(browser, "document.querySelector('select').value") == "a"
    result = backend.check(sid, ref(snap, "Radio"), False)
    assert not result["success"] and result["action_completed"] is False
    assert evaluate(browser, "document.querySelector('input').checked")


def test_scroll_movement_is_reported_with_css_smooth_scrolling(browser):
    backend, sid, load = browser
    load('<style>html{scroll-behavior:smooth}body{height:2200px}</style><button>Top</button>')
    result = backend.scroll(sid)
    assert result["success"] and result["scroll"]["moved"]
    assert result["scroll"]["after"] == evaluate(browser, "window.scrollY") == 600


def test_hidden_iframe_is_excluded_from_snapshot_and_visibility_wait(browser, tmp_path):
    backend, sid, load = browser
    child = tmp_path / "child.html"
    child.write_text('<button>Inside frame</button><p>Child text</p>', encoding="utf-8")
    snap = load(f'<div id="container"><iframe src="{child.as_uri()}"></iframe></div>')
    child_ref = ref(snap, "Inside frame")
    evaluate(browser, "container.style.opacity='0'")
    hidden = backend.wait_for(sid, ref=child_ref, state="hidden", timeout_ms=100)
    assert hidden["success"] and hidden["condition_met"]
    assert "Inside frame" not in hidden["snapshot"] and "Child text" not in hidden["snapshot"]
    visible = backend.wait_for(sid, text="Child text", timeout_ms=100)
    assert not visible["success"] and visible["error_code"] == "WAIT_TIMEOUT"
