"""Operation-first rollback coordinator."""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from mclaw.safety.context_rollback import ContextRollbackManager
from mclaw.safety.operation_journal import OperationJournal, default_journal, inspect_path


class RollbackCoordinator:
    """Coordinate file restore, conflict backup, context rollback, and audit."""

    def __init__(
        self,
        *,
        checkpoint_manager: Any,
        session_db: Any = None,
        agent: Any = None,
        journal: Optional[OperationJournal] = None,
        config: Optional[Dict] = None,
    ):
        self.checkpoint_manager = checkpoint_manager
        self.session_db = session_db
        self.agent = agent
        self.journal = journal or default_journal()
        self.config = config or {}
        self.context = ContextRollbackManager(session_db=session_db, agent=agent)

    def list_operations(self, *, session_id: str = None, workspace: str = None, limit: int = 20) -> List[Dict[str, Any]]:
        return self.journal.list_operations(session_id=session_id, workspace=workspace, limit=limit)

    def list_groups(self, *, session_id: str = None, workspace: str = None, limit: int = 20) -> List[Dict[str, Any]]:
        operations = self.list_operations(session_id=session_id, workspace=workspace, limit=200)
        grouped: List[Dict[str, Any]] = []
        by_key: Dict[str, Dict[str, Any]] = {}
        for operation in operations:
            key = operation.get("turn_id") or operation.get("operation_id") or operation.get("op_id")
            if not key:
                continue
            group = by_key.get(key)
            if group is None:
                group = {
                    "group_id": key,
                    "turn_id": operation.get("turn_id"),
                    "session_id": operation.get("session_id"),
                    "operations": [],
                    "updated_at": operation.get("updated_at") or operation.get("created_at") or "",
                }
                by_key[key] = group
                grouped.append(group)
            group["operations"].append(operation)
        newer_operations: List[Dict[str, Any]] = []
        for group in grouped:
            operations_in_group = group.get("operations") or []
            group["state"] = _group_state(operations_in_group, newer_operations=newer_operations)
            newer_operations.extend(operations_in_group)
        return grouped[:limit]

    def format_groups(self, groups: List[Dict[str, Any]], workspace: str = "") -> str:
        if not groups:
            lines = [
                "╭─ M-Claw 文件安全层 / Rollback",
                f"│ 工作区  {workspace}" if workspace else "│ 工作区  当前目录",
                "│ 状态    当前目录没有可撤销的文件操作",
                "╰─ 可继续正常对话",
            ]
            return "\n".join(lines)
        lines = [
            "╭─ M-Claw 文件安全层 / Rollback",
            f"│ 工作区  {workspace}" if workspace else "│ 工作区  当前目录",
            "│ 最近可撤销变更",
            "├─ 变更列表",
        ]
        for idx, group in enumerate(groups, 1):
            ops = group.get("operations") or []
            label = _group_summary(ops)
            when = _short_time(group.get("updated_at") or "")
            count = len(ops)
            suffix = f"（{count} 个操作）" if count > 1 else ""
            state = group.get("state") or _group_state(ops)
            cursor = "  ← 当前已回到这里" if state == "undone" else ""
            lines.append(f"│ {idx:>2}. [{_state_label(state)}] {label}{suffix}  {when}{cursor}")
            if state == "undone":
                lines.append(f"│     恢复这条：/rollback restore {idx}")
        lines.extend([
            "├─ 状态说明",
            "│ [可撤销] 可以撤销这条变更",
            "│ [已撤销] 当前文件已回到这条变更之前",
            "│ [有后续改动] 后面又改过，撤销前建议先看 diff",
            "├─ 常用命令",
            "│ /rollback 1          撤销第 1 条变更",
            "│ /rollback restore 1  恢复第 1 条已撤销变更",
            "│ /rollback undo       恢复最近一条已撤销变更",
            "│ /rollback ops        查看底层操作明细",
            "╰─ 高级：/rollback diff 1、/rollback project、/checkpoints status",
        ])
        return "\n".join(lines)

    def format_operations(self, operations: List[Dict[str, Any]], workspace: str = "") -> str:
        if not operations:
            lines = [
                "╭─ M-Claw 文件安全层 / 底层操作",
                f"│ 工作区  {workspace}" if workspace else "│ 工作区  当前目录",
                "│ 状态    没有底层操作明细",
                "╰─ 普通撤销请使用 /rollback",
            ]
            return "\n".join(lines)
        lines = [
            "╭─ M-Claw 文件安全层 / 底层操作",
            f"│ 工作区  {workspace}" if workspace else "│ 工作区  当前目录",
            "│ 底层操作明细",
            "├─ 操作列表",
        ]
        for idx, op in enumerate(operations, 1):
            targets = _target_summary(op)
            op_id = str(op.get("operation_id") or op.get("op_id") or "")[:8]
            created = str(op.get("updated_at") or op.get("created_at") or "")
            action = _action_label(op)
            context = "可同步上下文" if op.get("message_id_before_turn") is not None else "仅文件"
            lines.append(f"│ {idx:>2}. {op_id}  {created}  {action}  {targets}  [{context}]")
        lines.extend([
            "├─ 命令",
            "│ /rollback op <N>       只撤销某一个底层操作",
            "│ /rollback op diff <N>  查看某一个底层操作差异",
            "╰─ 普通用户建议回到 /rollback 使用组合变更",
        ])
        return "\n".join(lines)

    def get_group(self, ref: str, *, session_id: str = None, workspace: str = None) -> Optional[Dict[str, Any]]:
        ref = str(ref or "").strip()
        if not ref:
            return None
        groups = self.list_groups(session_id=session_id, workspace=workspace, limit=200)
        try:
            idx = int(ref) - 1
            if 0 <= idx < len(groups):
                return groups[idx]
        except ValueError:
            pass
        for group in groups:
            group_id = str(group.get("group_id") or "")
            if group_id == ref or group_id.startswith(ref):
                return group
        return None

    def rollback_group(
        self,
        ref: str,
        *,
        session_id: str = None,
        workspace: str = None,
        context_mode: str = "soft",
    ) -> Dict[str, Any]:
        group = self.get_group(ref, session_id=session_id, workspace=workspace)
        if not group:
            return {"success": False, "error": f"没有找到第 {ref} 条变更"}
        operations = group.get("operations") or []
        state = _group_state(operations)
        if state == "undone":
            return {
                "success": False,
                "already_rolled_back": True,
                "error": f"第 {ref} 条变更已经撤销过了，当前文件已在撤销后的状态。需要反悔请输入 /rollback undo。",
                "group": group,
            }
        if len(operations) == 1:
            op_ref = operations[0].get("operation_id") or operations[0].get("op_id")
            result = self.rollback_operation(op_ref, session_id=session_id, workspace=workspace, context_mode=context_mode)
            result["group"] = group
            return result
        turn_id = group.get("turn_id") or group.get("group_id")
        result = self.rollback_turn(turn_id, session_id=session_id, workspace=workspace, context_mode=context_mode)
        result["group"] = group
        return result

    def restore_group(self, ref: str, *, session_id: str = None, workspace: str = None) -> Dict[str, Any]:
        """Restore a group that is currently marked as already rolled back."""
        group = self.get_group(ref, session_id=session_id, workspace=workspace)
        if not group:
            return {"success": False, "error": f"没有找到第 {ref} 条变更"}
        operations = group.get("operations") or []
        state = _group_state(operations)
        if state != "undone":
            return {
                "success": False,
                "error": f"第 {ref} 条变更当前不是已撤销状态，不需要恢复。要撤销它请输入 /rollback {ref}。",
                "group": group,
            }
        rollback_records = self._rollback_records_for_operations(operations, session_id=session_id, workspace=workspace)
        if not rollback_records:
            return {
                "success": False,
                "error": f"第 {ref} 条变更没有找到可用于恢复的 rollback 事务。",
                "group": group,
            }

        results = []
        for rollback_record in rollback_records:
            result = self._undo_rollback_record(rollback_record, session_id=session_id, workspace=workspace)
            results.append(result)
            if not result.get("success"):
                return {
                    "success": False,
                    "error": result.get("error") or "恢复已撤销变更失败",
                    "group": group,
                    "results": results,
                }
        return {
            "success": True,
            "group": group,
            "operation_count": len(operations),
            "rollback_count": len(rollback_records),
            "results": results,
        }

    def restore_latest_undone_group(self, *, session_id: str = None, workspace: str = None) -> Dict[str, Any]:
        """Restore the newest currently-undone group in the visible rollback list."""
        groups = self.list_groups(session_id=session_id, workspace=workspace, limit=200)
        for idx, group in enumerate(groups, 1):
            if (group.get("state") or _group_state(group.get("operations") or [])) == "undone":
                result = self.restore_group(str(idx), session_id=session_id, workspace=workspace)
                result["group_index"] = idx
                return result
        return {
            "success": False,
            "error": "当前没有已撤销的变更可恢复。",
        }

    def diff_operation(self, ref: str, *, session_id: str = None, workspace: str = None) -> Dict[str, Any]:
        operation = self.journal.get_operation(ref, session_id=session_id, workspace=workspace)
        if not operation:
            return {"success": False, "error": f"Operation '{ref}' not found"}
        if _operation_state(operation) == "undone":
            return {
                "success": False,
                "already_rolled_back": True,
                "error": f"操作 {ref} 已经撤销过了，当前文件已在撤销后的状态。需要反悔请输入 /rollback undo。",
                "operation": operation,
            }
        before_commit = operation.get("before_commit")
        if not before_commit:
            return {"success": False, "error": "Operation has no before checkpoint"}
        work_dir = operation.get("workspace") or operation.get("cwd") or workspace
        result = self.checkpoint_manager.diff(work_dir, before_commit)
        result["operation"] = operation
        result["context_impact"] = self._context_impact(operation)
        return result

    def rollback_operation(
        self,
        ref: str,
        *,
        session_id: str = None,
        workspace: str = None,
        context_mode: str = "soft",
    ) -> Dict[str, Any]:
        operation = self.journal.get_operation(ref, session_id=session_id, workspace=workspace)
        if not operation:
            return {"success": False, "error": f"Operation '{ref}' not found"}
        before_commit = operation.get("before_commit")
        created_targets = _created_targets(operation)
        if not before_commit and not created_targets:
            return {"success": False, "error": "Operation has no before checkpoint"}
        work_dir = operation.get("workspace") or operation.get("cwd") or workspace
        if not work_dir:
            return {"success": False, "error": "Operation has no workspace"}

        backups = self._backup_conflicts(operation)
        rollback_id = f"rollback_{time.strftime('%Y%m%d_%H%M%S')}_{str(operation.get('operation_id') or operation.get('op_id'))[:8]}"
        target_paths = [target.get("path") for target in operation.get("targets") or [] if target.get("path")]
        self.checkpoint_manager.create_checkpoint(
            work_dir,
            f"pre-rollback snapshot ({rollback_id})",
            metadata={
                "rollback_id": rollback_id,
                "rollback_source_operation_id": operation.get("operation_id") or operation.get("op_id"),
            },
            target_paths=target_paths or None,
        )
        pre_commit = (getattr(self.checkpoint_manager, "last_attempt", {}) or {}).get("commit")

        if before_commit:
            restore = self.checkpoint_manager.restore(
                work_dir,
                before_commit,
                file_path=None,
                create_pre_snapshot=False,
            )
            restored_to = before_commit if restore.get("success") else None
        else:
            restore = _delete_created_targets(created_targets)
            restored_to = "created-targets-deleted" if restore.get("success") else None
        context_result = {"skipped": True}
        if restore.get("success"):
            context_result = self.context.apply(
                session_id=session_id or operation.get("session_id"),
                marker_message_id=operation.get("message_id_before_turn"),
                mode=context_mode,
                checkpoint_hash=before_commit or restored_to,
                operation_id=operation.get("operation_id") or operation.get("op_id"),
                turn_id=operation.get("turn_id"),
                metadata={
                    "source_operation_id": operation.get("operation_id") or operation.get("op_id"),
                    "rollback_id": rollback_id,
                    "operation": _slim_operation(operation),
                },
            )
        rollback_record = self.journal.record_rollback(
            source_operation=operation,
            session_id=session_id or operation.get("session_id") or "",
            rollback_id=rollback_id,
            mode=context_mode,
            success=bool(restore.get("success")),
            pre_rollback_commit=pre_commit,
            restored_commit=restored_to,
            context_rollback_id=context_result.get("rollback_id"),
            conflict_backups=backups,
            result=restore,
        )
        return {
            "success": bool(restore.get("success")),
            "error": restore.get("error"),
            "operation": operation,
            "rollback_id": rollback_id,
            "rollback_record": rollback_record,
            "pre_rollback_commit": pre_commit,
            "restored_to": restored_to,
            "context": context_result,
            "conflict_backups": backups,
            "restore": restore,
        }

    def rollback_turn(
        self,
        ref: str,
        *,
        session_id: str = None,
        workspace: str = None,
        context_mode: str = "soft",
    ) -> Dict[str, Any]:
        """Rollback all completed file operations in one turn, newest first."""
        anchor = self.journal.get_operation(ref, session_id=session_id, workspace=workspace)
        turn_id = ref if not anchor else anchor.get("turn_id")
        if not turn_id:
            return {"success": False, "error": f"Turn '{ref}' not found"}
        operations = [
            op for op in self.journal.list_operations(session_id=session_id, workspace=workspace, limit=200)
            if op.get("turn_id") == turn_id and _rollbackable_operation(op)
        ]
        if not operations:
            return {"success": False, "error": f"No rollbackable operations found for turn '{turn_id}'"}
        if _group_state(operations) == "undone":
            return {
                "success": False,
                "already_rolled_back": True,
                "error": f"这一轮变更已经撤销过了，当前文件已在撤销后的状态。需要反悔请输入 /rollback undo。",
                "turn_id": turn_id,
            }

        results = []
        for operation in operations:
            op_ref = operation.get("operation_id") or operation.get("op_id")
            result = self.rollback_operation(
                op_ref,
                session_id=session_id,
                workspace=workspace,
                context_mode="fs-only",
            )
            results.append(result)
            if not result.get("success"):
                return {
                    "success": False,
                    "error": result.get("error") or f"Failed rolling back operation {op_ref}",
                    "turn_id": turn_id,
                    "results": results,
                }

        earliest = sorted(operations, key=lambda op: str(op.get("created_at") or ""))[0]
        context_result = self.context.apply(
            session_id=session_id or earliest.get("session_id"),
            marker_message_id=earliest.get("message_id_before_turn"),
            mode=context_mode,
            checkpoint_hash=earliest.get("before_commit"),
            operation_id=None,
            turn_id=turn_id,
            metadata={
                "turn_id": turn_id,
                "operation_ids": [op.get("operation_id") or op.get("op_id") for op in operations],
                "rollback_scope": "turn",
            },
        )
        return {
            "success": True,
            "turn_id": turn_id,
            "operation_count": len(operations),
            "results": results,
            "context": context_result,
        }

    def undo_rollback(self, ref: str, *, session_id: str = None, workspace: str = None) -> Dict[str, Any]:
        if not ref:
            latest = self.latest_rollback(session_id=session_id, workspace=workspace)
            if not latest:
                return {"success": False, "error": "没有可反悔的 rollback 事务"}
            ref = latest.get("operation_id") or latest.get("op_id")
        rollback_record = self.journal.get_operation(ref, session_id=session_id, workspace=workspace, include_rollbacks=True)
        if not rollback_record or rollback_record.get("action") != "rollback":
            return {"success": False, "error": f"Rollback transaction '{ref}' not found"}
        return self._undo_rollback_record(rollback_record, session_id=session_id, workspace=workspace)

    def _undo_rollback_record(self, rollback_record: Dict[str, Any], *, session_id: str = None, workspace: str = None) -> Dict[str, Any]:
        before_commit = rollback_record.get("before_commit")
        if not before_commit:
            return {"success": False, "error": "Rollback transaction has no pre-rollback checkpoint"}
        work_dir = rollback_record.get("workspace") or rollback_record.get("cwd") or workspace
        restore = self.checkpoint_manager.restore(work_dir, before_commit, create_pre_snapshot=True)
        context_result = {"restored": 0}
        context_id = (rollback_record.get("rollback") or {}).get("context_rollback_id")
        if restore.get("success") and context_id:
            context_result = self.context.restore(context_id, session_id=session_id or rollback_record.get("session_id"))
        return {
            "success": bool(restore.get("success")),
            "error": restore.get("error"),
            "rollback_record": rollback_record,
            "restored_to": before_commit,
            "context": context_result,
            "restore": restore,
        }

    def _rollback_records_for_operations(
        self,
        operations: List[Dict[str, Any]],
        *,
        session_id: str = None,
        workspace: str = None,
    ) -> List[Dict[str, Any]]:
        source_ids = {
            op.get("operation_id") or op.get("op_id")
            for op in operations
            if op.get("operation_id") or op.get("op_id")
        }
        if not source_ids:
            return []
        selected: Dict[str, Dict[str, Any]] = {}
        for record in self.journal.list_operations(
            session_id=session_id,
            workspace=workspace,
            include_rollbacks=True,
            limit=500,
        ):
            if record.get("action") != "rollback" or not record.get("before_commit"):
                continue
            source_id = (record.get("rollback") or {}).get("source_operation_id")
            if source_id in source_ids:
                # list_operations returns newest first. Overwriting leaves the
                # oldest rollback for each source op, which is the one that
                # still contains the user's original post-change state.
                selected[source_id] = record
        return sorted(selected.values(), key=lambda rec: str(rec.get("created_at") or ""), reverse=True)

    def latest_rollback(self, *, session_id: str = None, workspace: str = None) -> Optional[Dict[str, Any]]:
        for record in self.journal.list_operations(
            session_id=session_id,
            workspace=workspace,
            include_rollbacks=True,
            limit=100,
        ):
            if record.get("action") == "rollback":
                return record
        return None

    def _backup_conflicts(self, operation: Dict[str, Any]) -> List[Dict[str, Any]]:
        backups: List[Dict[str, Any]] = []
        for target in operation.get("targets") or []:
            expected = target.get("after") or {}
            path = target.get("path")
            if not path:
                continue
            current = inspect_path(path)
            if _path_state_matches(current, expected):
                continue
            backup = _backup_path(path)
            if backup:
                backups.append({"path": path, "backup": backup, "expected": expected, "current": current})
        return backups

    @staticmethod
    def _context_impact(operation: Dict[str, Any]) -> Dict[str, Any]:
        marker = operation.get("message_id_before_turn")
        return {
            "available": marker is not None,
            "marker_message_id": marker,
            "session_id": operation.get("session_id"),
            "turn_id": operation.get("turn_id"),
        }


