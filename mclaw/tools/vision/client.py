# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Provider client interface for vision analysis."""

from __future__ import annotations

from dataclasses import replace
from typing import Protocol

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


class ProviderVisionClient:
    """Use shared request shaping, auth, deadlines and protocols for every provider."""
    provider = "registry"

    async def analyze(self, messages: list, credentials: VisionCredentials, timeout: float) -> str:
        from mclaw.agent.auxiliary_client import _call_auxiliary_model
        from mclaw.agent.transports.base import ModelCallOptions
        from mclaw.providers.resolver import resolve_provider_runtime_context

        runtime = credentials.runtime or resolve_provider_runtime_context(
            provider=credentials.provider, model=credentials.model,
            api_key=credentials.api_key, base_url=credentials.base_url,
        )
        runtime = replace(runtime, reasoning_config={"enabled": False})
        content, usage = await _call_auxiliary_model(
            runtime, messages, ModelCallOptions(timeout=timeout, max_output_tokens=2000, source="vision"),
        )
        return VisionAnalysis(content.strip(), usage)


class VisionAnalysis(str):
    """String-compatible result carrying provider-reported usage to the calling thread."""

    def __new__(cls, content: str, usage=None):
        instance = super().__new__(cls, content)
        instance.usage = usage
        return instance


_provider_client = ProviderVisionClient()


def get_vision_client(provider: str) -> VisionClient:
    """Resolve a configured provider name to a concrete vision client."""
    return _provider_client


async def call_vision_llm(
    messages: list,
    model: str,
    api_key: str,
    base_url: str,
    timeout: float,
    provider: str = "qwen",
    runtime=None,
) -> str:
    """Compatibility wrapper used by the tool-facing vision entrypoint."""
    credentials = VisionCredentials(
        provider=provider or "qwen",
        api_key=api_key,
        base_url=base_url,
        model=model,
        runtime=runtime,
    )
    return await get_vision_client(credentials.provider).analyze(
        messages,
        credentials,
        timeout,
    )
