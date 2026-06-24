# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Small helper for task-scoped auxiliary LLM calls.

Auxiliary tasks such as session_search should be configurable independently,
but "auto" should inherit the active agent credentials and model.
"""

from __future__ import annotations

from typing import Any, Dict, List


def _task_config(task: str, parent_agent: Any = None) -> Dict[str, Any]:
    cfg = getattr(parent_agent, "config", None) if parent_agent is not None else None
    if not cfg:
        try:
            from mclaw.cli.config import load_config
            cfg = load_config()
        except Exception:
            cfg = {}
    aux = cfg.get("auxiliary", {}) if isinstance(cfg, dict) else {}
    task_cfg = aux.get(task, {}) if isinstance(aux, dict) else {}
    return task_cfg if isinstance(task_cfg, dict) else {}


def _resolve_auxiliary_credentials(task: str, parent_agent: Any = None) -> Dict[str, Any]:
    task_cfg = _task_config(task, parent_agent)
    provider = str(task_cfg.get("provider") or "auto")
    model = str(task_cfg.get("model") or "")
    base_url = str(task_cfg.get("base_url") or "")
    timeout = float(task_cfg.get("timeout") or 60)

    parent_provider = str(getattr(parent_agent, "provider", "") or "")
    parent_model = str(getattr(parent_agent, "model", "") or "")
    parent_base_url = str(getattr(parent_agent, "base_url", "") or "")
    parent_api_key = str(getattr(parent_agent, "api_key", "") or "")
    parent_api_mode = str(getattr(parent_agent, "api_mode", "") or "chat_completions")

    if provider in ("", "auto"):
        return {
            "provider": parent_provider,
            "model": model or parent_model,
            "base_url": base_url or parent_base_url,
            "api_key": parent_api_key,
            "api_mode": parent_api_mode,
            "timeout": timeout,
        }

    try:
        from mclaw.cli.auth import PROVIDER_REGISTRY, resolve_api_key, resolve_base_url
        provider_cfg = PROVIDER_REGISTRY.get(provider)
        inherited_model = parent_model if provider == parent_provider else ""
        return {
            "provider": provider,
            "model": model or inherited_model,
            "base_url": base_url or resolve_base_url(provider),
            "api_key": resolve_api_key(provider) or parent_api_key,
            "api_mode": provider_cfg.api_mode if provider_cfg else "chat_completions",
            "timeout": timeout,
        }
    except Exception:
        return {
            "provider": provider,
            "model": model or parent_model,
            "base_url": base_url or parent_base_url,
            "api_key": parent_api_key,
            "api_mode": parent_api_mode,
            "timeout": timeout,
        }


def extract_content_or_reasoning(response: Any) -> str:
    """Extract text from OpenAI-compatible or Anthropic-compatible responses."""
    try:
        choices = getattr(response, "choices", None)
        if choices:
            msg = choices[0].message
            content = getattr(msg, "content", None) or ""
            reasoning = getattr(msg, "reasoning_content", None) or ""
            return (content or reasoning or "").strip()
    except Exception:
        pass

    try:
        blocks = getattr(response, "content", None)
        if isinstance(blocks, str):
            return blocks.strip()
        if isinstance(blocks, list):
            parts: List[str] = []
            for block in blocks:
                text = getattr(block, "text", None)
                if text is None and isinstance(block, dict):
                    text = block.get("text")
                if text:
                    parts.append(str(text))
            return "\n".join(parts).strip()
    except Exception:
        pass

    return ""


def call_auxiliary_llm(
    task: str,
    messages: List[Dict[str, str]],
    parent_agent: Any = None,
    temperature: float = 0.1,
    max_tokens: int = 4000,
) -> str:
    """Call the configured auxiliary model for a named task."""
    creds = _resolve_auxiliary_credentials(task, parent_agent)
    model = creds.get("model") or ""
    if not model:
        raise RuntimeError(f"No model configured for auxiliary task '{task}'")

    api_mode = creds.get("api_mode") or "chat_completions"
    if api_mode == "anthropic_messages":
        try:
            import anthropic
        except ImportError as exc:
            raise RuntimeError("anthropic package is required for Anthropic auxiliary calls") from exc

        system = ""
        anth_messages = []
        for msg in messages:
            if msg.get("role") == "system" and not system:
                system = msg.get("content") or ""
            else:
                anth_messages.append({
                    "role": "assistant" if msg.get("role") == "assistant" else "user",
                    "content": msg.get("content") or "",
                })

        anth_model = model
        if anth_model.lower().startswith("anthropic/"):
            anth_model = anth_model[len("anthropic/"):]
        anth_model = anth_model.replace(".", "-")

        client = anthropic.Anthropic(
            api_key=creds.get("api_key") or anthropic.NOT_GIVEN,
            base_url=creds.get("base_url") or anthropic.NOT_GIVEN,
            max_retries=0,
            timeout=creds.get("timeout") or 60,
        )
        kwargs = {
            "model": anth_model,
            "messages": anth_messages,
            "max_tokens": max_tokens,
        }
        if system:
            kwargs["system"] = system
        return extract_content_or_reasoning(client.messages.create(**kwargs))

    try:
        import openai
    except ImportError as exc:
        raise RuntimeError("openai package is required for auxiliary calls") from exc

    client = openai.OpenAI(
        api_key=creds.get("api_key") or openai.NOT_GIVEN,
        base_url=creds.get("base_url") or openai.NOT_GIVEN,
        max_retries=0,
        timeout=creds.get("timeout") or 60,
    )
    kwargs = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    try:
        return extract_content_or_reasoning(client.chat.completions.create(**kwargs))
    except Exception as exc:
        if exc.__class__.__name__ == "BadRequestError" and "temperature" in str(exc).lower():
            kwargs.pop("temperature", None)
            return extract_content_or_reasoning(client.chat.completions.create(**kwargs))
        raise
