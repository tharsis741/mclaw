# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Real-provider background notifications, including worker IPC and persistence."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import time
from pathlib import Path

import pytest

from mclaw.agent.skill_review_state import get_skill_review_state
from mclaw.application.sessions import SessionService
from mclaw.application.worker import SessionWorker
from mclaw.cli.config import save_config
from test_skill_review_live import ARITHMETIC, WORKFLOW, live, turn, wait_idle

pytestmark = pytest.mark.skipif(os.environ.get("MCLAW_RUN_LIVE_AGENT_TESTS") != "1",
                               reason="Real configured-provider requests require explicit opt-in")


def notices(snapshot):
    return [message for message in snapshot["messages"] if message.get("skill_review")]


async def until(predicate, *, timeout=480):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            pytest.fail("Live notification condition was not reached")
        await asyncio.sleep(0.1)


def report(name, data):
    directory = os.environ.get("MCLAW_LIVE_REPORT_DIR")
    if directory:
        target = Path(directory)
        target.mkdir(parents=True, exist_ok=True)
        (target / name).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


@pytest.mark.asyncio
async def test_live_notifications_cross_worker_and_restore(live):
    _, trace, home = live
    config = copy.deepcopy(trace.config)
    config["skills"] = {**config.get("skills", {}), "evolution_review_round": 2}
    config["memory"] = {"memory_enabled": False, "user_profile_enabled": False, "memory_review_round": 0}
    config["checkpoints"] = {"enabled": False}
    config["compression"] = {"enabled": False}
    config["agent"] = {**config.get("agent", {}), "max_turns": 12}
    workspace = home / "_temp"
    workspace.mkdir()
    config["security"] = {**config.get("security", {}), "trusted_workspaces": [str(workspace)]}
    save_config(config)
    service = SessionService(home)
    received = []
    actual = service._worker_background
    def observe(session_id, event):
        if event.get("type") == "skill_review":
            received.append({"event": copy.deepcopy(event),
                             "active_task": (service.snapshot(session_id).get("active_task") or {}).get("id")})
        actual(session_id, event)
    service._worker_background = observe
    sid = service.create("live", str(workspace), f"custom::{trace.context.model}")["id"]
    other = service.create("other")["id"]
    try:
        for index, message in enumerate((WORKFLOW, ARITHMETIC)):
            service.submit(sid, "live", message, f"live-{index}")
            await until(lambda: sid not in service.jobs)
            assert service.snapshot(sid)["status"] == "completed", "Real worker task did not complete"
        snapshot = service.snapshot(sid)
        trigger_task = service.store.get(sid)["tasks"][-1]["id"]
        trigger_response = service.store.get(sid)["tasks"][-1]["attempts"][-1]["id"]
        # Move the owning worker to another run while the original review continues.
        service.submit(sid, "live", ARITHMETIC, "live-next-turn")
        await until(lambda: sid not in service.jobs)
        await until(lambda: any(item["skill_review"]["status"] == "completed" for item in notices(service.snapshot(sid))))
        snapshot = service.snapshot(sid)
        saved = notices(snapshot)
        assert len(saved) == 1
        notice = saved[0]["skill_review"]
        assert notice["trigger_task_id"] == trigger_task
        assert notice["trigger_response_id"] == trigger_response
        assert notice["skills"] and not notices(service.snapshot(other))
        assert any(item["active_task"] != trigger_task for item in received)
        assert snapshot["status"] == "completed" and not snapshot["queue"] and not snapshot["active_task"]
        assert len(service.store.get(sid)["tasks"]) == 3
        # Presentation notices never become context or a user task.
        history = service.history.get_messages_as_conversation(sid)
        assert all("skill_review" not in row for row in history)
        assert all(row.get("content") != saved[0]["content"] for row in history)
        seq = snapshot["seq"]
        for item in reversed(received):
            actual(sid, item["event"])
        assert service.snapshot(sid)["seq"] == seq
        assert notices(service.snapshot(sid)) == saved
        actual(other, received[-1]["event"])
        assert not notices(service.snapshot(other))
        trace.worker_requests = sum("[MODEL REQUEST]" in line
            for path in (home / "logs" / "sessions").glob("*.log")
            for line in path.read_text(encoding="utf-8").splitlines())
        assert trace.worker_requests > 3
        report("worker-notification.json", {"notifications": saved, "events": received,
                                             "history_message_count": len(history), "task_count": 3,
                                             "worker_model_requests": trace.worker_requests,
                                             "model": trace.context.model, "transport": "production ProcessRunner / SessionWorker"})
        report("ui-notification-session.json", {"id": sid, "title": "后台通知验证", "status": "completed",
                                                "messages": snapshot["messages"], "queue": []})
    finally:
        await service.close()
    restored = SessionService(home)
    try:
        assert notices(restored.snapshot(sid)) == saved
        assert restored.snapshot(sid)["status"] == "completed"
        assert not notices(restored.snapshot(other))
    finally:
        await restored.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_after_save", [False, True], ids=["save", "partial"])
