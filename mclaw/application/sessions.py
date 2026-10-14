# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Backend-owned sessions, durable queues and exclusive presentation leases."""

from __future__ import annotations

import asyncio
import copy
import os
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from mclaw.cli.runtime.session_lock import InteractiveSessionLock, InteractiveSessionLockError
from mclaw.application.store import ApplicationStore, backup_history
from mclaw.application.process_events import PROCESS_VERSION
from mclaw.application.task_queue import adopt_terminal_queue, classify_task_result
from mclaw.application.workspaces import BACKGROUND_WORKSPACE_SOURCES, matches_managed_workspace
from mclaw.runtime.paths import PathPolicy


class ApplicationError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code, self.status = code, status


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


class SessionService:
    """Serialize control changes on the service loop; workers own execution only."""

    def __init__(self, home: Path, *, runner_factory: Callable | None = None,
                 history=None, clock: Callable = time.monotonic) -> None:
        self.home = home.resolve()
        backup_history(self.home)
        if history is None:
            from mclaw.state import SessionDB
            history = SessionDB(self.home / "state.db")
        self.history = history
        self.store = ApplicationStore(self.home / "application.sqlite3")
        self.store.recover()
        self.clock = clock
        self.owners: dict[str, tuple[str, float]] = {}
        self.locks: dict[str, InteractiveSessionLock] = {}
        self.jobs: dict[str, asyncio.Task] = {}
        self.runners: dict[str, Any] = {}
        self.maintenance: dict[str, asyncio.Task] = {}
        self.global_maintenance: set[str] = set()
        self.deletions: dict[str, asyncio.Task] = {}
        self.closing = False
        self.devices = None
        self.runtime_overrides: dict[str, dict] = {}
        if runner_factory is None:
            from mclaw.application.runner import ProcessRunner
            runner_factory = lambda session: ProcessRunner(self.home, session, devices=self.devices,
                background_emit=lambda event: self._worker_background(session["id"], event))
        self.runner_factory = runner_factory

    def _load(self, session_id: str) -> dict:
        if session_id in self.deletions:
            raise ApplicationError("SESSION_DELETING", "会话正在删除，请稍候", 409)
        if self.store.setting(f"deleted_session:{session_id}", False):
            raise ApplicationError("SESSION_NOT_FOUND", "会话已删除", 404)
        session = self.store.get(session_id)
        if session:
            changed = self._restore_background_workspace(session)
            missing = [message for message in session.get("messages", []) if str(message.get("id") or "").startswith("history_") and "turn_id" not in message]
            if missing:
                rows = {f"history_{row['id']}": row for row in self.history.get_messages(session_id, include_invalidated=True)}
                for message in missing:
                    row = rows.get(message["id"], {})
                    message.update(turn_id=row.get("turn_id"), operation_id=row.get("operation_id"), invalidated=bool(row.get("invalidated_at")))
            if changed or missing:
                self.store.save(session)
            return session
        row = self.history.get_session(session_id)
        if not row:
            raise ApplicationError("SESSION_NOT_FOUND", "会话不存在", 404)
        workspace = row.get("workspace") or ""
        session = self._new_snapshot(session_id, workspace, bool(workspace), row.get("model") or "")
        session.update(title=row.get("title") or "历史会话", source=row.get("source") or "web",
                       updated_at=row.get("started_at") or time.time())
        session["provider_snapshot"] = self.history.get_model_config(session_id)
        from mclaw.application.process_events import history_messages, secret_values
        session["messages"] = history_messages(
            self.history.get_messages(session_id, include_invalidated=True), secret_values(self.home))
        session["workspace_bound"] = True
        self.store.save(session)
        return session

    def _restore_background_workspace(self, session: dict) -> bool:
        """Recover old producer labels and allocation markers before retry/resume."""
        previous = (session.get("source"), session.get("managed_workspace_root"))
        if session.get("source") in {None, "web"}:
            row = self.history.get_session(session["id"]) or {}
            source = row.get("source")
            if source and "source" not in session:
                session["source"] = source
            if source in BACKGROUND_WORKSPACE_SOURCES:
                session["source"] = source
            elif not source:
                # Earlier scheduler snapshots used the Web bootstrap path. A
                # server-owned scheduled submission proves their origin even if
                # the worker never reached history creation before interruption.
                scheduled = {identifier for key, identifier in session.get("submissions", {}).items()
                             if key.startswith("schedule:")}
                tasks = session.get("tasks", []) + session.get("queue", [])
                if session.get("active_task"):
                    tasks = [*tasks, session["active_task"]]
                if any(task.get("id") in scheduled and task.get("source") == "scheduler"
                       and (task.get("execution") or {}).get("platform") == "scheduler" for task in tasks):
                    session["source"] = "scheduler"
            session.setdefault("source", "web")
        if session.get("source") in BACKGROUND_WORKSPACE_SOURCES and not session.get("managed_workspace_root"):
            root = str(Path(self.output_root()).expanduser().resolve())
            if matches_managed_workspace(session, root):
                session["managed_workspace_root"] = root
        return previous != (session.get("source"), session.get("managed_workspace_root"))

    @staticmethod
    def _new_snapshot(session_id: str, workspace: str, explicit: bool, model: str) -> dict:
        return {"id": session_id, "title": "新会话", "source": "web", "status": "idle", "seq": 0,
                "workspace": workspace, "workspace_explicit": explicit, "workspace_bound": False,
                "model": model, "updated_at": time.time(), "messages": [], "queue": [],
                "active_task": None, "paused": False, "error": None, "usage": {},
                "submissions": {}, "draft": "", "draft_skills": [], "draft_attachments": [], "draft_devices": [], "tasks": []}

    def claim(self, session_id: str, client: str) -> None:
        if not client or len(client) > 128:
            raise ApplicationError("CLIENT_REQUIRED", "缺少控制入口标识", 401)
        self._load(session_id)
        now = self.clock()
        owner = self.owners.get(session_id)
        if owner and owner[0] != client and owner[1] > now:
            raise ApplicationError("SESSION_OCCUPIED", "此会话正在另一个窗口或终端中控制", 409)
        self._claim_execution(session_id)
        self.owners[session_id] = (client, now + 30)

    def _claim_execution(self, session_id: str) -> None:
        """Acquire runtime ownership independently of a browser control lease."""
        if session_id not in self.locks:
            lock = InteractiveSessionLock(session_id, self.home / "sessions" / "locks")
            try:
                lock.acquire()
            except InteractiveSessionLockError as exc:
                raise ApplicationError("SESSION_OCCUPIED", str(exc), 409) from exc
            self.locks[session_id] = lock
            adopt_terminal_queue(self.store, session_id, history=self.history)

    def release(self, session_id: str, client: str) -> None:
        owner = self.owners.get(session_id)
        if owner and owner[0] == client:
            self.owners.pop(session_id, None)
            if session_id not in self.jobs and session_id not in self.runners and session_id not in self.deletions:
                lock = self.locks.pop(session_id, None)
                if lock:
                    lock.release()

    def expire_leases(self) -> None:
        for session_id, (client, deadline) in list(self.owners.items()):
            if deadline <= self.clock():
                self.release(session_id, client)

    def _save(self, session: dict, *, activity: bool = False) -> dict:
        session["seq"] += 1
        if activity:
            session["updated_at"] = time.time()
        self.store.save(session)
        return self.public(session)

    @staticmethod
    def public(session: dict) -> dict:
        value = copy.deepcopy({key: value for key, value in session.items()
                               if key not in {"submissions", "provider_snapshot", "tasks", "managed_workspace_root"}})
        for task in value.get("queue", []) + ([value["active_task"]] if value.get("active_task") else []):
            task.pop("provider_snapshot", None)
            task.pop("skill_snapshots", None)
            task.pop("execution", None)
            for supplement in task.get("supplements", []):
                supplement.pop("skill_snapshots", None)
        for message in value.get("messages", []):
            message.pop("skill_snapshots", None)
        return value

    def snapshot(self, session_id: str, client: str | None = None) -> dict:
        if client is not None:
            self.claim(session_id, client)
        session = self._load(session_id)
        if session_id not in self.jobs and any(m["role"] == "assistant" and m.get("process_version") != PROCESS_VERSION for m in session["messages"]):
            from mclaw.application.process_events import secret_values, upgrade_process
            if upgrade_process(session, self.history.get_messages(session_id, include_invalidated=True), secret_values(self.home)):
                return self._save(session)
        return self.public(session)

    def list(self, query: str = "", offset: int = 0, limit: int = 50) -> list[dict]:
        # Read old history without importing or rewriting every conversation.
        snapshots = {row["id"]: row for row in self.store.all()}
        for session in snapshots.values():
            if self._restore_background_workspace(session):
                self.store.save(session)
        for row in self.history.list_sessions_rich(limit=10000):
            if row["id"] not in snapshots:
                item = self._new_snapshot(row["id"], row.get("workspace") or "", bool(row.get("workspace")), row.get("model") or "")
                item.update(title=row.get("title") or row.get("preview") or "历史会话", source=row.get("source") or "web", updated_at=row.get("last_active") or row["started_at"])
                snapshots[row["id"]] = item
            else:
                snapshots[row["id"]].setdefault("source", row.get("source") or "web")
        rows = sorted(snapshots.values(), key=lambda row: row["updated_at"], reverse=True)
        if query:
            query = query.casefold()
            rows = [row for row in rows if query in (row["title"] + " " + row["workspace"] + " " + " ".join(m["content"] for m in row["messages"])).casefold()]
        return [{key: value for key, value in self.public(row).items() if key not in {"messages", "queue"}}
                | {"queue_count": len(row["queue"])} for row in rows[offset:offset + limit]]

    def create(self, client: str, workspace: str = "", model: str = "", *, source: str = "web") -> dict:
        if self.closing:
            raise ApplicationError("SERVICE_STOPPING", "服务正在退出", 409)
        if not client or len(client) > 128:
            raise ApplicationError("CLIENT_REQUIRED", "缺少控制入口标识", 401)
        session_id = new_id("session")
        explicit = bool(workspace.strip())
        if explicit:
            workspace = self.validate_workspace(workspace)
        session = self._new_snapshot(session_id, workspace, explicit, model)
        session["source"] = source
        self.store.save(session)
        self.claim(session_id, client)
        return self.public(session)

    def output_root(self) -> str:
        """Use the managed task directory unless the user chose another root."""
        return self.store.setting("output_root") or str(PathPolicy(mclaw_home=self.home).task_files_root())

    async def delete(self, session_id: str, client: str) -> dict:
        """Delete an idle conversation while retaining its workspace files."""
        if self.closing:
            raise ApplicationError("SERVICE_STOPPING", "服务正在退出", 409)
        self.claim(session_id, client)
        if session_id in self.jobs or session_id in self.maintenance:
            raise ApplicationError("SESSION_BUSY", "请先停止当前任务，等待收尾后再删除会话", 409)
        self.deletions[session_id] = asyncio.current_task()
        try:
            runner = self.runners.get(session_id)
            if runner is not None:
                await runner.close()
                self.runners.pop(session_id, None)
            # Reuse SessionDB's transactional cleanup, including message search
            # and rollback records. A paused task/queue is discarded with its snapshot.
            self.history.delete_session(session_id)
            self.store.delete(session_id)
            self.owners.pop(session_id, None)
            lock = self.locks.pop(session_id, None)
            if lock:
                lock.release()
            return {"deleted": True, "id": session_id}
        finally:
            self.deletions.pop(session_id, None)

    def _workspace_path(self, workspace: str) -> Path:
        path = Path(workspace).expanduser().resolve()
        if PathPolicy(mclaw_home=self.home).is_runtime_internal_path(path):
            raise ApplicationError("WORKSPACE_PROTECTED", "此目录用于保存 M-Claw 配置，请选择 _temp 或其他工作目录")
        return path

    def set_output_root(self, value: str) -> str:
        path = self._workspace_path(value)
        if path == PathPolicy(mclaw_home=self.home).task_files_root():
            path.mkdir(parents=True, exist_ok=True)
        saved = self.validate_workspace(str(path))
        self.store.set_setting("output_root", saved)
        return saved

    def validate_workspace(self, workspace: str) -> str:
        path = self._workspace_path(workspace)
        if not path.is_dir() or not os.access(path, os.R_OK | os.W_OK):
            raise ApplicationError("WORKSPACE_UNAVAILABLE", "工作目录不存在或不可读写，请恢复目录后重试")
        return str(path)

    def patch(self, session_id: str, client: str, changes: dict) -> dict:
        self.claim(session_id, client)
        session = self._load(session_id)
        if "title" in changes:
            title = str(changes["title"]).strip()
            if not title or len(title) > 200:
                raise ApplicationError("INVALID_TITLE", "标题须为 1–200 字")
            session["title"] = title
            if self.history.get_session(session_id):
                self.history.set_session_title(session_id, title)
        if "workspace" in changes:
            if session["workspace_bound"]:
                raise ApplicationError("WORKSPACE_BOUND", "已有任务的会话保持原目录；请新建会话", 409)
            raw = str(changes["workspace"] or "")
            session["workspace"] = self.validate_workspace(raw) if raw else ""
            session["workspace_explicit"] = bool(raw)
        if "model" in changes:
            session["model"] = str(changes["model"])
            session.pop("runtime_override_id", None)
            session.pop("provider_snapshot", None)
            for task in session["queue"]:
                task["model"] = session["model"]
                task["provider_snapshot"] = {}
                task.pop("runtime_override_id", None)
        if "draft" in changes:
            session["draft"] = str(changes["draft"])[:100000]
        if "draft_skills" in changes:
            names = changes["draft_skills"]
            if not isinstance(names, list) or len(names) > 20 or any(not isinstance(name, str) for name in names):
                raise ApplicationError("INVALID_SKILLS", "技能引用须为名称列表，最多 20 项")
            session["draft_skills"] = list(dict.fromkeys(names))
        if "draft_attachments" in changes:
            from mclaw.application.files import FileService
            selected = FileService(self.home).snapshots(session_id, changes["draft_attachments"])
            session["draft_attachments"] = [item["id"] for item in selected]
        if "draft_devices" in changes:
            from mclaw.application.collaboration import device_ids
            session["draft_devices"] = device_ids(changes["draft_devices"])
        return self._save(session, activity=True)

    def terminal_runtime(self, session_id: str, client: str, data: dict) -> dict:
        """Bind explicit CLI credentials to this session in memory, never SQLite."""
        self.claim(session_id, client)
        session = self._load(session_id)
        selector = str(data.get("model") or "")
        if "::" not in selector:
            raise ApplicationError("INVALID_MODEL", "模型必须包含供应商标识")
        context = data.get("context")
        if context is not None:
            if not isinstance(context, dict) or set(context) - {"api_key", "base_url", "snapshot"}:
                raise ApplicationError("INVALID_RUNTIME", "临时模型配置格式错误")
            if not isinstance(context.get("snapshot"), dict):
                raise ApplicationError("INVALID_RUNTIME", "缺少模型配置记录")
            if any(not isinstance(context.get(key, ""), str) or len(context.get(key, "")) > 32768
                   for key in ("api_key", "base_url")):
                raise ApplicationError("INVALID_RUNTIME", "临时模型凭据格式错误")
            provider, model = selector.split("::", 1)
            if context["snapshot"].get("provider") != provider or context["snapshot"].get("model") != model:
                raise ApplicationError("INVALID_RUNTIME", "临时配置与模型不匹配")
        self.patch(session_id, client, {"model": selector})
        session = self._load(session_id)
        if context is not None:
            identifier = new_id("runtime")
            self.runtime_overrides[identifier] = copy.deepcopy(context)
            session["runtime_override_id"] = identifier
            for task in session["queue"]:
                task["runtime_override_id"] = identifier
            # A restart cannot silently substitute configured credentials for
            # an interrupted task that originally used explicit CLI overrides.
            active = session.get("active_task")
            if active and session["paused"] and active.get("model") == selector and active.get("runtime_override_id"):
                active["runtime_override_id"] = identifier
        return self._save(session)

    def _bind_workspace(self, session: dict) -> None:
        if not session["workspace"]:
            if session["workspace_bound"]:
                raise ApplicationError("WORKSPACE_UNKNOWN", "历史会话未记录工作目录，需要先核对原目录", 409)
            root = self._workspace_path(self.output_root())
            workspace = self._workspace_path(str(root / session["id"]))
            workspace.mkdir(parents=True, exist_ok=True)
            session["workspace"] = str(workspace)
            # Record only the exact directory allocated by the service.
            if matches_managed_workspace(session, str(root)):
                session["managed_workspace_root"] = str(root)
        self.validate_workspace(session["workspace"])
        session["workspace_bound"] = True

    @staticmethod
    def _record_supplement(session: dict, active: dict, content: str, snapshots: list[dict] | None = None,
                           attachments: list[dict] | None = None) -> dict:
        from mclaw.application.skills import snapshot_references

        item = {"id": new_id("supplement"), "content": content, "applied": False,
                "skill_snapshots": snapshots or [], "skills": snapshot_references(snapshots or []),
                "attachments": attachments or []}
        active.setdefault("supplements", []).append(item)
        session["messages"].append({**item, "role": "user", "mode": "steer",
                                    "created_at": time.time(), "process": []})
        return item

    def submit(self, session_id: str, client: str, content: str, request_id: str,
               mode: str = "queue", skills: list[str] | None = None,
               attachments: list[str] | None = None, devices: list[str] | None = None) -> dict:
        self.claim(session_id, client)
        session = self._load(session_id)
        if self.closing:
            raise ApplicationError("SERVICE_STOPPING", "服务正在退出", 409)
        if not request_id or len(request_id) > 128:
            raise ApplicationError("REQUEST_ID_REQUIRED", "提交需要唯一请求标识")
        if request_id in session["submissions"]:
            return self.public(session)
        content = content.strip()
        if not content or len(content) > 100000:
            raise ApplicationError("INVALID_CONTENT", "请输入任务（最多 100000 字）")
        if mode not in {"queue", "steer"}:
            raise ApplicationError("INVALID_MODE", "未知提交方式")
        from mclaw.application.skills import SkillService, snapshot_references
        from mclaw.application.files import FileService
        snapshots = SkillService(self.home).snapshots(skills)
        files = FileService(self.home).snapshots(session_id, attachments)
        from mclaw.application.collaboration import selection_snapshot
        selection = selection_snapshot(devices if devices is not None else session.get("draft_devices", []), self.devices)
        self._bind_workspace(session)
        if mode == "steer":
            active = session["active_task"]
            if session["status"] != "running" or not active:
                raise ApplicationError("NOT_RUNNING", "当前没有可补充的运行任务", 409)
            item = self._record_supplement(session, active, content, snapshots, files)
            session["submissions"][request_id] = item["id"]
            self._save(session, activity=True)
            runner = self.runners.get(session_id)
            if runner:
                runner.send({"type": "steer", **item})
            return self.snapshot(session_id)
        task = {"id": new_id("task"), "content": content, "model": session["model"],
                "source": "cli" if client.startswith("tui:") else "web",
                "execution": {"platform": "cli" if client.startswith("tui:") else "web"},
                "device_selection": selection,
                "skill_snapshots": snapshots, "skills": snapshot_references(snapshots), "attachments": files,
                "provider_snapshot": session.get("provider_snapshot", {}), "status": "queued",
                "attempts": [], "supplements": [], "created_at": time.time()}
        if session.get("runtime_override_id"):
            task["runtime_override_id"] = session["runtime_override_id"]
        session["queue"].append(task)
        session["submissions"][request_id] = task["id"]
        session["draft"] = ""
        session["draft_skills"] = []
        session["draft_attachments"] = []
        session["draft_devices"] = [item["id"] for item in selection["devices"]]
        if session["title"] == "新会话":
            session["title"] = content[:35]
        self._save(session, activity=True)
        self._schedule(session_id)
        return self.snapshot(session_id)

    def edit_queue(self, session_id: str, client: str, items: list[dict]) -> dict:
        self.claim(session_id, client)
        session = self._load(session_id)
        existing = {task["id"]: task for task in session["queue"]}
        ids = [item.get("id") for item in items]
        if any(not isinstance(identifier, str) or not identifier for identifier in ids):
            raise ApplicationError("INVALID_QUEUE", "队列消息标识格式错误")
        if len(ids) != len(set(ids)) or not set(ids).issubset(existing):
            raise ApplicationError("QUEUE_CHANGED", "队列已变化，请刷新后重试", 409)
        queue = []
        for item in items:
            content = str(item.get("content") or "").strip()
            if not content or len(content) > 100000:
                raise ApplicationError("INVALID_CONTENT", "排队消息不能为空或过长")
            task = existing[item["id"]] | {"content": content}
            if "skills" in item:
                from mclaw.application.skills import SkillService, snapshot_references
                snapshots = SkillService(self.home).snapshots(item["skills"])
                task.update(skill_snapshots=snapshots, skills=snapshot_references(snapshots))
            if "attachments" in item:
                from mclaw.application.files import FileService
                task["attachments"] = FileService(self.home).snapshots(session_id, item["attachments"])
            queue.append(task)
        session["queue"] = queue
        return self._save(session, activity=True)

    def stop(self, session_id: str, client: str) -> dict:
        self.claim(session_id, client)
        session = self._load(session_id)
        session["paused"] = True
        session["status"] = "stopping" if session_id in self.jobs else "interrupted"
        self._save(session, activity=True)
        runner = self.runners.get(session_id)
        if runner:
            runner.send({"type": "stop"})
        return self.snapshot(session_id)

    def reply(self, session_id: str, client: str, request_id: str, value: Any) -> dict:
        """Pass dedicated authorization values to the worker without persistence."""
        self.claim(session_id, client)
        session = self._load(session_id)
        pending = session.get("pending_request")
        if not pending or pending["id"] != request_id or session["status"] != "waiting":
            raise ApplicationError("REQUEST_EXPIRED", "此确认已处理或过期", 409)
        runner = self.runners.get(session_id)
        if not runner:
            raise ApplicationError("WORKER_UNAVAILABLE", "执行进程不可用，请核对状态", 409)
        runner.send({"type": "reply", "request_id": request_id, "value": value})
        session.update(pending_request=None, status="running")
        return self._save(session)

    def resume(self, session_id: str, client: str, content: str = "", request_id: str = "",
               skills: list[str] | None = None, attachments: list[str] | None = None) -> dict:
        self.claim(session_id, client)
        session = self._load(session_id)
        if self.closing:
            raise ApplicationError("SERVICE_STOPPING", "服务正在退出", 409)
        if len(content) > 100000:
            raise ApplicationError("INVALID_CONTENT", "补充内容最多 100000 字")
        if request_id and request_id in session["submissions"]:
            return self.public(session)
        if session_id in self.jobs or session["status"] == "stopping":
            raise ApplicationError("STILL_STOPPING", "任务尚未完成收尾，请稍候", 409)
        from mclaw.application.skills import SkillService
        from mclaw.application.files import FileService
        snapshots = SkillService(self.home).snapshots(skills)
        files = FileService(self.home).snapshots(session_id, attachments)
        if (snapshots or files) and not content.strip():
            raise ApplicationError("INVALID_CONTENT", "请填写需要补充的内容")
        self._bind_workspace(session)
        active = session["active_task"]
        if active:
            if content.strip():
                self._record_supplement(session, active, content.strip(), snapshots, files)
            # A failed original task keeps its task identity but can use repaired settings.
            active["model"] = session["model"]
            active["provider_snapshot"] = session.get("provider_snapshot", {})
            active["status"] = "queued"
            session["queue"].insert(0, active)
            session["active_task"] = None
        elif content.strip():
            raise ApplicationError("NO_ORIGINAL_TASK", "请通过发送提交新任务")
        session.update(paused=False, error=None, status="idle")
        if request_id:
            session["submissions"][request_id] = active["id"] if active else "queue-resume"
        self._save(session, activity=True)
        self._schedule(session_id)
        return self.snapshot(session_id)

    def _schedule(self, session_id: str) -> None:
        if self.global_maintenance:
            return
        if session_id not in self.jobs and session_id not in self.maintenance and session_id not in self.deletions and not self.closing:
            session = self._load(session_id)
            if any(os.path.normcase(self._load(identifier).get("workspace") or "") == os.path.normcase(session.get("workspace") or "") for identifier in self.maintenance):
                return
            if session["queue"] and not session["paused"]:
                self.jobs[session_id] = asyncio.create_task(self._consume(session_id))

    def submit_background(self, session_id: str, content: str, request_id: str,
                          execution: dict | None = None) -> dict:
        """Queue a system producer's task without taking a browser control lease."""
        if self.closing:
            raise ApplicationError("SERVICE_STOPPING", "服务正在退出", 409)
        session = self._load(session_id)
        self._claim_execution(session_id)
        session = self._load(session_id)
        if request_id in session["submissions"]:
            return {"session_id": session_id, "task_id": session["submissions"][request_id]}
        if not request_id or len(request_id) > 128 or not content.strip() or len(content) > 100000:
            raise ApplicationError("INVALID_BACKGROUND_TASK", "后台任务标识或内容无效")
        execution = dict(execution or {})
        if set(execution) - {"extra_system", "enabled_toolsets", "max_iterations", "platform", "channel_toolsets"}:
            raise ApplicationError("INVALID_EXECUTION", "后台任务运行参数无效")
        if "channel_toolsets" in execution and type(execution["channel_toolsets"]) is not bool:
            raise ApplicationError("INVALID_EXECUTION", "渠道工具设置标记无效")
        maximum = execution.get("max_iterations")
        if maximum is not None and (type(maximum) is not int or maximum <= 0):
            raise ApplicationError("INVALID_EXECUTION", "运行轮次须为正整数")
        toolsets = execution.get("enabled_toolsets")
        if toolsets is not None and (not isinstance(toolsets, list) or any(not isinstance(value, str) for value in toolsets)):
            raise ApplicationError("INVALID_EXECUTION", "工具集须为名称列表")
        self._bind_workspace(session)
        task = {"id": new_id("task"), "content": content.strip(), "model": session["model"],
                "provider_snapshot": session.get("provider_snapshot", {}), "status": "queued",
                "attempts": [], "supplements": [], "created_at": time.time(),
                "execution": execution, "source": execution.get("platform", "system")}
        if session.get("runtime_override_id") and task["source"] in {"web", "cli"}:
            task["runtime_override_id"] = session["runtime_override_id"]
        session["queue"].append(task)
        session["submissions"][request_id] = task["id"]
        if session["title"] == "新会话":
            session["title"] = task["content"][:35]
        self._save(session, activity=True)
        self._schedule(session_id)
        return {"session_id": session_id, "task_id": task["id"]}

    def prepare_background_session(self, session_id: str, *, source: str = "") -> dict:
        """Bind a producer's session while preserving historical workspace rules."""
        if source and source not in BACKGROUND_WORKSPACE_SOURCES:
            raise ApplicationError("INVALID_SOURCE", "后台任务来源无效")
        self._load(session_id)
        self._claim_execution(session_id)
        session = self._load(session_id)
        row = self.history.get_session(session_id) or {}
        session["source"] = row.get("source") or source or session.get("source") or "web"
        if not session["workspace"] and session["source"] in BACKGROUND_WORKSPACE_SOURCES:
            pristine = not (session.get("messages") or session.get("queue") or session.get("active_task")
                            or self.history.get_messages(session_id, include_invalidated=True))
            if pristine:
                session["workspace_bound"] = False
        self._bind_workspace(session)
        self.history.create_session(session_id, source=session["source"], workspace=session["workspace"])
        self._restore_background_workspace(session)
        if session["source"] in {"weixin", "dingtalk"} and not row.get("title"):
            occupied = {item.get("title") for item in self.history.list_sessions_rich(limit=100000)
                        if item["id"] != session_id and item.get("title")}
            while True:
                title = self.store.channel_title(session["source"], session_id, occupied)
                try:
                    self.history.set_session_title(session_id, title)
                    session["title"] = title
                    break
                except ValueError:
                    occupied.add(title)
        elif row.get("title"):
            session["title"] = row["title"]
        return self._save(session)

    def pause_background(self, session_id: str, task_ids: list[str]) -> dict:
        """Pause a producer's accepted work without deleting it or taking a UI lease."""
        self._load(session_id)
        self._claim_execution(session_id)
        session = self._load(session_id)
        identifiers = set(task_ids)
        active = session.get("active_task")
        active_matches = bool(active and active["id"] in identifiers)
        if not active_matches and not any(item["id"] in identifiers for item in session["queue"]):
            return self.public(session)
        session["paused"] = True
        if active_matches:
            session["status"] = "stopping" if session_id in self.jobs else "interrupted"
        elif not active:
            session["status"] = "interrupted"
        result = self._save(session)
        if active_matches and session_id in self.runners:
            self.runners[session_id].send({"type": "stop"})
        return result

    def cancel_background(self, session_id: str, task_id: str) -> None:
        """Cancel exactly the system producer's accepted task, preserving other work."""
        session = self._load(session_id)
        self._claim_execution(session_id)
        session = self._load(session_id)
        for task in session["queue"]:
            if task["id"] == task_id:
                session["queue"].remove(task)
                task.update(status="interrupted", result={"status": "interrupted", "interrupted": True, "completed": False})
                session["tasks"].append(task)
                self._save(session, activity=True)
                return
        active = session.get("active_task")
        if active and active["id"] == task_id:
            session.update(paused=True, status="stopping" if session_id in self.jobs else "interrupted")
            self._save(session, activity=True)
            runner = self.runners.get(session_id)
            if runner:
                runner.send({"type": "stop"})

    def update_background_model(self, session_id: str, model: str) -> None:
        self._load(session_id)
        self._claim_execution(session_id)
        session = self._load(session_id)
        session["model"] = str(model)
        session.pop("provider_snapshot", None)
        for task in session["queue"]:
            task.update(model=session["model"], provider_snapshot={})
        self._save(session)

    def task_result(self, session_id: str, task_id: str) -> dict | None:
        if session_id in self.deletions:
            return None
        if self.store.setting(f"deleted_session:{session_id}", False):
            return {"completed": False, "cancelled": True, "interrupted": True, "status": "interrupted"}
        session = self._load(session_id)
        tasks = session.get("tasks", []) + ([session["active_task"]] if session.get("active_task") else [])
        for task in tasks:
            if task["id"] == task_id and task["status"] not in {"queued", "running", "stopping", "waiting"}:
                return copy.deepcopy(task.get("result") or {"status": task["status"], "completed": False})
        return None

    async def _consume(self, session_id: str) -> None:
        try:
            while not self.closing:
                session = self._load(session_id)
                if session["paused"] or not session["queue"]:
                    return
                task = session["queue"].pop(0)
                task["status"] = "running"
                attempt = {"id": new_id("attempt"), "started_at": time.time(), "status": "running"}
                task["attempts"].append(attempt)
                session.update(active_task=task, status="running", error=None, status_text="")
                if len(task["attempts"]) == 1:
                    session["messages"].append({"id": task["id"], "role": "user", "source": task.get("source", "web"), "content": task["content"], "process": [], "created_at": task["created_at"], "skills": task.get("skills", []), "attachments": task.get("attachments", []), "device_selection": task.get("device_selection", {"mode": "auto", "devices": []})})
                response_id = attempt["id"]
                session["messages"].append({"id": response_id, "role": "assistant", "content": "", "process": [], "process_version": PROCESS_VERSION, "created_at": time.time()})
                self._save(session)
                try:
                    runner = self.runners.get(session_id)
                    if runner is None:
                        runner = self.runner_factory(session)
                        self.runners[session_id] = runner
                    override_id = task.get("runtime_override_id")
                    if override_id and override_id not in self.runtime_overrides:
                        result = {"completed": False, "error": "此任务使用的临时模型凭据已随后台退出清除。请重新提供原接入参数后继续。"}
                    else:
                        execution_task = {**task, "_runtime_context": self.runtime_overrides[override_id]} if override_id else task
                        result = await runner.run(execution_task, lambda event: self._event(session_id, response_id, event))
                except Exception:
                    result = {"error": "执行进程异常结束，请核对已知结果后重试", "completed": False}
                session = self._load(session_id)
                active = session["active_task"]
                if not active:
                    return
                pending_supplements = any(not item["applied"] for item in active["supplements"])
                state = classify_task_result(result, stopping=session["status"] == "stopping", pending_supplements=pending_supplements)
                if state != "completed":
                    active["status"] = state
                    session.update(status=state, paused=True,
                                   error=result.get("error") or result.get("abort_message") or ("补充尚未应用，请继续处理" if pending_supplements else (None if state == "interrupted" else "任务未完成，请检查配置或执行过程后重试")))
                else:
                    active["status"] = state
                    session["tasks"].append(active)
                    session.update(active_task=None, status="completed")
                active["attempts"][-1].update(status=state, ended_at=time.time())
                active["result"] = {**result, "status": state}
                session["usage"] = result.get("token_usage") or session["usage"]
                if result.get("session_usage"):
                    session["session_usage"] = result["session_usage"]
                if result.get("runtime_view"):
                    session["runtime_view"] = result["runtime_view"]
                session["pending_request"] = None
                session["status_text"] = ""
                response = next(item for item in session["messages"] if item["id"] == response_id)
                response["turn_id"] = result.get("turn_id")
                response["result"] = {key: value for key, value in active["result"].items()
                                      if key in {"status", "completed", "interrupted", "cancelled", "error",
                                                 "abort_message", "abort_reason", "stop_reason", "completion_unknown", "token_usage",
                                                 "duration", "api_calls", "tool_calls", "max_iterations_reached"}}
                from mclaw.application.process_events import finish_process
                finish_process(response)
                from mclaw.application.delegation_events import finish_delegations
                finish_delegations(response)
                if result.get("final_response"):
                    response["content"] = result["final_response"]
                self._save(session, activity=True)
                if session["paused"]:
                    return
        finally:
            self.jobs.pop(session_id, None)
            session = self._load(session_id)
            if session["status"] == "stopping" and not session.get("active_task"):
                session["status"] = "interrupted"
                self._save(session)
            self.expire_leases()

    def _event(self, session_id: str, response_id: str, event: dict) -> None:
        session = self._load(session_id)
        message = next(item for item in session["messages"] if item["id"] == response_id)
        events = event["events"] if event.get("type") == "batch" else [event]
        for item in events:
            self._apply_event(session, message, item)
        self._save(session)

    def _apply_event(self, session: dict, message: dict, event: dict) -> None:
        kind = event.get("type")
        if kind == "delta":
            message["content"] += str(event.get("text") or "")
        elif kind == "process":
            from mclaw.application.process_events import resolve_remote_waits, update_process
            update_process(message, event)
            resolve_remote_waits(session["messages"], event)
            if event.get("kind") == "remote" and event.get("files"):
                attachments = {item["id"]: item for item in message.get("attachments", [])}
                attachments.update({item["id"]: item for item in event["files"]})
                message["attachments"] = list(attachments.values())
        elif kind == "status":
            session["status_text"] = str(event.get("text") or "")
        elif kind == "body":
            message["content"] = str(event.get("text") or "")
        elif kind == "body_reset":
            message["content"] = ""
        elif kind == "runtime_view":
            session["runtime_view"] = event["view"]
        elif kind == "delegation":
            from mclaw.application.delegation_events import update_delegation
            update_delegation(message, event)
        elif kind == "request":
            session["pending_request"] = event["request"]
            if event["request"].get("kind") == "skill_install" and session.get("active_task"):
                session["active_task"]["pending_skill_install"] = event["request"]
            if session["status"] != "stopping":
                session["status"] = "waiting"
        elif kind == "supplement_applied":
            for item in session["active_task"]["supplements"]:
                if item["id"] == event.get("id"):
                    item["applied"] = True
            for item in session["messages"]:
                if item["id"] == event.get("id"):
                    item["applied"] = True
        elif kind == "request_resolved":
            if (session.get("pending_request") or {}).get("id") == event.get("request_id"):
                session["pending_request"] = None
                if session["status"] == "waiting":
                    session["status"] = "running"
        elif kind == "skill_install_resolved":
            if session.get("active_task"):
                session["active_task"].pop("pending_skill_install", None)
        else:
            message["process"].append({"id": new_id("event"), "kind": kind or "progress",
                                       "title": event.get("title") or kind or "执行进展",
                                       "content": str(event.get("text") or "")})

    def _worker_background(self, session_id: str, event: dict) -> None:
        """Queue watcher follow-ups through the same durable execution owner."""
        if self.closing or session_id in self.deletions:
            return
        try:
            session = self._load(session_id)
            content = str(event.get("notice") or "")
            if not content:
                return
            self.submit_background(session_id, content, "watcher:" + str(event["id"]),
                                   execution={"platform": session.get("source") or "web"})
        except (ApplicationError, KeyError):
            return

    async def change_operation(self, session_id: str, client: str, request: dict) -> dict:
        """Fence queue dispatch while one idle worker owns a recovery request."""
        if self.closing:
            raise ApplicationError("SERVICE_STOPPING", "服务正在退出", 409)
        self.claim(session_id, client)
        session = self._load(session_id)
        if session_id in self.jobs or session_id in self.maintenance or session.get("active_task"):
            raise ApplicationError("SESSION_BUSY", "请等待当前任务和工具收尾后再操作文件恢复", 409)
        if not session.get("workspace"):
            return {"groups": [], "checkpoints": []}
        workspace = os.path.normcase(session["workspace"])
        global_scope = request.get("operation") == "terminal_files" or (
            request.get("operation") == "apply" and request.get("action") in {"clear", "prune"})
        if self.global_maintenance or (global_scope and (self.jobs or self.maintenance)):
            raise ApplicationError("WORKSPACE_BUSY", "请等待其他任务及文件维护完成后再操作恢复记录", 409)
        if any(os.path.normcase(self._load(identifier).get("workspace") or "") == workspace for identifier in self.maintenance):
            raise ApplicationError("WORKSPACE_BUSY", "此工作区正在执行恢复维护，请稍后重试", 409)
        if any(os.path.normcase(self._load(identifier).get("workspace") or "") == workspace for identifier in self.jobs):
            raise ApplicationError("WORKSPACE_BUSY", "此工作区还有任务在运行，请等待工具收尾后再恢复或清理", 409)
        extra_locks = []
        if global_scope:
            try:
                for row in self.history.list_sessions_rich(limit=10000):
                    if row["id"] == session_id or row["id"] in self.locks:
                        continue
                    lock = InteractiveSessionLock(row["id"], self.home / "sessions" / "locks")
                    lock.acquire()
                    extra_locks.append(lock)
            except InteractiveSessionLockError as exc:
                for lock in extra_locks:
                    lock.release()
                raise ApplicationError("WORKSPACE_BUSY", "此工作区的其他会话仍由终端控制，请先结束控制再清理", 409) from exc
        self.maintenance[session_id] = asyncio.current_task()
        if global_scope:
            self.global_maintenance.add(session_id)
        try:
            runner = self.runners.get(session_id)
            if runner is None:
                runner = self.runner_factory(session)
                self.runners[session_id] = runner
            result = await runner.control(request)
            if request.get("operation") in {"apply", "terminal_files"}:
                # Terminal recovery can explicitly target another directory.
                # Refresh every affected view before queued work may resume.
                for current in self.store.all():
                    if current["id"] != session_id and not global_scope and os.path.normcase(current.get("workspace") or "") != workspace:
                        continue
                    rows = self.history.get_messages(current["id"], include_invalidated=True)
                    by_id = {f"history_{row['id']}": row for row in rows}
                    by_operation = {str(row.get("operation_id")).split(":", 1)[1]: row for row in rows
                                    if str(row.get("operation_id") or "").startswith(("task:", "supplement:"))}
                    changed = False
                    for message in current["messages"]:
                        previous = bool(message.get("invalidated"))
                        row = by_id.get(message["id"]) or by_operation.get(message["id"])
                        if row is not None:
                            message["invalidated"] = bool(row.get("invalidated_at"))
                        elif message.get("turn_id"):
                            related = [row for row in rows if row.get("turn_id") == message["turn_id"] and row.get("role") == message["role"]]
                            message["invalidated"] = bool(related) and all(row.get("invalidated_at") for row in related)
                        changed |= previous != bool(message.get("invalidated"))
                    if current["id"] == session_id:
                        result["session"] = self._save(current, activity=True)
                    elif changed:
                        self._save(current)
            return result
        except BaseException as exc:
            if request.get("operation") in {"apply", "terminal_files"} and (not isinstance(exc, ApplicationError) or exc.code in {"RECOVERY_FAILED", "RECOVERY_UNKNOWN"}):
                session = self._load(session_id)
                session.update(paused=True, status="failed", error="恢复结果需要核对，请检查文件和恢复记录后继续")
                self._save(session)
                for other in self.store.all():
                    if other["id"] != session_id and (global_scope or os.path.normcase(other.get("workspace") or "") == workspace) and other.get("queue"):
                        other.update(paused=True, status="interrupted", error="工作区恢复结果需要核对，请检查文件后继续")
                        self._save(other)
            raise
        finally:
            self.maintenance.pop(session_id, None)
            self.global_maintenance.discard(session_id)
            if getattr(self.runners.get(session_id), "_closed", False):
                self.runners.pop(session_id, None)
            for lock in extra_locks:
                lock.release()
            self._schedule(session_id)
            for other in self.store.all():
                if other["id"] != session_id and (global_scope or os.path.normcase(other.get("workspace") or "") == workspace):
                    self._schedule(other["id"])

    async def close(self) -> None:
        self.closing = True
        if self.deletions:
            await asyncio.gather(*list(self.deletions.values()), return_exceptions=True)
        for session_id in list(self.jobs):
            session = self._load(session_id)
            session.update(paused=True, status="stopping")
            self._save(session)
            runner = self.runners.get(session_id)
            if runner:
                runner.send({"type": "stop"})
        if self.jobs:
            await asyncio.gather(*list(self.jobs.values()), return_exceptions=True)
        if self.maintenance:
            await asyncio.gather(*list(self.maintenance.values()), return_exceptions=True)
        if self.runners:
            await asyncio.gather(*(runner.close() for runner in self.runners.values()), return_exceptions=True)
        self.runners.clear()
        self.runtime_overrides.clear()
        for lock in self.locks.values():
            lock.release()
        self.locks.clear()
        self.history.close()
        self.store.close()
