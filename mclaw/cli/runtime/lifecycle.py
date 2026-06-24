"""Lifecycle coordination for interactive runtimes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import Thread

from .interactive import InteractiveRuntime


@dataclass(frozen=True)
class RuntimeShutdownHooks:
    """Host operations used during interactive runtime shutdown."""

    stop_asr_service: Callable[[], None]
    interrupt_agent: Callable[[], None]
    restore_project_env: Callable[[], None]
    stop_pet: Callable[[], None]
    end_session: Callable[[bool], None]
    close_session_db: Callable[[], None]
    clear_terminal_title: Callable[[], None]


class RuntimeShutdownCoordinator:
    """Owns the ordered shutdown sequence for an interactive runtime."""

    def __init__(self, runtime: InteractiveRuntime, hooks: RuntimeShutdownHooks) -> None:
        self.runtime = runtime
        self.hooks = hooks

    def shutdown(self, process_thread: Thread, animation_thread: Thread) -> None:
        self.runtime.request_exit()
        self._safe(self.hooks.stop_asr_service)
        self._safe(self.hooks.interrupt_agent)

        self._join(process_thread, timeout=1.5)
        self._join(animation_thread, timeout=0.5)

        should_flush = not self.runtime.force_exit_no_flush
        self._safe(self.hooks.restore_project_env)
        self._safe(self.hooks.stop_pet)
        self.hooks.end_session(should_flush)
        self.hooks.close_session_db()
        self._safe(self.hooks.clear_terminal_title)

    def _join(self, thread: Thread, *, timeout: float) -> None:
        try:
            thread.join(timeout=timeout)
        except KeyboardInterrupt:
            self.runtime.force_exit_no_flush = True

    @staticmethod
    def _safe(fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception:
            pass
