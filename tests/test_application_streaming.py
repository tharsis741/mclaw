# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import asyncio
import copy
import json

import pytest

from mclaw.application.sessions import SessionService
from mclaw.application.process_events import PROCESS_VERSION
from mclaw.application.streaming import EventBatcher, snapshot_changes


@pytest.mark.asyncio
async def test_stream_batches_keep_order_and_flush_before_user_requests(tmp_path, monkeypatch):
    monkeypatch.setenv("MCLAW_HOME", str(tmp_path))
    service = SessionService(tmp_path)
    sid = service.create("browser")["id"]
    session = service._load(sid)
    session["messages"] = [{"id": "reply", "role": "assistant", "content": "", "process": [], "process_version": PROCESS_VERSION}]
    service.store.save(session)
    writes = []
    save = service.store.save
    monkeypatch.setattr(service.store, "save", lambda value: (writes.append(value["seq"]), save(value)))
    batch = EventBatcher(lambda event: service._event(sid, "reply", event))
    for character in "准备检查" * 10:
        batch.push({"type": "delta", "text": character})
    batch.push({"type": "body_reset"})
    batch.push({"type": "process", "id": "progress", "kind": "progress", "content": "准备检查"})
    batch.push({"type": "delta", "text": "最终回复"})
    batch.push({"type": "request", "request": {"id": "confirmation", "kind": "workspace"}})
    saved = service.snapshot(sid)
    assert saved["messages"][0]["content"] == "最终回复"
    assert saved["messages"][0]["process"][0]["content"] == "准备检查"
    assert saved["pending_request"]["id"] == "confirmation"
    assert len(writes) == 2
    batch.push({"type": "request_resolved", "request_id": "confirmation"})
    batch.push({"type": "delta", "text": "继续"})
    await asyncio.sleep(0.04)
    assert service.snapshot(sid)["messages"][0]["content"] == "最终回复继续"
    assert service.snapshot(sid)["pending_request"] is None
    batch.flush()
    await service.close()


def _apply(value, changes):
    value = copy.deepcopy(value)
    for change in changes:
        target = value
        for key in change["path"][:-1]:
            target = target[key]
        key = change["path"][-1]
        if change["op"] == "remove":
            del target[key]
        elif change["op"] == "append":
            target[key] += change["value"]
        else:
            target[key] = change["value"]
    return value


@pytest.mark.asyncio
async def test_scheduled_persistence_failure_is_reported_at_the_result_boundary():
    def fail(_event):
        raise OSError("storage unavailable")
    batch = EventBatcher(fail, interval=0.001)
    batch.push({"type": "delta", "text": "正在写入"})
    await asyncio.sleep(0.01)
    with pytest.raises(OSError, match="storage unavailable"):
        batch.flush()


def test_stream_patch_only_sends_new_text_in_long_history_and_child_output():
    old = {"seq": 1, "messages": [{"id": str(i), "content": "已完成的回复" * 1000} for i in range(80)]}
    old["messages"].append({"id": "reply", "content": "", "delegations": {"a": {"tasks": {
        "0": {"messages": [{"content": "正在", "process": [{"id": "tool", "output": "large output" * 5000}]}]}
    }}}})
    new = copy.deepcopy(old)
    new["seq"] = 2
    new["messages"][-1]["delegations"]["a"]["tasks"]["0"]["messages"][0]["content"] += "检查"
    changes = snapshot_changes(old, new)
    assert _apply(old, changes) == new
    assert len(json.dumps(changes)) < 400
    assert "large output" not in json.dumps(changes)


def test_stream_patch_handles_body_reset_deletions_tool_results_and_appended_turns():
    old = {"seq": 2, "messages": [{"id": "reply", "content": "临时文字", "process": [{"id": "tool", "status": "running"}]}], "error": "old"}
    new = {"seq": 3, "messages": [{"id": "reply", "content": "", "process": [{"id": "tool", "status": "failed", "output": "error"}]}, {"id": "next", "content": "第二轮"}]}
    assert _apply(old, snapshot_changes(old, new)) == new
    assert _apply(new, snapshot_changes(new, old)) == old
