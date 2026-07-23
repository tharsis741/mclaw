from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

from mclaw.cli.runtime.delegation import RuntimeDelegationCoordinator, RuntimeDelegationHooks
from mclaw.cli.app import InteractiveChat
from mclaw.tools import delegate_tool
from mclaw.tools.interrupt import reset_interrupt_event, set_interrupt_event


def _hooks(events: list, **overrides) -> RuntimeDelegationHooks:
    defaults = {
        "emit_delegation_started": lambda payload: events.append(("started", payload)),
        "make_subtask_manager": lambda _count, _goals: SimpleNamespace(
            completion_event=threading.Event()
        ),
        "set_subtask_manager": lambda manager: events.append(("manager", manager)),
        "replay_pending_subagent_events": lambda _manager: None,
        "update_subagent_status": lambda: None,
        "set_delegating_status": lambda _count: None,
        "set_aggregating_status": lambda: events.append(("aggregating", None)),
        "set_synthesis_status": lambda: events.append(("synthesizing", None)),
        "clear_stream_state": lambda: None,
        "clear_subagent_state": lambda: events.append(("cleared", None)),
        "emit_delegation_completed": lambda payload: events.append(("completed", payload)),
        "invalidate": lambda: None,
        "sleep": time.sleep,
        "get_pending_result": lambda _task_id, _timeout: None,
        "render_aggregation": lambda _result: "synthesis prompt",
        "render_result_timeout": lambda: events.append(("timeout", None)),
        "render_display_error": lambda error: events.append(("display_error", error)),
        "run_synthesis": lambda _prompt, _system: {"final_response": "done"},
        "render_synthesis_response": lambda result: events.append(("response", result)),
        "render_synthesis_error": lambda error: events.append(("synthesis_error", error)),
        "render_synthesis_timeout": lambda: events.append(("synthesis_timeout", None)),
        "log_info": lambda _message, _args: None,
        "log_warning": lambda _message, _args: None,
    }
    defaults.update(overrides)
    return RuntimeDelegationHooks(**defaults)


def _pending_result() -> dict:
    return {
        "pending_delegate": True,
        "pending_data": {
            "task_id": "task-1",
            "num_tasks": 1,
            "task_info": {"goals": ["investigate"]},
        },
        "completed": True,
    }


def test_pending_delegation_cancel_wakes_startup_wait_and_closes_ui() -> None:
    cancel_event = threading.Event()
    events: list = []
    pending_calls = []
    coordinator = RuntimeDelegationCoordinator(
        _hooks(
            events,
            get_pending_result=lambda *_args: pending_calls.append(True),
        ),
        startup_delay_seconds=5,
    )
    result = _pending_result()
    token = set_interrupt_event(cancel_event)
    timer = threading.Timer(0.05, cancel_event.set)
    timer.start()
    started = time.monotonic()
    try:
        assert coordinator.handle_pending_delegate(result) is True
    finally:
        timer.join(1)
        reset_interrupt_event(token)

    assert time.monotonic() - started < 0.5
    assert pending_calls == []
    assert result["interrupted"] is True
    assert result["completed"] is False
    assert ("cleared", None) in events
    assert ("completed", {"task_id": "task-1", "interrupted": True}) in events
    assert all(kind not in {"aggregating", "synthesizing", "response"} for kind, _ in events)


def test_pending_delegation_preserves_child_tool_abort_details() -> None:
    cancel_event = threading.Event()
    cancel_event.set()
    events: list = []
    coordinator = RuntimeDelegationCoordinator(
        _hooks(
            events,
            get_abort_details=lambda: {
                "abort_reason": "tool_completion_unknown",
                "abort_message": "restart required",
            },
            render_abort=lambda message: events.append(("abort", message)),
        ),
        startup_delay_seconds=0,
    )
    result = _pending_result()

    assert coordinator.handle_pending_delegate(result, cancel_event=cancel_event) is True

    assert result["interrupted"] is True
    assert result["completed"] is False
    assert result["abort_reason"] == "tool_completion_unknown"
    assert result["stop_reason"] == "tool_completion_unknown"
    assert result["abort_message"] == "restart required"
    assert ("abort", "restart required") in events


