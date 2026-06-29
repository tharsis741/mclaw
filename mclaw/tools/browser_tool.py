# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Browser Tool — 8 sub-tools for web browsing via Playwright.

Sessions are managed per-agent-turn and auto-cleaned after inactivity.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from typing import Any, Dict, Optional

from mclaw.tools.browser_backend import BrowserBackend
from mclaw.tools.registry import registry, tool_error

logger = logging.getLogger(__name__)

# ── Session management ──────────────────────────────────────────────────────

_SESSION_TIMEOUT = 300.0  # seconds
_CLEANUP_INTERVAL = 30.0

_browser_sessions: Dict[str, "BrowserSession"] = {}
_sessions_lock = threading.Lock()
_cleanup_started = False
_REF_RE = re.compile(r"^e\d+$")


class BrowserSession:
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.backend = BrowserBackend(headless=True)
        self.last_activity = time.time()

    def touch(self) -> None:
        self.last_activity = time.time()

    def is_expired(self) -> bool:
        return time.time() - self.last_activity > _SESSION_TIMEOUT

    def close(self) -> None:
        try:
            self.backend.stop()
        except Exception as e:
            logger.debug("Error closing session %s: %s", self.session_id, e)


def _get_or_create_session(session_id: str) -> BrowserSession:
    with _sessions_lock:
        sess = _browser_sessions.get(session_id)
        if sess is None:
            logger.info("Creating browser session: %s", session_id)
            sess = BrowserSession(session_id)
            _browser_sessions[session_id] = sess
        sess.touch()
        return sess


def _cleanup_loop() -> None:
    """Background thread: close expired sessions."""
    while True:
        time.sleep(_CLEANUP_INTERVAL)
        with _sessions_lock:
            expired = [
                sid for sid, sess in _browser_sessions.items()
                if sess.is_expired()
            ]
            for sid in expired:
                logger.info("Browser session expired, closing: %s", sid)
                try:
                    _browser_sessions[sid].close()
                except Exception as e:
                    logger.warning("Cleanup error for %s: %s", sid, e)
                _browser_sessions.pop(sid, None)


def _start_cleanup_if_needed() -> None:
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
    """Allow '@e5' or 'e5' → 'e5'."""
    return ref.lstrip("@").strip()


def _clean_ref(ref: str) -> tuple[Optional[str], Optional[str]]:
    if not ref or not isinstance(ref, str):
        return None, "ref is required"
    clean = _strip_ref_prefix(ref)
    if not _REF_RE.fullmatch(clean):
        return None, "ref must be an element id from browser_snapshot, e.g. 'e5' or '@e5'"
    return clean, None


def _output_path_error(path: Optional[str], tool_name: str, parent_agent=None) -> Optional[str]:
    if not path:
        return None
    try:
        from mclaw.tools import file_operations as ops

        if ops._check_sensitive_path_write(path):
            return f"File safety blocked {tool_name}: sensitive path is not writable: {path}"
    except Exception as exc:
        return f"File safety blocked {tool_name}: invalid path {path!r}: {exc}"

    try:
        from mclaw.tools.file_tools import (
            FILE_WRITE_BLOCKED_PREFIXES,
            _check_delegation_path,
            _skill_store_mutation_error,
        )

        normalized = ops._normalize_path(path)
        normalized_lower = normalized.lower()
        for blocked_prefix in FILE_WRITE_BLOCKED_PREFIXES:
            blocked = str(blocked_prefix).rstrip("/\\").lower()
            if normalized_lower == blocked or normalized_lower.startswith(blocked + os.sep):
                return f"File safety blocked {tool_name}: sensitive path is not writable: {path}"

        blocked = _skill_store_mutation_error(path, tool_name)
        if blocked:
            return blocked
        safe, err = _check_delegation_path(path, parent_agent)
        if not safe:
            return err
    except Exception as exc:
        logger.debug("Browser output path policy check failed: %s", exc)
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
            "url": nav.get("url", ""),
            "title": nav.get("title", ""),
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
    """Get the current page accessibility snapshot."""
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

def browser_screenshot(path: Optional[str] = None, parent_agent=None) -> str:
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
    url: Optional[str] = None,
    path: Optional[str] = None,
    ref: Optional[str] = None,
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


# ── Requirements check ──────────────────────────────────────────────────────

def _has_chromium_browser(root: "Path") -> bool:
    return any(root.glob("chromium-*")) or any(root.glob("chrome-*")) or any(root.glob("**/chrome.exe"))


def _find_playwright_browsers_root(config: dict | None = None) -> tuple[bool, str]:
    import os
    from pathlib import Path

    env_value = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip().strip('"')
    if env_value:
        root = Path(env_value)
        if not root.exists():
            return False, f"{env_value}; path does not exist"
        return _has_chromium_browser(root), f"{env_value}; chromium={'yes' if _has_chromium_browser(root) else 'no'}"

    candidates: list[Path] = []
    local_appdata = os.environ.get("LOCALAPPDATA", "").strip()
    if local_appdata:
        candidates.append(Path(local_appdata) / "ms-playwright")
    candidates.append(Path.home() / ".cache" / "ms-playwright")
    if os.name == "nt":
        candidates.append(Path.home() / "AppData" / "Local" / "ms-playwright")

    seen: set[str] = set()
    for root in candidates:
        key = os.path.normcase(str(root))
        if key in seen:
            continue
        seen.add(key)
        if root.exists():
            return _has_chromium_browser(root), f"{root}; chromium={'yes' if _has_chromium_browser(root) else 'no'}; source=default cache"
    return False, "not found in PLAYWRIGHT_BROWSERS_PATH or default Playwright cache"


