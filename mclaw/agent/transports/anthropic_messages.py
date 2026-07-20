# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Single-attempt Anthropic Messages transport."""

from __future__ import annotations

import json
import queue
import threading
import time
from contextlib import nullcontext
from copy import deepcopy
from typing import Any, Callable, Mapping

from mclaw.agent.transports import base as transport_base
from mclaw.agent.transports.base import (
    ModelCallOptions,
    ModelCallResult,
    ReasoningTrace,
    json_safe_value,
)
from mclaw.providers.runtime import ProviderRuntimeContext


NONSTREAM_WATCHDOG_TIMEOUT = 90.0
STREAM_CREATE_TIMEOUT = 90.0
STREAM_SAFETY_TIMEOUT = 300.0
STREAM_STALL_TIMEOUT = 60.0
POLL_INTERVAL = 0.05


def _field(value: object, name: str, default: Any = None) -> Any:
    return value.get(name, default) if isinstance(value, Mapping) else getattr(value, name, default)


def _arguments(value: Any) -> str:
    try:
        return json.dumps(json_safe_value(value), ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return "{}"


def _usage_fields(raw: object) -> dict[str, int]:
    result: dict[str, int] = {}
    for name in (
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    ):
        value = _field(raw, name)
        if isinstance(value, int) and not isinstance(value, bool):
            result[name] = value
    return result


class AnthropicMessagesTransport(transport_base.ModelTransport):
    """Convert canonical messages and execute one Anthropic SDK attempt."""

    def call(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        options: ModelCallOptions,
        stream_callback: Callable[[str], None] | None = None,
        interrupted: Callable[[], bool] | None = None,
    ) -> ModelCallResult:
        try:
            kwargs = self._request_kwargs(messages, tools or [], options)
            traits = self.context.profile.model_traits(self.context.model)
            if options.stream or traits.requires_stream:
                return self._stream(kwargs, options, stream_callback, interrupted)
            return self._nonstream(kwargs, options, interrupted)
        except transport_base.ModelCallError:
            raise
        except Exception as exc:
            raise transport_base.normalize_model_call_error(exc, self.context) from None

    def _request_kwargs(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        options: ModelCallOptions,
    ) -> dict[str, Any]:
        system, conversation = self._convert_messages(messages)
        traits = self.context.profile.model_traits(self.context.model)
        base: dict[str, Any] = {
            "model": self.context.model,
            "messages": conversation,
            "max_tokens": traits.max_output_tokens or 8_192,
            "timeout": options.timeout,
        }
        if system:
            base["system"] = system
        if tools and traits.supports_tools:
            base["tools"] = self._convert_tools(tools)
        prepared = self.context.profile.prepare_request(base, self.context, options)
        return prepared

    @staticmethod
    def _convert_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        converted = []
        for tool in deepcopy(tools):
            function = tool.get("function", {}) if isinstance(tool, dict) else {}
            converted.append({
                "name": function.get("name", ""),
                "description": function.get("description", ""),
                "input_schema": function.get(
                    "parameters",
                    {"type": "object", "properties": {}},
                ),
            })
        return converted

    @staticmethod
    def _convert_messages(
        messages: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        system_blocks: list[dict[str, Any]] = []
        conversation: list[dict[str, Any]] = []

        for original in deepcopy(messages):
            role = str(original.get("role") or "")
            content = original.get("content", "")
            if role == "system":
                if isinstance(content, list):
                    system_blocks.extend(
                        deepcopy(block)
                        if isinstance(block, dict)
                        else {"type": "text", "text": str(block)}
                        for block in content
                        if isinstance(block, dict) or str(block)
                    )
                elif content:
                    system_blocks.append({"type": "text", "text": str(content)})
                continue
            if role == "tool":
                block = {
                    "type": "tool_result",
                    "tool_use_id": str(original.get("tool_call_id") or ""),
                    "content": content,
                }
                if isinstance(original.get("is_error"), bool):
                    block["is_error"] = original["is_error"]
                previous = conversation[-1] if conversation else None
                previous_content = previous.get("content") if isinstance(previous, dict) else None
                if (
                    isinstance(previous, dict)
                    and previous.get("role") == "user"
                    and isinstance(previous_content, list)
                    and all(
                        isinstance(item, dict) and item.get("type") == "tool_result"
                        for item in previous_content
                    )
                ):
                    previous_content.append(block)
                else:
                    conversation.append({"role": "user", "content": [block]})
                continue
            if role == "assistant" and original.get("tool_calls"):
                blocks = deepcopy(content) if isinstance(content, list) else []
                if content and not isinstance(content, list):
                    blocks.append({"type": "text", "text": str(content)})
                for call in original.get("tool_calls", []):
                    function = call.get("function", {}) if isinstance(call, dict) else {}
                    try:
                        arguments = json.loads(function.get("arguments") or "{}")
                    except (TypeError, json.JSONDecodeError):
                        arguments = {}
                    blocks.append({
                        "type": "tool_use",
                        "id": str(call.get("id") or ""),
                        "name": str(function.get("name") or ""),
                        "input": arguments,
                    })
                converted = {"role": "assistant", "content": blocks}
            else:
                converted = {"role": role, "content": content}
            if role == "assistant":
                for name in ("reasoning", "reasoning_details"):
                    if name in original:
                        converted[name] = original[name]
            conversation.append(converted)

        return system_blocks, conversation

    def _nonstream(
        self,
        kwargs: dict[str, Any],
        options: ModelCallOptions,
        interrupted: Callable[[], bool] | None,
    ) -> ModelCallResult:
        result: list[Any] = [None]

        def invoke() -> None:
            try:
                result[0] = self.client.messages.create(**kwargs)
            except Exception as exc:
                result[0] = exc

        worker = threading.Thread(target=invoke, daemon=True)
        worker.start()
        deadline = time.monotonic() + max(
            NONSTREAM_WATCHDOG_TIMEOUT,
            options.timeout,
        )
        while worker.is_alive():
            if interrupted and interrupted():
                return self._interrupted_result(False)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Anthropic request exceeded the hard watchdog timeout")
            worker.join(min(POLL_INTERVAL, remaining))
        if interrupted and interrupted():
            return self._interrupted_result(False)
        if isinstance(result[0], Exception):
            raise result[0]
        if result[0] is None:
            raise TimeoutError("Anthropic request returned no response")
        return self._result_from_response(result[0], options, was_streamed=False)

    def _stream(
        self,
        kwargs: dict[str, Any],
        options: ModelCallOptions,
        stream_callback: Callable[[str], None] | None,
        interrupted: Callable[[], bool] | None,
    ) -> ModelCallResult:
        events: queue.Queue[tuple[float, Any]] = queue.Queue()
        done = threading.Event()
        started = threading.Event()
        stop_requested = threading.Event()
        stream_holder: list[Any] = [None]
        stream_started_holder: list[float | None] = [None]
        producer_error: list[Exception | None] = [None]
        created_at = time.monotonic()

        def produce() -> None:
            try:
                manager = self.client.messages.stream(**kwargs)
                context = manager if hasattr(manager, "__enter__") else nullcontext(manager)
                with context as stream:
                    stream_holder[0] = stream
                    stream_started_holder[0] = time.monotonic()
                    started.set()
                    if stop_requested.is_set():
                        close = getattr(stream, "close", None)
                        if callable(close):
                            close()
                        return
                    for event in stream:
                        if stop_requested.is_set():
                            break
                        events.put((time.monotonic(), event))
            except Exception as exc:
                producer_error[0] = exc
            finally:
                done.set()

        threading.Thread(target=produce, daemon=True).start()
        stream_started_at: float | None = None
        last_event_at = created_at
        timed_out = False
        timeout_finish_reason = "stream_stalled"
        stream_create_timeout = max(STREAM_CREATE_TIMEOUT, options.timeout)
        stream_safety_timeout = max(STREAM_SAFETY_TIMEOUT, options.timeout)
        stream_stall_timeout = max(STREAM_STALL_TIMEOUT, options.timeout)
        was_interrupted = False
        complete = False
        finish_reason: str | None = None
        content: list[str] = []
        thinking: dict[int, dict[str, Any]] = {}
        tool_uses: dict[int, dict[str, Any]] = {}
        completed_tool_uses: set[int] = set()
        usage: dict[str, int] = {}

        def emit(text: str) -> None:
            if not text or stream_callback is None:
                return
            try:
                stream_callback(text)
            except Exception:
                stream = stream_holder[0]
                close = getattr(stream, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass
                raise

        def merge_usage(event: object) -> None:
            event_type = _field(event, "type", "")
            if event_type == "message_start":
                usage.update(_usage_fields(_field(_field(event, "message"), "usage")))
            elif event_type == "message_delta":
                usage.update(_usage_fields(_field(event, "usage")))
            elif event_type == "message_stop":
                usage.update(_usage_fields(_field(_field(event, "message"), "usage")))

        while not done.is_set() or not events.empty():
            if complete and events.empty():
                break
            if interrupted and interrupted():
                was_interrupted = True
                stop_requested.set()
                while True:
                    try:
                        _arrived_at, pending = events.get_nowait()
                    except queue.Empty:
                        break
                    merge_usage(pending)
                break
            now = time.monotonic()
            stream_is_started = started.is_set()
            if not stream_is_started:
                if now - created_at >= stream_create_timeout:
                    timed_out = True
                    timeout_finish_reason = "stream_create_timeout"
                    stop_requested.set()
                    break
            else:
                if stream_started_at is None:
                    stream_started_at = stream_started_holder[0] or now
                    last_event_at = stream_started_at
                if now - stream_started_at >= stream_safety_timeout:
                    timed_out = True
                    timeout_finish_reason = "stream_timeout"
                    stop_requested.set()
                    break
            try:
                arrived_at, event = events.get_nowait()
            except queue.Empty:
                if stream_is_started and now - last_event_at >= stream_stall_timeout:
                    timed_out = True
                    timeout_finish_reason = "stream_stalled"
                    stop_requested.set()
                    break
                wait_timeout = POLL_INTERVAL
                if not stream_is_started:
                    wait_timeout = min(
                        wait_timeout,
                        stream_create_timeout - (now - created_at),
                    )
                else:
                    wait_timeout = min(
                        wait_timeout,
                        stream_safety_timeout - (now - stream_started_at),
                        stream_stall_timeout - (now - last_event_at),
                    )
                try:
                    arrived_at, event = events.get(timeout=wait_timeout)
                except queue.Empty:
                    continue
            if stream_started_at is None:
                stream_started_at = stream_started_holder[0] or arrived_at
                last_event_at = stream_started_at
            now = time.monotonic()
            if now - stream_started_at >= stream_safety_timeout:
                timed_out = True
                timeout_finish_reason = "stream_timeout"
                stop_requested.set()
                break
            if arrived_at - last_event_at >= stream_stall_timeout:
                timed_out = True
                timeout_finish_reason = "stream_stalled"
                stop_requested.set()
                break
            last_event_at = arrived_at
            event_type = _field(event, "type", "")
            if event_type == "message_start":
                message = _field(event, "message")
                usage.update(_usage_fields(_field(message, "usage")))
                finish_reason = _field(message, "stop_reason") or finish_reason
            elif event_type == "content_block_start":
                index = int(_field(event, "index", 0))
                block = _field(event, "content_block")
                block_type = _field(block, "type", "")
                if block_type == "text" and (text := _field(block, "text", "")):
                    content.append(str(text))
                    emit(str(text))
                elif block_type == "thinking":
                    thinking[index] = {
                        "type": "thinking",
                        "thinking": str(_field(block, "thinking", "")),
                        "signature": str(_field(block, "signature", "")),
                    }
                elif block_type == "redacted_thinking":
                    thinking[index] = {
                        "type": "redacted_thinking",
                        "data": str(_field(block, "data", "")),
                    }
                elif block_type == "tool_use":
                    tool_uses[index] = {
                        "id": str(_field(block, "id", "")),
                        "name": str(_field(block, "name", "")),
                        "input": json_safe_value(_field(block, "input", {})),
                        "partial_json": "",
                    }
            elif event_type == "content_block_delta":
                index = int(_field(event, "index", 0))
                delta = _field(event, "delta")
                delta_type = _field(delta, "type", "")
                if delta_type == "text_delta":
                    text = str(_field(delta, "text", ""))
                    content.append(text)
                    emit(text)
                elif delta_type == "thinking_delta":
                    block = thinking.setdefault(index, {"type": "thinking", "thinking": "", "signature": ""})
                    block["thinking"] += str(_field(delta, "thinking", ""))
                elif delta_type == "signature_delta":
                    block = thinking.setdefault(index, {"type": "thinking", "thinking": "", "signature": ""})
                    block["signature"] += str(_field(delta, "signature", ""))
                elif delta_type == "input_json_delta":
                    block = tool_uses.setdefault(index, {"id": "", "name": "", "input": {}, "partial_json": ""})
                    block["partial_json"] += str(_field(delta, "partial_json", ""))
            elif event_type == "content_block_stop":
                index = int(_field(event, "index", 0))
                if index in tool_uses:
                    partial = str(tool_uses[index].get("partial_json") or "")
                    if not partial:
                        completed_tool_uses.add(index)
                    else:
                        try:
                            parsed = json.loads(partial)
                        except (TypeError, json.JSONDecodeError):
                            parsed = None
                        if isinstance(parsed, dict):
                            completed_tool_uses.add(index)
            elif event_type == "message_delta":
                finish_reason = _field(_field(event, "delta"), "stop_reason") or finish_reason
                usage.update(_usage_fields(_field(event, "usage")))
            elif event_type == "message_stop":
                complete = True
                message = _field(event, "message")
                if message is not None:
                    finish_reason = _field(message, "stop_reason") or finish_reason
                    usage.update(_usage_fields(_field(message, "usage")))

        stop_requested.set()
        stream = stream_holder[0]
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass

        has_payload = bool(content or thinking or completed_tool_uses or finish_reason)
        if was_interrupted:
            finish_reason = "interrupted"
        elif producer_error[0] is not None and not has_payload:
            raise producer_error[0]
        elif timed_out and not has_payload:
            raise TimeoutError("Anthropic stream stalled before returning payload")
        elif not complete and timed_out:
            finish_reason = timeout_finish_reason
        elif not complete and producer_error[0] is not None:
            finish_reason = "stream_error"
        elif not complete and has_payload:
            finish_reason = finish_reason or "stream_stalled"
        elif not has_payload:
            raise TimeoutError("Anthropic stream returned no payload")

        return self._assembled_result(
            content=content,
            thinking=thinking,
            tool_uses={
                index: tool_uses[index]
                for index in sorted(completed_tool_uses)
            },
            finish_reason=finish_reason,
            usage=usage,
            source=options.source,
            was_streamed=True,
            interrupted=was_interrupted,
        )

    def _result_from_response(
        self,
        response: object,
        options: ModelCallOptions,
        *,
        was_streamed: bool,
    ) -> ModelCallResult:
        content: list[str] = []
        thinking: dict[int, dict[str, Any]] = {}
        tool_uses: dict[int, dict[str, Any]] = {}
        for index, block in enumerate(_field(response, "content", []) or []):
            block_type = _field(block, "type", "")
            if block_type == "text":
                content.append(str(_field(block, "text", "")))
            elif block_type == "thinking":
                thinking[index] = {
                    "type": "thinking",
                    "thinking": str(_field(block, "thinking", "")),
                    "signature": str(_field(block, "signature", "")),
                }
            elif block_type == "redacted_thinking":
                thinking[index] = {
                    "type": "redacted_thinking",
                    "data": str(_field(block, "data", "")),
                }
            elif block_type == "tool_use":
                tool_uses[index] = {
                    "id": str(_field(block, "id", "")),
                    "name": str(_field(block, "name", "")),
                    "input": json_safe_value(_field(block, "input", {})),
                    "partial_json": "",
                }
        return self._assembled_result(
            content=content,
            thinking=thinking,
            tool_uses=tool_uses,
            finish_reason=_field(response, "stop_reason"),
            usage=_usage_fields(_field(response, "usage")),
            source=options.source,
            was_streamed=was_streamed,
            interrupted=False,
        )

    def _assembled_result(
        self,
        *,
        content: list[str],
        thinking: dict[int, dict[str, Any]],
        tool_uses: dict[int, dict[str, Any]],
        finish_reason: str | None,
        usage: dict[str, int],
        source: str,
        was_streamed: bool,
        interrupted: bool,
    ) -> ModelCallResult:
        reasoning_blocks = [thinking[index] for index in sorted(thinking)]
        reasoning_text = "".join(
            str(block.get("thinking") or "")
            for block in reasoning_blocks
            if block.get("type") == "thinking"
        )
        reasoning = (
            ReasoningTrace(
                text=reasoning_text or None,
                provider=self.context.provider,
                model=self.context.model,
                api_mode=self.context.api_mode,
                format="anthropic_thinking_blocks",
                payload=reasoning_blocks,
            )
            if reasoning_blocks
            else None
        )
        tool_calls = []
        for index in sorted(tool_uses):
            block = tool_uses[index]
            partial = str(block.get("partial_json") or "")
            tool_calls.append({
                "id": block["id"],
                "type": "function",
                "function": {
                    "name": block["name"],
                    "arguments": partial or _arguments(block.get("input", {})),
                },
            })
        parsed_usage = self.context.profile.parse_usage(
            usage or None,
            self.context,
            source=source,
        )
        return ModelCallResult(
            content="".join(content),
            tool_calls=tool_calls or None,
            finish_reason=finish_reason,
            reasoning=reasoning,
            usage=parsed_usage,
            was_streamed=was_streamed,
            provider=self.context.provider,
            model=self.context.model,
            interrupted=interrupted,
        )

    def _interrupted_result(self, was_streamed: bool) -> ModelCallResult:
        return ModelCallResult(
            content="",
            tool_calls=None,
            finish_reason="interrupted",
            reasoning=None,
            usage=None,
            was_streamed=was_streamed,
            provider=self.context.provider,
            model=self.context.model,
            interrupted=True,
        )
