# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Browser Backend — Playwright wrapper for headless Chromium automation.

Provides a synchronous API over Playwright's async internals. Each session
gets an isolated BrowserContext + Page. Pages are snapshotted via in-page
JavaScript that tags interactive elements with data-mclaw-ref IDs.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import shutil
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mclaw.constants import get_mclaw_home
from mclaw.tools.interrupt import get_cancel_id, get_interrupt_event, safe_cancel_trace

logger = logging.getLogger(__name__)

_DEFAULT_VIEWPORT = {"width": 1280, "height": 720}
_DEFAULT_TIMEOUT = 30_000  # ms
_MAX_SNAPSHOT_CHARS = 8_000
_DOM_CONTENT_LOADED_TIMEOUT = 3_000  # ms
_NETWORK_IDLE_TIMEOUT = 2_000  # ms
_DOM_SETTLE_MIN = 250  # ms
_DOM_SETTLE_QUIET = 200  # ms
_DOM_SETTLE_TIMEOUT = 1_500  # ms
_INTERRUPT_POLL_INTERVAL = 0.05
_CANCEL_SESSION_GRACE = 0.35
_CANCEL_CONNECTION_GRACE = 0.35


class BrowserOperationCancelled(RuntimeError):
    """Raised when the current turn cancels a browser operation."""

    def __init__(
        self,
        message: str,
        *,
        completion_unknown: bool = False,
        fence: "_WorkerTask | None" = None,
    ) -> None:
        super().__init__(message)
        self.completion_unknown = completion_unknown
        self.fence = fence


@dataclass(eq=False)
class _WorkerTask:
    fn: Callable
    args: tuple
    kwargs: dict
    future: Future
    cancel_session_id: str | None = None
    cancel_id: str = "none"
    completion_lock: threading.Lock = field(default_factory=threading.Lock)
    operation_finished: threading.Event = field(default_factory=threading.Event)
    cancel_requested: bool = False
    connection_aborted: bool = False
    cleanup_committed: bool = False
    phase: str = "queued"
    created_at: float = field(default_factory=time.monotonic)

    def is_alive(self) -> bool:
        return not self.future.done()

    @property
    def diagnostic_name(self) -> str:
        name = getattr(self.fn, "__name__", type(self.fn).__name__)
        return f"browser:{name.strip('_').removesuffix('_impl')}"

    @property
    def blocking_reason(self) -> str:
        age = max(0.0, time.monotonic() - self.created_at)
        return (
            f"Browser operation '{self.diagnostic_name}' is still {self.phase} "
            f"after cancellation ({age:.1f}s); wait for browser cleanup to finish"
        )


def _default_downloads_dir() -> Path:
    return get_mclaw_home() / "downloads"


