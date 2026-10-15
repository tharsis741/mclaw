# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import asyncio
import threading
from types import SimpleNamespace

import pytest

from mclaw.application.device_bridge import invoke, WorkerDeviceClient
from mclaw.application.runner import ProcessRunner
from mclaw.application.worker import SessionWorker


@pytest.mark.asyncio
async def test_device_rpc_cancellation_waits_for_actual_owner_cleanup():
    loop = asyncio.get_running_loop()
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def outbound():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()

    runtime = SimpleNamespace(prepare_run_agent_task_outbound=lambda *_a, **_k: (loop, outbound(), "context", "message"))
    service = SimpleNamespace(runtime=runtime, _closing=False)
    cancel = threading.Event()
    task = asyncio.create_task(invoke(service, {"operation": "run_agent_task", "args": ["peer", "task"]},
                                      session_id="session-a", cancel=cancel))
    await asyncio.wait_for(entered.wait(), 2)
    cancel.set()
    await asyncio.wait_for(cleaning.wait(), 2)
    assert not task.done()
    release.set()
    with pytest.raises(Exception) as caught:
        await asyncio.wait_for(task, 2)
    assert caught.value.code == "AGENT_INTERRUPTED"


@pytest.mark.asyncio
async def test_device_rpc_uses_one_runtime_and_rejects_native_lifecycle():
    calls = []
    runtime = SimpleNamespace(list_peers=lambda **kw: calls.append(kw) or [{"id": "peer"}])
    service = SimpleNamespace(runtime=runtime, _closing=False)
    request = {"operation": "list_peers", "kwargs": {"ready_only": True}}
    assert await invoke(service, request, session_id="a") == [{"id": "peer"}]
    assert await invoke(service, request, session_id="b") == [{"id": "peer"}]
    assert len(calls) == 2
    for operation in ("stop", "start", "update_provider_runtime", "pair_device"):
        with pytest.raises(Exception) as caught:
            await invoke(service, {"operation": operation})
        assert caught.value.code == "INVALID_PARAMS"


@pytest.mark.asyncio
async def test_runner_routes_device_calls_privately_and_keeps_progress_flowing(tmp_path, monkeypatch):
    started, finish = asyncio.Event(), asyncio.Event()

    async def call(devices, request, **kwargs):
        assert kwargs["session_id"] == "session-a"
        started.set()
        await finish.wait()
        return {"peers": []}

    monkeypatch.setattr("mclaw.application.device_bridge.invoke", call)
    runner = ProcessRunner(tmp_path, {"id": "session-a", "workspace": str(tmp_path)})
    monkeypatch.setattr(runner, "_start", lambda: None)
    sent, displayed = [], []
    monkeypatch.setattr(runner, "send", sent.append)
    runner._output = asyncio.Queue()
    for event in ({"type": "request", "request": {"id": "rpc-a", "kind": "device_rpc", "operation": "list_peers"}},
                  {"type": "delta", "text": "progress"}, {"type": "result", "result": {"completed": True}}):
        runner._output.put_nowait({**event, "run_id": "attempt"})
    task = asyncio.create_task(runner.run({"attempts": [{"id": "attempt"}]}, displayed.append))
    await asyncio.wait_for(started.wait(), 2)
    assert not task.done()
    assert displayed == [{"type": "delta", "text": "progress"}]
    finish.set()
    assert (await asyncio.wait_for(task, 2))["completed"]
    assert sent[-1]["value"] == {"value": {"peers": []}}


def test_worker_device_cancel_is_scoped_and_waits_for_reply(tmp_path, monkeypatch):
    monkeypatch.setenv("MCLAW_HOME", str(tmp_path))
    emitted, request_seen, cancel_seen = [], threading.Event(), threading.Event()

    def emit(event):
        emitted.append(event)
        if event["type"] == "request":
            request_seen.set()
        if event["type"] == "device_cancel":
            cancel_seen.set()

    worker = SessionWorker(tmp_path, {"id": "a", "workspace": str(tmp_path)}, emit)
    child_cancel = threading.Event()
    result = []
    thread = threading.Thread(target=lambda: result.append(worker.request("device_rpc", _cancel_event=child_cancel,
                                                                          operation="run_agent_task")))
    thread.start()
    assert request_seen.wait(2)
    child_cancel.set()
    assert cancel_seen.wait(2)
    assert thread.is_alive()
    assert not worker.cancel.is_set()
    identifier = emitted[0]["request"]["id"]
    worker.control({"type": "reply", "request_id": identifier, "value": {"error": {"code": "AGENT_INTERRUPTED"}}})
    thread.join(2)
    assert not thread.is_alive()
    assert result[0]["error"]["code"] == "AGENT_INTERRUPTED"


def test_worker_proxy_preserves_unknown_remote_outcome():
    client = WorkerDeviceClient(lambda *_a, **_k: {"error": {"code": "AGENT_INTERRUPTED", "outcome_unknown": True, "interrupted": True}})
    with pytest.raises(Exception) as caught:
        client.list_peers()
    assert caught.value.outcome_unknown
    assert caught.value.interrupted


@pytest.mark.asyncio
async def test_worker_exit_cancels_inflight_native_calls_before_releasing_turn(tmp_path, monkeypatch):
    entered = asyncio.Event()
    async def call(_devices, _request, **kwargs):
        entered.set()
        while not kwargs["cancel"].is_set():
            await asyncio.sleep(0.01)
        return {"cancelled": True}
    monkeypatch.setattr("mclaw.application.device_bridge.invoke", call)
    runner = ProcessRunner(tmp_path, {"id": "session", "workspace": str(tmp_path)})
    monkeypatch.setattr(runner, "_start", lambda: None)
    monkeypatch.setattr(runner, "send", lambda _value: None)
    runner.process = SimpleNamespace(wait=lambda: 1)
    runner._output = asyncio.Queue()
    runner._output.put_nowait({"run_id": "attempt", "type": "request", "request": {"id": "rpc", "kind": "device_rpc"}})
    task = asyncio.create_task(runner.run({"attempts": [{"id": "attempt"}]}, lambda _event: None))
    await asyncio.wait_for(entered.wait(), 2)
    runner._output.put_nowait({"type": "eof"})
    result = await asyncio.wait_for(task, 2)
    assert result["completion_unknown"]
    assert not runner._device_cancels
    assert not runner._running
