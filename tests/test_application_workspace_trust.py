# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import asyncio
from pathlib import Path
from unittest.mock import Mock

import pytest

from mclaw.application.sessions import SessionService
from mclaw.application.worker import SessionWorker
from mclaw.application.workspaces import is_managed_background_workspace
from mclaw.cli import config as config_io
from mclaw.cli.runtime.workspace_trust import is_workspace_trusted


@pytest.fixture
def sessions(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("MCLAW_HOME", str(home))
    config_io.save_config({"toolsets": ["mclaw-required"]})
    service = SessionService(home, runner_factory=lambda _: None)
    yield service
    for lock in service.locks.values():
        lock.release()
    service.store.close()
    service.history.close()


def schedule_session(sessions, workspace=""):
    sid = sessions.create("scheduler-bootstrap", workspace)["id"]
    sessions.release(sid, "scheduler-bootstrap")
    sessions.prepare_background_session(sid, source="scheduler")
    return sessions.store.get(sid)


def legacy_scheduled_task():
    return {"id": "original-task", "content": "整理文件", "model": "", "status": "interrupted",
            "source": "scheduler", "execution": {"platform": "scheduler"},
            "attempts": [{"id": "original-attempt", "status": "interrupted"}],
            "supplements": [], "created_at": 1}


@pytest.mark.parametrize("custom_root", [False, True])
def test_scheduler_allocated_directory_is_trusted_without_a_browser(sessions, tmp_path, custom_root):
    if custom_root:
        root = tmp_path / "custom-outputs"
        root.mkdir()
        sessions.set_output_root(str(root))
    session = schedule_session(sessions)
    worker = SessionWorker(sessions.home, session, lambda _: None)
    worker.request = Mock(side_effect=AssertionError("automatic directory must not prompt"))
    worker._ensure_workspace_trusted(config_io.load_config(), "scheduler")
    assert session["source"] == "scheduler"
    assert sessions.history.get_session(session["id"])["source"] == "scheduler"
    assert is_workspace_trusted(config_io.load_config(), session["workspace"])
    assert not is_workspace_trusted(config_io.load_config(), Path(session["workspace"]).parent)
    assert "managed_workspace_root" not in sessions.snapshot(session["id"])


def test_scheduler_explicit_directory_still_needs_confirmation(sessions, tmp_path):
    work = tmp_path / "user-project"
    work.mkdir()
    session = schedule_session(sessions, str(work))
    worker = SessionWorker(sessions.home, session, lambda _: None)
    worker.request = Mock(return_value=False)
    with pytest.raises(PermissionError):
        worker._ensure_workspace_trusted(config_io.load_config(), "scheduler")
    worker.request.assert_called_once()
    assert worker.request.call_args.args == ("workspace",)
    assert not is_workspace_trusted(config_io.load_config(), work)
    worker.request.return_value = True
    worker._ensure_workspace_trusted(config_io.load_config(), "scheduler")
    worker.request.reset_mock()
    worker._ensure_workspace_trusted(config_io.load_config(), "scheduler")
    worker.request.assert_not_called()


@pytest.mark.parametrize("location", ["active_task", "queue", "tasks", "history"])
def test_old_scheduler_source_and_marker_are_recovered_without_changing_work(sessions, location):
    sid = sessions.create("old-scheduler")["id"]
    session = sessions.store.get(sid)
    sessions._bind_workspace(session)
    session.pop("managed_workspace_root")
    task = legacy_scheduled_task()
    session.update(title="保留这个名字", paused=True, status="interrupted")
    session["submissions"] = {"schedule:old-run": task["id"]}
    if location == "history":
        sessions.history.create_session(sid, source="scheduler", workspace=session["workspace"])
        sessions.history.append_message(sid, "user", content="历史内容")
    elif location == "active_task":
        session[location] = task
    else:
        session[location] = [task]
    sessions.store.save(session)
    # The recent list must identify old schedules even before they are opened.
    assert sessions.list()[0]["source"] == "scheduler"
    restored = sessions.store.get(sid)
    assert restored["source"] == "scheduler"
    assert is_managed_background_workspace(restored, "scheduler")
    assert restored["title"] == session["title"]
    assert restored["workspace"] == session["workspace"]
    assert restored["paused"] and restored["status"] == "interrupted"
    assert restored["active_task"] == session["active_task"]
    assert restored["queue"] == session["queue"]
    assert restored["tasks"] == session["tasks"]
    assert not is_workspace_trusted(config_io.load_config(), restored["workspace"])
    if location == "history":
        assert sessions.history.get_messages(sid)[0]["content"] == "历史内容"


def test_browser_submission_cannot_claim_scheduler_origin_with_a_request_prefix(sessions):
    sid = sessions.create("browser")["id"]
    session = sessions.store.get(sid)
    sessions._bind_workspace(session)
    session["submissions"] = {"schedule:forged": "browser-task"}
    session["queue"] = [{"id": "browser-task", "source": "web", "content": "普通任务"}]
    sessions.store.save(session)
    assert sessions.snapshot(sid)["source"] == "web"
    assert not is_managed_background_workspace(sessions.store.get(sid), "scheduler")


def test_old_user_directory_is_not_reclassified_as_automatically_created(sessions, tmp_path):
    work = tmp_path / "existing-project"
    work.mkdir()
    sessions.history.create_session("old-schedule", source="scheduler", workspace=str(work))
    session = sessions.prepare_background_session("old-schedule", source="scheduler")
    assert session["workspace_explicit"] is True
    assert "managed_workspace_root" not in sessions.store.get("old-schedule")


@pytest.mark.parametrize("mutation", ["explicit", "web", "channel", "parent", "missing-marker", "symlink", "junction", "root-junction"])
def test_scheduler_exemption_stays_scoped_to_its_allocated_directory(sessions, monkeypatch, mutation):
    session = schedule_session(sessions)
    if mutation == "explicit":
        session["workspace_explicit"] = True
    elif mutation in {"web", "channel"}:
        session["source"] = "web" if mutation == "web" else "weixin"
    elif mutation == "parent":
        session["workspace"] = str(Path(session["workspace"]).parent)
    elif mutation == "missing-marker":
        session.pop("managed_workspace_root")
    else:
        target = Path(session["workspace"] if mutation != "root-junction" else session["managed_workspace_root"])
        method = "is_symlink" if mutation == "symlink" else "is_junction"
        original = getattr(Path, method, lambda _: False)
        monkeypatch.setattr(Path, method, lambda path: path == target or original(path), raising=False)
    assert not is_managed_background_workspace(session, "scheduler")


def test_existing_managed_directory_survives_output_root_change(sessions, tmp_path):
    session = schedule_session(sessions)
    root = tmp_path / "next-outputs"
    root.mkdir()
    sessions.set_output_root(str(root))
    sessions.prepare_background_session(session["id"], source="scheduler")
    restored = sessions.store.get(session["id"])
    assert restored["workspace"] == session["workspace"]
    assert is_managed_background_workspace(restored, "scheduler")


@pytest.mark.asyncio
async def test_resume_old_blocked_schedule_uses_original_task_without_workspace_prompt(sessions):
    session = schedule_session(sessions)
    session.update(source="web", paused=True, status="interrupted", active_task=legacy_scheduled_task())
    session.pop("managed_workspace_root")
    session["submissions"] = {"schedule:old-run": "original-task"}
    sessions.store.save(session)
    calls = []

    class TrustRunner:
        def __init__(self, snapshot):
            self.worker = SessionWorker(sessions.home, snapshot, lambda _: None)
            self.worker.request = Mock(side_effect=AssertionError("resume must not prompt"))

        async def run(self, task, emit):
            self.worker._ensure_workspace_trusted(config_io.load_config(), task["execution"]["platform"])
            calls.append(task)
            return {"completed": True, "final_response": "已完成"}

    sessions.runner_factory = TrustRunner
    sessions.resume(session["id"], "browser")
    await asyncio.wait_for(sessions.jobs[session["id"]], 3)
    assert len(calls) == 1 and calls[0]["id"] == "original-task"
    assert len(calls[0]["attempts"]) == 2
    assert sessions.snapshot(session["id"])["status"] == "completed"
    assert not sessions.snapshot(session["id"]).get("pending_request")
