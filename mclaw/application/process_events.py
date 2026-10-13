# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Public execution records built from provider text and identified tool calls."""

from __future__ import annotations

import json
import re
from collections.abc import Collection
from pathlib import Path
from typing import Any

from mclaw.runtime.secrets import redact_secret_values

_SECRET_KEY = re.compile(r"^(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|passwd|secret|client[_-]?secret|authorization|cookie|set-cookie)$", re.I)
_INLINE_SECRET = re.compile(r"(?i)(\b(?:authorization\s*[:=]\s*bearer|api[_-]?key\s*[:=]|access[_-]?token\s*[:=]|password\s*[:=])\s*)[^\s,;\"']+")
DISPLAY_LIMIT = 64000


def secret_values(home: Path) -> set[str]:
    from dotenv import dotenv_values

    try:
        return {value for key, value in dotenv_values(home / ".env").items()
                if value and re.search(r"key|token|secret|password|credential", key, re.I)}
    except (OSError, UnicodeError):
        return set()


def public_detail(value: Any, secrets: Collection[str] = ()) -> Any:
    """Remove credentials from structured payloads and embedded JSON output."""
    if isinstance(value, dict):
        return {key: "<redacted>" if _SECRET_KEY.fullmatch(str(key)) else public_detail(item, secrets)
                for key, item in value.items()}
    if isinstance(value, list):
        return [public_detail(item, secrets) for item in value]
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (ValueError, TypeError):
            decoded = None
        if isinstance(decoded, (dict, list)):
            return json.dumps(public_detail(decoded, secrets), ensure_ascii=False, indent=2)
        return _INLINE_SECRET.sub(r"\1<redacted>", redact_secret_values(value, secrets))
    return value


def display_text(value: Any, secrets: Collection[str] = ()) -> tuple[str, bool]:
    safe = public_detail(value, secrets)
    text = safe if isinstance(safe, str) else json.dumps(safe, ensure_ascii=False, indent=2)
    return text[:DISPLAY_LIMIT], len(text) > DISPLAY_LIMIT


def tool_status(result: Any) -> str:
    try:
        data = json.loads(result) if isinstance(result, str) else result
    except (ValueError, TypeError):
        data = None
    if isinstance(data, dict):
        if data.get("interrupted") or data.get("cancelled"):
            return "interrupted"
        if (data.get("error") or data.get("success") is False
                or any(data.get(key) not in (None, 0) for key in ("exit_code", "returncode"))):
            return "failed"
    return "completed"


def agent_events(event: dict, scope: str, secrets: Collection[str] = ()) -> list[dict]:
    """Use the same public conversation records for main and delegated agents."""
    kind = event.get("type")
    records = []
    if kind in {"delta", "body_reset"}:
        return [public_detail(event, secrets)]
    if kind == "status":
        return [{"type": "status", "text": status_text(str(event.get("text") or ""))}]
    if kind == "remote.task":
        entry = public_detail({key: value for key, value in event.items() if key not in {"type", "origin"}}, secrets)
        output = entry.get("output")
        if isinstance(output, dict) and isinstance(output.get("text"), str):
            # Body and technical details have separate budgets. Never parse clipped JSON as a reply.
            entry["result_text"] = output["text"]
            entry["output"] = {**output, "text": "完整回复见上方正文"}
        for key in ("arguments", "output"):
            if key in entry:
                entry[key], entry[key + "_truncated"] = display_text(entry[key], secrets)
        return [{**entry, "type": "process"}]
    if kind in {"assistant.reasoning", "assistant.message"}:
        reasoning = event.get("content") if kind == "assistant.reasoning" else event.get("reasoning")
        if reasoning:
            content, truncated = display_text(reasoning, secrets)
            records.append({"type": "process", "kind": "reasoning", "title": "思考",
                            "id": f"reasoning_{scope}_{event.get('turn_id')}_{event.get('api_call_index')}",
                            "content": content, "truncated": truncated,
                            "status": "running" if kind == "assistant.reasoning" else "completed"})
    if kind == "assistant.message":
        if event.get("is_final"):
            records.append({"type": "body", "text": public_detail(event.get("content") or "", secrets)})
        else:
            if event.get("was_streamed"):
                records.append({"type": "body_reset"})
            if event.get("content_source") != "reasoning_content" and event.get("content"):
                content, truncated = display_text(event["content"], secrets)
                records.append({"type": "process", "kind": "progress", "title": "执行进展",
                                "id": f"progress_{scope}_{event.get('turn_id')}_{event.get('api_call_index')}",
                                "content": content, "truncated": truncated})
    elif kind in {"tool.started", "tool.finished"}:
        entry = {"type": "process", "id": f"tool_{event['tool_call_id']}", "kind": "tool",
                 "title": event["name"], "tool_call_id": event["tool_call_id"]}
        if kind == "tool.started":
            text, truncated = display_text(event.get("arguments") or {}, secrets)
            entry.update(arguments=text, status="running", truncated=truncated)
        else:
            text, truncated = display_text(event.get("result") or "", secrets)
            entry.update(output=text, status=tool_status(event.get("result")),
                         output_truncated=truncated, elapsed_ms=event.get("elapsed_ms"))
        records.append(entry)
    return records


def status_text(text: str) -> str:
    label = {"Thinking...": "思考中…", "Waiting for model...": "等待回复…",
             "Preparing tool call...": "准备调用工具…", "Pruning tool results...": "整理执行记录…",
             "Refreshing context...": "整理上下文…", "Compressing context...": "整理上下文…",
             "Reviewing memory and skills...": "整理记忆和技能…", "Reviewing memory...": "整理记忆…",
             "Reviewing skills...": "整理技能…"}.get(text, text)
    return "正在执行工具…" if label.startswith("Running ") and "tool" in label else label


