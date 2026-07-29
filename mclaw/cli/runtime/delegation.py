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
from mclaw.tools.interrupt import get_interrupt_event

_SYNTHESIS_CANCEL_UNWIND_GRACE_SECONDS = 0.2


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
    render_synthesis_incomplete: Callable[[], None]
    log_info: Callable[[str, tuple[Any, ...]], None]
    log_warning: Callable[[str, tuple[Any, ...]], None]
    register_synthesis_worker: Callable[[threading.Thread], None] | None = None
    unregister_synthesis_worker: Callable[[threading.Thread], None] | None = None
    get_abort_details: Callable[[], dict[str, str]] = lambda: {}
    render_abort: Callable[[str], None] = lambda _message: None


class RuntimeDelegationCoordinator:
    """Collects delegated subagent results and runs parent synthesis."""

    def __init__(
        self,
        hooks: RuntimeDelegationHooks,
        *,
        startup_delay_seconds: float = 0.3,
        poll_timeout_seconds: float | None = None,
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

    def handle_pending_delegate(
        self,
        result: dict[str, Any],
        cancel_event: threading.Event | None = None,
    ) -> bool:
        """Resolve one pending delegation payload and optionally run synthesis."""
        if not result.get("pending_delegate"):
            return False

        pending_data = result.get("pending_data", {}) or {}
        task_id = str(pending_data.get("task_id") or "")
        num_tasks = int(pending_data.get("num_tasks") or 1)
        task_info = pending_data.get("task_info", {}) or {}
        goals = task_info.get("goals", []) or []
        if cancel_event is None:
            cancel_event = get_interrupt_event()

        manager = self.hooks.make_subtask_manager(num_tasks, goals)
        setattr(manager, "delegation_id", task_id)
        self.hooks.set_subtask_manager(manager)
        self.hooks.replay_pending_subagent_events(manager)
        self.hooks.update_subagent_status()
        self.hooks.set_delegating_status(num_tasks)
        self.hooks.emit_delegation_started(pending_data)
        self.hooks.invalidate()
        self.hooks.log_info("[DELEGATE TUI] polling started task_id=%s num_tasks=%s", (task_id, num_tasks))

        if cancel_event is not None:
            cancel_event.wait(max(0.0, self.startup_delay_seconds))
        else:
            self.hooks.sleep(self.startup_delay_seconds)
        pending_result = self._poll_result(task_id, manager, cancel_event)
        if cancel_event is not None and cancel_event.is_set():
            return self._finish_cancelled(result, task_id)
        if pending_result is None:
            self.hooks.log_warning("[DELEGATE TUI] result timeout or missing task_id=%s", (task_id,))

        self.hooks.invalidate()
        if cancel_event is not None:
            cancel_event.wait(max(0.0, self.render_settle_seconds))
            if cancel_event.is_set():
                return self._finish_cancelled(result, task_id)
        else:
            self.hooks.sleep(self.render_settle_seconds)

        self.hooks.set_aggregating_status()
        synthesis_prompt, aggregation_error = self._render_result(pending_result)
        self.hooks.clear_subagent_state()
        self.hooks.emit_delegation_completed(pending_result or {})

        if synthesis_prompt:
            synthesis_result = self._run_synthesis(synthesis_prompt, cancel_event)
            if synthesis_result is None:
                result["interrupted"] = True
                result["completed"] = False
                result["pending_delegate"] = False
                result.pop("pending_data", None)
                self._apply_abort_details(result)
            else:
                previous_api_calls = int(result.get("api_calls") or 0)
                previous_rounds = result.get("assistant_rounds")
                previous_usage = result.get("token_usage")
                previous_skills_changed = bool(result.get("skills_changed"))
                result.update(synthesis_result)
                result["pending_delegate"] = False
                result.pop("pending_data", None)
                result["interrupted"] = False
                if "api_calls" in synthesis_result:
                    result["api_calls"] = previous_api_calls + int(
                        synthesis_result.get("api_calls") or 0
                    )
                synthesis_rounds = synthesis_result.get("assistant_rounds")
                if isinstance(synthesis_rounds, list):
                    result["assistant_rounds"] = (
                        previous_rounds if isinstance(previous_rounds, list) else []
                    ) + synthesis_rounds
                synthesis_usage = synthesis_result.get("token_usage")
                if isinstance(previous_usage, dict) and isinstance(synthesis_usage, dict):
                    result["token_usage"] = {
                        key: int(previous_usage.get(key) or 0)
                        + int(synthesis_usage.get(key) or 0)
                        for key in previous_usage.keys() | synthesis_usage.keys()
                    }
                result["skills_changed"] = previous_skills_changed or bool(
                    synthesis_result.get("skills_changed")
                )
        elif aggregation_error:
            result["pending_delegate"] = False
            result.pop("pending_data", None)
            result["interrupted"] = False
            result["completed"] = False
            result["stop_reason"] = "delegate_aggregation_error"
            result["error"] = f"显示结果时出错: {aggregation_error}"
        elif pending_result is None:
            result["pending_delegate"] = False
            result.pop("pending_data", None)
            result["interrupted"] = False
            result["completed"] = False
            result["stop_reason"] = "delegate_result_timeout"
            result["error"] = "子代理结果获取超时"
        return True

    def _finish_cancelled(self, result: dict[str, Any], task_id: str) -> bool:
        """Close the parent-side delegation UI without waiting for stale children."""
        result["interrupted"] = True
        result["completed"] = False
        result["pending_delegate"] = False
        result.pop("pending_data", None)
        self._apply_abort_details(result)
        self.hooks.clear_subagent_state()
        self.hooks.emit_delegation_completed({"task_id": task_id, "interrupted": True})
        self.hooks.invalidate()
        return True

    def _apply_abort_details(self, result: dict[str, Any]) -> None:
        details = self.hooks.get_abort_details() or {}
        abort_reason = str(details.get("abort_reason") or "")
        abort_message = str(details.get("abort_message") or "")
        if abort_reason:
            result["abort_reason"] = abort_reason
            result["stop_reason"] = abort_reason
        if abort_message:
            result["abort_message"] = abort_message
            self.hooks.render_abort(abort_message)

    def _poll_result(
        self,
        task_id: str,
        manager: Any,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any] | None:
        """Poll the subagent result queue with completion-event grace windows."""
        deadline = (
            None
            if self.poll_timeout_seconds is None
            else time.time() + max(0.0, self.poll_timeout_seconds)
        )
        while deadline is None or time.time() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                return None
            pending_result = self.hooks.get_pending_result(task_id, 0.2)
            if pending_result:
                self.hooks.log_info("[DELEGATE TUI] result received task_id=%s", (task_id,))
                return pending_result
            if _completion_event_is_set(manager):
                self.hooks.log_info("[DELEGATE TUI] completion event set task_id=%s; entering queue grace", (task_id,))
                grace_deadline = time.time() + max(0.0, self.queue_grace_seconds)
                while time.time() < grace_deadline:
                    if cancel_event is not None and cancel_event.is_set():
                        return None
                    pending_result = self.hooks.get_pending_result(task_id, 0.3)
                    if pending_result:
                        self.hooks.log_info("[DELEGATE TUI] result received during grace task_id=%s", (task_id,))
                        return pending_result
                break
            self.hooks.invalidate()
            self.hooks.sleep(0.05)

        if cancel_event is not None and cancel_event.is_set():
            return None
        if _completion_event_is_set(manager):
            for _ in range(max(0, self.final_attempts)):
                if cancel_event is not None and cancel_event.is_set():
                    return None
                pending_result = self.hooks.get_pending_result(task_id, self.final_attempt_timeout_seconds)
                if pending_result:
                    self.hooks.log_info("[DELEGATE TUI] result received during final fallback task_id=%s", (task_id,))
                    return pending_result
        return None

    def _render_result(
        self,
        pending_result: dict[str, Any] | None,
    ) -> tuple[str, str | None]:
        """Render aggregation output and return the prompt used for synthesis."""
        try:
            if pending_result:
                return self.hooks.render_aggregation(pending_result), None
            self.hooks.render_result_timeout()
            return "", None
        except Exception as exc:
            self.hooks.render_display_error(str(exc))
            return "", str(exc)

    def _run_synthesis(
        self,
        synthesis_prompt: str,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any] | None:
        """Run parent synthesis in a worker so UI timeout policy stays local."""
        if cancel_event is not None and cancel_event.is_set():
            return
        self.hooks.set_synthesis_status()
        self.hooks.invalidate()
        self.hooks.clear_stream_state()
        result_container: list[dict[str, Any] | None] = [None]
        decision = ["pending"]
        decision_lock = threading.Lock()
        extra_system = build_delegate_synthesis_extra_system()

        def _target() -> None:
            synthesis_result: dict[str, Any] | None = None
            try:
                synthesis_result = self.hooks.run_synthesis(
                    synthesis_prompt,
                    extra_system,
                )
            except Exception as exc:
                self.hooks.log_warning("Synthesis error: %s", (exc,))
                synthesis_result = {"final_response": None, "error": str(exc)}
            finally:
                with decision_lock:
                    if decision[0] == "pending":
                        result_container[0] = synthesis_result
                        decision[0] = "completed"
                if self.hooks.unregister_synthesis_worker is not None:
                    self.hooks.unregister_synthesis_worker(threading.current_thread())

        def _commit_stop(reason: str) -> str:
            with decision_lock:
                if decision[0] == "pending":
                    decision[0] = reason
                return decision[0]

        def _snapshot() -> tuple[str, dict[str, Any] | None]:
            with decision_lock:
                return decision[0], result_container[0]

        thread = threading.Thread(target=_target, daemon=True)
        if self.hooks.register_synthesis_worker is not None:
            self.hooks.register_synthesis_worker(thread)
        try:
            thread.start()
        except BaseException:
            if self.hooks.unregister_synthesis_worker is not None:
                self.hooks.unregister_synthesis_worker(thread)
            raise
        deadline = (
            None
            if self.synthesis_timeout_seconds is None
            else time.monotonic() + max(0.0, self.synthesis_timeout_seconds)
        )
        while thread.is_alive():
            if cancel_event is not None and cancel_event.wait(0.05):
                if _commit_stop("cancelled") == "cancelled":
                    thread.join(_SYNTHESIS_CANCEL_UNWIND_GRACE_SECONDS)
                    return
                break
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                thread.join(timeout=min(0.05, remaining))
            else:
                thread.join(timeout=0.05)

        state, synth_result = _snapshot()
        if state != "completed" and cancel_event is not None and cancel_event.is_set():
            state = _commit_stop("cancelled")
            if state == "cancelled":
                thread.join(_SYNTHESIS_CANCEL_UNWIND_GRACE_SECONDS)
                return
            state, synth_result = _snapshot()
        if state != "completed" and thread.is_alive():
            if _commit_stop("timed_out") == "completed":
                state, synth_result = _snapshot()
            else:
                self.hooks.log_warning(
                    "[DELEGATE TUI] synthesis deadline exceeded; worker still alive",
                    (),
                )
                self.hooks.render_synthesis_timeout()
                return {
                    "completed": False,
                    "stop_reason": "synthesis_timeout",
                    "error": "综合结果超时",
                }

        synth_result = synth_result or {"final_response": None}
        if synth_result.get("interrupted"):
            return None
        if synth_result.get("error"):
            message = str(synth_result["error"])
            self.hooks.render_synthesis_error(message)
            return {
                **synth_result,
                "completed": False,
                "stop_reason": "synthesis_error",
                "error": f"综合结果时出错: {message}",
            }
        if synth_result.get("final_response") and synth_result.get("completed") is not False:
            self.hooks.render_synthesis_response(synth_result)
            return {
                **synth_result,
                "completed": True,
                "stop_reason": synth_result.get("stop_reason") or "synthesis_completed",
            }

        pending_data = synth_result.get("pending_data", {}) or {}
        self.hooks.log_warning(
            "[DELEGATE TUI] synthesis incomplete stop_reason=%s nested_task_id=%s",
            (
                str(synth_result.get("stop_reason") or ""),
                str(pending_data.get("task_id") or ""),
            ),
        )
        self.hooks.render_synthesis_incomplete()
        return {
            **synth_result,
            "completed": False,
            "stop_reason": "synthesis_incomplete",
            "error": "综合结果未完成：模型没有返回最终内容",
        }


def _completion_event_is_set(manager: Any) -> bool:
    """Read a manager completion event without depending on its concrete type."""
    event = getattr(manager, "completion_event", None)
    if event is None:
        return False
    is_set = getattr(event, "is_set", None)
    return bool(is_set()) if callable(is_set) else False
