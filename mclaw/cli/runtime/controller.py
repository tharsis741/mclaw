"""Frontend controller boundary for interactive M-Claw runtimes."""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from .events import EventBus, MClawEvent
from .interactive import InteractiveRuntime
from .session import RuntimeSessionState
from .workers import RuntimeWorkerSupervisor


class RuntimeController:
    """Stable interface used by classic TUI to drive an interactive runtime.

    The current implementation adapts the existing InteractiveChat host. This
    keeps TUI code off host private fields while the runtime is extracted
    into a dedicated object in the next refactor step.
    """

    def __init__(self, host: Any) -> None:
        self._host = host
        runtime = getattr(host, "runtime", None)
        if not isinstance(runtime, InteractiveRuntime):
            runtime = InteractiveRuntime(
                event_bus=getattr(host, "event_bus", EventBus()),
                session_state=getattr(host, "runtime_state", None) or RuntimeSessionState(),
            )
            setattr(host, "runtime", runtime)
        setattr(host, "event_bus", runtime.event_bus)
        setattr(host, "runtime_state", runtime.session_state)
        self.runtime = runtime

    @property
    def host(self) -> Any:
        return self._host

    @property
    def model(self) -> str:
        return str(getattr(self._host, "model", ""))

    @property
    def provider(self) -> str:
        return str(getattr(self._host, "provider", ""))

    @property
    def cwd(self) -> str:
        return os.getcwd()

    @property
    def config(self) -> dict:
        config = getattr(self._host, "config", {})
        return config if isinstance(config, dict) else {}

    @property
    def session_id(self) -> str:
        return str(getattr(self._host, "session_id", ""))

    @property
    def event_bus(self) -> EventBus:
        return self.runtime.event_bus

    @property
    def skill_registry(self) -> Any:
        return getattr(self._host, "skill_registry", None)

    @property
    def is_running(self) -> bool:
        return bool(self.runtime.agent_running)

    def subscribe(self, handler: Callable[[MClawEvent], None]) -> Callable[[], None]:
        return self.event_bus.subscribe(handler)

    def request_exit(self, *, force_no_flush: bool = False) -> None:
        self.runtime.request_exit(force_no_flush=force_no_flush)

    def interrupt_or_request_exit(self, *, now: float | None = None, double_tap_seconds: float = 2.0) -> str:
        if not self.is_running:
            self.request_exit()
            return "exit"

        if self.runtime.interrupt_window_hit(now=now, seconds=double_tap_seconds):
            self.request_exit(force_no_flush=True)
            return "force_exit"

        agent = getattr(self._host, "agent", None)
        if agent is not None:
            try:
                agent.interrupt()
            except Exception:
                pass
        return "interrupt"

    def request_status_snapshot(self) -> None:
        getattr(self._host, "_emit_status_snapshot")()

    def start_runtime_threads(self, *, invalidate, exit_ui, is_ui_running):
        hook_factory = getattr(self._host, "build_runtime_worker_hooks", None)
        if callable(hook_factory):
            hooks = hook_factory(
                invalidate=invalidate,
                exit_ui=exit_ui,
                is_ui_running=is_ui_running,
            )
            supervisor = RuntimeWorkerSupervisor(self.runtime, hooks)
            return supervisor.start()
        return getattr(self._host, "_start_runtime_threads")(
            invalidate=invalidate,
            exit_ui=exit_ui,
            is_ui_running=is_ui_running,
        )

    def shutdown_runtime_threads(self, process_thread, anim_thread) -> None:
        getattr(self._host, "_shutdown_runtime_threads")(process_thread, anim_thread)


def get_runtime_controller(host: Any) -> RuntimeController:
    controller = getattr(host, "_runtime_controller", None)
    if isinstance(controller, RuntimeController):
        return controller
    controller = RuntimeController(host)
    setattr(host, "_runtime_controller", controller)
    return controller
