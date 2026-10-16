# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from mclaw.application.sessions import ApplicationError, SessionService
from mclaw.application.store import ApplicationStore, backup_history
from mclaw.cli.runtime.session_lock import InteractiveSessionLock, InteractiveSessionLockError
from mclaw.state import SessionDB


class ControlledRunner:
    def __init__(self, session):
        self.session = session
        self.calls = []
        self.commands = []
        self.results = asyncio.Queue()
        self.emit = None
        self.closed = False

    async def run(self, task, emit):
        self.calls.append(task)
        self.emit = emit
        return await self.results.get()

    def send(self, command):
        self.commands.append(command)

    async def close(self):
        self.closed = True


@pytest.fixture
def service(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("MCLAW_HOME", str(home))
    history = SessionDB(home / "state.db")
    result = SessionService(home, history=history, runner_factory=ControlledRunner)
    output = tmp_path / "outputs"
    output.mkdir()
    result.store.set_setting("output_root", str(output))
    return result


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_delete_removes_history_and_snapshot_but_preserves_files(service, legacy):
    sid = "legacy" if legacy else service.create("browser")["id"]
    service.history.create_session(sid, source="cli" if legacy else "web")
    service.history.append_message(sid, "user", "delete this history")
    other = service.create("other")["id"]
    output = Path(service.output_root()) / "result.txt"
    output.write_text("keep this file", encoding="utf-8")
    assert await service.delete(sid, "browser") == {"deleted": True, "id": sid}
    assert service.history.get_session(sid) is None
    assert service.history.get_messages(sid) == []
    assert service.store.get(sid) is None
    assert [row["id"] for row in service.list()] == [other]
    assert output.read_text(encoding="utf-8") == "keep this file"
    assert sid not in service.owners and sid not in service.locks
    with pytest.raises(ApplicationError) as failure:
        service.snapshot(sid)
    assert failure.value.code == "SESSION_NOT_FOUND"
    home = service.home
    await service.close()
    restored = SessionService(home)
    assert [row["id"] for row in restored.list()] == [other]
    await restored.close()


@pytest.mark.asyncio
async def test_delete_respects_browser_and_terminal_ownership(service):
    sid = service.create("a")["id"]
    with pytest.raises(ApplicationError) as failure:
        await service.delete(sid, "b")
    assert failure.value.code == "SESSION_OCCUPIED"
    service.release(sid, "a")
    lock = InteractiveSessionLock(sid, service.home / "sessions" / "locks")
    lock.acquire()
    try:
        with pytest.raises(ApplicationError) as failure:
            await service.delete(sid, "b")
        assert failure.value.code == "SESSION_OCCUPIED"
        assert service.store.get(sid)
    finally:
        lock.release()
        await service.close()


@pytest.mark.asyncio
async def test_delete_requires_task_drain_then_discards_paused_queue(service):
    sid = service.create("a")["id"]
    service.submit(sid, "a", "first", "first")
    service.submit(sid, "a", "queued", "second")
    await settle()
    runner = service.runners[sid]
    with pytest.raises(ApplicationError) as failure:
        await service.delete(sid, "a")
    assert failure.value.code == "SESSION_BUSY"
    assert not runner.closed
    service.stop(sid, "a")
    with pytest.raises(ApplicationError) as failure:
        await service.delete(sid, "a")
    assert failure.value.code == "SESSION_BUSY"
    runner.results.put_nowait({"interrupted": True})
    await settle()
    assert service.snapshot(sid)["queue"]
    await service.delete(sid, "a")
    assert runner.closed and sid not in service.runners
    assert not service.list()
    await service.close()


@pytest.mark.asyncio
async def test_delete_fences_new_work_until_worker_exits(service):
    sid = service.create("a")["id"]
    entered, finish = asyncio.Event(), asyncio.Event()
    class ClosingRunner:
        async def close(self):
            entered.set()
            await finish.wait()
    service.runners[sid] = ClosingRunner()
    deletion = asyncio.create_task(service.delete(sid, "a"))
    await entered.wait()
    assert service.store.get(sid)
    assert service.task_result(sid, "pending") is None
    for action in (lambda: service.submit(sid, "a", "new", "new"),
                   lambda: service.submit_background(sid, "new", "new"),
                   lambda: service.patch(sid, "a", {"title": "new"})):
        with pytest.raises(ApplicationError) as failure:
            action()
        assert failure.value.code == "SESSION_DELETING"
    finish.set()
    await deletion
    assert not service.deletions
    assert service.task_result(sid, "pending")["cancelled"]
    await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["weixin", "dingtalk"])
