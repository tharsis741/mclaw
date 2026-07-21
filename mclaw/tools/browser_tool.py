# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Browser Tool — 8 sub-tools for web browsing via Playwright.

Logical sessions use isolated contexts in one lazy shared Chromium process and
are auto-cleaned after inactivity.
"""

from __future__ import annotations

import atexit
import json
import logging
import re
import threading
import time
import uuid

from mclaw.tools.browser_backend import BrowserBackend
from mclaw.tools.browser_requirements import check_browser_requirements, diagnose_browser_requirements
from mclaw.tools.registry import registry, tool_error

logger = logging.getLogger(__name__)

# ── Session management ──────────────────────────────────────────────────────

_SESSION_TIMEOUT = 300.0  # seconds
_CLEANUP_INTERVAL = 30.0

_browser_sessions: dict[str, "BrowserSession"] = {}
_sessions_lock = threading.Lock()
_cleanup_started = False
_REF_RE = re.compile(r"^e\d+$")

# ponytail: one Playwright owner thread serializes calls; add a backend pool only
# if measured concurrent browser workloads require it.
_browser_backend = BrowserBackend(headless=True)


def _shutdown_browser_backend() -> None:
    try:
        _browser_backend.stop()
    except Exception as exc:
        logger.debug("Error stopping shared browser backend: %s", exc)


atexit.register(_shutdown_browser_backend)


class BrowserSession:
    """Tool-level session with an isolated context and inactivity timestamp."""

    def __init__(self, session_id: str, backend: BrowserBackend | None = None):
        self.session_id = session_id
        self.backend = backend or _browser_backend
        self.last_activity = time.time()

    def touch(self) -> None:
        self.last_activity = time.time()

    def is_expired(self, now: float | None = None) -> bool:
        return (time.time() if now is None else now) - self.last_activity > _SESSION_TIMEOUT

    def close(self) -> None:
        try:
            self.backend.close_session(self.session_id)
        except Exception as e:
            logger.debug("Error closing session %s: %s", self.session_id, e)


def _get_or_create_session(session_id: str) -> BrowserSession:
    """Return a cached browser session and refresh its inactivity timer."""
    with _sessions_lock:
        sess = _browser_sessions.get(session_id)
        if sess is None:
            logger.info("Creating browser session: %s", session_id)
            sess = BrowserSession(session_id)
            _browser_sessions[session_id] = sess
        sess.touch()
        return sess


def _cleanup_expired_sessions(now: float | None = None) -> int:
    """Remove expired sessions and close their contexts atomically by session ID."""
    check_time = time.time() if now is None else now
    with _sessions_lock:
        expired = [
            (sid, sess)
            for sid, sess in _browser_sessions.items()
            if sess.is_expired(check_time)
        ]
        for sid, sess in expired:
            _browser_sessions.pop(sid, None)
            logger.info("Browser session expired, closing: %s", sid)
            try:
                sess.close()
            except Exception as exc:
                logger.warning("Cleanup error for %s: %s", sid, exc)
    return len(expired)


def _cleanup_loop() -> None:
    """Background thread: close expired sessions."""
    while True:
        time.sleep(_CLEANUP_INTERVAL)
        _cleanup_expired_sessions()


def _start_cleanup_if_needed() -> None:
    """Start the daemon cleanup thread once for the process."""
    global _cleanup_started
    if _cleanup_started:
        return
    with _sessions_lock:
        if _cleanup_started:
            return
        _cleanup_started = True
        t = threading.Thread(target=_cleanup_loop, daemon=True, name="browser-cleanup")
        t.start()
        logger.debug("Browser cleanup thread started")


# ── Helpers ─────────────────────────────────────────────────────────────────

def _resolve_session_id(parent_agent=None) -> str:
    """Build a stable session ID from parent agent or context."""
    from mclaw.tools.dispatch import get_current_session_id
    sid = get_current_session_id()
    if sid:
        return f"browser_{sid}"
    if parent_agent is not None:
        aid = getattr(parent_agent, "session_id", "") or getattr(parent_agent, "id", "")
        if aid:
            return f"browser_{aid}"
    return f"browser_{uuid.uuid4().hex[:8]}"


def _strip_ref_prefix(ref: str) -> str:
    """Normalize element refs such as '@e5' or 'e5' to 'e5'."""
    return ref.lstrip("@").strip()


def _clean_ref(ref: str) -> tuple[str | None, str | None]:
    """Validate that a model-supplied ref came from the latest snapshot format."""
    if not ref or not isinstance(ref, str):
        return None, "ref is required"
    clean = _strip_ref_prefix(ref)
    if not _REF_RE.fullmatch(clean):
        return None, "ref must be an element id from browser_snapshot, e.g. 'e5' or '@e5'"
    return clean, None


def _output_path_error(path: str | None, tool_name: str, parent_agent=None) -> str | None:
    """Apply file-tool safety checks before browser tools write artifacts."""
    if not path:
        return None
    try:
        from mclaw.tools import file_operations as ops
        from mclaw.tools.file_tools import _check_delegation_path, _skill_store_mutation_error

        try:
            ops._checked_path(path, "write")
        except PermissionError as exc:
            return f"File safety blocked {tool_name}: {exc}"

        blocked = _skill_store_mutation_error(path, tool_name)
        if blocked:
            return blocked
        safe, err = _check_delegation_path(path, parent_agent)
        if not safe:
            return err
    except Exception as exc:
        return f"File safety blocked {tool_name}: invalid or unsafe output path {path!r}: {exc}"
    return None


# ── 1. browser_navigate ─────────────────────────────────────────────────────

def browser_navigate(url: str, parent_agent=None) -> str:
    """Navigate to a URL."""
    if not url or not isinstance(url, str):
        return tool_error("url is required", success=False)

    _start_cleanup_if_needed()
    session_id = _resolve_session_id(parent_agent)
    sess = _get_or_create_session(session_id)

    try:
        nav = sess.backend.navigate(session_id, url)
        snap = sess.backend.snapshot(session_id)
        result = {
            "success": not bool(nav.get("error")),
            "url": snap.get("url", nav.get("url", "")),
            "title": snap.get("title", nav.get("title", "")),
            "snapshot": snap["snapshot"],
        }
        if nav.get("error"):
            result["error"] = nav.get("error")
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        logger.exception("browser_navigate error: %s", e)
        return tool_error(str(e), success=False)


# ── 2. browser_snapshot ─────────────────────────────────────────────────────

def browser_snapshot(parent_agent=None) -> str:
    """Get the current viewport's compact interaction snapshot."""
    _start_cleanup_if_needed()
    session_id = _resolve_session_id(parent_agent)
    sess = _get_or_create_session(session_id)

    try:
        snap = sess.backend.snapshot(session_id)
        return json.dumps({
            "success": True,
            "url": snap["url"],
            "title": snap["title"],
            "snapshot": snap["snapshot"],
        }, ensure_ascii=False)
    except Exception as e:
        logger.exception("browser_snapshot error: %s", e)
        return tool_error(str(e), success=False)