async def test_live_notification_during_next_turn(live, fail_after_save):
    make_agent, trace, home = live
    trace.gate_mode = "save"
    trace.fail_after_save = fail_after_save
    service = SessionService(home)
    snapshot = service.create("live")
    sid = snapshot["id"]
    snapshot["workspace"] = str(home)
    loop = asyncio.get_running_loop()
    wire = []
    def receive(event):
        if event.get("type") == "skill_review":
            wire.append(copy.deepcopy(event))
            loop.call_soon_threadsafe(service._worker_background, sid, event)
    worker = SessionWorker(home, snapshot, receive)
    worker.run_id = "original-response"
    agent = make_agent(2, session_id=sid, event_callback=worker._agent_event)
    agent._application_task_id = "original-task"
    agent._application_response_id = "original-response"
    try:
        await asyncio.to_thread(turn, agent, WORKFLOW)
        await asyncio.to_thread(turn, agent, ARITHMETIC)
        await until(trace.ready.is_set)
        # Changing the foreground run must not relabel the held background write.
        worker.run_id = "next-response"
        agent._application_task_id = "next-task"
        agent._application_response_id = "next-response"
        await asyncio.to_thread(turn, agent, ARITHMETIC)
        await asyncio.to_thread(turn, agent, ARITHMETIC)
        assert get_skill_review_state(agent).snapshot()["pending"]
        trace.gate_release.set()
        await asyncio.to_thread(wait_idle, agent)
        await until(lambda: bool(notices(service.snapshot(sid))) and notices(service.snapshot(sid))[0]["skill_review"]["status"] != "running")
        saved = notices(service.snapshot(sid))
        assert len(saved) == 1
        notice = saved[0]["skill_review"]
        assert notice["status"] == ("partial" if fail_after_save else "completed")
        assert notice["trigger_task_id"] == "original-task"
        assert notice["trigger_response_id"] == "original-response"
        assert all("run_id" not in event for event in wire)
        assert any(event["notice"]["status"] == "running" and event["notice"]["skills"] for event in wire)
        state = get_skill_review_state(agent).snapshot()
        assert state["turns"] == 0 and not state["pending"]
        assert trace.backgrounds == 1
        assert service.snapshot(sid)["status"] == "idle" and not service.snapshot(sid)["active_task"]
        report(f"controlled-{'partial' if fail_after_save else 'save'}.json", {"notification": saved[0], "state": state,
                                                                           "requests": len(trace.calls), "wire": wire})
        # Replay a genuinely observed in-progress write into an isolated store,
        # then recover it as an exited review without changing main-task state.
        recovery = service.create("recovery")
        first_save = copy.deepcopy(next(event for event in wire if event["notice"]["skills"]))
        first_save["notice"]["session_id"] = recovery["id"]
        service._worker_background(recovery["id"], first_save)
    finally:
        trace.gate_release.set()
        await service.close()
    restored = SessionService(home)
    try:
        assert notices(restored.snapshot(sid)) == saved
        recovered = notices(restored.snapshot(recovery["id"]))[0]["skill_review"]
        assert recovered["status"] == "interrupted" and recovered["skills"] == first_save["notice"]["skills"]
        assert restored.snapshot(recovery["id"])["status"] == "idle"
    finally:
        await restored.close()


@pytest.mark.asyncio
async def test_live_no_saved_skill_stays_quiet(live):
    make_agent, trace, home = live
    agent = make_agent(2)
    await asyncio.to_thread(turn, agent, ARITHMETIC)
    await asyncio.to_thread(turn, agent, ARITHMETIC)
    await asyncio.to_thread(wait_idle, agent)
    assert trace.backgrounds == 1 and not trace.events
    assert not any(row["success"] for row in trace.results)


@pytest.mark.asyncio
async def test_live_failure_before_save_is_not_reported_as_success(live):
    make_agent, trace, _ = live
    trace.fail_before_save = True
    agent = make_agent(2)
    await asyncio.to_thread(turn, agent, ARITHMETIC)
    await asyncio.to_thread(turn, agent, ARITHMETIC)
    await asyncio.to_thread(wait_idle, agent)
    assert len(trace.events) == 1
    notice = trace.events[0]["notice"]
    assert notice["status"] == "failed" and not notice["skills"]
    assert notice["error"]
    assert not any(row["success"] for row in trace.results)
    report("failed-notice.json", notice)


@pytest.mark.asyncio
async def test_live_background_update_uses_actual_saved_actions(live):
    make_agent, trace, _ = live
    agent = make_agent(2)
    await asyncio.to_thread(turn, agent, "请创建并保存技能 notification-ledger-review。"
        "这是长期复用的纯文本样本核对流程：输入每行编号、预期状态和实际状态；"
        "保留输入顺序逐行比较；缺失值标记待确认；输出一致数、差异数、缺失数和对应编号。"
        "仅处理文本，信息完整，使用 skill_manage 创建并校验。")
    assert any(row["name"] == "notification-ledger-review" and row["action"] in {"create", "edit", "patch"} and row["success"]
               for row in trace.results)
    assert not trace.events
    await asyncio.to_thread(turn, agent, "刚才创建的 notification-ledger-review 增加一条已反复验证的稳定经验："
        "编号如果包含前导零，应保留原始文本，不能转换成数字；例如 007 应继续输出 007。"
        "这条经验今后始终适用于这个已有技能。"
        "本条只回复收到，不调用工具；限制仅适用于本条回复。")
    await asyncio.to_thread(turn, agent, ARITHMETIC)
    await asyncio.to_thread(wait_idle, agent)
    assert trace.events
    notice = trace.events[-1]["notice"]
    assert notice["status"] == "completed"
    updated = next(item for item in notice["skills"] if item["name"] == "notification-ledger-review")
    assert "create" not in updated["actions"]
    assert set(updated["actions"]) & {"edit", "patch", "evolution_update"}
    report("updated-notice.json", notice)
