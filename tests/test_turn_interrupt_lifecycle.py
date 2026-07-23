from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections import OrderedDict
from types import MethodType, SimpleNamespace

import pytest

from mclaw.agent.core import MClaw
from mclaw.agent.transports.base import ModelCallResult
from mclaw.channels.base import ChannelMessage, ChannelSource
from mclaw.channels.runner import AgentRunner
from mclaw.cli.runtime.events import RuntimeStatus
from mclaw.cli.runtime.session import RuntimeSessionState
from mclaw.providers.registry import PROVIDER_REGISTRY
from mclaw.providers.runtime import ProviderRuntimeContext
from mclaw.tools.interrupt import (
    get_cancel_id,
    get_interrupt_event,
    reset_interrupt_event,
    set_interrupt,
    set_interrupt_event,
)


def _bare_agent(session_id: str = "test-session") -> MClaw:
    agent = MClaw.__new__(MClaw)
    agent.session_id = session_id
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
    agent._workspace_quarantine_key = f"test:{session_id}:{id(agent)}"
    return agent


def test_begin_and_end_turn_own_one_stable_event() -> None:
    agent = _bare_agent()

    turn_event = agent.begin_turn()

    assert agent.begin_turn() is turn_event
    assert agent._turn_active is True
    different_event = threading.Event()
    with pytest.raises(RuntimeError, match="different cancellation event"):
        agent.begin_turn(different_event)

    agent.end_turn(different_event)
    assert agent._turn_active is True
    assert agent._turn_cancel_event is turn_event

    agent.end_turn(turn_event)
    assert agent._turn_active is False
    assert agent._turn_cancel_event is not turn_event
    assert agent._turn_cancel_event.is_set() is False


def test_interrupt_between_begin_and_run_is_not_lost() -> None:
    agent = _bare_agent()
    observed_events: list[threading.Event] = []

    def fake_run_impl(self, _message, **kwargs):
        observed_events.append(kwargs["cancel_event"])
        return {"interrupted": kwargs["cancel_event"].is_set()}

    agent._run_conversation_impl = MethodType(fake_run_impl, agent)
    turn_event = agent.begin_turn()
    agent.interrupt()

    result = agent.run_conversation("work")

    assert observed_events == [turn_event]
    assert turn_event.is_set() is True
    assert result["interrupted"] is True
    assert agent._turn_active is True
    agent.end_turn(turn_event)


def test_user_interrupt_stays_the_turn_outcome_while_late_worker_remains_fenced() -> None:
    class LateWorker:
        def is_alive(self) -> bool:
            return True

    agent = _bare_agent("user-cancel-late-worker")
    turn_event = agent.begin_turn()
    worker = LateWorker()
    agent._register_turn_worker(worker)

    agent.interrupt()
    agent._request_turn_abort("tool_completion_unknown", turn_event)

    assert agent._turn_abort_reason == "user_cancelled"
    result = {"interrupted": turn_event.is_set(), **agent.current_turn_abort_details()}
    assert result == {"interrupted": True}
    assert "工具仍在收尾" in agent._turn_worker_block_reason()

    state = RuntimeSessionState()
    state.begin_turn()
    state.finish_turn(result)
    assert state.status == RuntimeStatus.INTERRUPTED

    agent._unregister_turn_worker(worker)
    agent.end_turn(turn_event)


def test_idle_interrupt_does_not_poison_the_next_turn() -> None:
    agent = _bare_agent()

    agent.interrupt()
    turn_event = agent.begin_turn()

    assert turn_event.is_set() is False
    assert agent._is_interrupted(turn_event) is False
    agent.end_turn(turn_event)


@pytest.mark.parametrize("trigger", ["interrupt", "tool_abort"])
def test_cancellation_survives_faulty_worker_diagnostics_and_logging(trigger: str) -> None:
    from mclaw.agent import core as core_module

    class FaultyWorker:
        @property
        def diagnostic_name(self):
            raise RuntimeError("diagnostic_name failed")

        @property
        def persistent(self):
            raise RuntimeError("persistent failed")

        @property
        def blocking_reason(self):
            raise RuntimeError("blocking_reason failed")

        def is_alive(self) -> bool:
            raise RuntimeError("is_alive failed")

    class RaisingHandler(logging.Handler):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def emit(self, _record) -> None:
            self.calls += 1
            raise RuntimeError("logging failed")

    agent = _bare_agent("faulty-cancel-trace")
    turn_event = agent.begin_turn()
    worker = FaultyWorker()
    agent._register_turn_worker(worker)
    handler = RaisingHandler()
    old_level = core_module.logger.level
    core_module.logger.setLevel(logging.WARNING)
    core_module.logger.addHandler(handler)
    try:
        if trigger == "interrupt":
            agent.interrupt()
        else:
            agent._request_turn_abort("tool_completion_unknown", turn_event)
    finally:
        core_module.logger.removeHandler(handler)
        core_module.logger.setLevel(old_level)

    assert handler.calls >= 1
    assert turn_event.is_set() is True
    assert agent._interrupted is True
    assert agent._persistent_turn_worker_reason() is None
    assert "工具仍在收尾" in agent._turn_worker_block_reason()

    agent._unregister_turn_worker(worker)
    agent.end_turn(turn_event)
    next_event = agent.begin_turn()
    agent.end_turn(next_event)


