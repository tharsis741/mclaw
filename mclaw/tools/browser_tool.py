# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Browser tools for observing and interacting with pages via Playwright.

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
from contextlib import contextmanager
from urllib.parse import urlsplit

from mclaw.tools.browser_backend import BrowserBackend, BrowserOperationCancelled, BrowserOperationTimedOut
from mclaw.tools.browser_requirements import check_browser_requirements, diagnose_browser_requirements
from mclaw.tools.interrupt import get_cancel_id, safe_cancel_trace
from mclaw.tools.registry import registry, tool_error

logger = logging.getLogger(__name__)

# ── Session management ──────────────────────────────────────────────────────

_SESSION_TIMEOUT = 300.0  # seconds
_CLEANUP_INTERVAL = 30.0

_browser_sessions: dict[str, "BrowserSession"] = {}
_sessions_lock = threading.Lock()
_closing_sessions: dict[str, threading.Event] = {}
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
        self.active_calls = 0

    def touch(self) -> None:
        self.last_activity = time.time()

    def is_expired(self, now: float | None = None) -> bool:
        return not self.active_calls and (time.time() if now is None else now) - self.last_activity > _SESSION_TIMEOUT

    def close(self) -> None:
        try:
            self.backend.close_session(self.session_id)
        except Exception as e:
            logger.debug("Error closing session %s: %s", self.session_id, e)


def _get_or_create_session(session_id: str) -> BrowserSession:
    """Return a cached browser session and refresh its inactivity timer."""
    while True:
        with _sessions_lock:
            closing = _closing_sessions.get(session_id)
            if closing is None:
                sess = _browser_sessions.get(session_id)
                if sess is None:
                    logger.info("Creating browser session: %s", session_id)
                    sess = BrowserSession(session_id)
                    _browser_sessions[session_id] = sess
                sess.touch()
                return sess
        if not closing.wait(timeout=50):
            raise RuntimeError("Previous browser session is still closing; retry when cleanup completes")


@contextmanager
def _session_lease(session_id: str):
    """Prevent idle cleanup from retiring a queued or running operation."""
    while True:
        sess = _get_or_create_session(session_id)
        if not isinstance(sess, BrowserSession):  # Lightweight backend adapters.
            yield sess
            return
        with _sessions_lock:
            if _browser_sessions.get(session_id) is sess:
                sess.active_calls += 1
                break
    try:
        yield sess
    finally:
        with _sessions_lock:
            sess.active_calls -= 1
            sess.touch()


def _cleanup_expired_sessions(now: float | None = None) -> int:
    """Close idle contexts without holding the global session lock during I/O."""
    check_time = time.time() if now is None else now
    with _sessions_lock:
        expired = [
            (sid, sess)
            for sid, sess in _browser_sessions.items()
            if sess.is_expired(check_time)
        ]
        for sid, sess in expired:
            _browser_sessions.pop(sid, None)
            _closing_sessions[sid] = threading.Event()
    for sid, sess in expired:
        logger.info("Browser session expired, closing: %s", sid)
        try:
            sess.close()
        except Exception as exc:
            logger.warning("Cleanup error for %s: %s", sid, exc)
        finally:
            with _sessions_lock:
                _closing_sessions.pop(sid).set()
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
    return ref.strip().removeprefix("@")


def _clean_ref(ref: str) -> tuple[str | None, str | None]:
    """Validate that a model-supplied ref came from the latest snapshot format."""
    if not ref or not isinstance(ref, str):
        return None, "ref is required"
    clean = _strip_ref_prefix(ref)
    if not _REF_RE.fullmatch(clean):
        return None, "ref must be an element id from browser_snapshot, e.g. 'e5' or '@e5'"
    return clean, None


