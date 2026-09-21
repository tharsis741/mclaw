# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared queue transitions and durable handoff between Web and terminal."""

from __future__ import annotations

import copy
import json
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any


def classify_task_result(result: dict[str, Any], *, stopping: bool = False,
                         pending_supplements: bool = False) -> str:
    """Only a positively completed task permits the next queued task to run."""
    if stopping or result.get("interrupted"):
        return "interrupted"
    if result.get("pending_skill_import_confirmation"):
        return "waiting"
    if result.get("error") or result.get("abort_reason") or not result.get("completed") or pending_supplements:
        return "failed"
    return "completed"


def _queue_key(session_id: str) -> str:
    return f"tui_queue:{session_id}"


def apply_pending_supplement(db, session_id: str, messages: list[dict], turn_id: str,
                             item: dict, applied_ids: set[str]) -> bool:
    """Persist one task addition before acknowledging it at a safe boundary.

    Hosts restore applied_ids from SessionDB operation_id values on startup.
    The stable operation marker makes replay after a crash idempotent.
    """
    operation_id = "supplement:" + str(item["id"])
    from mclaw.application.skills import apply_skill_snapshots
    # Recover an interrupted commit after the text was saved but before its
    # selected Skill blocks were applied and acknowledged.
    if operation_id in applied_ids:
        apply_skill_snapshots(db, session_id, messages, turn_id, item, applied_ids)
        return False
    content = str(item["content"])
    if item.get("attachments"):
        from mclaw.application.files import attachment_context
        content += attachment_context(item["attachments"])
    db.append_message(session_id, "user", content=content, turn_id=turn_id, operation_id=operation_id)
    messages.append({"role": "user", "content": content})
    applied_ids.add(operation_id)
    apply_skill_snapshots(db, session_id, messages, turn_id, item, applied_ids)
    return True


def claim_terminal_queue(store, session_id: str) -> dict[str, Any] | None:
    """Atomically move Web queue ownership after the caller acquired its lock."""
    with store._lock, store._db:
        row = store._db.execute("SELECT value FROM app_settings WHERE key=?", (_queue_key(session_id),)).fetchone()
        if row:
            return json.loads(row[0])
        row = store._db.execute("SELECT document FROM app_sessions WHERE id=?", (session_id,)).fetchone()
        if not row:
            return None
        session = json.loads(row[0])
        state = {"active": session.get("active_task"), "pending": session.get("queue", []),
                 "paused": session.get("paused", False), "status": session.get("status", "idle")}
        store._db.execute("INSERT OR REPLACE INTO app_settings VALUES (?, ?)",
                          (_queue_key(session_id), json.dumps(state, ensure_ascii=False)))
        session.update(active_task=None, queue=[], paused=True, queue_owner="terminal")
        session["seq"] = session.get("seq", 0) + 1
        store._db.execute("UPDATE app_sessions SET document=?, updated_at=? WHERE id=?",
                          (json.dumps(session, ensure_ascii=False), session["updated_at"], session_id))
        return state