def test_blocking_cancel_log_holds_no_state_locks_and_event_is_already_set() -> None:
    from mclaw.agent import core as core_module

    entered = threading.Event()
    release = threading.Event()

    class BlockingHandler(logging.Handler):
        def emit(self, _record) -> None:
            entered.set()
            release.wait(2)

    agent = _bare_agent("blocking-cancel-trace")
    turn_event = agent.begin_turn()
    errors: list[BaseException] = []
    handler = BlockingHandler()
    old_level = core_module.logger.level
    core_module.logger.setLevel(logging.WARNING)
    core_module.logger.addHandler(handler)

    def run_interrupt() -> None:
        try:
            agent.interrupt()
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run_interrupt)
    try:
        thread.start()
        assert entered.wait(1)
        assert turn_event.is_set() is True
        assert agent._interrupt_lock.acquire(timeout=0.2)
        agent._interrupt_lock.release()
        assert core_module._WORKSPACE_ABORT_QUARANTINE_LOCK.acquire(timeout=0.2)
        core_module._WORKSPACE_ABORT_QUARANTINE_LOCK.release()
    finally:
        release.set()
        thread.join(1)
        core_module.logger.removeHandler(handler)
        core_module.logger.setLevel(old_level)

    assert not thread.is_alive()
    assert errors == []
    agent.end_turn(turn_event)
    next_event = agent.begin_turn()
    agent.end_turn(next_event)


def test_late_old_turn_cleanup_cannot_clear_or_end_a_new_turn() -> None:
    agent = _bare_agent()
    old_event = agent.begin_turn()
    agent.interrupt()
    agent.end_turn(old_event)

    new_event = agent.begin_turn()
    agent.end_turn(old_event)

    assert old_event.is_set() is True
    assert new_event is not old_event
    assert new_event.is_set() is False
    assert agent._turn_active is True
    assert agent._turn_cancel_event is new_event
    agent.end_turn(new_event)


def test_next_turn_is_fenced_until_late_worker_exits() -> None:
    agent = _bare_agent()
    release = threading.Event()
    worker_started = threading.Event()

    def _worker() -> None:
        try:
            worker_started.set()
            release.wait(2)
        finally:
            agent._unregister_turn_worker(threading.current_thread())

    old_event = agent.begin_turn()
    worker = threading.Thread(target=_worker)
    agent._register_turn_worker(worker)
    worker.start()
    assert worker_started.wait(1)
    agent.end_turn(old_event)

    with pytest.raises(RuntimeError, match="工具仍在收尾"):
        agent.begin_turn()

    release.set()
    worker.join(1)
    assert not worker.is_alive()
    new_event = agent.begin_turn()
    agent.end_turn(new_event)


def test_child_late_worker_transitively_fences_parent_turn() -> None:
    parent = _bare_agent("parent")
    child = _bare_agent("child")
    child._turn_worker_parent = parent
    release = threading.Event()

    def _worker() -> None:
        try:
            release.wait(2)
        finally:
            child._unregister_turn_worker(threading.current_thread())

    parent_event = parent.begin_turn()
    worker = threading.Thread(target=_worker)
    child._register_turn_worker(worker)
    worker.start()
    parent.end_turn(parent_event)

    with pytest.raises(RuntimeError, match="工具仍在收尾"):
        parent.begin_turn()

    release.set()
    worker.join(1)
    next_event = parent.begin_turn()
    parent.end_turn(next_event)


def test_child_tool_abort_reason_propagates_to_parent_turn() -> None:
    parent = _bare_agent("parent")
    child = _bare_agent("child")
    child._turn_worker_parent = parent
    parent_event = parent.begin_turn()
    child_event = child.begin_turn(parent_event)

    child._request_turn_abort("tool_completion_unknown", child_event)

    assert parent_event.is_set() is True
    assert child._turn_abort_reason == "tool_completion_unknown"
    assert parent._turn_abort_reason == "tool_completion_unknown"
    child.end_turn(child_event)
    parent.end_turn(parent_event)


def test_unconfirmed_external_process_permanently_taints_agent() -> None:
    from mclaw.runtime.base import UnresolvedOperationFence

    agent = _bare_agent()
    old_event = agent.begin_turn()
    agent._register_turn_worker(UnresolvedOperationFence())
    agent.end_turn(old_event)

    with pytest.raises(RuntimeError, match="could not be confirmed stopped"):
        agent.begin_turn()
    assert agent._turn_workers_drained.is_set() is False


