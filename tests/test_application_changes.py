# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from mclaw.application.changes import ChangeService
from mclaw.application.runner import ProcessRunner
from mclaw.application.sessions import ApplicationError, SessionService
from mclaw.safety.operation_journal import OperationJournal
from mclaw.safety.rollback_coordinator import RollbackCoordinator
from mclaw.state import SessionDB
from mclaw.tools import checkpoint_manager


@pytest.fixture
def changes(tmp_path, monkeypatch):
    from mclaw.runtime.manager import RuntimeManager
    from mclaw.runtime.paths import PathPolicy

    home, workspace = tmp_path / "home", tmp_path / "work"
    home.mkdir()
    workspace.mkdir()
    monkeypatch.setenv("MCLAW_HOME", str(home))
    monkeypatch.setattr(checkpoint_manager, "CHECKPOINT_BASE", home / "checkpoints")
    policy = PathPolicy(mclaw_home=home)
    monkeypatch.setattr(RuntimeManager, "current", lambda *_args, **_kwargs: SimpleNamespace(paths=policy))
    if not checkpoint_manager.shutil.which("git"):
        pytest.skip("Git is required for real file recovery")
    manager = checkpoint_manager.CheckpointManager(enabled=True, max_snapshots=100)
    db = SessionDB(home / "state.db")
    db.create_session(session_id="session", source="test", workspace=str(workspace))
    db.append_message("session", "system", content="base")
    agent = SimpleNamespace(session_id="session", messages=[], _recalled_memory="old memory")
    journal = OperationJournal(home / "operations")
    coordinator = RollbackCoordinator(checkpoint_manager=manager, session_db=db, agent=agent, journal=journal)
    service = ChangeService(home, {"id": "session", "workspace": str(workspace)}, coordinator)
    service.target = workspace / "note.txt"
    service.target.write_text("original", encoding="utf-8")
    yield service
    db.close()


def record(service, turn="turn-one", content="modified"):
    db, manager, journal = service.coordinator.session_db, service.coordinator.checkpoint_manager, service.coordinator.journal
    marker = db.get_messages("session")[-1]["id"]
    db.append_message("session", "user", content="please edit", turn_id=turn)
    assert manager.create_checkpoint(service.workspace, "before " + turn, target_paths=[str(service.target)],
                                     metadata={"session_id": "session", "turn_id": turn, "message_id_before_turn": marker})
    op = journal.begin(session_id="session", turn_id=turn, message_id_before_turn=marker, messages_len_before_turn=1,
                       tool_call_id="tool-" + turn, tool_name="write_file", action="overwrite", cwd=service.workspace,
                       workspace=service.workspace, targets=[str(service.target)], checkpoint_commit=manager.last_attempt["commit"])
    service.target.write_text(content, encoding="utf-8")
    journal.finalize(op, success=True)
    db.append_message("session", "assistant", content="edited " + turn, turn_id=turn, operation_id=op["operation_id"])
    service.coordinator.agent.messages = db.get_messages_as_conversation("session")
    return op


def test_latest_turn_undo_and_confirmed_restore_refresh_context(changes):
    record(changes)
    preview = changes.preview({"group_id": "turn-one", "action": "undo"})
    assert "+original" in preview["diff"].splitlines()
    assert "-modified" in preview["diff"].splitlines()
    # Existing TUI/checkpoint callers retain the checkpoint-to-current view.
    legacy_diff = changes.coordinator.checkpoint_manager.diff(changes.workspace, changes.list()["checkpoints"][0]["hash"])
    assert "+modified" in legacy_diff["diff"].splitlines()
    assert "-original" in legacy_diff["diff"].splitlines()
    listed = changes.list()
    assert listed["groups"][0]["direct_undo"] is True
    assert listed["groups"][0]["files"][0]["change"] == "modified"
    result = changes.apply({"group_id": "turn-one", "action": "undo"})
    assert result["success"] and result["context"]["invalidated"] > 0
    assert changes.target.read_text() == "original"
    assert not any(item.get("content") == "edited turn-one" for item in changes.coordinator.agent.messages)
    assert changes.coordinator.agent._recalled_memory == ""
    with pytest.raises(ApplicationError, match="先预览"):
        changes.apply({"group_id": "turn-one", "action": "restore"})
    preview = changes.preview({"group_id": "turn-one", "action": "restore"})
    assert preview["context_impact"]["scope"] == "turn"
    assert "+modified" in preview["diff"].splitlines()
    assert "-original" in preview["diff"].splitlines()
    result = changes.apply({"group_id": "turn-one", "action": "restore", "confirmation_token": preview["confirmation_token"]})
    assert result["context"]["restored"] > 0
    assert changes.target.read_text() == "modified"
    assert any(item.get("content") == "edited turn-one" for item in changes.coordinator.agent.messages)