def _path_state_matches(current: Dict[str, Any], expected: Dict[str, Any]) -> bool:
    if not expected:
        return True
    if bool(current.get("exists")) != bool(expected.get("exists")):
        return False
    if not current.get("exists"):
        return True
    if current.get("kind") != expected.get("kind"):
        return False
    if current.get("sha256") or expected.get("sha256"):
        return current.get("sha256") == expected.get("sha256")
    return current.get("size") == expected.get("size")


def _operation_state(operation: Dict[str, Any]) -> str:
    targets = operation.get("targets") or []
    if not targets:
        return "unknown"
    before_matches = []
    after_matches = []
    for target in targets:
        path = target.get("path")
        if not path:
            continue
        current = inspect_path(path)
        before_matches.append(_path_state_matches(current, target.get("before") or {}))
        after_matches.append(_path_state_matches(current, target.get("after") or {}))
    if before_matches and all(before_matches):
        return "undone"
    if after_matches and all(after_matches):
        return "current"
    if any(before_matches) or any(after_matches):
        return "partial"
    return "changed"


def _group_state(operations: List[Dict[str, Any]], *, newer_operations: List[Dict[str, Any]] = None) -> str:
    snapshots = _group_target_snapshots(operations)
    if not snapshots:
        return "unknown"
    before_matches = []
    after_matches = []
    for path, pair in snapshots.items():
        current = inspect_path(path)
        before_matches.append(_path_state_matches(current, pair.get("before") or {}))
        after_matches.append(_path_state_matches(current, pair.get("after") or {}))
    if before_matches and all(before_matches):
        if _missing_created_target_was_changed_later(snapshots, newer_operations or []):
            return "changed"
        return "undone"
    if after_matches and all(after_matches):
        return "current"
    if any(before_matches) or any(after_matches):
        return "partial"
    return "changed"


