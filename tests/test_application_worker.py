# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import asyncio
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from mclaw.application.runner import ProcessRunner
from mclaw.application.worker import SessionWorker, _CONTINUE_MESSAGE, resolve_task_provider
from mclaw.state import SessionDB


@pytest.mark.parametrize("approved", [False, True])
def test_worker_skill_install_decision_is_persisted_and_cancel_pauses(worker, tmp_path, monkeypatch, approved):
    from mclaw.application.skills import SkillService
    source = tmp_path / "skill-source"
    source.mkdir()
    (source / "SKILL.md").write_text("---\nname: example\ndescription: Example\n---\n\nRead the task carefully.\n", encoding="utf-8")
    skills = SkillService(worker.home)
    prepared = skills.prepare(str(source))
    worker.db = SessionDB(worker.home / "state.db")
    worker.db.create_session(session_id=worker.session["id"], source="test")
    worker.agent = SimpleNamespace(_checkpoint_turn_id="turn", mark_prompt_epoch_dirty=lambda: None)
    def reply(kind, **detail):
        assert kind == "skill_install"
        return {"approved": approved, "confirmation_token": detail["confirmation_token"]}
    monkeypatch.setattr(worker, "request", reply)
    assert worker._resolve_skill_install(prepared) is approved
    rows = worker.db.get_messages(worker.session["id"])
    assert len(rows) == 1 and rows[0]["operation_id"] == "skill-install:" + prepared["drafting_id"]
    assert (worker.home / "skills" / "example").exists() is approved
    assert any(event["type"] == "skill_install_resolved" for event in worker.events)
    worker.db.close()


def test_worker_recovers_committed_install_without_another_install(worker, tmp_path, monkeypatch):
    from mclaw.application.skills import SkillService
    source = tmp_path / "skill-source"
    source.mkdir()
    (source / "SKILL.md").write_text("---\nname: example\ndescription: Example\n---\n\nRead the task carefully.\n", encoding="utf-8")
    skills = SkillService(worker.home)
    prepared = skills.prepare(str(source))
    skills.confirm(prepared["drafting_id"], prepared["confirmation_token"])
    worker.db = SessionDB(worker.home / "state.db")
    worker.db.create_session(session_id=worker.session["id"], source="test")
    worker.agent = SimpleNamespace(_checkpoint_turn_id="turn", mark_prompt_epoch_dirty=lambda: None)
    monkeypatch.setattr(worker, "request", lambda *_a, **_k: pytest.fail("Already committed decision must not ask again"))
    assert worker._resolve_skill_install(prepared)
    assert worker._resolve_skill_install(prepared)
    assert len(worker.db.get_messages(worker.session["id"])) == 1
    worker.db.close()


@pytest.fixture
def worker(tmp_path, monkeypatch):
    home = tmp_path / "state"
    workspace = tmp_path / "work"
    workspace.mkdir()
    monkeypatch.setenv("MCLAW_HOME", str(home))
    events = []
    instance = SessionWorker(home, {"id": "worker-test", "workspace": str(workspace)}, events.append)
    instance.events = events
    return instance


def _task(**changes):
    return {"id": "task-one", "content": "original work", "model": "test-model", "attempts": [{"id": "attempt-one"}], **changes}


def test_provider_selector_distinguishes_identical_model_names(monkeypatch):
    from mclaw.providers import resolver

    calls = []
    monkeypatch.setattr(resolver, "resolve_provider_runtime_context", lambda **kw: calls.append(kw) or kw)
    first = resolve_task_provider({"model": "first::same-model", "provider_snapshot": {"provider": "old"}}, {})
    second = resolve_task_provider({"model": "second::same-model"}, {})
    assert first["provider"] == "first"
    assert second["provider"] == "second"
    assert first["model"] == second["model"] == "same-model"


