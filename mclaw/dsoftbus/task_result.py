# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Validate remote replies separately from their bounded continuation context."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
from typing import Any

from . import protocol
from .agent_message import AgentMessageError


TEXT_RESULT_CAPABILITY = "mclaw.textResult"


def text_result_capability() -> dict[str, Any]:
    """Opt in per request so legacy AgentCard validation remains unchanged."""
    return {"version": 1, "transport": "task-artifact", "maxBytes": protocol.REMOTE_TEXT_RESULT_BYTES_MAX}


def select_text_result(metadata: Any) -> dict[str, Any]:
    value = metadata.get(TEXT_RESULT_CAPABILITY) if isinstance(metadata, Mapping) else None
    if (not isinstance(value, Mapping) or type(value.get("version")) is not int
            or value["version"] != 1 or value.get("transport") != "task-artifact"
            or type(value.get("maxBytes")) is not int
            or value["maxBytes"] <= protocol.REMOTE_MESSAGE_TEXT_BYTES_MAX):
        return {}
    return {TEXT_RESULT_CAPABILITY: {
        **text_result_capability(), "maxBytes": min(value["maxBytes"], protocol.REMOTE_TEXT_RESULT_BYTES_MAX),
    }}


def text_result_limit(metadata: Any) -> int:
    selected = select_text_result(metadata)
    return selected.get(TEXT_RESULT_CAPABILITY, {}).get("maxBytes", protocol.REMOTE_MESSAGE_TEXT_BYTES_MAX)


class TaskResultValidationError(AgentMessageError):
    """Keep the existing wire error with content-free diagnostic details."""

    def __init__(self, stage: str, **metrics: int) -> None:
        super().__init__("INVALID_AGENT_RESPONSE")
        self.stage = stage
        self.metrics = metrics


@dataclass(frozen=True, slots=True)
class TaskHistory:
    messages: list[dict[str, Any]]
    byte_length: int
    original_message_count: int
    original_byte_length: int


def result_diagnostics(value: Any, error: AgentMessageError) -> dict[str, Any]:
    """Describe a rejection without logging reply text, prompts or credentials."""
    raw = value if isinstance(value, Mapping) else {}
    text = raw.get("final_response")
    history = raw.get("messages")
    try:
        text_bytes = len(text.encode("utf-8")) if isinstance(text, str) else -1
    except UnicodeEncodeError:
        text_bytes = -1
    details = {
        "stage": getattr(error, "stage", "completion"),
        "completed": raw.get("completed") is True,
        "finalBytes": text_bytes,
        "historyMessages": len(history) if isinstance(history, (list, tuple)) else -1,
    }
    if isinstance(error, TaskResultValidationError):
        details.update(error.metrics)
    return details


