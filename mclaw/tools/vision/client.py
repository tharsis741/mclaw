# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Provider client interface for vision analysis."""

from __future__ import annotations

from typing import Any, Protocol

from mclaw.tools.vision.types import VisionCredentials


class VisionClient(Protocol):
    provider: str

    def analyze(self, messages: list, credentials: VisionCredentials, timeout: float) -> str:
        ...


class UnsupportedVisionProviderError(RuntimeError):
    pass


class QwenVisionClient:
    provider = "qwen"

    def analyze(self, messages: list, credentials: VisionCredentials, timeout: float) -> str:
        try:
            import openai
        except ImportError as exc:
            raise RuntimeError("openai package is required for vision analysis") from exc

        client = openai.OpenAI(
            api_key=credentials.api_key or openai.NOT_GIVEN,
            base_url=credentials.base_url or openai.NOT_GIVEN,
            max_retries=1,
            timeout=timeout,
        )
        kwargs: dict[str, Any] = {
            "model": credentials.model,
            "messages": messages,
            "max_tokens": 2000,
            "temperature": 0.1,
        }

        try:
            response = client.chat.completions.create(**kwargs)
        except openai.BadRequestError as exc:
            err_msg = str(exc).lower()
            if "temperature" not in err_msg:
                raise
            kwargs.pop("temperature", None)
            response = client.chat.completions.create(**kwargs)

        if not response.choices:
            return ""

        msg = response.choices[0].message
        content = msg.content or ""
        reasoning = getattr(msg, "reasoning_content", None) or ""
        return content or reasoning or ""


_qwen_client = QwenVisionClient()
_CLIENTS: dict[str, VisionClient] = {
    "qwen": _qwen_client,
    "dashscope": _qwen_client,
}


def get_vision_client(provider: str) -> VisionClient:
    provider_key = str(provider or "").strip().lower()
    client = _CLIENTS.get(provider_key)
    if client is None:
        raise UnsupportedVisionProviderError(f"vision provider '{provider_key}' is not supported")
    return client


def call_vision_llm(
    messages: list,
    model: str,
    api_key: str,
    base_url: str,
    timeout: float,
    provider: str = "qwen",
) -> str:
    credentials = VisionCredentials(
        provider=provider or "qwen",
        api_key=api_key,
        base_url=base_url,
        model=model,
    )
    return get_vision_client(credentials.provider).analyze(messages, credentials, timeout)