def adopt_terminal_queue(store, session_id: str, *, history=None) -> dict[str, Any] | None:
    """Atomically receive terminal work under the already-acquired Session lock.

    The caller first creates/imports its app_sessions record. Source deletion and
    destination update commit together, so a crash cannot duplicate queued work.
    """
    with store._lock, store._db:
        row = store._db.execute("SELECT value FROM app_settings WHERE key=?", (_queue_key(session_id),)).fetchone()
        target = store._db.execute("SELECT document FROM app_sessions WHERE id=?", (session_id,)).fetchone()
        if not row or not target:
            return None
        state, session = json.loads(row[0]), json.loads(target[0])
        session.update(active_task=state.get("active"), queue=state.get("pending", []),
                       paused=bool(state.get("active") or state.get("pending")),
                       status="interrupted" if state.get("active") or state.get("pending") else "idle",
                       queue_owner="web")
        if history is not None:
            known_ids = {message["id"] for message in session.get("messages", [])}
            cursor = int(state.get("history_after", 0))
            rows = history.get_messages(session_id, include_invalidated=True)
            by_id = {f"history_{message['id']}": message for message in rows}
            by_operation = {message.get("operation_id"): message for message in rows if message.get("operation_id")}
            matched_ids: set[int] = set()
            for displayed in session.get("messages", []):
                source = by_id.get(displayed["id"]) or by_operation.get("task:" + displayed["id"])
                if source is None:
                    source = next((message for message in rows if (
                        message["id"] <= cursor and message["id"] not in matched_ids
                        and message["role"] == displayed.get("role")
                        and message.get("content") == displayed.get("content")
                    )), None)
                if source is not None:
                    displayed["invalidated"] = bool(source.get("invalidated_at"))
                    displayed["turn_id"] = source.get("turn_id")
                    displayed["operation_id"] = source.get("operation_id")
                    matched_ids.add(source["id"])
            for message in rows:
                message_id = f"history_{message['id']}"
                if (
                    message["id"] <= cursor or message_id in known_ids
                    or message["role"] not in {"user", "assistant"} or not message.get("content")
                ):
                    continue
                session.setdefault("messages", []).append({
                    "id": message_id, "role": message["role"], "content": message["content"],
                    "process": [], "created_at": message.get("timestamp"),
                    "invalidated": bool(message.get("invalidated_at")),
                    "turn_id": message.get("turn_id"), "operation_id": message.get("operation_id"),
                })
                known_ids.add(message_id)
                session["updated_at"] = max(session.get("updated_at", 0), message.get("timestamp") or 0)
            row = history.get_session(session_id) or {}
            session["model"] = row.get("model") or session.get("model", "")
            session["title"] = row.get("title") or session.get("title", "历史会话")
            session["provider_snapshot"] = history.get_model_config(session_id)
        session["seq"] = session.get("seq", 0) + 1
        store._db.execute("UPDATE app_sessions SET document=?, updated_at=? WHERE id=?",
                          (json.dumps(session, ensure_ascii=False), session["updated_at"], session_id))
        store._db.execute("DELETE FROM app_settings WHERE key=?", (_queue_key(session_id),))
        return session


