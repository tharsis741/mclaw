# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Cancellation-safe bridge from synchronous channel tools to asyncio loops."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from typing import Any

from mclaw.channels.base import SendResult
from mclaw.tools.interrupt import get_cancel_id, safe_cancel_trace

logger = logging.getLogger(__name__)
_INTERRUPT_POLL_INTERVAL = 0.05


@dataclass
class OutboundBridgeResult(SendResult):
    """Send result with cancellation state preserved for the tool dispatcher."""

    interrupted: bool = False
    completion_unknown: bool = False


class _OutboundFutureFence:
    """Fence the real coroutine lifetime behind a cancelled proxy Future."""

    def __init__(
        self,
        *,
        platform: str,
        display_name: str,
        parent_agent: Any,
        cancel_event: threading.Event | None,
        label: str,
    ) -> None:
        self.platform = platform
        self.display_name = display_name
        self.parent_agent = parent_agent
        self.cancel_event = cancel_event
        self.label = label
        self.created_at = time.monotonic()
        self._started = threading.Event()
        self._finished = threading.Event()
        self._state_lock = threading.Lock()
        self._registered = False
        self._inner_coro = None
        self._outcome_ready = False
        self._outcome = None
        self._outcome_error: BaseException | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._persistent = False

    @property
    def diagnostic_name(self) -> str:
        return f"{self.platform}_outbound_{self.label}"

    @property
    def blocking_reason(self) -> str:
        age = max(0.0, time.monotonic() - self.created_at)
        if self.persistent:
            return (
                f"{self.display_name} {self.label} send cannot be confirmed because "
                "its event loop stopped; restart the runtime before retrying"
            )
        return (
            f"{self.display_name} {self.label} send is still shutting down after "
            f"cancellation ({age:.1f}s)"
        )

    def is_alive(self) -> bool:
        return not self._finished.is_set()

    @property
    def persistent(self) -> bool:
        loop = self._loop
        return self._persistent or (
            self.is_alive() and loop is not None and not loop.is_running()
        )

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def bind_coroutine(self, coro) -> None:
        self._inner_coro = coro

    def register(self) -> None:
        register = getattr(self.parent_agent, "_register_turn_worker", None)
        if not callable(register):
            return
        register(self)
        with self._state_lock:
            self._registered = True
        safe_cancel_trace(
            lambda: logger.info(
                "[CANCEL_TRACE] outbound_fence_register cancel_id=%s "
                "platform=%s session=%s operation=%s",
                get_cancel_id(self.cancel_event),
                self.platform,
                getattr(self.parent_agent, "session_id", "?"),
                self.label,
            )
        )

    async def run(self, coro):
        """Mark actual Task entry/exit independently of the proxy Future state."""
        with self._state_lock:
            self._started.set()
            self._inner_coro = None
        try:
            result = await coro
        except BaseException as exc:
            with self._state_lock:
                self._outcome_ready = True
                self._outcome_error = exc
            raise
        else:
            with self._state_lock:
                self._outcome_ready = True
                self._outcome = result
            return result
        finally:
            self.finish()

    def terminal_outcome(self) -> tuple[bool, Any, BaseException | None]:
        with self._state_lock:
            return self._outcome_ready, self._outcome, self._outcome_error

    def request_cancel(self, future, loop: asyncio.AbstractEventLoop) -> bool:
        """Cancel the proxy while retaining the fence until the Task really exits."""
        if not loop.is_running():
            self._persistent = True
            safe_cancel_trace(
                lambda: logger.warning(
                    "[CANCEL_TRACE] outbound_loop_stopped cancel_id=%s platform=%s "
                    "session=%s operation=%s persistent_fence=true",
                    get_cancel_id(self.cancel_event),
                    self.platform,
                    getattr(self.parent_agent, "session_id", "?"),
                    self.label,
                )
            )
        proxy_cancelled = future.cancel()
        try:
            # If cancellation won before the Task's first bytecode, ``run`` cannot
            # execute its finally block. This callback runs after submission and
            # proxy cancellation have both reached the owner loop.
            loop.call_soon_threadsafe(self._finish_if_never_started)
        except RuntimeError:
            # A stopped loop cannot prove that submitted work reached a terminal
            # state, so deliberately retain the fail-closed fence.
            pass
        return proxy_cancelled

    def _finish_if_never_started(self) -> None:
        inner_coro = None
        never_started = False
        with self._state_lock:
            if not self._started.is_set():
                never_started = True
                inner_coro = self._inner_coro
                self._inner_coro = None
        if inner_coro is not None:
            try:
                inner_coro.close()
            except BaseException:
                pass
        if never_started:
            self.finish()

    def finish(self) -> None:
        unregister = None
        with self._state_lock:
            if self._finished.is_set():
                return
            self._finished.set()
            self._inner_coro = None
            if self._registered:
                self._registered = False
                unregister = getattr(self.parent_agent, "_unregister_turn_worker", None)
        if callable(unregister):
            try:
                unregister(self)
            except BaseException:
                # ``is_alive`` is already false, so the generic worker registry
                # can still prune this fence on the next safety check.
                pass
        if self.cancel_event is not None and self.cancel_event.is_set():
            safe_cancel_trace(
                lambda: logger.info(
                    "[CANCEL_TRACE] outbound_fence_release cancel_id=%s "
                    "platform=%s session=%s operation=%s elapsed_ms=%d",
                    get_cancel_id(self.cancel_event),
                    self.platform,
                    getattr(self.parent_agent, "session_id", "?"),
                    self.label,
                    int((time.monotonic() - self.created_at) * 1000),
                )
            )


