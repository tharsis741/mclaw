# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import hashlib
import json
from types import SimpleNamespace
import uuid

import pytest

from mclaw.application.collaboration import device_ids, import_receipts, selected_scope
from mclaw.application.device_bridge import invoke
from mclaw.application.files import FileService
from mclaw.application.process_events import finish_process, resolve_remote_waits, update_process
from mclaw.application.runner import ProcessRunner
from mclaw.application.sessions import ApplicationError, SessionService
from mclaw.application.worker import SessionWorker
from mclaw.dsoftbus.workspace import peer_directory

A, B = ("urn:mclaw:device:oh:" + letter * 64 for letter in "ab")


@pytest.mark.parametrize("value", ["auto", {}, ["nearby-device"], [None], [A] * 21])
def test_device_choices_reject_malformed_identity(value):
    with pytest.raises(ApplicationError):
        device_ids(value)


@pytest.mark.asyncio
async def test_device_scope_stays_with_original_task_across_queue_resume_and_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("MCLAW_HOME", str(tmp_path))
    service = SessionService(tmp_path)
    monkeypatch.setattr(service, "_schedule", lambda _: None)
    service.devices = SimpleNamespace(report={"devices": [
        {"deviceId": A, "deviceName": "电脑 A"}, {"deviceId": B, "deviceName": "电脑 B"},
    ]})
    sid = service.create("web")["id"]
    service.patch(sid, "web", {"draft_devices": [A, A]})
    service.submit(sid, "web", "任务一", "one")
    service.patch(sid, "web", {"draft_devices": [B]})
    result = service.submit(sid, "web", "任务二", "two")
    assert result["queue"][0]["device_selection"]["devices"] == [{"id": A, "name": "电脑 A"}]
    assert result["queue"][1]["device_selection"]["devices"] == [{"id": B, "name": "电脑 B"}]
    original = service._load(sid)
    original.update(active_task=original["queue"].pop(0), status="failed", paused=True)
    service.store.save(original)
    service.resume(sid, "web", "补充", "continue")
    assert selected_scope(service.snapshot(sid)["queue"][0]) == frozenset({A})
    await service.close()
    reopened = SessionService(tmp_path)
    saved = reopened.snapshot(sid)
    assert saved["draft_devices"] == [B]
    assert selected_scope(saved["queue"][0]) == frozenset({A})
    assert selected_scope({}) is None
    await reopened.close()


@pytest.mark.asyncio
async def test_scope_filters_peers_and_blocks_out_of_scope_calls_without_native_work():
    calls = []
    runtime = SimpleNamespace(list_peers=lambda **_: [{"device_id": A}, {"device_id": B}],
                              get_cached_device_context=lambda peer: calls.append(peer) or {"id": peer})
    service = SimpleNamespace(runtime=runtime, _closing=False)
    request = {"operation": "list_peers"}
    assert await invoke(service, request, allowed_devices={A}) == [{"device_id": A}]
    assert len(await invoke(service, request)) == 2
    for operation in ["get_cached_device_context", "get_device_context", "run_agent_task", "continue_agent_task"]:
        with pytest.raises(Exception) as caught:
            await invoke(service, {"operation": operation, "args": [B]}, allowed_devices={A})
        assert caught.value.code == "AGENT_TOOLS_FORBIDDEN"
    assert not calls


@pytest.mark.asyncio
async def test_remote_progress_keeps_call_identity_and_continuation_identifiers(tmp_path, monkeypatch):
    task_id, request_id = str(uuid.uuid4()), str(uuid.uuid4())
    worker_events, sent = [], []
    worker = SessionWorker(tmp_path, {"id": "s", "workspace": str(tmp_path)}, worker_events.append)
    worker._secret_values.add("private-test-value")
    worker.run_id = "attempt"
    runner = ProcessRunner(tmp_path, {"id": "s", "workspace": str(tmp_path)})
    def deliver(command):
        sent.append(command)
        worker.control(command)
    monkeypatch.setattr(runner, "send", deliver)
    async def native(_service, request, **options):
        assert request["args"] == [A, task_id, request_id]
        for content in ["读取资料 private-test-value", "已完成分析"]:
            options["event_sink"]({"type": "assistant.message", "remote_task_id": task_id, "content": content})
        return {"success": True, "task_id": task_id, "text": "完成", "artifacts": []}
    monkeypatch.setattr("mclaw.application.device_bridge.invoke", native)
    await runner._device_request({"id": "rpc-one", "tool_call_id": "call-one", "operation": "continue_agent_task", "args": [A, task_id, request_id]})
    await runner._device_request({"id": "rpc-two", "operation": "continue_agent_task", "args": [A, task_id, request_id]})
    final = [event for event in worker_events if event.get("status") == "completed"]
    assert len(final) == 2 and final[0]["id"] != final[1]["id"]
    assert [event["id"] for event in final[0]["progress"]] == [1, 2]
    assert final[0]["task_id"] == task_id
    assert final[0]["tool_call_id"] == "call-one"
    assert "private-test-value" not in json.dumps(worker_events)
    assert sent[-1]["type"] == "reply"


