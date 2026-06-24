"""Browser Backend — Playwright wrapper for headless Chromium automation.

Provides a synchronous API over Playwright's async internals. Each session
gets an isolated BrowserContext + Page. Pages are snapshotted via in-page
JavaScript that tags interactive elements with data-mclaw-ref IDs.
"""

from __future__ import annotations

import logging
import os
import queue
import shutil
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from mclaw.constants import get_mclaw_home

logger = logging.getLogger(__name__)

_DEFAULT_VIEWPORT = {"width": 1280, "height": 720}
_DEFAULT_TIMEOUT = 30_000  # ms
_MAX_SNAPSHOT_CHARS = 8_000


def _default_downloads_dir() -> Path:
    return get_mclaw_home() / "downloads"


def _browser_executable_path() -> str:
    env_value = os.environ.get("MCLAW_BROWSER_EXECUTABLE_PATH", "").strip().strip('"')
    if env_value:
        return env_value
    if os.name != "nt":
        for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "microsoft-edge"):
            found = shutil.which(name)
            if found:
                return found
    return ""

# JS 片段：标记交互元素并返回元数据。
_SNAPSHOT_JS = """
(() => {
    // Clear old refs
    document.querySelectorAll('[data-mclaw-ref]').forEach(el =>
        el.removeAttribute('data-mclaw-ref')
    );

    const selectors = [
        'a', 'button', 'input:not([type="hidden"])', 'textarea', 'select',
        '[role="button"]', '[role="link"]', '[role="checkbox"]',
        '[role="textbox"]', '[role="searchbox"]', 'label'
    ];
    const candidates = document.querySelectorAll(selectors.join(','));
    const elements = [];

    candidates.forEach((el, idx) => {
        const rect = el.getBoundingClientRect();
        const style = window.getComputedStyle(el);
        if (rect.width === 0 || rect.height === 0) return;
        if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return;

        const ref = 'e' + (idx + 1);
        el.setAttribute('data-mclaw-ref', ref);

        const tag = el.tagName.toLowerCase();
        let text = '';
        if (tag === 'input' || tag === 'textarea') {
            text = el.placeholder || el.value || el.getAttribute('aria-label') || '';
        } else {
            text = (el.textContent || '').trim().slice(0, 100);
            if (!text) text = el.getAttribute('aria-label') || el.getAttribute('title') || '';
        }

        elements.push({
            ref: ref,
            tag: tag,
            type: el.type || '',
            text: text,
            href: el.href || '',
            role: el.getAttribute('role') || ''
        });
    });

    const headings = [];
    document.querySelectorAll('h1, h2, h3, h4').forEach(h => {
        const txt = (h.textContent || '').trim();
        if (txt) headings.push({level: parseInt(h.tagName[1]), text: txt.slice(0, 100)});
    });

    const textBlocks = [];
    const seenText = new Set();
    const textSelectors = [
        'main p', 'article p', 'p', 'li', 'td', 'th',
        '[role="article"]', '[role="main"]'
    ];
    document.querySelectorAll(textSelectors.join(',')).forEach(el => {
        if (el.closest('a, button, input, textarea, select, nav, header, footer')) return;
        const rect = el.getBoundingClientRect();
        const style = window.getComputedStyle(el);
        if (rect.width === 0 || rect.height === 0) return;
        if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return;
        const txt = (el.textContent || '').replace(/\\s+/g, ' ').trim();
        if (!txt || txt.length < 2 || seenText.has(txt)) return;
        seenText.add(txt);
        textBlocks.push(txt.slice(0, 220));
    });

    return {elements, headings, textBlocks: textBlocks.slice(0, 40)};
})()
"""


def _safe_page_info(page) -> Tuple[str, str]:
    """Safely read page url and title, swallowing errors."""
    url = ""
    title = ""
    try:
        url = page.url
    except Exception:
        pass
    try:
        title = page.title()
    except Exception:
        pass
    return url, title


@dataclass
class _SessionState:
    context: Any
    page: Any
    ref_map: Dict[str, Any] = field(default_factory=dict)
    last_activity: float = field(default_factory=time.time)
    downloads_dir: Path = field(default_factory=_default_downloads_dir)
    downloads: List[Path] = field(default_factory=list)
    download_errors: List[str] = field(default_factory=list)