def test_cancelled_turn_fence_survives_agent_replacement() -> None:
    old_agent = _bare_agent("old")
    replacement = _bare_agent("replacement")
    shared_workspace = f"test:replacement:{id(old_agent)}"
    old_agent._workspace_quarantine_key = shared_workspace
    replacement._workspace_quarantine_key = shared_workspace
    release = threading.Event()

    def _worker() -> None:
        try:
            release.wait(2)
        finally:
            old_agent._unregister_turn_worker(threading.current_thread())

    old_event = old_agent.begin_turn()
    worker = threading.Thread(target=_worker)
    old_agent._register_turn_worker(worker)
    worker.start()
    old_agent.interrupt()
    old_agent.end_turn(old_event)

    with pytest.raises(RuntimeError, match="工具仍在收尾"):
        replacement.begin_turn()

    release.set()
    worker.join(1)
    replacement_event = replacement.begin_turn()
    replacement.end_turn(replacement_event)


def test_active_cancelled_turn_fences_replacement_until_end() -> None:
    old_agent = _bare_agent("old-active")
    replacement = _bare_agent("replacement-active")
    shared_workspace = f"test:active-replacement:{id(old_agent)}"
    old_agent._workspace_quarantine_key = shared_workspace
    replacement._workspace_quarantine_key = shared_workspace

    old_event = old_agent.begin_turn()
    old_agent.interrupt()

    with pytest.raises(RuntimeError, match="cancelled turn.*still shutting down"):
        replacement.begin_turn()

    old_agent.end_turn(old_event)
    replacement_event = replacement.begin_turn()
    replacement.end_turn(replacement_event)


def test_resolved_abort_fence_does_not_capture_a_later_healthy_turn() -> None:
    reused_agent = _bare_agent("reused")
    peer_agent = _bare_agent("peer")
    shared_workspace = f"test:reused-generation:{id(reused_agent)}"
    reused_agent._workspace_quarantine_key = shared_workspace
    peer_agent._workspace_quarantine_key = shared_workspace

    cancelled_event = reused_agent.begin_turn()
    reused_agent.interrupt()
    reused_agent.end_turn(cancelled_event)

    healthy_event = reused_agent.begin_turn()
    peer_event = peer_agent.begin_turn()

    assert healthy_event.is_set() is False
    assert peer_event.is_set() is False
    reused_agent.end_turn(healthy_event)
    peer_agent.end_turn(peer_event)


@pytest.mark.parametrize("drain_before_end", [True, False])
def test_drained_cancelled_agent_is_removed_from_workspace_quarantine(
    drain_before_end: bool,
) -> None:
    import mclaw.agent.core as core_module

    class Worker:
        def is_alive(self):
            return True

    agent = _bare_agent(f"drain-order-{drain_before_end}")
    key = agent._workspace_quarantine_key
    worker = Worker()
    event = agent.begin_turn()
    agent._register_turn_worker(worker)
    agent.interrupt()
    try:
        if drain_before_end:
            agent._unregister_turn_worker(worker)
            assert agent in core_module._WORKSPACE_ABORT_QUARANTINE[key]
            agent.end_turn(event)
        else:
            agent.end_turn(event)
            assert agent in core_module._WORKSPACE_ABORT_QUARANTINE[key]
            agent._unregister_turn_worker(worker)

        assert key not in core_module._WORKSPACE_ABORT_QUARANTINE
    finally:
        with core_module._WORKSPACE_ABORT_QUARANTINE_LOCK:
            core_module._WORKSPACE_ABORT_QUARANTINE.pop(key, None)


def test_workspace_quarantine_prune_is_generation_safe(
    monkeypatch,
    caplog,
) -> None:
    import mclaw.agent.core as core_module

    agent = _bare_agent("generation-safe-prune")
    key = agent._workspace_quarantine_key
    old_event = threading.Event()
    new_event = threading.Event()
    old_cancel_id = get_cancel_id(old_event)
    new_cancel_id = get_cancel_id(new_event)
    agent._workspace_abort_event = old_event
    with core_module._WORKSPACE_ABORT_QUARANTINE_LOCK:
        core_module._WORKSPACE_ABORT_QUARANTINE[key] = [agent]

    original_status = core_module._workspace_abort_fence_status

    def publish_new_generation_during_scan(current_agent):
        result = original_status(current_agent)
        with core_module._WORKSPACE_ABORT_QUARANTINE_LOCK:
            with current_agent._interrupt_lock:
                current_agent._turn_cancel_event = new_event
                current_agent._workspace_abort_event = new_event
                current_agent._turn_active = True
        return result

    monkeypatch.setattr(
        core_module,
        "_workspace_abort_fence_status",
        publish_new_generation_during_scan,
    )
    caplog.set_level(logging.INFO)
    try:
        reason, resolved, live_agents = core_module._workspace_abort_block_reason(key)
        core_module._trace_workspace_abort_scan(key, reason, resolved, live_agents)

        assert core_module._WORKSPACE_ABORT_QUARANTINE[key] == [agent]
        assert resolved == [(agent, old_event)]
        prune_messages = [
            record.getMessage()
            for record in caplog.records
            if "quarantine_prune" in record.getMessage()
        ]
        assert any(old_cancel_id in message for message in prune_messages)
        assert all(new_cancel_id not in message for message in prune_messages)
    finally:
        agent._turn_active = False
        with core_module._WORKSPACE_ABORT_QUARANTINE_LOCK:
            core_module._WORKSPACE_ABORT_QUARANTINE.pop(key, None)


