# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import asyncio
from pathlib import Path
import time

import pytest

from mclaw.application.schedules import ScheduleService
from mclaw.application.sessions import ApplicationError, SessionService
from mclaw.cli.runtime.session_lock import InteractiveSessionLock
from mclaw.scheduler.models import SchedulerTarget
from mclaw.scheduler.ownership import SchedulerOwnership
from mclaw.scheduler.targets import bind_pairing_from_channel


class ControlledRunner:
    def __init__(self, session):
        self.calls = []
        self.results = asyncio.Queue()

    async def run(self, task, emit):
        self.calls.append(task)
        return await self.results.get()

    def send(self, command):
        if command["type"] == "stop":
            self.results.put_nowait({"completed": False, "interrupted": True})

    async def close(self):
        pass


@pytest.fixture
def services(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("MCLAW_HOME", str(home))
    sessions = SessionService(home, runner_factory=ControlledRunner)
    output = tmp_path / "outputs"
    output.mkdir()
    sessions.store.set_setting("output_root", str(output))
    schedules = ScheduleService(home, sessions, config={}, tick_interval=0.02)
    return schedules, sessions


def create(service, **values):
    return service.create({"name": "每天整理", "prompt": "整理工作目录", "schedule": {
        "trigger_type": "daily", "time": "09:00", "timezone": "Asia/Shanghai"}, **values})


@pytest.mark.asyncio
async def test_creation_defaults_follow_shared_config_without_changing_existing_jobs(services):
    from mclaw.cli.config import save_config

    service, sessions = services
    service._provided_config = None
    try:
        save_config({"scheduler": {"default_timezone": "UTC", "default_timeout_seconds": 7200,
                                   "default_max_iterations": 321}})
        first = service.create({"name": "配置默认值", "prompt": "测试", "schedule": {"trigger_type": "daily", "time": "09:00"}})
        assert first["schedule"]["timezone"] == "UTC"
        assert first["timeout_seconds"] == 7200
        assert first["max_iterations"] == 321
        save_config({"scheduler": {"default_timezone": "Asia/Tokyo", "default_timeout_seconds": 1234,
                                   "default_max_iterations": 456}})
        assert service.list()["defaults"] == {"timezone": "Asia/Tokyo", "timeout_seconds": 1234, "max_iterations": 456}
        assert service.update(first["id"], {"name": "原计划"})["timeout_seconds"] == 7200
        explicit = service.create({"name": "显式值", "prompt": "测试", "timeout_seconds": 600,
                                   "max_iterations": 12, "schedule": {"trigger_type": "daily", "time": "09:00", "timezone": "UTC"}})
        assert explicit["timeout_seconds"] == 600
        assert explicit["max_iterations"] == 12
        assert explicit["schedule"]["timezone"] == "UTC"
    finally:
        await close(service, sessions)


async def until(predicate):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(0.01)
    raise AssertionError("asynchronous scheduler condition not reached")


async def running(service, sessions, job):
    run = await service.run_now(job["id"])
    await until(lambda: service.store.get_run(run["id"]).session_id in sessions.runners)
    sid = service.store.get_run(run["id"]).session_id
    return run, sid, sessions.runners[sid]


async def finish(service, runner, run, **result):
    runner.results.put_nowait({"completed": True, "final_response": "已完成", **result})
    return await until(lambda: (row if (row := service.store.get_run(run["id"])).status not in {"running", "queued"} else None))


async def close(service, sessions):
    await service.close()
    await sessions.close()


@pytest.mark.asyncio
async def test_deleted_task_thread_is_replaced_on_next_scheduled_run(services):
    service, sessions = services
    await service.start()
    try:
        job = create(service)
        run, sid, runner = await running(service, sessions, job)
        await finish(service, runner, run)
        await sessions.delete(sid, "browser")
        assert not service.store.get_job(job["id"]).session_id
        service.update(job["id"], {"model": "test-updated-model"})
        run2, sid2, runner2 = await running(service, sessions, job)
        assert sid2 != sid
        assert service.store.get_job(job["id"]).session_id == sid2
        await finish(service, runner2, run2)
        assert service.get(job["id"])["enabled"]
    finally:
        await close(service, sessions)


@pytest.mark.asyncio
async def test_management_preserves_scheduler_schema_and_validates_preview(services):
    service, sessions = services
    try:
        job = create(service)
        assert job["session_policy"] == "task_thread"
        assert len(job["next_runs"]) == 3
        assert job["next_runs"] == sorted(job["next_runs"])
        assert sessions.history._conn.execute("SELECT name FROM scheduler_jobs").fetchone()[0] == job["name"]
        changed = service.update(job["id"], {"schedule": job["schedule"], "enabled": False, "model": "new-model"})
        assert changed["status"] == "paused"
        assert changed["model"] == "new-model"
        assert service.update(job["id"], {"enabled": True})["status"] == "idle"
        for data in ({"schedule": {"trigger_type": "daily", "time": "99:00"}}, {"session_policy": "invalid"}, {"enabled": "false"}):
            with pytest.raises(ApplicationError):
                service.update(job["id"], data)
        assert service.get(job["id"])["schedule"] == job["schedule"]
        service.delete(job["id"])
        assert service.list()["total"] == 0
    finally:
        await close(service, sessions)


@pytest.mark.asyncio
async def test_background_tick_survives_browser_detach_and_reuses_task_thread(services):
    service, sessions = services
    await service.start()
    try:
        job = create(service)
        service.store.update_job(job["id"], {"next_run_at": time.time() - 1})
        await until(lambda: service.store.list_runs(job["id"]))
        row = service.store.list_runs(job["id"])[0]
        await until(lambda: service.store.get_run(row.id).session_id in sessions.runners)
        sid = service.store.get_run(row.id).session_id
        sessions.claim(sid, "browser")
        sessions.release(sid, "browser")
        runner = sessions.runners[sid]
        result = await finish(service, runner, row.to_dict(), token_usage={"total_tokens": 7})
        assert result.status == "succeeded"
        assert Path(result.output_path).is_file()
        assert result.delivery_result["success"]
        run2, sid2, runner2 = await running(service, sessions, job)
        assert sid == sid2
        assert runner2.calls[-1]["execution"]["platform"] == "scheduler"
        assert runner2.calls[-1]["execution"]["extra_system"].startswith("[MClaw Scheduler Run]")
        await finish(service, runner2, run2)
        assert len(service.runs(job["id"])["items"]) == 2
    finally:
        await close(service, sessions)


@pytest.mark.asyncio
async def test_new_session_policy_uses_distinct_sessions(services):
    service, sessions = services
    await service.start()
    try:
        job = create(service, session_policy="new_session")
        first, sid, runner = await running(service, sessions, job)
        await finish(service, runner, first)
        second, other, runner = await running(service, sessions, job)
        assert sid != other
        await finish(service, runner, second)
    finally:
        await close(service, sessions)


@pytest.mark.asyncio
async def test_same_session_browser_work_serializes_before_scheduled_work_without_stealing_lease(services):
    service, sessions = services
    await service.start()
    try:
        job = create(service)
        first, sid, runner = await running(service, sessions, job)
        await finish(service, runner, first)
        sessions.claim(sid, "browser")
        sessions.submit(sid, "browser", "浏览器任务", "browser-request")
        await until(lambda: len(runner.calls) == 2)
        second = await service.run_now(job["id"])
        await until(lambda: len(sessions.snapshot(sid)["queue"]) == 1)
        assert sessions.owners[sid][0] == "browser"
        assert len(runner.calls) == 2
        runner.results.put_nowait({"completed": True})
        await until(lambda: len(runner.calls) == 3)
        await finish(service, runner, second)
    finally:
        await close(service, sessions)


@pytest.mark.asyncio
async def test_cancel_interrupts_actual_agent_preserves_original_and_pauses_queue(services):
    service, sessions = services
    await service.start()
    try:
        job = create(service)
        run, sid, runner = await running(service, sessions, job)
        sessions.claim(sid, "browser")
        sessions.submit(sid, "browser", "后续任务", "later")
        with pytest.raises(ApplicationError) as failure:
            service.delete(job["id"])
        assert failure.value.code == "SCHEDULE_RUNNING"
        result = await service.cancel_run(run["id"])
        assert result["status"] == "cancelled"
        await until(lambda: sessions.snapshot(sid)["status"] == "interrupted")
        snapshot = sessions.snapshot(sid)
        assert snapshot["paused"] and snapshot["active_task"]["content"] == job["prompt"]
        assert snapshot["queue"][0]["content"] == "后续任务"
        assert len(runner.calls) == 1
        assert service.store.get_run(run["id"]).status == "cancelled"
    finally:
        await close(service, sessions)


@pytest.mark.asyncio
async def test_execution_failure_with_partial_response_stays_failed_and_recoverable(services):
    service, sessions = services
    await service.start()
    try:
        job = create(service)
        run, sid, runner = await running(service, sessions, job)
        result = await finish(service, runner, run, completed=False, final_response="部分结果", error="provider unavailable")
        assert result.status == "failed"
        assert service.get(job["id"])["failure_count"] == 1
        assert sessions.snapshot(sid)["paused"]
        assert sessions.snapshot(sid)["active_task"]
    finally:
        await close(service, sessions)


@pytest.mark.asyncio
async def test_service_exit_cancels_run_and_releases_scheduler_lock(services):
    service, sessions = services
    await service.start()
    job = create(service)
    run, sid, runner = await running(service, sessions, job)
    await service.close()
    assert service.store.get_run(run["id"]).status == "cancelled"
    await until(lambda: sessions.snapshot(sid)["paused"])
    competing = SchedulerOwnership(service.home)
    assert competing.acquire()
    competing.release()
    with pytest.raises(ApplicationError):
        await service.run_now(job["id"])
    await sessions.close()


@pytest.mark.asyncio
async def test_second_frontend_cannot_tick_or_run_same_scheduler(services):
    service, sessions = services
    competing = SchedulerOwnership(service.home)
    assert competing.acquire()
    await service.start()
    try:
        job = create(service)
        service.store.update_job(job["id"], {"next_run_at": time.time() - 1})
        assert service.engine.tick_once() == 0
        assert service.runtime()["owner"] == "other"
        with pytest.raises(ApplicationError) as failure:
            await service.run_now(job["id"])
        assert failure.value.code == "SCHEDULER_OCCUPIED"
        assert not service.store.list_runs()
        competing.release()
        await until(lambda: service.store.list_runs())
    finally:
        competing.release()
        await close(service, sessions)


@pytest.mark.asyncio
async def test_terminal_session_lock_delays_scheduled_submission(services):
    service, sessions = services
    await service.start()
    try:
        sid = sessions.create("seed")["id"]
        sessions.release(sid, "seed")
        lock = InteractiveSessionLock(sid, service.home / "sessions" / "locks")
        lock.acquire()
        job = create(service)
        service.store.update_job(job["id"], {"session_id": sid})
        run = await service.run_now(job["id"])
        await asyncio.sleep(0.15)
        assert not sessions.snapshot(sid)["queue"]
        assert sid not in sessions.runners
        lock.release()
        await until(lambda: sid in sessions.runners)
        await finish(service, sessions.runners[sid], run)
    finally:
        await close(service, sessions)


@pytest.mark.asyncio
async def test_targets_binding_redacts_transport_credentials_and_validates_references(services):
    service, sessions = services
    try:
        pairing = service.create_pairing({"type": "weixin_private", "display_name": "团队"})
        assert pairing["command"] == "/schedule-bind " + pairing["code"]
        bound = bind_pairing_from_channel(
            store=service.store, code=pairing["code"], target_type="weixin_private", display_name="团队", account_id="bot", chat_id="person",
            chat_type="private", route_metadata={"context_token": "DO-NOT-EXPOSE"},
        )
        assert bound.success
        target_id = bound.target.id
        assert "DO-NOT-EXPOSE" not in str(service.targets())
        assert service.pairing(pairing["code"])["target_id"] == target_id
        assert service.update_target(target_id, {"enabled": False})["route_status"] == "disabled"
        job = create(service, delivery={"target_id": target_id})
        with pytest.raises(ApplicationError) as failure:
            service.delete_target(target_id)
        assert failure.value.code == "TARGET_IN_USE"
        service.delete(job["id"])
        service.delete_target(target_id)
        assert (await service.test_target("local"))["success"]
        with pytest.raises(ApplicationError):
            service.delete_target("local")
    finally:
        await close(service, sessions)


@pytest.mark.asyncio
async def test_restart_fails_abandoned_run_without_replaying_paused_session(services):
    service, sessions = services
    job = create(service)
    sid = sessions.create("seed")["id"]
    sessions.stop(sid, "seed")
    service.store.update_job(job["id"], {"session_id": sid})
    abandoned = service.store.start_run(service.store.get_job(job["id"]), worker_id="lost", scheduled_for=None, now=time.time())
    await service.start()
    try:
        assert service.store.get_run(abandoned.id).status == "failed"
        run = await service.run_now(job["id"])
        await until(lambda: sessions.snapshot(sid)["queue"])
        assert sid not in sessions.runners
        assert sessions.snapshot(sid)["paused"]
        await service.cancel_run(run["id"])
        await until(lambda: not sessions.snapshot(sid)["queue"])
        assert sessions.snapshot(sid)["paused"]
    finally:
        await close(service, sessions)


@pytest.mark.asyncio
async def test_other_scheduler_owner_prevents_recovery_of_its_active_run(services):
    service, sessions = services
    job = create(service)
    active = service.store.start_run(service.store.get_job(job["id"]), worker_id="terminal", scheduled_for=None, now=1)
    owner = SchedulerOwnership(service.home)
    assert owner.acquire()
    await service.start()
    try:
        await asyncio.sleep(0.05)
        assert service.store.get_run(active.id).status == "running"
        assert not service.runtime()["running"]
    finally:
        await close(service, sessions)
        owner.release()


@pytest.mark.asyncio
async def test_timeout_interrupts_exact_task_and_keeps_partial_context(services):
    service, sessions = services
    await service.start()
    try:
        job = create(service, timeout_seconds=1)
        run, sid, runner = await running(service, sessions, job)
        result = await until(lambda: (row if (row := service.store.get_run(run["id"])).status == "failed" else None))
        assert "超时" in result.error
        await until(lambda: sessions.snapshot(sid)["status"] == "interrupted")
        assert sessions.snapshot(sid)["active_task"] is not None
        assert len(runner.calls) == 1
    finally:
        await close(service, sessions)


@pytest.mark.asyncio
async def test_model_edit_updates_only_not_started_session_tasks(services):
    service, sessions = services
    await service.start()
    try:
        job = create(service, model="old-model")
        run, sid, runner = await running(service, sessions, job)
        sessions.claim(sid, "browser")
        sessions.submit(sid, "browser", "排队任务", "later")
        service.update(job["id"], {"model": "new-model"})
        assert runner.calls[0]["model"] == "old-model"
        assert sessions.snapshot(sid)["queue"][0]["model"] == "new-model"
        await finish(service, runner, run)
        await until(lambda: len(runner.calls) == 2)
        runner.results.put_nowait({"completed": True})
        await until(lambda: sessions.snapshot(sid)["status"] == "completed")
    finally:
        await close(service, sessions)


@pytest.mark.asyncio
async def test_completion_cannot_overwrite_concurrent_run_cancellation(services):
    service, sessions = services
    try:
        job = create(service)
        row = service.store.start_run(service.store.get_job(job["id"]), worker_id="test", scheduled_for=None, now=time.time())
        service.store.cancel_run(row.id)
        row.status = "succeeded"
        service.store.finish_run(row, job_patch={"status": "idle", "failure_count": 0})
        assert service.store.get_run(row.id).status == "cancelled"
    finally:
        await close(service, sessions)


@pytest.mark.asyncio
async def test_invalid_existing_config_leaves_setup_and_management_accessible(services):
    service, sessions = services
    (service.home / "config.yaml").write_text("scheduler: [invalid", encoding="utf-8")
    other = ScheduleService(service.home, sessions)
    try:
        await other.start()
        assert not other.runtime()["running"]
        assert "配置" in other.runtime()["reason"]
        assert other.list()["total"] == 0
        assert (service.home / "config.yaml").read_text(encoding="utf-8") == "scheduler: [invalid"
    finally:
        await other.close()
        await close(service, sessions)
