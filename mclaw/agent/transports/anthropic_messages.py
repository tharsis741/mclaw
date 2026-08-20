# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Single-attempt Anthropic Messages transport."""

from __future__ import annotations

import json
import threading
import time
from contextlib import nullcontext
from copy import deepcopy
from typing import Any, Callable, Mapping

from mclaw.agent.transports import base as transport_base
from mclaw.agent.transports.base import (
    BoundedNormalizedEventBuffer,
    ModelCallError,
    ModelCallOptions,
    ModelCallResult,
    ReasoningTrace,
    StreamBufferLimitError,
    effective_call_deadline,
    json_safe_value,
    remaining_call_time,
)
from mclaw.providers.runtime import ProviderRuntimeContext

NONSTREAM_WATCHDOG_TIMEOUT = 90.0
STREAM_CREATE_TIMEOUT = 90.0
STREAM_SAFETY_TIMEOUT = 300.0
STREAM_STALL_TIMEOUT = 60.0
POLL_INTERVAL = 0.05

_MISSING = object()
_INTERRUPTED = object()


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


def _output_limit_error(context: ProviderRuntimeContext) -> ModelCallError:
    return ModelCallError(
        message="Provider response exceeded the local output limit",
        provider=context.provider,
        model=context.model,
        code="INVALID_AGENT_RESPONSE",
    )