def test_permanent_abort_fence_survives_agent_replacement() -> None:
    from mclaw.runtime.base import UnresolvedOperationFence

    old_agent = _bare_agent("old-permanent")
    replacement = _bare_agent("replacement-permanent")
    shared_workspace = f"test:permanent-replacement:{id(old_agent)}"
    old_agent._workspace_quarantine_key = shared_workspace
    replacement._workspace_quarantine_key = shared_workspace

    old_event = old_agent.begin_turn()
    old_agent._register_turn_worker(UnresolvedOperationFence())
    old_agent._request_turn_abort("tool_completion_unknown", old_event)
    old_agent.end_turn(old_event)

    with pytest.raises(RuntimeError, match="could not be confirmed stopped"):
        replacement.begin_turn()


def test_workspace_quarantine_concurrent_reentry_does_not_deadlock(monkeypatch) -> None:
    """Two resolved quarantine entries must not acquire each other's locks."""
    import mclaw.agent.core as core_module

    first = _bare_agent("quarantine-first")
    second = _bare_agent("quarantine-second")
    old_keys = [first._workspace_quarantine_key, second._workspace_quarantine_key]
    for agent in (first, second):
        event = agent.begin_turn()
        agent.interrupt()
        agent.end_turn(event)

    shared_workspace = f"test:concurrent-quarantine:{id(first)}"
    first._workspace_quarantine_key = shared_workspace
    second._workspace_quarantine_key = shared_workspace
    with core_module._WORKSPACE_ABORT_QUARANTINE_LOCK:
        for key in old_keys:
            core_module._WORKSPACE_ABORT_QUARANTINE.pop(key, None)
        core_module._WORKSPACE_ABORT_QUARANTINE[shared_workspace] = [first, second]

    original_status = core_module._workspace_abort_fence_status
    simultaneous_status = threading.Barrier(2)

    def synchronized_status(agent):
        # Before the registry lock became the common linearization point, both
        # callers reached this barrier while holding their own interrupt lock
        # and then deadlocked while inspecting the other agent.  With the fixed
        # lock order, the first caller times out here and completes before the
        # second caller can enter.
        try:
            simultaneous_status.wait(timeout=0.15)
        except threading.BrokenBarrierError:
            pass
        return original_status(agent)

    monkeypatch.setattr(
        core_module,
        "_workspace_abort_fence_status",
        synchronized_status,
    )
    errors: list[BaseException] = []

    def begin_and_end(agent: MClaw) -> None:
        try:
            event = agent.begin_turn()
            agent.end_turn(event)
        except BaseException as exc:  # retain thread failures for the assertion
            errors.append(exc)

    threads = [
        threading.Thread(target=begin_and_end, args=(first,), daemon=True),
        threading.Thread(target=begin_and_end, args=(second,), daemon=True),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(1)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []


def test_agents_have_isolated_turn_events() -> None:
    first = _bare_agent("first")
    second = _bare_agent("second")
    first_event = first.begin_turn()
    second_event = second.begin_turn()

    first.interrupt()

    assert first_event.is_set() is True
    assert second_event.is_set() is False
    assert first._is_interrupted(first_event) is True
    assert second._is_interrupted(second_event) is False

    first.end_turn(first_event)
    replacement = first.begin_turn()
    assert replacement is not first_event
    assert replacement.is_set() is False
    assert second._turn_cancel_event is second_event
    assert second_event.is_set() is False

    first.end_turn(replacement)
    second.end_turn(second_event)


def test_legacy_reset_cannot_clear_a_bound_turn_event() -> None:
    event = threading.Event()
    set_interrupt_event(event)
    set_interrupt(True)
    set_interrupt(False)

    assert event.is_set() is True
    set_interrupt_event(None)


def test_core_passes_the_exact_turn_event_into_tool_dispatch(monkeypatch) -> None:
    from mclaw.tools import dispatch

    agent = _bare_agent()
    turn_event = agent.begin_turn()
    captured = {}

    def fake_handle(**kwargs):
        captured.update(kwargs)
        assert get_interrupt_event() is turn_event
        return [json.dumps({"success": True})]

    monkeypatch.setattr(dispatch, "handle_function_calls", fake_handle)
    agent._session_db = None
    agent._delegate_depth = 0
    agent._tool_callback = None
    agent._tool_end_callback = None
    agent._memory_manager = None
    agent._memory_changed_in_turn = False
    agent._skills_changed_in_turn = False
    agent.valid_tool_names = {"test_tool"}
    agent.config = {}
    agent._emit_status = lambda _message: None
    agent._get_checkpoint_manager = lambda: SimpleCheckpointManager()
    messages = []
    call = {
        "id": "tool-call",
        "type": "function",
        "function": {"name": "test_tool", "arguments": "{}"},
    }

    outer_event = threading.Event()
    outer_token = set_interrupt_event(outer_event)
    try:
        agent._execute_tool_calls([call], messages)
        assert get_interrupt_event() is outer_event
    finally:
        reset_interrupt_event(outer_token)

    assert captured["cancel_event"] is turn_event
    assert messages[-1]["tool_call_id"] == "tool-call"
    agent.end_turn(turn_event)


def test_cancel_before_dispatch_keeps_assistant_tool_history_pair() -> None:
    agent = _bare_agent()
    turn_event = agent.begin_turn()
    agent._delegate_depth = 0
    persisted = []

    class RecordingDB:
        def append_message(self, *args, **kwargs):
            persisted.append((args, kwargs))

    agent._session_db = RecordingDB()
    agent._tool_callback = None
    agent._tool_end_callback = None
    agent._memory_manager = None
    agent._memory_changed_in_turn = False
    agent._skills_changed_in_turn = False
    agent._memory_review_round = 0
    agent._evolution_review_round = 0
    agent.valid_tool_names = {"terminal"}
    agent.config = {}
    agent._checkpoint_turn_id = "cancelled-turn"
    agent._emit_status = lambda _message: None
    agent._get_checkpoint_manager = lambda: SimpleCheckpointManager()
    agent.interrupt()
    call = {
        "id": "cancelled-tool",
        "type": "function",
        "function": {"name": "terminal", "arguments": "{}"},
    }
    messages = []

    agent._execute_tool_calls(
        [call],
        messages,
        assistant_content="before cancellation",
        cancel_event=turn_event,
    )

    assert [message["role"] for message in messages] == ["assistant", "tool"]
    assert messages[0]["tool_calls"] == [call]
    assert messages[1]["tool_call_id"] == "cancelled-tool"
    assert json.loads(messages[1]["content"])["status"] == "cancelled"
    assert len(persisted) == 1
    assert persisted[0][0][1] == "tool"
    assert persisted[0][1]["tool_call_id"] == "cancelled-tool"
    agent.end_turn(turn_event)


def test_cancel_during_cleanup_only_batch_cannot_report_completed(monkeypatch) -> None:
    profile = PROVIDER_REGISTRY["openai"]
    context = ProviderRuntimeContext(
        profile=profile,
        model=profile.normalize_model("gpt-5.6"),
        api_key="test-secret",
        base_url=profile.base_url,
    )
    cleanup_call = {
        "id": "cleanup-1",
        "type": "function",
        "function": {
            "name": "terminal",
            "arguments": json.dumps({"command": "Remove-Item temp.tmp"}),
        },
    }

    class CleanupTransport:
        def call(self, **_kwargs):
            return ModelCallResult(
                content="work is complete",
                tool_calls=[cleanup_call],
                finish_reason="tool_calls",
                reasoning=None,
                usage=None,
                was_streamed=False,
                provider=context.provider,
                model=context.model,
            )

    monkeypatch.setattr("mclaw.agent.core.create_transport", lambda _context: CleanupTransport())
    monkeypatch.setattr(MClaw, "_discover_tools", lambda self: None)
    agent = MClaw(
        provider_runtime=context,
        system_prompt="system",
        skip_memory=True,
        config={
            "compression": {"enabled": False},
            "checkpoints": {"enabled": False},
        },
    )
    seen_events = []

    def fake_execute(self, _calls, _messages, **kwargs):
        seen_events.append(kwargs["cancel_event"])
        self._request_turn_abort("tool_timeout", kwargs["cancel_event"])
        return None

    agent._execute_tool_calls = MethodType(fake_execute, agent)

    result = agent.run_conversation("work", advance_background_review=False)

    assert len(seen_events) == 1
    assert seen_events[0].is_set() is True
    assert result["interrupted"] is True
    assert result["completed"] is False
    assert result["abort_reason"] == "tool_timeout"
    assert result["stop_reason"] == "tool_timeout"
    assert "deadline" in result["abort_message"]


class SimpleCheckpointManager:
    def new_turn(self) -> None:
        pass


def test_channel_runner_begins_turn_before_exposing_active_agent() -> None:
    sequence: list[str] = []

    class FakeAgent:
        def __init__(self) -> None:
            self.event = threading.Event()

        def begin_turn(self):
            sequence.append("begin")
            return self.event

        def interrupt(self):
            sequence.append("interrupt")
            self.event.set()

        def run_conversation(self, **_kwargs):
            sequence.append("run")
            return {"final_response": "", "interrupted": self.event.is_set()}

        def end_turn(self, event):
            assert event is self.event
            sequence.append("end")

    agent = FakeAgent()
    runner = AgentRunner.__new__(AgentRunner)
    runner._agents = OrderedDict()
    runner._pending = {}
    runner._cancelled_sessions = set()
    runner._get_or_create_agent = lambda *, session_id: agent

    class ObservedActiveAgents(dict):
        def __setitem__(self, session_id, value):
            assert value is agent
            assert sequence == ["begin"]
            sequence.append("expose")
            super().__setitem__(session_id, value)
            assert runner.interrupt(session_id) is True

    runner._active_agents = ObservedActiveAgents()
    message = ChannelMessage(
        text="work",
        source=ChannelSource(channel="test", chat_id="chat"),
    )

    result = asyncio.run(
        runner._run_single_turn(
            message=message,
            session_id="channel-session",
            conversation_history=None,
        )
    )

    assert result.interrupted is True
    assert sequence == ["begin", "expose", "interrupt", "run", "end"]
    assert "channel-session" not in runner._active_agents


def test_channel_reset_refuses_session_replacement_while_cancel_drains() -> None:
    class FakeAgent:
        def __init__(self) -> None:
            self.interrupted = False

        def interrupt(self) -> None:
            self.interrupted = True

        def _replacement_block_reason(self) -> str:
            return "previous external operation is still shutting down"

    agent = FakeAgent()
    runner = AgentRunner.__new__(AgentRunner)
    runner._pending = {}
    runner._active_agents = {"old": agent}
    runner._closing_agents = {}
    runner._closing_sessions = {}
    runner._cancelled_sessions = set()
    runner._agents = OrderedDict()

    assert runner.reset_session("old") is True
    assert agent.interrupted is True
    assert (
        runner.session_replacement_block_reason("old")
        == "previous external operation is still shutting down"
    )


def test_dingtalk_new_does_not_replace_a_fenced_session() -> None:
    from mclaw.channels.dingtalk.adapter import DingTalkAdapter

    reset_sessions: list[str] = []
    new_sessions: list[str] = []
    sent: list[str] = []
    adapter = DingTalkAdapter.__new__(DingTalkAdapter)
    adapter.config = SimpleNamespace(client_id="client")
    adapter.dedup = SimpleNamespace(
        is_duplicate=lambda _key: False,
        content_key=lambda *_args: "content-key",
    )
    adapter._message_contexts = {}
    adapter._done_reaction_fired = set()
    adapter._is_user_allowed = lambda *_args, **_kwargs: True
    adapter._should_process_message = lambda **_kwargs: True
    adapter._remember_session_webhook = lambda *_args: None
    adapter._fire_thinking_reaction = lambda *_args: None
    adapter._status_context_text = lambda **_kwargs: ""

    async def no_schedule_bind(**_kwargs):
        return None

    async def send(_chat_id, content, **_kwargs):
        sent.append(content)

    adapter._maybe_handle_schedule_bind = no_schedule_bind
    adapter.send = send
    adapter.command_router = SimpleNamespace(
        handle=lambda *_args, **_kwargs: SimpleNamespace(
            handled=True,
            action="new",
            text="Started a fresh DingTalk session.",
        )
    )
    adapter.runner = SimpleNamespace(
        startup_provider_runtime=SimpleNamespace(model="test-model"),
        get_status=lambda _session_id: "running",
        reset_session=lambda session_id: reset_sessions.append(session_id) or True,
        session_replacement_block_reason=lambda _session_id: (
            "Previous operation is still shutting down"
        ),
    )
    adapter.session_router = SimpleNamespace(
        route=lambda *_args, **_kwargs: SimpleNamespace(session_id="old-session"),
        new_session=lambda *_args, **_kwargs: new_sessions.append("created"),
    )
    message = SimpleNamespace(
        message_id="message-1",
        conversation_id="chat-1",
        conversation_type="1",
        message_type="text",
        sender_id="user-1",
        sender_nick="User",
        sender_staff_id="staff-1",
        text="/new",
    )

    result = asyncio.run(adapter.process_message(message))

    assert reset_sessions == ["old-session"]
    assert new_sessions == []
    assert result is not None
    assert result.session_id == "old-session"
    assert "not created yet" in result.final_response
    assert sent == [result.final_response]


def test_weixin_new_does_not_replace_a_fenced_session() -> None:
    from mclaw.channels.weixin.adapter import WeixinAdapter

    reset_sessions: list[str] = []
    new_sessions: list[str] = []
    sent: list[str] = []
    adapter = WeixinAdapter.__new__(WeixinAdapter)
    adapter.config = SimpleNamespace(account_id="account")
    adapter.dedup = SimpleNamespace(
        is_duplicate=lambda _key: False,
        content_key=lambda *_args: "content-key",
    )
    adapter.token_store = SimpleNamespace(set=lambda *_args: None)
    adapter.is_dm_allowed = lambda _sender_id: True

    async def no_typing_ticket(*_args, **_kwargs):
        return None

    async def no_schedule_bind(**_kwargs):
        return None

    async def send(_chat_id, content):
        sent.append(content)

    adapter._maybe_fetch_typing_ticket = no_typing_ticket
    adapter._maybe_handle_schedule_bind = no_schedule_bind
    adapter.send = send
    adapter.command_router = SimpleNamespace(
        handle=lambda *_args, **_kwargs: SimpleNamespace(
            handled=True,
            action="new",
            text="Started a fresh Weixin session.",
        )
    )
    adapter.runner = SimpleNamespace(
        startup_provider_runtime=SimpleNamespace(model="test-model"),
        get_status=lambda _session_id: "running",
        reset_session=lambda session_id: reset_sessions.append(session_id) or True,
        session_replacement_block_reason=lambda _session_id: (
            "Previous operation is still shutting down"
        ),
    )
    adapter.session_router = SimpleNamespace(
        route=lambda *_args, **_kwargs: SimpleNamespace(session_id="old-session"),
        new_session=lambda *_args, **_kwargs: new_sessions.append("created"),
    )
    message = {
        "from_user_id": "user-1",
        "message_id": "message-1",
        "item_list": [{"type": 1, "text_item": {"text": "/new"}}],
    }

    result = asyncio.run(adapter.process_message(message))

    assert reset_sessions == ["old-session"]
    assert new_sessions == []
    assert result is not None
    assert result.session_id == "old-session"
    assert "not created yet" in result.final_response
    assert sent == [result.final_response]


def test_channel_task_cancellation_discards_agent_until_worker_unwinds(monkeypatch) -> None:
    import mclaw.channels.runner as runner_module

    monkeypatch.setattr(runner_module, "_CANCEL_UNWIND_GRACE_SECONDS", 0.05)
    started = threading.Event()
    release = threading.Event()
    ended = threading.Event()

    class FakeAgent:
        def __init__(self, *, blocking: bool, response: str) -> None:
            self.event = threading.Event()
            self._event_callback = lambda _event: None
            self.blocking = blocking
            self.response = response
            self.run_started = threading.Event()
            self.ended = threading.Event()

        def begin_turn(self):
            return self.event

        def interrupt(self):
            self.event.set()

        def run_conversation(self, **_kwargs):
            self.run_started.set()
            if self.blocking:
                started.set()
                release.wait(2)
            return {
                "final_response": self.response,
                "interrupted": self.event.is_set(),
            }

        def end_turn(self, event):
            assert event is self.event
            self.ended.set()
            if self.blocking:
                ended.set()

    async def scenario() -> None:
        agent = FakeAgent(blocking=True, response="stale")
        fresh_agent = FakeAgent(blocking=False, response="fresh")
        factory_calls = []
        runner = AgentRunner.__new__(AgentRunner)
        runner._agents = OrderedDict({"channel-session": (agent, 0.0)})
        runner._pending = {}
        runner._closing_sessions = {}
        runner._cancelled_sessions = set()
        runner._active_agents = {}

        def get_agent(*, session_id):
            factory_calls.append(session_id)
            return agent if len(factory_calls) == 1 else fresh_agent

        runner._get_or_create_agent = get_agent
        message = ChannelMessage(
            text="work",
            source=ChannelSource(channel="test", chat_id="chat"),
        )
        task = asyncio.create_task(
            runner._run_single_turn(
                message=message,
                session_id="channel-session",
                conversation_history=None,
            )
        )
        while not started.is_set():
            await asyncio.sleep(0.005)
        runner._pending["channel-session"] = ChannelMessage(
            text="stale queued work",
            source=ChannelSource(channel="test", chat_id="chat"),
        )
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert agent.event.is_set() is True
        assert agent._event_callback is None
        assert "channel-session" not in runner._agents
        assert "channel-session" not in runner._active_agents
        assert "channel-session" not in runner._pending
        assert ended.is_set() is False
        assert "channel-session" in runner._closing_sessions

        next_turn = asyncio.create_task(
            runner._run_single_turn(
                message=message,
                session_id="channel-session",
                conversation_history=None,
            )
        )
        await asyncio.sleep(0.08)
        assert next_turn.done() is False
        assert factory_calls == ["channel-session"]
        assert fresh_agent.run_started.is_set() is False

        release.set()
        deadline = time.monotonic() + 1
        while not ended.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert ended.is_set() is True
        next_result = await asyncio.wait_for(next_turn, timeout=1)
        assert next_result.final_response == "fresh"
        assert fresh_agent.run_started.is_set() is True
        assert fresh_agent.ended.is_set() is True
        assert factory_calls == ["channel-session", "channel-session"]
        assert "channel-session" not in runner._closing_sessions

    asyncio.run(scenario())


def test_channel_interrupt_fences_new_agent_until_late_tool_drains() -> None:
    started = threading.Event()
    release_tool = threading.Event()

    class InterruptedAgent:
        def __init__(self) -> None:
            self.event = threading.Event()
            self._turn_workers_drained = threading.Event()
            self._turn_workers_drained.clear()

        def begin_turn(self):
            return self.event

        def interrupt(self):
            self.event.set()

        def run_conversation(self, **_kwargs):
            started.set()
            self.event.wait(1)
            threading.Thread(
                target=lambda: (release_tool.wait(2), self._turn_workers_drained.set()),
                daemon=True,
            ).start()
            return {"final_response": "", "interrupted": True}

        def end_turn(self, event):
            assert event is self.event

        def _has_outstanding_turn_workers(self):
            return not self._turn_workers_drained.is_set()

    class FreshAgent:
        def __init__(self) -> None:
            self.event = threading.Event()
            self.started = threading.Event()
            self._turn_workers_drained = threading.Event()
            self._turn_workers_drained.set()

        def begin_turn(self):
            return self.event

        def run_conversation(self, **_kwargs):
            self.started.set()
            return {"final_response": "fresh", "interrupted": False}

        def end_turn(self, event):
            assert event is self.event

        def _has_outstanding_turn_workers(self):
            return False

    async def scenario() -> None:
        old_agent = InterruptedAgent()
        fresh_agent = FreshAgent()
        factory_calls = []
        runner = AgentRunner.__new__(AgentRunner)
        runner._agents = OrderedDict()
        runner._pending = {}
        runner._closing_sessions = {}
        runner._cancelled_sessions = set()
        runner._active_agents = {}

        def get_agent(*, session_id):
            factory_calls.append(session_id)
            return old_agent if len(factory_calls) == 1 else fresh_agent

        runner._get_or_create_agent = get_agent
        message = ChannelMessage(
            text="work",
            source=ChannelSource(channel="test", chat_id="chat"),
        )
        first = asyncio.create_task(
            runner._run_single_turn(
                message=message,
                session_id="channel-session",
                conversation_history=None,
            )
        )
        while not started.is_set():
            await asyncio.sleep(0.005)
        assert runner.interrupt("channel-session") is True
        await asyncio.wait_for(first, timeout=1)
        assert "channel-session" in runner._closing_sessions

        second = asyncio.create_task(
            runner._run_single_turn(
                message=message,
                session_id="channel-session",
                conversation_history=None,
            )
        )
        await asyncio.sleep(0.08)
        assert second.done() is False
        assert fresh_agent.started.is_set() is False
        assert factory_calls == ["channel-session"]

        release_tool.set()
        second_result = await asyncio.wait_for(second, timeout=1)
        assert second_result.final_response == "fresh"
        assert fresh_agent.started.is_set() is True

    asyncio.run(scenario())


def test_channel_reports_persistent_external_fence_instead_of_waiting_forever() -> None:
    async def scenario() -> None:
        runner = AgentRunner.__new__(AgentRunner)
        runner._agents = OrderedDict()
        runner._pending = {}
        runner._cancelled_sessions = set()
        runner._active_agents = {}
        runner._closing_sessions = {"channel-session": asyncio.Event()}
        runner._closing_agents = {
            "channel-session": SimpleNamespace(
                _persistent_turn_worker_reason=lambda: "restart required"
            )
        }
        runner._get_or_create_agent = lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("must not start a fresh agent")
        )
        message = ChannelMessage(
            text="work",
            source=ChannelSource(channel="test", chat_id="chat"),
        )

        result = await runner._run_single_turn(
            message=message,
            session_id="channel-session",
            conversation_history=None,
        )

        assert result.error == "restart required"

    asyncio.run(scenario())