async def test_channel_message_after_deletion_gets_new_session(service, platform):
    from mclaw.application.channel_runner import PersistentChannelRouter
    from mclaw.channels.base import ChannelSource
    from mclaw.channels.weixin.session_router import WeixinSessionRouter
    from mclaw.channels.dingtalk.session_router import DingTalkSessionRouter

    router_class = WeixinSessionRouter if platform == "weixin" else DingTalkSessionRouter
    router = PersistentChannelRouter(router_class(account_id="test", session_db=service.history), service.store, platform)
    source = ChannelSource(platform, "chat", account_id="test", user_id="user")
    sid = router.route(source).session_id
    service.history.append_message(sid, "user", "old conversation")
    service.store.set_setting(f"channel_pending:{platform}", {
        "old": {"session_id": sid, "text": "old conversation"},
        "other": {"session_id": "another", "text": "keep"},
    })
    await service.delete(sid, "browser")
    fresh = router.route(source).session_id
    assert fresh != sid
    assert service.history.get_session(sid) is None
    assert service.history.get_messages(fresh) == []
    assert service.store.setting(f"channel_pending:{platform}") == {"other": {"session_id": "another", "text": "keep"}}
    restored = PersistentChannelRouter(router_class(account_id="test", session_db=service.history), service.store, platform)
    assert restored.route(source).session_id == fresh
    await service.close()


@pytest.mark.asyncio
async def test_skill_references_freeze_on_accept_and_queue_selection_change(service):
    from mclaw.application.skills import SkillService
    skills = SkillService(service.home)
    def content(body):
        return "---\nname: demo\ndescription: Example\n---\n\n" + body
    original = skills.create("demo", "示例技能", content("Original guidance."))
    sid = service.create("a")["id"]
    service.stop(sid, "a")
    snapshot = service.submit(sid, "a", "queued task", "one", skills=["demo"])
    queued = snapshot["queue"][0]
    assert queued["skills"][0]["version"] == original["version"]
    assert "skill_snapshots" not in queued
    changed = skills.update("demo", {"content": content("Changed guidance."), "version": original["version"]})
    assert "Original guidance" in service._load(sid)["queue"][0]["skill_snapshots"][0]["content"]
    service.edit_queue(sid, "a", [{"id": queued["id"], "content": "edited text"}])
    assert service.snapshot(sid)["queue"][0]["skills"][0]["version"] == original["version"]
    service.edit_queue(sid, "a", [{"id": queued["id"], "content": "edited text", "skills": ["demo"]}])
    assert service.snapshot(sid)["queue"][0]["skills"][0]["version"] == changed["version"]
    service.edit_queue(sid, "a", [{"id": queued["id"], "content": "edited text", "skills": []}])
    assert service._load(sid)["queue"][0]["skill_snapshots"] == []
    await service.close()


