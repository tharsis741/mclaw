# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import copy
import json
from types import MappingProxyType

import pytest

from mclaw.dsoftbus import protocol
from mclaw.dsoftbus.task_result import (
    TEXT_RESULT_CAPABILITY,
    select_text_result,
    text_result_capability,
    TaskResultValidationError,
    prepare_history,
    result_diagnostics,
    validate_final_response,
)


@pytest.mark.parametrize('value', [None, {}, {'version': 2}, {'version': True},
                                   {'version': 1, 'transport': 'task-artifact', 'maxBytes': True}])
def test_unknown_or_invalid_reply_capability_uses_legacy_behavior(value):
    assert select_text_result({TEXT_RESULT_CAPABILITY: value}) == {}


def test_reply_capability_negotiates_the_smaller_supported_limit():
    for requested, expected in [(32768, 32768), (262144, 262144), (1048576, 262144)]:
        value = {**text_result_capability(), 'maxBytes': requested}
        assert select_text_result({TEXT_RESULT_CAPABILITY: value})[TEXT_RESULT_CAPABILITY]['maxBytes'] == expected


def tool_exchange(identifier: str, text: str, *, parallel: bool = False) -> list[dict]:
    identifiers = [identifier, identifier + "-second"] if parallel else [identifier]
    return [
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": name, "type": "function", "function": {"name": "search", "arguments": "{}"}}
            for name in identifiers
        ]},
        *[{"role": "tool", "tool_call_id": name, "content": text}
          for name in reversed(identifiers)],
    ]


def assert_complete_tools(messages: list[dict]) -> None:
    pending = set()
    for message in messages:
        if message["role"] == "tool":
            assert message["tool_call_id"] in pending
            pending.remove(message["tool_call_id"])
        else:
            assert not pending
            pending = {call["id"] for call in message.get("tool_calls", [])}
    assert not pending


def test_small_history_is_a_detached_copy_of_frozen_executor_values():
    source = [MappingProxyType({"role": "user", "content": [{"type": "text", "text": "question"}]})]
    history = prepare_history(source)
    assert history.messages == [dict(source[0])]
    history.messages[0]["content"][0]["text"] = "modified"
    assert source[0]["content"][0]["text"] == "question"


