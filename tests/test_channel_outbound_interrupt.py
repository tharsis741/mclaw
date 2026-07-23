from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from pathlib import Path

import pytest

from mclaw.agent.core import MClaw
from mclaw.channels import outbound_bridge
from mclaw.channels.base import SendResult
from mclaw.channels.dingtalk.outbound_registry import DingTalkOutboundTarget
from mclaw.channels.outbound_bridge import run_outbound_coroutine
from mclaw.channels.weixin.outbound_registry import WeixinOutboundTarget
from mclaw.tools import dingtalk_tool, dispatch, weixin_tool
from mclaw.tools.interrupt import get_cancel_id


def _bare_agent(session_id: str) -> MClaw:
    agent = MClaw.__new__(MClaw)
    agent.session_id = session_id
    agent.config = {}
    agent._interrupted = False
    agent._interrupt_lock = threading.Lock()
    agent._turn_cancel_event = threading.Event()
    agent._turn_active = False
    agent._turn_abort_reason = None
    agent._workspace_abort_event = None
    agent._turn_workers_lock = threading.Lock()
    agent._outstanding_turn_workers = set()
    agent._turn_workers_drained = threading.Event()
    agent._turn_workers_drained.set()
    agent._turn_worker_parent = None
    agent._workspace_quarantine_key = f"test:outbound:{session_id}:{id(agent)}"
    return agent


class _LoopThread:
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.ready.set()
        self.loop.run_forever()

    def start(self) -> asyncio.AbstractEventLoop:
        self.thread.start()
        assert self.ready.wait(1)
        return self.loop

    def stop(self) -> None:
        if not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(1)
        assert not self.thread.is_alive()
        self.loop.close()


class _ReportedRunningLoop:
    """Minimal owner-loop façade for submission-race unit tests."""

    @staticmethod
    def is_closed() -> bool:
        return False

    @staticmethod
    def is_running() -> bool:
        return True

    @staticmethod
    def call_soon_threadsafe(callback, *args) -> None:
        callback(*args)


def _dispatch_send_file(
    tool_name: str,
    arguments: dict,
    *,
    agent: MClaw,
    cancel_event: threading.Event,
) -> str:
    results = dispatch.handle_function_calls(
        [
            {
                "id": f"{tool_name}-call",
                "type": "function",
                "function": {
                    "name": tool_name,
                    "arguments": json.dumps(arguments),
                },
            }
        ],
        {tool_name},
        parent_agent=agent,
        cancel_event=cancel_event,
    )
    assert len(results) == 1
    return results[0]


def _assert_outbound_cancel_lifecycle(
    *,
    platform: str,
    invoke,
    agent: MClaw,
    turn_event: threading.Event,
    operation_started: threading.Event,
    cancellation_seen: threading.Event,
    release_operation: threading.Event,
    loop_thread: _LoopThread,
    caplog,
) -> None:
    # Keep one-time registry/module discovery outside the cancellation latency
    # window; this test measures the bridge and dispatcher shutdown path only.
    dispatch._discover_tools()
    result_box: list[str] = []
    caller = threading.Thread(target=lambda: result_box.append(invoke()), daemon=True)
    caller.start()
    try:
        assert operation_started.wait(1)
        with agent._turn_workers_lock:
            fences = [
                worker
                for worker in agent._outstanding_turn_workers
                if getattr(worker, "diagnostic_name", "").startswith(f"{platform}_outbound")
            ]
        assert len(fences) == 1
        assert fences[0].cancel_event is turn_event

        started_at = time.monotonic()
        agent.interrupt()
        caller.join(0.5)
        assert not caller.is_alive()
        assert time.monotonic() - started_at < 0.5
        assert cancellation_seen.wait(1)

        result = json.loads(result_box[0])
        assert result["success"] is False
        assert result["interrupted"] is True
        assert result["completion_unknown"] is True
        assert result["status"] == "cancel_requested"

        agent.end_turn(turn_event)
        with pytest.raises(RuntimeError, match="file send is still shutting down"):
            agent.begin_turn()

        release_operation.set()
        assert agent._turn_workers_drained.wait(1)
        next_event = agent.begin_turn()
        agent.end_turn(next_event)

        cancel_id = get_cancel_id(turn_event)
        release_deadline = time.monotonic() + 1
        while time.monotonic() < release_deadline:
            if any(
                "outbound_fence_release" in record.getMessage()
                for record in caplog.records
            ):
                break
            time.sleep(0.005)
        messages = [record.getMessage() for record in caplog.records]
        for event_name in (
            "outbound_fence_register",
            "outbound_cancel_request",
            "outbound_fence_release",
        ):
            assert any(
                event_name in message
                and cancel_id in message
                and f"platform={platform}" in message
                for message in messages
            )
    finally:
        release_operation.set()
        caller.join(1)
        deadline = time.monotonic() + 1
        while not agent._turn_workers_drained.is_set() and time.monotonic() < deadline:
            time.sleep(0.01)
        loop_thread.stop()