@pytest.mark.asyncio
async def test_background_queue_preserves_browser_lease_and_precise_cancel(service):
    sid = service.create("browser", model="first")["id"]
    service.submit(sid, "browser", "interactive", "interactive")
    await settle()
    runner = service.runners[sid]
    accepted = service.submit_background(sid, "scheduled", "job-run", {"platform": "scheduler", "max_iterations": 5})
    assert service.owners[sid][0] == "browser"
    assert service.submit_background(sid, "scheduled", "job-run")["task_id"] == accepted["task_id"]
    service.update_background_model(sid, "second")
    assert runner.calls[0]["model"] == "first"
    assert service._load(sid)["queue"][0]["model"] == "second"
    assert "execution" not in service.snapshot(sid)["queue"][0]
    service.cancel_background(sid, accepted["task_id"])
    assert service.task_result(sid, accepted["task_id"])["status"] == "interrupted"
    assert not runner.commands
    runner.results.put_nowait({"completed": True, "final_response": "finished"})
    await settle()
    assert len(runner.calls) == 1
    await service.close()


@pytest.mark.asyncio
async def test_background_task_result_and_active_cancellation(service):
    sid = service.create("browser")["id"]
    task = service.submit_background(sid, "scheduled", "run", {"platform": "scheduler", "extra_system": "Background task"})
    await settle()
    runner = service.runners[sid]
    assert runner.calls[0]["execution"]["extra_system"] == "Background task"
    assert service.task_result(sid, task["task_id"]) is None
    service.cancel_background(sid, task["task_id"])
    assert runner.commands == [{"type": "stop"}]
    runner.results.put_nowait({"completed": False, "interrupted": True})
    await settle()
    assert service.task_result(sid, task["task_id"])["status"] == "interrupted"
    assert service.snapshot(sid)["active_task"]["id"] == task["task_id"]
    assert service.owners[sid][0] == "browser"
    await service.close()


@pytest.mark.asyncio
async def test_queue_idempotency_and_model_binding(service):
    session = service.create("window-a", model="old")
    sid = session["id"]
    service.submit(sid, "window-a", "first", "request-1")
    await settle()
    runner = service.runners[sid]
    service.submit(sid, "window-a", "second", "request-2")
    service.submit(sid, "window-a", "second", "request-2")
    assert len(service.snapshot(sid)["queue"]) == 1
    service.patch(sid, "window-a", {"model": "new"})
    assert runner.calls[0]["model"] == "old"
    assert service.snapshot(sid)["queue"][0]["model"] == "new"
    runner.emit({"type": "status", "text": "Thinking..."})
    runner.results.put_nowait({"completed": True, "final_response": "one"})
    await settle()
    assert [task["content"] for task in runner.calls] == ["first", "second"]
    assert not service.snapshot(sid)["status_text"]
    runner.emit({"type": "status", "text": "Running a tool..."})
    runner.results.put_nowait({"completed": True, "final_response": "two"})
    await settle()
    assert service.snapshot(sid)["status"] == "completed"
    assert service.snapshot(sid)["messages"][-1]["content"] == "two"
    assert not service.snapshot(sid)["status_text"]
    await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("result,state", [({"error": "invalid model"}, "failed"), ({"interrupted": True}, "interrupted")])
async def test_failed_or_stopped_task_retained_and_resumed_before_queue(service, result, state):
    sid = service.create("a")["id"]
    service.submit(sid, "a", "original", "one")
    service.submit(sid, "a", "queued", "two")
    await settle()
    runner = service.runners[sid]
    task_id = runner.calls[0]["id"]
    runner.emit({"type": "status", "text": "Thinking..."})
    runner.results.put_nowait(result)
    await settle()
    snapshot = service.snapshot(sid)
    assert snapshot["status"] == state
    assert not snapshot["status_text"]
    assert snapshot["paused"] and len(snapshot["queue"]) == 1
    assert len(runner.calls) == 1
    service.resume(sid, "a")
    await settle()
    assert runner.calls[1]["id"] == task_id
    assert len(runner.calls[1]["attempts"]) == 2
    runner.results.put_nowait({"completed": True})
    await settle()
    assert runner.calls[2]["content"] == "queued"
    runner.results.put_nowait({"completed": True})
    await settle()
    await service.close()


