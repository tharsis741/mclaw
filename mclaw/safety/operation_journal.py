# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Operation journal for file-mutating tool calls.

The journal records what M-Claw intended to mutate, what the targets looked
like before execution, and what they looked like afterwards. It is deliberately
small and append-friendly; Shadow Git remains the content storage engine.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from mclaw.constants import get_mclaw_home
from mclaw.tools.cancellation import cancellation_checkpoint
from mclaw.tools.interrupt import get_interrupt_event


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())


def _sha256_file(
    path: Path,
    cancel_event: threading.Event | None = None,
) -> str | None:
    cancel_event = cancel_event or get_interrupt_event()
    try:
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                cancellation_checkpoint(cancel_event)
                h.update(chunk)
        cancellation_checkpoint(cancel_event)
        return h.hexdigest()
    except InterruptedError:
        raise
    except OSError:
        return None


def inspect_path(
    path_value: str,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Capture stable path metadata used to compare rollback states later."""
    cancel_event = cancel_event or get_interrupt_event()
    cancellation_checkpoint(cancel_event)
    path = Path(path_value).expanduser()
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    out: dict[str, Any] = {"path": str(resolved), "exists": resolved.exists()}
    if not out["exists"]:
        return out
    try:
        stat = resolved.stat()
        out.update({
            "size": stat.st_size,
            "mtime": stat.st_mtime,
            "kind": "directory" if resolved.is_dir() else "file",
        })
        if resolved.is_file():
            out["sha256"] = _sha256_file(resolved, cancel_event=cancel_event)
    except InterruptedError:
        raise
    except OSError as exc:
        out["error"] = str(exc)
    return out


class OperationJournal:
    """Persist operation records under M-Claw home operations."""

    def __init__(self, base_dir: Path | None = None):
        self.base_dir = Path(base_dir or (get_mclaw_home() / "operations"))
        self.operations_dir = self.base_dir / "operations"
        self.sessions_dir = self.base_dir / "sessions"

    def begin(
        self,
        *,
        session_id: str,
        turn_id: str,
        message_id_before_turn: int | None,
        messages_len_before_turn: int | None,
        tool_call_id: str,
        tool_name: str,
        action: str,
        cwd: str,
        workspace: str,
        raw_command: str = "",
        targets: Iterable[str] | None = None,
        risk: str = "normal",
        checkpoint_commit: str | None = None,
        checkpoint_status: str | None = None,
        checkpoint_reason: str | None = None,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        """Start an audit record before executing a mutating tool call."""
        cancel_event = cancel_event or get_interrupt_event()
        cancellation_checkpoint(cancel_event)
        operation_id = f"op_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
        target_records: list[dict[str, Any]] = []
        for target in targets or []:
            cancellation_checkpoint(cancel_event)
            before = inspect_path(str(target), cancel_event=cancel_event)
            target_records.append({
                "path": before.get("path") or str(target),
                "before": before,
                "after": None,
            })
        record = {
            "operation_id": operation_id,
            "session_id": session_id,
            "turn_id": turn_id,
            "message_id_before_turn": message_id_before_turn,
            "messages_len_before_turn": messages_len_before_turn,
            "tool_call_id": tool_call_id,
            "tool": tool_name,
            "action": action,
            "raw_command": raw_command,
            "cwd": cwd,
            "workspace": workspace,
            "targets": target_records,
            "before_commit": checkpoint_commit,
            "after_commit": None,
            "checkpoint_status": checkpoint_status,
            "checkpoint_reason": checkpoint_reason,
            "risk": risk,
            "status": "pending",
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
        }
        cancellation_checkpoint(cancel_event)
        self._write_record(record)
        return record

    def finalize(
        self,
        record: dict[str, Any],
        *,
        success: bool,
        result_preview: str = "",
        after_commit: str | None = None,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        """Complete an operation record with post-execution target snapshots."""
        if not record:
            return {}
        cancel_event = cancel_event or get_interrupt_event()
        inspection_cancelled = bool(cancel_event is not None and cancel_event.is_set())
        for target in record.get("targets") or []:
            if inspection_cancelled:
                target["after"] = {"path": target.get("path") or "", "inspection_skipped": "cancelled"}
                continue
            try:
                target["after"] = inspect_path(
                    target.get("path") or "",
                    cancel_event=cancel_event,
                )
            except InterruptedError:
                inspection_cancelled = True
                target["after"] = {"path": target.get("path") or "", "inspection_skipped": "cancelled"}
        record["status"] = "completed" if success else "failed"
        record["after_commit"] = after_commit
        record["result_preview"] = (result_preview or "")[:1000]
        record["updated_at"] = _now_iso()
        self._write_record(record)
        self._append_session(record)
        return record

    def update_checkpoint(
        self,
        record: dict[str, Any],
        *,
        checkpoint_commit: str | None = None,
        checkpoint_status: str | None = None,
        checkpoint_reason: str | None = None,
    ) -> dict[str, Any]:
        """Attach checkpoint outcome metadata after preflight completes."""
        if not record:
            return {}
        record["before_commit"] = checkpoint_commit
        record["checkpoint_status"] = checkpoint_status
        record["checkpoint_reason"] = checkpoint_reason
        record["updated_at"] = _now_iso()
        self._write_record(record)
        return record

    def record_rollback(
        self,
        *,
        source_operation: dict[str, Any],
        session_id: str,
        rollback_id: str,
        mode: str,
        success: bool,
        pre_rollback_commit: str | None = None,
        restored_commit: str | None = None,
        context_rollback_id: str | None = None,
        conflict_backups: list[dict[str, Any]] | None = None,
        result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist a rollback transaction as an operation-journal record."""
        operation_id = rollback_id or f"rollback_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
        record = {
            "operation_id": operation_id,
            "session_id": session_id or source_operation.get("session_id") or "",
            "turn_id": source_operation.get("turn_id"),
            "message_id_before_turn": source_operation.get("message_id_before_turn"),
            "messages_len_before_turn": source_operation.get("messages_len_before_turn"),
            "tool_call_id": source_operation.get("tool_call_id"),
            "tool": "rollback",
            "action": "rollback",
            "raw_command": "",
            "cwd": source_operation.get("cwd") or source_operation.get("workspace") or "",
            "workspace": source_operation.get("workspace") or source_operation.get("cwd") or "",
            "targets": source_operation.get("targets") or [],
            "before_commit": pre_rollback_commit,
            "after_commit": restored_commit,
            "checkpoint_status": "taken" if pre_rollback_commit else None,
            "checkpoint_reason": f"rollback {source_operation.get('operation_id')}",
            "risk": source_operation.get("risk", "normal"),
            "status": "completed" if success else "failed",
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "rollback": {
                "source_operation_id": source_operation.get("operation_id"),
                "mode": mode,
                "context_rollback_id": context_rollback_id,
                "conflict_backups": conflict_backups or [],
                "result": result or {},
            },
        }
        self._write_record(record)
        self._append_session(record)
        return record

    def list_operations(
        self,
        *,
        session_id: str | None = None,
        workspace: str | None = None,
        include_rollbacks: bool = False,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Return recent completed operations, newest first."""
        records: list[dict[str, Any]] = []
        if not self.operations_dir.exists():
            return []
        day_dirs = sorted(
            [p for p in self.operations_dir.iterdir() if p.is_dir()],
            key=lambda p: p.name,
            reverse=True,
        )
        workspace_resolved = _resolve_for_compare(workspace) if workspace else None
        for day_dir in day_dirs:
            for path in sorted(day_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
                try:
                    record = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if not include_rollbacks and record.get("action") == "rollback":
                    continue
                if record.get("status") not in {"completed", "failed"}:
                    continue
                if session_id and record.get("session_id") != session_id:
                    continue
                if workspace_resolved:
                    if not _record_matches_workspace(record, workspace_resolved):
                        continue
                records.append(record)
                if len(records) >= limit:
                    return records
        return records

    def get_operation(
        self,
        ref: str,
        *,
        session_id: str | None = None,
        workspace: str | None = None,
        include_rollbacks: bool = False,
    ) -> dict[str, Any] | None:
        """Resolve an operation by 1-based recent index, exact id, or id prefix."""
        ref = str(ref or "").strip()
        if not ref:
            return None
        operations = self.list_operations(
            session_id=session_id,
            workspace=workspace,
            include_rollbacks=include_rollbacks,
            limit=200,
        )
        try:
            idx = int(ref) - 1
            if 0 <= idx < len(operations):
                return operations[idx]
        except ValueError:
            pass
        for record in operations:
            operation_id = str(record.get("operation_id") or "")
            if operation_id == ref or operation_id.startswith(ref):
                return record
        return None

    def _write_record(self, record: dict[str, Any]) -> None:
        """Write the canonical JSON record keyed by operation id and day."""
        day = time.strftime("%Y-%m-%d")
        out_dir = self.operations_dir / day
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{record['operation_id']}.json"
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

    def _append_session(self, record: dict[str, Any]) -> None:
        """Append a session-local event stream for quick rollback listing."""
        session_id = record.get("session_id") or "unknown"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        path = self.sessions_dir / f"{session_id}.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def default_journal() -> OperationJournal:
    """Return the journal rooted at the configured M-Claw home directory."""
    return OperationJournal()


def _resolve_for_compare(path_value: str) -> str:
    try:
        return str(Path(path_value).expanduser().resolve()).lower()
    except OSError:
        return str(Path(path_value).expanduser()).lower()


def _record_matches_workspace(record: dict[str, Any], workspace_resolved: str) -> bool:
    """Match records by workspace root or by any tracked target under that root."""
    rec_workspace = _resolve_for_compare(record.get("workspace") or record.get("cwd") or "")
    if rec_workspace == workspace_resolved:
        return True
    for target in record.get("targets") or []:
        target_path = _resolve_for_compare(target.get("path") or "")
        if _is_path_within(target_path, workspace_resolved):
            return True
    return False


def _is_path_within(path_value: str, root_value: str) -> bool:
    try:
        path = Path(path_value)
        root = Path(root_value)
        path.relative_to(root)
        return True
    except ValueError:
        return False
    except Exception:
        sep = "\\" if "\\" in root_value else "/"
        root = root_value.rstrip("\\/")
        return path_value == root or path_value.startswith(root + sep)
