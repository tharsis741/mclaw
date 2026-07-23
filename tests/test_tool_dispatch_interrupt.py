import asyncio
import json
import logging
import threading
import time
from types import SimpleNamespace

import pytest

from mclaw.agent.core import MClaw
from mclaw.tools import dispatch
from mclaw.tools.interrupt import (
    get_cancel_id,
    get_interrupt_event,
    reset_interrupt_event,
    set_interrupt_event,
)
from mclaw.tools.registry import ToolRegistry


def _call(name: str) -> dict:
    return {
        "id": name,
        "type": "function",
        "function": {"name": name, "arguments": "{}"},
    }


def _bare_agent(session_id: str = "async-tool-test") -> MClaw:
    agent = MClaw.__new__(MClaw)
    agent.session_id = session_id
    agent.config = {}
    agent._interrupted = False
    agent._interrupt_lock = threading.Lock()
    agent._turn_cancel_event = threading.Event()
    agent._turn_active = False
    agent._turn_abort_reason = None
    agent._turn_workers_lock = threading.Lock()
    agent._outstanding_turn_workers = set()
    agent._turn_workers_drained = threading.Event()
    agent._turn_workers_drained.set()
    agent._turn_worker_parent = None
    return agent


def test_async_handler_that_delays_cancellation_keeps_parent_turn_fenced() -> None:
    cancel_event = threading.Event()
    handler_started = threading.Event()
    cancellation_seen = threading.Event()
    release_handler = threading.Event()
    agent = _bare_agent()
    turn_event = agent.begin_turn(cancel_event)
    local_registry = ToolRegistry()

    async def stubborn_handler(_args, **_kwargs):
        handler_started.set()
        try:
            while True:
                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            cancellation_seen.set()
            while not release_handler.is_set():
                await asyncio.sleep(0.01)
            return json.dumps({"success": False, "status": "cleanup_finished"})

    local_registry.register(
        name="stubborn_async",
        toolset="test",
        schema={"type": "function", "function": {"name": "stubborn_async"}},
        handler=stubborn_handler,
        is_async=True,
    )
    interrupt_token = set_interrupt_event(cancel_event)
    timer = threading.Timer(0.05, cancel_event.set)
    timer.start()
    ended = False
    try:
        result = local_registry.dispatch(
            "stubborn_async",
            {},
            parent_agent=agent,
        )
        assert handler_started.is_set()
        assert cancellation_seen.wait(1)
        assert json.loads(result)["completion_unknown"] is True

        agent.end_turn(turn_event)
        ended = True
        with pytest.raises(RuntimeError, match="async tool handler.*still shutting down"):
            agent.begin_turn()

        release_handler.set()
        assert agent._turn_workers_drained.wait(1)
        next_event = agent.begin_turn()
        agent.end_turn(next_event)
    finally:
        timer.cancel()
        release_handler.set()
        if not ended:
            agent.end_turn(turn_event)
        reset_interrupt_event(interrupt_token)


def test_async_handler_timeout_keeps_fence_until_task_really_finishes(monkeypatch) -> None:
    handler_started = threading.Event()
    cancellation_seen = threading.Event()
    release_handler = threading.Event()
    agent = _bare_agent("async-timeout-test")
    turn_event = agent.begin_turn()
    local_registry = ToolRegistry()

    async def slow_timeout_handler(_args, **_kwargs):
        handler_started.set()
        try:
            while True:
                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            cancellation_seen.set()
            while not release_handler.is_set():
                await asyncio.sleep(0.01)
            return json.dumps({"success": False, "status": "late_timeout_cleanup"})

    local_registry.register(
        name="slow_timeout_async",
        toolset="test",
        schema={"type": "function", "function": {"name": "slow_timeout_async"}},
        handler=slow_timeout_handler,
        is_async=True,
    )
    monkeypatch.setattr(dispatch, "_ASYNC_HANDLER_TIMEOUT_SECONDS", 0.03)
    ended = False
    try:
        result = local_registry.dispatch(
            "slow_timeout_async",
            {},
            parent_agent=agent,
        )
        assert handler_started.is_set()
        assert cancellation_seen.wait(1)
        parsed = json.loads(result)
        assert parsed["status"] == "timeout"
        assert parsed["completion_unknown"] is True

        agent.end_turn(turn_event)
        ended = True
        with pytest.raises(RuntimeError, match="async tool handler.*still shutting down"):
            agent.begin_turn()

        release_handler.set()
        assert agent._turn_workers_drained.wait(1)
        next_event = agent.begin_turn()
        agent.end_turn(next_event)
    finally:
        release_handler.set()
        if not ended:
            agent.end_turn(turn_event)