def diagnose_browser_requirements(config: dict | None = None) -> dict:
    try:
        import playwright  # noqa: F401
    except ImportError:
        return {
            "available": False,
            "reason": "Playwright Python package is missing",
            "fix": "Install playwright in the active Python environment.",
        }

    has_chromium, detail = _find_playwright_browsers_root(config)
    if not has_chromium:
        return {
            "available": False,
            "reason": detail,
            "fix": "Run python -m playwright install chromium.",
        }
    return {"available": True, "reason": detail, "fix": ""}


def check_browser_requirements(config: dict | None = None) -> bool:
    """Return True if Playwright and a Chromium browser binary are available."""
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False

    has_chromium, _ = _find_playwright_browsers_root(config)
    return has_chromium


# ── Registry ────────────────────────────────────────────────────────────────

_BROWSER_TOOLS = [
    {
        "name": "browser_navigate",
        "description": (
            "Navigate the browser to a URL. Returns the page title and an accessibility "
            "snapshot with numbered elements you can interact with.\n\n"
            "Use this as the first step when the user asks you to visit a website."
        ),
        "params": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL to navigate to. Must include scheme (http:// or https://).",
                },
            },
            "required": ["url"],
        },
        "handler": lambda args, **kw: browser_navigate(args.get("url", ""), parent_agent=kw.get("parent_agent")),
        "emoji": "🌐",
    },
    {
        "name": "browser_snapshot",
        "description": (
            "Capture an accessibility snapshot of the current page. Shows interactive "
            "elements (links, buttons, inputs) with ref IDs like [3] link 'Login'.\n\n"
            "Use this to understand the page structure before clicking or typing."
        ),
        "params": {
            "type": "object",
            "properties": {},
        },
        "handler": lambda args, **kw: browser_snapshot(parent_agent=kw.get("parent_agent")),
        "emoji": "📸",
    },
    {
        "name": "browser_screenshot",
        "description": (
            "Take a full-page screenshot and save it to a file. Returns the file path.\n\n"
            "Use this when the user asks about visual aspects of a page (colors, layout, etc.) "
            "or when you need to share what you see. The screenshot can then be analysed with vision_analyze."
        ),
        "params": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Optional file path to save the screenshot. If omitted, a default path in the M-Claw downloads directory is used.",
                },
            },
        },
        "handler": lambda args, **kw: browser_screenshot(path=args.get("path"), parent_agent=kw.get("parent_agent")),
        "emoji": "📷",
    },
    {
        "name": "browser_click",
        "description": (
            "Click an element on the page by its ref ID. Ref IDs come from browser_snapshot "
            "or browser_navigate output (e.g. 'e5' or '@e5').\n\n"
            "After clicking, the page may navigate — the result includes the new snapshot."
        ),
        "params": {
            "type": "object",
            "properties": {
                "ref": {
                    "type": "string",
                    "description": "The element ref ID to click, e.g. 'e5' or '@e5'.",
                },
            },
            "required": ["ref"],
        },
        "handler": lambda args, **kw: browser_click(args.get("ref", ""), parent_agent=kw.get("parent_agent")),
        "emoji": "👆",
    },
    {
        "name": "browser_type",
        "description": (
            "Type text into an input field identified by its ref ID.\n\n"
            "Use browser_snapshot first to find the input's ref ID, then call this tool."
        ),
        "params": {
            "type": "object",
            "properties": {
                "ref": {
                    "type": "string",
                    "description": "The input element ref ID, e.g. 'e3'.",
                },
                "text": {
                    "type": "string",
                    "description": "The text to type into the input field.",
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
        "description": (
            "Scroll the page in a direction.\n\n"
            "Use this when the snapshot says content is truncated or when you need to "
            "see elements further down the page."
        ),
        "params": {
            "type": "object",
            "properties": {
                "direction": {
                    "type": "string",
                    "enum": ["up", "down", "left", "right"],
                    "description": "Scroll direction. Default is down.",
                },
            },
        },
        "handler": lambda args, **kw: browser_scroll(args.get("direction", "down"), parent_agent=kw.get("parent_agent")),
        "emoji": "📜",
    },
    {
        "name": "browser_press",
        "description": (
            "Press a keyboard key (e.g. 'Enter', 'Escape', 'ArrowDown', 'Tab').\n\n"
            "Useful for form submission (Enter), closing modals (Escape), or keyboard navigation."
        ),
        "params": {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "Key to press. Examples: 'Enter', 'Escape', 'ArrowDown', 'Tab', 'Backspace'.",
                },
            },
            "required": ["key"],
        },
        "handler": lambda args, **kw: browser_press(args.get("key", ""), parent_agent=kw.get("parent_agent")),
        "emoji": "🔘",
    },
    {
        "name": "browser_download",
        "description": (
            "Download a file. Modes:\n"
            "1. Provide a direct download URL — the browser navigates to it and captures the file.\n"
            "2. Provide a ref from browser_snapshot — the browser clicks it and waits for the download.\n"
            "3. Omit URL/ref — return the latest download already captured by browser_click.\n\n"
            "Returns the saved file path and size."
        ),
        "params": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Optional direct download URL. If omitted, waits for a download triggered by a previous action.",
                },
                "ref": {
                    "type": "string",
                    "description": "Optional element ref ID for a link/button that triggers a download, e.g. 'e5' or '@e5'.",
                },
                "path": {
                    "type": "string",
                    "description": "Optional save file path or existing directory. If omitted, saves to the M-Claw downloads directory.",
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
        description="浏览器自动化工具",
        emoji=_tool["emoji"],
        max_result_size_chars=12_000,
        diagnose_fn=diagnose_browser_requirements,
    )