def test_old_turn_and_external_conflict_require_bound_preview(changes):
    record(changes)
    record(changes, "turn-two", "later")
    with pytest.raises(ApplicationError) as exc:
        changes.apply({"group_id": "turn-one", "action": "undo"})
    assert exc.value.code == "CONFIRMATION_REQUIRED"
    preview = changes.preview({"group_id": "turn-one", "action": "undo"})
    assert preview["conflicts"] == [str(changes.target)]
    assert "later" in preview["diff"]
    changes.target.write_text("edited after preview", encoding="utf-8")
    with pytest.raises(ApplicationError) as exc:
        changes.apply({"group_id": "turn-one", "action": "undo", "confirmation_token": preview["confirmation_token"]})
    assert exc.value.code == "PREVIEW_EXPIRED"
    assert changes.target.read_text() == "edited after preview"
    preview = changes.preview({"group_id": "turn-one", "action": "undo"})
    changes.apply({"group_id": "turn-one", "action": "undo", "confirmation_token": preview["confirmation_token"]})
    assert changes.target.read_text() == "original"
    backups = list(changes.target.parent.glob("*.mclaw-conflict-*"))
    assert backups and backups[0].read_text() == "edited after preview"
    assert any(row.get("content") == "edited turn-two" for row in changes.coordinator.session_db.get_messages("session"))


def test_checkpoint_restore_requires_preview_and_refreshes_tail(changes):
    record(changes)
    checkpoint = changes.list()["checkpoints"][0]
    request = {"action": "checkpoint", "checkpoint_hash": checkpoint["hash"]}
    with pytest.raises(ApplicationError) as exc:
        changes.apply(request)
    assert exc.value.code == "CONFIRMATION_REQUIRED"
    preview = changes.preview(request)
    assert preview["context_impact"]["scope"] == "tail"
    assert "+original" in preview["diff"].splitlines()
    assert "-modified" in preview["diff"].splitlines()
    result = changes.apply({**request, "confirmation_token": preview["confirmation_token"]})
    assert result["context"]["invalidated"] > 0
    assert changes.target.read_text() == "original"


def test_checkpoint_context_failure_compensates_files(changes, monkeypatch):
    record(changes)
    request = {"action": "checkpoint", "checkpoint_hash": changes.list()["checkpoints"][0]["hash"]}
    preview = changes.preview(request)
    monkeypatch.setattr(changes.coordinator.context, "apply", lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("injected DB error")))
    with pytest.raises(ApplicationError) as exc:
        changes.apply({**request, "confirmation_token": preview["confirmation_token"]})
    assert exc.value.code == "RECOVERY_FAILED"
    assert changes.target.read_text() == "modified"