# JavaScript snippet that tags interactive elements in the current viewport.
_SNAPSHOT_JS = """
(() => {
    // Clear stale refs from the previous snapshot.
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

    const viewportWidth = window.innerWidth || document.documentElement.clientWidth;
    const viewportHeight = window.innerHeight || document.documentElement.clientHeight;
    const inViewport = rect => (
        rect.bottom > 0 && rect.right > 0 &&
        rect.top < viewportHeight && rect.left < viewportWidth
    );
    const isVisible = el => {
        const rect = el.getBoundingClientRect();
        const style = window.getComputedStyle(el);
        if (rect.width <= 0 || rect.height <= 0 || !inViewport(rect) ||
            style.display === 'none' || style.visibility === 'hidden' ||
            style.opacity === '0') return false;

        const clips = value => /^(auto|scroll|hidden|clip|overlay)$/.test(value);
        for (let ancestor = el.parentElement; ancestor; ancestor = ancestor.parentElement) {
            const ancestorStyle = window.getComputedStyle(ancestor);
            if (ancestorStyle.display === 'none' || ancestorStyle.visibility === 'hidden' ||
                ancestorStyle.opacity === '0') return false;
            const ancestorRect = ancestor.getBoundingClientRect();
            if (clips(ancestorStyle.overflowY) &&
                (rect.bottom <= ancestorRect.top || rect.top >= ancestorRect.bottom)) return false;
            if (clips(ancestorStyle.overflowX) &&
                (rect.right <= ancestorRect.left || rect.left >= ancestorRect.right)) return false;
        }
        return true;
    };

    candidates.forEach(el => {
        if (!isVisible(el)) return;

        const ref = 'e' + (elements.length + 1);
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
        if (!isVisible(h)) return;
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
        if (!isVisible(el)) return;
        const txt = (el.textContent || '').replace(/\\s+/g, ' ').trim();
        if (!txt || txt.length < 2 || seenText.has(txt)) return;
        seenText.add(txt);
        textBlocks.push(txt.slice(0, 220));
    });

    const scrollY = Math.max(0, Math.round(window.scrollY || document.documentElement.scrollTop || 0));
    const pageHeight = Math.max(
        viewportHeight,
        document.documentElement.scrollHeight,
        document.body ? document.body.scrollHeight : 0
    );

    return {
        elements,
        headings,
        textBlocks: textBlocks.slice(0, 40),
        viewport: {
            top: scrollY,
            bottom: Math.min(pageHeight, scrollY + viewportHeight),
            height: viewportHeight,
            pageHeight: pageHeight,
            hasMoreAbove: scrollY > 1,
            hasMoreBelow: scrollY + viewportHeight < pageHeight - 1
        }
    };
})()
"""


_DOM_SETTLE_JS = """
({minimumMs, quietMs, timeoutMs}) => new Promise(resolve => {
    const startedAt = performance.now();
    let lastMutationAt = startedAt;
    let finished = false;

    const finish = () => {
        if (finished) return;
        finished = true;
        observer.disconnect();
        clearInterval(timer);
        resolve();
    };
    const observer = new MutationObserver(() => {
        lastMutationAt = performance.now();
    });
    const root = document.documentElement || document;
    observer.observe(root, {
        subtree: true,
        childList: true,
        attributes: true,
        characterData: true
    });
    const timer = setInterval(() => {
        const now = performance.now();
        if (now - startedAt >= timeoutMs || (
            now - startedAt >= minimumMs && now - lastMutationAt >= quietMs
        )) finish();
    }, 50);
})
"""


_SCROLL_JS = """
({dx, dy}) => {
    const root = document.scrollingElement || document.documentElement;
    const vertical = dy !== 0;
    const amount = vertical ? dy : dx;
    const viewportWidth = window.innerWidth || document.documentElement.clientWidth;
    const viewportHeight = window.innerHeight || document.documentElement.clientHeight;

    const overflowAllowsScroll = (element, axis) => {
        const style = getComputedStyle(element);
        const value = axis === 'y' ? style.overflowY : style.overflowX;
        return /^(auto|scroll|overlay)$/.test(value);
    };
    const hasRoom = (element, axis, delta) => {
        if (axis === 'y') {
            if (element.scrollHeight <= element.clientHeight + 1) return false;
            return delta > 0
                ? element.scrollTop + element.clientHeight < element.scrollHeight - 1
                : element.scrollTop > 1;
        }
        if (element.scrollWidth <= element.clientWidth + 1) return false;
        return delta > 0
            ? element.scrollLeft + element.clientWidth < element.scrollWidth - 1
            : element.scrollLeft > 1;
    };
    const canScroll = element => {
        if (!element || element === document.body || element === document.documentElement) {
            return false;
        }
        const axis = vertical ? 'y' : 'x';
        return overflowAllowsScroll(element, axis) && hasRoom(element, axis, amount);
    };
    const visibleArea = element => {
        const rect = element.getBoundingClientRect();
        const width = Math.max(0, Math.min(rect.right, viewportWidth) - Math.max(rect.left, 0));
        const height = Math.max(0, Math.min(rect.bottom, viewportHeight) - Math.max(rect.top, 0));
        return width * height;
    };

    let target = null;
    const seen = new Set();
    for (const hit of document.elementsFromPoint(viewportWidth / 2, viewportHeight / 2)) {
        for (let element = hit; element && element !== document.body; element = element.parentElement) {
            if (seen.has(element)) continue;
            seen.add(element);
            if (canScroll(element)) {
                target = element;
                break;
            }
        }
        if (target) break;
    }

    if (!target) {
        let largestArea = 0;
        for (const element of document.querySelectorAll('body *')) {
            if (!canScroll(element)) continue;
            const area = visibleArea(element);
            if (area > largestArea) {
                largestArea = area;
                target = element;
            }
        }
    }

    if (!target && hasRoom(root, vertical ? 'y' : 'x', amount)) target = root;
    if (!target) target = root;

    const before = vertical ? target.scrollTop : target.scrollLeft;
    target.scrollBy({left: dx, top: dy, behavior: 'auto'});
    const after = vertical ? target.scrollTop : target.scrollLeft;
    const label = target === root
        ? 'document'
        : target.tagName.toLowerCase() + (target.id ? '#' + target.id : '');
    return {
        target: label,
        axis: vertical ? 'vertical' : 'horizontal',
        before: Math.round(before),
        after: Math.round(after),
        moved: Math.abs(after - before) > 1
    };
}
"""