# ── 3. browser_screenshot ───────────────────────────────────────────────────

def browser_screenshot(path: str | None = None, parent_agent=None) -> str:
    """Take a full-page screenshot."""
    err = _output_path_error(path, "browser_screenshot", parent_agent)
    if err:
        return tool_error(err, success=False)

    _start_cleanup_if_needed()
    session_id = _resolve_session_id(parent_agent)
    sess = _get_or_create_session(session_id)

    try:
        result = sess.backend.screenshot(session_id, path=path)
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        logger.exception("browser_screenshot error: %s", e)
        return tool_error(str(e), success=False)


# ── 4. browser_click ────────────────────────────────────────────────────────

def browser_click(ref: str, parent_agent=None) -> str:
    """Click an element by its ref ID (e.g. 'e5' or '@e5')."""
    clean_ref, err = _clean_ref(ref)
    if err:
        return tool_error(err, success=False)

    _start_cleanup_if_needed()
    session_id = _resolve_session_id(parent_agent)
    sess = _get_or_create_session(session_id)

    try:
        result = sess.backend.click(session_id, clean_ref)
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        logger.exception("browser_click error: %s", e)
        return tool_error(str(e), success=False)


# ── 5. browser_type ─────────────────────────────────────────────────────────

def browser_type(ref: str, text: str, parent_agent=None) -> str:
    """Type text into an input element."""
    clean_ref, err = _clean_ref(ref)
    if err:
        return tool_error(err, success=False)
    if text is None:
        return tool_error("text is required", success=False)

    _start_cleanup_if_needed()
    session_id = _resolve_session_id(parent_agent)
    sess = _get_or_create_session(session_id)

    try:
        result = sess.backend.type_text(session_id, clean_ref, text)
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        logger.exception("browser_type error: %s", e)
        return tool_error(str(e), success=False)