def test_async_submission_failure_does_not_resubmit_same_coroutine(monkeypatch) -> None:
    agent = _bare_agent("async-submit-failure")
    submissions = []

    async def handler():
        return "unused"

    coro = handler()

    def fail_submission(submitted, _loop):
        submissions.append(submitted)
        raise RuntimeError("loop rejected submission")

    monkeypatch.setattr(dispatch.asyncio, "run_coroutine_threadsafe", fail_submission)

    with pytest.raises(RuntimeError, match="loop rejected submission"):
        dispatch._run_async(coro, parent_agent=agent)

    assert len(submissions) == 1
    assert agent._outstanding_turn_workers == set()
    assert agent._turn_workers_drained.is_set()


@pytest.mark.parametrize("trigger", ["event", "deadline"])
def test_async_handler_preserves_terminal_result_before_proxy_propagation(
    monkeypatch,
    caplog,
    trigger: str,
) -> None:
    caplog.set_level(logging.INFO)
    cancel_event = threading.Event()
    expected = {"committed": trigger}

    async def handler():
        return expected

    class DelayedProxy:
        def done(self) -> bool:
            return False

        def result(self, timeout=None):
            if trigger == "event":
                cancel_event.set()
            raise dispatch.FutureTimeout()

    def complete_without_propagating(tracked, _loop):
        assert asyncio.run(tracked) is expected
        return DelayedProxy()

    loop = asyncio.new_event_loop()
    monkeypatch.setattr(dispatch, "_get_worker_loop", lambda: loop)
    monkeypatch.setattr(
        dispatch.asyncio,
        "run_coroutine_threadsafe",
        complete_without_propagating,
    )
    token = set_interrupt_event(cancel_event)
    try:
        result = dispatch._run_async(
            handler(),
            timeout_seconds=0.001 if trigger == "deadline" else 1,
            raise_on_stop=True,
        )
    finally:
        reset_interrupt_event(token)
        loop.close()

    assert result is expected
    assert any(
        "async_handler_completion_won_stop" in record.getMessage()
        and f"trigger={trigger}" in record.getMessage()
        for record in caplog.records
    )


def test_dispatcher_terminal_deadline_matches_terminal_timeout_clamping() -> None:
    func = {"arguments": json.dumps({"timeout": 10})}

    assert dispatch._serial_tool_timeout("terminal", func) == 198
    assert dispatch._serial_tool_timeout(
        "terminal",
        func,
        SimpleNamespace(config={"terminal": {"timeout": 20, "max_timeout": 30}}),
    ) == 38


def test_dispatcher_deadlines_cover_real_retry_and_wait_budgets() -> None:
    parent = SimpleNamespace(
        config={
            "terminal": {"timeout": 180},
            "delegation": {"timeout_seconds": 600},
            "auxiliary": {
                "web_search": {
                    "backend": "tavily",
                    "fallback": True,
                    "tavily_timeout": 30,
                    "dashscope_deep_timeout": 120,
                },
                "vision": {"timeout": 30, "download_timeout": 30},
            },
        }
    )

    assert dispatch._concurrent_tool_timeout("web_search", parent) == 160
    assert dispatch._concurrent_tool_timeout("vision_analyze", parent) == 166
    assert dispatch._serial_tool_timeout(
        "process",
        {"arguments": json.dumps({"action": "wait", "timeout": 300})},
        parent,
    ) == 185
    assert dispatch._serial_tool_timeout(
        "delegate_task",
        {"arguments": "{}"},
        parent,
    ) == 630
    parent.config["delegation"]["timeout_seconds"] = 0
    assert dispatch._serial_tool_timeout(
        "delegate_task",
        {"arguments": "{}"},
        parent,
    ) == 630
    parent.config["auxiliary"]["web_search"].update(
        dashscope_timeout=200,
        dashscope_deep_timeout=20,
    )
    assert dispatch._concurrent_tool_timeout("web_search", parent) == 240


