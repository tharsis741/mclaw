# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Rollback and checkpoint TUI rendering."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from mclaw.cli.runtime.panels import (
    PanelColumn,
    PanelModel,
    command_block,
    key_value_block,
    notice_panel,
    panel_cell,
    section_block,
    spacer_block,
    table_block,
    text_block,
)
from mclaw.cli.tui.panel_renderer import render_panel_model


class SafetyRenderer:
    """Render file safety, rollback, and checkpoint command output."""

    def __init__(
        self,
        *,
        printer: Callable[[str], None],
        run_external_output: Callable,
        box_factory: Callable,
        panel_sink: Callable[[PanelModel], None] | None = None,
    ):
        self._printer = printer
        self._run_external_output = run_external_output
        self._box_factory = box_factory
        self._panel_sink = panel_sink

    def render_panel_model(self, panel: PanelModel, *, border_style: str | None = None) -> None:
        """Render a panel through the sink when tests or alternate shells provide one."""
        if self._panel_sink is not None:
            self._panel_sink(panel)
            return
        render_panel_model(
            panel,
            printer=self._printer,
            border_style=border_style,
            box=self._box_factory(),
            run_external_output=self._run_external_output,
        )

    def render_rollback_groups(self, groups: list, workspace: str) -> None:
        """Render user-facing rollback groups rather than raw journal operations."""
        if not groups:
            self.render_panel_model(PanelModel(
                title="M-Claw 文件安全层 / Rollback",
                namespace="file_safety",
                blocks=(key_value_block([
                    ("工作区", workspace or "当前目录"),
                    ("状态", "当前没有可撤销的文件操作"),
                    ("下一步", "继续正常对话，或使用 /checkpoints status 查看底层存储"),
                ]),),
            ))
            return

        rows = []
        for idx, group in enumerate(groups, 1):
            ops = group.get("operations") or []
            state = group.get("state") or "unknown"
            action = f"/rollback restore {idx}" if state == "undone" else f"/rollback {idx}"
            if state == "changed":
                action = f"/rollback diff {idx}"
            rows.append((
                str(idx),
                panel_cell(self._state_text(state), self._state_role(state)),
                self._rollback_group_label(ops),
                self._short_time(group.get("updated_at") or ""),
                action,
            ))

        self.render_panel_model(PanelModel(
            title="M-Claw 文件安全层 / Rollback",
            namespace="file_safety",
            blocks=(
                key_value_block([("工作区", workspace or "当前目录")]),
                spacer_block(),
                table_block(
                    (
                        PanelColumn("#", role="muted", justify="right", width=3),
                        PanelColumn("状态", role="accent", no_wrap=True),
                        PanelColumn("变更", role="primary"),
                        PanelColumn("时间", role="muted", no_wrap=True),
                        PanelColumn("操作", role="accent"),
                    ),
                    rows,
                ),
                spacer_block(),
                section_block("状态说明"),
                key_value_block([
                    ("可撤销", "可以直接撤销这条变更"),
                    ("已撤销", "当前文件已回到这条变更之前"),
                    ("后续改动", "后面又改过，先看 diff 再决定"),
                ]),
                spacer_block(),
                section_block("常用命令"),
                command_block([
                    ("/rollback 1", "撤销第 1 条变更"),
                    ("/rollback restore 1", "恢复第 1 条已撤销变更"),
                    ("/rollback undo", "恢复最近一条已撤销变更"),
                    ("/rollback ops", "查看底层操作明细"),
                ]),
            ),
        ))

    def render_rollback_operations(self, operations: list, workspace: str) -> None:
        """Render raw operation-level rollback entries for advanced inspection."""
        if not operations:
            self.render_panel_model(PanelModel(
                title="M-Claw 文件安全层 / 底层操作",
                namespace="file_safety",
                blocks=(key_value_block([
                    ("工作区", workspace or "当前目录"),
                    ("状态", "没有底层操作明细"),
                ]),),
            ))
            return

        rows = []
        for idx, op in enumerate(operations, 1):
            rows.append((
                str(idx),
                str(op.get("operation_id") or "")[:8],
                self._action_label(op),
                self._target_summary(op),
                "可同步" if op.get("message_id_before_turn") is not None else "仅文件",
                self._short_time(op.get("updated_at") or op.get("created_at") or ""),
            ))

        self.render_panel_model(PanelModel(
            title="M-Claw 文件安全层 / 底层操作",
            namespace="file_safety",
            blocks=(
                key_value_block([("工作区", workspace or "当前目录")]),
                spacer_block(),
                table_block(
                    (
                        PanelColumn("#", role="muted", justify="right", width=3),
                        PanelColumn("操作ID", role="muted", no_wrap=True),
                        PanelColumn("动作", role="primary", no_wrap=True),
                        PanelColumn("目标", role="primary"),
                        PanelColumn("上下文", role="muted", no_wrap=True),
                        PanelColumn("时间", role="muted", no_wrap=True),
                    ),
                    rows,
                ),
                spacer_block(),
                section_block("命令"),
                command_block([
                    ("/rollback ops <N>", "只撤销某一个底层操作"),
                    ("/rollback ops diff <N>", "查看某一个底层操作差异"),
                ]),
            ),
        ))

    def render_project_checkpoints(self, checkpoints: list, workspace: str) -> None:
        """Render raw checkpoint restore options with their broader restore risk."""
        meta_rows = [
            ("工作区", workspace or "当前目录"),
            ("类型", "原始 checkpoint，高级恢复入口"),
            ("风险", "只按 checkpoint 中记录的路径恢复；可能保留后续新增或重命名文件"),
        ]

        if not checkpoints:
            meta_rows.append(("状态", "未找到原始 checkpoint"))
            self.render_panel_model(PanelModel(
                title="M-Claw 文件安全层 / 原始 Checkpoint",
                namespace="file_safety",
                blocks=(key_value_block(meta_rows),),
            ))
            return

        rows = []
        for idx, cp in enumerate(checkpoints, 1):
            files = int(cp.get("files_changed", 0) or 0)
            stat = "无统计" if not files else f"{files} 文件 +{int(cp.get('insertions', 0) or 0)}/-{int(cp.get('deletions', 0) or 0)}"
            reason = str(cp.get("reason") or "")
            rows.append((str(idx), cp.get("short_hash", ""), self._short_time(cp.get("timestamp", "")), reason, stat))

        self.render_panel_model(PanelModel(
            title="M-Claw 文件安全层 / 原始 Checkpoint",
            namespace="file_safety",
            blocks=(
                key_value_block(meta_rows),
                spacer_block(),
                table_block(
                    (
                        PanelColumn("#", role="muted", justify="right", width=3),
                        PanelColumn("Hash", role="muted", no_wrap=True),
                        PanelColumn("时间", role="muted", no_wrap=True),
                        PanelColumn("说明", role="primary"),
                        PanelColumn("变动", role="accent", no_wrap=True),
                    ),
                    rows,
                ),
                spacer_block(),
                section_block("命令"),
                command_block([
                    ("/rollback project diff <N>", "预览 checkpoint 差异"),
                    ("/rollback project <N> --yes", "确认恢复整个 checkpoint"),
                    ("/rollback project <N> <file> --yes", "只恢复一个文件"),
                ]),
            ),
        ))

    def render_project_restore_prompt(self, checkpoint_ref: str, file_path: str = "") -> None:
        """Ask for explicit confirmation before raw checkpoint restore."""
        target = f" {file_path}" if file_path else ""
        self.render_panel_model(PanelModel(
            title="M-Claw 文件安全层 / 需要确认",
            namespace="file_safety",
            tone="warning",
            blocks=(key_value_block([
                ("状态", "恢复未执行"),
                ("原因", "这是原始 checkpoint 恢复，不是操作级 rollback"),
                ("风险", "可能恢复旧路径，并保留 checkpoint 之后新增或重命名出来的文件"),
                ("预览", f"/rollback project diff {checkpoint_ref}"),
                ("确认", f"/rollback project {checkpoint_ref}{target} --yes"),
            ]),),
        ))

    def render_checkpoints_status(self, status: dict) -> None:
        total_mb = float(status.get("total_size_bytes", 0) or 0) / (1024 * 1024)
        self.render_panel_model(PanelModel(
            title="M-Claw 文件安全层 / Checkpoints",
            namespace="file_safety",
            blocks=(key_value_block([
                ("存储目录", str(status.get("base") or "")),
                ("占用空间", f"{total_mb:.1f} MB"),
                ("工作区数", f"{status.get('project_count', 0)} 个已跟踪工作区"),
                ("说明", "这里是全局 checkpoint store，不只统计当前目录"),
                ("回滚入口", "/rollback"),
            ]),),
        ))

    def render_checkpoints_prune(self, result: dict) -> None:
        detail = result.get("result") or {}
        self.render_panel_model(PanelModel(
            title="M-Claw 文件安全层 / Checkpoint 清理",
            namespace="file_safety",
            blocks=(key_value_block([
                ("扫描工作区", str(detail.get("scanned", 0))),
                ("删除孤儿项", str(detail.get("deleted_orphan", 0))),
                ("删除过期项", str(detail.get("deleted_stale", 0))),
                ("释放空间", self._format_bytes(int(detail.get("bytes_freed", 0) or 0))),
                ("错误", str(detail.get("errors", 0))),
            ]),),
        ))

    def render_checkpoints_clear_prompt(self) -> None:
        self.render_panel_model(PanelModel(
            title="M-Claw 文件安全层 / 危险操作",
            namespace="file_safety",
            tone="warning",
            blocks=(key_value_block([
                ("状态", "清理未执行"),
                ("风险", "会删除所有 checkpoint，无法再用原始 checkpoint 恢复"),
                ("确认", "/checkpoints clear --yes"),
                ("建议", "日常清理使用 /checkpoints prune"),
            ]),),
        ))

    def render_checkpoints_clear_result(self, result: dict) -> None:
        self.render_panel_model(PanelModel(
            title="M-Claw 文件安全层 / Checkpoint 清理",
            namespace="file_safety",
            tone="success",
            blocks=(key_value_block([
                ("已删除", "是" if result.get("deleted") else "否"),
                ("释放空间", self._format_bytes(int(result.get("bytes_freed", 0) or 0))),
            ]),),
        ))

    def render_notice(self, title: str, message: str, *, detail: str | None = None, kind: str = "info") -> None:
        self.render_panel_model(notice_panel(
            f"M-Claw 文件安全层 / {title}",
            message,
            detail=detail,
            tone=kind if kind in {"success", "warning", "danger", "info"} else "info",
            namespace="file_safety",
        ))

    def render_diff_result(self, result: dict) -> None:
        """Render rollback diff output with context impact and bounded text length."""
        if not result.get("success"):
            self.render_notice("Diff", str(result.get("error") or "差异生成失败"), kind="danger")
            return
        items = []
        impact = result.get("context_impact") or {}
        if impact.get("available"):
            items.append(key_value_block([
                ("上下文影响", f"session={impact.get('session_id')} turn={impact.get('turn_id')} marker={impact.get('marker_message_id')}"),
            ]))
            items.append(spacer_block())
        stat = result.get("stat") or ""
        diff = result.get("diff") or ""
        if not stat and not diff:
            self.render_notice("Diff", "该操作之后没有可显示的文件变化。")
            return
        if stat:
            items.extend([section_block("统计"), text_block(stat)])
        if diff:
            diff_lines = diff.splitlines()
            shown = diff_lines[:120]
            if items:
                items.append(spacer_block())
            items.extend([section_block("差异"), text_block("\n".join(shown))])
            if len(diff_lines) > len(shown):
                items.append(text_block(f"... 还有 {len(diff_lines) - len(shown)} 行，已截断显示", muted=True))
        self.render_panel_model(PanelModel(
            title="M-Claw 文件安全层 / Diff",
            namespace="file_safety",
            blocks=tuple(items),
        ))

    def render_project_restore_result(self, result: dict, *, file_path: str | None = None) -> None:
        if file_path:
            message = f"已从 checkpoint {result['restored_to']} 恢复文件 {file_path}: {result['reason']}"
        else:
            message = f"已恢复到 checkpoint {result['restored_to']}: {result['reason']}"
        self.render_panel_model(PanelModel(
            title="M-Claw 文件安全层 / Checkpoint 恢复",
            namespace="file_safety",
            tone="success",
            blocks=(key_value_block([
                ("结果", message),
                ("反悔", "已自动保存 pre-rollback snapshot，可用于反悔。"),
            ]),),
        ))

    def render_rollback_result(self, result: dict, *, grouped: bool = True, ref_label: str = "") -> None:
        """Render the final rollback outcome, including conflicts and context changes."""
        rows = []
        if grouped:
            group = result.get("group") or {}
            count = len(group.get("operations") or []) or result.get("operation_count", 1)
            rows.append(("结果", f"已撤销第 {ref_label} 条变更，共处理 {count} 个文件操作。"))
        else:
            operation_id = (result.get("operation") or {}).get("operation_id")
            rows.append(("结果", f"已撤销底层操作 {operation_id}。"))
        if result.get("rollback_id"):
            rows.append(("反悔", "输入 /rollback undo 即可恢复撤销前状态。"))
        backups = result.get("conflict_backups") or []
        if backups:
            rows.append(("冲突备份", f"检测到后续改动，已保留 {len(backups)} 份冲突备份。"))
        context = result.get("context") or {}
        if not context.get("skipped"):
            rows.append(("上下文", f"已同步整理聊天上下文：隐藏 {context.get('invalidated', 0)} 条相关消息。"))
        elif context.get("reason") and context.get("reason") != "missing marker":
            rows.append(("上下文", f"聊天上下文未处理：{context.get('reason')}。"))
        self.render_panel_model(PanelModel(
            title="M-Claw 文件安全层 / Rollback 结果",
            namespace="file_safety",
            tone="success",
            blocks=(key_value_block(rows),),
        ))

    @staticmethod
    def _state_text(state: str) -> str:
        labels = {
            "current": "可撤销",
            "undone": "已撤销",
            "changed": "后续改动",
            "partial": "部分撤销",
        }
        return labels.get(str(state), str(state))

    @staticmethod
    def _state_role(state: str) -> str:
        return {
            "current": "info",
            "undone": "success",
            "changed": "warning",
            "partial": "warning",
        }.get(str(state), "muted")

    @staticmethod
    def _short_time(value: str) -> str:
        text = str(value or "")
        if "T" in text:
            date, rest = text.split("T", 1)
            clock = rest.split("+", 1)[0].split("-", 1)[0][:5]
            return f"{date[-5:]} {clock}" if date else clock
        return text[:16]

    @staticmethod
    def _format_bytes(value: int) -> str:
        size = max(0, int(value or 0))
        if size < 1024:
            return f"{size} B"
        if size < 1024 * 1024:
            return f"{size / 1024:.1f} KB"
        return f"{size / (1024 * 1024):.1f} MB"

    def _rollback_group_label(self, operations: list) -> str:
        """Summarize a group using user-facing action and target labels."""
        if not operations:
            return "未知变更"
        actions = [self._action_label(op) for op in operations]
        targets = []
        for op in operations:
            for target in op.get("targets") or []:
                name = Path(target.get("path") or "").name
                if name and name not in targets:
                    targets.append(name)
        action = actions[0] if actions and len(set(actions)) == 1 else "修改"
        target_label = "、".join(targets[:3]) if targets else "文件"
        if len(targets) > 3:
            target_label += f" 等 {len(targets)} 个文件"
        suffix = f"（{len(operations)} 个操作）" if len(operations) > 1 else ""
        return f"{action} {target_label}{suffix}"

    @staticmethod
    def _target_summary(operation: dict) -> str:
        targets = operation.get("targets") or []
        if not targets:
            return operation.get("workspace") or operation.get("cwd") or "<workspace>"
        labels = [Path(t.get("path") or "").name or t.get("path") for t in targets[:3]]
        suffix = f" +{len(targets) - 3}" if len(targets) > 3 else ""
        return "、".join(labels) + suffix

    @staticmethod
    def _action_label(operation: dict) -> str:
        targets = operation.get("targets") or []
        if targets and all((t.get("before") or {}).get("exists") is False and (t.get("after") or {}).get("exists") is True for t in targets):
            return "创建"
        if targets and all((t.get("before") or {}).get("exists") is True and (t.get("after") or {}).get("exists") is False for t in targets):
            return "删除"
        mapping = {
            "write_file": "修改",
            "edit_file": "修改",
            "patch": "修改",
            "overwrite": "覆盖",
            "delete": "删除",
            "directory_delete": "删除目录",
            "move": "移动",
            "copy": "复制",
            "write": "写入",
        }
        return mapping.get(str(operation.get("action") or operation.get("tool")), "修改")
