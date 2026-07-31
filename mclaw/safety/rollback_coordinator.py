# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Operation-first rollback coordinator."""

from __future__ import annotations

import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from mclaw.runtime.manager import RuntimeManager
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
        journal: OperationJournal | None = None,
        config: dict | None = None,
    ):
        self.checkpoint_manager = checkpoint_manager
        self.session_db = session_db
        self.agent = agent
        self.journal = journal or default_journal()
        self.config = config or {}
        self.context = ContextRollbackManager(session_db=session_db, agent=agent)

    def list_operations(self, *, session_id: str | None = None, workspace: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        """Return rollback-visible file operations from the journal."""
        operations = self.journal.list_operations(
            session_id=session_id,
            workspace=workspace,
            limit=max(200, limit),
        )
        has_checkpoint = getattr(self.checkpoint_manager, "has_checkpoint", None)
        if not callable(has_checkpoint):
            return operations[:limit]
        available: dict[tuple[str, str], bool] = {}
        unavailable_groups: set[str] = set()
        for operation in operations:
            commit_hash = operation.get("before_commit")
            work_dir = operation.get("workspace") or operation.get("cwd") or workspace
            if not commit_hash or not work_dir:
                continue
            key = (str(work_dir), str(commit_hash))
            if key not in available:
                available[key] = bool(has_checkpoint(*key))
            if not available[key]:
                unavailable_groups.add(str(operation.get("turn_id") or operation.get("operation_id") or ""))
        return [
            operation
            for operation in operations
            if str(operation.get("turn_id") or operation.get("operation_id") or "") not in unavailable_groups
        ][:limit]

    def list_groups(self, *, session_id: str | None = None, workspace: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        """Group operations by turn so user-facing rollback mirrors chat turns."""
        operations = self.list_operations(session_id=session_id, workspace=workspace, limit=200)
        grouped: list[dict[str, Any]] = []
        by_key: dict[str, dict[str, Any]] = {}
        for operation in operations:
            key = operation.get("turn_id") or operation.get("operation_id")
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
        newer_operations: list[dict[str, Any]] = []
        for group in grouped:
            operations_in_group = group.get("operations") or []
            group["state"] = _group_state(operations_in_group, newer_operations=newer_operations)
            newer_operations.extend(operations_in_group)
        return grouped[:limit]

    def get_group(self, ref: str, *, session_id: str | None = None, workspace: str | None = None) -> dict[str, Any] | None:
        """Resolve a rollback group by list index, exact id, or id prefix."""
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

    def recover_restore(
        self,
        intent_id: str | None = None,
        *,
        workspace: str | None = None,
    ) -> dict[str, Any]:
        """Recover files and any paired context transaction from one intent."""
        recover = getattr(self.checkpoint_manager, "recover_restore", None)
        finish = getattr(self.checkpoint_manager, "finish_restore_intent", None)
        if not callable(recover) or not callable(finish):
            return {"success": False, "error": "Checkpoint manager cannot coordinate restore recovery"}
        result = recover(intent_id, working_dir=workspace, defer_completion=True)
        if not result.get("success"):
            return result

        intent = result.get("intent") or {}
        context_recovery = intent.get("context_recovery")
        context_result = {"skipped": True}
        try:
            if context_recovery:
                if not isinstance(context_recovery, dict) or not self.session_db:
                    raise RuntimeError("Restore recovery has no usable session database")
                action = context_recovery.get("action")
                rollback_ids = context_recovery.get("rollback_ids")
                if (
                    action not in {"restore", "reapply"}
                    or not isinstance(rollback_ids, list)
                    or not rollback_ids
                    or not all(isinstance(value, str) and value for value in rollback_ids)
                ):
                    raise RuntimeError("Restore recovery context metadata is invalid")
                context_session_id = context_recovery.get("session_id")
                if context_session_id is not None and not isinstance(context_session_id, str):
                    raise RuntimeError("Restore recovery context session is invalid")
                context_result = (
                    self.context.restore_many(rollback_ids, session_id=context_session_id)
                    if action == "restore"
                    else self.context.reapply_many(rollback_ids, session_id=context_session_id)
                )
        except Exception as exc:
            status_saved = finish(str(intent.get("id") or ""), "failed", error=str(exc))
            return {
                **result,
                "success": False,
                "error": (
                    f"File recovery completed but chat context recovery failed: {exc}"
                    + ("" if status_saved else "; recovery intent status could not be persisted")
                ),
                "context": context_result,
            }

        if not finish(str(intent.get("id") or ""), "recovered"):
            return {
                **result,
                "success": False,
                "error": "Recovery completed but its intent status could not be persisted",
                "context": context_result,
            }
        return {**result, "success": True, "context": context_result}

    def rollback_group(
        self,
        ref: str,
        *,
        session_id: str | None = None,
        workspace: str | None = None,
        context_mode: str = "soft",
    ) -> dict[str, Any]:
        """Rollback one visible group, delegating single-operation groups directly."""
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
            op_ref = operations[0].get("operation_id")
            result = self.rollback_operation(op_ref, session_id=session_id, workspace=workspace, context_mode=context_mode)
            result["group"] = group
            return result
        turn_id = group.get("turn_id") or group.get("group_id")
        result = self.rollback_turn(turn_id, session_id=session_id, workspace=workspace, context_mode=context_mode)
        result["group"] = group
        return result

    def restore_group(self, ref: str, *, session_id: str | None = None, workspace: str | None = None) -> dict[str, Any]:
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

        if len(rollback_records) == 1:
            result = self._undo_rollback_record(
                rollback_records[0],
                session_id=session_id,
                workspace=workspace,
            )
            if not result.get("success"):
                return {
                    "success": False,
                    "error": result.get("error") or "恢复已撤销变更失败",
                    "group": group,
                    "results": [result],
                }
            results = [result]
            context_result = result.get("context") or {"restored": 0}
        else:
            work_dirs = {
                record.get("workspace") or record.get("cwd") or workspace
                for record in rollback_records
            }
            work_dirs.discard(None)
            work_dirs.discard("")
            if len(work_dirs) != 1:
                return {
                    "success": False,
                    "error": "组合恢复必须属于同一个项目工作区。",
                    "group": group,
                }
            work_dir = work_dirs.pop()
            context_session_id = session_id or rollback_records[0].get("session_id")
            context_ids = list(dict.fromkeys(
                (record.get("rollback") or {}).get("context_rollback_id")
                for record in rollback_records
                if (record.get("rollback") or {}).get("context_rollback_id")
            ))
            restores = [{
                "commit_hash": record.get("before_commit"),
                "target_paths": [
                    target.get("path")
                    for target in record.get("targets") or []
                    if target.get("path")
                ],
            } for record in rollback_records]
            batch = self.checkpoint_manager.restore_batch(
                work_dir,
                restores,
                defer_completion=True,
                context_recovery=(
                    {
                        "action": "reapply",
                        "rollback_ids": context_ids,
                        "session_id": context_session_id,
                    }
                    if context_ids
                    else None
                ),
            )
            if not batch.get("success"):
                return {
                    "success": False,
                    "error": batch.get("error") or "恢复已撤销变更失败",
                    "group": group,
                    "results": batch.get("results") or [],
                    "batch": batch,
                }
            try:
                context_result = self.context.restore_many(
                    context_ids,
                    session_id=context_session_id,
                )
            except Exception as exc:
                compensation = self.checkpoint_manager.restore(
                    work_dir,
                    batch["recovery_commit"],
                    target_paths=batch["target_paths"],
                    create_pre_snapshot=False,
                )
                recovered = bool(compensation.get("success"))
                finish_intent = getattr(self.checkpoint_manager, "finish_restore_intent", None)
                if callable(finish_intent):
                    finished = finish_intent(
                        batch.get("restore_intent_id"),
                        "recovered" if recovered else "failed",
                        error=str(exc),
                    )
                    if not finished:
                        compensation["intent_status_error"] = True
                return {
                    "success": False,
                    "error": (
                        f"聊天上下文恢复失败：{exc}；"
                        + (
                            f"文件已恢复到批次开始前的 checkpoint {batch['recovery_commit'][:8]}。"
                            if recovered
                            else f"文件补偿也失败：{compensation.get('error') or 'unknown error'}"
                        )
                    ),
                    "group": group,
                    "results": batch.get("results") or [],
                    "batch": batch,
                    "compensation": compensation,
                }
            finish_intent = getattr(self.checkpoint_manager, "finish_restore_intent", None)
            if not callable(finish_intent) or not finish_intent(
                batch.get("restore_intent_id"),
                "completed",
            ):
                return {
                    "success": False,
                    "error": "文件和聊天上下文已恢复，但恢复事务状态无法持久化。",
                    "group": group,
                    "results": batch.get("results") or [],
                    "batch": batch,
                    "context": context_result,
                }
            results = batch.get("results") or []
        return {
            "success": True,
            "group": group,
            "operation_count": len(operations),
            "rollback_count": len(rollback_records),
            "results": results,
            "context": context_result,
        }

    def restore_latest_undone_group(self, *, session_id: str | None = None, workspace: str | None = None) -> dict[str, Any]:
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

    def diff_operation(self, ref: str, *, session_id: str | None = None, workspace: str | None = None) -> dict[str, Any]:
        """Show the filesystem diff for an operation's pre-mutation checkpoint."""
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
        target_paths = [
            target.get("path")
            for target in operation.get("targets") or []
            if target.get("path")
        ]
        if not target_paths:
            return {"success": False, "error": "Operation has no filesystem targets"}
        result = self.checkpoint_manager.diff(
            work_dir,
            before_commit,
            target_paths=target_paths,
        )
        result["operation"] = operation
        result["context_impact"] = self._context_impact(operation)
        return result

    def rollback_operation(
        self,
        ref: str,
        *,
        session_id: str | None = None,
        workspace: str | None = None,
        context_mode: str = "soft",
        recovery_commit: str | None = None,
        recovery_target_paths: list[str] | None = None,
        recovery_intent_id: str | None = None,
    ) -> dict[str, Any]:
        """Restore one operation's filesystem targets and invalidate affected context."""
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

        target_paths = [target.get("path") for target in operation.get("targets") or [] if target.get("path")]
        if not target_paths:
            return {
                "success": False,
                "error": "Operation has no filesystem targets; use project rollback for a full checkpoint restore",
            }
        try:
            path_policy = RuntimeManager.current().paths
        except Exception as exc:
            return {"success": False, "error": f"PathPolicy could not validate rollback targets: {exc}"}
        created_paths = {target.get("path") for target in created_targets}
        for path in target_paths:
            action = "delete" if path in created_paths or not before_commit else "overwrite"
            decision = path_policy.check(action, path)
            if not decision.allowed:
                return {"success": False, "error": decision.error_message(), "operation": operation}

        try:
            backups = self._backup_conflicts(operation)
        except OSError as exc:
            return {
                "success": False,
                "error": f"Conflict backup failed; rollback was not started: {exc}",
                "operation": operation,
            }
        rollback_id = f"rollback_{time.strftime('%Y%m%d_%H%M%S')}_{str(operation.get('operation_id') or '')[:8]}"

        if before_commit:
            restore = self.checkpoint_manager.restore(
                work_dir,
                before_commit,
                target_paths=target_paths,
                create_pre_snapshot=recovery_commit is None,
                recovery_commit=recovery_commit,
                recovery_target_paths=recovery_target_paths,
                recovery_intent_id=recovery_intent_id,
            )
            pre_commit = restore.get("pre_restore_commit") or recovery_commit
            restored_to = before_commit if restore.get("success") else None
        else:
            pre_commit = recovery_commit
            if pre_commit is None:
                pre_commit, checkpoint_error = _create_required_checkpoint(
                    self.checkpoint_manager,
                    work_dir,
                    f"pre-rollback snapshot ({rollback_id})",
                    {
                        "rollback_id": rollback_id,
                        "rollback_source_operation_id": operation.get("operation_id"),
                    },
                    target_paths,
                )
                if checkpoint_error:
                    return {
                        "success": False,
                        "error": checkpoint_error,
                        "operation": operation,
                        "conflict_backups": backups,
                    }
            restore = _delete_created_targets(created_targets)
            if not restore.get("success"):
                compensation = self.checkpoint_manager.restore(
                    work_dir,
                    pre_commit,
                    target_paths=target_paths,
                    create_pre_snapshot=False,
                )
                restore["compensation"] = compensation
                restore["recovered"] = bool(compensation.get("success"))
                if restore["recovered"]:
                    restore["error"] = (
                        f"Could not delete every created target; restored the pre-rollback state "
                        f"from checkpoint {pre_commit[:8]}"
                    )
                else:
                    restore["error"] = (
                        f"Could not delete every created target; automatic recovery from checkpoint "
                        f"{pre_commit[:8]} also failed: "
                        f"{compensation.get('error') or 'unknown error'}"
                    )
            restored_to = "created-targets-deleted" if restore.get("success") else None
        context_result = {"skipped": True}
        if restore.get("success"):
            context_result = self.context.apply(
                session_id=session_id or operation.get("session_id"),
                marker_message_id=operation.get("message_id_before_turn"),
                mode=context_mode,
                checkpoint_hash=before_commit or restored_to,
                operation_id=operation.get("operation_id"),
                turn_id=operation.get("turn_id"),
                metadata={
                    "source_operation_id": operation.get("operation_id"),
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
        session_id: str | None = None,
        workspace: str | None = None,
        context_mode: str = "soft",
    ) -> dict[str, Any]:
        """Rollback all completed file operations in one turn, newest first."""
        anchor = self.journal.get_operation(ref, session_id=session_id, workspace=workspace)
        turn_id = ref if not anchor else anchor.get("turn_id")
        if not turn_id:
            return {"success": False, "error": f"Turn '{ref}' not found"}
        operations = [
            op for op in self.list_operations(session_id=session_id, workspace=workspace, limit=200)
            if op.get("turn_id") == turn_id and _rollbackable_operation(op)
        ]
        if not operations:
            return {"success": False, "error": f"No rollbackable operations found for turn '{turn_id}'"}
        if _group_state(operations) == "undone":
            return {
                "success": False,
                "already_rolled_back": True,
                "error": "这一轮变更已经撤销过了，当前文件已在撤销后的状态。需要反悔请输入 /rollback undo。",
                "turn_id": turn_id,
            }

        earliest = sorted(operations, key=lambda op: str(op.get("created_at") or ""))[0]
        context_session_id = session_id or earliest.get("session_id")
        context_rollback_id = None
        if (
            str(context_mode or "soft").lower() not in {"off", "fs-only"}
            and earliest.get("message_id_before_turn") is not None
        ):
            context_rollback_id = f"ctxrb_turn_{uuid.uuid4().hex}"

        recovery_commit = None
        recovery_work_dir = None
        recovery_intent_id = None
        recovery_targets = list(dict.fromkeys(
            target.get("path")
            for operation in operations
            for target in operation.get("targets") or []
            if target.get("path")
        ))
        if len(operations) > 1:
            work_dirs = {
                operation.get("workspace") or operation.get("cwd") or workspace
                for operation in operations
            }
            work_dirs.discard(None)
            work_dirs.discard("")
            if len(work_dirs) != 1 or not recovery_targets:
                return {
                    "success": False,
                    "error": "Rollback turn does not have one recoverable workspace",
                    "turn_id": turn_id,
                }
            recovery_work_dir = work_dirs.pop()
            begin_intent = getattr(self.checkpoint_manager, "begin_restore_intent", None)
            if not callable(begin_intent):
                return {
                    "success": False,
                    "error": "Checkpoint manager cannot persist a turn recovery intent",
                    "turn_id": turn_id,
                }
            intent_result = begin_intent(
                recovery_work_dir,
                target_commits=[
                    operation.get("before_commit")
                    for operation in operations
                    if operation.get("before_commit")
                ],
                target_paths=recovery_targets,
                kind="rollback-turn",
                context_recovery=(
                    {
                        "action": "restore",
                        "rollback_ids": [context_rollback_id],
                        "session_id": context_session_id,
                    }
                    if context_rollback_id
                    else None
                ),
            )
            if not intent_result.get("success"):
                return {
                    "success": False,
                    "error": intent_result.get("error") or "Could not persist turn recovery intent",
                    "turn_id": turn_id,
                }
            recovery_commit = intent_result["recovery_commit"]
            recovery_targets = intent_result["target_paths"]
            recovery_intent_id = intent_result["intent_id"]

        finish_intent = getattr(self.checkpoint_manager, "finish_restore_intent", None)

        def finish_turn_intent(status: str, error: str | None = None) -> bool:
            if not recovery_intent_id:
                return True
            return bool(
                callable(finish_intent)
                and finish_intent(recovery_intent_id, status, error=error)
            )

        results = []

        def compensate_files() -> dict[str, Any] | None:
            if recovery_commit and recovery_work_dir:
                return self.checkpoint_manager.restore(
                    recovery_work_dir,
                    recovery_commit,
                    target_paths=recovery_targets,
                    create_pre_snapshot=False,
                )
            if len(operations) == 1 and results:
                pre_commit = results[0].get("pre_rollback_commit")
                work_dir = operations[0].get("workspace") or operations[0].get("cwd") or workspace
                targets = [
                    target.get("path")
                    for target in operations[0].get("targets") or []
                    if target.get("path")
                ]
                if pre_commit and work_dir and targets:
                    return self.checkpoint_manager.restore(
                        work_dir,
                        pre_commit,
                        target_paths=targets,
                        create_pre_snapshot=False,
                    )
            return None

        try:
            for operation in operations:
                op_ref = operation.get("operation_id")
                result = self.rollback_operation(
                    op_ref,
                    session_id=session_id,
                    workspace=workspace,
                    context_mode="fs-only",
                    recovery_commit=recovery_commit,
                    recovery_target_paths=recovery_targets,
                    recovery_intent_id=recovery_intent_id,
                )
                results.append(result)
                if result.get("success"):
                    continue
                compensation = compensate_files()
                recovered = bool(compensation and compensation.get("success"))
                error = result.get("error") or f"Failed rolling back operation {op_ref}"
                if compensation:
                    compensation_commit = recovery_commit or result.get("pre_rollback_commit")
                    error += (
                        f"; restored the turn's pre-rollback state"
                        + (
                            f" from checkpoint {compensation_commit[:8]}"
                            if compensation_commit
                            else ""
                        )
                        if recovered
                        else f"; turn recovery also failed: {compensation.get('error') or 'unknown error'}"
                    )
                if not finish_turn_intent("recovered" if recovered else "failed", error):
                    error += "; recovery intent status could not be persisted"
                return {
                    "success": False,
                    "error": error,
                    "turn_id": turn_id,
                    "results": results,
                    "recovery_commit": recovery_commit,
                    "compensation": compensation,
                }
        except InterruptedError as exc:
            finish_turn_intent("interrupted", str(exc))
            raise
        except Exception as exc:
            finish_turn_intent("failed", str(exc))
            raise

        try:
            if context_rollback_id:
                update_context = getattr(self.journal, "update_rollback_context", None)
                for result in results:
                    record = result.get("rollback_record")
                    if not record:
                        continue
                    if callable(update_context):
                        update_context(record, context_rollback_id)
                    else:
                        record.setdefault("rollback", {})["context_rollback_id"] = context_rollback_id
            context_result = self.context.apply(
                session_id=context_session_id,
                marker_message_id=earliest.get("message_id_before_turn"),
                mode=context_mode,
                checkpoint_hash=earliest.get("before_commit"),
                operation_id=None,
                turn_id=turn_id,
                rollback_id=context_rollback_id,
                metadata={
                    "turn_id": turn_id,
                    "operation_ids": [op.get("operation_id") for op in operations],
                    "rollback_scope": "turn",
                },
            )
        except Exception as exc:
            compensation = compensate_files()
            recovered = bool(compensation and compensation.get("success"))
            finish_turn_intent("recovered" if recovered else "failed", str(exc))
            return {
                "success": False,
                "error": (
                    f"Context rollback failed: {exc}; "
                    + (
                        "restored the turn's pre-rollback file state"
                        if recovered
                        else f"file compensation also failed: "
                        f"{(compensation or {}).get('error') or 'unknown error'}"
                    )
                ),
                "turn_id": turn_id,
                "results": results,
                "recovery_commit": recovery_commit,
                "compensation": compensation,
            }
        if not finish_turn_intent("completed"):
            context_compensation = (
                self.context.restore(context_rollback_id, session_id=context_session_id)
                if context_rollback_id
                else {"restored": 0}
            )
            file_compensation = compensate_files()
            return {
                "success": False,
                "error": "Rollback completed but its recovery intent could not be finalized; changes were compensated",
                "turn_id": turn_id,
                "results": results,
                "context_compensation": context_compensation,
                "compensation": file_compensation,
            }
        return {
            "success": True,
            "turn_id": turn_id,
            "operation_count": len(operations),
            "results": results,
            "context": context_result,
        }

    def undo_rollback(self, ref: str, *, session_id: str | None = None, workspace: str | None = None) -> dict[str, Any]:
        """Restore the pre-rollback snapshot for a rollback transaction."""
        if not ref:
            latest = self.latest_rollback(session_id=session_id, workspace=workspace)
            if not latest:
                return {"success": False, "error": "没有可反悔的 rollback 事务"}
            ref = latest.get("operation_id")
        rollback_record = self.journal.get_operation(ref, session_id=session_id, workspace=workspace, include_rollbacks=True)
        if not rollback_record or rollback_record.get("action") != "rollback":
            return {"success": False, "error": f"Rollback transaction '{ref}' not found"}
        return self._undo_rollback_record(rollback_record, session_id=session_id, workspace=workspace)

    def _undo_rollback_record(
        self,
        rollback_record: dict[str, Any],
        *,
        session_id: str | None = None,
        workspace: str | None = None,
    ) -> dict[str, Any]:
        before_commit = rollback_record.get("before_commit")
        if not before_commit:
            return {"success": False, "error": "Rollback transaction has no pre-rollback checkpoint"}
        work_dir = rollback_record.get("workspace") or rollback_record.get("cwd") or workspace
        target_paths = [
            target.get("path")
            for target in rollback_record.get("targets") or []
            if target.get("path")
        ]
        if not target_paths:
            return {"success": False, "error": "Rollback transaction has no filesystem targets"}
        restore = self.checkpoint_manager.restore(
            work_dir,
            before_commit,
            target_paths=target_paths,
            create_pre_snapshot=True,
        )
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
        operations: list[dict[str, Any]],
        *,
        session_id: str | None = None,
        workspace: str | None = None,
    ) -> list[dict[str, Any]]:
        source_ids = {
            op.get("operation_id")
            for op in operations
            if op.get("operation_id")
        }
        if not source_ids:
            return []
        selected: dict[str, dict[str, Any]] = {}
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

    def latest_rollback(self, *, session_id: str | None = None, workspace: str | None = None) -> dict[str, Any] | None:
        """Return the newest rollback transaction visible in the journal."""
        for record in self.journal.list_operations(
            session_id=session_id,
            workspace=workspace,
            include_rollbacks=True,
            limit=100,
        ):
            if record.get("action") == "rollback":
                return record
        return None

    def _backup_conflicts(self, operation: dict[str, Any]) -> list[dict[str, Any]]:
        """Preserve current targets when they no longer match the recorded after-state."""
        backups: list[dict[str, Any]] = []
        for target in operation.get("targets") or []:
            expected = target.get("after") or {}
            path = target.get("path")
            if not path:
                continue
            current = inspect_path(path)
            if _path_state_matches(current, expected):
                continue
            if not current.get("exists"):
                continue
            backup = _backup_path(path)
            if not backup:
                raise OSError(f"target disappeared while backing up {path!r}")
            backups.append({"path": path, "backup": backup, "expected": expected, "current": current})
        return backups

    @staticmethod
    def _context_impact(operation: dict[str, Any]) -> dict[str, Any]:
        marker = operation.get("message_id_before_turn")
        return {
            "available": marker is not None,
            "marker_message_id": marker,
            "session_id": operation.get("session_id"),
            "turn_id": operation.get("turn_id"),
        }


def _path_state_matches(current: dict[str, Any], expected: dict[str, Any]) -> bool:
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


def _operation_state(operation: dict[str, Any]) -> str:
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


def _group_state(
    operations: list[dict[str, Any]],
    *,
    newer_operations: list[dict[str, Any]] | None = None,
) -> str:
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
    snapshots: dict[str, dict[str, Any]],
    newer_operations: list[dict[str, Any]],
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


def _group_target_snapshots(operations: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    snapshots: dict[str, dict[str, Any]] = {}
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


def _backup_path(path_value: str) -> str | None:
    path = Path(path_value)
    if not path.exists():
        return None
    stamp = f"{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns()}"
    backup = path.with_name(f"{path.name}.mclaw-conflict-{stamp}")
    if path.is_dir():
        shutil.copytree(path, backup)
    else:
        shutil.copy2(path, backup)
    return str(backup)


def _create_required_checkpoint(
    manager: Any,
    working_dir: str,
    reason: str,
    metadata: dict[str, Any],
    target_paths: list[str],
) -> tuple[str | None, str | None]:
    created = manager.create_checkpoint(
        working_dir,
        reason,
        metadata=metadata,
        target_paths=target_paths,
    )
    attempt = getattr(manager, "last_attempt", {}) or {}
    commit = attempt.get("commit")
    if created and attempt.get("status") == "taken" and commit:
        list_checkpoints = getattr(manager, "list_checkpoints", None)
        if callable(list_checkpoints):
            retained = {
                item.get("hash")
                for item in list_checkpoints(working_dir)
            }
            if commit not in retained:
                return None, "Pre-rollback checkpoint was not retained by checkpoint pruning"
        return commit, None
    detail = attempt.get("detail") or attempt.get("status") or "unknown error"
    return None, f"Could not create pre-rollback checkpoint: {detail}"


def _created_targets(operation: dict[str, Any]) -> list[dict[str, Any]]:
    created = []
    for target in operation.get("targets") or []:
        before = target.get("before") or {}
        after = target.get("after") or {}
        if before.get("exists") is False and after.get("exists") is True and target.get("path"):
            created.append(target)
    return created


def _delete_created_targets(targets: list[dict[str, Any]]) -> dict[str, Any]:
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


def _rollbackable_operation(operation: dict[str, Any]) -> bool:
    return bool(operation.get("before_commit") or _created_targets(operation))


def _slim_operation(operation: dict[str, Any]) -> dict[str, Any]:
    return {
        "operation_id": operation.get("operation_id"),
        "action": operation.get("action"),
        "tool": operation.get("tool"),
        "workspace": operation.get("workspace"),
        "targets": operation.get("targets"),
        "before_commit": operation.get("before_commit"),
    }