def test_serial_cancel_returns_promptly_and_does_not_start_later_tool(
    monkeypatch,
    caplog,
) -> None:
    caplog.set_level(logging.WARNING)
    cancel_event = threading.Event()
    first_started = threading.Event()
    wait_started = threading.Event()
    release_first = threading.Event()
    started = []
    seen_events = []

    class Parent:
        session_id = "serial-cancel-trace"

        def __init__(self) -> None:
            self.abort_reasons = []

        def _request_turn_abort(self, reason, event) -> None:
            self.abort_reasons.append(reason)
            event.set()

    parent = Parent()

    def fake_dispatch(call, *_args):
        name = call["function"]["name"]
        started.append(name)
        seen_events.append(get_interrupt_event())
        if name == "first":
            first_started.set()
            release_first.wait(1)
        return json.dumps({"success": True, "tool": name})

    monkeypatch.setattr(dispatch, "_should_parallelize_tool_batch", lambda _calls: False)
    monkeypatch.setattr(dispatch, "_dispatch_single", fake_dispatch)
    monkeypatch.setattr(dispatch, "_serial_tool_timeout", lambda *_args: 5)
    monkeypatch.setattr(dispatch, "_INTERRUPT_CLEANUP_GRACE", 0.05)
    real_wait_for_worker = dispatch._wait_for_tool_worker

    def observed_wait_for_worker(*args):
        wait_started.set()
        return real_wait_for_worker(*args)

    monkeypatch.setattr(dispatch, "_wait_for_tool_worker", observed_wait_for_worker)

    result_box = []
    caller = threading.Thread(
        target=lambda: result_box.extend(
            dispatch.handle_function_calls(
                [_call("first"), _call("second")],
                {"first", "second"},
                parent_agent=parent,
                cancel_event=cancel_event,
            )
        )
    )
    caller.start()
    try:
        assert first_started.wait(1)
        assert wait_started.wait(1)
        started_at = time.monotonic()
        cancel_event.set()
        caller.join(0.5)
        assert not caller.is_alive()
        assert time.monotonic() - started_at < 0.5
        assert started == ["first"]
        assert seen_events == [cancel_event]
        assert json.loads(result_box[0])["status"] == "cancel_requested"
        assert json.loads(result_box[0])["completion_unknown"] is True
        assert json.loads(result_box[1])["status"] == "cancelled"
        assert parent.abort_reasons[-1] == "tool_completion_unknown"
        event_logs = [
            record.getMessage()
            for record in caplog.records
            if "[CANCEL_TRACE] dispatch_stop" in record.getMessage()
            and "trigger=user_interrupt" in record.getMessage()
        ]
        assert len(event_logs) == 1
        assert get_cancel_id(cancel_event) in event_logs[0]
        assert "session=serial-cancel-trace" in event_logs[0]
        assert "tool=first" in event_logs[0]
        assert "event_was_set=True" in event_logs[0]
    finally:
        release_first.set()
        caller.join(1)


def test_concurrent_timeouts_share_absolute_deadline_and_late_results_are_sealed(
    monkeypatch,
) -> None:
    release_workers = threading.Event()

    def fake_dispatch(call, *_args):
        release_workers.wait(1)
        return json.dumps({"success": True, "tool": call["function"]["name"]})

    monkeypatch.setattr(dispatch, "_should_parallelize_tool_batch", lambda _calls: True)
    monkeypatch.setattr(dispatch, "_dispatch_single", fake_dispatch)
    monkeypatch.setattr(dispatch, "_concurrent_tool_timeout", lambda *_args: 0.15)

    started_at = time.monotonic()
    results = dispatch.handle_function_calls(
        [_call("first"), _call("second")],
        {"first", "second"},
    )
    elapsed = time.monotonic() - started_at
    snapshots = list(results)

    try:
        assert elapsed < 0.25
        assert all("timed out" in json.loads(result)["error"] for result in results)
    finally:
        release_workers.set()

    time.sleep(0.05)
    assert results == snapshots