def test_legacy_raw_model_restores_provider_snapshot(monkeypatch):
    from mclaw.providers import resolver

    calls = []
    monkeypatch.setattr(resolver, "restore_provider_runtime_context", lambda snapshot, **kw: calls.append((snapshot, kw)))
    snapshot = {"provider": "saved-provider"}
    resolve_task_provider({"model": "legacy-model", "provider_snapshot": snapshot}, {"model": "default"})
    assert calls == [(snapshot, {"config": {"model": "default"}, "model": "legacy-model"})]


def test_worker_stop_unblocks_authorization_without_accepting_reply(worker):
    requested = threading.Event()
    worker.emit_raw = lambda event: (worker.events.append(event), requested.set())
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(worker.request, "workspace", title="trust")
        assert requested.wait(3)
        worker.control({"type": "stop"})
        with pytest.raises(InterruptedError):
            future.result(timeout=3)
    assert worker._waiting == {}
    assert worker.events[-1]["type"] == "request_resolved"


def test_worker_secret_reply_is_scoped_and_never_echoed(worker):
    requested = threading.Event()
    worker.emit_raw = lambda event: (worker.events.append(event), requested.set())
    needs = [{"env_var": "TEST_TOKEN", "state": "missing", "purpose": "testing"}]
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(worker.secret_request, "tool:terminal", needs)
        assert requested.wait(3)
        identifier = worker.events[0]["request"]["id"]
        worker.control({"type": "reply", "request_id": identifier, "value": {
            "values": {"TEST_TOKEN": "private-test-value", "UNREQUESTED": "ignored"},
            "authorized": ["TEST_TOKEN", "UNREQUESTED"], "skipped": [],
        }})
        result = future.result(timeout=3)
    assert result == {"values": {"TEST_TOKEN": "private-test-value"}, "authorized": ["TEST_TOKEN"], "skipped": []}
    worker.emit({"type": "progress", "text": "private-test-value"})
    assert "private-test-value" not in json.dumps(worker.events)
    assert worker.events[-1]["text"] == "<redacted>"


def test_worker_applies_supplements_once_after_database_commit(worker):
    worker.home.mkdir()
    worker.db = SessionDB(worker.home / "state.db")
    worker.db.create_session(session_id=worker.session["id"], source="web")
    item = {"id": "addition", "content": "new information", "applied": False}
    worker._supplements = {item["id"]: item}
    messages = []
    worker._apply_supplements(messages, "turn-one")
    worker._supplements = {item["id"]: item}
    worker._apply_supplements(messages, "turn-one")
    assert messages == [{"role": "user", "content": "new information"}]
    rows = worker.db.get_messages(worker.session["id"])
    assert len(rows) == 1
    assert rows[0]["operation_id"] == "supplement:addition"
    assert rows[0]["turn_id"] == "turn-one"
    worker.db.close()


def test_failed_supplement_persistence_is_not_acknowledged(worker):
    def fail(*_args, **_kwargs):
        raise OSError("test persistence failure")
    worker.db = SimpleNamespace(append_message=fail)
    worker._supplements = {"addition": {"id": "addition", "content": "new information"}}
    messages = []
    with pytest.raises(OSError):
        worker._apply_supplements(messages, "turn-one")
    assert not messages
    assert not worker.events
    assert "addition" in worker._supplements


def test_resume_passes_saved_context_and_does_not_replay_original_instruction(worker, monkeypatch):
    calls = []
    agent = SimpleNamespace(messages=[{"role": "user", "content": "original work"}, {"role": "assistant", "content": "known progress"}])
    agent.run_conversation = lambda content, **kw: (calls.append((content, kw)) or {"completed": True, "final_response": "done"})
    worker.agent = agent
    worker.db = SimpleNamespace(get_messages=lambda *_args, **_kwargs: [{"operation_id": "task:task-one"}])
    monkeypatch.setattr(worker, "_prepare_agent", lambda _task: agent)
    result = worker.execute(_task(attempts=[{"id": "first"}, {"id": "retry"}]), worker.cancel)
    assert result["completed"] is True
    assert calls[0][0] == _CONTINUE_MESSAGE
    assert calls[0][1]["conversation_history"] == agent.messages
    assert calls[0][1]["input_operation_id"] is None


