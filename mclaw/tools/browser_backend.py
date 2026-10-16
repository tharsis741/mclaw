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
import json
import logging
import os
import queue
import shutil
import threading
import tempfile
import time
import uuid
from collections.abc import Callable
from concurrent.futures import Future, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mclaw.constants import get_mclaw_home
from mclaw.tools.browser_snapshot import SNAPSHOT_JS as _SNAPSHOT_JS, element_line
from mclaw.tools.interrupt import get_cancel_id, get_interrupt_event, safe_cancel_trace

logger = logging.getLogger(__name__)

_DEFAULT_VIEWPORT = {"width": 1280, "height": 720}
_DEFAULT_TIMEOUT = 30_000  # ms
_MAX_SNAPSHOT_CHARS = 8_000
_MAX_SNAPSHOT_ELEMENTS = 100
_MAX_SNAPSHOT_FRAMES = 32
_DOM_CONTENT_LOADED_TIMEOUT = 3_000  # ms
_NETWORK_IDLE_TIMEOUT = 2_000  # ms
_DOM_SETTLE_MIN = 250  # ms
_DOM_SETTLE_QUIET = 200  # ms
_DOM_SETTLE_TIMEOUT = 1_500  # ms
_INTERRUPT_POLL_INTERVAL = 0.05
_CANCEL_SESSION_GRACE = 0.35
_CANCEL_CONNECTION_GRACE = 0.35
_OPERATION_TIMEOUT = 45.0  # Includes queueing, page scripts and observation.

_DIALOG_ACTIONS = {
    "_navigate_impl", "_navigate_and_snapshot_impl", "_click_impl", "_type_text_impl",
    "_scroll_impl", "_press_impl", "_element_action_impl", "_upload_impl",
    "_back_impl", "_download_impl",
}


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


class BrowserOperationTimedOut(BrowserOperationCancelled):
    """A browser operation exceeded its wall-clock deadline and was stopped."""


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
    target.scrollBy({left: dx, top: dy, behavior: 'instant'});
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


