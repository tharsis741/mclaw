# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Provider client interface for vision analysis."""

from __future__ import annotations

import asyncio
from typing import Any, Protocol

import openai

from mclaw.tools.vision.types import VisionCredentials


class VisionClient(Protocol):
    """Provider adapter contract for multimodal image analysis."""
    provider: str

    async def analyze(
        self,
        messages: list,
        credentials: VisionCredentials,
        timeout: float,
    ) -> str:
        """Return model text for a prepared multimodal message payload."""
        ...


class UnsupportedVisionProviderError(RuntimeError):
    """Raised when configuration names a provider without an adapter."""
    pass


class QwenVisionClient:
    """OpenAI-compatible client for Qwen/DashScope vision models."""
    provider = "qwen"

    async def analyze(
        self,
        messages: list,
        credentials: VisionCredentials,
        timeout: float,
    ) -> str:
        """Call the Qwen vision model and fall back when temperature is rejected."""
        client = openai.AsyncOpenAI(
            api_key=credentials.api_key or openai.NOT_GIVEN,
            base_url=credentials.base_url or openai.NOT_GIVEN,
            max_retries=0,
            timeout=timeout,
        )
        kwargs: dict[str, Any] = {
            "model": credentials.model,
            "messages": messages,
            "max_tokens": 2000,
            "temperature": 0.1,
        }
        if credentials.model.strip().lower().startswith("qwen3"):
            kwargs["extra_body"] = {"enable_thinking": False}

        async with client:
            async with asyncio.timeout(max(0.001, timeout)):
                try:
                    response = await client.chat.completions.create(**kwargs)
                except openai.BadRequestError as exc:
                    err_msg = str(exc).lower()
                    if "temperature" not in err_msg:
                        raise
                    kwargs.pop("temperature", None)
                    response = await client.chat.completions.create(**kwargs)

        if not response.choices:
            return ""

        msg = response.choices[0].message
        content = msg.content or ""
        reasoning = getattr(msg, "reasoning_content", None) or ""
        return content or reasoning or ""


_qwen_client = QwenVisionClient()
_CLIENTS: dict[str, VisionClient] = {
    "qwen": _qwen_client,
    "qwen-intl": _qwen_client,
    "dashscope": _qwen_client,
}


def get_vision_client(provider: str) -> VisionClient:
    """Resolve a configured provider name to a concrete vision client."""
    provider_key = str(provider or "").strip().lower()
    client = _CLIENTS.get(provider_key)
    if client is None:
        raise UnsupportedVisionProviderError(f"vision provider '{provider_key}' is not supported")
    return client


async def call_vision_llm(
    messages: list,
    model: str,
    api_key: str,
    base_url: str,
    timeout: float,
    provider: str = "qwen",
) -> str:
    """Compatibility wrapper used by the tool-facing vision entrypoint."""
    credentials = VisionCredentials(
        provider=provider or "qwen",
        api_key=api_key,
        base_url=base_url,
        model=model,
    )
    return await get_vision_client(credentials.provider).analyze(
        messages,
        credentials,
        timeout,
    )