def _close_quietly(value: object) -> None:
    close = getattr(value, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            return


def _normalized_stream_event(event: Any) -> dict[str, Any]:
    """Detach the consumer from provider SDK objects before queue admission."""

    event_type = str(_field(event, "type", ""))
    normalized: dict[str, Any] = {"type": event_type}
    if event_type in {"content_block_start", "content_block_delta", "content_block_stop"}:
        normalized["index"] = _field(event, "index", 0)
    if event_type == "message_start":
        message = _field(event, "message")
        normalized["message"] = {
            "stop_reason": _field(message, "stop_reason"),
            "usage": json_safe_value(_field(message, "usage")),
        }
    elif event_type == "content_block_start":
        block = _field(event, "content_block")
        normalized["content_block"] = {
            name: json_safe_value(_field(block, name))
            for name in (
                "type",
                "text",
                "thinking",
                "signature",
                "data",
                "id",
                "name",
                "input",
            )
            if _field(block, name) is not None
        }
    elif event_type == "content_block_delta":
        delta = _field(event, "delta")
        normalized["delta"] = {
            name: json_safe_value(_field(delta, name))
            for name in (
                "type",
                "text",
                "thinking",
                "signature",
                "partial_json",
            )
            if _field(delta, name) is not None
        }
    elif event_type == "message_delta":
        normalized["delta"] = {
            "stop_reason": _field(_field(event, "delta"), "stop_reason")
        }
        normalized["usage"] = json_safe_value(_field(event, "usage"))
    elif event_type == "message_stop":
        message = _field(event, "message")
        normalized["message"] = {
            "stop_reason": _field(message, "stop_reason"),
            "usage": json_safe_value(_field(message, "usage")),
        }
    return normalized


class AnthropicMessagesTransport(transport_base.ModelTransport):
    """Convert canonical messages and execute one Anthropic SDK attempt."""

    supports_dsoftbus_remote_fence = True

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
        except ModelCallError:
            raise
        except StreamBufferLimitError:
            raise _output_limit_error(self.context) from None
        except TimeoutError as exc:
            if (
                options.deadline_monotonic is not None
                and time.monotonic() >= float(options.deadline_monotonic)
            ):
                raise ModelCallError(
                    message="Remote model deadline exceeded",
                    provider=self.context.provider,
                    model=self.context.model,
                    code="DEADLINE_EXCEEDED",
                ) from None
            raise transport_base.normalize_model_call_error(exc, self.context) from None
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
        request_timeout = float(options.timeout)
        if options.deadline_monotonic is not None:
            remaining = remaining_call_time(float(options.deadline_monotonic))
            if remaining <= 0:
                raise TimeoutError("Remote model deadline exceeded")
            request_timeout = min(request_timeout, remaining)
        base: dict[str, Any] = {
            "model": self.context.model,
            "messages": conversation,
            "max_tokens": traits.max_output_tokens or 8_192,
            "timeout": request_timeout,
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
        deadline = effective_call_deadline(
            options,
            max(NONSTREAM_WATCHDOG_TIMEOUT, options.timeout),
        )
        response = self._watchdog(
            lambda: self.client.messages.create(**kwargs),
            deadline_monotonic=deadline,
            options=options,
            interrupted=interrupted,
        )
        if response is _INTERRUPTED:
            return self._interrupted_result(False)
        if response is None:
            raise ValueError("Anthropic request returned no response")
        return self._result_from_response(response, options, was_streamed=False)

    def _stream(
        self,
        kwargs: dict[str, Any],
        options: ModelCallOptions,
        stream_callback: Callable[[str], None] | None,
        interrupted: Callable[[], bool] | None,
    ) -> ModelCallResult:
        created_at = time.monotonic()
        stream_create_timeout = max(STREAM_CREATE_TIMEOUT, options.timeout)
        stream_safety_timeout = max(STREAM_SAFETY_TIMEOUT, options.timeout)
        stream_stall_timeout = max(STREAM_STALL_TIMEOUT, options.timeout)
        create_deadline = effective_call_deadline(options, stream_create_timeout)
        producer_deadline = effective_call_deadline(
            options,
            stream_create_timeout + stream_safety_timeout,
        )
        events = BoundedNormalizedEventBuffer(
            max_items=options.stream_queue_max_items,
            max_bytes=options.stream_queue_max_bytes,
        )
        started = threading.Event()
        stop_requested = threading.Event()
        stream_holder: list[Any] = [None]
        stream_started_holder: list[float | None] = [None]

        def produce() -> None:
            error: Exception | None = None
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
                    for raw_event in stream:
                        if stop_requested.is_set():
                            break
                        event = _normalized_stream_event(raw_event)
                        if not events.put(
                            event,
                            deadline_monotonic=producer_deadline,
                            stop_requested=stop_requested,
                        ):
                            break
            except Exception as exc:
                error = exc
            finally:
                events.finish(error)
                if options.unregister_worker is not None:
                    options.unregister_worker(threading.current_thread())

        producer = threading.Thread(target=produce, daemon=True)
        if options.register_worker is not None:
            options.register_worker(producer)
        try:
            producer.start()
        except BaseException:
            if options.unregister_worker is not None:
                options.unregister_worker(producer)
            raise
        stream_started_at: float | None = None
        stream_deadline: float | None = None
        last_event_at = created_at
        timed_out = False
        timeout_finish_reason = "stream_stalled"
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

        def enforce_output_bounds() -> None:
            content_text = "".join(content)
            if (
                options.response_utf8_max_bytes is not None
                and len(content_text.encode("utf-8"))
                > options.response_utf8_max_bytes
            ):
                raise StreamBufferLimitError("model response exceeded byte cap")
            if options.stream_accumulator_max_bytes is None:
                return
            aggregate = {
                "content": content_text,
                "thinking": thinking,
                "toolUses": tool_uses,
            }
            size = len(
                json.dumps(
                    json_safe_value(aggregate),
                    allow_nan=False,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            )
            if size > options.stream_accumulator_max_bytes:
                raise StreamBufferLimitError(
                    "model stream accumulator exceeded byte cap"
                )

        try:
            while True:
                if complete and events.empty:
                    break
                if interrupted and interrupted():
                    was_interrupted = True
                    while True:
                        pending = events.get(0.0)
                        if pending is None:
                            break
                        merge_usage(pending.value)
                    break

                now = time.monotonic()
                stream_is_started = started.is_set()
                if stream_is_started and stream_started_at is None:
                    stream_started_at = stream_started_holder[0] or now
                    last_event_at = stream_started_at
                    stream_deadline = effective_call_deadline(
                        options,
                        stream_safety_timeout,
                    )

                if not stream_is_started:
                    if now >= create_deadline:
                        timed_out = True
                        timeout_finish_reason = "stream_create_timeout"
                        break
                    wait_timeout = min(
                        POLL_INTERVAL,
                        remaining_call_time(create_deadline),
                    )
                else:
                    assert stream_deadline is not None
                    if now >= stream_deadline:
                        timed_out = True
                        timeout_finish_reason = "stream_timeout"
                        break
                    wait_timeout = min(
                        POLL_INTERVAL,
                        remaining_call_time(stream_deadline),
                        max(0.0, stream_stall_timeout - (now - last_event_at)),
                    )

                record = events.get(wait_timeout)
                if record is None:
                    if events.finished and events.empty:
                        break
                    now = time.monotonic()
                    if started.is_set() and now - last_event_at >= stream_stall_timeout:
                        timed_out = True
                        timeout_finish_reason = "stream_stalled"
                        break
                    continue

                arrived_at = record.arrived_at
                event = record.value
                if stream_started_at is None:
                    stream_started_at = stream_started_holder[0] or arrived_at
                    last_event_at = stream_started_at
                    stream_deadline = effective_call_deadline(
                        options,
                        stream_safety_timeout,
                    )
                if arrived_at - last_event_at >= stream_stall_timeout:
                    timed_out = True
                    timeout_finish_reason = "stream_stalled"
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
                        block = thinking.setdefault(
                            index,
                            {"type": "thinking", "thinking": "", "signature": ""},
                        )
                        block["thinking"] += str(_field(delta, "thinking", ""))
                    elif delta_type == "signature_delta":
                        block = thinking.setdefault(
                            index,
                            {"type": "thinking", "thinking": "", "signature": ""},
                        )
                        block["signature"] += str(_field(delta, "signature", ""))
                    elif delta_type == "input_json_delta":
                        block = tool_uses.setdefault(
                            index,
                            {"id": "", "name": "", "input": {}, "partial_json": ""},
                        )
                        block["partial_json"] += str(
                            _field(delta, "partial_json", "")
                        )
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
                    finish_reason = (
                        _field(_field(event, "delta"), "stop_reason")
                        or finish_reason
                    )
                    usage.update(_usage_fields(_field(event, "usage")))
                elif event_type == "message_stop":
                    complete = True
                    message = _field(event, "message")
                    if message is not None:
                        finish_reason = _field(message, "stop_reason") or finish_reason
                        usage.update(_usage_fields(_field(message, "usage")))
                enforce_output_bounds()
        finally:
            stop_requested.set()
            _close_quietly(stream_holder[0])

        has_payload = bool(content or thinking or completed_tool_uses or finish_reason)
        producer_error = events.terminal_error
        if isinstance(producer_error, StreamBufferLimitError):
            raise producer_error
        if (
            options.deadline_monotonic is not None
            and time.monotonic() >= float(options.deadline_monotonic)
        ):
            raise TimeoutError("Remote model deadline exceeded")
        if was_interrupted:
            finish_reason = "interrupted"
        elif producer_error is not None and not has_payload:
            raise producer_error
        elif timed_out and not has_payload:
            raise TimeoutError("Anthropic stream stalled before returning payload")
        elif not complete and timed_out:
            finish_reason = timeout_finish_reason
        elif not complete and producer_error is not None:
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
            options=options,
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
            options=options,
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
        options: ModelCallOptions,
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
        result = ModelCallResult(
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
        if (
            options.response_utf8_max_bytes is not None
            and len(result.content.encode("utf-8")) > options.response_utf8_max_bytes
        ):
            raise StreamBufferLimitError("model response exceeded byte cap")
        return result

    @staticmethod
    def _watchdog(
        call: Callable[[], Any],
        *,
        deadline_monotonic: float,
        options: ModelCallOptions,
        interrupted: Callable[[], bool] | None,
    ) -> Any:
        result: list[Any] = [_MISSING]
        cancelled = threading.Event()

        def invoke() -> None:
            try:
                value = call()
                result[0] = value
                if cancelled.is_set():
                    _close_quietly(value)
            except Exception as exc:
                result[0] = exc
            finally:
                if options.unregister_worker is not None:
                    options.unregister_worker(threading.current_thread())

        worker = threading.Thread(target=invoke, daemon=True)
        if options.register_worker is not None:
            options.register_worker(worker)
        try:
            worker.start()
        except BaseException:
            if options.unregister_worker is not None:
                options.unregister_worker(worker)
            raise
        while worker.is_alive():
            if interrupted and interrupted():
                cancelled.set()
                if result[0] is not _MISSING and not isinstance(result[0], Exception):
                    _close_quietly(result[0])
                return _INTERRUPTED
            remaining = remaining_call_time(deadline_monotonic)
            if remaining <= 0:
                break
            worker.join(min(POLL_INTERVAL, remaining))
        if interrupted and interrupted():
            cancelled.set()
            if result[0] is not _MISSING and not isinstance(result[0], Exception):
                _close_quietly(result[0])
            return _INTERRUPTED
        if worker.is_alive() or result[0] is _MISSING:
            cancelled.set()
            raise TimeoutError("Anthropic request exceeded the hard watchdog timeout")
        if isinstance(result[0], Exception):
            raise result[0]
        return result[0]

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