# ── 6. browser_scroll ───────────────────────────────────────────────────────

def browser_scroll(direction: str = "down", parent_agent=None) -> str:
    """Scroll the page. direction: up, down, left, right."""
    _start_cleanup_if_needed()
    session_id = _resolve_session_id(parent_agent)
    sess = _get_or_create_session(session_id)

    try:
        result = sess.backend.scroll(session_id, direction=direction)
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        logger.exception("browser_scroll error: %s", e)
        return tool_error(str(e), success=False)


# ── 7. browser_press ────────────────────────────────────────────────────────

def browser_press(key: str, parent_agent=None) -> str:
    """Press a keyboard key (e.g. 'Enter', 'Escape', 'ArrowDown')."""
    if not key or not isinstance(key, str):
        return tool_error("key is required", success=False)

    _start_cleanup_if_needed()
    session_id = _resolve_session_id(parent_agent)
    sess = _get_or_create_session(session_id)

    try:
        result = sess.backend.press(session_id, key)
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        logger.exception("browser_press error: %s", e)
        return tool_error(str(e), success=False)


# ── 8. browser_download ─────────────────────────────────────────────────────

def browser_download(
    url: str | None = None,
    path: str | None = None,
    ref: str | None = None,
    parent_agent=None,
) -> str:
    """Download a file by direct URL, by clicking a ref, or by returning a queued click download."""
    err = _output_path_error(path, "browser_download", parent_agent)
    if err:
        return tool_error(err, success=False)

    _start_cleanup_if_needed()
    session_id = _resolve_session_id(parent_agent)
    sess = _get_or_create_session(session_id)

    try:
        clean_ref = None
        if ref:
            clean_ref, err = _clean_ref(ref)
            if err:
                return tool_error(err, success=False)
        result = sess.backend.download(session_id, url=url, path=path, ref=clean_ref)
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        logger.exception("browser_download error: %s", e)
        return tool_error(str(e), success=False)


# ── Registry ────────────────────────────────────────────────────────────────