def test_channel_cancel_and_cleanup_survive_raising_logging_handler() -> None:
    import mclaw.channels.runner as runner_module

    class RaisingHandler(logging.Handler):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def emit(self, _record) -> None:
            self.calls += 1
            raise RuntimeError("logging failed")

    class ActiveAgent:
        def __init__(self) -> None:
            self.event = threading.Event()
            self.interrupt_calls = 0

        def current_turn_cancel_event(self):
            return self.event

        def interrupt(self) -> None:
            self.interrupt_calls += 1
            self.event.set()

    handler = RaisingHandler()
    old_level = runner_module.logger.level
    runner_module.logger.setLevel(logging.INFO)
    runner_module.logger.addHandler(handler)
    try:
        active_agent = ActiveAgent()
        runner = AgentRunner.__new__(AgentRunner)
        runner._pending = {}
        runner._active_agents = {"channel-session": active_agent}
        runner._cancelled_sessions = set()

        assert runner.interrupt("channel-session") is True
        assert active_agent.interrupt_calls == 1
        assert active_agent.event.is_set()

        async def scenario() -> None:
            cleanup_runner = AgentRunner.__new__(AgentRunner)
            cleanup_runner._closing_sessions = {}
            cleanup_runner._closing_agents = {}
            worker_finished = threading.Event()
            worker_finished.set()
            workers_drained = threading.Event()
            workers_drained.set()
            fence_agent = SimpleNamespace(
                _workspace_abort_event=active_agent.event,
                _turn_workers_drained=workers_drained,
                _turn_worker_log_snapshot=lambda: "[]",
            )

            cleanup_runner._start_closing_session_fence(
                session_id="channel-session",
                agent=fence_agent,
                worker_finished=worker_finished,
                end_turn=None,
            )
            deadline = time.monotonic() + 1
            while (
                "channel-session" in cleanup_runner._closing_sessions
                and time.monotonic() < deadline
            ):
                await asyncio.sleep(0.005)
            assert "channel-session" not in cleanup_runner._closing_sessions
            assert "channel-session" not in cleanup_runner._closing_agents

        asyncio.run(scenario())
        assert handler.calls >= 3
    finally:
        runner_module.logger.removeHandler(handler)
        runner_module.logger.setLevel(old_level)
