# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Single-attempt OpenAI-compatible Chat Completions transport."""

from __future__ import annotations

import json
import queue
import threading
import time
from collections.abc import Callable, Mapping
from copy import deepcopy
from typing import Any

from mclaw.agent.transports.base import (
    ModelCallError,
    ModelCallOptions,
    ModelCallResult,
    ModelTransport,
    ReasoningTrace,
    json_safe_value,
    normalize_model_call_error,
)
from mclaw.providers.base import (
    PROMPT_CACHE_LAYOUT_OPENAI_SYSTEM,
    PROMPT_CACHE_LAYOUT_OPENROUTER_GEMINI,
    PROMPT_CACHE_LAYOUT_OPENROUTER_SYSTEM,
    PROMPT_CACHE_LAYOUT_QWEN_SYSTEM,
)
from mclaw.providers.runtime import ProviderRuntimeContext


NONSTREAM_TIMEOUT = 90.0
CREATE_TIMEOUT = 90.0
STREAM_SAFETY_TIMEOUT = 300.0
STREAM_STALL_TIMEOUT = 60.0
POLL_INTERVAL = 0.2

_MISSING = object()
_INTERRUPTED = object()
_DONE = object()
_ORDERED_SYSTEM_CACHE_LAYOUTS = frozenset({
    PROMPT_CACHE_LAYOUT_OPENAI_SYSTEM,
    PROMPT_CACHE_LAYOUT_OPENROUTER_SYSTEM,
    PROMPT_CACHE_LAYOUT_OPENROUTER_GEMINI,
    PROMPT_CACHE_LAYOUT_QWEN_SYSTEM,
})


class _ProviderResponseError(RuntimeError):
    """Error envelope returned inside an otherwise successful stream."""