def test_interrupted_restore_is_previewed_and_recovered(changes):
    record(changes)
    manager = changes.coordinator.checkpoint_manager
    before = changes.list()["checkpoints"][0]["hash"]
    intent = manager.begin_restore_intent(changes.workspace, target_commits=[before], target_paths=[str(changes.target)], kind="rollback-turn")
    assert intent["success"]
    changes.target.write_text("partially restored", encoding="utf-8")
    manager.finish_restore_intent(intent["intent_id"], "interrupted")
    assert changes.list()["recoveries"][0]["id"] == intent["intent_id"]
    request = {"action": "recover", "intent_id": intent["intent_id"]}
    preview = changes.preview(request)
    assert preview["files"][0]["path"] == str(changes.target)
    assert "+modified" in preview["diff"].splitlines()
    assert "-partially restored" in preview["diff"].splitlines()
    changes.apply({**request, "confirmation_token": preview["confirmation_token"]})
    assert changes.target.read_text() == "modified"
    assert changes.list()["recoveries"] == []
    cleared = changes.preview({"action": "clear"})
    assert cleared["checkpoint_maintenance"]["blocked_by_recovery"] is False
    assert changes.apply({"action": "clear", "confirmation_token": cleared["confirmation_token"]})["success"]


def test_checkpoint_maintenance_is_scoped_and_confirmation_bound(changes):
    manager = changes.coordinator.checkpoint_manager
    record(changes)
    original = manager.last_attempt["commit"]
    changes.target.write_text("second snapshot", encoding="utf-8")
    assert manager.create_checkpoint(changes.workspace, "second", target_paths=[str(changes.target)])
    second = manager.last_attempt["commit"]
    other = Path(changes.workspace).parent / "other-workspace"
    other.mkdir()
    (other / "untouched.txt").write_text("other session", encoding="utf-8")
    assert manager.create_checkpoint(str(other), "other workspace")
    other_commit = manager.last_attempt["commit"]
    status = changes.status()
    assert status["workspace_snapshot_count"] == 2 and status["shared_store_bytes"] > 0
    manager.max_snapshots = 1
    preview = changes.preview({"action": "prune"})
    assert [item["hash"] for item in preview["checkpoint_maintenance"]["affected_checkpoints"]] == [original]
    result = changes.apply({"action": "prune", "confirmation_token": preview["confirmation_token"]})
    assert result["deleted_count"] == 1
    assert manager.has_checkpoint(changes.workspace, second)
    assert manager.has_checkpoint(str(other), other_commit)
    preview = changes.preview({"action": "clear"})
    with pytest.raises(ApplicationError) as exc:
        changes.apply({"action": "clear"})
    assert exc.value.code == "CONFIRMATION_REQUIRED"
    result = changes.apply({"action": "clear", "confirmation_token": preview["confirmation_token"]})
    assert result["deleted_count"] == 1
    assert manager.list_checkpoints(changes.workspace) == []
    assert manager.has_checkpoint(str(other), other_commit)
    assert changes.target.read_text() == "second snapshot"


def test_checkpoint_cleanup_refuses_pinned_recovery_and_stale_preview(changes):
    manager = changes.coordinator.checkpoint_manager
    record(changes)
    before = manager.last_attempt["commit"]
    preview = changes.preview({"action": "clear"})
    intent = manager.begin_restore_intent(changes.workspace, target_commits=[before], target_paths=[str(changes.target)], kind="rollback-turn")
    assert intent["success"]
    with pytest.raises(ApplicationError) as exc:
        changes.apply({"action": "clear", "confirmation_token": preview["confirmation_token"]})
    assert exc.value.code == "PREVIEW_EXPIRED"
    preview = changes.preview({"action": "clear"})
    assert preview["checkpoint_maintenance"]["blocked_by_recovery"]
    assert preview["checkpoint_maintenance"]["affected_checkpoints"] == []
    with pytest.raises(ApplicationError):
        changes.apply({"action": "clear", "confirmation_token": preview["confirmation_token"]})
    assert manager.has_checkpoint(changes.workspace, before)


