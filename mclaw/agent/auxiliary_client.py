# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Small helper for task-scoped auxiliary LLM calls.

Auxiliary tasks such as session_search should be configurable independently,
but "auto" should inherit the active agent credentials and model.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx


# Building a fresh default TLS context takes about a second on Windows and is
# synchronous. Build it at module load so the measured model-call deadline is
# fully spent inside the cancellable Task.
_TLS_CONTEXT = httpx.create_ssl_context()


def _runtime_config(parent_agent: Any = None) -> dict[str, Any]:
    cfg = getattr(parent_agent, "config", None) if parent_agent is not None else None
    if not cfg:
        from mclaw.cli.config import load_config

        cfg = load_config(strict=True)
    return cfg if isinstance(cfg, dict) else {}


def _task_config(task: str, parent_agent: Any = None) -> dict[str, Any]:
    """Read task-specific auxiliary config from the live agent or disk config."""
    cfg = _runtime_config(parent_agent)
    aux = cfg.get("auxiliary", {}) if isinstance(cfg, dict) else {}
    task_cfg = aux.get(task, {}) if isinstance(aux, dict) else {}
    return task_cfg if isinstance(task_cfg, dict) else {}


def _coerce_timeout(value: Any, default: float = 60.0) -> float:
    """Clamp auxiliary task timeouts to a bounded positive value."""
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        timeout = default
    if timeout <= 0:
        return default
    return min(timeout, 600.0)


def _resolve_auxiliary_runtime(task: str, parent_agent: Any = None):
    """Resolve one independent or parent-inherited auxiliary runtime."""
    from mclaw.providers.resolver import (
        default_model_for_provider,
        resolve_provider_runtime_context,
        restore_provider_runtime_context,
    )

    task_cfg = _task_config(task, parent_agent)
    provider = str(task_cfg.get("provider") or "auto").strip()
    model = str(task_cfg.get("model") or "").strip()
    base_url = str(task_cfg.get("base_url") or "").strip()
    timeout = _coerce_timeout(task_cfg.get("timeout"), default=60.0)
    config = _runtime_config(parent_agent)
    parent_runtime = getattr(parent_agent, "provider_runtime", None)

    if provider in ("", "auto"):
        if base_url:
            if parent_runtime is None:
                raise RuntimeError("An auxiliary base_url with provider=auto requires a parent runtime")
            custom_provider = (
                "custom_anthropic"
                if parent_runtime.api_mode == "anthropic_messages"
                else "custom"
            )
            context = resolve_provider_runtime_context(
                provider=custom_provider,
                model=model or parent_runtime.model,
                base_url=base_url,
                api_key=parent_runtime.api_key,
                config=config,
            )
        elif parent_runtime is not None:
            context = (
                restore_provider_runtime_context(
                    parent_runtime.snapshot(),
                    model=model,
                    api_key=parent_runtime.api_key,
                    config=config,
                )
                if model and model != parent_runtime.model
                else parent_runtime
            )
        else:
            context = resolve_provider_runtime_context(model=model, config=config)
        return context, timeout

    target_model = model or default_model_for_provider(provider, config=config)
    context = resolve_provider_runtime_context(
        provider=provider,
        model=target_model,
        base_url=base_url,
        config=config,
    )
    return context, timeout


def extract_content_or_reasoning(result: Any) -> str:
    """Extract display text from one normalized model-call result."""
    content = str(getattr(result, "content", "") or "")
    reasoning = getattr(result, "reasoning", None)
    return (content or (reasoning.text if reasoning and reasoning.text else "")).strip()


def _auxiliary_api_key(context: Any) -> str:
    if context.api_key:
        return context.api_key
    if context.profile.credential_required:
        raise ValueError(f"Provider '{context.provider}' requires an API credential")
    return "local-no-key"


