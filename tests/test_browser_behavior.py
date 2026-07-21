from __future__ import annotations

import json
import re

import pytest

from mclaw.tools import browser_tool
from mclaw.tools.browser_backend import BrowserBackend
from mclaw.tools.browser_requirements import check_browser_requirements


pytestmark = pytest.mark.skipif(
    not check_browser_requirements(),
    reason="Playwright Chromium is not installed",
)


def _page_url(tmp_path, body: str) -> str:
    path = tmp_path / "browser-contract.html"
    path.write_text(body, encoding="utf-8")
    return path.resolve().as_uri()


@pytest.fixture(scope="module")
def backend() -> BrowserBackend:
    instance = BrowserBackend(headless=True)
    yield instance
    instance.stop()


def _element_ref(snapshot: str, label: str) -> str:
    match = re.search(rf"\[(e\d+)\].*'{re.escape(label)}'", snapshot)
    assert match is not None
    return match.group(1)


def test_snapshot_and_scroll_follow_the_current_viewport(tmp_path, backend) -> None:
    url = _page_url(
        tmp_path,
        """<!doctype html>
        <html><head><title>Viewport contract</title><style>
        body { margin: 0; height: 2200px; }
        #top { position: absolute; top: 20px; }
        #lower { position: absolute; top: 900px; }
        </style></head><body>
        <section id="top"><p>Top context</p><button>Top action</button></section>
        <section id="lower"><p>Lower context</p><button>Lower action</button></section>
        </body></html>""",
    )
    session_id = "viewport-test"
    try:
        backend.navigate(session_id, url)
        first = backend.snapshot(session_id)["snapshot"]
        second = backend.scroll(session_id, "down")["snapshot"]
    finally:
        backend.close_session(session_id)

    assert "Top context" in first
    assert "Top action" in first
    assert "Lower context" not in first
    assert "Page continues below" in first
    assert "Lower context" in second
    assert "Lower action" in second
    assert "Top context" not in second
    assert "Page continues above and below" in second


def test_snapshot_waits_for_delayed_dom_render(tmp_path, backend) -> None:
    url = _page_url(
        tmp_path,
        """<!doctype html><html><head><title>Delayed render</title></head><body>
        <p>Loading</p>
        <script>
        setTimeout(() => {
            const button = document.createElement('button');
            button.textContent = 'Late action';
            document.body.appendChild(button);
        }, 700);
        </script></body></html>""",
    )
    session_id = "delayed-render-test"
    try:
        backend.navigate(session_id, url)
        snapshot = backend.snapshot(session_id)["snapshot"]
    finally:
        backend.close_session(session_id)

    assert "Late action" in snapshot


def test_scroll_targets_visible_nested_scroll_container(tmp_path, backend) -> None:
    url = _page_url(
        tmp_path,
        """<!doctype html><html><head><title>Nested scroll</title><style>
        html, body { margin: 0; height: 100%; overflow: hidden; }
        #scroller { width: 700px; height: 500px; margin: 100px auto 0;
                    overflow-y: auto; border: 1px solid black; }
        #content { position: relative; height: 1400px; }
        #top { position: absolute; top: 20px; }
        #lower { position: absolute; top: 800px; }
        </style></head><body><div id="scroller"><div id="content">
        <section id="top"><p>Nested top</p><button>Top nested action</button></section>
        <section id="lower"><p>Nested lower</p><button>Lower nested action</button></section>
        </div></div></body></html>""",
    )
    session_id = "nested-scroll-test"
    try:
        backend.navigate(session_id, url)
        first = backend.snapshot(session_id)["snapshot"]
        result = backend.scroll(session_id, "down")
    finally:
        backend.close_session(session_id)

    assert "Nested top" in first
    assert "Nested lower" not in first
    assert result["scroll"] == {
        "target": "div#scroller",
        "axis": "vertical",
        "before": 0,
        "after": 600,
        "moved": True,
    }
    assert "Nested lower" in result["snapshot"]
    assert "Nested top" not in result["snapshot"]


def test_type_and_press_submit_the_focused_form(tmp_path, backend) -> None:
    url = _page_url(
        tmp_path,
        """<!doctype html><html><head><title>Form actions</title></head><body>
        <form onsubmit="event.preventDefault(); document.getElementById('status').textContent =
                        'Submitted: ' + document.getElementById('query').value;">
        <input id="query" placeholder="Search term"><button>Submit</button>
        </form><p id="status">Waiting</p></body></html>""",
    )
    session_id = "type-press-test"
    try:
        backend.navigate(session_id, url)
        snapshot = backend.snapshot(session_id)["snapshot"]
        input_ref = _element_ref(snapshot, "Search term")
        typed = backend.type_text(session_id, input_ref, "browser contract")
        pressed = backend.press(session_id, "Enter")
    finally:
        backend.close_session(session_id)

    assert typed["success"] is True
    assert pressed["success"] is True
    assert "Submitted: browser contract" in pressed["snapshot"]