@pytest.mark.asyncio
async def test_stop_waits_for_cleanup_and_does_not_stop_another_session(service):
    a = service.create("a")["id"]
    b = service.create("a")["id"]
    service.submit(a, "a", "A", "a")
    service.submit(b, "a", "B", "b")
    await settle()
    service.stop(a, "a")
    assert service.snapshot(a)["status"] == "stopping"
    with pytest.raises(ApplicationError, match="收尾"):
        service.resume(a, "a")
    assert service.snapshot(b)["status"] == "running"
    assert not service.runners[b].commands
    service.runners[a].results.put_nowait({"interrupted": True})
    service.runners[b].results.put_nowait({"completed": True})
    await settle()
    assert service.snapshot(a)["status"] == "interrupted"
    await service.close()


@pytest.mark.asyncio
async def test_detach_reconnect_and_cross_frontend_ownership(service):
    sid = service.create("a")["id"]
    service.submit(sid, "a", "work", "id")
    await settle()
    with pytest.raises(ApplicationError) as failure:
        service.snapshot(sid, "b")
    assert failure.value.code == "SESSION_OCCUPIED"
    lock = InteractiveSessionLock(sid, service.home / "sessions" / "locks")
    with pytest.raises(InteractiveSessionLockError):
        lock.acquire()
    service.release(sid, "a")
    assert service.snapshot(sid)["status"] == "running"
    assert service.snapshot(sid, "b")["id"] == sid
    assert not service.runners[sid].commands
    service.runners[sid].emit({"type": "delta", "text": "continued"})
    assert service.snapshot(sid, "b")["messages"][-1]["content"] == "continued"
    service.runners[sid].results.put_nowait({"completed": True})
    await settle()
    await service.close()
    lock.acquire()
    lock.release()


@pytest.mark.asyncio
async def test_default_workspaces_are_distinct_and_bound(service, tmp_path):
    a, b = service.create("a")["id"], service.create("a")["id"]
    service.submit(a, "a", "a", "1")
    service.submit(b, "a", "b", "1")
    await settle()
    sa, sb = service.snapshot(a), service.snapshot(b)
    assert sa["workspace"] != sb["workspace"]
    assert not sa["workspace_explicit"]
    with pytest.raises(ApplicationError, match="原目录"):
        service.patch(a, "a", {"workspace": str(tmp_path)})
    for runner in service.runners.values():
        runner.results.put_nowait({"completed": True})
    await settle()
    await service.close()


@pytest.mark.asyncio
async def test_managed_task_directory_is_lazy_and_preserves_existing_bindings(service):
    old = service._load(service.create("a")["id"])
    service._bind_workspace(old)
    service.store.save(old)
    previous = Path(old["workspace"])
    marker = previous / "keep.txt"
    marker.write_text("existing task", encoding="utf-8")

    service.store.set_setting("output_root", None)
    root = service.home / "_temp"
    assert service.output_root() == str(root)
    assert not root.exists()
    fresh = [service._load(service.create("a")["id"]) for _ in range(2)]
    for session in fresh:
        service._bind_workspace(session)
        assert Path(session["workspace"]).parent == root
        assert Path(session["workspace"]).is_dir()
    assert fresh[0]["workspace"] != fresh[1]["workspace"]
    restored = service._load(old["id"])
    service._bind_workspace(restored)
    assert restored["workspace"] == str(previous)
    assert marker.read_text(encoding="utf-8") == "existing task"
    await service.close()


@pytest.mark.asyncio
async def test_saved_task_root_is_preserved_including_the_former_default(service, tmp_path):
    chosen = tmp_path / "M-Claw成果"
    chosen.mkdir()
    assert service.set_output_root(str(chosen)) == str(chosen)
    assert service.output_root() == str(chosen)
    session = service._load(service.create("a")["id"])
    service._bind_workspace(session)
    assert Path(session["workspace"]).parent == chosen
    await service.close()