def test_weixin_send_file_cancel_returns_fast_and_fences_real_coroutine(
    monkeypatch,
    tmp_path: Path,
    caplog,
) -> None:
    caplog.set_level(logging.INFO)
    operation_started = threading.Event()
    cancellation_seen = threading.Event()
    release_operation = threading.Event()

    class Adapter:
        async def send_file(self, _chat_id, **_kwargs) -> SendResult:
            operation_started.set()
            try:
                while True:
                    await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                cancellation_seen.set()
                while not release_operation.is_set():
                    await asyncio.sleep(0.01)
                return SendResult(success=False, error="cancel cleanup complete")

    loop_thread = _LoopThread()
    loop = loop_thread.start()
    target = WeixinOutboundTarget(adapter=Adapter(), chat_id="wx-chat", loop=loop)
    monkeypatch.setattr(weixin_tool, "get_weixin_outbound_target", lambda _session: target)
    path = tmp_path / "weixin.txt"
    path.write_text("payload", encoding="utf-8")
    agent = _bare_agent("weixin-outbound-cancel")
    turn_event = agent.begin_turn()

    _assert_outbound_cancel_lifecycle(
        platform="weixin",
        invoke=lambda: _dispatch_send_file(
            "weixin_send_file",
            {"file_path": str(path)},
            agent=agent,
            cancel_event=turn_event,
        ),
        agent=agent,
        turn_event=turn_event,
        operation_started=operation_started,
        cancellation_seen=cancellation_seen,
        release_operation=release_operation,
        loop_thread=loop_thread,
        caplog=caplog,
    )


def test_dingtalk_send_file_cancel_returns_fast_and_fences_real_coroutine(
    monkeypatch,
    tmp_path: Path,
    caplog,
) -> None:
    caplog.set_level(logging.INFO)
    operation_started = threading.Event()
    cancellation_seen = threading.Event()
    release_operation = threading.Event()

    class Adapter:
        async def send_file(self, _chat_id, **_kwargs) -> SendResult:
            operation_started.set()
            try:
                while True:
                    await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                cancellation_seen.set()
                while not release_operation.is_set():
                    await asyncio.sleep(0.01)
                return SendResult(success=False, error="cancel cleanup complete")

    loop_thread = _LoopThread()
    loop = loop_thread.start()
    target = DingTalkOutboundTarget(adapter=Adapter(), chat_id="dt-chat", loop=loop)
    monkeypatch.setattr(dingtalk_tool, "get_dingtalk_outbound_target", lambda _session: target)
    path = tmp_path / "dingtalk.txt"
    path.write_text("payload", encoding="utf-8")
    agent = _bare_agent("dingtalk-outbound-cancel")
    turn_event = agent.begin_turn()

    _assert_outbound_cancel_lifecycle(
        platform="dingtalk",
        invoke=lambda: _dispatch_send_file(
            "dingtalk_send_file",
            {"file_path": str(path)},
            agent=agent,
            cancel_event=turn_event,
        ),
        agent=agent,
        turn_event=turn_event,
        operation_started=operation_started,
        cancellation_seen=cancellation_seen,
        release_operation=release_operation,
        loop_thread=loop_thread,
        caplog=caplog,
    )


def test_outbound_bridge_pre_submit_cancel_never_registers_worker(caplog) -> None:
    caplog.set_level(logging.INFO)
    agent = _bare_agent("outbound-pre-submit-cancel")
    turn_event = agent.begin_turn()
    agent.interrupt()
    operation_started = threading.Event()

    async def operation() -> SendResult:
        operation_started.set()
        return SendResult(success=True)

    operation_coro = operation()
    loop = asyncio.new_event_loop()
    try:
        result = run_outbound_coroutine(
            operation_coro,
            loop=loop,
            timeout=1,
            platform="shared-test",
            display_name="SharedTest",
            label="file",
            cancel_event=turn_event,
            parent_agent=agent,
        )

        assert result.success is False
        assert result.interrupted is True
        assert result.completion_unknown is False
        assert operation_started.is_set() is False
        assert operation_coro.cr_frame is None
        with agent._turn_workers_lock:
            assert agent._outstanding_turn_workers == set()
        assert agent._turn_workers_drained.is_set()
        cancel_id = get_cancel_id(turn_event)
        assert any(
            "outbound_cancel_before_submit" in record.getMessage()
            and cancel_id in record.getMessage()
            and "platform=shared-test" in record.getMessage()
            for record in caplog.records
        )
    finally:
        agent.end_turn(turn_event)
        loop.close()


