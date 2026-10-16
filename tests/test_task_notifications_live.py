# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Real DeepSeek requests through the production worker and durable completion feed."""

import asyncio
import copy
import os

import pytest

from mclaw.application.runner import ProcessRunner
from mclaw.application.sessions import SessionService
from mclaw.application.task_notifications import first_reply_line
from mclaw.cli.config import save_config
from test_skill_notifications_live import report, until
from test_skill_review_live import live

pytestmark = pytest.mark.skipif(os.environ.get("MCLAW_RUN_LIVE_AGENT_TESTS") != "1",
                               reason="Requires real configured-provider Agent requests")


class ReplyGate:
    """Control finalization only after the production Agent has actually replied."""

    def __init__(self):
        self.ready = asyncio.Event()
        self.release = asyncio.Event()
        self.fail = False


@pytest.mark.asyncio
async def test_live_global_completion_queue_restart_and_terminal_states(live):
    _, trace, home = live
    config = copy.deepcopy(trace.config)
    config["skills"] = {**config.get("skills", {}), "evolution_review_round": 0}
    config["memory"] = {"memory_enabled": False, "user_profile_enabled": False, "memory_review_round": 0}
    config["checkpoints"] = {"enabled": False}
    config["compression"] = {"enabled": False}
    workspace = home / "_temp"
    workspace.mkdir()
    config["security"] = {**config.get("security", {}), "trusted_workspaces": [str(workspace)]}
    save_config(config)
    service = SessionService(home)
    gates = {}

    class ObservedRunner(ProcessRunner):
        async def run(self, task, emit):
            result = await super().run(task, emit)
            gate = gates.pop(task["id"], None)
            if gate is not None:
                assert result.get("completed"), "Real Agent failed before the finalization gate"
                gate.ready.set()
                await asyncio.wait_for(gate.release.wait(), timeout=180)
                if gate.fail:
                    raise RuntimeError("Controlled finalization failure after a real Agent response")
            return result

    service.runner_factory = lambda session: ObservedRunner(
        home, session, background_emit=lambda event: service._worker_background(session["id"], event))
    owner = "completion-live"
    a = service.create(owner, str(workspace), f"custom::{trace.context.model}")["id"]
    b = service.create(owner, str(workspace), f"custom::{trace.context.model}")["id"]
    service.patch(a, owner, {"title": "摘要横条：连续排队"})
    service.patch(b, owner, {"title": "摘要横条：另一会话"})
    prompt = "这是一次性 UI 验收，无需使用或保存技能，不调用工具。请严格回复两行：\n**复核完成：一致 1、差异 1、资料不全 1。**\n差异编号 B，资料不全编号 C。"
    gate = ReplyGate()
    snapshots = []
    try:
        service.submit(a, owner, prompt, "first")
        first = service.store.get(a)["queue"][0]["id"]
        gates[first] = gate
        service.submit(a, owner, prompt.replace("一致 1", "一致 2"), "queued-second")
        service.submit(b, owner, prompt.replace("一致 1", "一致 3"), "parallel")
        await until(lambda: gate.ready.is_set() or a not in service.jobs)
        assert gate.ready.is_set()
        await until(lambda: b not in service.jobs)
        # Seeing final streamed text is insufficient: A is still behind the finalization gate.
        feed = service.store.task_notifications()
        assert len(feed["notifications"]) == 1 and feed["notifications"][0]["session_id"] == b
        assert len(service.snapshot(a)["queue"]) == 1
        before_finalization = service.snapshot(a)
        gate.release.set()
        await until(lambda: a not in service.jobs)
        feed = service.store.task_notifications()
        assert feed["pending_count"] == 3
        snapshots = [service.snapshot(a), service.snapshot(b)]
        for notice in feed["notifications"]:
            snapshot = next(item for item in snapshots if item["id"] == notice["session_id"])
            response = next(item for item in snapshot["messages"] if item["id"] == notice["response_id"])
            assert response["result"]["status"] == "completed"
            assert notice["preview"] == first_reply_line(response["content"])
            assert "\n" not in notice["preview"] and "差异编号 B" not in notice["preview"]
            assert response["task_notification_id"] == notice["id"]
        assert len({item["task_id"] for item in feed["notifications"]}) == 3
        assert service.list("no-such-session") == []
        assert service.store.task_notifications() == feed
        first_notice = next(item for item in feed["notifications"] if item["task_id"] == first)
        dismissed = service.acknowledge_task_notifications([first_notice["id"]], "dismiss")
        assert dismissed["pending_count"] == 2 and a in dismissed["unread_session_ids"]

        terminal_results = []
        for index, kind in enumerate(("stop", "failed")):
            gate = ReplyGate()
            gate.fail = kind == "failed"
            service.submit(a, owner, prompt, f"terminal-{index}")
            task = service.store.get(a)["queue"][0]["id"]
            gates[task] = gate
            await until(lambda: gate.ready.is_set() or a not in service.jobs)
            assert gate.ready.is_set()
            if kind == "stop":
                service.stop(a, owner)
            gate.release.set()
            await until(lambda: a not in service.jobs)
            failed = service.snapshot(a)
            assert failed["status"] == ("interrupted" if kind == "stop" else "failed")
            assert not any(item["task_id"] == task for item in service.store.task_notifications()["notifications"])
            terminal_results.append({"kind": kind, "task_id": task, "status": failed["status"]})
            # Continue the actual task with a fresh production Agent request.
            service.resume(a, owner)
            await until(lambda: a not in service.jobs)
            assert service.snapshot(a)["status"] == "completed"
            completed = [item for item in service.store.task_notifications()["notifications"] if item["task_id"] == task]
            assert len(completed) == 1
            assert completed[0]["response_id"] != failed["active_task"]["attempts"][-1]["id"]
        before_restart = service.store.task_notifications()
        count = sum("[MODEL REQUEST]" in line for path in (home / "logs" / "sessions").glob("*.log")
                    for line in path.read_text(encoding="utf-8").splitlines())
        assert count >= 7
        report("task-notifications-live.json", {"feed": feed, "sessions": snapshots, "before_finalization": before_finalization,
            "after_dismiss": dismissed, "before_restart": before_restart, "terminal_results": terminal_results,
            "worker_model_requests": count, "model": trace.context.model,
            "transport": "production SessionService / ProcessRunner / SessionWorker / Agent",
            "controlled_conditions": "Finalization gates after real replies; stop and injected post-reply failure"})
    finally:
        gate.release.set()
        for pending in gates.values():
            pending.release.set()
        await service.close()
    restored = SessionService(home)
    try:
        assert restored.store.task_notifications() == before_restart
        assert first_notice["id"] not in {item["id"] for item in before_restart["notifications"]}
        restored.acknowledge_task_notifications([item["id"] for item in before_restart["notifications"]] + [first_notice["id"]], "read")
        assert restored.store.task_notifications() == {"notifications": [], "pending_count": 0, "unread_session_ids": []}
        report("task-notifications-restored.json", {"before": before_restart, "after_read": restored.store.task_notifications()})
    finally:
        await restored.close()