@pytest.mark.asyncio
async def test_queue_edits_reorder_and_remove_only_pending(service):
    sid = service.create("a")["id"]
    for n in range(3):
        service.submit(sid, "a", str(n), str(n))
    await settle()
    snapshot = service.snapshot(sid)
    first, second = snapshot["queue"]
    service.edit_queue(sid, "a", [{"id": second["id"], "content": "edited"}])
    with pytest.raises(ApplicationError):
        service.edit_queue(sid, "a", [{"id": first["id"], "content": "stale"}])
    runner = service.runners[sid]
    runner.results.put_nowait({"completed": True})
    await settle()
    assert runner.calls[-1]["content"] == "edited"
    runner.results.put_nowait({"completed": True})
    await settle()
    await service.close()


@pytest.mark.asyncio
async def test_secret_reply_is_not_saved_and_is_bound_to_request(service):
    sid = service.create("a")["id"]
    service.submit(sid, "a", "work", "1")
    await settle()
    runner = service.runners[sid]
    runner.emit({"type": "request", "request": {"id": "auth-1", "kind": "secret", "title": "Authorize"}})
    assert service.snapshot(sid)["status"] == "waiting"
    with pytest.raises(ApplicationError):
        service.reply(sid, "a", "wrong-id", {"values": {"KEY": "private-test-value"}})
    service.reply(sid, "a", "auth-1", {"values": {"KEY": "private-test-value"}})
    assert "private-test-value" not in str(service.store.get(sid))
    with pytest.raises(ApplicationError):
        service.reply(sid, "a", "auth-1", {})
    runner.results.put_nowait({"completed": True})
    await settle()
    await service.close()


@pytest.mark.asyncio
async def test_unapplied_supplement_is_retained_and_pauses_queue(service):
    sid = service.create("a")["id"]
    service.submit(sid, "a", "work", "1")
    await settle()
    runner = service.runners[sid]
    service.submit(sid, "a", "extra context", "2", "steer")
    assert runner.commands[-1]["type"] == "steer"
    runner.results.put_nowait({"completed": True})
    await settle()
    assert service.snapshot(sid)["paused"]
    assert not service.snapshot(sid)["active_task"]["supplements"][0]["applied"]
    await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("resume", [False, True])
async def test_applied_supplement_remains_visible_after_completion(service, resume):
    sid = service.create("a")["id"]
    service.submit(sid, "a", "work", "1")
    await settle()
    runner = service.runners[sid]
    if resume:
        runner.results.put_nowait({"interrupted": True})
        await settle()
        service.resume(sid, "a", content="extra facts", request_id="resume")
        await settle()
    else:
        service.submit(sid, "a", "extra facts", "supplement", "steer")
    snapshot = service.snapshot(sid)
    addition = next(item for item in snapshot["messages"] if item.get("mode") == "steer")
    assert addition["content"] == "extra facts" and addition["applied"] is False
    runner.emit({"type": "supplement_applied", "id": addition["id"]})
    runner.results.put_nowait({"completed": True})
    await settle()
    snapshot = service.snapshot(sid)
    assert snapshot["active_task"] is None
    addition = next(item for item in snapshot["messages"] if item.get("mode") == "steer")
    assert addition["content"] == "extra facts" and addition["applied"] is True
    await service.close()


@pytest.mark.asyncio
async def test_runner_construction_failure_pauses_original_and_preserves_queue(service):
    def fail(_session):
        raise OSError("worker startup failure")
    service.runner_factory = fail
    sid = service.create("a")["id"]
    service.submit(sid, "a", "original", "1")
    service.submit(sid, "a", "queued", "2")
    await settle()
    snapshot = service.snapshot(sid)
    assert snapshot["status"] == "failed" and snapshot["paused"]
    assert snapshot["active_task"]["content"] == "original"
    assert [task["content"] for task in snapshot["queue"]] == ["queued"]
    assert sid not in service.jobs
    await service.close()