_TARGET_SCROLL_JS = """
(element, {dx, dy}) => {
    const root = document.scrollingElement || document.documentElement;
    const vertical = dy !== 0;
    const amount = vertical ? dy : dx;
    let target = root;
    for (let node = element; node; node = node.parentElement || node.getRootNode().host) {
        const style = getComputedStyle(node);
        const overflow = vertical ? style.overflowY : style.overflowX;
        const before = vertical ? node.scrollTop : node.scrollLeft;
        const size = vertical ? node.clientHeight : node.clientWidth;
        const extent = vertical ? node.scrollHeight : node.scrollWidth;
        if (/^(auto|scroll|overlay)$/.test(overflow) && extent > size + 1 &&
            (amount > 0 ? before + size < extent - 1 : before > 1)) {
            target = node;
            break;
        }
    }
    const before = vertical ? target.scrollTop : target.scrollLeft;
    target.scrollBy({left: dx, top: dy, behavior: 'instant'});
    const after = vertical ? target.scrollTop : target.scrollLeft;
    return {target: target === root ? 'document' : target.tagName.toLowerCase() + (target.id ? '#' + target.id : ''),
        axis: vertical ? 'vertical' : 'horizontal', before: Math.round(before),
        after: Math.round(after), moved: Math.abs(after - before) > 1};
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
    pending_downloads: list[Any] = field(default_factory=list)
    needs_settle: bool = False
    tabs: dict[str, Any] = field(default_factory=dict)
    next_tab: int = 1
    snapshot_id: str = ""
    dialog_policies: dict[str, tuple[bool, str | None, float]] = field(default_factory=dict)
    dialogs: list[dict[str, Any]] = field(default_factory=list)
    file_choosers: dict[str, Any] = field(default_factory=dict)
    closed: bool = False


class BrowserBackend:
    """Lazy-started Playwright + Chromium backend with per-session isolation."""

    def __init__(self, headless: bool = True):
        self._headless = headless
        self._playwright: Any | None = None
        self._browser: Any | None = None
        self._sessions: dict[str, _SessionState] = {}
        self._reset_sessions: set[str] = set()
        self._lock = threading.Lock()
        self._started = False
        self._task_queue: "queue.Queue[_WorkerTask | None]" = queue.Queue()
        self._worker_lock = threading.Lock()
        self._worker_thread: threading.Thread | None = None
        self._worker_thread_id: int | None = None
        # Keep e<number> compatibility, without reusing refs after snapshots,
        # tab changes, context cleanup, or a backend restart.
        self._next_ref = int(uuid.uuid4().hex[:10], 16)

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
            state = (self._sessions.get(task.cancel_session_id)
                     if getattr(task.fn, "__name__", "") in _DIALOG_ACTIONS else None)
            policy_tab = self._tab_id(state) if state is not None else None
            try:
                result = task.fn(*task.args, **task.kwargs)
            except Exception as exc:
                result = exc
                failed = True
            else:
                failed = False
            finally:
                if state is not None:
                    state.dialog_policies.pop(policy_tab, None)
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
                    self._reset_sessions.add(close_session_id)
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
            self._reset_sessions.update(self._sessions)
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
        operation_timeout: float | None = None,
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
        deadline = task.created_at + (operation_timeout if operation_timeout is not None else _OPERATION_TIMEOUT)

        while True:
            try:
                return future.result(timeout=_INTERRUPT_POLL_INTERVAL)
            except FutureTimeout:
                if future.done():
                    return future.result()
                interrupted = cancel_event is not None and cancel_event.is_set()
                timed_out = not interrupted and time.monotonic() >= deadline
                if not interrupted and not timed_out:
                    continue
                error_type = BrowserOperationTimedOut if timed_out else BrowserOperationCancelled
                message = "Browser operation timed out" if timed_out else "Browser operation interrupted by user"
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
                    raise error_type(message)
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
                    raise error_type(message)
                self._request_connection_abort(task)
                if self._wait_for_cancel_drain(task, _CANCEL_CONNECTION_GRACE):
                    raise error_type(message)
                safe_cancel_trace(
                    lambda: logger.warning(
                        "[CANCEL_TRACE] browser_cancel_unresolved cancel_id=%s "
                        "task_id=%x operation=%s completion_unknown=true",
                        task.cancel_id,
                        id(task),
                        task.diagnostic_name,
                    )
                )
                raise error_type(
                    message + "; operation completion is unknown",
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
        if self._started and not self._browser.is_connected():
            self._reset_aborted_browser_impl()
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
            if session_id in self._sessions and self._sessions[session_id].closed:
                stale = self._sessions.pop(session_id)
                try:
                    stale.context.close()
                except Exception:
                    pass
                self._reset_sessions.add(session_id)
            if session_id in self._sessions:
                state = self._sessions[session_id]
                state.last_activity = time.time()
                if state.page.is_closed():
                    self._clear_refs(state)
                    state.page = next((p for p in state.tabs.values() if not p.is_closed()), None)
                    if state.page is None:
                        state.page = state.context.new_page()
                return state.page, state

            logger.info("Creating new browser session: %s", session_id)
            context = self._browser.new_context(
                viewport=_DEFAULT_VIEWPORT,
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
            self._register_page(state, page)
            context.on("page", lambda opened: self._register_page(state, opened))
            context.on("close", lambda: setattr(state, "closed", True))
            return page, state

    def _register_page(self, state: _SessionState, page: Any) -> None:
        """Track popups without silently switching the selected tab."""
        if page in state.tabs.values():
            return
        tab_id = f"t{state.next_tab}"
        state.next_tab += 1
        state.tabs[tab_id] = page
        page.set_default_timeout(_DEFAULT_TIMEOUT)
        page.on("dialog", lambda dialog: self._handle_page_dialog(state, tab_id, dialog))
        page.on("filechooser", lambda chooser: state.file_choosers.__setitem__(tab_id, chooser))
        # Keep capture alive for delayed downloads, keyboard actions and popups.
        # Event callbacks only enqueue; saving inside a callback can deadlock.
        page.on("download", lambda download: state.pending_downloads.append(download))
        page.on("framenavigated", lambda frame: state.file_choosers.pop(tab_id, None)
                if frame is page.main_frame else None)
        page.on("crash", lambda: setattr(state, "closed", True))

        def forget_closed_page() -> None:
            state.tabs.pop(tab_id, None)
            state.dialog_policies.pop(tab_id, None)
            state.file_choosers.pop(tab_id, None)

        page.on("close", forget_closed_page)

    @staticmethod
    def _tab_id(state: _SessionState) -> str:
        return next((key for key, page in state.tabs.items() if page is state.page), "")

    def _tabs_info(self, state: _SessionState) -> list[dict[str, Any]]:
        return [
            {"id": key, "url": self._bounded_text(page.url, 180), "active": page is state.page}
            for key, page in state.tabs.items() if not page.is_closed()
        ]

    @staticmethod
    def _handle_page_dialog(state: _SessionState, tab_id: str, dialog: Any) -> None:
        policy = state.dialog_policies.pop(tab_id, None)
        if policy is not None and time.monotonic() > policy[2]:
            policy = None
        accept, prompt = policy[:2] if policy is not None else (False, None)
        report = {"tab_id": tab_id, "type": dialog.type, "message": dialog.message[:200],
                  "accepted": accept, "configured": policy is not None}
        try:
            if accept:
                dialog.accept(prompt_text=prompt if dialog.type == "prompt" else None)
            else:
                dialog.dismiss()
        except Exception as exc:
            report["error"] = str(exc)
        state.dialogs.append(report)
        del state.dialogs[:-3]

    @staticmethod
    def _clear_refs(state: _SessionState) -> None:
        refs, state.ref_map = state.ref_map, {}
        for handle in refs.values():
            try:
                handle.dispose()
            except Exception:
                pass
        state.snapshot_id = ""

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
        self._clear_refs(state)
        state.file_choosers.pop(self._tab_id(state), None)
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

    def snapshot(self, session_id: str, offset: int = 0) -> dict[str, Any]:
        return self._run_on_worker(
            self._snapshot_impl,
            session_id,
            offset,
            cancel_session_id=session_id,
        )

    @staticmethod
    def _observe_frame(frame: Any, start: int, limit: int, offset: int = 0) -> tuple[dict[str, Any], dict[str, Any]]:
        """Capture labels and their exact nodes together, before mutation observers run."""
        snapshot_handle = frame.evaluate_handle(_SNAPSHOT_JS, {"start": start, "limit": limit, "offset": offset})
        nodes_handle = None
        node_handles: dict[str, Any] = {}
        try:
            data = snapshot_handle.evaluate("({nodes, ...data}) => data")
            nodes_handle = snapshot_handle.get_property("nodes")
            node_handles = nodes_handle.get_properties()
            refs = {}
            for index, element in enumerate(data["elements"]):
                handle = node_handles[str(index)].as_element()
                if handle is None:
                    raise RuntimeError("Snapshot node is no longer an element")
                refs[element["ref"]] = handle
            return data, refs
        except Exception:
            for handle in node_handles.values():
                try:
                    handle.dispose()
                except Exception:
                    pass
            raise
        finally:
            for handle in (nodes_handle, snapshot_handle):
                if handle is not None:
                    try:
                        handle.dispose()
                    except Exception:
                        pass

    @staticmethod
    def _bounded_text(value: str, limit: int) -> str:
        value = "".join(char if ord(char) >= 32 else " " for char in value)
        return value if len(value) <= limit else value[:limit] + "… [truncated]"

    @staticmethod
    def _frame_is_visible(frame: Any) -> bool:
        """A child document is visible only when its embedding frames are too."""
        from mclaw.tools.browser_snapshot import FRAME_VISIBLE_JS

        while frame.parent_frame is not None:
            element = frame.frame_element()
            try:
                if not element.is_visible() or not element.evaluate(FRAME_VISIBLE_JS):
                    return False
            finally:
                element.dispose()
            frame = frame.parent_frame
        return True

    def _snapshot_impl(self, session_id: str, offset: int = 0) -> dict[str, Any]:
        """Observe visible frames and issue references to exact DOM node handles."""
        page, state = self._get_or_create_page_impl(session_id)
        state.last_activity = time.time()
        self._clear_refs(state)
        self._settle_if_needed(page, state)
        url, title = _safe_page_info(page)
        state.snapshot_id = uuid.uuid4().hex
        lines = [f"url: {url[:1000]}", f"title: {title[:300]}"]
        control_lines: list[str] = []
        content_lines: list[str] = []
        errors: list[str] = []
        truncated = False
        remaining_offset = offset
        has_more = False
        frames = page.frames
        for index, frame in enumerate(frames[:_MAX_SNAPSHOT_FRAMES]):
            if len(state.ref_map) >= _MAX_SNAPSHOT_ELEMENTS:
                has_more = True
                break
            try:
                if frame is not page.main_frame:
                    if not self._frame_is_visible(frame):
                        continue
                    frame_element = frame.frame_element()
                    try:
                        box = frame_element.bounding_box()
                        viewport = page.viewport_size or _DEFAULT_VIEWPORT
                        if (not box or box["x"] + box["width"] <= 0 or box["y"] + box["height"] <= 0
                                or box["x"] >= viewport["width"] or box["y"] >= viewport["height"]):
                            continue
                    finally:
                        frame_element.dispose()
                limit = _MAX_SNAPSHOT_ELEMENTS - len(state.ref_map)
                first_ref = self._next_ref
                self._next_ref += limit
                observation, handles = self._observe_frame(frame, first_ref, limit, remaining_offset)
                remaining_offset = max(0, remaining_offset - observation["total"])
                state.ref_map.update(handles)
                viewport = observation["viewport"]
                frame_header = [f"frame: {index} {frame.url[:300]}",
                                f"viewport: {viewport['top']}-{viewport['bottom']} of {viewport['pageHeight']}px"]
                if handles:
                    control_lines.extend(frame_header)
                    control_lines.extend(element_line(element) for element in observation.get("elements", []))
                # Observe controls before prose so a text-heavy early frame
                # cannot hide every control of a later frame on every page.
                content_lines.extend(frame_header if not handles else [f"content in frame: {index}"])
                for heading in observation.get("headings", []):
                    content_lines.append(f"#{heading['level']} {heading['text']}")
                content_lines.extend(f"text: {text}" for text in observation.get("textBlocks", []))
                has_more = has_more or observation.get("truncated", False)
                directions = []
                if viewport.get("hasMoreAbove"):
                    directions.append("above")
                if viewport.get("hasMoreBelow"):
                    directions.append("below")
                if directions:
                    content_lines.append(f"[Page continues {' and '.join(directions)}; use browser_scroll.]")
            except Exception as exc:
                errors.append(f"Frame {index}: {exc}")
                if frame is page.main_frame:
                    self._clear_refs(state)
                    control_lines.clear()
                    content_lines.clear()
                    break
        lines.extend(control_lines)
        lines.extend(content_lines)
        if not state.ref_map:
            lines.append("[No interactive elements detected in the current viewport.]")
        text = "\n".join(lines)
        # Budget the serialized text as well: quotes and escapes count against
        # the registry's 12k result cap, which must never cut action status/refs.
        while len(json.dumps(text, ensure_ascii=False)) > _MAX_SNAPSHOT_CHARS:
            text = text.rsplit("\n", 1)[0] if "\n" in text else ""
            truncated = True
        if truncated:
            displayed_refs = {
                line[1:line.index("]")] for line in text.splitlines() if line.startswith("[e") and "]" in line
            }
            for ref in list(state.ref_map):
                if ref not in displayed_refs:
                    has_more = True
                    handle = state.ref_map.pop(ref)
                    try:
                        handle.dispose()
                    except Exception:
                        pass
        if truncated or has_more:
            text += "\n[Snapshot truncated; use next_offset with browser_snapshot for more controls.]"
        if len(frames) > _MAX_SNAPSHOT_FRAMES:
            text += f"\n[Only the first {_MAX_SNAPSHOT_FRAMES} frames were inspected.]"
        result = {"success": not errors, "url": self._bounded_text(url, 500), "title": self._bounded_text(title, 200),
                  "snapshot": text, "snapshot_id": state.snapshot_id,
                  "ref_count": len(state.ref_map), "tab_id": self._tab_id(state),
                  "tabs": self._tabs_info(state)[:5], "tab_count": len(state.tabs), "offset": offset}
        if has_more and state.ref_map:
            result["next_offset"] = offset + len(state.ref_map)
        if len(state.tabs) > 5:
            result["tabs_truncated"] = True
        if session_id in self._reset_sessions:
            self._reset_sessions.discard(session_id)
            result.update(session_reset=True, warning="Browser session was reset; previous tabs and login state were lost.")
        if errors:
            result.update(error="; ".join(errors)[:600], error_code="SNAPSHOT_FAILED",
                          status="partial" if state.ref_map else "error",
                          suggestion="Refresh browser_snapshot to observe the current page.")
        if state.dialogs:
            result["dialogs"] = list(state.dialogs)
            state.dialogs.clear()
        chooser = state.file_choosers.get(self._tab_id(state))
        if chooser is not None:
            result["file_chooser"] = {"multiple": chooser.is_multiple()}
        # Leave room for action status, navigation errors and download metadata
        # added by callers, so the tool dispatcher never slices the JSON.
        original_text = result["snapshot"]
        while len(json.dumps(result, ensure_ascii=False)) > 9_000 and result["snapshot"]:
            result["snapshot"] = result["snapshot"].rsplit("\n", 1)[0] if "\n" in result["snapshot"] else ""
        if result["snapshot"] != original_text:
            shown = {line[1:line.index("]")] for line in result["snapshot"].splitlines()
                     if line.startswith("[e") and "]" in line}
            for ref in list(state.ref_map):
                if ref not in shown:
                    state.ref_map.pop(ref).dispose()
                    has_more = True
            result["ref_count"] = len(state.ref_map)
            if has_more and state.ref_map:
                result["next_offset"] = offset + len(state.ref_map)
            result["snapshot"] += "\n[Snapshot truncated; use next_offset for more controls.]"
        return result

    def _get_element(self, session_id: str, ref: str) -> Any | None:
        """Only resolve issued, still-connected references from the last snapshot."""
        _, state = self._get_or_create_page_impl(session_id)
        handle = state.ref_map.get(ref)
        if handle is None:
            return None
        try:
            if handle.evaluate(
                "(el, ref) => el.isConnected && el.getAttribute('data-mclaw-ref') === ref", ref
            ):
                return handle
        except Exception:
            pass
        return None

    def _stale_ref(self, session_id: str, ref: str) -> dict[str, Any]:
        result = self._snapshot_impl(session_id)
        result.update(success=False, error=f"Element @{ref} is stale or unknown.",
                      error_code="STALE_REF", action_completed=False,
                      suggestion="Use a reference from the returned snapshot; the action was not performed.")
        return result

    def _after_action(self, session_id: str) -> dict[str, Any]:
        try:
            _, state = self._get_or_create_page_impl(session_id)
            state.needs_settle = True
            result = self._snapshot_impl(session_id)
        except Exception as exc:
            result = {"success": False, "error": str(exc), "error_code": "SNAPSHOT_FAILED"}
        result["action_completed"] = True
        if not result["success"]:
            result.update(status="partial", suggestion=(
                "The action completed, but observation failed. Refresh browser_snapshot; "
                "do not repeat the action automatically."
            ))
        return result

    @staticmethod
    def _action_error(exc: Exception) -> dict[str, Any]:
        return {"success": False, "error": str(exc)[:1000], "error_code": "ACTION_FAILED",
                "completion_unknown": True,
                "suggestion": "Inspect browser_snapshot before deciding whether to retry the action."}

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

    @staticmethod
    def _checked_output(dest: Path, *, managed: bool, tool_name: str) -> Path:
        """Check the final filename too, including names supplied by a server."""
        from mclaw.tools.file_operations import _runtime
        from mclaw.tools.file_tools import _skill_store_mutation_error

        decision = _runtime().paths.check("write", str(dest))
        managed_root = get_mclaw_home().resolve() / "downloads"
        managed_output = (managed and decision.scope == "runtime_internal"
                          and decision.resolved.parent == managed_root)
        if not decision.allowed and not managed_output:
            raise PermissionError(decision.error_message())
        blocked = _skill_store_mutation_error(str(decision.resolved), tool_name)
        if blocked:
            raise PermissionError(blocked)
        return decision.resolved

    @staticmethod
    def _temporary_output(dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=".mclaw-browser-", suffix=".part", dir=dest.parent)
        os.close(descriptor)
        return Path(name)

    def _download_destination(
        self, state: _SessionState, suggested: str, path: str | None = None, *, unique: bool = True,
    ) -> Path:
        """Resolve caller output path while preserving browser suggested names."""
        safe_name = Path((suggested or "download").replace("\\", "/")).name
        if safe_name in {"", ".", ".."}:
            safe_name = "download"
        if not path:
            dest = state.downloads_dir / safe_name
        else:
            raw = str(path)
            target = Path(raw).expanduser()
            if raw.endswith(("/", "\\")) or (target.exists() and target.is_dir()):
                dest = target / safe_name
            else:
                dest = target
        dest = self._checked_output(dest, managed=not path, tool_name="browser_download")
        if unique:
            dest = self._checked_output(self._unique_path(dest), managed=not path, tool_name="browser_download")
        return dest

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
        temporary = self._temporary_output(dest)
        try:
            download.save_as(str(temporary))
            temporary.replace(dest)
        finally:
            temporary.unlink(missing_ok=True)
        if download in state.pending_downloads:
            state.pending_downloads.remove(download)
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
        dest = self._download_destination(state, source.name, path=path, unique=False)
        if source.resolve() == dest.resolve():
            return source
        dest = self._download_destination(state, source.name, path=path)
        temporary = self._temporary_output(dest)
        try:
            shutil.copyfile(source, temporary)
            temporary.replace(dest)
        finally:
            temporary.unlink(missing_ok=True)
        try:
            source.unlink()
        except OSError as exc:
            logger.debug("Saved download; could not remove previous copy: %s", exc)
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
            return self._stale_ref(session_id, ref)

        logger.info("Clicking element @%s", ref)
        pending_before = list(state.pending_downloads)
        try:
            element.click(timeout=5_000)
            result = self._after_action(session_id)
        except Exception as e:
            logger.warning("Click error: %s", e)
            return self._action_error(e)
        for download in list(state.pending_downloads):
            if download in pending_before:
                continue
            try:
                saved = self._save_download(state, download)
                result["download"] = self._download_result(saved)
            except Exception as exc:
                result["download_error"] = str(exc)[:400]
                result["suggestion"] = "The click completed. Retry saving with browser_download(path=...); do not click again."
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
        element = self._get_element(session_id, ref)
        if not element:
            return self._stale_ref(session_id, ref)

        logger.info("Typing into element @%s", ref)
        try:
            element.fill(text, timeout=5_000)
        except Exception as e:
            logger.warning("Type error: %s", e)
            return self._action_error(e)
        return self._after_action(session_id)

    def scroll(self, session_id: str, direction: str = "down", ref: str | None = None) -> dict[str, Any]:
        return self._run_on_worker(
            self._scroll_impl,
            session_id,
            direction,
            ref,
            cancel_session_id=session_id,
        )

    def _scroll_impl(self, session_id: str, direction: str = "down", ref: str | None = None) -> dict[str, Any]:
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

        element = self._get_element(session_id, ref) if ref else None
        if ref and element is None:
            return self._stale_ref(session_id, ref)
        try:
            dx, dy = deltas[direction]
            scroll_info = (element.evaluate(_TARGET_SCROLL_JS, {"dx": dx, "dy": dy}) if element is not None
                           else page.evaluate(_SCROLL_JS, {"dx": dx, "dy": dy}))
        except Exception as e:
            logger.warning("Scroll error: %s", e)
            return self._action_error(e)
        result = self._after_action(session_id)
        result["scroll"] = scroll_info
        return result

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
        except Exception as e:
            logger.warning("Press error: %s", e)
            return self._action_error(e)
        return self._after_action(session_id)

    # ── Forms, tabs, dialogs and explicit waits ──────────────────────────────

    def select(self, session_id: str, ref: str, values: list[str]) -> dict[str, Any]:
        return self._run_on_worker(
            self._element_action_impl, session_id, ref, "select", values,
            cancel_session_id=session_id,
        )

    def check(self, session_id: str, ref: str, checked: bool) -> dict[str, Any]:
        return self._run_on_worker(
            self._element_action_impl, session_id, ref, "check", checked,
            cancel_session_id=session_id,
        )

    def hover(self, session_id: str, ref: str) -> dict[str, Any]:
        return self._run_on_worker(
            self._element_action_impl, session_id, ref, "hover", None,
            cancel_session_id=session_id,
        )

    def _element_action_impl(
        self, session_id: str, ref: str, action: str, value: Any,
    ) -> dict[str, Any]:
        element = self._get_element(session_id, ref)
        if element is None:
            return self._stale_ref(session_id, ref)
        try:
            if action == "select":
                if len(value) > 1 and not element.evaluate("el => Boolean(el.multiple)"):
                    return {"success": False, "error_code": "INVALID_ACTION", "action_completed": False,
                            "error": "This select accepts only one option value."}
                element.select_option(value=value, timeout=5_000)
            elif action == "check":
                if value is False and element.evaluate("""el =>
                    el.matches('input[type=radio],[role=radio]') &&
                    (el.checked || el.getAttribute('aria-checked') === 'true')"""):
                    return {"success": False, "error_code": "INVALID_ACTION", "action_completed": False,
                            "error": "A selected radio cannot be unchecked directly; select another radio in its group."}
                element.set_checked(value, timeout=5_000)
            elif action == "hover":
                element.hover(timeout=5_000)
        except Exception as exc:
            return self._action_error(exc)
        return self._after_action(session_id)

    def upload(self, session_id: str, files: list[str], ref: str | None = None) -> dict[str, Any]:
        return self._run_on_worker(
            self._upload_impl, session_id, files, ref, cancel_session_id=session_id,
        )

    def _upload_impl(self, session_id: str, files: list[str], ref: str | None) -> dict[str, Any]:
        _, state = self._get_or_create_page_impl(session_id)
        tab_id = self._tab_id(state)
        if ref:
            element = self._get_element(session_id, ref)
            if element is None:
                return self._stale_ref(session_id, ref)
        else:
            chooser = state.file_choosers.get(tab_id)
            if chooser is None:
                return {"success": False, "error_code": "NO_FILE_CHOOSER",
                        "error": "Provide a file input ref, or first click the page's upload button."}
        try:
            if ref:
                element.set_input_files(files, timeout=5_000)
            else:
                chooser.set_files(files, timeout=5_000)
            state.file_choosers.pop(tab_id, None)
        except Exception as exc:
            return self._action_error(exc)
        return self._after_action(session_id)

    def tabs(self, session_id: str, action: str = "list", tab_id: str | None = None, offset: int = 0) -> dict[str, Any]:
        return self._run_on_worker(
            self._tabs_impl, session_id, action, tab_id, offset, cancel_session_id=session_id,
        )

    def _tabs_impl(self, session_id: str, action: str, tab_id: str | None, offset: int = 0) -> dict[str, Any]:
        page, state = self._get_or_create_page_impl(session_id)
        if action == "list":
            # Pump pending popup/close events without evaluating page scripts.
            page.wait_for_timeout(1)
            tabs = self._tabs_info(state)
            result = {"success": True, "tab_id": self._tab_id(state), "tabs": tabs[offset:offset + 20],
                      "tab_count": len(tabs), "offset": offset}
            if len(tabs) > offset + 20:
                result["next_offset"] = offset + 20
            return result
        if action not in {"new", "select", "close"}:
            return {"success": False, "error": "action must be list, new, select, or close"}
        target_id = tab_id or self._tab_id(state)
        target = state.tabs.get(target_id)
        if action != "new" and (target is None or target.is_closed()):
            return {"success": False, "error_code": "TAB_NOT_FOUND", "error": f"Unknown tab: {target_id}",
                    "tabs": self._tabs_info(state)[:5]}
        try:
            self._clear_refs(state)
            if action == "new":
                state.page = state.context.new_page()
                self._register_page(state, state.page)
            elif action == "select":
                state.page = target
                target.bring_to_front()
            else:
                target.close()
                state.tabs.pop(target_id, None)
                state.dialog_policies.pop(target_id, None)
                state.file_choosers.pop(target_id, None)
                # Recreate a blank tab when the last tab is closed.
                self._get_or_create_page_impl(session_id)
        except Exception as exc:
            return self._action_error(exc)
        return self._after_action(session_id)

    def handle_dialog(
        self, session_id: str, accept: bool, prompt_text: str | None = None,
    ) -> dict[str, Any]:
        return self._run_on_worker(
            self._handle_dialog_impl, session_id, accept, prompt_text,
            cancel_session_id=session_id,
        )

    def _handle_dialog_impl(
        self, session_id: str, accept: bool, prompt_text: str | None,
    ) -> dict[str, Any]:
        _, state = self._get_or_create_page_impl(session_id)
        tab_id = self._tab_id(state)
        state.dialog_policies[tab_id] = (accept, prompt_text, time.monotonic() + 60)
        return {"success": True, "tab_id": tab_id, "armed": True, "accept": accept,
                "message": "Response applies to the next action in this tab and expires after that action or 60 seconds. Trigger it now."}

    def back(self, session_id: str) -> dict[str, Any]:
        return self._run_on_worker(self._back_impl, session_id, cancel_session_id=session_id)

    def _back_impl(self, session_id: str) -> dict[str, Any]:
        page, state = self._get_or_create_page_impl(session_id)
        self._clear_refs(state)
        state.file_choosers.pop(self._tab_id(state), None)
        try:
            page.go_back(wait_until="domcontentloaded")
        except Exception as exc:
            return self._action_error(exc)
        return self._after_action(session_id)

    def wait_for(
        self, session_id: str, text: str | None = None, ref: str | None = None,
        state: str = "visible", timeout_ms: int = 10_000,
    ) -> dict[str, Any]:
        return self._run_on_worker(
            self._wait_for_impl, session_id, text, ref, state, timeout_ms,
            cancel_session_id=session_id,
            operation_timeout=timeout_ms / 1000 + 5,
        )

    def _wait_for_impl(
        self, session_id: str, text: str | None, ref: str | None, state: str, timeout_ms: int,
    ) -> dict[str, Any]:
        page, browser_state = self._get_or_create_page_impl(session_id)
        # A known node that has just detached already satisfies hidden. Only
        # refs missing from the latest snapshot's map are stale for this wait.
        element = browser_state.ref_map.get(ref) if ref else None
        if ref and element is None:
            return self._stale_ref(session_id, ref)
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            try:
                if element is not None:
                    owner = element.owner_frame()
                    visible = element.is_visible() and owner is not None and self._frame_is_visible(owner)
                else:
                    visible = any(
                        self._frame_is_visible(frame) and
                        frame.get_by_text(text, exact=False).and_(frame.locator(":visible")).count() > 0
                        for frame in page.frames
                    )
            except Exception as exc:
                return {"success": False, "error": str(exc), "error_code": "WAIT_FAILED"}
            if visible == (state == "visible"):
                result = self._snapshot_impl(session_id)
                result["condition_met"] = True
                return result
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                result = self._snapshot_impl(session_id)
                result.update(success=False, error_code="WAIT_TIMEOUT", condition_met=False,
                              error=f"Condition did not become {state} within {timeout_ms} ms.")
                return result
            # Playwright pumps events here, so cancellation can close this context.
            page.wait_for_timeout(min(100, remaining * 1000))

    # ── Screenshot ────────────────────────────────────────────────────────────

    def screenshot(
        self, session_id: str, path: str | None = None, full_page: bool = True,
    ) -> dict[str, Any]:
        return self._run_on_worker(
            self._screenshot_impl,
            session_id,
            path,
            full_page,
            cancel_session_id=session_id,
        )

    def _screenshot_impl(
        self, session_id: str, path: str | None = None, full_page: bool = True,
    ) -> dict[str, Any]:
        page, state = self._get_or_create_page_impl(session_id)
        self._settle_if_needed(page, state)

        managed = not path
        if managed:
            filename = f"screenshot_{uuid.uuid4().hex}.png"
            path = str(state.downloads_dir / filename)

        logger.info("Taking screenshot: %s", path)
        temporary = None
        try:
            path_obj = self._checked_output(Path(path), managed=managed, tool_name="browser_screenshot")
            temporary = self._temporary_output(path_obj)
            image_type = "jpeg" if path_obj.suffix.lower() in {".jpg", ".jpeg"} else "png"
            page.screenshot(path=str(temporary), type=image_type, full_page=full_page)
            temporary.replace(path_obj)
            state.last_activity = time.time()
            return {
                "success": True,
                "path": str(path_obj.resolve()),
                "size": path_obj.stat().st_size,
                "mime_type": "image/jpeg" if path_obj.suffix.lower() in {".jpg", ".jpeg"} else "image/png",
                "full_page": full_page,
            }
        except Exception as e:
            logger.warning("Screenshot error: %s", e)
            return {"success": False, "error": str(e)}
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

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
        page.wait_for_timeout(1)  # Deliver download events queued since the last call.
        previous_downloads = set(state.pending_downloads)

        if ref:
            element = self._get_element(session_id, ref)
            if not element:
                return self._stale_ref(session_id, ref)
            action_completed = False
            try:
                logger.info("Clicking element @%s and waiting for download", ref)
                with page.expect_download(timeout=10_000) as download_info:
                    element.click(timeout=5_000)
                    action_completed = True
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
                return self._download_error(e, state, action_completed=action_completed,
                                            captured=any(item not in previous_downloads for item in state.pending_downloads))

        if not url:
            if state.pending_downloads:
                try:
                    saved = self._save_download(state, state.pending_downloads[-1], path, queue_download=False)
                    return self._download_result(saved)
                except Exception as exc:
                    return self._download_error(exc, state)
            if state.downloads:
                queued = state.downloads[-1]
                try:
                    queued = self._move_download(queued, state, path)
                    state.downloads.pop()
                    return self._download_result(queued)
                except Exception as e:
                    return self._download_error(e, state)
            return {
                "success": False,
                "error": "No download was captured.",
                "suggestion": "Provide url or ref. Downloads that already completed before this session cannot be captured.",
            }

        try:
            from mclaw.tools.browser_download import download_url

            logger.info("Triggering download from URL: %s", url)
            with download_url(state.context, url) as captured:
                downloaded_file = self._save_download(state, captured, path=path, queue_download=False)
            state.last_activity = time.time()
            return self._download_result(downloaded_file)
        except Exception as e:
            logger.warning("Download navigation error: %s", e)
            return self._download_error(e, state,
                                        captured=any(item not in previous_downloads for item in state.pending_downloads))

    @staticmethod
    def _download_error(exc: Exception, state: _SessionState, *, action_completed: bool = False,
                        captured: bool | None = None) -> dict[str, Any]:
        if captured is None:
            captured = bool(state.pending_downloads or state.downloads)
        return {"success": False, "error": str(exc)[:1000], "error_code": "DOWNLOAD_FAILED",
                "action_completed": action_completed, "completion_unknown": not action_completed,
                "download_retained": captured,
                "suggestion": ("Download retained. Retry browser_download with only a new path; do not click again."
                               if captured else "Inspect browser_snapshot before retrying; the action may have completed.")}