def test_old_and_long_turn_queries_are_not_truncated(tmp_path, monkeypatch):
    journal = OperationJournal(tmp_path / "journal")
    workspace = tmp_path / "work"
    workspace.mkdir()
    target = workspace / "new.txt"
    target.write_text("present", encoding="utf-8")
    operations = []
    for index in range(410):
        op = journal.begin(session_id="session", turn_id="long" if index < 205 else "new-" + str(index),
                           message_id_before_turn=None, messages_len_before_turn=None, tool_call_id=str(index),
                           tool_name="write_file", action="create", cwd=str(workspace), workspace=str(workspace), targets=[])
        op["targets"] = [{"path": str(target), "before": {"exists": False}, "after": {"exists": True, "kind": "file", "size": 7}}]
        journal.finalize(op, success=True)
        operations.append(op)
    assert journal.get_operation(operations[0]["operation_id"])["turn_id"] == "long"
    manager = SimpleNamespace(begin_restore_intent=lambda *_args, **kw: {"success": True, "intent_id": "intent", "recovery_commit": "a" * 40, "target_paths": kw["target_paths"]},
                              finish_restore_intent=lambda *_args, **_kw: True)
    coordinator = RollbackCoordinator(checkpoint_manager=manager, journal=journal)
    group = coordinator.get_group("long", workspace=str(workspace))
    assert len(group["operations"]) == 205
    restored = []
    monkeypatch.setattr(coordinator, "rollback_operation", lambda ref, **_kw: restored.append(ref) or {"success": True})
    result = coordinator.rollback_turn("long", workspace=str(workspace))
    assert result["success"] and result["operation_count"] == len(restored) == 205


@pytest.mark.asyncio
async def test_real_worker_maintenance_restores_without_loading_model(changes):
    from mclaw.cli.runtime.workspace_trust import is_workspace_trusted
    from mclaw.cli.config import load_config, save_config

    record(changes)
    save_config({"checkpoints": {"enabled": True}, "language": "preserved-test-setting"})
    runner = ProcessRunner(changes.home, changes.session)
    try:
        result = await asyncio.wait_for(runner.control({"operation": "list"}), 20)
        assert result["groups"][0]["id"] == "turn-one"
        preview = await runner.control({"operation": "preview", "action": "undo", "group_id": "turn-one"})
        assert preview["workspace_trust_required"] is True
        with pytest.raises(ApplicationError) as exc:
            await runner.control({"operation": "apply", "action": "undo", "group_id": "turn-one", "confirmation_token": preview["confirmation_token"]})
        assert exc.value.code == "WORKSPACE_UNTRUSTED"
        assert not is_workspace_trusted(load_config(), changes.workspace)
        with pytest.raises(ApplicationError) as exc:
            await runner.control({"operation": "trust", "workspace": str(Path(changes.workspace).parent), "confirmation_token": preview["confirmation_token"]})
        assert exc.value.code == "WORKSPACE_MISMATCH"
        assert not is_workspace_trusted(load_config(), changes.workspace)
        confirmed = await runner.control({"operation": "trust", "workspace": preview["workspace"], "confirmation_token": preview["confirmation_token"]})
        assert confirmed == {"workspace_trusted": True}
        assert is_workspace_trusted(load_config(), changes.workspace)
        assert load_config()["language"] == "preserved-test-setting"
        result = await asyncio.wait_for(runner.control({"operation": "apply", "action": "undo", "group_id": "turn-one", "confirmation_token": preview["confirmation_token"]}), 20)
        assert result["success"] and changes.target.read_text() == "original"
        assert runner.process.poll() is None
        runner._running = True
        with pytest.raises(ApplicationError) as exc:
            await runner.control({"operation": "list"})
        assert exc.value.code == "SESSION_BUSY"
        runner._running = False
    finally:
        await runner.close()


def test_recovery_trust_rejects_expired_preview_and_replaced_directory(changes):
    from mclaw.cli.config import load_config
    from mclaw.cli.runtime.workspace_trust import is_workspace_trusted

    record(changes)
    with pytest.raises(ApplicationError) as exc:
        changes.trust({"workspace": changes.workspace})
    assert exc.value.code == "PREVIEW_EXPIRED"
    preview = changes.preview({"action": "undo", "group_id": "turn-one"})
    workspace = Path(changes.workspace)
    relocated = workspace.with_name("previous-workspace")
    assert workspace.is_relative_to(changes.home.parent) and relocated.is_relative_to(changes.home.parent)
    workspace.rename(relocated)
    workspace.mkdir()
    try:
        with pytest.raises(ApplicationError) as exc:
            changes.trust({"workspace": changes.workspace, "confirmation_token": preview["confirmation_token"]})
        assert exc.value.code == "PREVIEW_EXPIRED"
        assert not is_workspace_trusted(load_config(), changes.workspace)
    finally:
        workspace.rmdir()
        relocated.rename(workspace)