@pytest.mark.parametrize(
    "calls,result", [(31, "small"), (40, "small"), (1, "x" * 140_000), (1, "文" * 50_000)],
    ids=["boundary-tools", "many-tools", "large-ascii-tool", "large-utf8-tool"],
)
def test_oversized_single_turn_keeps_request_final_answer_and_valid_tool_groups(calls, result):
    source = [{"role": "system", "content": "policy"}, {"role": "user", "content": "original request"}]
    for index in range(calls):
        source.extend(tool_exchange(str(index), result))
    source.append({"role": "assistant", "content": "OK"})
    original = copy.deepcopy(source)
    history = prepare_history(source)
    assert source == original
    assert history.messages[:2] == source[:2]
    assert history.messages[-1] == source[-1]
    assert_complete_tools(history.messages)
    assert len(history.messages) <= protocol.REMOTE_CONTEXT_MESSAGE_MAX
    assert history.byte_length <= protocol.REMOTE_CONTEXT_UTF8_MAX
    assert history.byte_length == len(json.dumps(history.messages, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())


def test_parallel_calls_are_retained_or_removed_together(monkeypatch):
    monkeypatch.setattr(protocol, "REMOTE_CONTEXT_MESSAGE_MAX", 6)
    head = [{"role": "system", "content": "policy"}, {"role": "user", "content": "question"}]
    first, latest = tool_exchange("first", "old", parallel=True), tool_exchange("latest", "new", parallel=True)
    answer = {"role": "assistant", "content": "answer"}
    history = prepare_history([*head, *first, *latest, answer])
    assert history.messages == [*head, *latest, answer]
    assert_complete_tools(history.messages)


def test_old_turn_is_evicted_before_compacting_latest_turn(monkeypatch):
    monkeypatch.setattr(protocol, "REMOTE_CONTEXT_MESSAGE_MAX", 7)
    policy = {"role": "system", "content": "policy"}
    latest = [{"role": "user", "content": "new"}, {"role": "assistant", "content": "new answer"}]
    old = [{"role": "user", "content": "old"}, *tool_exchange("old", "old", parallel=True),
           {"role": "assistant", "content": "old answer"}]
    assert prepare_history([policy, *old, *latest]).messages == [policy, *latest]


def test_waiting_turn_preserves_the_latest_completed_tool_exchange():
    head = [{"role": "user", "content": "original request"}]
    pending = tool_exchange("request-input", '{"pending":true,"message":"which module?"}')
    history = prepare_history([*head, *tool_exchange("large-search", "x" * 140_000), *pending])
    assert history.messages == [*head, *pending]
    assert_complete_tools(history.messages)


def test_oversized_mandatory_context_is_reported_without_truncating_user_input():
    source = [{"role": "user", "content": "x" * 140_000}, {"role": "assistant", "content": "OK"}]
    with pytest.raises(TaskResultValidationError) as caught:
        prepare_history(source)
    assert caught.value.stage == "history_capacity"
    assert caught.value.metrics["retainedBytes"] > protocol.REMOTE_CONTEXT_UTF8_MAX
    assert len(source[0]["content"]) == 140_000


def test_incomplete_tool_exchange_is_not_mistaken_for_a_removable_completed_group():
    messages = [{"role": "user", "content": "request"}, *tool_exchange("a", "x" * 140_000),
                {"role": "assistant", "content": "OK"}]
    messages[2]["tool_call_id"] = "unrelated"
    with pytest.raises(TaskResultValidationError, match="INVALID_AGENT_RESPONSE") as caught:
        prepare_history(messages)
    assert caught.value.stage == "history_tool_sequence"


@pytest.mark.parametrize("value,stage", [
    ("not messages", "history_format"),
    (["not a message"], "history_format"),
    ([{"role": "assistant", "content": float("nan")}], "history_encoding"),
    ([{"role": "assistant", "content": "\ud800"}], "history_encoding"),
])
def test_invalid_history_has_a_content_free_failure_stage(value, stage):
    with pytest.raises(TaskResultValidationError) as caught:
        prepare_history(value)
    assert caught.value.stage == stage


def test_inline_boundary_counts_utf8_bytes_and_preserves_the_reply():
    text = "文" * (protocol.REMOTE_MESSAGE_TEXT_BYTES_MAX // 3)
    assert validate_final_response({"completed": True, "final_response": text}) == text
    with pytest.raises(TaskResultValidationError) as caught:
        validate_final_response({"completed": True, "final_response": text + "x"})
    assert caught.value.stage == "final_response_size"
    assert caught.value.metrics["finalBytes"] == protocol.REMOTE_MESSAGE_TEXT_BYTES_MAX + 1


@pytest.mark.parametrize("raw,stage", [
    ({"completed": False, "final_response": "partial"}, "agent_completion"),
    ({"completed": True, "final_response": ""}, "final_response"),
    ({"completed": True, "final_response": 42}, "final_response"),
    ({"completed": True, "final_response": "\ud800"}, "final_response_encoding"),
])
def test_final_response_rejections_have_distinct_stages(raw, stage):
    with pytest.raises(TaskResultValidationError) as caught:
        validate_final_response(raw)
    assert caught.value.stage == stage


def test_result_diagnostics_exclude_reply_prompt_and_free_form_error_text():
    marker = "private-test-value"
    raw = {"completed": False, "final_response": marker, "messages": [{"role": "user", "content": marker}],
           "stop_reason": marker, "error": marker}
    details = result_diagnostics(raw, TaskResultValidationError("agent_completion"))
    assert details == {"stage": "agent_completion", "completed": False, "finalBytes": len(marker), "historyMessages": 1}
    assert marker not in json.dumps(details)