def test_retry_without_persisted_input_executes_original_with_task_marker(worker, monkeypatch):
    calls = []
    agent = SimpleNamespace(messages=[{"role": "user", "content": "an unrelated older task"}])
    agent.run_conversation = lambda content, **kw: (calls.append((content, kw)) or {"completed": True})
    worker.agent = agent
    worker.db = SimpleNamespace(get_messages=lambda *_args, **_kwargs: [{"operation_id": "task:older-task"}])
    monkeypatch.setattr(worker, "_prepare_agent", lambda _task: agent)
    worker.execute(_task(attempts=[{"id": "failed-configuration"}, {"id": "fixed"}]), worker.cancel)
    assert calls[0][0] == "original work"
    assert calls[0][1]["input_operation_id"] == "task:task-one"


def test_worker_does_not_report_stopped_until_tool_fence_drains(worker, monkeypatch):
    drained = threading.Event()
    returned = threading.Event()
    agent = SimpleNamespace(messages=[], _turn_workers_drained=drained)
    def run(*_args, **_kwargs):
        returned.set()
        return {"completed": False, "interrupted": True}
    agent.run_conversation = run
    worker.agent = agent
    worker.db = SimpleNamespace(get_messages=lambda *_args, **_kwargs: [])
    monkeypatch.setattr(worker, "_prepare_agent", lambda _task: agent)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(worker.execute, _task(), worker.cancel)
        assert returned.wait(3)
        assert not future.done()
        drained.set()
        assert future.result(timeout=3)["interrupted"] is True


@pytest.mark.asyncio
async def test_real_worker_configuration_failure_remains_alive_until_close(tmp_path):
    home, workspace = tmp_path / "state", tmp_path / "work"
    home.mkdir()
    workspace.mkdir()
    (home / "config.yaml").write_text("active_provider: invalid-worker-test-provider\nmodel: test-model\n", encoding="utf-8")
    runner = ProcessRunner(home, {"id": "configuration-test", "workspace": str(workspace)})
    try:
        result = await asyncio.wait_for(runner.run(_task(), lambda _event: None), 20)
        assert result["error_code"] == "unknown_provider"
        assert result["settings_required"] is True
        assert runner.process.poll() is None
        previous_pid = runner.process.pid
        runner.process.kill()
        await asyncio.to_thread(runner.process.wait)
        retried = await asyncio.wait_for(runner.run(_task(attempts=[{"id": "retry"}]), lambda _event: None), 20)
        assert retried["error_code"] == "unknown_provider"
        assert runner.process.pid != previous_pid
    finally:
        await asyncio.wait_for(runner.close(), 20)
    assert runner.process.returncode == 0


