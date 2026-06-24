# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Runtime coordination for delegated subagent result collection."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from mclaw.prompts.delegation import build_delegate_synthesis_extra_system


@dataclass(frozen=True)
class RuntimeDelegationHooks:
    """Host operations used while resolving a pending delegated task."""

    emit_delegation_started: Callable[[dict[str, Any]], None]
    make_subtask_manager: Callable[[int, list], Any]
    set_subtask_manager: Callable[[Any], None]
    replay_pending_subagent_events: Callable[[Any], None]
    update_subagent_status: Callable[[], None]
    set_delegating_status: Callable[[int], None]
    set_aggregating_status: Callable[[], None]
    set_synthesis_status: Callable[[], None]
    clear_stream_state: Callable[[], None]
    clear_subagent_state: Callable[[], None]
    emit_delegation_completed: Callable[[dict[str, Any]], None]
    invalidate: Callable[[], None]
    sleep: Callable[[float], None]
    get_pending_result: Callable[[str, float], dict[str, Any] | None]
    render_aggregation: Callable[[dict[str, Any]], str]
    render_result_timeout: Callable[[], None]
    render_display_error: Callable[[str], None]
    run_synthesis: Callable[[str, str], dict[str, Any]]
    render_synthesis_response: Callable[[dict[str, Any]], None]
    render_synthesis_error: Callable[[str], None]
    render_synthesis_timeout: Callable[[], None]
    log_info: Callable[[str, tuple[Any, ...]], None]
    log_warning: Callable[[str, tuple[Any, ...]], None]


class RuntimeDelegationCoordinator:
    """Collects delegated subagent results and runs parent synthesis."""

    def __init__(
        self,
        hooks: RuntimeDelegationHooks,
        *,
        startup_delay_seconds: float = 0.3,
        poll_timeout_seconds: float = 600.0,
        queue_grace_seconds: float = 5.0,
        final_attempts: int = 10,
        final_attempt_timeout_seconds: float = 0.2,
        render_settle_seconds: float = 0.4,
        synthesis_timeout_seconds: float | None = None,
    ) -> None:
        self.hooks = hooks
        self.startup_delay_seconds = startup_delay_seconds
        self.poll_timeout_seconds = poll_timeout_seconds
        self.queue_grace_seconds = queue_grace_seconds
        self.final_attempts = final_attempts
        self.final_attempt_timeout_seconds = final_attempt_timeout_seconds
        self.render_settle_seconds = render_settle_seconds
        self.synthesis_timeout_seconds = synthesis_timeout_seconds

    def handle_pending_delegate(self, result: dict[str, Any]) -> bool:
        if not result.get("pending_delegate"):
            return False

        pending_data = result.get("pending_data", {}) or {}
        self.hooks.emit_delegation_started(pending_data)
        task_id = str(pending_data.get("task_id") or "")
        num_tasks = int(pending_data.get("num_tasks") or 1)
        task_info = pending_data.get("task_info", {}) or {}
        goals = task_info.get("goals", []) or []

        manager = self.hooks.make_subtask_manager(num_tasks, goals)
        self.hooks.set_subtask_manager(manager)
        self.hooks.replay_pending_subagent_events(manager)
        self.hooks.update_subagent_status()
        self.hooks.set_delegating_status(num_tasks)
        self.hooks.invalidate()
        self.hooks.log_info("[DELEGATE TUI] polling started task_id=%s num_tasks=%s", (task_id, num_tasks))

        self.hooks.sleep(self.startup_delay_seconds)
        pending_result = self._poll_result(task_id, manager)
        if pending_result is None:
            self.hooks.log_warning("[DELEGATE TUI] result timeout or missing task_id=%s", (task_id,))

        self.hooks.invalidate()
        self.hooks.sleep(self.render_settle_seconds)

        self.hooks.set_aggregating_status()
        synthesis_prompt = self._render_result(pending_result)
        self.hooks.clear_subagent_state()
        self.hooks.emit_delegation_completed(pending_result or {})

        if synthesis_prompt:
            self._run_synthesis(synthesis_prompt)
        return True

    def _poll_result(self, task_id: str, manager: Any) -> dict[str, Any] | None:
        deadline = time.time() + max(0.0, self.poll_timeout_seconds)
        while time.time() < deadline:
            pending_result = self.hooks.get_pending_result(task_id, 0.2)
            if pending_result:
                self.hooks.log_info("[DELEGATE TUI] result received task_id=%s", (task_id,))
                return pending_result
            if _completion_event_is_set(manager):
                self.hooks.log_info("[DELEGATE TUI] completion event set task_id=%s; entering queue grace", (task_id,))
                grace_deadline = time.time() + max(0.0, self.queue_grace_seconds)
                while time.time() < grace_deadline:
                    pending_result = self.hooks.get_pending_result(task_id, 0.3)
                    if pending_result:
                        self.hooks.log_info("[DELEGATE TUI] result received during grace task_id=%s", (task_id,))
                        return pending_result
                break
            self.hooks.invalidate()
            self.hooks.sleep(0.05)

        if _completion_event_is_set(manager):
            for _ in range(max(0, self.final_attempts)):
                pending_result = self.hooks.get_pending_result(task_id, self.final_attempt_timeout_seconds)
                if pending_result:
                    self.hooks.log_info("[DELEGATE TUI] result received during final fallback task_id=%s", (task_id,))
                    return pending_result
        return None

    def _render_result(self, pending_result: dict[str, Any] | None) -> str:
        try:
            if pending_result:
                return self.hooks.render_aggregation(pending_result)
            self.hooks.render_result_timeout()
            return ""
        except Exception as exc:
            self.hooks.render_display_error(str(exc))
            return ""

    def _run_synthesis(self, synthesis_prompt: str) -> None:
        self.hooks.set_synthesis_status()
        self.hooks.invalidate()
        self.hooks.clear_stream_state()
        result_container: list[dict[str, Any] | None] = [None]
        extra_system = build_delegate_synthesis_extra_system()

        def _target() -> None:
            try:
                result_container[0] = self.hooks.run_synthesis(synthesis_prompt, extra_system)
            except Exception as exc:
                self.hooks.log_warning("Synthesis error: %s", (exc,))
                result_container[0] = {"final_response": None, "error": str(exc)}

        thread = threading.Thread(target=_target, daemon=True)
        thread.start()
        if self.synthesis_timeout_seconds is None:
            thread.join()
        else:
            thread.join(timeout=max(0.0, self.synthesis_timeout_seconds))

        synth_result = result_container[0] or {"final_response": None}
        if synth_result.get("final_response"):
            self.hooks.render_synthesis_response(synth_result)
        elif synth_result.get("error"):
            self.hooks.render_synthesis_error(str(synth_result["error"]))
        else:
            self.hooks.render_synthesis_timeout()


def _completion_event_is_set(manager: Any) -> bool:
    event = getattr(manager, "completion_event", None)
    if event is None:
        return False
    is_set = getattr(event, "is_set", None)
    return bool(is_set()) if callable(is_set) else False
