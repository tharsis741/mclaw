# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""UI-neutral worker supervision for interactive runtimes."""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from .interactive import InteractiveRuntime


@dataclass(frozen=True)
class RuntimeWorkerHooks:
    """Callbacks supplied by the agent host to the runtime worker supervisor."""

    handle_input: Callable[[str], None]
    on_idle: Callable[[], None]
    on_animation_tick: Callable[[], None]
    on_error: Callable[[Exception], None]
    invalidate: Callable[[], None]
    animation_enabled: Callable[[], bool]
    animation_interval: Callable[[bool], float]


@dataclass(frozen=True)
class RuntimeWorkerHandles:
    """Thread handles returned by the runtime worker supervisor."""

    process_thread: threading.Thread
    animation_thread: threading.Thread

    def __iter__(self):
        yield self.process_thread
        yield self.animation_thread


class RuntimeWorkerSupervisor:
    """Owns input and animation worker loops for an interactive runtime."""

    def __init__(self, runtime: InteractiveRuntime, hooks: RuntimeWorkerHooks) -> None:
        self.runtime = runtime
        self.hooks = hooks

    def start(self) -> RuntimeWorkerHandles:
        """Start daemon worker threads owned by the interactive runtime."""
        process_thread = threading.Thread(target=self._process_loop, daemon=True)
        animation_thread = threading.Thread(target=self._animation_loop, daemon=True)
        started_threads: list[threading.Thread] = []
        try:
            process_thread.start()
            started_threads.append(process_thread)
            animation_thread.start()
            started_threads.append(animation_thread)
        except BaseException:
            self.runtime.request_exit()
            deadline = time.monotonic() + 2.0
            for thread in reversed(started_threads):
                try:
                    thread.join(timeout=max(0.0, deadline - time.monotonic()))
                except BaseException:
                    pass
            raise
        return RuntimeWorkerHandles(process_thread, animation_thread)

    def _safe_invalidate(self) -> None:
        """Request a frontend redraw without letting renderer failures kill a worker."""
        try:
            self.hooks.invalidate()
        except Exception:
            pass

    def _process_loop(self) -> None:
        """Drain queued user input while allowing idle hooks between messages."""
        while not self.runtime.should_exit:
            try:
                try:
                    user_input = self.runtime.pending_input.get(timeout=0.15)
                except queue.Empty:
                    if not self.runtime.agent_running:
                        self.hooks.on_idle()
                    continue

                if not user_input:
                    self.hooks.on_idle()
                    continue

                self.hooks.handle_input(str(user_input))
            except Exception as exc:
                self.hooks.on_error(exc)

    def _animation_loop(self) -> None:
        """Tick animation and redraw hooks until cooperative runtime shutdown."""
        while not self.runtime.should_exit:
            running = bool(self.runtime.agent_running)
            interval = self.hooks.animation_interval(running)
            self.hooks.on_animation_tick()
            if self.hooks.animation_enabled():
                self._safe_invalidate()
            time.sleep(interval)
