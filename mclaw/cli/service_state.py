# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Terminal presentation adapters for backend-owned history and queues."""

from __future__ import annotations

import copy
import threading
import uuid


class ServiceHistory:
    """The terminal never opens the application's session database."""

    def __init__(self, client, *, owns_client=False):
        self.client = client
        self.owns_client = owns_client

    def detail(self, identifier):
        return self.client.request("GET", f"/terminal/sessions/{identifier}")

    def get_session(self, identifier):
        data = self.detail(identifier)
        row = {**(data.get("conversation") or {}), **data["session"]}
        row["model"] = row.get("model", "").split("::")[-1]
        return row

    def get_model_config(self, identifier):
        data = self.detail(identifier)
        snapshot = data.get("model_config") or {}
        selector = data["session"].get("model", "")
        if "::" in selector:
            provider, model = selector.split("::", 1)
            if (snapshot.get("provider"), snapshot.get("model")) != (provider, model):
                from mclaw.cli.config import load_merged_config
                from mclaw.providers.resolver import resolve_provider_runtime_context
                from pathlib import Path
                context = resolve_provider_runtime_context(provider=provider, model=model,
                    config=load_merged_config(cwd=Path(data["session"]["workspace"])))
                return context.snapshot()
        return snapshot

    def get_messages_as_conversation(self, identifier):
        return self.detail(identifier).get("messages") or []

    def export_session(self, identifier):
        data = self.detail(identifier)
        return data.get("conversation") or data["session"]

    def set_session_title(self, identifier, title):
        self.client.patch(identifier, {"title": title})
        return True

    def list_sessions_rich(self, *, source=None, workspace=None, limit=15):
        from urllib.parse import urlencode
        # Paginate before filtering: other frontends share the same history.
        found, offset = [], 0
        while len(found) < limit:
            rows = self.client.request("GET", "/sessions?" + urlencode({"offset": offset, "limit": 100}))["sessions"]
            for row in rows:
                if workspace and row.get("workspace") != workspace:
                    continue
                if source and row.get("source") != source:
                    continue
                found.append({**row, "started_at": row.get("updated_at", 0),
                              "last_active": row.get("updated_at", 0), "preview": row.get("title", ""),
                              "message_count": row.get("message_count", 0)})
            if len(rows) < 100:
                break
            offset += 100
        return found[:limit]

    def resolve_session_id(self, reference, *, workspace=None):
        rows = self.list_sessions_rich(workspace=workspace, limit=10000)
        matches = [row["id"] for row in rows if row["id"].startswith(reference)]
        return matches[0] if len(matches) == 1 else None

    def latest_session_id(self, *, source=None, workspace=None):
        rows = self.list_sessions_rich(source=source, workspace=workspace, limit=1)
        return rows[0]["id"] if rows else None

    def close(self):
        if self.owns_client:
            self.client.close()

    def close_with_deadline(self, **_kwargs):
        return True


class ServiceTaskQueue:
    """Adapt existing TUI queue controls, with idempotent submission retries."""

    def __init__(self, owner):
        self.owner = owner
        self._guard = threading.RLock()
        self._uncertain = None

    def snapshot(self):
        state = self.owner._service_snapshot
        return {"active": copy.deepcopy(state.get("active_task")),
                "pending": copy.deepcopy(state.get("queue", [])), "paused": bool(state.get("paused")),
                "status": state.get("status", "idle")}

    def _send(self, action, data):
        from mclaw.application.client import ServiceError

        with self._guard:
            fingerprint = (self.owner.session_id, action, repr(data))
            if self._uncertain and self._uncertain[0] != fingerprint:
                raise ServiceError("SUBMISSION_UNKNOWN", "上次提交尚未确认。请先重试相同内容，或重新连接查看队列。")
            request_id = self._uncertain[1] if self._uncertain else uuid.uuid4().hex
            try:
                state = self.owner.service_client.action(self.owner.session_id, action, {**data, "request_id": request_id})
            except ServiceError as exc:
                if exc.code == "SERVICE_UNAVAILABLE":
                    self._uncertain = (fingerprint, request_id)
                raise
            self._uncertain = None
            self.owner._accept_snapshot(state)
            return state

    def submit(self, content, _model="", *, skills=None):
        selected = self.owner._service_snapshot.get("draft_skills", []) if skills is None else skills
        state = self._send("messages", {"content": content, "skills": selected})
        return state

    def supplement(self, content):
        return self._send("messages", {"content": content, "mode": "steer"})

    def resume(self, content=""):
        return self._send("resume", {"content": content})

    def stop(self):
        state = self.owner.service_client.action(self.owner.session_id, "stop")
        self.owner._accept_snapshot(state)

    def edit(self, index, content):
        state = self.owner.service_client.session(self.owner.session_id)
        queue = state.get("queue", [])
        if not 0 <= index < len(queue):
            raise ValueError("队列编号不存在")
        items = [{"id": item["id"], "content": item["content"]} for item in queue]
        if content is None:
            items.pop(index)
        elif content.strip():
            items[index]["content"] = content
        else:
            raise ValueError("请输入消息内容")
        self.owner._accept_snapshot(self.owner.service_client.action(self.owner.session_id, "queue", {"items": items}))

    def update_model(self, _model):
        pass  # Model changes are committed by the model command, preserving provider ID.

    def take_next(self):
        return None  # Only SessionService dispatches work.


class ServiceAgentView:
    """Display data only; deliberately has no conversation execution method."""

    def __init__(self, owner, context):
        self.owner, self.provider_runtime = owner, context
        self.messages = []
        self.context_compressor = None
        self.session_input_tokens = self.session_output_tokens = self.session_api_calls = 0
        self.session_user_messages = 0
        self.platform = "cli"

    @property
    def model(self):
        return self.provider_runtime.model if self.provider_runtime else ""

    def switch_model(self, context):
        state = self.owner.service_client.runtime(self.owner.session_id, context)
        self.owner._accept_snapshot(state)
        self.provider_runtime = context

    def interrupt(self):
        self.owner.runtime.task_queue.stop()

    def close(self, **_kwargs):
        return True
