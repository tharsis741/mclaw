# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Presentation-only Skill review records in durable application snapshots."""

from __future__ import annotations

import math
import re
import time

from mclaw.agent.skill_review_notice import WRITE_ACTIONS


def notice_text(notice: dict) -> str:
    skills = notice.get("skills") or []
    counts = {"新建": 0, "更新": 0, "删除": 0}
    for skill in skills:
        counts["删除" if skill.get("deleted") else "新建" if "create" in skill["actions"] else "更新"] += 1
    summary = "、".join(f"{label} {count} 项" for label, count in counts.items() if count)
    status = notice["status"]
    title = "技能整理未完成" if not skills else "技能已保存"
    if status in {"partial", "interrupted"} and skills:
        title += "，后续整理未完成"
    elif status == "running":
        title += "，后续整理中"
    return title + (f" · {summary}" if summary else "")


def apply_skill_notice(session: dict, event: dict) -> bool:
    """Accept ordered full snapshots only from this session's owning worker."""
    source = event.get("notice")
    if not isinstance(source, dict) or source.get("session_id") != session["id"]:
        return False
    identifier = source.get("id")
    revision = source.get("revision")
    status = source.get("status")
    if (not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", identifier)
            or type(revision) is not int or revision < 1
            or status not in {"running", "completed", "partial", "failed"}):
        return False
    skills = []
    for item in source.get("skills") or []:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            continue
        actions = [action for action in item.get("actions", []) if action in WRITE_ACTIONS]
        if actions:
            skills.append({"name": item["name"][:200], "description": str(item.get("description") or "")[:280],
                           "actions": actions, "deleted": bool(item.get("deleted"))})
    if not skills and status in {"completed", "running"}:
        return False
    notice = {key: source.get(key) for key in ("id", "session_id", "revision", "status",
                                              "trigger_task_id", "trigger_response_id")}
    for key in ("started_at", "updated_at", "completed_at"):
        value = source.get(key)
        if isinstance(value, (int, float)) and math.isfinite(value):
            notice[key] = value
    notice.update(skills=skills, error=str(source.get("error") or "")[:500])
    message_id = "skill_review_" + identifier
    existing = next((item for item in session["messages"] if item["id"] == message_id), None)
    if existing:
        previous = existing.get("skill_review") or {}
        if (revision <= previous.get("revision", 0)
                or previous.get("status") != "running" and status == "running"
                or any(previous.get(key) != notice.get(key) for key in ("trigger_task_id", "trigger_response_id"))):
            return False
    else:
        existing = {"id": message_id, "role": "system", "source": "skill_review",
                    "created_at": notice.get("updated_at", time.time())}
        session["messages"].append(existing)
    existing.update(skill_review=notice, content=notice_text(notice),
                    skill_review_worker=event.get("worker_id"))
    return True


def interrupt_skill_notices(session: dict, *, worker_id: str | None = None) -> bool:
    """Preserve saved results while marking an exited review as unconfirmed."""
    changed = False
    for message in session.get("messages", []):
        notice = message.get("skill_review")
        if not notice or notice.get("status") != "running":
            continue
        if worker_id is not None and message.get("skill_review_worker") != worker_id:
            continue
        notice.update(status="interrupted", completed_at=time.time(),
                      error="执行进程已退出，后续整理结果未确认。")
        message["content"] = notice_text(notice)
        changed = True
    return changed
