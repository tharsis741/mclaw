# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import asyncio
import sys
from types import SimpleNamespace

import pytest

from mclaw.application.runner import ProcessRunner


@pytest.mark.asyncio
async def test_parent_channel_delivery_keeps_events_flowing_and_drains_before_result(tmp_path, monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()
    async def send(home, session_id, request):
        assert home == tmp_path and session_id == "session-a" and request["id"] == "file-request"
        started.set()
        await release.wait()
        return {"success": True, "message_id": "delivered"}

    monkeypatch.setitem(sys.modules, "mclaw.application.channel_outbound", SimpleNamespace(handle_channel_outbound=send))
    runner = ProcessRunner(tmp_path, {"id": "session-a", "workspace": str(tmp_path)})
    commands, events = [], []
    monkeypatch.setattr(runner, "_start", lambda: None)
    monkeypatch.setattr(runner, "send", commands.append)
    runner._output = asyncio.Queue()
    for event in [
        {"type": "request", "request": {"id": "file-request", "kind": "channel_outbound"}},
        {"type": "delta", "text": "progress"},
        {"type": "result", "result": {"completed": True}},
    ]:
        runner._output.put_nowait(event | {"run_id": "attempt"})
    running = asyncio.create_task(runner.run({"attempts": [{"id": "attempt"}]}, events.append))
    await asyncio.wait_for(started.wait(), 2)
    assert not running.done()
    assert events == [{"type": "delta", "text": "progress"}]
    release.set()
    assert await asyncio.wait_for(running, 2) == {"completed": True}
    assert commands[-1] == {"type": "reply", "request_id": "file-request", "value": {"success": True, "message_id": "delivered"}}


@pytest.mark.asyncio
async def test_channel_rpc_failure_returns_bounded_safe_reply(tmp_path, monkeypatch):
    async def fail(*_args):
        raise RuntimeError("sensitive-route-token")
    monkeypatch.setitem(sys.modules, "mclaw.application.channel_outbound", SimpleNamespace(handle_channel_outbound=fail))
    runner = ProcessRunner(tmp_path, {"id": "session-a", "workspace": str(tmp_path)})
    commands = []
    monkeypatch.setattr(runner, "send", commands.append)
    await runner._channel_request({"id": "request", "kind": "channel_outbound"})
    assert commands[-1]["value"]["success"] is False
    assert "sensitive-route-token" not in str(commands)
