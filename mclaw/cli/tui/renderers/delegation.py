# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Render subagent delegation progress and result summaries."""

from __future__ import annotations

from typing import Callable

from mclaw.cli.runtime.panels import PanelColumn, PanelModel, panel_cell, table_block
from mclaw.cli.tui.panel_renderer import render_panel_model
from mclaw.cli.tui.theme import ACCENT_COLOR


class DelegationRenderer:
    def __init__(
        self,
        *,
        run_external_output: Callable,
        box_factory: Callable,
        panel_sink: Callable[[PanelModel], None] | None = None,
    ):
        self._run_external_output = run_external_output
        self._box_factory = box_factory
        self._panel_sink = panel_sink

    def render_aggregation(self, results: dict) -> str:
        task_results = results.get("results", [])
        total_duration = results.get("total_duration_seconds", 0)
        icons = {"completed": "✅", "error": "❌", "interrupted": "⚠️", "failed": "❌"}

        rows = []
        for r in task_results:
            status = r.get("status", "unknown")
            icon = icons.get(status, "❓")
            goal = r.get("goal", "") or ""
            goal_short = goal[:30] + "…" if len(goal) > 30 else goal
            duration = r.get("duration_seconds", 0)
            api_calls = r.get("api_calls", 0)
            rows.append((
                f"任务 {r.get('task_index', '?')}",
                goal_short,
                panel_cell(f"{icon} {status}", _status_role(status)),
                f"{duration:.1f}s",
                str(api_calls) if api_calls else "-",
            ))

        panel = PanelModel(
            title=f"M-Claw 委托完成 · {len(task_results)} 个任务 · {total_duration:.1f}s",
            namespace="delegation",
            blocks=(
                table_block(
                    [
                        PanelColumn("任务", role="accent", width=8),
                        PanelColumn("目标", role="primary"),
                        PanelColumn("状态", role="accent", width=10),
                        PanelColumn("耗时", role="muted", width=8),
                        PanelColumn("调用", role="muted", width=6),
                    ],
                    rows,
                ),
            ),
        )

        def _print_panel():
            render_panel_model(
                panel,
                border_style=ACCENT_COLOR,
                box=self._box_factory(),
            )

        if self._panel_sink is not None:
            self._panel_sink(panel)
        else:
            self._run_external_output(_print_panel)

        lines = [f"子代理任务全部完成（共 {len(task_results)} 个任务，总耗时 {total_duration:.1f} 秒）："]
        for r in task_results:
            idx = r.get("task_index", "?")
            status = r.get("status", "unknown")
            goal = r.get("goal", "")
            summary = r.get("summary") or ""
            error = r.get("error", "")
            duration = r.get("duration_seconds", 0)
            lines.append(f"\n【任务 {idx}】{goal}")
            lines.append(f"状态：{status}（{duration:.1f} 秒）")
            if error:
                lines.append(f"错误：{error}")
            if summary:
                lines.append(f"结果：{summary}")
        lines.append(
            "\n请把以上子代理结果作为中间材料，回到原始用户任务继续执行。"
            "如果原始任务还需要创建或修改文件、生成 PPT、运行验证、调用工具或继续处理，必须继续完成。"
            "只有确认原始用户任务已经完整完成后，才给出最终回复；不要仅总结子代理结果后结束。"
        )
        return "\n".join(lines)


def _status_role(status: str) -> str:
    if status == "completed":
        return "success"
    if status in {"error", "failed"}:
        return "danger"
    if status == "interrupted":
        return "warning"
    return "default"