async def _call_openai_auxiliary(
    context: Any,
    messages: list[dict[str, str]],
    options: Any,
) -> tuple[str, Any]:
    """Execute one cancellable OpenAI-compatible auxiliary request."""
    if context.profile.auth_scheme != "bearer":
        raise ValueError(
            f"Provider '{context.provider}' has incompatible auth scheme "
            f"'{context.profile.auth_scheme}' for chat_completions"
        )

    import openai

    from mclaw.agent.transports.openai_chat_completions import (
        _chunk_usage,
        _clean_messages,
        _field,
        _final_sanitize,
        _merge_text,
        _response_error,
        _sequence,
        _text,
    )

    traits = context.profile.model_traits(context.model)
    kwargs = context.profile.prepare_request(
        {
            "model": context.model,
            "messages": _clean_messages(messages),
            "timeout": options.timeout,
        },
        context,
        options,
    )
    kwargs = _final_sanitize(kwargs)
    http_client = httpx.AsyncClient(timeout=None, verify=_TLS_CONTEXT)
    client = openai.AsyncOpenAI(
        api_key=_auxiliary_api_key(context),
        base_url=context.base_url,
        max_retries=0,
        http_client=http_client,
    )
    async with client:
        if not traits.requires_stream:
            kwargs.pop("stream", None)
            kwargs.pop("stream_options", None)
            response = await client.chat.completions.create(**kwargs)
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
            content = _text(_field(message, "content")) or _text(_field(message, "refusal"))
            reasoning = "".join(
                _text(_field(message, name))
                for name in ("reasoning_content", "reasoning", "reasoning_details")
            )
            usage = context.profile.parse_usage(
                _chunk_usage(None, response),
                context,
                source=options.source,
            )
            return (content or reasoning).strip(), usage

        kwargs["stream"] = True
        if traits.include_stream_usage_option:
            stream_options = dict(kwargs.get("stream_options") or {})
            stream_options["include_usage"] = True
            kwargs["stream_options"] = stream_options
        content = ""
        reasoning = ""
        raw_usage: object | None = None
        stream = await client.chat.completions.create(**kwargs)
        async with stream:
            async for chunk in stream:
                if response_error := _response_error(chunk):
                    raise response_error
                raw_usage = _chunk_usage(raw_usage, chunk)
                for choice in _sequence(_field(chunk, "choices")):
                    delta = _field(choice, "delta") or _field(choice, "message")
                    content, _ = _merge_text(
                        content,
                        _text(_field(delta, "content")),
                        traits.content_stream_mode,
                    )
                    for name in ("reasoning_content", "reasoning", "reasoning_details"):
                        reasoning, _ = _merge_text(
                            reasoning,
                            _text(_field(delta, name)),
                            traits.reasoning_stream_mode,
                        )
        usage = context.profile.parse_usage(
            raw_usage,
            context,
            source=options.source,
        )
        return (content or reasoning).strip(), usage


async def _call_anthropic_auxiliary(
    context: Any,
    messages: list[dict[str, str]],
    options: Any,
) -> tuple[str, Any]:
    """Execute one cancellable Anthropic Messages auxiliary request."""
    if context.profile.auth_scheme != "anthropic_x_api_key":
        raise ValueError(
            f"Provider '{context.provider}' has incompatible auth scheme "
            f"'{context.profile.auth_scheme}' for anthropic_messages"
        )

    import anthropic

    from mclaw.agent.transports.anthropic_messages import AnthropicMessagesTransport

    http_client = httpx.AsyncClient(timeout=None, verify=_TLS_CONTEXT)
    client = anthropic.AsyncAnthropic(
        api_key=_auxiliary_api_key(context),
        base_url=context.base_url,
        max_retries=0,
        http_client=http_client,
    )
    transport = AnthropicMessagesTransport(context, client)
    kwargs = transport._request_kwargs(messages, [], options)
    async with client:
        response = await client.messages.create(**kwargs)
    result = transport._result_from_response(response, options, was_streamed=False)
    return extract_content_or_reasoning(result), result.usage


async def _call_auxiliary_model(
    context: Any,
    messages: list[dict[str, str]],
    options: Any,
) -> tuple[str, Any]:
    """Run exactly one provider attempt under an absolute wall-clock deadline."""
    async with asyncio.timeout(options.timeout):
        if context.api_mode == "chat_completions":
            return await _call_openai_auxiliary(context, messages, options)
        if context.api_mode == "anthropic_messages":
            return await _call_anthropic_auxiliary(context, messages, options)
        raise ValueError(
            f"Provider '{context.provider}' has unsupported api_mode '{context.api_mode}'"
        )


def call_auxiliary_llm(
    task: str,
    messages: list[dict[str, str]],
    parent_agent: Any = None,
    temperature: float = 0.1,
    max_tokens: int = 4000,
) -> str:
    """Call the configured auxiliary model for a named task."""
    from mclaw.agent.transports.base import ModelCallOptions
    from mclaw.tools.dispatch import _run_async

    context, timeout = _resolve_auxiliary_runtime(task, parent_agent)
    options = ModelCallOptions(
        timeout=timeout,
        max_output_tokens=max_tokens,
        temperature=temperature,
        source="auxiliary",
        cache_plan=None,
    )
    content, usage = _run_async(
        _call_auxiliary_model(context, messages, options),
        parent_agent=parent_agent,
        diagnostic_name=f"auxiliary_{task}",
        # The coroutine owns the exact request deadline. This extra second is
        # only for AsyncClient cancellation/close to reach a terminal state.
        timeout_seconds=timeout + 1.0,
        raise_on_stop=True,
    )
    if usage is not None and parent_agent is not None:
        parent_agent._record_usage(usage)
    return content
