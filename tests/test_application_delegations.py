# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import json
from types import SimpleNamespace

import pytest

from mclaw.application.sessions import SessionService
from mclaw.application.process_events import PROCESS_VERSION
from mclaw.application.worker import SessionWorker
from mclaw.tools.delegate_tool import SubtaskEvent, _run_single_child


@pytest.fixture
def delegation(tmp_path, monkeypatch):
    monkeypatch.setenv("MCLAW_HOME", str(tmp_path))
    (tmp_path / ".env").write_text("SEARCH_API_KEY=private-test-value\n", encoding="utf-8")
    service = SessionService(tmp_path)
    sid = service.create("browser")["id"]
    session = service._load(sid)
    session["messages"] = [{"id": "reply", "role": "assistant", "content": "主代理的回复", "process": [], "process_version": PROCESS_VERSION}]
    service.store.save(session)
    worker = SessionWorker(tmp_path, {"id": sid, "workspace": str(tmp_path)}, lambda event: service._event(sid, "reply", event))
    worker.run_id = "attempt"
    yield service, worker, sid
    for lock in service.locks.values():
        lock.release()
    service.store.close()
    service.history.close()


def test_child_tools_and_streams_are_isolated_redacted_and_persisted(delegation):
    service, worker, sid = delegation
    worker._delegation_event(SubtaskEvent(-1, "batch_started", {"num_tasks": 2, "goals": ["检查接口", "检查文档"], "tool_call_id": "parent-call"}, delegation_id="batch"))
    for index in range(2):
        worker._delegation_event(SubtaskEvent(index, "started", {"goal": f"任务 {index}", "model": "test-model"}, delegation_id="batch"))
    for index in (1, 0):
        events = [
            {"type": "assistant.reasoning", "content": f"任务 {index} 的过程", "api_call_index": 1},
            {"type": "delta", "text": "准备检查"},
            {"type": "assistant.message", "content": "准备检查", "was_streamed": True, "is_final": False, "api_call_index": 1},
            {"type": "tool.started", "tool_call_id": "same-id", "name": "terminal", "arguments": {"command": f"echo task-{index}", "api_key": "other-private-value"}},
            {"type": "tool.finished", "tool_call_id": "same-id", "name": "terminal", "result": {"output": f"task-{index} private-test-value", "exit_code": index}},
            {"type": "delta", "text": f"任务 {index} 的结果"},
        ]
        for event in events:
            worker._delegation_event(SubtaskEvent(index, "activity", event, delegation_id="batch"))
    worker._delegation_event(SubtaskEvent(0, "completed", {"status": "completed", "summary": "结果 0", "duration": 1.2}, delegation_id="batch"))
    # Large activity counts must not erase the child's earlier tool details.
    for i in range(205):
        worker._delegation_event(SubtaskEvent(1, "activity", {"type": "status", "text": f"阶段 {i}"}, delegation_id="batch"))
    saved = service.snapshot(sid)["messages"][0]
    assert saved["content"] == "主代理的回复" and saved["process"] == []
    group = saved["delegations"]["batch"]
    assert len(group["events"]) == 3
    for index in range(2):
        task = group["tasks"][str(index)]
        assert task["messages"][0]["created_at"] <= task["messages"][-1]["created_at"]
        assert task["messages"][-1]["created_at"] == task["started_at"]
        process = task["messages"][-1]["process"]
        assert process[-1]["tool_call_id"] == "same-id"
        assert f"task-{index}" in process[-1]["output"]
        assert process[-1]["status"] == ("completed" if index == 0 else "failed")
        assert len([entry for entry in process if entry["kind"] == "tool"]) == 1
    assert group["tasks"]["0"]["messages"][-1]["content"] == "结果 0"
    assert group["tasks"]["1"]["messages"][-1]["content"] == "任务 1 的结果"
    assert "private-test-value" not in json.dumps(saved)
    assert "other-private-value" not in json.dumps(saved)
    assert service.store.get(sid)["messages"][0] == saved