def _safe_page_info(page) -> tuple[str, str]:
    """Read page URL and title without surfacing transient browser state errors."""
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
    """Mutable browser state owned by one logical M-Claw browser session."""
    context: Any
    page: Any
    ref_map: dict[str, Any] = field(default_factory=dict)
    last_activity: float = field(default_factory=time.time)
    downloads_dir: Path = field(default_factory=_default_downloads_dir)
    downloads: list[Path] = field(default_factory=list)
    download_errors: list[str] = field(default_factory=list)
    needs_settle: bool = False


class BrowserBackend:
    """Lazy-started Playwright + Chromium backend with per-session isolation."""

    def __init__(self, headless: bool = True):
        self._headless = headless
        self._playwright: Any | None = None
        self._browser: Any | None = None
        self._sessions: dict[str, _SessionState] = {}
        self._lock = threading.Lock()
        self._started = False
        self._task_queue: "queue.Queue[_WorkerTask | None]" = queue.Queue()
        self._worker_lock = threading.Lock()
        self._worker_thread: threading.Thread | None = None
        self._worker_thread_id: int | None = None

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
        """Run all Playwright sync calls on the thread that owns Playwright."""
        self._worker_thread_id = threading.get_ident()
        while True:
            task = self._task_queue.get()
            if task is None:
                break
            future = task.future
            if not future.set_running_or_notify_cancel():
                continue
            with task.completion_lock:
                task.phase = "running"
            try:
                result = task.fn(*task.args, **task.kwargs)
            except Exception as exc:
                result = exc
                failed = True
            else:
                failed = False
            task.operation_finished.set()

            with task.completion_lock:
                cancelled = task.cancel_requested
                if not cancelled:
                    if failed:
                        future.set_exception(result)
                    else:
                        future.set_result(result)

            if cancelled:
                # Linearize cleanup selection with the fallback connection abort.
                # A connection abort may still unblock close_session; the worker
                # rechecks it before publishing a terminal future and resets all
                # handles if the abort won that race.
                reset_browser = False
                close_session_id = None
                with task.completion_lock:
                    if task.connection_aborted:
                        task.cleanup_committed = True
                        task.phase = "resetting_browser"
                        reset_browser = True
                    elif task.cancel_session_id is not None:
                        task.phase = "closing_session"
                        close_session_id = task.cancel_session_id
                    else:
                        task.cleanup_committed = True
                        task.phase = "finishing"
                if reset_browser:
                    self._reset_aborted_browser_impl()
                elif close_session_id is not None:
                    self._close_session_impl(close_session_id)
                    with task.completion_lock:
                        task.cleanup_committed = True
                        reset_browser = task.connection_aborted
                        task.phase = (
                            "resetting_browser" if reset_browser else "finishing"
                        )
                    if reset_browser:
                        self._reset_aborted_browser_impl()
                    else:
                        safe_cancel_trace(
                            lambda: logger.info(
                                "[CANCEL_TRACE] browser_session_close cancel_id=%s "
                                "task_id=%x session=%s elapsed_ms=%d",
                                task.cancel_id,
                                id(task),
                                close_session_id,
                                int((time.monotonic() - task.created_at) * 1000),
                            )
                        )
                with task.completion_lock:
                    task.phase = "finishing"
                future.set_exception(
                    BrowserOperationCancelled(
                        "Browser operation interrupted by user",
                    )
                )
                safe_cancel_trace(
                    lambda: logger.info(
                        "[CANCEL_TRACE] browser_task_finished cancel_id=%s "
                        "task_id=%x operation=%s elapsed_ms=%d",
                        task.cancel_id,
                        id(task),
                        task.diagnostic_name,
                        int((time.monotonic() - task.created_at) * 1000),
                    )
                )
        self._worker_thread_id = None

    @staticmethod
    def _submit_cancel_coro(loop, coro) -> Future | None:
        """Submit Playwright cleanup to its live owner loop."""
        try:
            return asyncio.run_coroutine_threadsafe(coro, loop)
        except BaseException:
            coro.close()
            return None

    def _request_session_cancel(self, task: _WorkerTask) -> bool:
        """Close the active context on Playwright's loop to abort its current call."""
        session_id = task.cancel_session_id
        if session_id is None or not self._lock.acquire(timeout=0.01):
            return False
        try:
            state = self._sessions.get(session_id)
        finally:
            self._lock.release()
        if state is None:
            return False
        impl = getattr(state.context, "_impl_obj", None)
        loop = getattr(impl, "_loop", None)
        close = getattr(impl, "close", None)
        if loop is None or not callable(close):
            return False
        submitted = self._submit_cancel_coro(
            loop,
            close("M-Claw browser operation interrupted"),
        )
        if submitted is None:
            return False
        with task.completion_lock:
            task.phase = "cancelling_session"
        safe_cancel_trace(
            lambda: logger.info(
                "[CANCEL_TRACE] browser_session_cancel_requested cancel_id=%s "
                "task_id=%x session=%s",
                task.cancel_id,
                id(task),
                session_id,
            )
        )
        return True

    def _request_connection_abort(self, task: _WorkerTask) -> bool:
        """Last-resort abort for startup or a context that cannot close promptly."""
        owner = self._browser or self._playwright
        impl = getattr(owner, "_impl_obj", None)
        connection = getattr(impl, "_connection", None)
        loop = getattr(impl, "_loop", None)
        stop_async = getattr(connection, "stop_async", None)
        if loop is None or not callable(stop_async):
            return False
        with task.completion_lock:
            if task.cleanup_committed:
                return False
            previous_phase = task.phase
            task.connection_aborted = True
            task.phase = "aborting_connection"
            submitted = self._submit_cancel_coro(loop, stop_async())
            if submitted is None:
                task.connection_aborted = False
                task.phase = previous_phase
                return False
        safe_cancel_trace(
            lambda: logger.warning(
                "[CANCEL_TRACE] browser_connection_abort cancel_id=%s task_id=%x "
                "operation=%s",
                task.cancel_id,
                id(task),
                task.diagnostic_name,
            )
        )
        return True

    def _wait_for_cancel_drain(self, task: _WorkerTask, timeout: float) -> bool:
        try:
            task.future.result(timeout=timeout)
        except FutureTimeout:
            return False
        except BaseException:
            return True
        return True

    def _reset_aborted_browser_impl(self) -> None:
        """Discard objects backed by an aborted Playwright connection."""
        with self._lock:
            self._sessions.clear()
            playwright = self._playwright
            self._browser = None
            self._playwright = None
            self._started = False
        if playwright is not None:
            try:
                playwright.stop()
            except Exception as exc:
                safe_cancel_trace(
                    lambda: logger.debug(
                        "Error finalizing aborted Playwright connection: %s", exc
                    )
                )

    def _run_on_worker(
        self,
        fn: Callable,
        *args,
        cancel_session_id: str | None = None,
        **kwargs,
    ):
        """Marshal browser work onto the Playwright owner thread."""
        if threading.get_ident() == self._worker_thread_id:
            return fn(*args, **kwargs)

        cancel_event = get_interrupt_event()
        if cancel_event is not None and cancel_event.is_set():
            safe_cancel_trace(
                lambda: logger.info(
                    "[CANCEL_TRACE] browser_cancel_before_queue cancel_id=%s operation=%s",
                    get_cancel_id(cancel_event),
                    getattr(fn, "__name__", type(fn).__name__),
                )
            )
            raise BrowserOperationCancelled("Browser operation interrupted by user")

        self._ensure_worker()
        future: Future = Future()
        task = _WorkerTask(
            fn,
            args,
            kwargs,
            future,
            cancel_session_id,
            get_cancel_id(cancel_event),
        )
        self._task_queue.put(task)

        while True:
            try:
                return future.result(timeout=_INTERRUPT_POLL_INTERVAL)
            except FutureTimeout:
                if future.done():
                    return future.result()
                if cancel_event is None or not cancel_event.is_set():
                    continue
                if future.cancel():
                    safe_cancel_trace(
                        lambda: logger.info(
                            "[CANCEL_TRACE] browser_cancel_queued cancel_id=%s "
                            "task_id=%x operation=%s elapsed_ms=%d",
                            task.cancel_id,
                            id(task),
                            task.diagnostic_name,
                            int((time.monotonic() - task.created_at) * 1000),
                        )
                    )
                    raise BrowserOperationCancelled("Browser operation interrupted by user")
                with task.completion_lock:
                    if future.done():
                        return future.result()
                    if task.operation_finished.is_set():
                        completion_won = True
                    else:
                        completion_won = False
                        task.cancel_requested = True
                    phase = task.phase
                if completion_won:
                    safe_cancel_trace(
                        lambda: logger.info(
                            "[CANCEL_TRACE] browser_completion_won_cancel "
                            "cancel_id=%s task_id=%x operation=%s elapsed_ms=%d",
                            task.cancel_id,
                            id(task),
                            task.diagnostic_name,
                            int((time.monotonic() - task.created_at) * 1000),
                        )
                    )
                    return future.result()
                safe_cancel_trace(
                    lambda: logger.info(
                        "[CANCEL_TRACE] browser_cancel_detected cancel_id=%s task_id=%x "
                        "session=%s operation=%s phase=%s elapsed_ms=%d",
                        task.cancel_id,
                        id(task),
                        cancel_session_id or "none",
                        task.diagnostic_name,
                        phase,
                        int((time.monotonic() - task.created_at) * 1000),
                    )
                )
                self._request_session_cancel(task)
                if self._wait_for_cancel_drain(task, _CANCEL_SESSION_GRACE):
                    raise BrowserOperationCancelled(
                        "Browser operation interrupted by user"
                    )
                self._request_connection_abort(task)
                if self._wait_for_cancel_drain(task, _CANCEL_CONNECTION_GRACE):
                    raise BrowserOperationCancelled(
                        "Browser operation interrupted by user"
                    )
                safe_cancel_trace(
                    lambda: logger.warning(
                        "[CANCEL_TRACE] browser_cancel_unresolved cancel_id=%s "
                        "task_id=%x operation=%s completion_unknown=true",
                        task.cancel_id,
                        id(task),
                        task.diagnostic_name,
                    )
                )
                raise BrowserOperationCancelled(
                    "Browser cancellation requested; operation completion is unknown",
                    completion_unknown=True,
                    fence=task,
                )

    def _shutdown_worker(self) -> None:
        """Stop the worker after browser resources have been closed."""
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
                    "  python -m playwright install chromium"
                ) from exc

            self._playwright = sync_playwright().start()
            try:
                self._browser = self._playwright.chromium.launch(headless=self._headless)
            except Exception as exc:
                try:
                    if self._playwright:
                        self._playwright.stop()
                except Exception:
                    logger.debug("Error stopping Playwright after launch failure", exc_info=True)
                self._playwright = None
                raise RuntimeError(
                    "Failed to launch Chromium. Run 'python -m playwright install chromium' first.\n"
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

    def _wait_for_page_ready(self, page: Any) -> None:
        """Wait for loading and DOM changes to become briefly quiet, within fixed bounds."""
        try:
            page.wait_for_load_state(
                "domcontentloaded",
                timeout=_DOM_CONTENT_LOADED_TIMEOUT,
            )
        except Exception as exc:
            logger.debug("DOMContentLoaded wait ended early: %s", exc)
        try:
            page.wait_for_load_state("networkidle", timeout=_NETWORK_IDLE_TIMEOUT)
        except Exception as exc:
            logger.debug("Network-idle wait ended early: %s", exc)
        try:
            page.evaluate(
                _DOM_SETTLE_JS,
                {
                    "minimumMs": _DOM_SETTLE_MIN,
                    "quietMs": _DOM_SETTLE_QUIET,
                    "timeoutMs": _DOM_SETTLE_TIMEOUT,
                },
            )
        except Exception as exc:
            logger.debug("DOM-settle wait ended early: %s", exc)

    def _settle_if_needed(self, page: Any, state: _SessionState) -> None:
        if not state.needs_settle:
            return
        try:
            self._wait_for_page_ready(page)
        finally:
            state.needs_settle = False

    # ── Session management ────────────────────────────────────────────────────

    def get_or_create_page(self, session_id: str) -> tuple[Any, _SessionState]:
        return self._run_on_worker(
            self._get_or_create_page_impl,
            session_id,
            cancel_session_id=session_id,
        )

    def _get_or_create_page_impl(self, session_id: str) -> tuple[Any, _SessionState]:
        """Create an isolated context and page for a session on first use."""
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
        """Close one isolated browser context without stopping shared Chromium."""
        if not self._worker_thread or not self._worker_thread.is_alive():
            return
        self._run_on_worker(self._close_session_impl, session_id)

    def _close_session_impl(self, session_id: str) -> None:
        with self._lock:
            state = self._sessions.pop(session_id, None)
        if state is None:
            return
        try:
            state.context.close()
        except Exception as exc:
            safe_cancel_trace(
                lambda: logger.debug(
                    "Error closing browser context %s: %s", session_id, exc
                )
            )

    # ── Navigation ────────────────────────────────────────────────────────────

    def navigate(self, session_id: str, url: str) -> dict[str, str]:
        return self._run_on_worker(
            self._navigate_impl,
            session_id,
            url,
            cancel_session_id=session_id,
        )

    def navigate_and_snapshot(
        self,
        session_id: str,
        url: str,
    ) -> tuple[dict[str, str], dict[str, Any]]:
        """Keep navigation and its result snapshot inside one cancellable task."""
        return self._run_on_worker(
            self._navigate_and_snapshot_impl,
            session_id,
            url,
            cancel_session_id=session_id,
        )

    def _navigate_and_snapshot_impl(
        self,
        session_id: str,
        url: str,
    ) -> tuple[dict[str, str], dict[str, Any]]:
        nav = self._navigate_impl(session_id, url)
        return nav, self._snapshot_impl(session_id)

    def _navigate_impl(self, session_id: str, url: str) -> dict[str, str]:
        page, state = self._get_or_create_page_impl(session_id)
        logger.info("Navigating to %s", url)
        state.needs_settle = True
        try:
            page.goto(url, wait_until="domcontentloaded")
            state.last_activity = time.time()
            url_val, title_val = _safe_page_info(page)
            return {"url": url_val, "title": title_val}
        except Exception as e:
            logger.warning("Navigation error for %s: %s", url, e)
            url_val, title_val = _safe_page_info(page)
            return {"url": url_val, "title": title_val, "error": str(e)}

    # ── Snapshot ──────────────────────────────────────────────────────────────

    def snapshot(self, session_id: str) -> dict[str, Any]:
        return self._run_on_worker(
            self._snapshot_impl,
            session_id,
            cancel_session_id=session_id,
        )

    def _snapshot_impl(self, session_id: str) -> dict[str, Any]:
        """Return a compact current-viewport snapshot and refresh element refs."""
        page, state = self._get_or_create_page_impl(session_id)
        state.last_activity = time.time()
        self._settle_if_needed(page, state)
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
        viewport = result.get("viewport", {})

        lines: list[str] = []
        ref_map: dict[str, Any] = {}

        # Include headings as page context.
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

        viewport_top = int(viewport.get("top") or 0)
        viewport_bottom = int(viewport.get("bottom") or 0)
        page_height = int(viewport.get("pageHeight") or viewport_bottom)
        snapshot_text = (
            f"url: {url_val}\n"
            f"title: {title_val}\n"
            f"viewport: {viewport_top}-{viewport_bottom} of {page_height}px\n\n"
        )
        snapshot_text += "\n".join(lines)

        if not elements:
            snapshot_text += "\n[No interactive elements detected in the current viewport.]"

        page_directions = []
        if viewport.get("hasMoreAbove"):
            page_directions.append("above")
        if viewport.get("hasMoreBelow"):
            page_directions.append("below")
        if page_directions:
            snapshot_text += (
                f"\n[Page continues {' and '.join(page_directions)}; "
                "use browser_scroll to move the viewport.]"
            )

        if len(snapshot_text) > _MAX_SNAPSHOT_CHARS:
            trunc = snapshot_text[:_MAX_SNAPSHOT_CHARS]
            last_nl = trunc.rfind("\n")
            if last_nl > _MAX_SNAPSHOT_CHARS // 2:
                trunc = trunc[:last_nl]
            snapshot_text = (
                f"{trunc}\n\n"
                f"[Current viewport snapshot truncated: {len(elements)} interactive elements.]"
            )

        return {
            "url": url_val,
            "title": title_val,
            "snapshot": snapshot_text,
            "ref_count": len(elements),
        }

    # ── Interaction ───────────────────────────────────────────────────────────

    def _get_element(self, session_id: str, ref: str) -> Any | None:
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
        """Choose a non-conflicting destination without overwriting downloads."""
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

    def _download_destination(self, state: _SessionState, suggested: str, path: str | None = None) -> Path:
        """Resolve caller output path while preserving browser suggested names."""
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
        path: str | None = None,
        *,
        queue_download: bool = True,
    ) -> Path:
        """Persist a Playwright Download and optionally queue it for later pickup."""
        suggested = download.suggested_filename or "download"
        dest = self._download_destination(state, suggested, path=path)
        download.save_as(str(dest))
        if queue_download:
            state.downloads.append(dest)
        logger.info("Download saved: %s (%d bytes)", dest, dest.stat().st_size)
        return dest

    def _download_result(self, path: Path) -> dict[str, Any]:
        return {
            "success": True,
            "filename": path.name,
            "path": str(path.resolve()),
            "size": path.stat().st_size,
        }

    def _move_download(self, source: Path, state: _SessionState, path: str | None) -> Path:
        if not path:
            return source
        dest = self._download_destination(state, source.name, path=path)
        if source.resolve() == dest.resolve():
            return source
        shutil.move(str(source), str(dest))
        return dest

    def click(self, session_id: str, ref: str) -> dict[str, Any]:
        return self._run_on_worker(
            self._click_impl,
            session_id,
            ref,
            cancel_session_id=session_id,
        )

    def _click_impl(self, session_id: str, ref: str) -> dict[str, Any]:
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
        captured_download: Path | None = None

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
            state.needs_settle = True
            state.last_activity = time.time()
        except Exception as e:
            logger.warning("Click error: %s", e)
            url_val, title_val = _safe_page_info(page)
            try:
                page.remove_listener("download", _handle_download)
            except Exception as exc:
                logger.debug("Download listener cleanup failed: %s", exc)
            return {
                "success": False,
                "error": f"Click failed: {e}",
                "url": url_val,
                "title": title_val,
            }

        try:
            snap = self._snapshot_impl(session_id)
        finally:
            try:
                page.remove_listener("download", _handle_download)
            except Exception as exc:
                logger.debug("Download listener cleanup failed: %s", exc)

        result = {
            "success": True,
            "url": snap["url"],
            "title": snap["title"],
            "snapshot": snap["snapshot"],
        }
        if captured_download and captured_download.exists():
            result["download"] = self._download_result(captured_download)
        return result

    def type_text(self, session_id: str, ref: str, text: str) -> dict[str, Any]:
        return self._run_on_worker(
            self._type_text_impl,
            session_id,
            ref,
            text,
            cancel_session_id=session_id,
        )

    def _type_text_impl(self, session_id: str, ref: str, text: str) -> dict[str, Any]:
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
            state.needs_settle = True
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

    def scroll(self, session_id: str, direction: str = "down") -> dict[str, Any]:
        return self._run_on_worker(
            self._scroll_impl,
            session_id,
            direction,
            cancel_session_id=session_id,
        )

    def _scroll_impl(self, session_id: str, direction: str = "down") -> dict[str, Any]:
        page, state = self._get_or_create_page_impl(session_id)
        logger.info("Scrolling %s", direction)

        delta = 600
        deltas = {
            "down": (0, delta),
            "up": (0, -delta),
            "left": (-delta, 0),
            "right": (delta, 0),
        }
        if direction not in deltas:
            return {
                "success": False,
                "error": f"Unknown direction '{direction}'. Use up/down/left/right.",
            }

        try:
            dx, dy = deltas[direction]
            scroll_info = page.evaluate(_SCROLL_JS, {"dx": dx, "dy": dy})
            state.needs_settle = True
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
            "scroll": scroll_info,
        }

    def press(self, session_id: str, key: str) -> dict[str, Any]:
        return self._run_on_worker(
            self._press_impl,
            session_id,
            key,
            cancel_session_id=session_id,
        )

    def _press_impl(self, session_id: str, key: str) -> dict[str, Any]:
        page, state = self._get_or_create_page_impl(session_id)
        logger.info("Pressing key: %s", key)
        try:
            page.keyboard.press(key)
            state.needs_settle = True
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

    def screenshot(self, session_id: str, path: str | None = None) -> dict[str, Any]:
        return self._run_on_worker(
            self._screenshot_impl,
            session_id,
            path,
            cancel_session_id=session_id,
        )

    def _screenshot_impl(self, session_id: str, path: str | None = None) -> dict[str, Any]:
        page, state = self._get_or_create_page_impl(session_id)
        self._settle_if_needed(page, state)

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
        url: str | None = None,
        path: str | None = None,
        ref: str | None = None,
    ) -> dict[str, Any]:
        return self._run_on_worker(
            self._download_impl,
            session_id,
            url,
            path,
            ref,
            cancel_session_id=session_id,
        )

    def _download_impl(
        self,
        session_id: str,
        url: str | None = None,
        path: str | None = None,
        ref: str | None = None,
    ) -> dict[str, Any]:
        """Handle the three download modes exposed by browser_download."""
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
