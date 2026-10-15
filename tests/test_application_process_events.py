# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import json
from copy import deepcopy

import pytest

from mclaw.application.process_events import history_messages, history_process, tool_status, upgrade_process
from mclaw.application.process_events import PROCESS_VERSION, agent_events
from mclaw.application.sessions import SessionService
from mclaw.application.worker import SessionWorker


def test_timeout_retry_status_explains_the_wait():
    from mclaw.application.process_events import status_text

    assert status_text("Model response timed out; retrying in 7s...") == "模型响应超时，7 秒后重试…"


def test_remote_reply_body_is_complete_and_redacted_separately_from_technical_details():
    text = '远程正文\n' * 35000 + 'test-secret END'
    entry = agent_events({'type': 'remote.task', 'id': 'remote', 'kind': 'remote',
                          'output': {'text': text, 'success': True, 'api_key': 'another-secret'}},
                         'run', {'test-secret'})[0]
    assert entry['result_text'].endswith('<redacted> END')
    assert entry['result_text'].count('远程正文') == 35000
    assert '远程正文' not in entry['output']
    assert json.loads(entry['output'])['api_key'] == '<redacted>'


def test_worker_records_streamed_reasoning_and_one_tool_with_arguments_and_result(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text("SEARCH_API_KEY=test-private-credential\n", encoding="utf-8")
    monkeypatch.setenv("MCLAW_HOME", str(home))
    service = SessionService(home)
    sid = service.create("browser")["id"]
    session = service._load(sid)
    session["messages"] = [{"id": "response", "role": "assistant", "content": "", "process": [], "process_version": PROCESS_VERSION}]
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


def test_snapshot_migrates_cached_waiting_status_once(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("MCLAW_HOME", str(home))
    service = SessionService(home)
    try:
        sid = service.create("browser")["id"]
        session = service._load(sid)
        rows = _confirmation_rows()
        session["messages"] = [{"id": "response", "role": "assistant", "content": "确认后继续",
                                "process_version": 2, "process": [
            {"id": "tool_call-1", "kind": "tool", "title": "skill_manage", "tool_call_id": "call-1",
             "status": "failed", "output": rows[2]["content"]},
        ]}]
        service.store.save(session)
        reads = []

        def history(*args, **kwargs):
            reads.append(args)
            return rows

        monkeypatch.setattr(service.history, "get_messages", history)
        first = service.snapshot(sid)
        assert first["messages"][0]["process"][0]["status"] == "awaiting_confirmation"
        assert service.snapshot(sid)["messages"] == first["messages"]
        assert len(reads) == 1
    finally:
        service.store.close()
        service.history.close()
        for lock in service.locks.values():
            lock.release()


def test_delegated_confirmation_migration_does_not_reuse_parent_call_results():
    pending = {"id": "tool_call-1", "kind": "tool", "tool_call_id": "call-1", "status": "failed",
               "output": _confirmation_rows()[2]["content"]}
    session = {"messages": [{"id": "response", "role": "assistant", "process_version": 2,
                             "delegations": {"delegate": {"tasks": {"0": {"messages": [
        {"id": "child", "role": "assistant", "process_version": 2, "process": [pending]},
    ]}}}}}]}
    rows = [{"role": "tool", "tool_call_id": "call-1", "content": '{"error":"parent failed"}'}]
    assert upgrade_process(session, rows)
    assert pending["status"] == "awaiting_confirmation"


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


@pytest.mark.parametrize("extra,expected", [
    ({}, "awaiting_confirmation"),
    ({"success": True}, "awaiting_confirmation"),
    ({"requires_confirmation": False}, "failed"),
    ({"requires_confirmation": "true"}, "failed"),
    ({"error": "review failed"}, "failed"),
    ({"returncode": 1}, "failed"),
    ({"exit_code": 2}, "failed"),
    ({"cancelled": True}, "interrupted"),
    ({"interrupted": True}, "interrupted"),
])
def test_confirmation_status_distinguishes_waiting_from_errors(extra, expected):
    payload = {"success": False, "requires_confirmation": True,
               "confirmation_type": "skill_enable_drafting", **extra}
    assert tool_status(payload) == expected
    assert tool_status(json.dumps(payload)) == expected


def _confirmation_rows():
    rows = _rows()
    rows[1]["tool_calls"][0]["function"] = {
        "name": "skill_manage", "arguments": '{"action":"install_prepare"}',
    }
    rows[2]["content"] = json.dumps({
        "success": False, "requires_confirmation": True,
        "confirmation_type": "skill_enable_drafting", "drafting_id": "draft-test",
    })
    return rows


def test_live_and_historical_confirmation_calls_have_the_same_waiting_status():
    rows = _confirmation_rows()
    live = agent_events({"type": "tool.finished", "tool_call_id": "call-1",
                         "name": "skill_manage", "result": rows[2]["content"]}, "test")
    historical = history_process(rows)
    assert live[0]["status"] == "awaiting_confirmation"
    assert next(entry for entry in historical if entry["kind"] == "tool")["status"] == "awaiting_confirmation"


@pytest.mark.parametrize("clipped_output", [False, True])
def test_cached_confirmation_status_is_upgraded_without_rebuilding_other_entries(clipped_output):
    rows = _confirmation_rows()
    original_rows = deepcopy(rows)
    other = {"id": "remote", "kind": "remote", "title": "远程任务", "status": "waiting",
             "result_text": "保留远程回复", "input_request": {"message": "补充资料"}}
    session = {"messages": [{"id": "response", "role": "assistant", "content": "确认后继续",
                            "created_at": 2, "process_version": 2, "process": [
        {"id": "tool_call-1", "kind": "tool", "title": "skill_manage", "tool_call_id": "call-1",
         "status": "failed", "output": '{"success":false,' if clipped_output else rows[2]["content"]},
        deepcopy(other),
    ]}]}
    assert upgrade_process(session, rows)
    assert session["messages"][0]["process"][0]["status"] == "awaiting_confirmation"
    assert session["messages"][0]["process"][1] == other
    assert session["messages"][0]["content"] == "确认后继续"
    assert rows == original_rows
    assert not upgrade_process(session, rows)