class BrowserBackend:
    """Lazy-started Playwright + Chromium backend with per-session isolation."""

    def __init__(self, headless: bool = True):
        self._headless = headless
        self._playwright: Optional[Any] = None
        self._browser: Optional[Any] = None
        self._sessions: Dict[str, _SessionState] = {}
        self._lock = threading.Lock()
        self._started = False
        self._task_queue: "queue.Queue[tuple[Callable, tuple, dict, Future] | None]" = queue.Queue()
        self._worker_lock = threading.Lock()
        self._worker_thread: Optional[threading.Thread] = None
        self._worker_thread_id: Optional[int] = None

    # Playwright's sync API is thread-affine: all operations on a browser/page
    # must run on the same thread that started sync_playwright().
    def _ensure_worker(self) -> None:
        with self._worker_lock:
            if self._worker_thread and self._worker_thread.is_alive():
                return
            self._task_queue = queue.Queue()
            self._worker_thread = threading.Thread(
                target=self._worker_loop,
                daemon=True,
                name="mclaw-browser-backend",
            )
            self._worker_thread.start()

    def _worker_loop(self) -> None:
        self._worker_thread_id = threading.get_ident()
        while True:
            task = self._task_queue.get()
            if task is None:
                break
            fn, args, kwargs, future = task
            if future.cancelled():
                continue
            try:
                future.set_result(fn(*args, **kwargs))
            except Exception as exc:
                future.set_exception(exc)
        self._worker_thread_id = None

    def _run_on_worker(self, fn: Callable, *args, **kwargs):
        if threading.get_ident() == self._worker_thread_id:
            return fn(*args, **kwargs)
        self._ensure_worker()
        future: Future = Future()
        self._task_queue.put((fn, args, kwargs, future))
        return future.result()

    def _shutdown_worker(self) -> None:
        thread = self._worker_thread
        if not thread or not thread.is_alive():
            return
        self._task_queue.put(None)
        thread.join(timeout=5)
        if thread.is_alive():
            logger.debug("Browser worker did not stop within timeout")
            return
        with self._worker_lock:
            if self._worker_thread is thread:
                self._worker_thread = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        return self._run_on_worker(self._start_impl)

    def _start_impl(self) -> None:
        if self._started:
            return
        with self._lock:
            if self._started:
                return
            try:
                from playwright.sync_api import sync_playwright
            except ImportError as exc:
                raise RuntimeError(
                    "playwright package is required. Install it with:\n"
                    "  pip install playwright\n"
                    "  playwright install chromium"
                ) from exc

            self._playwright = sync_playwright().start()
            try:
                executable_path = _browser_executable_path()
                launch_kwargs = {"headless": self._headless}
                if executable_path:
                    launch_kwargs["executable_path"] = executable_path
                self._browser = self._playwright.chromium.launch(**launch_kwargs)
            except Exception as exc:
                try:
                    if self._playwright:
                        self._playwright.stop()
                except Exception:
                    logger.debug("Error stopping Playwright after launch failure", exc_info=True)
                self._playwright = None
                raise RuntimeError(
                    "Failed to launch Chromium. Run 'playwright install chromium' first, "
                    "or set MCLAW_BROWSER_EXECUTABLE_PATH to a system Chromium/Chrome binary.\n"
                    f"Error: {exc}"
                ) from exc
            self._started = True
            logger.info("BrowserBackend started (headless=%s)", self._headless)

    def stop(self) -> None:
        if not self._worker_thread or not self._worker_thread.is_alive():
            return
        if threading.get_ident() == self._worker_thread_id:
            self._stop_impl()
            return
        try:
            self._run_on_worker(self._stop_impl)
        finally:
            self._shutdown_worker()

    def _stop_impl(self) -> None:
        with self._lock:
            for sid, state in list(self._sessions.items()):
                try:
                    state.context.close()
                except Exception as e:
                    logger.debug("Error closing context %s: %s", sid, e)
            self._sessions.clear()

            if self._browser:
                try:
                    self._browser.close()
                except Exception as e:
                    logger.debug("Error closing browser: %s", e)
                self._browser = None

            if self._playwright:
                try:
                    self._playwright.stop()
                except Exception as e:
                    logger.debug("Error stopping playwright: %s", e)
                self._playwright = None

            self._started = False
            logger.info("BrowserBackend stopped")

    def _ensure_started_impl(self) -> None:
        if not self._started:
            self._start_impl()

    # ── Session management ────────────────────────────────────────────────────

    def get_or_create_page(self, session_id: str) -> Tuple[Any, _SessionState]:
        return self._run_on_worker(self._get_or_create_page_impl, session_id)

    def _get_or_create_page_impl(self, session_id: str) -> Tuple[Any, _SessionState]:
        self._ensure_started_impl()
        with self._lock:
            if session_id in self._sessions:
                state = self._sessions[session_id]
                state.last_activity = time.time()
                return state.page, state

            logger.info("Creating new browser session: %s", session_id)
            context = self._browser.new_context(
                viewport=_DEFAULT_VIEWPORT,
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                accept_downloads=True,
            )
            page = context.new_page()
            page.set_default_timeout(_DEFAULT_TIMEOUT)

            downloads_dir = _default_downloads_dir()
            downloads_dir.mkdir(parents=True, exist_ok=True)

            state = _SessionState(
                context=context,
                page=page,
                downloads_dir=downloads_dir,
            )
            self._sessions[session_id] = state
            return page, state

    def close_session(self, session_id: str) -> None:
        return self._run_on_worker(self._close_session_impl, session_id)

    def _close_session_impl(self, session_id: str) -> None:
        with self._lock:
            state = self._sessions.pop(session_id, None)
            if state:
                try:
                    state.context.close()
                except Exception as e:
                    logger.debug("Error closing session %s: %s", session_id, e)
                logger.info("Closed browser session: %s", session_id)

    def list_sessions(self) -> List[str]:
        return self._run_on_worker(self._list_sessions_impl)

    def _list_sessions_impl(self) -> List[str]:
        with self._lock:
            return list(self._sessions.keys())

    def get_session_age(self, session_id: str) -> float:
        return self._run_on_worker(self._get_session_age_impl, session_id)

    def _get_session_age_impl(self, session_id: str) -> float:
        with self._lock:
            state = self._sessions.get(session_id)
            return time.time() - state.last_activity if state else float("inf")

    # ── Navigation ────────────────────────────────────────────────────────────

    def navigate(self, session_id: str, url: str) -> Dict[str, str]:
        return self._run_on_worker(self._navigate_impl, session_id, url)

    def _navigate_impl(self, session_id: str, url: str) -> Dict[str, str]:
        page, state = self._get_or_create_page_impl(session_id)
        logger.info("Navigating to %s", url)
        try:
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_timeout(500)
            state.last_activity = time.time()
            url_val, title_val = _safe_page_info(page)
            return {"url": url_val, "title": title_val}
        except Exception as e:
            logger.warning("Navigation error for %s: %s", url, e)
            url_val, title_val = _safe_page_info(page)
            return {"url": url_val, "title": title_val, "error": str(e)}

    # ── Snapshot ──────────────────────────────────────────────────────────────

    def snapshot(self, session_id: str) -> Dict[str, Any]:
        return self._run_on_worker(self._snapshot_impl, session_id)

    def _snapshot_impl(self, session_id: str) -> Dict[str, Any]:
        page, state = self._get_or_create_page_impl(session_id)
        state.last_activity = time.time()
        url_val, title_val = _safe_page_info(page)

        try:
            result = page.evaluate(_SNAPSHOT_JS)
        except Exception as e:
            logger.warning("JS snapshot failed: %s", e)
            return {
                "url": url_val,
                "title": title_val,
                "snapshot": f"Snapshot error: {e}\n\nURL: {url_val}\nTitle: {title_val}",
                "ref_count": 0,
            }

        elements = result.get("elements", [])
        headings = result.get("headings", [])
        text_blocks = result.get("textBlocks", [])

        lines: List[str] = []
        ref_map: Dict[str, Any] = {}

        # 提取标题作为页面上下文。
        for h in headings[:8]:
            indent = "  " * (h["level"] - 1)
            lines.append(f"{indent}#{h['level']} {h['text']}")

        if headings and elements:
            lines.append("")

        for txt in text_blocks[:30]:
            lines.append(f"text: {txt}")

        if text_blocks and elements:
            lines.append("")

        for el in elements:
            ref = el["ref"]
            tag = el.get("tag", "")
            text = el.get("text", "") or ""
            role = el.get("role", "")
            href = el.get("href", "")
            etype = el.get("type", "")

            label = text or ""
            if len(label) > 60:
                label = label[:57] + "..."

            if tag == "a" or role == "link":
                line = f"[{ref}] link '{label}'"
                if href and len(href) < 60:
                    line += f"  → {href}"
            elif tag == "button" or role == "button":
                line = f"[{ref}] button '{label}'"
            elif tag in ("input", "textarea") or role in ("textbox", "searchbox"):
                itype = etype or tag
                line = f"[{ref}] {itype} '{label}'"
            elif tag == "select":
                line = f"[{ref}] select '{label}'"
            elif role == "checkbox":
                line = f"[{ref}] checkbox '{label}'"
            elif tag == "label":
                line = f"[{ref}] label '{label}'"
            else:
                line = f"[{ref}] {tag} '{label}'"

            lines.append(line)
            ref_map[ref] = el

        state.ref_map = ref_map

        snapshot_text = f"url: {url_val}\ntitle: {title_val}\n\n"
        snapshot_text += "\n".join(lines)

        if not elements:
            snapshot_text += "\n[No interactive elements detected on this page.]"

        if len(snapshot_text) > _MAX_SNAPSHOT_CHARS:
            trunc = snapshot_text[:_MAX_SNAPSHOT_CHARS]
            last_nl = trunc.rfind("\n")
            if last_nl > _MAX_SNAPSHOT_CHARS // 2:
                trunc = trunc[:last_nl]
            snapshot_text = (
                f"{trunc}\n\n"
                f"[Snapshot truncated: {len(elements)} interactive elements. "
                f"Use browser_scroll to see more.]"
            )

        return {
            "url": url_val,
            "title": title_val,
            "snapshot": snapshot_text,
            "ref_count": len(elements),
        }

    # ── Interaction ───────────────────────────────────────────────────────────

    def _get_element(self, session_id: str, ref: str) -> Optional[Any]:
        """Resolve a ref ID to a Playwright Locator via data-mclaw-ref attribute."""
        page, _ = self._get_or_create_page_impl(session_id)
        locator = page.locator(f'[data-mclaw-ref="{ref}"]')
        try:
            if locator.count() > 0:
                return locator.first
        except Exception as e:
            logger.debug("Element resolution error for @%s: %s", ref, e)
        return None

    def _unique_path(self, dest: Path) -> Path:
        if not dest.exists():
            return dest
        stem = dest.stem
        suffix = dest.suffix
        parent = dest.parent
        for i in range(1, 1000):
            candidate = parent / f"{stem}_{int(time.time())}_{i}{suffix}"
            if not candidate.exists():
                return candidate
        return parent / f"{stem}_{int(time.time() * 1000)}{suffix}"

    def _download_destination(self, state: _SessionState, suggested: str, path: Optional[str] = None) -> Path:
        safe_name = Path(suggested or "download").name or "download"
        if not path:
            dest = state.downloads_dir / safe_name
        else:
            raw = str(path)
            target = Path(raw).expanduser()
            if raw.endswith(("/", "\\")) or (target.exists() and target.is_dir()):
                dest = target / safe_name
            else:
                dest = target
        dest.parent.mkdir(parents=True, exist_ok=True)
        return self._unique_path(dest)

    def _save_download(
        self,
        state: _SessionState,
        download: Any,
        path: Optional[str] = None,
        *,
        queue_download: bool = True,
    ) -> Path:
        suggested = download.suggested_filename or "download"
        dest = self._download_destination(state, suggested, path=path)
        download.save_as(str(dest))
        if queue_download:
            state.downloads.append(dest)
        logger.info("Download saved: %s (%d bytes)", dest, dest.stat().st_size)
        return dest

    def _download_result(self, path: Path) -> Dict[str, Any]:
        return {
            "success": True,
            "filename": path.name,
            "path": str(path.resolve()),
            "size": path.stat().st_size,
        }

    def _move_download(self, source: Path, state: _SessionState, path: Optional[str]) -> Path:
        if not path:
            return source
        dest = self._download_destination(state, source.name, path=path)
        if source.resolve() == dest.resolve():
            return source
        shutil.move(str(source), str(dest))
        return dest

    def click(self, session_id: str, ref: str) -> Dict[str, Any]:
        return self._run_on_worker(self._click_impl, session_id, ref)

    def _click_impl(self, session_id: str, ref: str) -> Dict[str, Any]:
        page, state = self._get_or_create_page_impl(session_id)
        element = self._get_element(session_id, ref)

        if not element:
            snap = self._snapshot_impl(session_id)
            url_val, title_val = _safe_page_info(page)
            return {
                "success": False,
                "error": f"Element @{ref} not found. Current snapshot:\n{snap['snapshot']}",
                "url": url_val,
                "title": title_val,
            }

        logger.info("Clicking element @%s", ref)
        captured_download: Optional[Path] = None

        def _handle_download(download):
            nonlocal captured_download
            try:
                captured_download = self._save_download(state, download)
            except Exception as e:
                state.download_errors.append(str(e))
                logger.warning("Download save error after click: %s", e)

        page.on("download", _handle_download)
        try:
            element.click(timeout=5_000)
            page.wait_for_timeout(800)
            state.last_activity = time.time()
        except Exception as e:
            logger.warning("Click error: %s", e)
            url_val, title_val = _safe_page_info(page)
            return {
                "success": False,
                "error": f"Click failed: {e}",
                "url": url_val,
                "title": title_val,
            }
        finally:
            try:
                page.remove_listener("download", _handle_download)
            except Exception:
                pass

        snap = self._snapshot_impl(session_id)
        result = {
            "success": True,
            "url": snap["url"],
            "title": snap["title"],
            "snapshot": snap["snapshot"],
        }
        if captured_download and captured_download.exists():
            result["download"] = self._download_result(captured_download)
        return result

    def type_text(self, session_id: str, ref: str, text: str) -> Dict[str, Any]:
        return self._run_on_worker(self._type_text_impl, session_id, ref, text)

    def _type_text_impl(self, session_id: str, ref: str, text: str) -> Dict[str, Any]:
        page, state = self._get_or_create_page_impl(session_id)
        element = self._get_element(session_id, ref)

        if not element:
            snap = self._snapshot_impl(session_id)
            url_val, title_val = _safe_page_info(page)
            return {
                "success": False,
                "error": f"Input element @{ref} not found. Current snapshot:\n{snap['snapshot']}",
                "url": url_val,
                "title": title_val,
            }

        logger.info("Typing into element @%s", ref)
        try:
            element.fill(text, timeout=5_000)
            state.last_activity = time.time()
        except Exception as e:
            logger.warning("Type error: %s", e)
            url_val, title_val = _safe_page_info(page)
            return {
                "success": False,
                "error": f"Type failed: {e}",
                "url": url_val,
                "title": title_val,
            }

        snap = self._snapshot_impl(session_id)
        return {
            "success": True,
            "url": snap["url"],
            "title": snap["title"],
            "snapshot": snap["snapshot"],
        }

    def scroll(self, session_id: str, direction: str = "down") -> Dict[str, Any]:
        return self._run_on_worker(self._scroll_impl, session_id, direction)

    def _scroll_impl(self, session_id: str, direction: str = "down") -> Dict[str, Any]:
        page, state = self._get_or_create_page_impl(session_id)
        logger.info("Scrolling %s", direction)

        delta = 800
        try:
            if direction == "down":
                page.mouse.wheel(0, delta)
            elif direction == "up":
                page.mouse.wheel(0, -delta)
            elif direction == "left":
                page.mouse.wheel(-delta, 0)
            elif direction == "right":
                page.mouse.wheel(delta, 0)
            else:
                return {
                    "success": False,
                    "error": f"Unknown direction '{direction}'. Use up/down/left/right.",
                }
            page.wait_for_timeout(500)
            state.last_activity = time.time()
        except Exception as e:
            logger.warning("Scroll error: %s", e)
            return {"success": False, "error": str(e)}

        snap = self._snapshot_impl(session_id)
        return {
            "success": True,
            "url": snap["url"],
            "title": snap["title"],
            "snapshot": snap["snapshot"],
        }

    def press(self, session_id: str, key: str) -> Dict[str, Any]:
        return self._run_on_worker(self._press_impl, session_id, key)

    def _press_impl(self, session_id: str, key: str) -> Dict[str, Any]:
        page, state = self._get_or_create_page_impl(session_id)
        logger.info("Pressing key: %s", key)
        try:
            page.keyboard.press(key)
            page.wait_for_timeout(500)
            state.last_activity = time.time()
        except Exception as e:
            logger.warning("Press error: %s", e)
            return {"success": False, "error": str(e)}

        snap = self._snapshot_impl(session_id)
        return {
            "success": True,
            "url": snap["url"],
            "title": snap["title"],
            "snapshot": snap["snapshot"],
        }

    # ── Screenshot ────────────────────────────────────────────────────────────

    def screenshot(self, session_id: str, path: Optional[str] = None) -> Dict[str, Any]:
        return self._run_on_worker(self._screenshot_impl, session_id, path)

    def _screenshot_impl(self, session_id: str, path: Optional[str] = None) -> Dict[str, Any]:
        page, state = self._get_or_create_page_impl(session_id)

        if not path:
            ts = int(time.time())
            filename = f"screenshot_{session_id[:8]}_{ts}.png"
            path = str(state.downloads_dir / filename)

        path_obj = Path(path)
        path_obj.parent.mkdir(parents=True, exist_ok=True)

        logger.info("Taking screenshot: %s", path)
        try:
            page.screenshot(path=str(path_obj), full_page=True)
            state.last_activity = time.time()
            return {
                "success": True,
                "path": str(path_obj.resolve()),
                "size": path_obj.stat().st_size,
            }
        except Exception as e:
            logger.warning("Screenshot error: %s", e)
            return {"success": False, "error": str(e)}

    # ── Download ──────────────────────────────────────────────────────────────

    def download(
        self,
        session_id: str,
        url: Optional[str] = None,
        path: Optional[str] = None,
        ref: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self._run_on_worker(self._download_impl, session_id, url, path, ref)

    def _download_impl(
        self,
        session_id: str,
        url: Optional[str] = None,
        path: Optional[str] = None,
        ref: Optional[str] = None,
    ) -> Dict[str, Any]:
        page, state = self._get_or_create_page_impl(session_id)

        if ref:
            element = self._get_element(session_id, ref)
            if not element:
                snap = self._snapshot_impl(session_id)
                return {
                    "success": False,
                    "error": f"Element @{ref} not found. Current snapshot:\n{snap['snapshot']}",
                    "url": snap["url"],
                    "title": snap["title"],
                }
            try:
                logger.info("Clicking element @%s and waiting for download", ref)
                with page.expect_download(timeout=10_000) as download_info:
                    element.click(timeout=5_000)
                downloaded_file = self._save_download(
                    state,
                    download_info.value,
                    path=path,
                    queue_download=False,
                )
                state.last_activity = time.time()
                return self._download_result(downloaded_file)
            except Exception as e:
                logger.warning("Download click error: %s", e)
                return {
                    "success": False,
                    "error": str(e),
                    "suggestion": "Verify the ref points to a link or button that starts a browser download.",
                }

        if not url:
            if state.downloads:
                queued = state.downloads.pop()
                try:
                    queued = self._move_download(queued, state, path)
                    return self._download_result(queued)
                except Exception as e:
                    return {"success": False, "error": f"Failed to move captured download: {e}"}
            err_msg = state.download_errors.pop() if state.download_errors else "No download was captured."
            return {
                "success": False,
                "error": err_msg,
                "suggestion": "Provide url or ref. Downloads that already completed before this session cannot be captured.",
            }

        try:
            logger.info("Triggering download from URL: %s", url)
            with page.expect_download(timeout=10_000) as download_info:
                page.goto(url, wait_until="domcontentloaded")
            downloaded_file = self._save_download(
                state,
                download_info.value,
                path=path,
                queue_download=False,
            )
            state.last_activity = time.time()
            return self._download_result(downloaded_file)
        except Exception as e:
            logger.warning("Download navigation error: %s", e)
            return {
                "success": False,
                "error": str(e),
                "suggestion": "Use ref for links/buttons with download attributes, or provide a direct URL that triggers a browser download.",
            }
