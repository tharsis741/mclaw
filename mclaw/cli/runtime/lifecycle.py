# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Lifecycle coordination for interactive runtimes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import Thread
import time

from mclaw.dsoftbus.protocol import DSOFTBUS_SHUTDOWN_TIMEOUT_S
from .interactive import InteractiveRuntime


@dataclass(frozen=True)
class RuntimeShutdownHooks:
    """Host operations used during interactive runtime shutdown."""

    stop_asr_service: Callable[[], None]
    interrupt_agent: Callable[[], None]
    begin_dsoftbus_shutdown: Callable[[], None]
    stop_dsoftbus: Callable[[float], None]
    restore_project_env: Callable[[], None]
    stop_pet: Callable[[], None]
    end_session: Callable[[bool, float], None]
    close_active_agent: Callable[[float], bool]
    close_session_db: Callable[[float], bool]
    clear_terminal_title: Callable[[], None]


class RuntimeShutdownCoordinator:
    """Owns the ordered shutdown sequence for an interactive runtime."""

    def __init__(
        self,
        runtime: InteractiveRuntime,
        hooks: RuntimeShutdownHooks,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.runtime = runtime
        self.hooks = hooks
        self._monotonic = monotonic

    def shutdown(
        self,
        process_thread: Thread | None,
        animation_thread: Thread | None,
    ) -> None:
        """Stop runtime services in the order required for a clean terminal exit.

        The sequence interrupts active work before joining UI workers, then
        restores process-level state and decides whether the session should
        flush based on the runtime's force-exit flag.
        """
        deadline = self._monotonic() + DSOFTBUS_SHUTDOWN_TIMEOUT_S
        self.runtime.request_exit()
        self._safe(self.hooks.begin_dsoftbus_shutdown)
        self._safe(self.hooks.stop_asr_service)
        self._safe(self.hooks.interrupt_agent)
        self._safe(self.hooks.stop_dsoftbus, deadline)

        self._join(process_thread, deadline=deadline)
        self._join(animation_thread, deadline=deadline)

        should_flush = not self.runtime.force_exit_no_flush
        self._safe(self.hooks.restore_project_env)
        self._safe(self.hooks.stop_pet)
        self._safe(self.hooks.end_session, should_flush, deadline)
        self._safe(self.hooks.close_active_agent, deadline)
        self._safe(self.hooks.close_session_db, deadline)
        self._safe(self.hooks.clear_terminal_title)

    def _join(self, thread: Thread | None, *, deadline: float) -> None:
        """Join a worker within the one shared shutdown deadline."""
        if thread is None:
            return
        try:
            thread.join(timeout=max(0.0, deadline - self._monotonic()))
        except KeyboardInterrupt:
            self.runtime.force_exit_no_flush = True

    @staticmethod
    def _safe(fn: Callable, *args) -> None:
        try:
            fn(*args)
        except BaseException:
            pass