def test_continuing_remote_task_resolves_previous_wait_and_unknown_is_not_completion():
    message = {"process": [{"id": "old", "kind": "remote", "device_id": A,
                            "task_id": "same-task", "status": "waiting"}]}
    update_process(message, {"type": "process", "id": "new", "kind": "remote", "device_id": A,
                             "task_id": "same-task", "status": "running"})
    assert message["process"][0]["status"] == "waiting"
    finish_process(message)
    assert message["process"][1]["status"] == "unknown"
    update_process(message, {"id": "new", "status": "completed"})
    assert message["process"][0]["continued"] and message["process"][0]["status"] == "completed"


def test_continuation_updates_earlier_message_but_replay_cannot_close_a_later_wait():
    old = {"id": "old", "kind": "remote", "device_id": A, "task_id": "same", "status": "waiting"}
    new = {**old, "id": "new"}
    messages = [{"process": [old]}, {"process": [new]}]
    resolve_remote_waits(messages, old)
    assert new["status"] == "waiting"
    resolve_remote_waits(messages, new)
    assert old["continued"] and old["status"] == "completed"
    assert new["status"] == "waiting"


def test_concurrent_tool_calls_keep_their_identity_through_async_rpc(monkeypatch):
    import asyncio
    import concurrent.futures
    import threading
    from mclaw.application.device_bridge import WorkerDeviceClient
    from mclaw.tools import dispatch

    seen = []
    barrier = threading.Barrier(2)
    def request(_kind, **fields):
        barrier.wait(timeout=5)
        seen.append((fields["tool_call_id"], fields["args"][1]))
        return {"value": {"success": True}}
    client = WorkerDeviceClient(request)
    def execute(_name, arguments, **_kwargs):
        return json.dumps(asyncio.run(client.arun_agent_task(A, arguments["text"])))
    monkeypatch.setattr(dispatch.registry, "dispatch", execute)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda number: dispatch._dispatch_single(
            {"id": f"call-{number}", "function": {"name": "dsoftbus_run_agent_task", "arguments": json.dumps({"text": str(number)})}},
            {"dsoftbus_run_agent_task"}, None), [1, 2]))
    assert all(json.loads(result)["success"] for result in results)
    assert sorted(seen) == [("call-1", "1"), ("call-2", "2")]
    assert dispatch.get_current_tool_call_id() == ""


def receipt_fixture(tmp_path, monkeypatch):
    root = tmp_path / "native"
    monkeypatch.setattr("mclaw.dsoftbus.workspace.default_workspace_root", lambda _: root)
    task_id, artifact_id = str(uuid.uuid4()), str(uuid.uuid4())
    path = root / "artifacts" / "received" / peer_directory(A) / task_id / artifact_id / "报告.md"
    path.parent.mkdir(parents=True)
    content = "来自电脑 A 的文件".encode()
    path.write_bytes(content)
    part = {"index": 0, "localPath": str(path), "byteLength": len(content), "sha256": hashlib.sha256(content).hexdigest()}
    return path, {"task_id": task_id, "artifacts": [{"artifactId": artifact_id, "parts": [part]}]}


def test_verified_remote_file_is_previewable_session_bound_and_replay_safe(tmp_path, monkeypatch):
    path, result = receipt_fixture(tmp_path, monkeypatch)
    home = tmp_path / "home"
    monkeypatch.setenv("MCLAW_HOME", str(home))
    files, errors = import_receipts(home, "s-a", A, "电脑 A", result)
    assert not errors and len(files) == 1
    item = files[0]
    assert item["device"] == "电脑 A" and item["source"] == "remote"
    assert import_receipts(home, "s-a", A, "电脑 A", result)[0] == files
    store = FileService(home)
    assert store.preview("s-a", item["id"])["content"] == path.read_text(encoding="utf-8")
    with pytest.raises(ApplicationError):
        store.preview("s-b", item["id"])


def test_internal_reply_receipt_is_not_imported_as_a_user_file(tmp_path, monkeypatch):
    _path, result = receipt_fixture(tmp_path, monkeypatch)
    result['artifacts'][0]['artifactRole'] = 'final-response'
    assert import_receipts(tmp_path / 'home', 's-a', A, '电脑 A', result) == ([], [])


@pytest.mark.parametrize("change", ["path", "hash", "size", "peer"])
def test_remote_receipt_rejects_wrong_path_peer_or_changed_content(tmp_path, monkeypatch, change):
    path, result = receipt_fixture(tmp_path, monkeypatch)
    part = result["artifacts"][0]["parts"][0]
    if change == "path":
        outside = tmp_path / "private.txt"
        outside.write_bytes(path.read_bytes())
        part["localPath"] = str(outside)
    elif change == "hash":
        part["sha256"] = "0" * 64
    elif change == "size":
        part["byteLength"] += 1
    files, errors = import_receipts(tmp_path / "home", "s", B if change == "peer" else A, "设备", result)
    assert not files and errors
