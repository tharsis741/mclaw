# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import json
from copy import deepcopy

import pytest

from mclaw.application.process_events import history_messages, history_process, tool_status, upgrade_process
from mclaw.application.sessions import SessionService
from mclaw.application.worker import SessionWorker


def test_worker_records_streamed_reasoning_and_one_tool_with_arguments_and_result(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text("SEARCH_API_KEY=test-private-credential\n", encoding="utf-8")
    monkeypatch.setenv("MCLAW_HOME", str(home))
    service = SessionService(home)
    sid = service.create("browser")["id"]
    session = service._load(sid)
    session["messages"] = [{"id": "response", "role": "assistant", "content": "", "process": [], "process_version": 2}]
    service.store.save(session)
    worker = SessionWorker(home, {"id": sid, "workspace": str(tmp_path)}, lambda event: service._event(sid, "response", event))
    worker.run_id = "attempt"
    for content in ["Check", "Check the file"]:
        worker._agent_event({"type": "assistant.reasoning", "content": content, "api_call_index": 1, "turn_id": "turn"})
    worker._agent_event({"type": "assistant.message", "reasoning": "Check the file", "content": "Ready", "is_final": False,
                         "api_call_index": 1, "turn_id": "turn", "content_source": "content"})
    worker._agent_event({"type": "tool.started", "tool_call_id": "call-one", "name": "terminal",
                         "arguments": {"command": "echo test-private-credential", "api_key": "another-secret"}})
    worker._status("Running 1 tool(s)...")
    worker._agent_event({"type": "tool.finished", "tool_call_id": "call-one", "name": "terminal",
                         "result": json.dumps({"output": "test-private-credential", "exit_code": 1}), "elapsed_ms": 15})
    worker._agent_event({"type": "tool.finished", "tool_call_id": "call-one", "name": "terminal",
                         "result": json.dumps({"output": "test-private-credential", "exit_code": 1}), "elapsed_ms": 15})
    saved = service.snapshot(sid)
    entries = saved["messages"][0]["process"]
    assert len(entries) == 3
    assert entries[0]["content"] == "Check the file" and entries[0]["status"] == "completed"
    assert entries[1]["content"] == "Ready"
    tool = entries[2]
    assert tool["kind"] == "tool" and tool["status"] == "failed"
    assert "echo <redacted>" in tool["arguments"]
    assert "another-secret" not in json.dumps(saved)
    assert "test-private-credential" not in json.dumps(saved)
    assert saved["status_text"] == "正在执行工具…"
    service.store.close()
    service.history.close()
    for lock in service.locks.values():
        lock.release()


def _rows():
    return [
        {"id": 1, "role": "user", "content": "inspect", "timestamp": 1},
        {"id": 2, "role": "assistant", "content": "", "reasoning": "Inspect the directory first.", "timestamp": 3,
         "tool_calls": [{"id": "call-1", "function": {"name": "terminal", "arguments": '{"command":"ls","token":"secret-value"}'}}]},
        {"id": 3, "role": "tool", "tool_call_id": "call-1", "content": '{"output":"file.txt","exit_code":0}', "timestamp": 4},
        {"id": 4, "role": "assistant", "content": "Done", "reasoning": "One file.", "timestamp": 5},
    ]


def test_prior_web_process_is_rebuilt_from_history_without_rewriting_history():
    rows = _rows()
    original = deepcopy(rows)
    session = {"messages": [{"id": "attempt", "role": "assistant", "content": "Done", "created_at": 2,
                              "process": [{"id": str(i), "kind": "progress", "title": "progress"} for i in range(45)]}]}
    assert upgrade_process(session, rows)
    assert rows == original
    entries = session["messages"][0]["process"]
    assert sum(entry["kind"] == "tool" for entry in entries) == 1
    assert [entry["content"] for entry in entries if entry["kind"] == "reasoning"] == ["Inspect the directory first.", "One file."]
    assert "secret-value" not in json.dumps(entries)
    assert not upgrade_process(session, rows)


def test_historical_import_keeps_reasoning_only_rounds_and_tool_output():
    messages = history_messages(_rows())
    assert len(messages) == 3
    assert messages[1]["content"] == ""
    assert messages[1]["process"][1]["output"] == '{\n  "output": "file.txt",\n  "exit_code": 0\n}'
    assert messages[-1]["process"][0]["content"] == "One file."


def test_mixed_import_and_web_history_keeps_results_without_repeating_message_body():
    rows = _rows()
    rows[1]["content"] = "I will inspect the directory."
    session = {"messages": [{"id": "history_2", "role": "assistant", "content": rows[1]["content"], "created_at": 3},
                            {"id": "web-response", "role": "assistant", "content": "Done", "created_at": 5}]}
    upgrade_process(session, rows)
    entries = session["messages"][0]["process"]
    assert [entry["kind"] for entry in entries] == ["reasoning", "tool"]
    assert "file.txt" in entries[1]["output"]
    assert entries[1]["status"] == "completed"


def test_missing_tool_result_stays_unknown_and_opaque_reasoning_is_not_exposed():
    rows = _rows()[:2]
    rows[1].update(reasoning=None, reasoning_details={"payload": "encrypted-provider-payload"})
    entries = history_process(rows)
    assert len(entries) == 1 and entries[0]["status"] == "unknown"
    assert "encrypted-provider-payload" not in json.dumps(entries)


def test_two_attempts_keep_their_own_process_records():
    rows = _rows() + [{"id": 5, "role": "assistant", "content": "Next", "reasoning": "Next reasoning", "timestamp": 9}]
    session = {"messages": [{"id": "one", "role": "assistant", "content": "Done", "created_at": 2},
                            {"id": "two", "role": "assistant", "content": "Next", "created_at": 8}]}
    upgrade_process(session, rows)
    assert len(session["messages"][0]["process"]) == 3
    assert len(session["messages"][1]["process"]) == 1


@pytest.mark.parametrize("result,status", [
    ({"returncode": 1, "error": "", "output": "failed"}, "failed"),
    ({"returncode": 0, "error": "", "output": "ok"}, "completed"),
    ({"exit_code": 2}, "failed"),
    ({"interrupted": True}, "interrupted"),
])
def test_tool_status_uses_terminal_return_code(result, status):
    assert tool_status(json.dumps(result)) == status
