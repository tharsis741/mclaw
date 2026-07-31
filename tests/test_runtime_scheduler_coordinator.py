# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import time
from pathlib import Path
from types import SimpleNamespace

from mclaw.cli.runtime import scheduler as scheduler_runtime
from mclaw.cli.runtime.scheduler import RuntimeSchedulerCoordinator
from mclaw.scheduler.models import SchedulerTarget
from mclaw.scheduler.store import SchedulerStore


def _coordinator(tmp_path: Path, *, now: float):
    store = SchedulerStore(db_path=tmp_path / "scheduler.db")
    notices = []
    ticks = []
    coordinator = RuntimeSchedulerCoordinator(
        store=store,
        engine=SimpleNamespace(tick_once=lambda **kwargs: ticks.append(kwargs)),
        delivery=SimpleNamespace(deliver_one=lambda *_args, **_kwargs: {"success": True}),
        config={
            "toolsets": ["mclaw-required"],
            "scheduler": {
                "default_timezone": "Asia/Shanghai",
                "default_max_iterations": 12,
                "default_timeout_seconds": 90,
            },
        },
        workspace=str(tmp_path),
        render_notice=lambda *args: notices.append(args),
        available_toolsets=lambda: [],
        now=lambda: now,
    )
    return store, coordinator, notices, ticks


def test_local_job_creation_wizard_persists_the_completed_draft(tmp_path: Path) -> None:
    store, coordinator, notices, _ticks = _coordinator(tmp_path, now=1_800_000_000)
    try:
        coordinator.handle_command("new")
        for value, expected_state in (
            ("Y", "new_step1"),
            ("1", "new_step2"),
            ("4", "new_schedule_interval"),
            ("1h", "new_name"),
            ("Hourly report", "new_prompt"),
            ("Summarize the workspace", "new_output_choice"),
            ("1", "new_preview"),
            ("1", "created_done"),
        ):
            coordinator.handle_input(value)
            assert coordinator._state == expected_state

        jobs = store.list_jobs()
        assert len(jobs) == 1
        job = jobs[0]
        assert job.name == "Hourly report"
        assert job.prompt == "Summarize the workspace"
        assert job.delivery.target_id == "local"
        assert job.schedule.trigger_type == "interval"
        assert job.enabled is True
        assert job.max_iterations == 12
        assert job.timeout_seconds == 90
        assert all(not notice[2] for notice in notices)
    finally:
        store.session_db.close()


def test_pairing_flow_moves_a_bound_target_into_the_job_draft(
    monkeypatch,
    tmp_path: Path,
) -> None:
    now = time.time()
    monkeypatch.setattr(scheduler_runtime, "new_pairing_code", lambda: "ABC123")
    store, coordinator, notices, ticks = _coordinator(tmp_path, now=now)
    try:
        coordinator.handle_command("new")
        for value in ("Y", "3", "1"):
            coordinator.handle_input(value)
        assert coordinator._state == "pairing_wait"
        assert store.get_pairing("ABC123").status == "waiting"

        store.bind_pairing(
            "ABC123",
            target=SchedulerTarget(
                id="dingtalk-group-1",
                type="dingtalk_group",
                display_name="研发群",
                account_id="account-1",
                chat_id="chat-1",
                chat_type="group",
            ),
        )
        coordinator.pump()
        assert coordinator._state == "pairing_bound"
        assert ticks == [{"now": now}]

        coordinator.handle_input("2")
        assert coordinator._state == "new_step2"
        assert coordinator._draft.delivery.target_id == "dingtalk-group-1"
        assert all(not notice[2] for notice in notices)
    finally:
        store.session_db.close()