@pytest.mark.asyncio
async def test_tool_round_stream_moves_to_process_and_next_stream_starts_clean(service):
    sid = service.create("a")["id"]
    service.submit(sid, "a", "work", "1")
    await settle()
    runner = service.runners[sid]
    runner.emit({"type": "delta", "text": "preparing a tool"})
    runner.emit({"type": "body_reset"})
    runner.emit({"type": "progress", "title": "执行进展", "text": "preparing a tool"})
    runner.emit({"type": "delta", "text": "final answer"})
    message = service.snapshot(sid)["messages"][-1]
    assert message["content"] == "final answer"
    assert message["process"][-1]["content"] == "preparing a tool"
    runner.results.put_nowait({"completed": True, "final_response": "final answer"})
    await settle()
    await service.close()


@pytest.mark.asyncio
async def test_resolved_request_does_not_clear_a_newer_request_or_stop(service):
    sid = service.create("a")["id"]
    service.submit(sid, "a", "work", "1")
    await settle()
    runner = service.runners[sid]
    runner.emit({"type": "request", "request": {"id": "new", "kind": "workspace"}})
    runner.emit({"type": "request_resolved", "request_id": "old"})
    assert service.snapshot(sid)["pending_request"]["id"] == "new"
    service.stop(sid, "a")
    runner.emit({"type": "request_resolved", "request_id": "new"})
    assert service.snapshot(sid)["pending_request"] is None
    assert service.snapshot(sid)["status"] == "stopping"
    runner.results.put_nowait({"interrupted": True})
    await settle()
    await service.close()


@pytest.mark.asyncio
async def test_restart_keeps_queue_and_never_reexecutes(service):
    sid = service.create("a")["id"]
    data = service.store.get(sid)
    data.update(status="running", active_task={"id": "original", "status": "running"}, queue=[{"id": "later"}])
    service.store.save(data)
    await service.close()
    restored = SessionService(service.home, runner_factory=ControlledRunner)
    snapshot = restored.snapshot(sid)
    assert snapshot["status"] == "interrupted" and snapshot["paused"]
    assert snapshot["active_task"]["recovery_required"]
    assert snapshot["queue"] == [{"id": "later"}]
    assert not restored.jobs
    await restored.close()


@pytest.mark.asyncio
async def test_legacy_history_and_provider_snapshot_preserved(service, tmp_path):
    service.history.create_session("legacy", "cli", model="old-model", workspace=str(tmp_path))
    service.history.append_message("legacy", "user", content="past context")
    service.history.append_message("legacy", "assistant", content="past reply")
    service.history.set_session_title("legacy", "旧标题")
    service.history.update_model_config("legacy", model="old-model", model_config={"provider": "example", "model": "old-model"})
    assert service.list()[0]["title"] == "旧标题"
    snapshot = service.snapshot("legacy", "a")
    assert snapshot["messages"][0]["content"] == "past context"
    assert snapshot["workspace"] == str(tmp_path)
    assert service.store.get("legacy")["provider_snapshot"]["model"] == "old-model"
    assert service.history.get_messages_as_conversation("legacy")[0]["content"] == "past context"
    await service.close()


def test_backup_is_consistent_and_does_not_overwrite_previous_copy(tmp_path):
    history = SessionDB(tmp_path / "state.db")
    history.create_session("old", "cli")
    backup = backup_history(tmp_path)
    history.create_session("new", "cli")
    assert backup_history(tmp_path) == backup
    with sqlite3.connect(backup) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("SELECT id FROM sessions").fetchall() == [("old",)]
    history.close()


def test_future_sidecar_schema_is_rejected(tmp_path):
    path = tmp_path / "application.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version=999")
    with pytest.raises(RuntimeError, match="更新版本"):
        ApplicationStore(path)