def test_concurrent_result_finished_after_its_deadline_is_still_timeout(monkeypatch) -> None:
    def fake_dispatch(call, *_args):
        name = call["function"]["name"]
        time.sleep(0.12 if name == "first" else 0.08)
        return json.dumps({"success": True, "tool": name})

    monkeypatch.setattr(dispatch, "_should_parallelize_tool_batch", lambda _calls: True)
    monkeypatch.setattr(dispatch, "_dispatch_single", fake_dispatch)
    monkeypatch.setattr(
        dispatch,
        "_concurrent_tool_timeout",
        lambda name, *_args: 0.3 if name == "first" else 0.03,
    )

    results = dispatch.handle_function_calls(
        [_call("first"), _call("second")],
        {"first", "second"},
    )

    assert json.loads(results[0])["success"] is True
    assert json.loads(results[1])["status"] == "timeout"
    assert json.loads(results[1])["completion_unknown"] is False


def test_concurrent_batch_observes_the_earliest_deadline_first(monkeypatch) -> None:
    cancel_event = threading.Event()
    release_workers = threading.Event()

    def fake_dispatch(_call, *_args):
        release_workers.wait(2)
        return json.dumps({"success": True})

    monkeypatch.setattr(dispatch, "_should_parallelize_tool_batch", lambda _calls: True)
    monkeypatch.setattr(dispatch, "_dispatch_single", fake_dispatch)
    monkeypatch.setattr(
        dispatch,
        "_concurrent_tool_timeout",
        lambda name, *_args: 0.3 if name == "first" else 0.03,
    )
    monkeypatch.setattr(dispatch, "_INTERRUPT_CLEANUP_GRACE", 0.02)

    started_at = time.monotonic()
    results = dispatch.handle_function_calls(
        [_call("first"), _call("second")],
        {"first", "second"},
        cancel_event=cancel_event,
    )
    elapsed = time.monotonic() - started_at
    try:
        assert elapsed < 0.15
        assert cancel_event.is_set() is True
        assert json.loads(results[0])["status"] == "cancel_requested"
        assert json.loads(results[1])["status"] == "timeout"
    finally:
        release_workers.set()


def test_serial_timeout_skips_later_tools_while_completion_is_unknown(monkeypatch) -> None:
    release_first = threading.Event()
    started = []

    def fake_dispatch(call, *_args):
        name = call["function"]["name"]
        started.append(name)
        if name == "first":
            release_first.wait(1)
        return json.dumps({"success": True, "tool": name})

    monkeypatch.setattr(dispatch, "_should_parallelize_tool_batch", lambda _calls: False)
    monkeypatch.setattr(dispatch, "_dispatch_single", fake_dispatch)
    monkeypatch.setattr(dispatch, "_serial_tool_timeout", lambda *_args: 0.05)
    monkeypatch.setattr(dispatch, "_INTERRUPT_CLEANUP_GRACE", 0.02)

    results = dispatch.handle_function_calls(
        [_call("first"), _call("second")],
        {"first", "second"},
    )
    snapshots = list(results)

    try:
        assert started == ["first"]
        assert json.loads(results[0])["status"] == "timeout"
        assert json.loads(results[0])["completion_unknown"] is True
        assert json.loads(results[1]) == {
            "error": "Tool 'second' was skipped because the previous tool may still be running",
            "success": False,
            "status": "skipped",
            "reason": "previous_completion_unknown",
        }
    finally:
        release_first.set()

    time.sleep(0.05)
    assert results == snapshots