@pytest.mark.asyncio
@pytest.mark.parametrize("cross_session", [False, True])
async def test_service_maintenance_fences_queue_and_syncs_invalidated_history(changes, cross_session):
    op = record(changes)
    entered, proceed = asyncio.Event(), asyncio.Event()
    class Runner:
        def send(self, _command):
            pass
        async def control(self, request):
            entered.set()
            await proceed.wait()
            return changes.apply(request)
        async def close(self):
            pass
    service = SessionService(changes.home, runner_factory=lambda _session: Runner())
    sid = "session"
    original = service.snapshot(sid, "browser")
    assert original["messages"][-1]["turn_id"] == "turn-one"
    assert original["messages"][-1]["operation_id"] == op["operation_id"]
    old = service._load(sid)
    for message in old["messages"]:
        message.pop("turn_id", None)
    service.store.save(old)
    assert service.snapshot(sid)["messages"][-1]["turn_id"] == "turn-one"
    controller = sid
    if cross_session:
        other_workspace = changes.home.parent / "other-workspace"
        other_workspace.mkdir()
        controller = service.create("browser", workspace=str(other_workspace))["id"]
    operation = "terminal_files" if cross_session else "apply"
    task = asyncio.create_task(service.change_operation(controller, "browser", {"operation": operation, "action": "undo", "group_id": "turn-one"}))
    await entered.wait()
    service.submit(sid, "browser", "queued while restoring", "queued")
    assert sid not in service.jobs
    service.stop(sid, "browser")
    proceed.set()
    result = await task
    assert any(row.get("invalidated") for row in service.snapshot(sid)["messages"] if row["role"] == "assistant")
    assert service._load(sid)["queue"][0]["content"] == "queued while restoring"
    assert not service.maintenance
    await service.close()


@pytest.mark.asyncio
async def test_unknown_recovery_pauses_queue_and_preserves_tasks(tmp_path):
    entered, proceed = asyncio.Event(), asyncio.Event()
    class Runner:
        async def control(self, request):
            entered.set()
            await proceed.wait()
            raise ApplicationError("RECOVERY_UNKNOWN", "worker exited", 409)
        async def close(self):
            pass
    workspace = tmp_path / "work"
    workspace.mkdir()
    service = SessionService(tmp_path / "home", runner_factory=lambda _session: Runner())
    sid = service.create("owner", workspace=str(workspace))["id"]
    task = asyncio.create_task(service.change_operation(sid, "owner", {"operation": "apply"}))
    await entered.wait()
    service.submit(sid, "owner", "preserved", "once")
    proceed.set()
    with pytest.raises(ApplicationError):
        await task
    snapshot = service.snapshot(sid)
    assert snapshot["paused"] and snapshot["status"] == "failed" and snapshot["queue"][0]["content"] == "preserved"
    assert sid not in service.jobs
    await service.close()


def test_change_http_contract_and_json_validation():
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.testclient import TestClient
    from mclaw.web.changes import build_change_routes

    calls = []
    class Service:
        async def change_operation(self, sid, client, request):
            calls.append((sid, client, request))
            return {"groups": [], "checkpoints": []}
    async def error(_request, exc):
        return JSONResponse({"code": exc.code}, status_code=exc.status)
    with TestClient(Starlette(routes=build_change_routes(Service()), exception_handlers={ApplicationError: error})) as client:
        assert client.get("/api/v1/sessions/sid/changes", headers={"x-mclaw-client": "browser"}).status_code == 200
        assert calls[-1] == ("sid", "browser", {"operation": "list"})
        assert client.post("/api/v1/sessions/sid/changes/preview", json={"group_id": "turn", "action": "undo"}).status_code == 200
        assert calls[-1][2]["operation"] == "preview"
        assert client.post("/api/v1/sessions/sid/changes/apply", json=[]).status_code == 400