def _field(value: object, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _response_error(value: object) -> Exception | None:
    raw = _field(value, "error", _MISSING)
    if raw is _MISSING or raw is None:
        return None
    message = str(_field(raw, "message") or raw or "Provider returned an error")
    error_type = _field(_field(raw, "metadata"), "error_type")
    if error_type and str(error_type) not in message:
        message = f"{message} ({error_type})"
    error = _ProviderResponseError(message)
    try:
        status_code = int(_field(raw, "code"))
    except (TypeError, ValueError):
        status_code = None
    if status_code is not None and 100 <= status_code <= 599:
        setattr(error, "status_code", status_code)
    return error


def _merge_usage_payload(current: object | None, incoming: object | None) -> object | None:
    """Merge independently reported usage fields; later cumulative values win."""
    if incoming is None:
        return current
    incoming_value = json_safe_value(incoming)
    if not isinstance(incoming_value, Mapping):
        return incoming_value
    current_value = json_safe_value(current)
    merged = dict(current_value) if isinstance(current_value, Mapping) else {}
    for name, value in incoming_value.items():
        if value is None:
            continue
        previous = merged.get(name)
        if isinstance(previous, Mapping) and isinstance(value, Mapping):
            merged[name] = _merge_usage_payload(previous, value)
        else:
            merged[name] = value
    return merged


def _chunk_usage(current: object | None, chunk: object) -> object | None:
    """Collect standard top-level and Kimi-compatible choice-level usage."""
    current = _merge_usage_payload(current, _field(chunk, "usage"))
    for choice in _sequence(_field(chunk, "choices")):
        current = _merge_usage_payload(current, _field(choice, "usage"))
    return current


def _sequence(value: object) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return "".join(_text(item) for item in value)
    if isinstance(value, Mapping) or value is not None:
        for name in ("text", "content", "reasoning_content", "reasoning"):
            candidate = _field(value, name)
            if candidate is not None and candidate is not value:
                if result := _text(candidate):
                    return result
    return ""


def _string(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(json_safe_value(value), ensure_ascii=False, separators=(",", ":"))


def _has_payload(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, (str, list, tuple, Mapping)):
        return bool(value)
    return True


def _close_quietly(value: object) -> None:
    close = getattr(value, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def _merge_text(current: str, incoming: str, mode: str) -> tuple[str, str]:
    if not incoming:
        return current, ""
    if mode == "cumulative":
        delta = incoming[len(current):] if incoming.startswith(current) else incoming
        return incoming, delta
    return current + incoming, incoming


def _clean_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    for raw in deepcopy(messages):
        if not isinstance(raw, dict):
            raise TypeError("OpenAI-compatible messages must be dictionaries")
        message = {
            key: value
            for key, value in raw.items()
            if not str(key).startswith("_") and key != "finish_reason"
        }
        cleaned.append(message)
    return cleaned


def _shape_ordered_system_prefix(
    messages: list[dict[str, Any]],
    *,
    layout: str,
    system_message_index: int | None,
) -> None:
    """Expose a stable system block so profiles can append dynamic context after it."""
    if layout not in _ORDERED_SYSTEM_CACHE_LAYOUTS:
        return
    if (
        type(system_message_index) is not int
        or not 0 <= system_message_index < len(messages)
    ):
        return
    message = messages[system_message_index]
    if message.get("role") != "system":
        return

    content = message.get("content")
    if isinstance(content, list):
        return
    if content:
        message["content"] = [{"type": "text", "text": str(content)}]


def _final_sanitize(kwargs: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(kwargs)
    messages = result.get("messages")
    if not isinstance(messages, list):
        return result
    for message in messages:
        if not isinstance(message, dict):
            continue
        details = message.get("reasoning_details")
        if isinstance(details, Mapping) and "schema_version" in details:
            message.pop("reasoning_details", None)
        for key in tuple(message):
            if str(key).startswith("_") or key == "finish_reason":
                message.pop(key, None)
    return result


def _tool_calls(raw_calls: object) -> list[dict[str, Any]] | None:
    result: list[dict[str, Any]] = []
    for raw in _sequence(raw_calls):
        function = _field(raw, "function")
        result.append({
            "id": str(_field(raw, "id") or ""),
            "type": str(_field(raw, "type") or "function"),
            "function": {
                "name": str(_field(function, "name") or ""),
                "arguments": _string(_field(function, "arguments")),
            },
        })
    return result or None


def _completed_stream_tool_calls(
    tool_map: dict[int, dict[str, Any]],
    provider_finish_reason: str | None,
) -> list[dict[str, Any]] | None:
    if not tool_map or provider_finish_reason not in {"tool_calls", "function_call"}:
        return None
    completed: list[dict[str, Any]] = []
    for index in sorted(tool_map):
        call = tool_map[index]
        function = call.get("function")
        if not call.get("id") or not isinstance(function, dict) or not function.get("name"):
            return None
        arguments = function.get("arguments") or "{}"
        try:
            parsed = json.loads(arguments)
        except (TypeError, json.JSONDecodeError):
            return None
        if not isinstance(parsed, dict):
            return None
        completed.append(call)
    return completed


def _reasoning_trace(
    value: object,
    raw_tool_calls: object,
    context: ProviderRuntimeContext,
) -> ReasoningTrace | None:
    text_parts: list[str] = []
    structured: list[dict[str, Any]] = []
    for name in ("reasoning_content", "reasoning", "reasoning_details"):
        raw = _field(value, name)
        if not _has_payload(raw):
            continue
        if name != "reasoning_details" and isinstance(raw, str):
            if raw and raw not in text_parts:
                text_parts.append(raw)
            continue
        structured.append({"field": name, "value": json_safe_value(raw)})
        if not text_parts and (visible := _text(raw)):
            text_parts.append(visible)
    for index, raw in enumerate(_sequence(raw_tool_calls)):
        extra = _field(raw, "extra_content")
        if _has_payload(extra):
            structured.append({
                "field": "tool_calls.extra_content",
                "index": index,
                "value": json_safe_value(extra),
            })
    text = "".join(text_parts) or None
    if not text and not structured:
        return None
    return ReasoningTrace(
        text=text,
        provider=context.provider,
        model=context.model,
        api_mode=context.api_mode,
        format=_structured_reasoning_format(structured, context),
        payload=structured if structured else text,
    )


def _structured_reasoning_format(
    structured: list[dict[str, Any]],
    context: ProviderRuntimeContext,
) -> str:
    """Identify Gemini tool-call signatures while keeping protocol defaults generic."""
    if not structured:
        return "reasoning_content"
    if context.provider == "google":
        for event in structured:
            if event.get("field") != "tool_calls.extra_content":
                continue
            value = event.get("value")
            google = value.get("google") if isinstance(value, Mapping) else None
            if (
                isinstance(google, Mapping)
                and isinstance(google.get("thought_signature"), str)
                and google["thought_signature"]
            ):
                return "gemini_thought_signature"
    return "reasoning_details"


class OpenAIChatCompletionsTransport(ModelTransport):
    """Bind one provider context to one OpenAI-compatible SDK client."""

    def __init__(self, context: ProviderRuntimeContext, client: Any) -> None:
        super().__init__(context, client)

    def call(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        options: ModelCallOptions,
        stream_callback: Callable[[str], None] | None = None,
        interrupted: Callable[[], bool] | None = None,
    ) -> ModelCallResult:
        try:
            traits = self.context.profile.model_traits(self.context.model)
            effective_stream = options.stream or traits.requires_stream
            cleaned_messages = _clean_messages(messages)
            cache_plan = options.cache_plan
            if cache_plan is not None and cache_plan.enabled:
                _shape_ordered_system_prefix(
                    cleaned_messages,
                    layout=traits.prompt_cache_layout,
                    system_message_index=cache_plan.system_message_index,
                )
            base_kwargs: dict[str, Any] = {
                "model": self.context.model,
                "messages": cleaned_messages,
                "timeout": options.timeout,
            }
            if tools and traits.supports_tools:
                base_kwargs["tools"] = deepcopy(tools)
            kwargs = self.context.profile.prepare_request(
                base_kwargs,
                self.context,
                options,
            )
            kwargs = _final_sanitize(kwargs)
            if effective_stream:
                kwargs["stream"] = True
                if traits.include_stream_usage_option:
                    stream_options = dict(kwargs.get("stream_options") or {})
                    stream_options["include_usage"] = True
                    kwargs["stream_options"] = stream_options
                return self._stream(
                    kwargs,
                    options,
                    content_mode=traits.content_stream_mode,
                    reasoning_mode=traits.reasoning_stream_mode,
                    stream_callback=stream_callback,
                    interrupted=interrupted,
                )
            kwargs.pop("stream", None)
            kwargs.pop("stream_options", None)
            return self._nonstream(kwargs, options, interrupted)
        except ModelCallError:
            raise
        except Exception as exc:
            raise normalize_model_call_error(exc, self.context) from None

    def _nonstream(
        self,
        kwargs: dict[str, Any],
        options: ModelCallOptions,
        interrupted: Callable[[], bool] | None,
    ) -> ModelCallResult:
        response = self._watchdog(
            lambda: self.client.chat.completions.create(**kwargs),
            timeout=max(NONSTREAM_TIMEOUT, options.timeout),
            interrupted=interrupted,
        )
        if response is _INTERRUPTED:
            return self._interrupted_result(was_streamed=False)

        if response_error := _response_error(response):
            raise response_error
        choices = _sequence(_field(response, "choices"))
        if not choices:
            raise ValueError("Provider response did not contain a completion choice")
        choice = choices[0]
        if response_error := _response_error(choice):
            raise response_error
        message = _field(choice, "message")
        if message is None:
            raise ValueError("Provider response choice did not contain a message")
        raw_tools = _field(message, "tool_calls")
        return ModelCallResult(
            content=(
                _text(_field(message, "content"))
                or _text(_field(message, "refusal"))
            ),
            tool_calls=_tool_calls(raw_tools),
            finish_reason=_field(choice, "finish_reason"),
            reasoning=_reasoning_trace(message, raw_tools, self.context),
            usage=self.context.profile.parse_usage(
                _chunk_usage(None, response),
                self.context,
                source=options.source,
            ),
            was_streamed=False,
            provider=self.context.provider,
            model=self.context.model,
        )

    def _stream(
        self,
        kwargs: dict[str, Any],
        options: ModelCallOptions,
        *,
        content_mode: str,
        reasoning_mode: str,
        stream_callback: Callable[[str], None] | None,
        interrupted: Callable[[], bool] | None,
    ) -> ModelCallResult:
        stream = self._watchdog(
            lambda: self.client.chat.completions.create(**kwargs),
            timeout=max(CREATE_TIMEOUT, options.timeout),
            interrupted=interrupted,
        )
        if stream is _INTERRUPTED:
            return self._interrupted_result(was_streamed=True)

        chunks: queue.Queue[Any] = queue.Queue()
        producer_error: list[Exception | None] = [None]

        def produce() -> None:
            try:
                for chunk in stream:
                    chunks.put(chunk)
                    if self._is_interrupted(interrupted):
                        break
            except Exception as exc:
                raw_body = getattr(exc, "body", None)
                if raw_body is None:
                    producer_error[0] = exc
                else:
                    envelope = (
                        raw_body
                        if isinstance(raw_body, Mapping) and "error" in raw_body
                        else {"error": raw_body}
                    )
                    producer_error[0] = _response_error(envelope) or exc
            finally:
                chunks.put(_DONE)

        threading.Thread(target=produce, daemon=True).start()
        content = ""
        reasoning_text = ""
        tool_map: dict[int, dict[str, Any]] = {}
        structured: list[dict[str, Any]] = []
        structured_positions: dict[tuple[str, int | None], int] = {}
        finish_reason: str | None = None
        provider_finish_reason: str | None = None
        raw_usage: object | None = None
        interrupted_result = False
        stalled = False
        safety_timeout = False
        stream_safety_timeout = max(STREAM_SAFETY_TIMEOUT, options.timeout)
        stream_stall_timeout = max(STREAM_STALL_TIMEOUT, options.timeout)
        started = last_meaningful = time.monotonic()

        def add_structured(field: str, raw: Any, index: int | None = None) -> None:
            event = {"field": field, "value": json_safe_value(raw)}
            if index is not None:
                event["index"] = index
            key = (field, index)
            if reasoning_mode == "cumulative" and key in structured_positions:
                structured[structured_positions[key]] = event
            else:
                structured_positions[key] = len(structured)
                structured.append(event)

        try:
            while True:
                if self._is_interrupted(interrupted):
                    interrupted_result = True
                    while True:
                        try:
                            pending = chunks.get_nowait()
                        except queue.Empty:
                            break
                        if pending is _DONE:
                            break
                        raw_usage = _chunk_usage(raw_usage, pending)
                    break
                now = time.monotonic()
                if now - started >= stream_safety_timeout:
                    safety_timeout = True
                    break
                if now - last_meaningful >= stream_stall_timeout:
                    stalled = True
                    break
                try:
                    chunk = chunks.get(timeout=POLL_INTERVAL)
                except queue.Empty:
                    continue
                if chunk is _DONE:
                    break

                if response_error := _response_error(chunk):
                    raise response_error
                raw_usage = _chunk_usage(raw_usage, chunk)
                choices = _sequence(_field(chunk, "choices"))
                if not choices:
                    continue
                choice = choices[0]
                if response_error := _response_error(choice):
                    raise response_error
                provider_finish = _field(choice, "finish_reason")
                if provider_finish:
                    provider_finish_reason = finish_reason = str(provider_finish)
                delta = _field(choice, "delta")
                meaningful = bool(provider_finish)
                if delta is None:
                    if meaningful:
                        last_meaningful = time.monotonic()
                    continue

                incoming_content = (
                    _text(_field(delta, "content"))
                    or _text(_field(delta, "refusal"))
                )
                content, visible_delta = _merge_text(content, incoming_content, content_mode)
                if visible_delta:
                    meaningful = True
                    if stream_callback is not None:
                        stream_callback(visible_delta)

                direct_reasoning: list[str] = []
                for name in ("reasoning_content", "reasoning"):
                    raw = _field(delta, name)
                    if isinstance(raw, str):
                        if raw and raw not in direct_reasoning:
                            direct_reasoning.append(raw)
                    elif _has_payload(raw):
                        add_structured(name, raw)
                details = _field(delta, "reasoning_details")
                details_text = ""
                if _has_payload(details):
                    add_structured("reasoning_details", details)
                    details_text = _text(details)
                incoming_reasoning = "".join(direct_reasoning) or details_text
                reasoning_text, reasoning_delta = _merge_text(
                    reasoning_text,
                    incoming_reasoning,
                    reasoning_mode,
                )
                meaningful = meaningful or bool(reasoning_delta) or _has_payload(details)

                for position, raw_tool in enumerate(_sequence(_field(delta, "tool_calls"))):
                    raw_index = _field(raw_tool, "index", position)
                    index = raw_index if type(raw_index) is int else position
                    entry = tool_map.setdefault(index, {
                        "id": "",
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                    })
                    if tool_id := _field(raw_tool, "id"):
                        entry["id"] = str(tool_id)
                        meaningful = True
                    if tool_type := _field(raw_tool, "type"):
                        entry["type"] = str(tool_type)
                    function = _field(raw_tool, "function")
                    name = str(_field(function, "name") or "")
                    arguments = _string(_field(function, "arguments"))
                    if name:
                        entry["function"]["name"] += name
                        meaningful = True
                    if arguments:
                        entry["function"]["arguments"] += arguments
                        meaningful = True
                    extra = _field(raw_tool, "extra_content")
                    if _has_payload(extra):
                        add_structured("tool_calls.extra_content", extra, index)
                        meaningful = True
                if meaningful:
                    last_meaningful = time.monotonic()
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass

        completed_tool_calls = _completed_stream_tool_calls(tool_map, provider_finish_reason)
        if tool_map and completed_tool_calls is None:
            structured = [
                event
                for event in structured
                if event.get("field") != "tool_calls.extra_content"
            ]
        has_payload = bool(
            content
            or reasoning_text
            or structured
            or completed_tool_calls
            or provider_finish_reason
        )
        if (
            producer_error[0] is not None
            and not interrupted_result
            and (
                not has_payload
                or isinstance(producer_error[0], _ProviderResponseError)
            )
        ):
            raise producer_error[0]
        if (stalled or safety_timeout) and not has_payload and not interrupted_result:
            raise TimeoutError("Provider stream stalled before returning a payload")
        if not has_payload and not interrupted_result:
            raise ValueError("Provider stream ended before returning a payload")
        if interrupted_result:
            finish_reason = "interrupted"
        elif stalled:
            finish_reason = "stream_stalled"
        elif safety_timeout:
            finish_reason = "stream_timeout"
        elif producer_error[0] is not None:
            finish_reason = "stream_error"
        elif not provider_finish_reason:
            finish_reason = "stream_incomplete"

        reasoning = None
        if reasoning_text or structured:
            reasoning = ReasoningTrace(
                text=reasoning_text or None,
                provider=self.context.provider,
                model=self.context.model,
                api_mode=self.context.api_mode,
                format=_structured_reasoning_format(structured, self.context),
                payload=structured if structured else reasoning_text,
            )
        return ModelCallResult(
            content=content,
            tool_calls=completed_tool_calls,
            finish_reason=finish_reason,
            reasoning=reasoning,
            usage=self.context.profile.parse_usage(
                raw_usage,
                self.context,
                source=options.source,
            ),
            was_streamed=True,
            provider=self.context.provider,
            model=self.context.model,
            interrupted=interrupted_result,
        )

    @staticmethod
    def _watchdog(
        call: Callable[[], Any],
        *,
        timeout: float,
        interrupted: Callable[[], bool] | None,
    ) -> Any:
        result: list[Any] = [_MISSING]
        cancelled = threading.Event()

        def run() -> None:
            try:
                value = call()
                result[0] = value
                if cancelled.is_set():
                    _close_quietly(value)
            except Exception as exc:
                result[0] = exc

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        deadline = time.monotonic() + timeout
        while thread.is_alive():
            if OpenAIChatCompletionsTransport._is_interrupted(interrupted):
                cancelled.set()
                if result[0] is not _MISSING and not isinstance(result[0], Exception):
                    _close_quietly(result[0])
                return _INTERRUPTED
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(timeout=min(POLL_INTERVAL, remaining))
        if OpenAIChatCompletionsTransport._is_interrupted(interrupted):
            cancelled.set()
            if result[0] is not _MISSING and not isinstance(result[0], Exception):
                _close_quietly(result[0])
            return _INTERRUPTED
        if thread.is_alive() or result[0] is _MISSING:
            cancelled.set()
            raise TimeoutError("Provider call timed out")
        if isinstance(result[0], Exception):
            raise result[0]
        return result[0]

    @staticmethod
    def _is_interrupted(callback: Callable[[], bool] | None) -> bool:
        return bool(callback is not None and callback())

    def _interrupted_result(self, *, was_streamed: bool) -> ModelCallResult:
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