def test_tool_timeout_aborts_turn_when_logging_handler_raises(monkeypatch) -> None:
    cancel_event = threading.Event()
    release_worker = threading.Event()

    class RaisingHandler(logging.Handler):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def emit(self, _record) -> None:
            self.calls += 1
            raise RuntimeError("logging failed")

    class Parent:
        config = {}

        def __init__(self) -> None:
            self.workers = set()
            self.abort_reasons = []

        def _request_turn_abort(self, reason, event) -> None:
            self.abort_reasons.append(reason)
            event.set()

        def _register_turn_worker(self, worker) -> None:
            self.workers.add(worker)

        def _unregister_turn_worker(self, worker) -> None:
            self.workers.discard(worker)

    parent = Parent()

    def fake_dispatch(_call, *_args):
        release_worker.wait(2)
        return json.dumps({"success": True})

    monkeypatch.setattr(dispatch, "_should_parallelize_tool_batch", lambda _calls: False)
    monkeypatch.setattr(dispatch, "_dispatch_single", fake_dispatch)
    monkeypatch.setattr(dispatch, "_serial_tool_timeout", lambda *_args: 0.03)
    monkeypatch.setattr(dispatch, "_INTERRUPT_CLEANUP_GRACE", 0.02)

    handler = RaisingHandler()
    old_level = dispatch.logger.level
    dispatch.logger.setLevel(logging.WARNING)
    dispatch.logger.addHandler(handler)
    try:
        results = dispatch.handle_function_calls(
            [_call("first"), _call("second")],
            {"first", "second"},
            parent_agent=parent,
            cancel_event=cancel_event,
        )
        assert cancel_event.is_set() is True
        assert json.loads(results[0])["status"] == "timeout"
        assert json.loads(results[0])["completion_unknown"] is True
        assert json.loads(results[1])["status"] == "cancelled"
        assert len(parent.workers) == 1
        assert next(iter(parent.workers)).is_alive()
        assert parent.abort_reasons == ["tool_timeout", "tool_completion_unknown"]
        assert handler.calls >= 3
    finally:
        release_worker.set()
        dispatch.logger.removeHandler(handler)
        dispatch.logger.setLevel(old_level)

    deadline = time.monotonic() + 1
    while parent.workers and time.monotonic() < deadline:
        time.sleep(0.01)
    assert parent.workers == set()


def test_completion_unknown_aborts_turn_and_skips_later_tools(monkeypatch) -> None:
    cancel_event = threading.Event()
    started = []

    def fake_dispatch(call, *_args):
        name = call["function"]["name"]
        started.append(name)
        return json.dumps(
            {
                "success": False,
                "status": "timeout",
                "completion_unknown": True,
            }
        )

    monkeypatch.setattr(dispatch, "_should_parallelize_tool_batch", lambda _calls: False)
    monkeypatch.setattr(dispatch, "_dispatch_single", fake_dispatch)

    results = dispatch.handle_function_calls(
        [_call("first"), _call("second")],
        {"first", "second"},
        cancel_event=cancel_event,
    )

    assert started == ["first"]
    assert cancel_event.is_set() is True
    assert json.loads(results[0])["completion_unknown"] is True
    assert json.loads(results[1])["status"] == "skipped"
    assert json.loads(results[1])["reason"] == "previous_completion_unknown"


def test_cooperative_worker_result_wins_during_cancel_cleanup(monkeypatch) -> None:
    cancel_event = threading.Event()
    worker_started = threading.Event()

    def fake_dispatch(_call, *_args):
        worker_started.set()
        assert cancel_event.wait(1)
        return json.dumps({"success": False, "status": "stopped_by_tool"})

    monkeypatch.setattr(dispatch, "_should_parallelize_tool_batch", lambda _calls: False)
    monkeypatch.setattr(dispatch, "_dispatch_single", fake_dispatch)
    monkeypatch.setattr(dispatch, "_serial_tool_timeout", lambda *_args: 5)

    result_box = []
    caller = threading.Thread(
        target=lambda: result_box.extend(
            dispatch.handle_function_calls(
                [_call("first"), _call("second")],
                {"first", "second"},
                cancel_event=cancel_event,
            )
        )
    )
    caller.start()
    assert worker_started.wait(1)
    cancel_event.set()
    caller.join(0.5)

    assert not caller.is_alive()
    assert json.loads(result_box[0])["status"] == "stopped_by_tool"
    assert json.loads(result_box[1])["status"] == "cancelled"


