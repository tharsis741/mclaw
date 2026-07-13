# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Small helper for task-scoped auxiliary LLM calls.

Auxiliary tasks such as session_search should be configurable independently,
but "auto" should inherit the active agent credentials and model.
"""

from __future__ import annotations

from typing import Any


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


def call_auxiliary_llm(
    task: str,
    messages: list[dict[str, str]],
    parent_agent: Any = None,
    temperature: float = 0.1,
    max_tokens: int = 4000,
) -> str:
    """Call the configured auxiliary model for a named task."""
    from mclaw.agent.transports.base import ModelCallOptions
    from mclaw.agent.transports.factory import create_transport

    context, timeout = _resolve_auxiliary_runtime(task, parent_agent)
    result = create_transport(context).call(
        messages=messages,
        tools=[],
        options=ModelCallOptions(
            timeout=timeout,
            max_output_tokens=max_tokens,
            temperature=temperature,
            source="auxiliary",
            cache_plan=None,
        ),
    )
    if result.usage is not None and parent_agent is not None:
        parent_agent._record_usage(result.usage)
    return extract_content_or_reasoning(result)
