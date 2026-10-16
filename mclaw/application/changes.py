# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Session-worker file recovery, with previews bound to actual current state."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
import uuid
from contextlib import ExitStack
from pathlib import Path

from mclaw.application.sessions import ApplicationError
from mclaw.safety.operation_journal import inspect_path
from mclaw.safety.rollback_coordinator import (
    RollbackCoordinator, _group_target_snapshots, _path_state_matches,
)


class ChangeService:
    """Run only at the owning worker's idle boundary, including context refresh."""

    def __init__(self, home: Path, session: dict, coordinator: RollbackCoordinator):
        self.home, self.session, self.coordinator = Path(home), session, coordinator
        self.workspace = str(Path(session["workspace"]).resolve())
        self.scope = {"session_id": session["id"], "workspace": self.workspace}
        self.tokens: dict[str, dict] = {}

    def _rows(self):
        db = self.coordinator.session_db
        return db.get_messages(self.session["id"], include_invalidated=True) if db else []

    def _groups(self):
        return self.coordinator.list_groups(**self.scope, limit=None)

    def status(self):
        settings = self.coordinator.config.get("checkpoints") or {}
        return self.coordinator.checkpoint_manager.workspace_maintenance(self.workspace, retention_days=int(settings.get("prune_retention_days", 30)))

    def _files(self, group: dict):
        result = []
        for path, pair in _group_target_snapshots(group["operations"]).items():
            before, after = pair.get("before") or {}, pair.get("after") or {}
            current = inspect_path(path)
            result.append({"path": path, "change": "created" if not before.get("exists") else "deleted" if not after.get("exists") else "modified",
                           "current": current, "conflict": current.get("kind") == "directory" or not _path_state_matches(current, before if group["state"] == "undone" else after)})
        return result

    def _direct(self, group: dict, groups: list) -> bool:
        latest_turn = next((row.get("turn_id") for row in reversed(self._rows())
                            if row.get("turn_id") and not row.get("invalidated_at") and row.get("role") in {"user", "assistant"}), None)
        return bool(groups and group["group_id"] == groups[0]["group_id"] and group["state"] == "current"
                    and (not latest_turn or group.get("turn_id") == latest_turn)
                    and not any(item["conflict"] for item in self._files(group)))

    def list(self) -> dict:
        groups = self._groups()
        return {"groups": [{"id": group["group_id"], "turn_id": group.get("turn_id"),
                            "state": group["state"], "updated_at": group.get("updated_at"),
                            "operation_count": len(group["operations"]),
                            "operations": [{"id": op["operation_id"], "tool": op.get("tool"), "action": op.get("action")} for op in group["operations"]],
                            "files": self._files(group), "direct_undo": self._direct(group, groups)} for group in groups],
                "checkpoints": [{key: cp.get(key) for key in ("hash", "short_hash", "timestamp", "reason", "files_changed", "insertions", "deletions")}
                                for cp in self.coordinator.checkpoint_manager.list_checkpoints(self.workspace)],
                "recoveries": [{"id": item["id"], "status": item.get("status"), "created_at": item.get("created_at"),
                                "target_paths": item.get("target_paths", [])}
                               for item in self.coordinator.checkpoint_manager.list_restore_intents(self.workspace)
                               if (item.get("context_recovery") or {}).get("session_id") in {None, self.session["id"]}]}

    def _describe(self, request: dict) -> dict:
        action = request.get("action", "undo")
        if action not in {"undo", "restore", "checkpoint", "recover", "prune", "clear"}:
            raise ApplicationError("INVALID_RECOVERY", "恢复操作无效")
        groups = self._groups()
        group, checkpoint, chunks = None, None, []
        if action in {"prune", "clear"}:
            settings = self.coordinator.config.get("checkpoints") or {}
            checkpoint = self.coordinator.checkpoint_manager.workspace_maintenance(self.workspace, action,
                               retention_days=int(settings.get("prune_retention_days", 30)))
            files = []
            impact = {"mode": "soft", "scope": "turn", "available": False, "message_count": 0}
            chunks.append("将清理当前工作区所列检查点；对应历史变更将失去这些恢复点。对话记录和工作区文件保留。")
        elif action == "recover":
            intent = next((item for item in self.coordinator.checkpoint_manager.list_restore_intents(self.workspace)
                           if item["id"] == request.get("intent_id") and (item.get("context_recovery") or {}).get("session_id") in {None, self.session["id"]}), None)
            if not intent:
                raise ApplicationError("RECOVERY_NOT_FOUND", "待恢复事务不存在或已处理", 404)
            files = [{"path": str(Path(self.workspace) / path), "change": "modified",
                      "current": inspect_path(str(Path(self.workspace) / path)), "conflict": True} for path in intent.get("target_paths", [])]
            diff = self.coordinator.checkpoint_manager.diff(self.workspace, intent["recovery_commit"], target_paths=[item["path"] for item in files], reverse=True)
            if not diff.get("success"):
                raise ApplicationError("DIFF_UNAVAILABLE", "中断恢复差异无法读取，请检查恢复点", 409)
            chunks.append(diff.get("diff") or "")
            checkpoint = intent
            impact = {"mode": "soft", "scope": "turn", "available": bool(intent.get("context_recovery")), "message_count": 0}
        elif action == "checkpoint":
            checkpoint = next((item for item in self.coordinator.checkpoint_manager.list_checkpoints(self.workspace)
                               if item["hash"] == request.get("checkpoint_hash")), None)
            if not checkpoint:
                raise ApplicationError("CHECKPOINT_NOT_FOUND", "恢复点不存在或已清理", 404)
            metadata = checkpoint.get("metadata") or {}
            plan = self.coordinator.checkpoint_manager.describe_restore(self.workspace, checkpoint["hash"])
            if not plan.get("success"):
                raise ApplicationError("RECOVERY_UNAVAILABLE", "恢复点的实际文件范围无法读取", 409)
            checkpoint = {**checkpoint, "restore_paths": plan["target_paths"]}
            files = [{"path": path, "change": "modified", "current": inspect_path(path), "conflict": True} for path in plan["target_paths"]]
            diff = self.coordinator.checkpoint_manager.diff(self.workspace, checkpoint["hash"], reverse=True)
            if not diff.get("success"):
                raise ApplicationError("DIFF_UNAVAILABLE", "恢复差异无法读取，请检查恢复点", 409)
            chunks.append(diff.get("diff") or "")
            marker = metadata.get("message_id_before_turn") if metadata.get("session_id") == self.session["id"] else None
            impact = {"mode": "soft", "scope": "tail", "available": marker is not None,
                      "message_count": sum(row["id"] > marker and not row.get("invalidated_at") for row in self._rows()) if marker is not None else 0}
        else:
            group = next((item for item in groups if item["group_id"] == request.get("group_id")), None)
            if not group:
                raise ApplicationError("CHANGE_NOT_FOUND", "回合变更不存在或恢复点已清理", 404)
            if (action == "restore") != (group["state"] == "undone"):
                raise ApplicationError("CHANGE_STATE_CHANGED", "文件状态已改变，请刷新变更列表", 409)
            files = self._files(group)
            operations = group["operations"]
            records = self.coordinator._rollback_records_for_operations(operations, **self.scope) if action == "restore" else operations
            if action == "restore" and not records:
                raise ApplicationError("RECOVERY_UNAVAILABLE", "没有可恢复的撤销记录", 409)
            # Each path compares against its earliest state in the selected
            # transaction; subsequent writes in that turn never hide it.
            seen = set()
            for operation in reversed(records):
                for target in operation.get("targets") or []:
                    path = target.get("path")
                    if not path or path in seen:
                        continue
                    seen.add(path)
                    commit = operation.get("before_commit")
                    if commit:
                        diff = self.coordinator.checkpoint_manager.diff(self.workspace, commit, target_paths=[path], reverse=True)
                        if not diff.get("success"):
                            raise ApplicationError("DIFF_UNAVAILABLE", "恢复差异无法读取，请检查恢复点", 409)
                        chunks.append(diff.get("diff") or "")
                    else:
                        chunks.append(f"{path}\n新建文件；撤销会删除此路径。\n")
            impact = {"mode": "soft", "scope": "turn", "turn_id": group.get("turn_id"),
                      "available": any(op.get("message_id_before_turn") is not None for op in operations),
                      "message_count": sum(row.get("turn_id") == group.get("turn_id")
                                           and bool(row.get("invalidated_at")) == (action == "restore") for row in self._rows())}
        full_diff = "\n".join(chunks)
        return {"action": action, "group_id": request.get("group_id"), "checkpoint_hash": request.get("checkpoint_hash"), "intent_id": request.get("intent_id"),
                "workspace": self.workspace, "device": "local", "files": files, "diff": full_diff[:200000], "diff_truncated": len(full_diff) > 200000,
                "conflicts": [item["path"] for item in files if item["conflict"]], "context_impact": impact,
                "checkpoint_maintenance": checkpoint if action in {"prune", "clear"} else None,
                "backup_policy": "恢复前保存恢复点；回合撤销对不符合原状态的文件保留冲突副本。",
                "_diff_digest": hashlib.sha256(full_diff.encode()).hexdigest(),
                "_group": group, "_checkpoint": checkpoint, "_direct": bool(group and action == "undo" and self._direct(group, groups))}

    def _fingerprint(self, description: dict) -> str:
        from mclaw.tools.file_concurrency import file_version

        records = self.coordinator.journal.list_operations(**self.scope, include_rollbacks=True, limit=None)
        directories = []
        for item in description["files"]:
            root = Path(item["path"])
            if root.is_dir():
                for path in sorted(root.rglob("*")):
                    directories.append((str(path), str(path.lstat()) if path.is_symlink() else file_version(path)))
        value = [description, records, directories, [(row["id"], row.get("invalidated_at"), row.get("turn_id")) for row in self._rows()]]
        return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()

    def preview(self, request: dict) -> dict:
        description = self._describe(request)
        self.tokens = {key: value for key, value in self.tokens.items() if value["expires"] > time.monotonic()}
        token = secrets.token_urlsafe(32)
        self.tokens[token] = {"fingerprint": self._fingerprint(description), "expires": time.monotonic() + 600,
                              "workspace_identity": self._workspace_identity()}
        return {key: value for key, value in description.items() if not key.startswith("_")} | {"confirmation_token": token}

    def _workspace_identity(self) -> tuple:
        path = Path(self.workspace)
        try:
            resolved = path.resolve(strict=True)
            stat = path.stat()
        except OSError as exc:
            raise ApplicationError("PREVIEW_EXPIRED", "工作目录已变化，请重新查看恢复范围", 409) from exc
        if not resolved.is_dir() or os.path.normcase(str(resolved)) != os.path.normcase(self.workspace):
            raise ApplicationError("PREVIEW_EXPIRED", "工作目录指向已变化，请重新查看恢复范围", 409)
        return os.path.normcase(str(resolved)), stat.st_dev, stat.st_ino

    def trust(self, request: dict) -> dict:
        """Persist only an explicit confirmation of this preview's directory."""
        from mclaw.cli.runtime.workspace_trust import ensure_workspace_trusted
        from mclaw.system.lock import file_lock

        workspace = request.get("workspace")
        if not isinstance(workspace, str) or os.path.normcase(workspace) != os.path.normcase(self.workspace):
            raise ApplicationError("WORKSPACE_MISMATCH", "确认的工作目录与恢复预览不一致", 409)
        confirmation = self.tokens.get(str(request.get("confirmation_token") or ""))
        with file_lock(self.home / "config.lock"):
            if not confirmation or confirmation["expires"] < time.monotonic() or confirmation["workspace_identity"] != self._workspace_identity():
                raise ApplicationError("PREVIEW_EXPIRED", "工作目录或预览已变化，请重新预览后确认", 409)
            ensure_workspace_trusted(self.workspace, prompt=lambda _path: True)
        return {"workspace_trusted": True}

    def apply(self, request: dict) -> dict:
        from mclaw.tools.file_concurrency import locked_target
        from mclaw.tools.checkpoint_manager import checkpoint_store_guard

        # Use the same target locks as standard writes. External edits remain
        # detectable by the immediate fingerprint recheck and conflict backups.
        description = self._describe(request)
        with ExitStack() as stack:
            for path in sorted({item["path"] for item in description["files"]}):
                stack.enter_context(locked_target(Path(path)))
            stack.enter_context(checkpoint_store_guard())
            description = self._describe(request)
            token = str(request.get("confirmation_token") or "")
            confirmation = self.tokens.get(token)
            if token:
                if not confirmation or confirmation["expires"] < time.monotonic() or confirmation["workspace_identity"] != self._workspace_identity() or confirmation["fingerprint"] != self._fingerprint(description):
                    raise ApplicationError("PREVIEW_EXPIRED", "恢复范围或文件状态已变化，请重新预览后确认", 409)
            elif not description["_direct"]:
                raise ApplicationError("CONFIRMATION_REQUIRED", "此恢复需要先预览影响并确认", 409)
            if description["action"] in {"prune", "clear"}:
                settings = self.coordinator.config.get("checkpoints") or {}
                result = self.coordinator.checkpoint_manager.workspace_maintenance(self.workspace, description["action"],
                                   retention_days=int(settings.get("prune_retention_days", 30)), apply=True)
                self.tokens.clear()
                if not result.get("success"):
                    raise ApplicationError("RECOVERY_FAILED", result.get("error") or "检查点清理未完成，请核对实际恢复点", 409)
                return result
            if description["action"] == "recover":
                result = self.coordinator.recover_restore(description["intent_id"], workspace=self.workspace)
            elif description["action"] == "checkpoint":
                result = self._restore_checkpoint(description["_checkpoint"])
            elif description["action"] == "restore":
                result = self.coordinator.restore_group(description["group_id"], **self.scope)
            else:
                result = self.coordinator.rollback_group(description["group_id"], **self.scope, context_mode="soft")
            self.tokens.clear()
            if not result.get("success"):
                raise ApplicationError("RECOVERY_FAILED", str(result.get("error") or "恢复未完成，请检查恢复记录"), 409)
            if self.coordinator.session_db is not None:
                summary = {"action": description["action"], "group_id": description["group_id"],
                           "checkpoint_hash": description["checkpoint_hash"], "paths": [item["path"] for item in description["files"]],
                           "context": result.get("context") or {}}
                self.coordinator.session_db.append_message(self.session["id"], "system",
                    content="用户已完成文件恢复。请依据当前文件和有效上下文继续，原历史仅保留作记录。恢复结果：" + json.dumps(summary, ensure_ascii=False),
                    operation_id="recovery:" + uuid.uuid4().hex)
            self.coordinator.context.reload_agent_messages(self.session["id"])
            self.coordinator.context.reset_runtime_context()
            return {"success": True, "context": result.get("context") or {},
                    "files_result": {"success": True, "paths": [item["path"] for item in description["files"]],
                                     "operation_count": result.get("operation_count", len((description["_group"] or {}).get("operations", [])))}}

    def _restore_checkpoint(self, checkpoint: dict) -> dict:
        """Pair checkpoint restoration with its existing soft tail context rule."""
        metadata = checkpoint.get("metadata") or {}
        targets = checkpoint["restore_paths"]
        marker = metadata.get("message_id_before_turn") if metadata.get("session_id") == self.session["id"] else None
        rollback_id = "ctxrb_checkpoint_" + uuid.uuid4().hex
        context_recovery = {"action": "restore", "rollback_ids": [rollback_id], "session_id": self.session["id"]} if marker is not None else None
        manager = self.coordinator.checkpoint_manager
        result = manager.restore_batch(self.workspace, [{"commit_hash": checkpoint["hash"], "target_paths": targets}],
                                       defer_completion=True, context_recovery=context_recovery)
        if not result.get("success"):
            return result
        try:
            context = self.coordinator.context.apply(session_id=self.session["id"], marker_message_id=marker,
                        mode="soft", checkpoint_hash=checkpoint["hash"], rollback_id=rollback_id, metadata=metadata, scope="tail")
            if not manager.finish_restore_intent(result["restore_intent_id"], "completed"):
                raise RuntimeError("恢复记录未完成保存")
            return {**result, "context": context}
        except Exception:
            recovery = self.coordinator.recover_restore(result["restore_intent_id"], workspace=self.workspace)
            return {"success": False, "error": "上下文恢复失败，已恢复操作前状态" if recovery.get("success") else "恢复未完成，请在恢复记录中重试", "recovery": recovery}