def test_real_child_callback_routing_keeps_parent_stream_untouched(delegation, monkeypatch):
    service, worker, sid = delegation
    parent_stream = []
    class Child:
        max_iterations = 2
        tools = []
        session_api_calls = session_input_tokens = session_output_tokens = 0
        session_id = "child-session"
        model = "test-model"
        _delegate_depth = 1
        _stream_callback = parent_stream.append
        _status_callback = parent_stream.append

        def run_conversation(self, **kwargs):
            self._status_callback("Thinking...")
            self._stream_callback("独立回复")
            self._tool_callback("read_file", {"path": "README.md"})
            self._event_callback({"type": "tool.started", "tool_call_id": "read", "name": "read_file", "arguments": {"path": "README.md"}})
            self._event_callback({"type": "tool.finished", "tool_call_id": "read", "name": "read_file", "result": "文件内容"})
            return {"final_response": "已检查", "completed": True, "api_calls": 1}
    monkeypatch.setattr("mclaw.tools.terminal_tool.cleanup_session", lambda _sid: None)
    parent = SimpleNamespace(_delegate_live_callback=worker._delegation_event)
    result = _run_single_child(0, "检查文件", Child(), parent, worker._delegation_event, delegation_id="one")
    assert result["status"] == "completed"
    assert parent_stream == []
    message = service.snapshot(sid)["messages"][0]
    assert message["content"] == "主代理的回复"
    task = message["delegations"]["one"]["tasks"]["0"]
    assert task["model"] == "test-model"
    assert task["messages"][-1]["content"] == "已检查"
    assert task["messages"][-1]["process"][0]["output"] == "文件内容"


@pytest.mark.parametrize("state", ["failed", "interrupted", "timed_out", "completion_unknown"])
def test_terminal_child_states_do_not_become_success_or_accept_late_text(delegation, state):
    service, worker, sid = delegation
    worker._delegation_event(SubtaskEvent(0, "started", {"goal": "任务"}, delegation_id="batch"))
    worker._delegation_event(SubtaskEvent(0, "completed", {"status": state, "summary": "部分结果"}, delegation_id="batch"))
    worker._delegation_event(SubtaskEvent(0, "activity", {"type": "delta", "text": "迟到的输出"}, delegation_id="batch"))
    task = service.snapshot(sid)["messages"][0]["delegations"]["batch"]["tasks"]["0"]
    assert task["status"] == state
    assert task["messages"][-1]["content"] == "部分结果"


def test_restart_retains_detail_and_marks_unfinished_children_unknown(delegation):
    service, worker, sid = delegation
    worker._delegation_event(SubtaskEvent(0, "started", {"goal": "任务"}, delegation_id="batch"))
    worker._delegation_event(SubtaskEvent(0, "activity", {"type": "tool.started", "tool_call_id": "read", "name": "read_file", "arguments": {"path": "README.md"}}, delegation_id="batch"))
    session = service._load(sid)
    session["active_task"] = {"id": "task"}
    service.store.save(session)
    service.store.recover()
    task = service.snapshot(sid)["messages"][0]["delegations"]["batch"]["tasks"]["0"]
    assert task["status"] == "completion_unknown"
    assert task["messages"][-1]["process"][0]["status"] == "unknown"


def test_finalizing_keeps_partial_work_separate_from_the_summary(delegation):
    service, worker, sid = delegation
    for kind, data in [
        ("started", {"goal": "任务"}),
        ("activity", {"type": "delta", "text": "检查到一半"}),
        ("finalizing", {"reason": "max_iterations"}),
        ("activity", {"type": "delta", "text": "已有结论"}),
    ]:
        worker._delegation_event(SubtaskEvent(0, kind, data, delegation_id="batch"))
    task = service.snapshot(sid)["messages"][0]["delegations"]["batch"]["tasks"]["0"]
    assert task["status"] == "finalizing"
    assert task["messages"][-1]["content"] == "已有结论"
    assert task["messages"][-1]["process"][0]["content"] == "检查到一半"