def _output_path(path: str | None, tool_name: str, parent_agent=None) -> tuple[str | None, str | None]:
    """Apply file-tool safety checks before browser tools write artifacts."""
    if path is None:
        return None, None
    if not isinstance(path, str) or not path.strip():
        return None, "path must be a non-empty string"
    try:
        from mclaw.tools import file_operations as ops
        from mclaw.tools.file_tools import _resolve_agent_path, _skill_store_mutation_error

        resolved = str(ops._checked_path(_resolve_agent_path(path, parent_agent), "write"))
        blocked = _skill_store_mutation_error(resolved, tool_name)
        if blocked:
            return None, blocked
        if path.endswith(("/", "\\")):
            resolved += "/"
        return resolved, None
    except Exception as exc:
        return None, f"File safety blocked {tool_name}: invalid or unsafe output path {path!r}: {exc}"


def _register_browser_fence(parent_agent, fence) -> None:
    if parent_agent is None or fence is None:
        return
    register = getattr(parent_agent, "_register_turn_worker", None)
    unregister = getattr(parent_agent, "_unregister_turn_worker", None)
    if not callable(register):
        return
    register(fence)
    cancel_event = getattr(fence, "_mclaw_cancel_event", None)
    future = getattr(fence, "future", None)
    if future is not None and callable(unregister):
        def _release_fence(_future) -> None:
            unregister(fence)
            safe_cancel_trace(
                lambda: logger.info(
                    "[CANCEL_TRACE] browser_fence_release cancel_id=%s session=%s "
                    "task_id=%x elapsed_ms=%d",
                    get_cancel_id(cancel_event),
                    getattr(parent_agent, "session_id", "?"),
                    id(fence),
                    int(
                        (
                            time.monotonic()
                            - getattr(fence, "created_at", time.monotonic())
                        )
                        * 1000
                    ),
                )
            )

        future.add_done_callback(_release_fence)
    safe_cancel_trace(
        lambda: logger.warning(
            "[CANCEL_TRACE] browser_fence_register cancel_id=%s session=%s "
            "task_id=%x operation=%s phase=%s",
            get_cancel_id(cancel_event),
            getattr(parent_agent, "session_id", "?"),
            id(fence),
            getattr(fence, "diagnostic_name", type(fence).__name__),
            getattr(fence, "phase", "unknown"),
        )
    )


def _browser_exception_result(tool_name: str, exc: Exception, parent_agent=None) -> str:
    if isinstance(exc, BrowserOperationTimedOut):
        _register_browser_fence(parent_agent, exc.fence)
        return tool_error(str(exc), success=False, error_code="BROWSER_TIMEOUT",
                          completion_unknown=True, status="cleanup_pending" if exc.fence else "timed_out",
                          suggestion="The operation was stopped. Inspect the page before retrying; it may have completed before the timeout.")
    if isinstance(exc, BrowserOperationCancelled):
        _register_browser_fence(parent_agent, exc.fence)
        details = {
            "success": False,
            "interrupted": True,
            "status": "cancel_requested" if exc.completion_unknown else "cancelled",
        }
        if exc.completion_unknown:
            details["completion_unknown"] = True
        return tool_error(str(exc), **details)
    logger.exception("%s error: %s", tool_name, exc)
    return tool_error(str(exc), success=False)


# ── 1. browser_navigate ─────────────────────────────────────────────────────

def browser_navigate(url: str, parent_agent=None) -> str:
    """Navigate to a URL."""
    if not url or not isinstance(url, str):
        return tool_error("url is required", success=False)

    return _browser_call("browser_navigate", "navigate_and_snapshot", parent_agent, url=url)


# ── 2. browser_snapshot ─────────────────────────────────────────────────────

def browser_snapshot(parent_agent=None, *, offset: int = 0) -> str:
    """Get the current viewport, continuing controls at an optional offset."""
    if type(offset) is not int or offset < 0:
        return tool_error("offset must be a non-negative integer", success=False)
    return _browser_call("browser_snapshot", "snapshot", parent_agent, **({"offset": offset} if offset else {}))


# ── 3. browser_screenshot ───────────────────────────────────────────────────

