# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Report committed background Skill writes independently of model prose."""

from __future__ import annotations

import copy
import threading
import time
from typing import Any, Callable

from mclaw.agent.skill_review_state import SkillReviewRequest


WRITE_ACTIONS = frozenset({"create", "edit", "patch", "delete", "write_file", "remove_file", "evolution_update"})


class SkillReviewNotice:
    """Keep one immutable owner and ordered revisions for a review's lifetime."""

    def __init__(self, request: SkillReviewRequest, emit: Callable[[dict], None], *, session_id: str | None) -> None:
        self._emit = emit
        self._lock = threading.Lock()
        self._skills: dict[str, dict] = {}
        self._notice = {
            "id": request.review_id, "session_id": session_id,
            "trigger_task_id": request.trigger_task_id,
            "trigger_response_id": request.trigger_response_id,
            "started_at": time.time(), "revision": 0, "status": "running",
        }

    def _publish(self) -> None:
        self._notice["revision"] += 1
        self._notice["updated_at"] = time.time()
        self._emit({"type": "skill_review", "notice": copy.deepcopy({
            **self._notice, "skills": list(self._skills.values()),
        })})

    def saved(self, action: str, result: dict[str, Any]) -> None:
        if result.get("success") is not True or action not in WRITE_ACTIONS:
            return
        name = str(result.get("name") or "").strip()
        if not name:
            return
        with self._lock:
            item = self._skills.setdefault(name, {"name": name, "description": "", "actions": []})
            # This aggregates tool actions in one notification, not Skill content.
            if action not in item["actions"]:
                item["actions"].append(action)
            item["deleted"] = action == "delete"
            if action != "delete":
                try:
                    from mclaw.skills_hub.skill_store import get_skill_record
                    record = get_skill_record(name)
                    item["description"] = str(record.get("short_description") or "")[:280]
                except Exception:
                    pass  # A later read must not hide a committed write.
            if self._notice["status"] != "running":
                # A tool that drains after cancellation can still report a
                # committed write. Preserve it in the same final record.
                if self._notice["status"] == "failed":
                    self._notice["status"] = "partial"
                self._notice["completed_at"] = time.time()
            self._publish()

    def finish(self, result: dict | None = None, *, failed: bool = False) -> list[dict]:
        with self._lock:
            result = result or {}
            completed = not failed and result.get("completed") is True
            if completed and not self._skills:
                return []
            self._notice["status"] = "completed" if completed else "partial" if self._skills else "failed"
            self._notice["completed_at"] = time.time()
            if not completed:
                reason = result.get("abort_reason") or result.get("stop_reason")
                self._notice["error"] = {
                    "timeout": "整理请求超时。",
                    "first_response_timeout": "等待模型响应超时。",
                    "stream_idle_timeout": "模型响应中断。",
                    "max_iterations": "整理已达到本次调用上限。",
                    "user_cancelled": "整理已中断。",
                    "startup_failed": "后台整理未能启动。",
                }.get(reason, "后台整理未能完成，请在诊断日志中查看原因。")
            self._publish()
            return copy.deepcopy(list(self._skills.values()))