def test_serial_cleanup_cannot_accept_a_result_finished_after_deadline(monkeypatch) -> None:
    cancel_event = threading.Event()
    worker_started = threading.Event()
    release_worker = threading.Event()

    def fake_dispatch(_call, *_args):
        worker_started.set()
        release_worker.wait(1)
        return json.dumps({"success": True})

    monkeypatch.setattr(dispatch, "_should_parallelize_tool_batch", lambda _calls: False)
    monkeypatch.setattr(dispatch, "_dispatch_single", fake_dispatch)
    monkeypatch.setattr(dispatch, "_serial_tool_timeout", lambda *_args: 0.05)
    monkeypatch.setattr(dispatch, "_INTERRUPT_CLEANUP_GRACE", 0.2)
    result_box = []
    caller = threading.Thread(
        target=lambda: result_box.extend(
            dispatch.handle_function_calls(
                [_call("first")],
                {"first"},
                cancel_event=cancel_event,
            )
        )
    )
    caller.start()
    assert worker_started.wait(1)
    time.sleep(0.02)
    cancel_event.set()
    time.sleep(0.05)
    release_worker.set()
    caller.join(1)

    assert not caller.is_alive()
    assert json.loads(result_box[0])["status"] == "timeout"


def test_concurrent_cancel_uses_one_cleanup_deadline_for_the_batch(
    monkeypatch,
    caplog,
) -> None:
    caplog.set_level(logging.WARNING)
    cancel_event = threading.Event()
    all_started = threading.Event()
    wait_started = threading.Event()
    release_workers = threading.Event()
    started = []

    def fake_dispatch(call, *_args):
        started.append(call["function"]["name"])
        if len(started) == 3:
            all_started.set()
        release_workers.wait(1)
        return json.dumps({"success": True})

    monkeypatch.setattr(dispatch, "_should_parallelize_tool_batch", lambda _calls: True)
    monkeypatch.setattr(dispatch, "_dispatch_single", fake_dispatch)
    monkeypatch.setattr(dispatch, "_concurrent_tool_timeout", lambda *_args: 5)
    monkeypatch.setattr(dispatch, "_INTERRUPT_CLEANUP_GRACE", 0.05)
    real_wait_for_worker = dispatch._wait_for_tool_worker

    def observed_wait_for_worker(*args):
        wait_started.set()
        return real_wait_for_worker(*args)

    monkeypatch.setattr(dispatch, "_wait_for_tool_worker", observed_wait_for_worker)

    result_box = []
    parent = SimpleNamespace(session_id="concurrent-cancel-trace")
    caller = threading.Thread(
        target=lambda: result_box.extend(
            dispatch.handle_function_calls(
                [_call("first"), _call("second"), _call("third")],
                {"first", "second", "third"},
                parent_agent=parent,
                cancel_event=cancel_event,
            )
        )
    )
    caller.start()
    try:
        assert all_started.wait(1)
        assert wait_started.wait(1)
        started_at = time.monotonic()
        cancel_event.set()
        caller.join(0.5)
        assert not caller.is_alive()
        assert time.monotonic() - started_at < 0.12
        assert [json.loads(result)["status"] for result in result_box] == [
            "cancel_requested",
            "cancel_requested",
            "cancel_requested",
        ]
        event_logs = [
            record.getMessage()
            for record in caplog.records
            if "[CANCEL_TRACE] dispatch_stop" in record.getMessage()
            and "trigger=user_interrupt" in record.getMessage()
        ]
        assert len(event_logs) == 1
        assert get_cancel_id(cancel_event) in event_logs[0]
        assert "session=concurrent-cancel-trace" in event_logs[0]
        assert "tool=first" in event_logs[0]
        assert "event_was_set=True" in event_logs[0]
    finally:
        release_workers.set()
        caller.join(1)