def history_process(rows: list[dict], secrets: Collection[str] = ()) -> list[dict]:
    """Reconstruct earlier Web/TUI records from persisted call IDs, once."""
    entries: list[dict] = []
    calls: dict[str, dict] = {}
    for row in rows:
        if row["role"] == "assistant":
            reasoning = row.get("reasoning")
            if reasoning:
                text, truncated = display_text(reasoning, secrets)
                entries.append({"id": f"reasoning_{row['id']}", "kind": "reasoning", "title": "思考",
                                "content": text, "status": "completed", "truncated": truncated})
            tool_calls = row.get("tool_calls") or []
            if isinstance(tool_calls, str):
                try:
                    tool_calls = json.loads(tool_calls)
                except (ValueError, TypeError):
                    tool_calls = []
            if tool_calls and row.get("content"):
                content, truncated = display_text(row["content"], secrets)
                entries.append({"id": f"progress_{row['id']}", "kind": "progress", "title": "执行进展",
                                "content": content, "truncated": truncated})
            for call in tool_calls:
                identifier = call.get("id")
                if not identifier or identifier in calls:
                    continue
                fn = call.get("function") or {}
                arguments, truncated = display_text(fn.get("arguments") or {}, secrets)
                entry = {"id": "tool_" + identifier, "kind": "tool", "title": fn.get("name") or "工具调用",
                         "tool_call_id": identifier, "arguments": arguments, "truncated": truncated, "status": "unknown"}
                calls[identifier] = entry
                entries.append(entry)
        elif row["role"] == "tool" and row.get("tool_call_id") in calls:
            entry = calls[row["tool_call_id"]]
            output, truncated = display_text(row.get("content") or "", secrets)
            entry.update(output=output, status=tool_status(row.get("content")),
                         truncated=entry["truncated"] or truncated)
    return entries


def update_process(message: dict, event: dict) -> None:
    identifier = str(event["id"])
    entries = message.setdefault("process", [])
    entry = next((item for item in entries if item["id"] == identifier), None)
    if entry is None:
        entry = {"id": identifier}
        entries.append(entry)
    entry.update({key: value for key, value in event.items() if key not in {"type", "run_id", "turn_id"}})
    resolve_remote_waits([message], entry)


def resolve_remote_waits(messages: list[dict], event: dict) -> None:
    """Resolve earlier input requests only after a continuation is confirmed."""
    if (event.get("kind") != "remote" or not event.get("task_id")
            or event.get("status") not in {"completed", "waiting"}):
        return
    for message in messages:
        for previous in message.get("process", []):
            if previous.get("id") == event["id"]:
                return  # Replayed older events must not resolve a later request.
            if (previous.get("kind") == "remote" and previous.get("status") == "waiting"
                    and previous.get("device_id") == event.get("device_id")
                    and previous.get("task_id") == event["task_id"]):
                previous.update(status="completed", continued=True)


def finish_process(message: dict) -> None:
    for entry in message.get("process", []):
        if entry.get("status") == "running":
            entry["status"] = "unknown" if entry["kind"] in {"tool", "remote"} else "completed"


def history_messages(rows: list[dict], secrets: Collection[str] = ()) -> list[dict]:
    messages = []
    results = {row.get("tool_call_id"): row for row in rows if row["role"] == "tool"}
    for row in rows:
        if row["role"] not in {"user", "assistant"}:
            continue
        if not (row.get("content") or row.get("reasoning") or row.get("tool_calls")):
            continue
        calls = row.get("tool_calls") or []
        if isinstance(calls, str):
            try:
                calls = json.loads(calls)
            except (ValueError, TypeError):
                calls = []
        identifiers = {call.get("id") for call in calls}
        # This content is already displayed as the message body.
        related = [{**row, "content": ""}, *(results[identifier] for identifier in identifiers if identifier in results)]
        messages.append({"id": f"history_{row['id']}", "role": row["role"], "content": row.get("content") or "",
                         "process": history_process(related, secrets) if row["role"] == "assistant" else [],
                         "process_version": 2, "created_at": row.get("timestamp"),
                         "turn_id": row.get("turn_id"), "operation_id": row.get("operation_id"),
                         "invalidated": bool(row.get("invalidated_at"))})
    return messages


def upgrade_process(session: dict, rows: list[dict], secrets: Collection[str] = ()) -> bool:
    """Upgrade only presentation data; leave original history and tasks intact."""
    messages = session.get("messages", [])
    if not any(m["role"] == "assistant" and m.get("process_version") != 2 for m in messages):
        return False
    if messages and all(str(m.get("id", "")).startswith("history_") for m in messages):
        session["messages"] = history_messages(rows, secrets)
        return True
    responses = [m for m in messages if m["role"] == "assistant"]
    imports = {item["id"]: item for item in history_messages(rows, secrets)} if any(
        str(m.get("id", "")).startswith("history_") and m.get("process_version") != 2 for m in responses) else {}
    for index, message in enumerate(responses):
        if message.get("process_version") == 2:
            continue
        start = message.get("created_at") or 0
        end = responses[index + 1].get("created_at") if index + 1 < len(responses) else float("inf")
        related = [row for row in rows if start <= (row.get("timestamp") or 0) < (end or float("inf"))
                   and (not message.get("turn_id") or row.get("turn_id") == message["turn_id"])]
        # An individual imported message does not represent an entire turn.
        if str(message.get("id", "")).startswith("history_"):
            imported = imports.get(message["id"])
            if imported:
                message["process"] = imported["process"]
            message["process_version"] = 2
            continue
        entries = history_process(related, secrets)
        if entries:
            message["process"] = entries
        message["process_version"] = 2
    return True
