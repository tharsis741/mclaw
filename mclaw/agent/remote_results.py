# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Project remote replies into model context while retaining session-scoped originals."""

from __future__ import annotations

import json
from typing import Any

REMOTE_TASK_TOOLS = frozenset({"dsoftbus_run_agent_task", "dsoftbus_continue_agent_task"})
READ_RESULT_TOOL = "dsoftbus_read_task_result"


def project_reply(agent: Any, result: str, call_id: str, messages: list[dict]) -> str:
    """Only shorten the model projection when the full result cannot fit comfortably."""
    if READ_RESULT_TOOL not in getattr(agent, "valid_tool_names", ()):
        return result
    try:
        value = json.loads(result)
    except (ValueError, TypeError):
        return result
    if (not isinstance(value, dict) or not isinstance(value.get("text"), str)
            or value.get("text_truncated") is True):
        return result
    if not getattr(agent, "_session_db", None):
        # Ephemeral agents have no history database; retain at most 32 bounded replies.
        cache = getattr(agent, "_remote_result_cache", None)
        if cache is None:
            cache = agent._remote_result_cache = {}
        cache[call_id] = value
        while len(cache) > 32:
            cache.pop(next(iter(cache)))
    if len(result) <= 8192 or not getattr(agent, "provider_runtime", None):
        return result
    from mclaw.agent.context_compressor import estimate_tokens_rough
    from mclaw.agent.context_metadata import resolve_context_length
    from mclaw.agent.token_budget import estimate_request_budget

    budget = estimate_request_budget(
        messages=messages, tools=getattr(agent, "tools", []), dynamic_system_context="",
        context=agent.provider_runtime, context_window=resolve_context_length(agent.provider_runtime),
    )
    available = max(1024, int((budget.context_window - budget.total_budget) * 0.7))
    if estimate_tokens_rough(result) <= available:
        return result
    length = min(4096, available)
    return json.dumps({
        **value, "text": value["text"][:length], "text_truncated": True,
        "total_characters": len(value["text"]),
        "read_more": {"tool": READ_RESULT_TOOL, "tool_call_id": call_id, "offset": length},
        "note": "完整回复已收到。当前模型上下文有限，请分段读取原文后分析；不要将本段当作完整结果。",
    }, ensure_ascii=False)


def project_history(agent: Any, messages: list[dict]) -> list[dict]:
    """Apply the same model budget after loading original results from session history."""
    calls = {
        call.get("id"): call.get("function", {}).get("name")
        for message in messages for call in message.get("tool_calls") or []
        if isinstance(call, dict)
    }
    result = list(messages)
    for index, message in enumerate(messages):
        call_id = message.get("tool_call_id")
        if message.get("role") == "tool" and calls.get(call_id) in REMOTE_TASK_TOOLS:
            content = project_reply(agent, message.get("content") or "", call_id,
                                    result[:index] + result[index + 1:])
            if content != message.get("content"):
                result[index] = {**message, "content": content}
    return result


def read_reply(agent: Any, call_id: str, offset: int, limit: int) -> dict:
    """Read original text only from this agent's own session, even after a restart."""
    if not isinstance(call_id, str) or not call_id or len(call_id) > 512:
        raise ValueError("Invalid tool call ID")
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 8192:
        raise ValueError("Invalid text range")
    database = getattr(agent, "_session_db", None)
    if database is not None:
        row = database.get_tool_result(str(agent.session_id), call_id)
        if row is None or row.get("tool_name") not in REMOTE_TASK_TOOLS:
            raise LookupError("Remote reply is not available in this session")
        value = json.loads(row["content"])
    else:
        value = getattr(agent, "_remote_result_cache", {}).get(call_id)
    if not isinstance(value, dict) or not isinstance(value.get("text"), str):
        raise LookupError("Remote reply is not available in this session")
    text = value["text"]
    if offset > len(text):
        raise ValueError("Offset is beyond the reply")
    end = min(len(text), offset + limit)
    return {
        "success": True, "text": text[offset:end], "offset": offset, "next_offset": end,
        "total_characters": len(text), "eof": end == len(text),
        "device_id": value.get("device_id"), "task_id": value.get("task_id"),
        "_mclawProvenance": value.get("_mclawProvenance"), "_untrustedRemoteData": True,
    }