class TaskQueue:
    """A thread-safe queue that retains its original task until completion."""

    def __init__(self, state: dict[str, Any] | None = None, *,
                 save: Callable[[dict[str, Any]], None] | None = None,
                 recover: bool = False) -> None:
        self._lock = threading.RLock()
        self._save = save or (lambda _state: None)
        self.state = copy.deepcopy(state or {"active": None, "pending": [], "paused": False, "status": "idle"})
        self._committed = copy.deepcopy(self.state)
        if recover and (self.state["active"] or self.state["pending"]):
            self.state.update(paused=True, status="interrupted")
            if self.state["active"]:
                self.state["active"]["recovery_required"] = True
            self._persist()

    def _persist(self) -> None:
        try:
            self._save(copy.deepcopy(self.state))
        except BaseException:
            self.state = copy.deepcopy(self._committed)
            raise
        self._committed = copy.deepcopy(self.state)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self.state)

    def submit(self, content: str, model: str = "") -> str:
        if not content.strip():
            raise ValueError("排队消息不能为空")
        with self._lock:
            snapshots = self._selected_skill_snapshots()
            from mclaw.application.skills import snapshot_references
            task = {"id": f"task_{uuid.uuid4().hex[:20]}", "content": content.strip(), "model": model,
                    "status": "queued", "created_at": time.time(), "attempts": [], "supplements": [],
                    "skill_snapshots": snapshots, "skills": snapshot_references(snapshots)}
            self.state["pending"].append(task)
            self.state["selected_skills"] = []
            self._persist()
            return task["id"]

    def select_skill(self, name: str) -> None:
        """Retain a terminal Skill reference until the next accepted input."""
        from mclaw.skills_hub.paths import validate_skill_name
        name = validate_skill_name(name)
        with self._lock:
            self.state["selected_skills"] = list(dict.fromkeys([*self.state.get("selected_skills", []), name]))
            self._persist()

    def _selected_skill_snapshots(self) -> list[dict]:
        from mclaw.application.skills import SkillService
        from mclaw.constants import get_mclaw_home
        return SkillService(get_mclaw_home()).snapshots(self.state.get("selected_skills", []))

    def take_next(self) -> dict[str, Any] | None:
        with self._lock:
            if self.state["paused"] or self.state["active"] or not self.state["pending"]:
                return None
            task = self.state["pending"].pop(0)
            task["status"] = "running"
            task["attempts"].append({"id": f"attempt_{uuid.uuid4().hex[:20]}", "status": "running", "started_at": time.time()})
            self.state.update(active=task, status="running")
            self._persist()
            return copy.deepcopy(task)

    def stop(self) -> None:
        with self._lock:
            status = "stopping" if self.state["active"] else "interrupted"
            self.state.update(paused=True, status=status)
            try:
                self._persist()
            except BaseException:
                # Storage errors must never turn a user's stop into permission
                # to start the next task in this still-running process.
                self.state.update(paused=True, status=status)
                raise

    def finish(self, result: dict[str, Any]) -> str:
        with self._lock:
            task = self.state["active"]
            if not task:
                return self.state["status"]
            pending = any(not item.get("applied") for item in task["supplements"])
            state = classify_task_result(result, stopping=self.state["status"] == "stopping", pending_supplements=pending)
            task["status"] = state
            if task["attempts"]:
                task["attempts"][-1].update(status=state, ended_at=time.time())
            self.state.update(status=state, paused=state != "completed")
            if state == "completed":
                self.state["active"] = None
            try:
                self._persist()
            except BaseException:
                self.state.update(paused=True, status="interrupted" if state == "interrupted" else "failed")
                raise
            return state

    def resume(self, supplement: str = "") -> None:
        with self._lock:
            if self.state["status"] in {"running", "stopping"}:
                raise ValueError("任务尚未完成收尾，请稍候")
            task = self.state["active"]
            if task:
                if supplement.strip():
                    task["supplements"].append(self._supplement(supplement))
                task["status"] = "queued"
                self.state["pending"].insert(0, task)
                self.state["active"] = None
            elif supplement.strip():
                raise ValueError("当前没有未完成的原任务，请通过发送提交新任务")
            self.state.update(paused=False, status="idle")
            self._persist()

    def _supplement(self, content: str) -> dict[str, Any]:
        from mclaw.application.skills import snapshot_references
        snapshots = self._selected_skill_snapshots()
        self.state["selected_skills"] = []
        return {"id": f"supplement_{uuid.uuid4().hex[:20]}", "content": content.strip(), "applied": False,
                "skill_snapshots": snapshots, "skills": snapshot_references(snapshots)}

    def supplement(self, content: str) -> None:
        with self._lock:
            if self.state["status"] != "running" or not self.state["active"]:
                raise ValueError("当前没有可补充的运行任务")
            if not content.strip():
                raise ValueError("补充内容不能为空")
            self.state["active"]["supplements"].append(self._supplement(content))
            self._persist()

    def apply_supplements(self, apply: Callable[[dict[str, Any]], None]) -> None:
        """Apply only on the agent's safe model boundary, never in the UI thread."""
        with self._lock:
            task = self.state["active"]
            if task:
                for item in task["supplements"]:
                    if not item.get("applied"):
                        apply(copy.deepcopy(item))
                        item["applied"] = True
                        self._persist()

    def update_model(self, model: str) -> None:
        with self._lock:
            for item in self.state["pending"]:
                item["model"] = model
            self._persist()

    def edit(self, index: int, content: str | None) -> None:
        with self._lock:
            if not 0 <= index < len(self.state["pending"]):
                raise ValueError("排队消息不存在")
            if content is None:
                self.state["pending"].pop(index)
            elif content.strip():
                self.state["pending"][index]["content"] = content.strip()
            else:
                raise ValueError("排队消息不能为空")
            self._persist()


def terminal_queue(store, session_id: str, *, history_after: int = 0) -> TaskQueue:
    state = claim_terminal_queue(store, session_id)
    queue = TaskQueue(state, save=lambda value: store.set_setting(_queue_key(session_id), value), recover=True)
    if "history_after" not in queue.state:
        queue.state["history_after"] = history_after
        queue._persist()
    return queue
