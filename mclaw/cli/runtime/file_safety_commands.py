# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Runtime coordination for file safety slash commands."""

from __future__ import annotations

import shlex
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from mclaw.utils import is_truthy_value


@dataclass(frozen=True)
class RuntimeFileSafetyCommandHooks:
    """Host operations used by UI-neutral file safety commands."""

    get_checkpoint_manager: Callable[[], Any]
    create_rollback_coordinator: Callable[[Any], Any]
    resolve_rollback_workspace: Callable[[dict[str, Any]], tuple[str, str | None]]
    resolve_checkpoint_ref: Callable[[str, list[dict[str, Any]]], str | None]
    checkpoint_config: Callable[[], dict[str, Any]]
    store_status: Callable[[], dict[str, Any]]
    prune_checkpoints: Callable[[int, bool], dict[str, Any]]
    clear_checkpoints: Callable[[], dict[str, Any]]
    restore_chat_context: Callable[[dict[str, Any], str], None]
    render_notice: Callable[[str, str, str, str], None]
    render_diff_result: Callable[[dict[str, Any]], None]
    render_rollback_result: Callable[[dict[str, Any], bool, str], None]
    render_rollback_groups: Callable[[list[Any], str], None]
    render_rollback_operations: Callable[[list[Any], str], None]
    render_project_checkpoints: Callable[[list[dict[str, Any]], str], None]
    render_project_restore_prompt: Callable[[str, str], None]
    render_project_restore_result: Callable[[dict[str, Any], str | None], None]
    render_checkpoints_status: Callable[[dict[str, Any]], None]
    render_checkpoints_prune: Callable[[dict[str, Any]], None]
    render_checkpoints_clear_prompt: Callable[[], None]
    render_checkpoints_clear_result: Callable[[dict[str, Any]], None]