def test_already_cancelled_batch_starts_no_workers(monkeypatch) -> None:
    cancel_event = threading.Event()
    cancel_event.set()
    started = []

    monkeypatch.setattr(dispatch, "_should_parallelize_tool_batch", lambda _calls: True)
    monkeypatch.setattr(dispatch, "_dispatch_single", lambda call, *_args: started.append(call))

    results = dispatch.handle_function_calls(
        [_call("first"), _call("second")],
        {"first", "second"},
        cancel_event=cancel_event,
    )

    assert started == []
    assert [json.loads(result)["status"] for result in results] == ["cancelled", "cancelled"]


def test_cancel_between_thread_start_and_handler_entry_skips_handler(monkeypatch) -> None:
    cancel_event = threading.Event()
    worker_entered = threading.Event()
    release_worker = threading.Event()
    dispatched = []

    class DelayedContext:
        def run(self, function, *args):
            worker_entered.set()
            release_worker.wait(1)
            return function(*args)

    monkeypatch.setattr(dispatch, "copy_context", DelayedContext)
    monkeypatch.setattr(dispatch, "_should_parallelize_tool_batch", lambda _calls: False)
    monkeypatch.setattr(dispatch, "_serial_tool_timeout", lambda *_args: 5)
    monkeypatch.setattr(dispatch, "_dispatch_single", lambda call, *_args: dispatched.append(call))

    result_box = []
    caller = threading.Thread(
        target=lambda: result_box.extend(
            dispatch.handle_function_calls(
                [_call("first"), _call("second")],
                {"first", "second"},
                cancel_event=cancel_event,
            )
        )
    )
    caller.start()
    assert worker_entered.wait(1)
    cancel_event.set()
    release_worker.set()
    caller.join(0.5)

    assert not caller.is_alive()
    assert dispatched == []
    assert [json.loads(result)["status"] for result in result_box] == [
        "cancelled",
        "cancelled",
    ]


def test_cancel_during_checkpoint_preparation_never_enters_handler(monkeypatch) -> None:
    cancel_event = threading.Event()
    dispatched = []
    monkeypatch.setattr(dispatch, "_file_safety_block_error", lambda *_args: None)
    monkeypatch.setattr(
        dispatch,
        "_maybe_checkpoint_before_tool",
        lambda *_args: cancel_event.set(),
    )
    monkeypatch.setattr(
        dispatch.registry,
        "dispatch",
        lambda *_args, **_kwargs: dispatched.append(True),
    )
    token = set_interrupt_event(cancel_event)
    try:
        result = dispatch._dispatch_single(
            _call("write_file"),
            {"write_file"},
            checkpoint_manager=object(),
        )
    finally:
        reset_interrupt_event(token)

    assert dispatched == []
    assert json.loads(result)["status"] == "cancelled"


def test_operation_id_is_linked_before_a_late_mutation_worker_can_return(
    monkeypatch,
    tmp_path,
) -> None:
    from mclaw.safety import operation_journal

    class Journal:
        def begin(self, **kwargs):
            return {
                "tool_call_id": kwargs["tool_call_id"],
                "operation_id": "operation-1",
            }

        def update_checkpoint(self, *_args, **_kwargs):
            pass

    class Checkpoints:
        last_attempt = {}

        def ensure_checkpoint(self, *_args, **_kwargs):
            pass

    plan = SimpleNamespace(
        mutates=True,
        workspace=str(tmp_path),
        target_paths=[str(tmp_path / "changed.txt")],
        intent=SimpleNamespace(raw_command=""),
        decision=SimpleNamespace(level="normal"),
    )
    parent = SimpleNamespace(
        config={"file_safety": {"enabled": True, "journal_enabled": True}},
        session_id="session",
        _checkpoint_turn_id="turn",
    )
    monkeypatch.setattr(dispatch, "_build_file_safety_plan", lambda *_args: plan)
    monkeypatch.setattr(operation_journal, "default_journal", lambda: Journal())

    operation = dispatch._maybe_checkpoint_before_tool(
        "write_file",
        {"_tool_call_id": "call-1", "path": "changed.txt", "content": "new"},
        Checkpoints(),
        parent,
    )

    assert operation["operation_id"] == "operation-1"
    assert parent._tool_operation_ids == {"call-1": "operation-1"}
