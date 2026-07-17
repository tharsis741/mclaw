# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from types import SimpleNamespace

from mclaw.cli.tui.frontends.classic import formatted_text_height
from mclaw.cli.tui.renderers.delegation import DelegationRenderer
from mclaw.cli.tui.renderers.status import StatusRenderer


def test_live_subagent_progress_shows_all_five_tasks_and_truncates_at_100() -> None:
    goals = ["g" * 100, "x" * 101, "three", "four", "five"]
    statuses = ["completed", "running", "pending", "completed", "error"]
    owner = SimpleNamespace(
        subtask_manager=SimpleNamespace(tasks=[
            {"index": index, "status": status, "goal": goal}
            for index, (status, goal) in enumerate(zip(statuses, goals))
        ])
    )

    fragments = StatusRenderer().build_subagent_compact_progress(owner)
    rendered = "".join(text for _style, text in fragments)

    assert all(f"Task {index}:" in rendered for index in range(1, 6))
    assert all(
        not line.startswith(" ")
        for line in rendered.splitlines()
        if "Task " in line
    )
    assert "+" not in rendered
    assert goals[0] in rendered
    assert "x" * 99 + "…" in rendered
    assert "x" * 100 not in rendered
    assert (
        "class:status-bar-subagent-running",
        "◼ Task 2: ",
    ) in fragments
    assert (
        "class:status-bar-subagent-goal",
        "x" * 99 + "…",
    ) in fragments
    assert (
        "class:status-bar-subagent-done",
        goals[0],
    ) in fragments


def test_delegation_result_table_truncates_goal_at_100() -> None:
    panels = []
    renderer = DelegationRenderer(
        run_external_output=lambda callback: callback(),
        box_factory=lambda: None,
        panel_sink=panels.append,
    )
    goal = "目" * 101

    follow_up = renderer.render_aggregation({
        "total_duration_seconds": 1.0,
        "results": [{
        "task_index": 0,
        "goal": goal,
        "status": "timed_out",
            "duration_seconds": 1.0,
            "api_calls": 1,
            "summary": "done",
            "summary_path": "C:/handoff.md",
        }],
    })

    displayed_goal = panels[0].blocks[0].table_rows[0][1].text
    assert displayed_goal == "目" * 99 + "…"
    assert len(displayed_goal) == 100
    assert panels[0].blocks[0].table_rows[0][0].text == "任务 1"
    assert panels[0].blocks[0].table_rows[0][2].text == "⚠️ timed_out"
    assert follow_up.startswith(
        "子代理任务全部结束并已完成结果交接（共 1 个任务，总耗时 1.0 秒）："
    )
    assert "子代理任务全部完成" not in follow_up
    assert "【任务 1】" in follow_up
    assert "【任务 0】" not in follow_up
    assert "状态：timed_out（1.0 秒）" in follow_up
    assert "完整结果文件：C:/handoff.md（继续任务前必须读取）" in follow_up
    assert goal in follow_up


def test_live_subagent_progress_labels_finalization_phase() -> None:
    owner = SimpleNamespace(subtask_manager=SimpleNamespace(tasks=[{
        "index": 0,
        "status": "finalizing",
        "goal": "research",
    }]))

    rendered = "".join(
        text for _style, text
        in StatusRenderer().build_subagent_compact_progress(owner)
    )

    assert "Task 1（收尾中）: research" in rendered


def test_status_height_counts_wrapped_cjk_rows() -> None:
    fragments = [("", "header\n"), ("", "界" * 6 + "\n")]

    assert formatted_text_height(fragments, width=10) == 4