class RuntimeFileSafetyCommandCoordinator:
    """Handles `/rollback` and `/checkpoints` without depending on a concrete TUI."""

    def __init__(self, hooks: RuntimeFileSafetyCommandHooks) -> None:
        self.hooks = hooks

    def handle_rollback(self, raw_args: str = "") -> None:
        """Route `/rollback` subcommands through checkpoint and operation rollback APIs."""
        mgr = self.hooks.get_checkpoint_manager()
        if mgr is None:
            self.hooks.render_notice("Rollback", "当前没有可用 agent session。", "", "warning")
            return
        if not getattr(mgr, "enabled", False):
            self.hooks.render_notice("Rollback", "Checkpoint 未启用。请在 config.yaml 中设置 checkpoints.enabled: true", "", "warning")
            return

        options, rollback_args = self.parse_rollback_options(raw_args)
        context_mode = options.get("context") or "soft"
        cwd, option_file_path = self.hooks.resolve_rollback_workspace(options)
        command, rest = self.split_ref_and_rest(rollback_args)
        coordinator = self.hooks.create_rollback_coordinator(mgr)

        if not command:
            groups = coordinator.list_groups(session_id=None, workspace=cwd)
            self.hooks.render_rollback_groups(groups, cwd)
            return

        lower = command.lower()
        if lower == "project":
            self._handle_project_rollback(
                mgr,
                cwd,
                rest,
                option_file_path=option_file_path,
                options=options,
                context_mode=context_mode,
            )
            return

        if lower == "ops":
            self._handle_ops_rollback(coordinator, cwd, rest, context_mode=context_mode, ref_label=command)
            return

        if lower == "undo":
            self._handle_undo(coordinator, cwd, rest)
            return

        if lower == "restore":
            self._handle_restore_group(coordinator, cwd, rest)
            return

        if lower == "turn":
            self._handle_turn_rollback(coordinator, cwd, rest, context_mode=context_mode)
            return

        if lower == "diff":
            self._handle_group_diff(coordinator, cwd, rest)
            return

        result = coordinator.rollback_group(command, session_id=None, workspace=cwd, context_mode=context_mode)
        if not result.get("success"):
            self.hooks.render_notice("Rollback", str(result.get("error") or "撤销失败"), "", "danger")
            return
        self.hooks.render_rollback_result(result, True, command)

    def handle_checkpoints(self, raw_args: str = "") -> None:
        """Route `/checkpoints` maintenance commands through host-provided hooks."""
        args = str(raw_args or "").split()
        subcmd = args[0].lower() if args else "status"

        if subcmd == "status":
            self.hooks.render_checkpoints_status(self.hooks.store_status())
            return

        if subcmd == "prune":
            cp_cfg = self.hooks.checkpoint_config()
            try:
                retention_days = int(cp_cfg.get("prune_retention_days", 30)) if isinstance(cp_cfg, dict) else 30
            except (TypeError, ValueError):
                retention_days = 30
            delete_orphans = is_truthy_value(cp_cfg.get("delete_orphans", True), True) if isinstance(cp_cfg, dict) else True
            self.hooks.render_checkpoints_prune(self.hooks.prune_checkpoints(retention_days, delete_orphans))
            return

        if subcmd == "clear":
            if "--yes" not in args:
                self.hooks.render_checkpoints_clear_prompt()
                return
            self.hooks.render_checkpoints_clear_result(self.hooks.clear_checkpoints())
            return

        self.hooks.render_notice("Checkpoints", "用法: /checkpoints [status|prune|clear --yes]", "", "warning")

    def _handle_project_rollback(
        self,
        mgr: Any,
        cwd: str,
        project_args: str,
        *,
        option_file_path: str | None,
        options: dict[str, Any],
        context_mode: str,
    ) -> None:
        """Handle raw checkpoint restore paths that bypass operation-level rollback."""
        project_args, project_confirmed = self.strip_yes_flag(project_args)
        project_command, project_rest = self.split_ref_and_rest(project_args)
        if not project_command:
            self.hooks.render_project_checkpoints(mgr.list_checkpoints(cwd), cwd)
            return

        if project_command.lower() == "diff":
            target_ref, _ = self.split_ref_and_rest(project_rest)
            if not target_ref:
                self.hooks.render_notice("Rollback", "用法: /rollback project diff <N|hash>", "", "warning")
                return
            checkpoints = mgr.list_checkpoints(cwd)
            if not checkpoints:
                self.hooks.render_notice("Checkpoint", f"未找到 checkpoint: {cwd}", "", "warning")
                return
            target_hash = self.hooks.resolve_checkpoint_ref(target_ref, checkpoints)
            if target_hash:
                self.hooks.render_diff_result(mgr.diff(cwd, target_hash))
            return

        checkpoints = mgr.list_checkpoints(cwd)
        if not checkpoints:
            self.hooks.render_notice("Checkpoint", f"未找到 checkpoint: {cwd}", "", "warning")
            return
        target_hash = self.hooks.resolve_checkpoint_ref(project_command, checkpoints)
        if not target_hash:
            return
        file_path = project_rest or option_file_path or None
        if not (project_confirmed or options.get("yes")):
            self.hooks.render_project_restore_prompt(project_command, file_path or "")
            return
        result = mgr.restore(cwd, target_hash, file_path=file_path)
        if not result.get("success"):
            self.hooks.render_notice("Checkpoint 恢复", str(result.get("error") or "恢复失败"), "", "danger")
            return
        self.hooks.render_project_restore_result(result, file_path)
        self.hooks.restore_chat_context(result.get("metadata") or {}, context_mode)

    def _handle_ops_rollback(self, coordinator: Any, cwd: str, rest: str, *, context_mode: str, ref_label: str) -> None:
        """Handle low-level operation rollback and diff commands."""
        op_command, op_rest = self.split_ref_and_rest(rest)
        if not op_command:
            operations = coordinator.list_operations(session_id=None, workspace=cwd)
            self.hooks.render_rollback_operations(operations, cwd)
            return
        if op_command.lower() == "diff":
            target_ref, _ = self.split_ref_and_rest(op_rest)
            if not target_ref:
                self.hooks.render_notice("Rollback", "用法：/rollback ops diff <编号>", "", "warning")
                return
            self.hooks.render_diff_result(coordinator.diff_operation(target_ref, session_id=None, workspace=cwd))
            return
        result = coordinator.rollback_operation(op_command, session_id=None, workspace=cwd, context_mode=context_mode)
        if not result.get("success"):
            self.hooks.render_notice("Rollback", str(result.get("error") or "撤销失败"), "", "danger")
            return
        self.hooks.render_rollback_result(result, False, ref_label)

    def _handle_undo(self, coordinator: Any, cwd: str, rest: str) -> None:
        """Restore a previously rolled-back group or rollback transaction."""
        target_ref, _ = self.split_ref_and_rest(rest)
        if target_ref and target_ref.isdigit():
            result = coordinator.restore_group(target_ref, session_id=None, workspace=cwd)
            restore_by_group = True
        elif target_ref:
            result = coordinator.undo_rollback(target_ref, session_id=None, workspace=cwd)
            restore_by_group = False
        else:
            result = coordinator.restore_latest_undone_group(session_id=None, workspace=cwd)
            restore_by_group = True
        if not result.get("success"):
            self.hooks.render_notice("Rollback", str(result.get("error") or "反悔失败"), "", "danger")
            return
        if restore_by_group:
            restored_ref = target_ref or str(result.get("group_index") or "")
            self.hooks.render_notice("Rollback", f"已恢复第 {restored_ref} 条已撤销变更。", "", "success")
        else:
            self.hooks.render_notice("Rollback", "已反悔上一次撤销，文件恢复到撤销前状态。", "", "success")
        restored = (result.get("context") or {}).get("restored", 0)
        if restored:
            self.hooks.render_notice("Rollback", f"已恢复聊天上下文 {restored} 条消息。", "", "success")

    def _handle_restore_group(self, coordinator: Any, cwd: str, rest: str) -> None:
        target_ref, _ = self.split_ref_and_rest(rest)
        if not target_ref:
            self.hooks.render_notice("Rollback", "用法：/rollback restore <编号>", "", "warning")
            return
        result = coordinator.restore_group(target_ref, session_id=None, workspace=cwd)
        if not result.get("success"):
            self.hooks.render_notice("Rollback", str(result.get("error") or "恢复失败"), "", "danger")
            return
        self.hooks.render_notice("Rollback", f"已恢复第 {target_ref} 条已撤销变更。", "", "success")

    def _handle_turn_rollback(self, coordinator: Any, cwd: str, rest: str, *, context_mode: str) -> None:
        """Rollback every journaled operation in one conversation turn."""
        target_ref, _ = self.split_ref_and_rest(rest)
        if not target_ref:
            self.hooks.render_notice("Rollback", "用法: /rollback turn <N|turn_id>", "", "warning")
            return
        result = coordinator.rollback_turn(target_ref, session_id=None, workspace=cwd, context_mode=context_mode)
        if not result.get("success"):
            self.hooks.render_notice("Rollback", str(result.get("error") or "整轮撤销失败"), "", "danger")
            return
        self.hooks.render_notice("Rollback", f"已撤销这一轮变更，共处理 {result.get('operation_count', 0)} 个文件操作。", "", "success")
        context = result.get("context") or {}
        if not context.get("skipped"):
            self.hooks.render_notice("Rollback", f"已同步整理聊天上下文：隐藏 {context.get('invalidated', 0)} 条相关消息。", "", "success")

    def _handle_group_diff(self, coordinator: Any, cwd: str, rest: str) -> None:
        """Show a diff for a user-visible rollback group when possible."""
        target_ref, _ = self.split_ref_and_rest(rest)
        if not target_ref:
            self.hooks.render_notice("Rollback", "用法：/rollback diff <编号>", "", "warning")
            return
        group = coordinator.get_group(target_ref, session_id=None, workspace=cwd)
        if group and len(group.get("operations") or []) > 1:
            self.hooks.render_notice(
                "Rollback",
                f"这是一轮组合变更，包含 {len(group.get('operations') or [])} 个底层操作。",
                "要看底层差异，请使用：/rollback ops diff <编号>",
                "warning",
            )
            return
        if group and group.get("operations"):
            op_ref = group["operations"][0].get("operation_id")
            self.hooks.render_diff_result(coordinator.diff_operation(op_ref, session_id=None, workspace=cwd))
        else:
            self.hooks.render_diff_result(coordinator.diff_operation(target_ref, session_id=None, workspace=cwd))

    @staticmethod
    def split_ref_and_rest(raw_args: str) -> tuple[str, str]:
        """Split the first command/reference token from the remaining argument string."""
        parts = (raw_args or "").strip().split(maxsplit=1)
        if not parts:
            return "", ""
        rest = parts[1].strip().strip("'\"") if len(parts) > 1 else ""
        return parts[0].strip("'\""), rest

    @staticmethod
    def parse_rollback_options(raw_args: str) -> tuple[dict[str, Any], str]:
        """Parse rollback flags while preserving the remaining command payload."""
        try:
            tokens = shlex.split(raw_args or "", posix=False)
        except ValueError:
            tokens = (raw_args or "").split()
        options: dict[str, Any] = {}
        remaining: list[str] = []
        i = 0
        while i < len(tokens):
            token = tokens[i].strip()
            lower = token.lower()
            if lower == "--yes":
                options["yes"] = True
                i += 1
                continue
            if lower in {"--context"} and i + 1 < len(tokens):
                options["context"] = tokens[i + 1].strip("'\"").lower()
                i += 2
                continue
            if lower in {"--dir", "--path"} and i + 1 < len(tokens):
                options[lower[2:]] = tokens[i + 1].strip("'\"")
                i += 2
                continue
            remaining = tokens[i:]
            break
        return options, " ".join(remaining).strip()

    @staticmethod
    def strip_yes_flag(raw_args: str) -> tuple[str, bool]:
        """Remove confirmation flags used by project checkpoint restore."""
        try:
            tokens = shlex.split(raw_args or "", posix=False)
        except ValueError:
            tokens = (raw_args or "").split()
        kept = []
        confirmed = False
        for token in tokens:
            if token.strip().lower() == "--yes":
                confirmed = True
            else:
                kept.append(token.strip("'\""))
        return " ".join(kept).strip(), confirmed