@pytest.fixture
def local_provider():
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            chunks = []
            for content, finish in [("本地测试回复", None), (None, "stop")]:
                payload = {"id": "local-test", "object": "chat.completion.chunk", "created": 1, "model": body["model"],
                           "choices": [{"index": 0, "delta": {"content": content} if content else {}, "finish_reason": finish}]}
                chunks.append(("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode("utf-8"))
            data = b"".join(chunks) + b"data: [DONE]\n\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            self.wfile.flush()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1], requests
    server.shutdown()
    server.server_close()
    thread.join(timeout=3)


@pytest.mark.asyncio
async def test_real_worker_stream_history_model_change_and_trust(local_provider, tmp_path):
    import yaml

    port, requests = local_provider
    home, workspace = tmp_path / "state", tmp_path / "work"
    home.mkdir()
    workspace.mkdir()
    config = {"active_provider": "custom", "model": "test-model", "compression": {"enabled": False},
              "checkpoints": {"enabled": False}, "memory": {"memory_review_round": 0},
              "skills": {"evolution_review_round": 0}}
    (home / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    (home / ".env").write_text(f"MCLAW_BASE_URL=http://127.0.0.1:{port}/v1\nMCLAW_API_KEY=local-test-credential\n", encoding="utf-8")
    runner = ProcessRunner(home, {"id": "stream-test", "workspace": str(workspace)})
    events = []
    approve = False
    def emit(event):
        events.append(event)
        if event.get("type") == "request":
            assert event["request"]["kind"] == "workspace"
            runner.send({"type": "reply", "request_id": event["request"]["id"], "value": approve})
    try:
        declined = await asyncio.wait_for(runner.run(_task(), emit), 30)
        assert declined["completed"] is False
        assert not requests
        approve = True
        first = await asyncio.wait_for(runner.run(_task(attempts=[{"id": "declined"}, {"id": "retry"}]), emit), 30)
        assert first.get("completed") is True, first
        assert first["final_response"] == "本地测试回复"
        process_id = runner.process.pid
        assert any(event["type"] == "delta" and "本地" in event["text"] for event in events)
        second = await asyncio.wait_for(runner.run(_task(id="task-two", content="follow up", model="other-model", attempts=[{"id": "attempt-two"}]), emit), 30)
        assert second.get("completed") is True, second
        assert runner.process.pid == process_id
        assert requests[-1]["model"] == "other-model"
        assert any(message.get("content") == "original work" for message in requests[-1]["messages"])
        assert sum(event["type"] == "request" for event in events) == 2
        assert "local-test-credential" not in json.dumps(events)
    finally:
        await asyncio.wait_for(runner.close(), 20)
    db = SessionDB(home / "state.db")
    assert db.get_session("stream-test")["workspace"] == str(workspace)
    rows = db.get_messages("stream-test")
    assert [row["role"] for row in rows] == ["user", "assistant", "user", "assistant"]
    assert [row["operation_id"] for row in rows] == ["task:task-one", None, "task:task-two", None]
    db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["task_thread", "new_session"])
async def test_real_scheduler_workers_finish_without_workspace_prompt(local_provider, tmp_path, monkeypatch, policy):
    import yaml

    from mclaw.application.schedules import ScheduleService
    from mclaw.application.sessions import SessionService

    port, requests = local_provider
    home = tmp_path / "scheduler-home"
    home.mkdir()
    monkeypatch.setenv("MCLAW_HOME", str(home))
    config = {"active_provider": "custom", "model": "test-model", "compression": {"enabled": False},
              "checkpoints": {"enabled": False}, "memory": {"memory_review_round": 0},
              "skills": {"evolution_review_round": 0}}
    (home / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    (home / ".env").write_text(f"MCLAW_BASE_URL=http://127.0.0.1:{port}/v1\nMCLAW_API_KEY=local-test-credential\n", encoding="utf-8")
    events = []

    class ObservedRunner(ProcessRunner):
        async def run(self, task, emit):
            def observe(event):
                events.append(event)
                emit(event)
                if event.get("type") == "request":
                    self.send({"type": "reply", "request_id": event["request"]["id"], "value": False})
            return await super().run(task, observe)

    sessions = SessionService(home, runner_factory=lambda session: ObservedRunner(home, session))
    schedules = ScheduleService(home, sessions, config=config, tick_interval=0.05)

    async def completed_run(identifier=None):
        async def wait():
            while True:
                rows = schedules.store.list_runs(job["id"])
                row = next((row for row in rows if not identifier or row.id == identifier), None)
                if row is not None and row.status not in {"running", "queued"}:
                    return row
                await asyncio.sleep(0.02)
        return await asyncio.wait_for(wait(), 25)

    await schedules.start()
    try:
        job = schedules.create({"name": "本地定时验证", "prompt": "回复测试消息", "session_policy": policy,
                                "timeout_seconds": 20, "schedule": {"trigger_type": "daily", "time": "09:00",
                                                                        "timezone": "Asia/Shanghai"}})
        schedules.store.update_job(job["id"], {"next_run_at": time.time() - 1})
        first = await completed_run()
        assert first.status == "succeeded", first.error
        second = await completed_run((await schedules.run_now(job["id"]))["id"])
        assert second.status == "succeeded", second.error
        assert (first.session_id == second.session_id) is (policy == "task_thread")
        for run in (first, second):
            snapshot = sessions.snapshot(run.session_id)
            assert snapshot["source"] == "scheduler"
            assert Path(snapshot["workspace"]).parent == home / "_temp"
            assert run.final_response == "本地测试回复"
            assert Path(run.output_path).is_file()
        assert not [event for event in events if event.get("type") == "request"]
        assert len(requests) == 2
        assert len(sessions.list()) == (1 if policy == "task_thread" else 2)
    finally:
        await schedules.close()
        await sessions.close()


@pytest.mark.asyncio
async def test_real_worker_uses_accepted_skill_snapshot_and_preserves_it_next_turn(local_provider, tmp_path, monkeypatch):
    from mclaw.application.skills import SkillService
    import yaml

    port, requests = local_provider
    home, workspace = tmp_path / "state", tmp_path / "work"
    home.mkdir()
    workspace.mkdir()
    monkeypatch.setenv("MCLAW_HOME", str(home))
    (home / "config.yaml").write_text(yaml.safe_dump({
        "active_provider": "custom", "model": "test-model", "compression": {"enabled": False},
        "checkpoints": {"enabled": False}, "memory": {"memory_review_round": 0},
        "skills": {"evolution_review_round": 0}}), encoding="utf-8")
    (home / ".env").write_text(f"MCLAW_BASE_URL=http://127.0.0.1:{port}/v1\nMCLAW_API_KEY=local-test-credential\n", encoding="utf-8")
    skills = SkillService(home)
    body = "---\nname: snapshot-example\ndescription: Example\n---\n\nFrozen accepted guidance.\n"
    original = skills.create("snapshot-example", "示例技能", body)
    selected = skills.snapshots(["snapshot-example"])
    skills.update("snapshot-example", {"content": body.replace("Frozen accepted", "Unselected later"), "version": original["version"]})
    runner = ProcessRunner(home, {"id": "skill-stream-test", "workspace": str(workspace)})
    def emit(event):
        if event["type"] == "request":
            runner.send({"type": "reply", "request_id": event["request"]["id"], "value": True})
    try:
        first = await asyncio.wait_for(runner.run(_task(skill_snapshots=selected), emit), 30)
        assert first["completed"]
        second = await asyncio.wait_for(runner.run(_task(id="next", attempts=[{"id": "next"}], skill_snapshots=selected,
                                                        execution={"platform": "scheduler", "max_iterations": 3}), emit), 30)
        assert second["completed"]
    finally:
        await asyncio.wait_for(runner.close(), 20)
    assert len(requests) == 2
    for request in requests:
        context = "\n".join(str(message.get("content") or "") for message in request["messages"])
        assert context.count("[Skill: snapshot-example]") == 1
        assert "Frozen accepted guidance." in context
        assert "Unselected later guidance." not in context


@pytest.mark.asyncio
async def test_real_channel_worker_skips_allocated_directory_prompt_and_refreshes_tool_switches(local_provider, tmp_path, monkeypatch):
    from mclaw.application.sessions import SessionService
    from mclaw.application.tools import ToolService
    from mclaw.cli.config import save_config, save_env_value

    port, requests = local_provider
    monkeypatch.setenv("MCLAW_HOME", str(tmp_path))
    save_config({"active_provider": "custom", "model": "test-model", "compression": {"enabled": False},
                 "checkpoints": {"enabled": False}, "memory": {"memory_review_round": 0},
                 "skills": {"evolution_review_round": 0}, "toolsets": ["mclaw-required", "web"],
                 "tools": {"disabled": ["web_search", "web_extract"]},
                 "channels": {"weixin": {"enabled": False, "toolsets": ["mclaw-required", "web"]}}})
    save_env_value("MCLAW_BASE_URL", f"http://127.0.0.1:{port}/v1")
    save_env_value("MCLAW_API_KEY", "local-test-credential")
    sessions = SessionService(tmp_path)
    sessions.history.create_session("channel-stream-test", source="weixin")
    sessions.prepare_background_session("channel-stream-test")
    runner = ProcessRunner(tmp_path, sessions.store.get("channel-stream-test"))
    events = []
    def emit(event):
        events.append(event)
        if event["type"] == "request":
            # A regression must fail promptly rather than hang the real worker.
            runner.send({"type": "reply", "request_id": event["request"]["id"], "value": False})
    tools = ToolService(tmp_path)
    try:
        for index, enabled in enumerate((False, True, False)):
            if index:
                tools.save("web_extract", {"enabled": enabled, "revision": tools.catalog()["revision"]})
            result = await asyncio.wait_for(runner.run(_task(id=f"turn-{index}", attempts=[{"id": f"attempt-{index}"}],
                execution={"platform": "weixin", "channel_toolsets": True, "enabled_toolsets": ["mclaw-required", "web"]}), emit), 30)
            assert result["completed"], result
            exposed = {tool["function"]["name"] for tool in requests[-1].get("tools", [])}
            assert ("web_extract" in exposed) == enabled
            if index == 0:
                pid = runner.process.pid
            else:
                assert runner.process.pid == pid
        assert not any(event["type"] == "request" for event in events)
        assert sessions.history.get_session("channel-stream-test")["title"] == "微信消息1"
    finally:
        await asyncio.wait_for(runner.close(), 20)
        await sessions.close()


@pytest.mark.asyncio
async def test_real_worker_attachment_paths_are_persisted_once_on_continuation(local_provider, tmp_path, monkeypatch):
    from mclaw.application.files import FileService

    port, requests = local_provider
    home, workspace = tmp_path / "state", tmp_path / "work"
    home.mkdir()
    workspace.mkdir()
    monkeypatch.setenv("MCLAW_HOME", str(home))
    (home / "config.yaml").write_text(
        "active_provider: custom\nmodel: test-model\ncompression:\n  enabled: false\ncheckpoints:\n  enabled: false\nmemory:\n  memory_review_round: 0\nskills:\n  evolution_review_round: 0\n", encoding="utf-8")
    (home / ".env").write_text(f"MCLAW_BASE_URL=http://127.0.0.1:{port}/v1\nMCLAW_API_KEY=local-test-credential\n", encoding="utf-8")
    files = FileService(home)
    attached = files.upload("file-stream-test", "材料.txt", "资料正文".encode())
    runner = ProcessRunner(home, {"id": "file-stream-test", "workspace": str(workspace)})
    def emit(event):
        if event["type"] == "request":
            runner.send({"type": "reply", "request_id": event["request"]["id"], "value": True})
    try:
        first = await asyncio.wait_for(runner.run(_task(attachments=[attached]), emit), 30)
        assert first["completed"]
        continued = await asyncio.wait_for(runner.run(_task(attachments=[attached], attempts=[{"id": "first"}, {"id": "resume"}]), emit), 30)
        assert continued["completed"]
    finally:
        await asyncio.wait_for(runner.close(), 20)
    for request in requests:
        context = "\n".join(str(message.get("content") or "") for message in request["messages"])
        assert context.count("[Attached files]") == 1
        assert "材料.txt" in context
    db = SessionDB(home / "state.db")
    rows = [row for row in db.get_messages("file-stream-test") if row.get("operation_id") == "task:task-one"]
    assert len(rows) == 1 and "材料.txt" in rows[0]["content"]
    db.close()