_BROWSER_TOOLS = [
    {
        "name": "browser_navigate",
        "short_description": "Navigate the interactive browser to a URL",
        "description": (
            "Navigate to a URL in the interactive browser. Initializes the session, loads "
            "the page, and returns a compact snapshot of the current viewport with element "
            "refs. For information "
            "retrieval, prefer web_search or web_extract. Use browser tools when you need to "
            "interact with a page, such as clicking or filling forms. Call browser_navigate "
            "before browser tools that act on or inspect the current page; browser_snapshot "
            "is not needed immediately after navigation."
        ),
        "params": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Absolute URL to open, including its scheme.",
                },
            },
            "required": ["url"],
        },
        "handler": lambda args, **kw: browser_navigate(args.get("url", ""), parent_agent=kw.get("parent_agent")),
        "emoji": "🌐",
    },
    {
        "name": "browser_snapshot",
        "short_description": "Refresh the current viewport snapshot and element refs",
        "description": "Refresh the compact snapshot of the current viewport and its element refs.",
        "params": {
            "type": "object",
            "properties": {},
        },
        "handler": lambda args, **kw: browser_snapshot(parent_agent=kw.get("parent_agent")),
        "emoji": "📸",
    },
    {
        "name": "browser_screenshot",
        "short_description": "Save a full-page screenshot",
        "description": "Save a full-page screenshot and return its file path.",
        "params": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Output file path. Defaults to the M-Claw downloads directory.",
                },
            },
        },
        "handler": lambda args, **kw: browser_screenshot(path=args.get("path"), parent_agent=kw.get("parent_agent")),
        "emoji": "📷",
    },
    {
        "name": "browser_click",
        "short_description": "Click an element by snapshot ref",
        "description": "Click an element by ref and return the updated current-viewport snapshot.",
        "params": {
            "type": "object",
            "properties": {
                "ref": {
                    "type": "string",
                    "description": "Element ref from the current page snapshot.",
                },
            },
            "required": ["ref"],
        },
        "handler": lambda args, **kw: browser_click(args.get("ref", ""), parent_agent=kw.get("parent_agent")),
        "emoji": "👆",
    },
    {
        "name": "browser_type",
        "short_description": "Set text in an input by snapshot ref",
        "description": "Set an input's text by ref and return the updated current-viewport snapshot.",
        "params": {
            "type": "object",
            "properties": {
                "ref": {
                    "type": "string",
                    "description": "Input ref from the current page snapshot.",
                },
                "text": {
                    "type": "string",
                    "description": "Text to set.",
                },
            },
            "required": ["ref", "text"],
        },
        "handler": lambda args, **kw: browser_type(
            args.get("ref", ""), args.get("text", ""), parent_agent=kw.get("parent_agent")
        ),
        "emoji": "⌨️",
    },
    {
        "name": "browser_scroll",
        "short_description": "Scroll the current page",
        "description": "Scroll the current page and return a compact snapshot of the new viewport.",
        "params": {
            "type": "object",
            "properties": {
                "direction": {
                    "type": "string",
                    "enum": ["up", "down", "left", "right"],
                    "default": "down",
                    "description": "Scroll direction.",
                },
            },
        },
        "handler": lambda args, **kw: browser_scroll(args.get("direction", "down"), parent_agent=kw.get("parent_agent")),
        "emoji": "📜",
    },
    {
        "name": "browser_press",
        "short_description": "Press a keyboard key on the current page",
        "description": "Press a keyboard key and return the updated current-viewport snapshot.",
        "params": {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "Key name, such as Enter, Escape, ArrowDown, or Tab.",
                },
            },
            "required": ["key"],
        },
        "handler": lambda args, **kw: browser_press(args.get("key", ""), parent_agent=kw.get("parent_agent")),
        "emoji": "🔘",
    },
    {
        "name": "browser_download",
        "short_description": "Download a file by URL or snapshot ref",
        "description": (
            "Download from ref if provided, otherwise from url; with neither, return the latest "
            "captured download."
        ),
        "params": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Direct download URL.",
                },
                "ref": {
                    "type": "string",
                    "description": "Download-triggering ref from the current page snapshot.",
                },
                "path": {
                    "type": "string",
                    "description": "Destination file or directory. Defaults to the M-Claw downloads directory.",
                },
            },
        },
        "handler": lambda args, **kw: browser_download(
            url=args.get("url"), path=args.get("path"), ref=args.get("ref"), parent_agent=kw.get("parent_agent")
        ),
        "emoji": "⬇️",
    },
]


for _tool in _BROWSER_TOOLS:
    registry.register(
        name=_tool["name"],
        toolset="browser",
        schema={
            "type": "function",
            "function": {
                "name": _tool["name"],
                "description": _tool["description"],
                "parameters": _tool["params"],
            },
        },
        handler=_tool["handler"],
        check_fn=check_browser_requirements,
        description=_tool.get(
            "short_description",
            _tool["description"].split("\n", 1)[0],
        ),
        emoji=_tool["emoji"],
        max_result_size_chars=12_000,
        diagnose_fn=diagnose_browser_requirements,
    )