@pytest.mark.parametrize("trigger", ["cancel", "deadline"])
def test_outbound_bridge_returns_result_when_proxy_cancellation_loses(
    monkeypatch,
    trigger: str,
) -> None:
    loop_thread = _LoopThread()
    loop = loop_thread.start()
    release_operation = asyncio.Event()
    operation_started = threading.Event()
    cancel_event = threading.Event()
    expected = SendResult(success=True, message_id=f"completed-{trigger}")
    result_box: list[SendResult] = []
    original_request_cancel = outbound_bridge._OutboundFutureFence.request_cancel

    async def operation() -> SendResult:
        operation_started.set()
        await release_operation.wait()
        return expected

    def request_after_completion(self, future, owner_loop):
        owner_loop.call_soon_threadsafe(release_operation.set)
        deadline = time.monotonic() + 1
        while not future.done() and time.monotonic() < deadline:
            time.sleep(0.001)
        assert future.done()
        return original_request_cancel(self, future, owner_loop)

    monkeypatch.setattr(
        outbound_bridge._OutboundFutureFence,
        "request_cancel",
        request_after_completion,
    )
    caller = threading.Thread(
        target=lambda: result_box.append(
            run_outbound_coroutine(
                operation(),
                loop=loop,
                timeout=0.05 if trigger == "deadline" else 2,
                platform="race-test",
                display_name="RaceTest",
                label="file",
                cancel_event=cancel_event,
            )
        )
    )
    caller.start()
    try:
        assert operation_started.wait(1)
        if trigger == "cancel":
            cancel_event.set()
        caller.join(1)
        assert caller.is_alive() is False
        assert result_box == [expected]
    finally:
        loop.call_soon_threadsafe(release_operation.set)
        caller.join(1)
        loop_thread.stop()


@pytest.mark.parametrize("trigger", ["cancel", "deadline"])
def test_outbound_bridge_preserves_terminal_result_before_proxy_propagation(
    monkeypatch,
    trigger: str,
) -> None:
    cancel_event = threading.Event()
    expected = SendResult(success=True, message_id=f"unpropagated-{trigger}")

    async def operation() -> SendResult:
        return expected

    class DelayedProxy:
        def done(self) -> bool:
            return False

        def cancel(self) -> bool:
            return True

        def result(self, timeout=None):
            if trigger == "cancel":
                cancel_event.set()
            raise outbound_bridge.FutureTimeoutError()

    def complete_without_propagating(tracked, _loop):
        assert asyncio.run(tracked) is expected
        return DelayedProxy()

    monkeypatch.setattr(
        outbound_bridge.asyncio,
        "run_coroutine_threadsafe",
        complete_without_propagating,
    )
    result = run_outbound_coroutine(
        operation(),
        loop=_ReportedRunningLoop(),
        timeout=0 if trigger == "deadline" else 1,
        platform="race-test",
        display_name="RaceTest",
        label="file",
        cancel_event=cancel_event,
    )

    assert result is expected


def test_outbound_bridge_submission_failure_closes_coroutines_and_fence(
    monkeypatch,
) -> None:
    agent = _bare_agent("outbound-submit-failure")
    turn_event = agent.begin_turn()
    operation_started = threading.Event()
    submitted_coroutines = []

    async def operation() -> SendResult:
        operation_started.set()
        return SendResult(success=True)

    def fail_submission(coro, _loop):
        submitted_coroutines.append(coro)
        raise RuntimeError("loop stopped during submission")

    monkeypatch.setattr(
        outbound_bridge.asyncio,
        "run_coroutine_threadsafe",
        fail_submission,
    )
    operation_coro = operation()
    try:
        result = run_outbound_coroutine(
            operation_coro,
            loop=_ReportedRunningLoop(),
            timeout=1,
            platform="shared-test",
            display_name="SharedTest",
            label="file",
            cancel_event=turn_event,
            parent_agent=agent,
        )

        assert result.success is False
        assert "event loop is unavailable" in (result.error or "")
        assert operation_started.is_set() is False
        assert operation_coro.cr_frame is None
        assert len(submitted_coroutines) == 1
        assert submitted_coroutines[0].cr_frame is None
        with agent._turn_workers_lock:
            assert agent._outstanding_turn_workers == set()
        assert agent._turn_workers_drained.is_set()
    finally:
        agent.end_turn(turn_event)


def test_outbound_bridge_rejects_stopped_open_loop_before_submission() -> None:
    operation_started = threading.Event()

    async def operation() -> SendResult:
        operation_started.set()
        return SendResult(success=True)

    operation_coro = operation()
    loop = asyncio.new_event_loop()
    try:
        result = run_outbound_coroutine(
            operation_coro,
            loop=loop,
            timeout=1,
            platform="stopped-loop-test",
            display_name="StoppedLoopTest",
            label="file",
        )
    finally:
        loop.close()

    assert result.success is False
    assert "event loop is not running" in (result.error or "")
    assert operation_started.is_set() is False
    assert operation_coro.cr_frame is None


