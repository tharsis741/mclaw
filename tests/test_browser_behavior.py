from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

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


def test_download_tool_saves_a_direct_http_attachment(tmp_path, backend, monkeypatch) -> None:
    payload = b"%PDF-1.4\nlocal download regression\n%%EOF"
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Disposition", 'attachment; filename="paper.pdf"')
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        def log_message(self, *_args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    sid = "browser_direct-download-tool"
    output = tmp_path / "paper.pdf"
    monkeypatch.setattr(browser_tool, "_start_cleanup_if_needed", lambda: None)
    monkeypatch.setattr(browser_tool, "_get_or_create_session", lambda _sid: SimpleNamespace(backend=backend))
    try:
        result = json.loads(browser_tool.browser_download(url=f"http://127.0.0.1:{server.server_port}/paper.pdf",
            path=str(output), parent_agent=SimpleNamespace(session_id="direct-download-tool")))
        assert result["success"], result
        assert output.read_bytes() == payload
    finally:
        backend.close_session(sid)
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def inline_download_server():
    import io
    from PIL import Image

    requests = []
    picture = io.BytesIO()
    Image.new("RGB", (4, 4), "blue").save(picture, format="PNG")
    text = b"original text file\n"
    payloads = {"plain": text, "redirect": text, "missing": b"File not found",
                "picture.png": picture.getvalue(), "document.pdf": b"%PDF-1.4\nlocal fixture\n%%EOF"}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, self.headers.get("Cookie", "")))
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/plain")
                self.send_header("Set-Cookie", "redirected=yes; Path=/")
                self.end_headers()
                return
            self.send_response(404 if self.path == "/missing" else 200)
            payload = payloads[self.path.lstrip("/")]
            mime = {"/picture.png": "image/png", "/document.pdf": "application/pdf"}.get(self.path, "text/plain")
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(payload)))
            if self.path == "/document.pdf":
                self.send_header("Content-Disposition", 'inline; filename="server-name.pdf"')
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", payloads, requests
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


@pytest.mark.parametrize("endpoint", ["plain", "picture.png", "document.pdf", "redirect"])
def test_direct_download_accepts_inline_files_and_preserves_current_tab(backend, tmp_path, inline_download_server, endpoint):
    base, payloads, requests = inline_download_server
    sid = "inline-download-" + endpoint
    url = _page_url(tmp_path, '<button onclick="this.textContent=\'Still works\'">Existing control</button>')
    try:
        backend.navigate(sid, url)
        snap = backend.snapshot(sid)
        existing = _element_ref(snap["snapshot"], "Existing control")
        backend._run_on_worker(lambda: backend._sessions[sid].context.add_cookies([
            {"name": "authenticated", "value": "yes", "url": base}]))
        result = backend.download(sid, url=base + "/" + endpoint, path=str(tmp_path) + "/")
        assert result["success"], result
        from pathlib import Path
        assert Path(result["path"]).read_bytes() == payloads[endpoint]
        if endpoint == "document.pdf":
            assert Path(result["path"]).name == "server-name.pdf"
        assert all("authenticated=yes" in cookie for _, cookie in requests)
        if endpoint == "redirect":
            assert "redirected=yes" in requests[-1][1]
        tabs = backend.tabs(sid)
        assert len(tabs["tabs"]) == 1 and tabs["tabs"][0]["url"] == url
        assert "Still works" in backend.click(sid, existing)["snapshot"]
    finally:
        backend.close_session(sid)


def test_direct_download_http_error_preserves_existing_file(backend, tmp_path, inline_download_server):
    base, _, _ = inline_download_server
    sid = "download-http-error"
    output = tmp_path / "saved.txt"
    output.write_bytes(b"keep existing")
    try:
        # A different retained download must not be reported as this request's file.
        backend.get_or_create_page(sid)
        backend._run_on_worker(lambda: backend._sessions[sid].downloads.append(output))
        result = backend.download(sid, url=base + "/missing", path=str(output))
        assert not result["success"] and "404" in result["error"]
        assert result["download_retained"] is False
        assert output.read_bytes() == b"keep existing"
        assert len(backend.tabs(sid)["tabs"]) == 1
    finally:
        backend.close_session(sid)


def test_direct_download_keeps_capture_when_saving_fails(backend, tmp_path, inline_download_server):
    base, payloads, requests = inline_download_server
    sid = "inline-download-retry"
    blocker = tmp_path / "blocker"
    blocker.write_text("keep")
    try:
        failed = backend.download(sid, url=base + "/plain", path=str(blocker / "saved.txt"))
        assert not failed["success"] and failed["download_retained"]
        retried = backend.download(sid, path=str(tmp_path / "saved.txt"))
        assert retried["success"], retried
        assert (tmp_path / "saved.txt").read_bytes() == payloads["plain"]
        assert len(requests) == 1
    finally:
        backend.close_session(sid)


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
