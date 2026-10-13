# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import json
import threading
import time
from types import SimpleNamespace

import pytest

from mclaw.tools import dispatch


def _calls():
    return [{"id": name, "function": {"name": "read_file", "arguments": "{}"}}
            for name in ("slow", "fast", "failed")]


@pytest.mark.parametrize("concurrent", [False, True])
def test_tool_timings_measure_each_worker_including_failures(monkeypatch, concurrent):
    clock = threading.local()
    caller = threading.current_thread()
    durations = {"slow": 0.875, "fast": 0.125, "failed": 0.025}

    def monotonic():
        if threading.current_thread() is caller:
            return time.monotonic()
        if not hasattr(clock, "now"):
            clock.now = time.monotonic() - 100
        return clock.now

    def run(call, *_args):
        clock.now += durations[call["id"]]
        if call["id"] == "failed":
            raise ValueError("fixture failure")
        return json.dumps({"success": True})

    monkeypatch.setattr(dispatch, "time", SimpleNamespace(monotonic=monotonic))
    monkeypatch.setattr(dispatch, "_should_parallelize_tool_batch", lambda _calls: concurrent)
    monkeypatch.setattr(dispatch, "_dispatch_single", run)
    timings = {}
    results = dispatch.handle_function_calls(_calls(), {"read_file"}, timings=timings)
    assert timings == {"slow": 875, "fast": 125, "failed": 25}
    assert json.loads(results[0])["success"] and json.loads(results[1])["success"]
    assert not json.loads(results[2])["success"]


@pytest.mark.parametrize("concurrent", [False, True])
def test_cancelled_unstarted_tools_have_zero_duration(monkeypatch, concurrent):
    cancel = threading.Event()
    cancel.set()
    monkeypatch.setattr(dispatch, "_should_parallelize_tool_batch", lambda _calls: concurrent)
    monkeypatch.setattr(dispatch, "_dispatch_single", lambda *_args: pytest.fail("Cancelled tool was executed"))
    timings = {}
    results = dispatch.handle_function_calls(_calls(), {"read_file"}, cancel_event=cancel, timings=timings)
    assert timings == {"slow": 0, "fast": 0, "failed": 0}
    assert all(json.loads(result)["interrupted"] for result in results)


def test_unfinished_tool_duration_is_frozen_at_dispatch_return(monkeypatch):
    monkeypatch.setattr(dispatch, "time", SimpleNamespace(monotonic=lambda: 15.0))
    slot = {"started_at": 12.5, "finished_at": None}
    timings = {}
    dispatch._record_tool_timings(_calls(), {0: slot}, timings)
    slot["finished_at"] = 30.0
    assert timings == {"slow": 2500, "fast": 0, "failed": 0}