def browser_screenshot(path: str | None = None, parent_agent=None, *, full_page: bool = True) -> str:
    """Take a screenshot of the full page or viewport."""
    if not isinstance(full_page, bool):
        return tool_error("full_page must be a boolean", success=False)
    path, err = _output_path(path, "browser_screenshot", parent_agent)
    if err:
        return tool_error(err, success=False)

    return _browser_call("browser_screenshot", "screenshot", parent_agent, path=path, full_page=full_page)


# ── 4. browser_click ────────────────────────────────────────────────────────

def browser_click(ref: str, parent_agent=None) -> str:
    """Click an element by its ref ID (e.g. 'e5' or '@e5')."""
    clean_ref, err = _clean_ref(ref)
    if err:
        return tool_error(err, success=False)

    return _browser_call("browser_click", "click", parent_agent, ref=clean_ref)


# ── 5. browser_type ─────────────────────────────────────────────────────────

def browser_type(ref: str, text: str, parent_agent=None) -> str:
    """Type text into an input element."""
    clean_ref, err = _clean_ref(ref)
    if err:
        return tool_error(err, success=False)
    if not isinstance(text, str):
        return tool_error("text is required", success=False)

    return _browser_call("browser_type", "type_text", parent_agent, ref=clean_ref, text=text)


# ── 6. browser_scroll ───────────────────────────────────────────────────────

def browser_scroll(direction: str = "down", parent_agent=None, *, ref: str | None = None) -> str:
    """Scroll the page or the nearest scrollable ancestor of a ref."""
    if not isinstance(direction, str) or direction not in {"up", "down", "left", "right"}:
        return tool_error("direction must be up, down, left, or right", success=False)
    if ref is not None:
        ref, err = _clean_ref(ref)
        if err:
            return tool_error(err, success=False)
    return _browser_call("browser_scroll", "scroll", parent_agent, direction=direction,
                         **({"ref": ref} if ref else {}))


# ── 7. browser_press ────────────────────────────────────────────────────────

def browser_press(key: str, parent_agent=None) -> str:
    """Press a keyboard key (e.g. 'Enter', 'Escape', 'ArrowDown')."""
    if not key or not isinstance(key, str):
        return tool_error("key is required", success=False)

    return _browser_call("browser_press", "press", parent_agent, key=key)


# ── 8. browser_download ─────────────────────────────────────────────────────

def browser_download(
    url: str | None = None,
    path: str | None = None,
    ref: str | None = None,
    parent_agent=None,
) -> str:
    """Download a file by direct URL, by clicking a ref, or by returning a queued click download."""
    path, err = _output_path(path, "browser_download", parent_agent)
    if err:
        return tool_error(err, success=False)

    if url is not None and (not isinstance(url, str) or not url.strip()):
        return tool_error("url must be a non-empty string", success=False)
    if url is not None:
        try:
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError
        except ValueError:
            return tool_error("url must be an HTTP or HTTPS URL. Use ref for page-generated downloads.", success=False)
    if ref is not None:
        ref, err = _clean_ref(ref)
        if err:
            return tool_error(err, success=False)
    return _browser_call("browser_download", "download", parent_agent, url=url, path=path, ref=ref)


def _browser_call(tool_name: str, method: str, parent_agent=None, **arguments) -> str:
    """Share session leases, cancellation and result handling across all tools."""
    try:
        _start_cleanup_if_needed()
        session_id = _resolve_session_id(parent_agent)
        with _session_lease(session_id) as sess:
            invoke = getattr(sess.backend, method)
            if method == "navigate_and_snapshot":
                result = invoke(session_id, arguments["url"])
            else:
                result = invoke(session_id, **arguments)
            if method == "navigate_and_snapshot":
                nav, snap = result
                result = dict(snap)
                if nav.get("error"):
                    result.update(success=False, error=BrowserBackend._bounded_text(nav["error"], 1000), error_code="NAVIGATION_FAILED",
                                  completion_unknown=True,
                                  suggestion="Inspect the returned snapshot before deciding whether to navigate again.")
                else:
                    result["action_completed"] = True
                    if not snap.get("success"):
                        result.update(status="partial", suggestion=(
                            "Navigation completed, but observation failed. Refresh browser_snapshot."
                        ))
            return json.dumps(result, ensure_ascii=False)
    except Exception as exc:
        return _browser_exception_result(tool_name, exc, parent_agent)