def run_outbound_coroutine(
    coro,
    *,
    loop: asyncio.AbstractEventLoop,
    timeout: float,
    platform: str,
    display_name: str,
    label: str,
    cancel_event: threading.Event | None = None,
    parent_agent: Any = None,
) -> SendResult:
    """Run one channel coroutine with cooperative turn cancellation and fencing."""
    if cancel_event is not None and cancel_event.is_set():
        coro.close()
        safe_cancel_trace(
            lambda: logger.info(
                "[CANCEL_TRACE] outbound_cancel_before_submit cancel_id=%s "
                "platform=%s session=%s operation=%s",
                get_cancel_id(cancel_event),
                platform,
                getattr(parent_agent, "session_id", "?"),
                label,
            )
        )
        return OutboundBridgeResult(
            success=False,
            error=f"{display_name} {label} send interrupted",
            interrupted=True,
            completion_unknown=False,
        )
    if loop.is_closed() or not loop.is_running():
        coro.close()
        state = "closed" if loop.is_closed() else "not running"
        return SendResult(success=False, error=f"{display_name} event loop is {state}")

    fence = _OutboundFutureFence(
        platform=platform,
        display_name=display_name,
        parent_agent=parent_agent,
        cancel_event=cancel_event,
        label=label,
    )
    fence.bind_loop(loop)
    fence.bind_coroutine(coro)
    fence.register()
    tracked = fence.run(coro)
    try:
        future = asyncio.run_coroutine_threadsafe(tracked, loop)
    except RuntimeError as exc:
        tracked.close()
        coro.close()
        fence.finish()
        return SendResult(
            success=False,
            error=f"{display_name} event loop is unavailable: {exc}",
        )

    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        if future.done():
            try:
                return future.result()
            except Exception as exc:
                return OutboundBridgeResult(
                    success=False,
                    error=f"{display_name} {label} send failed: {exc}",
                    completion_unknown=fence.is_alive(),
                )
        if cancel_event is not None and cancel_event.is_set():
            if future.done():
                continue
            proxy_cancelled = fence.request_cancel(future, loop)
            outcome_ready, outcome, outcome_error = fence.terminal_outcome()
            if outcome_ready:
                safe_cancel_trace(
                    lambda: logger.info(
                        "[CANCEL_TRACE] outbound_completion_won_stop "
                        "cancel_id=%s platform=%s session=%s operation=%s "
                        "trigger=event success=%s",
                        get_cancel_id(cancel_event),
                        platform,
                        getattr(parent_agent, "session_id", "?"),
                        label,
                        outcome_error is None,
                    )
                )
            if outcome_ready and outcome_error is None:
                return outcome
            if outcome_ready and not isinstance(outcome_error, asyncio.CancelledError):
                return OutboundBridgeResult(
                    success=False,
                    error=f"{display_name} {label} send failed: {outcome_error}",
                    completion_unknown=False,
                )
            if not proxy_cancelled:
                continue
            completion_unknown = fence.is_alive()
            safe_cancel_trace(
                lambda: logger.warning(
                    "[CANCEL_TRACE] outbound_cancel_request cancel_id=%s "
                    "platform=%s session=%s operation=%s "
                    "proxy_cancelled=%s completion_unknown=%s",
                    get_cancel_id(cancel_event),
                    platform,
                    getattr(parent_agent, "session_id", "?"),
                    label,
                    proxy_cancelled,
                    completion_unknown,
                )
            )
            return OutboundBridgeResult(
                success=False,
                error=f"{display_name} {label} send interrupted",
                interrupted=True,
                completion_unknown=completion_unknown,
            )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if future.done():
                continue
            proxy_cancelled = fence.request_cancel(future, loop)
            outcome_ready, outcome, outcome_error = fence.terminal_outcome()
            if outcome_ready:
                safe_cancel_trace(
                    lambda: logger.info(
                        "[CANCEL_TRACE] outbound_completion_won_stop "
                        "cancel_id=%s platform=%s session=%s operation=%s "
                        "trigger=deadline success=%s",
                        get_cancel_id(cancel_event),
                        platform,
                        getattr(parent_agent, "session_id", "?"),
                        label,
                        outcome_error is None,
                    )
                )
            if outcome_ready and outcome_error is None:
                return outcome
            if outcome_ready and not isinstance(outcome_error, asyncio.CancelledError):
                return OutboundBridgeResult(
                    success=False,
                    error=f"{display_name} {label} send failed: {outcome_error}",
                    completion_unknown=False,
                )
            if not proxy_cancelled:
                continue
            return OutboundBridgeResult(
                success=False,
                error=f"{display_name} {label} send timed out",
                completion_unknown=fence.is_alive(),
            )
        try:
            return future.result(timeout=min(_INTERRUPT_POLL_INTERVAL, remaining))
        except FutureTimeoutError:
            continue
        except Exception as exc:
            return OutboundBridgeResult(
                success=False,
                error=f"{display_name} {label} send failed: {exc}",
                completion_unknown=fence.is_alive(),
            )
