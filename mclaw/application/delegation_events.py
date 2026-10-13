# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Durable child conversations, separate from their parent's reply and tools."""

from __future__ import annotations

from mclaw.application.process_events import finish_process, update_process

ACTIVE = {"pending", "running", "finalizing"}


def _task(group: dict, identifier: str, index: int, goal: str = "", timestamp=None) -> dict:
    tasks = group.setdefault("tasks", {})
    key = str(index)
    if key not in tasks:
        prefix = f"{identifier}_{index}"
        tasks[key] = {"task_index": index, "goal": goal, "status": "pending", "messages": [
            {"id": prefix + "_assignment", "role": "user", "source": "delegate", "content": goal,
             "created_at": timestamp},
            {"id": prefix + "_reply", "role": "assistant", "content": "", "process": [], "process_version": 2},
        ]}
    return tasks[key]


def update_delegation(message: dict, event: dict) -> None:
    identifier = event["delegation_id"]
    group = message.setdefault("delegations", {}).setdefault(identifier, {"events": []})
    kind, data = event["event_type"], event.get("data") or {}
    timestamp = event.get("timestamp")
    if kind == "batch_started":
        group.update(data)
        for index, goal in enumerate(data.get("goals", [])):
            _task(group, identifier, index, goal, timestamp)
        return
    if kind == "batch_completed":
        group["result"] = data
        for result in data.get("results", []):
            task = _task(group, identifier, result["task_index"], result.get("goal", ""))
            # A completed event may hold a longer answer than the tool's handoff summary.
            if task["status"] in ACTIVE:
                _finish(task, result, timestamp)
            for field in ("model", "tokens", "api_calls", "duration_seconds", "exit_reason"):
                if field in result:
                    task[field] = result[field]
        return
    index = event.get("task_index", -1)
    if index < 0:
        return
    task = _task(group, identifier, index, data.get("goal", ""), timestamp)
    reply = task["messages"][-1]
    if kind == "activity":
        if task["status"] not in ACTIVE:
            return
        activity = data.get("type")
        if activity == "delta":
            reply["content"] += str(data.get("text") or "")
        elif activity == "body":
            reply["content"] = str(data.get("text") or "")
        elif activity == "body_reset":
            reply["content"] = ""
        elif activity == "process":
            update_process(reply, data)
        elif activity == "status":
            task["status_text"] = str(data.get("text") or "")
        return
    # Keep the existing compact TUI progress contract; streamed text lives only in messages.
    group["events"].append({key: value for key, value in event.items() if key not in {"type", "run_id"}})
    group["events"] = group["events"][-200:]
    if kind == "started":
        task.update(status="running", started_at=timestamp, model=data.get("model"), session_id=data.get("session_id"))
        reply.setdefault("created_at", timestamp)
        if data.get("goal"):
            task["goal"] = task["messages"][0]["content"] = data["goal"]
    elif kind == "finalizing":
        task.update(status="finalizing", status_text="正在整理结果…")
        if reply["content"]:
            update_process(reply, {"type": "process", "id": reply["id"] + "_partial",
                                   "kind": "progress", "title": "执行进展", "content": reply["content"]})
            reply["content"] = ""
    elif kind == "tool_call":
        task["last_tool"] = data.get("tool")
    elif kind in {"completed", "error"}:
        _finish(task, {**data, "status": "failed" if kind == "error" else data.get("status", "completed")}, timestamp)


def _finish(task: dict, data: dict, timestamp) -> None:
    task.update(status=data.get("status") or "completion_unknown", status_text="", ended_at=timestamp,
                duration_seconds=data.get("duration_seconds", data.get("duration")), error=data.get("error"),
                exit_reason=data.get("exit_reason"), api_calls=data.get("api_calls"))
    if task["status"] == "error":
        task["status"] = "failed"
    reply = task["messages"][-1]
    if data.get("summary"):
        reply["content"] = data["summary"]
    finish_process(reply)


def finish_delegations(message: dict) -> None:
    """Do not leave children looking alive after their owning turn exits."""
    for group in message.get("delegations", {}).values():
        for task in group.get("tasks", {}).values():
            if task["status"] in ACTIVE:
                task.update(status="completion_unknown", status_text="")
                finish_process(task["messages"][-1])