def browser_tabs(action: str = "list", tab_id: str | None = None, parent_agent=None, *, offset: int = 0) -> str:
    if not isinstance(action, str) or action not in {"list", "new", "select", "close"}:
        return tool_error("action must be list, new, select, or close", success=False)
    if action == "select" and not tab_id:
        return tool_error("tab_id is required for select", success=False)
    if tab_id is not None and (not isinstance(tab_id, str) or not tab_id):
        return tool_error("tab_id must be a non-empty string", success=False)
    if type(offset) is not int or offset < 0 or (offset and action != "list"):
        return tool_error("offset must be a non-negative integer used with list", success=False)
    return _browser_call("browser_tabs", "tabs", parent_agent, action=action, tab_id=tab_id,
                         **({"offset": offset} if offset else {}))


def browser_select(ref: str, values: list[str], parent_agent=None) -> str:
    ref, err = _clean_ref(ref)
    if err:
        return tool_error(err, success=False)
    if not isinstance(values, list) or not values or not all(isinstance(v, str) for v in values):
        return tool_error("values must be a non-empty list of option values", success=False)
    return _browser_call("browser_select", "select", parent_agent, ref=ref, values=values)


def browser_check(ref: str, checked: bool, parent_agent=None) -> str:
    ref, err = _clean_ref(ref)
    if err:
        return tool_error(err, success=False)
    if not isinstance(checked, bool):
        return tool_error("checked must be a boolean", success=False)
    return _browser_call("browser_check", "check", parent_agent, ref=ref, checked=checked)


def browser_hover(ref: str, parent_agent=None) -> str:
    ref, err = _clean_ref(ref)
    if err:
        return tool_error(err, success=False)
    return _browser_call("browser_hover", "hover", parent_agent, ref=ref)


def browser_upload(files: list[str], ref: str | None = None, parent_agent=None) -> str:
    if not isinstance(files, list) or len(files) > 20 or not all(isinstance(f, str) and f for f in files):
        return tool_error("files must contain at most 20 local file paths; [] clears the selection", success=False)
    if ref is not None:
        ref, err = _clean_ref(ref)
        if err:
            return tool_error(err, success=False)
    try:
        from mclaw.tools.file_operations import _checked_path
        from mclaw.tools.file_tools import _resolve_agent_path

        paths = [_checked_path(_resolve_agent_path(f, parent_agent), "read") for f in files]
        for path in paths:
            if not path.is_file():
                return tool_error(f"Upload file does not exist or is not a file: {path}", success=False)
    except Exception as exc:
        return tool_error(f"File safety blocked browser_upload: {exc}", success=False)
    return _browser_call("browser_upload", "upload", parent_agent, files=[str(p) for p in paths], ref=ref)


def browser_handle_dialog(accept: bool, prompt_text: str | None = None, parent_agent=None) -> str:
    if not isinstance(accept, bool) or (prompt_text is not None and not isinstance(prompt_text, str)):
        return tool_error("accept must be a boolean; prompt_text must be a string", success=False)
    return _browser_call("browser_handle_dialog", "handle_dialog", parent_agent,
                         accept=accept, prompt_text=prompt_text)


def browser_wait_for(
    text: str | None = None, ref: str | None = None, state: str = "visible",
    timeout_ms: int = 10_000, parent_agent=None,
) -> str:
    if (text is None) == (ref is None):
        return tool_error("Provide exactly one of text or ref", success=False)
    if text is not None and (not isinstance(text, str) or not text.strip()):
        return tool_error("text must be a non-empty string", success=False)
    if ref is not None:
        ref, err = _clean_ref(ref)
        if err:
            return tool_error(err, success=False)
    if not isinstance(state, str) or state not in {"visible", "hidden"}:
        return tool_error("state must be visible or hidden", success=False)
    if type(timeout_ms) is not int or not 1 <= timeout_ms <= 30_000:
        return tool_error("timeout_ms must be an integer from 1 to 30000", success=False)
    return _browser_call("browser_wait_for", "wait_for", parent_agent,
                         text=text, ref=ref, state=state, timeout_ms=timeout_ms)