def test_outbound_bridge_promotes_fence_when_loop_stops_after_submission() -> None:
    loop_thread = _LoopThread()
    loop = loop_thread.start()
    operation_started = threading.Event()
    result_box: list[SendResult] = []
    agent = _bare_agent("outbound-loop-stopped-after-submit")
    turn_event = agent.begin_turn()

    async def operation() -> SendResult:
        operation_started.set()
        await asyncio.Event().wait()
        return SendResult(success=True)

    caller = threading.Thread(
        target=lambda: result_box.append(
            run_outbound_coroutine(
                operation(),
                loop=loop,
                timeout=0.15,
                platform="stopped-loop-test",
                display_name="StoppedLoopTest",
                label="file",
                cancel_event=turn_event,
                parent_agent=agent,
            )
        ),
        daemon=True,
    )
    caller.start()
    restart_thread = None
    try:
        assert operation_started.wait(1)
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.thread.join(1)
        assert loop_thread.thread.is_alive() is False

        caller.join(1)
        assert caller.is_alive() is False
        assert result_box and result_box[0].completion_unknown is True
        agent.end_turn(turn_event)
        with pytest.raises(RuntimeError, match="restart the runtime"):
            agent.begin_turn()

        restart_thread = threading.Thread(target=loop.run_forever, daemon=True)
        restart_thread.start()
        assert agent._turn_workers_drained.wait(1)
    finally:
        if restart_thread is not None and restart_thread.is_alive():
            loop.call_soon_threadsafe(loop.stop)
            restart_thread.join(1)
        caller.join(1)
        if not loop.is_closed():
            loop.close()


def test_outbound_bridge_cancel_before_coroutine_start_keeps_fence_until_owner_loop_runs(
    caplog,
) -> None:
    caplog.set_level(logging.INFO)
    loop_thread = _LoopThread()
    loop = loop_thread.start()
    owner_loop_blocked = threading.Event()
    release_owner_loop = threading.Event()
    operation_started = threading.Event()
    result_box: list[SendResult] = []
    agent = _bare_agent("outbound-never-start-race")
    turn_event = agent.begin_turn()

    def block_owner_loop() -> None:
        owner_loop_blocked.set()
        assert release_owner_loop.wait(2)

    async def operation() -> SendResult:
        operation_started.set()
        return SendResult(success=True)

    loop.call_soon_threadsafe(block_owner_loop)
    assert owner_loop_blocked.wait(1)
    operation_coro = operation()
    caller = threading.Thread(
        target=lambda: result_box.append(
            run_outbound_coroutine(
                operation_coro,
                loop=loop,
                timeout=2,
                platform="shared-test",
                display_name="SharedTest",
                label="file",
                cancel_event=turn_event,
                parent_agent=agent,
            )
        ),
        daemon=True,
    )
    caller.start()
    try:
        register_deadline = time.monotonic() + 1
        while time.monotonic() < register_deadline:
            with agent._turn_workers_lock:
                if agent._outstanding_turn_workers:
                    break
            time.sleep(0.005)
        with agent._turn_workers_lock:
            assert len(agent._outstanding_turn_workers) == 1

        agent.interrupt()
        caller.join(0.5)
        assert caller.is_alive() is False
        assert len(result_box) == 1
        assert result_box[0].interrupted is True
        assert result_box[0].completion_unknown is True
        assert operation_started.is_set() is False

        agent.end_turn(turn_event)
        with pytest.raises(RuntimeError, match="file send is still shutting down"):
            agent.begin_turn()

        release_owner_loop.set()
        assert agent._turn_workers_drained.wait(1)
        owner_loop_drained = threading.Event()
        loop.call_soon_threadsafe(owner_loop_drained.set)
        assert owner_loop_drained.wait(1)
        assert operation_started.is_set() is False
        assert operation_coro.cr_frame is None

        next_event = agent.begin_turn()
        agent.end_turn(next_event)

        cancel_id = get_cancel_id(turn_event)
        messages = [record.getMessage() for record in caplog.records]
        for event_name in (
            "outbound_fence_register",
            "outbound_cancel_request",
            "outbound_fence_release",
        ):
            assert any(
                event_name in message
                and cancel_id in message
                and "platform=shared-test" in message
                for message in messages
            )
    finally:
        release_owner_loop.set()
        caller.join(1)
        deadline = time.monotonic() + 1
        while not agent._turn_workers_drained.is_set() and time.monotonic() < deadline:
            time.sleep(0.01)
        loop_thread.stop()