def validate_final_response(
    raw: Mapping[str, Any], *, maximum: int = protocol.REMOTE_MESSAGE_TEXT_BYTES_MAX,
) -> str:
    """Validate the currently negotiated inline reply, independently of history."""
    text = raw.get("final_response")
    if raw.get("completed") is not True:
        raise TaskResultValidationError("agent_completion")
    if not isinstance(text, str) or not text:
        raise TaskResultValidationError("final_response")
    try:
        byte_length = len(text.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise TaskResultValidationError("final_response_encoding") from error
    if byte_length > maximum:
        raise TaskResultValidationError(
            "final_response_size",
            finalBytes=byte_length,
            limitBytes=maximum,
        )
    return text


def continuation_history(value: Any, text: str, task_id: str) -> Any:
    """Keep a bounded continuation copy; the complete reply is persisted separately."""
    if (len(text.encode("utf-8")) <= protocol.REMOTE_MESSAGE_TEXT_BYTES_MAX
            and len(_encode(text)) <= 49_152):
        return value
    messages = _plain(value)
    if not isinstance(messages, list):
        return messages
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") == "assistant" and message.get("content") == text:
            head = text.encode("utf-8")[:8192].decode("utf-8", errors="ignore")
            tail = text.encode("utf-8")[-4096:].decode("utf-8", errors="ignore")
            message["content"] = (
                f"{head}\n\n[续聊上下文节选：完整回复已回传，任务 {task_id}，"
                f"共 {len(text.encode('utf-8'))} 字节。]\n\n{tail}"
            )
            # Reasoning belongs to intermediate context, not the delivered answer.
            for key in ("reasoning", "reasoning_content", "reasoning_details"):
                message.pop(key, None)
            break
    return messages


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _encode(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _tool_units(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Group one assistant's parallel tool calls with all of their results."""
    units = []
    index = 0
    while index < len(messages):
        item = messages[index]
        end = index + 1
        calls = item.get("tool_calls")
        if item.get("role") == "assistant" and calls:
            if not isinstance(calls, list) or any(
                not isinstance(call, dict) or not isinstance(call.get("id"), str)
                or not call["id"] for call in calls
            ):
                raise TaskResultValidationError("history_tool_sequence")
            expected = [call["id"] for call in calls]
            while end < len(messages) and messages[end].get("role") == "tool":
                end += 1
            actual = [result.get("tool_call_id") for result in messages[index + 1:end]]
            if (len(expected) != len(set(expected)) or len(actual) != len(expected)
                    or any(not isinstance(identifier, str) for identifier in actual)
                    or set(actual) != set(expected)):
                raise TaskResultValidationError("history_tool_sequence")
        elif item.get("role") == "tool":
            raise TaskResultValidationError("history_tool_sequence")
        units.append(messages[index:end])
        index = end
    return units


def prepare_history(value: Any) -> TaskHistory:
    """Evict whole turns, then complete tool exchanges within an oversized turn.

    Preserve system context, the latest request and the last response or tool
    exchange. A final answer remains independent of omitted intermediate work.
    """
    if not isinstance(value, (tuple, list)) or any(
        not isinstance(item, Mapping) for item in value
    ):
        raise TaskResultValidationError("history_format")
    try:
        original = _encode(_plain(value))
        messages = json.loads(original)
    except (TypeError, ValueError, OverflowError, RecursionError) as error:
        raise TaskResultValidationError("history_encoding") from error
    original_count, original_bytes = len(messages), len(original)
    sizes = {id(message): len(_encode(message)) for message in messages}

    def size(items: list[dict[str, Any]]) -> int:
        return 2 + max(0, len(items) - 1) + sum(sizes[id(item)] for item in items)

    def fits(items: list[dict[str, Any]]) -> bool:
        return (len(items) <= protocol.REMOTE_CONTEXT_MESSAGE_MAX
                and size(items) <= protocol.REMOTE_CONTEXT_UTF8_MAX)

    def snapshot(items: list[dict[str, Any]]) -> TaskHistory:
        return TaskHistory(items, size(items), original_count, original_bytes)

    if fits(messages):
        return snapshot(messages)
    users = [index for index, message in enumerate(messages) if message.get("role") == "user"]
    if not users:
        raise TaskResultValidationError(
            "history_capacity", historyMessages=original_count, historyBytes=original_bytes
        )
    prefix = messages[:users[0]]
    # User boundaries keep old tool calls and their results in the same turn.
    for start in users[1:]:
        retained = prefix + messages[start:]
        if fits(retained):
            return snapshot(retained)
    latest = messages[users[-1]:]
    units = _tool_units(latest[1:])
    retained = prefix + latest
    for index, unit in enumerate(units[:-1]):
        if unit[0].get("role") != "assistant":
            continue
        units[index] = []
        retained = prefix + latest[:1] + [item for group in units for item in group]
        if fits(retained):
            return snapshot(retained)
    raise TaskResultValidationError(
        "history_capacity",
        historyMessages=original_count,
        historyBytes=original_bytes,
        retainedMessages=len(retained),
        retainedBytes=size(retained),
    )