def _missing_created_target_was_changed_later(
    snapshots: Dict[str, Dict[str, Any]],
    newer_operations: List[Dict[str, Any]],
) -> bool:
    missing_created_paths = {
        path
        for path, pair in snapshots.items()
        if (pair.get("before") or {}).get("exists") is False
        and (pair.get("after") or {}).get("exists") is True
        and not inspect_path(path).get("exists")
    }
    if not missing_created_paths:
        return False
    for operation in newer_operations:
        for target in operation.get("targets") or []:
            path = target.get("path")
            if path in missing_created_paths and (target.get("after") or {}).get("exists") is False:
                return True
    return False


def _group_target_snapshots(operations: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    snapshots: Dict[str, Dict[str, Any]] = {}
    # OperationJournal returns newest first. Reversing is more stable than
    # timestamp sorting because multiple file writes can share the same second.
    ordered = list(reversed(operations))
    for operation in ordered:
        for target in operation.get("targets") or []:
            path = target.get("path")
            if not path:
                continue
            if path not in snapshots:
                snapshots[path] = {"before": target.get("before") or {}}
            snapshots[path]["after"] = target.get("after") or {}
    return snapshots


def _state_label(state: str) -> str:
    mapping = {
        "current": "可撤销",
        "undone": "已撤销",
        "partial": "部分撤销",
        "changed": "有后续改动",
    }
    return mapping.get(str(state), "状态未知")


def _backup_path(path_value: str) -> Optional[str]:
    path = Path(path_value)
    if not path.exists():
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.mclaw-conflict-{stamp}")
    try:
        if path.is_dir():
            shutil.copytree(path, backup)
        else:
            shutil.copy2(path, backup)
        return str(backup)
    except OSError:
        return None


def _created_targets(operation: Dict[str, Any]) -> List[Dict[str, Any]]:
    created = []
    for target in operation.get("targets") or []:
        before = target.get("before") or {}
        after = target.get("after") or {}
        if before.get("exists") is False and after.get("exists") is True and target.get("path"):
            created.append(target)
    return created


def _delete_created_targets(targets: List[Dict[str, Any]]) -> Dict[str, Any]:
    deleted = []
    errors = []
    for target in targets:
        path = Path(target.get("path") or "")
        try:
            if path.is_file() or path.is_symlink():
                path.unlink()
                deleted.append(str(path))
            elif path.is_dir():
                shutil.rmtree(path)
                deleted.append(str(path))
            elif not path.exists():
                deleted.append(str(path))
        except OSError as exc:
            errors.append({"path": str(path), "error": str(exc)})
    return {
        "success": not errors,
        "reason": "deleted files created by operation",
        "deleted": deleted,
        "errors": errors,
    }


def _rollbackable_operation(operation: Dict[str, Any]) -> bool:
    return bool(operation.get("before_commit") or _created_targets(operation))


def _target_summary(operation: Dict[str, Any]) -> str:
    targets = operation.get("targets") or []
    if not targets:
        return operation.get("workspace") or operation.get("cwd") or "<workspace>"
    labels = [Path(t.get("path") or "").name or t.get("path") for t in targets[:3]]
    suffix = f" +{len(targets) - 3}" if len(targets) > 3 else ""
    return ", ".join(labels) + suffix


def _group_summary(operations: List[Dict[str, Any]]) -> str:
    if not operations:
        return "未知变更"
    targets = []
    actions = []
    for op in operations:
        actions.append(_action_label(op))
        for target in op.get("targets") or []:
            name = Path(target.get("path") or "").name
            if name and name not in targets:
                targets.append(name)
    action = _dominant_action(actions)
    target_label = "、".join(targets[:3]) if targets else "文件"
    if len(targets) > 3:
        target_label += f" 等 {len(targets)} 个文件"
    return f"{action} {target_label}"


def _dominant_action(actions: List[str]) -> str:
    values = set(actions)
    if len(values) == 1:
        return actions[0]
    if "创建" in values and len(values) == 1:
        return "创建"
    return "修改"


def _action_label(operation_or_action: Any) -> str:
    action = operation_or_action
    if isinstance(operation_or_action, dict):
        before_after = operation_or_action.get("targets") or []
        if before_after and all((t.get("before") or {}).get("exists") is False and (t.get("after") or {}).get("exists") is True for t in before_after):
            return "创建"
        if before_after and all((t.get("before") or {}).get("exists") is True and (t.get("after") or {}).get("exists") is False for t in before_after):
            return "删除"
        action = operation_or_action.get("action") or operation_or_action.get("tool")
    mapping = {
        "write_file": "修改",
        "edit_file": "修改",
        "patch": "修改",
        "delete_file": "删除",
        "overwrite": "覆盖",
        "delete": "删除",
        "directory_delete": "删除目录",
        "move": "移动",
        "copy": "复制",
        "write": "写入",
    }
    return mapping.get(str(action), "修改")


def _short_time(value: str) -> str:
    text = str(value or "")
    if "T" in text:
        return text.split("T", 1)[1][:5]
    return text[:16]


def _slim_operation(operation: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "operation_id": operation.get("operation_id") or operation.get("op_id"),
        "action": operation.get("action"),
        "tool": operation.get("tool"),
        "workspace": operation.get("workspace"),
        "targets": operation.get("targets"),
        "before_commit": operation.get("before_commit"),
    }