def test_parent_synthesis_cancel_returns_without_rendering_stale_result() -> None:
    cancel_event = threading.Event()
    synthesis_release = threading.Event()
    synthesis_started = threading.Event()
    events: list = []
    registered_workers = []
    unregistered_workers = []

    def run_synthesis(_prompt, _system):
        synthesis_started.set()
        synthesis_release.wait(2)
        return {"final_response": "stale"}

    coordinator = RuntimeDelegationCoordinator(
        _hooks(
            events,
            get_pending_result=lambda *_args: {"task_id": "task-1", "results": []},
            run_synthesis=run_synthesis,
            register_synthesis_worker=registered_workers.append,
            unregister_synthesis_worker=unregistered_workers.append,
        ),
        startup_delay_seconds=0,
        render_settle_seconds=0,
    )
    result = _pending_result()
    token = set_interrupt_event(cancel_event)
    timer = threading.Timer(0.05, cancel_event.set)
    timer.start()
    started = time.monotonic()
    try:
        assert coordinator.handle_pending_delegate(result) is True
        assert synthesis_started.is_set()
        assert len(registered_workers) == 1
        assert registered_workers[0].is_alive()
        assert unregistered_workers == []
    finally:
        timer.join(1)
        synthesis_release.set()
        reset_interrupt_event(token)

    registered_workers[0].join(1)

    assert time.monotonic() - started < 0.5
    assert unregistered_workers == registered_workers
    assert result["interrupted"] is True
    assert result["completed"] is False
    assert all(kind != "response" for kind, _ in events)


def test_delegate_child_receives_the_parent_turn_event(monkeypatch) -> None:
    cancel_event = threading.Event()
    seen_events = []

    class Child:
        max_iterations = 1
        tools = []
        session_api_calls = 0
        session_input_tokens = 0
        session_output_tokens = 0
        session_id = "child-session"
        model = "test-model"
        _delegate_depth = 1

        def run_conversation(self, **kwargs):
            seen_events.append(kwargs.get("cancel_event"))
            return {
                "final_response": "",
                "completed": False,
                "interrupted": True,
                "api_calls": 0,
            }

    monkeypatch.setattr("mclaw.tools.terminal_tool.cleanup_session", lambda _session_id: None)
    result = delegate_tool._run_single_child(
        task_index=0,
        goal="investigate",
        child=Child(),
        parent_agent=SimpleNamespace(),
        cancel_event=cancel_event,
    )

    assert seen_events == [cancel_event]
    assert result["status"] == "interrupted"
    assert result["exit_reason"] == "interrupted"


def test_nonblocking_delegation_coordinator_fences_parent_until_exit(monkeypatch) -> None:
    started = threading.Event()
    release = threading.Event()

    class Parent:
        _delegate_depth = 0
        config = {"delegation": {"timeout_seconds": 10, "max_iterations": 1}}

        def __init__(self) -> None:
            self._delegate_progress_callback = lambda _event: None
            self.workers = set()
            self.lock = threading.Lock()

        def _register_turn_worker(self, worker) -> None:
            with self.lock:
                self.workers.add(worker)

        def _unregister_turn_worker(self, worker) -> None:
            with self.lock:
                self.workers.discard(worker)

    def fake_background(*_args, **_kwargs) -> None:
        started.set()
        release.wait(2)

    parent = Parent()
    monkeypatch.setattr(delegate_tool, "_build_child_agent", lambda **_kwargs: object())
    monkeypatch.setattr(delegate_tool, "_run_all_children_background", fake_background)

    result = delegate_tool.delegate_task([{"goal": "inspect"}], parent_agent=parent)
    assert json.loads(result)["pending"] is True
    assert started.wait(1)
    with parent.lock:
        workers = list(parent.workers)
    assert len(workers) == 1
    assert workers[0].is_alive()

    release.set()
    workers[0].join(1)
    with parent.lock:
        assert parent.workers == set()


def test_late_delegate_progress_cannot_contaminate_the_next_delegation() -> None:
    chat = InteractiveChat.__new__(InteractiveChat)
    chat._pending_subagent_events = []
    chat.subtask_manager = None
    old_event = delegate_tool.SubtaskEvent(
        0,
        delegate_tool.SUBAGENT_COMPLETED,
        {"status": "completed", "summary": "old"},
        delegation_id="old-task",
    )
    new_event = delegate_tool.SubtaskEvent(
        0,
        delegate_tool.SUBAGENT_COMPLETED,
        {"status": "completed", "summary": "new"},
        delegation_id="new-task",
    )
    chat._delegate_progress_callback(old_event)
    chat._delegate_progress_callback(new_event)

    manager = InteractiveChat.SubtaskManager(1, ["new goal"])
    manager.delegation_id = "new-task"
    chat._replay_pending_delegate_events(manager)

    assert manager.tasks[0]["status"] == "completed"
    assert manager.tasks[0]["summary"] == "new"
    assert chat._pending_subagent_events == []

    chat.subtask_manager = manager
    chat._pet_emit = lambda *_args, **_kwargs: None
    chat._update_subagent_status_detail = lambda: None
    chat._delegate_progress_callback(old_event)
    assert manager.tasks[0]["summary"] == "new"