def test_screenshot_writes_a_valid_png(tmp_path, backend) -> None:
    url = _page_url(
        tmp_path,
        "<!doctype html><html><head><title>Screenshot</title></head><body><h1>Capture me</h1></body></html>",
    )
    output = tmp_path / "capture.png"
    session_id = "screenshot-test"
    try:
        backend.navigate(session_id, url)
        result = backend.screenshot(session_id, str(output))
    finally:
        backend.close_session(session_id)

    assert result["success"] is True
    assert result["size"] > 8
    assert output.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def test_download_by_snapshot_ref_writes_expected_content(tmp_path, backend) -> None:
    url = _page_url(
        tmp_path,
        """<!doctype html><html><head><title>Download</title></head><body>
        <a download="sample.txt" href="data:text/plain;charset=utf-8,hello%20browser">Save sample</a>
        </body></html>""",
    )
    output = tmp_path / "saved.txt"
    session_id = "download-test"
    try:
        backend.navigate(session_id, url)
        snapshot = backend.snapshot(session_id)["snapshot"]
        download_ref = _element_ref(snapshot, "Save sample")
        result = backend.download(session_id, ref=download_ref, path=str(output))
    finally:
        backend.close_session(session_id)

    assert result["success"] is True
    assert output.read_text(encoding="utf-8") == "hello browser"


def test_close_session_preserves_other_browser_contexts(tmp_path, backend) -> None:
    first_url = _page_url(tmp_path, "<html><head><title>First</title></head><body></body></html>")
    second_path = tmp_path / "second.html"
    second_path.write_text(
        "<html><head><title>Second</title></head><body><button>Still open</button></body></html>",
        encoding="utf-8",
    )
    second_url = second_path.resolve().as_uri()
    first_id = "close-first-test"
    second_id = "close-second-test"
    try:
        backend.navigate(first_id, first_url)
        backend.navigate(second_id, second_url)
        backend.snapshot(first_id)
        backend.snapshot(second_id)

        backend.close_session(first_id)
        remaining = backend.snapshot(second_id)
    finally:
        backend.close_session(first_id)
        backend.close_session(second_id)

    assert "Still open" in remaining["snapshot"]
    assert first_id not in backend._sessions
    assert second_id not in backend._sessions


def test_tool_sessions_share_backend_and_cleanup_only_expired_contexts() -> None:
    class RecordingBackend:
        def __init__(self) -> None:
            self.closed: list[str] = []

        def close_session(self, session_id: str) -> None:
            self.closed.append(session_id)

    recording = RecordingBackend()
    now = 10_000.0
    expired = browser_tool.BrowserSession("cleanup-expired", backend=recording)
    active = browser_tool.BrowserSession("cleanup-active", backend=recording)
    expired.last_activity = now - browser_tool._SESSION_TIMEOUT - 1
    active.last_activity = now

    with browser_tool._sessions_lock:
        browser_tool._browser_sessions[expired.session_id] = expired
        browser_tool._browser_sessions[active.session_id] = active
    try:
        count = browser_tool._cleanup_expired_sessions(now=now)
        with browser_tool._sessions_lock:
            assert expired.session_id not in browser_tool._browser_sessions
            assert active.session_id in browser_tool._browser_sessions
    finally:
        with browser_tool._sessions_lock:
            browser_tool._browser_sessions.pop(expired.session_id, None)
            browser_tool._browser_sessions.pop(active.session_id, None)

    assert count == 1
    assert recording.closed == [expired.session_id]
    assert browser_tool.BrowserSession("shared-one").backend is browser_tool._browser_backend
    assert browser_tool.BrowserSession("shared-two").backend is browser_tool._browser_backend


def test_navigate_returns_refs_ready_for_interaction(tmp_path, monkeypatch) -> None:
    url = _page_url(
        tmp_path,
        """<!doctype html><html><head><title>Interaction contract</title></head><body>
        <p id="status">Idle</p>
        <button onclick="document.getElementById('status').textContent='Clicked'">Run</button>
        </body></html>""",
    )
    session_id = "browser_contract_test"
    monkeypatch.setattr(browser_tool, "_resolve_session_id", lambda _parent=None: session_id)

    try:
        navigated = json.loads(browser_tool.browser_navigate(url))
        match = re.search(r"\[(e\d+)\] button 'Run'", navigated["snapshot"])
        assert match is not None

        clicked = json.loads(browser_tool.browser_click(match.group(1)))
        assert clicked["success"] is True
        assert "text: Clicked" in clicked["snapshot"]
    finally:
        with browser_tool._sessions_lock:
            session = browser_tool._browser_sessions.pop(session_id, None)
        if session is not None:
            session.close()