def browser_back(parent_agent=None) -> str:
    return _browser_call("browser_back", "back", parent_agent)


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
            "is not needed immediately after navigation. Use refs from the latest returned "
            "snapshot; every new snapshot invalidates earlier refs."
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
        "description": "Refresh the current viewport, including visible frames and open shadow roots. Replaces all earlier element refs. If next_offset is returned, pass it as offset to inspect remaining controls in the same viewport.",
        "params": {
            "type": "object",
            "properties": {"offset": {"type": "integer", "minimum": 0, "default": 0}},
        },
        "handler": lambda args, **kw: browser_snapshot(parent_agent=kw.get("parent_agent"), offset=args.get("offset", 0)),
        "emoji": "📸",
    },
    {
        "name": "browser_screenshot",
        "short_description": "Save a full-page or viewport screenshot",
        "description": "Save a screenshot (PNG by default, JPEG for .jpg paths) and return its file path. Set full_page=false for the current viewport.",
        "params": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Output file path. Defaults to the M-Claw downloads directory.",
                },
                "full_page": {"type": "boolean", "default": True},
            },
        },
        "handler": lambda args, **kw: browser_screenshot(
            path=args.get("path"), full_page=args.get("full_page", True), parent_agent=kw.get("parent_agent")
        ),
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
            args.get("ref", ""), args.get("text"), parent_agent=kw.get("parent_agent")
        ),
        "emoji": "⌨️",
    },
    {
        "name": "browser_scroll",
        "short_description": "Scroll the current page",
        "description": "Scroll the current page and return a compact snapshot of the new viewport. Supply ref to scroll its nearest scrollable ancestor, including in an iframe or open shadow root.",
        "params": {
            "type": "object",
            "properties": {
                "direction": {
                    "type": "string",
                    "enum": ["up", "down", "left", "right"],
                    "default": "down",
                    "description": "Scroll direction.",
                },
                "ref": {"type": "string", "description": "Optional ref inside the intended scroll area."},
            },
        },
        "handler": lambda args, **kw: browser_scroll(args.get("direction", "down"), parent_agent=kw.get("parent_agent"), ref=args.get("ref")),
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
            "captured download, including delayed downloads and downloads from keyboard actions. "
            "If saving fails with download_retained=true, retry using only path to avoid triggering the action again."
        ),
        "params": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Direct HTTP or HTTPS file URL. Preserves the active tab; inline files are saved too.",
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
    {
        "name": "browser_tabs",
        "short_description": "List, create, select, or close browser tabs",
        "description": (
            "Manage tabs in this browser session. New creates and selects a blank tab; use "
            "browser_navigate next. Select requires tab_id. Close defaults to the selected tab. "
            "Popups appear in the tab list without changing the selected tab. List preserves refs; "
            "other actions return a fresh snapshot. List returns at most 20 tabs; pass next_offset as offset for more."
        ),
        "params": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "new", "select", "close"], "default": "list"},
                "tab_id": {"type": "string", "description": "Tab id returned by the browser, such as t1."},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
            },
        },
        "handler": lambda args, **kw: browser_tabs(
            args.get("action", "list"), args.get("tab_id"), parent_agent=kw.get("parent_agent"), offset=args.get("offset", 0)
        ),
        "emoji": "🗂️",
    },
    {
        "name": "browser_select",
        "short_description": "Select native dropdown options by value",
        "description": (
            "Select options in a native select element by snapshot ref and return a fresh snapshot. "
            "Use option values shown in the snapshot; pass multiple values for a multi-select. "
            "For custom dropdowns, use click and keyboard interactions."
        ),
        "params": {
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "Select element ref from the latest snapshot."},
                "values": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            },
            "required": ["ref", "values"],
        },
        "handler": lambda args, **kw: browser_select(
            args.get("ref", ""), args.get("values"), parent_agent=kw.get("parent_agent")
        ),
        "emoji": "📋",
    },
    {
        "name": "browser_check",
        "short_description": "Set a checkbox or radio button state",
        "description": "Set checked=true or false on a checkbox ref, or select a radio with checked=true, then return a fresh snapshot. To clear a selected radio, select another radio in its group. Already-matching state stays unchanged.",
        "params": {
            "type": "object",
            "properties": {"ref": {"type": "string"}, "checked": {"type": "boolean"}},
            "required": ["ref", "checked"],
        },
        "handler": lambda args, **kw: browser_check(
            args.get("ref", ""), args.get("checked"), parent_agent=kw.get("parent_agent")
        ),
        "emoji": "☑️",
    },
    {
        "name": "browser_hover",
        "short_description": "Hover over an element by snapshot ref",
        "description": "Hover over a ref to reveal menus or tooltips, then return a fresh snapshot.",
        "params": {"type": "object", "properties": {"ref": {"type": "string"}}, "required": ["ref"]},
        "handler": lambda args, **kw: browser_hover(args.get("ref", ""), parent_agent=kw.get("parent_agent")),
        "emoji": "🖱️",
    },
    {
        "name": "browser_upload",
        "short_description": "Choose local files for a page upload",
        "description": (
            "Set local files on a file input ref, or omit ref to use the file chooser opened by "
            "the preceding click in this tab. Relative paths use the session workspace. An empty "
            "files array clears the selection. This may trigger the site's automatic upload. "
            "Returns a fresh snapshot."
        ),
        "params": {
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "Optional file input ref from the latest snapshot."},
                "files": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
            },
            "required": ["files"],
        },
        "handler": lambda args, **kw: browser_upload(
            args.get("files"), args.get("ref"), parent_agent=kw.get("parent_agent")
        ),
        "emoji": "⬆️",
    },
    {
        "name": "browser_handle_dialog",
        "short_description": "Set the response for the next browser dialog",
        "description": (
            "BEFORE triggering an alert, confirm, or prompt, set its one-time response in the "
            "selected tab. Then click or press the control that opens it. prompt_text supplies "
            "an accepted prompt's answer. Unconfigured dialogs are dismissed and reported in "
            "the next snapshot. The response expires after the next action in that tab or 60 seconds. "
            "Does not refresh or invalidate refs."
        ),
        "params": {
            "type": "object",
            "properties": {"accept": {"type": "boolean"}, "prompt_text": {"type": "string"}},
            "required": ["accept"],
        },
        "handler": lambda args, **kw: browser_handle_dialog(
            args.get("accept"), args.get("prompt_text"), parent_agent=kw.get("parent_agent")
        ),
        "emoji": "💬",
    },
    {
        "name": "browser_wait_for",
        "short_description": "Wait for page text or an element to appear or disappear",
        "description": (
            "Wait for exactly one of text (substring in any frame) or a current snapshot ref "
            "to become visible or hidden. Use text for content not yet present. Bounded and "
            "cancellable; returns a fresh snapshot on success or timeout."
        ),
        "params": {
            "type": "object",
            "properties": {
                "text": {"type": "string"}, "ref": {"type": "string"},
                "state": {"type": "string", "enum": ["visible", "hidden"], "default": "visible"},
                "timeout_ms": {"type": "integer", "minimum": 1, "maximum": 30000, "default": 10000},
            },
        },
        "handler": lambda args, **kw: browser_wait_for(
            text=args.get("text"), ref=args.get("ref"), state=args.get("state", "visible"),
            timeout_ms=args.get("timeout_ms", 10000), parent_agent=kw.get("parent_agent")
        ),
        "emoji": "⏳",
    },
    {
        "name": "browser_back",
        "short_description": "Go back in the selected tab's history",
        "description": "Go back one entry in the selected tab's history and return a fresh snapshot.",
        "params": {"type": "object", "properties": {}},
        "handler": lambda args, **kw: browser_back(parent_agent=kw.get("parent_agent")),
        "emoji": "↩️",
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
