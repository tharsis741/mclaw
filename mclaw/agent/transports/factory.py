# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Construct one SDK client and protocol transport for a resolved runtime."""

from __future__ import annotations

from mclaw.agent.transports.base import ModelTransport
from mclaw.providers.runtime import ProviderRuntimeContext

_DEFAULT_TRANSPORT_TIMEOUT = 60.0
_LOCAL_API_KEY_PLACEHOLDER = "local-no-key"


def _client_api_key(context: ProviderRuntimeContext) -> str:
    if context.api_key:
        return context.api_key
    if context.profile.credential_required:
        raise ValueError(f"Provider '{context.provider}' requires an API credential")
    return _LOCAL_API_KEY_PLACEHOLDER


def create_transport(context: ProviderRuntimeContext) -> ModelTransport:
    """Bind the resolved context, zero-retry SDK client, and protocol adapter."""
    api_key = _client_api_key(context)
    if context.api_mode == "chat_completions":
        if context.profile.auth_scheme != "bearer":
            raise ValueError(
                f"Provider '{context.provider}' has incompatible auth scheme "
                f"'{context.profile.auth_scheme}' for chat_completions"
            )
        import openai

        from mclaw.agent.transports.openai_chat_completions import (
            OpenAIChatCompletionsTransport,
        )

        client = openai.OpenAI(
            api_key=api_key,
            base_url=context.base_url,
            max_retries=0,
            timeout=_DEFAULT_TRANSPORT_TIMEOUT,
        )
        return OpenAIChatCompletionsTransport(context, client)

    if context.api_mode == "anthropic_messages":
        if context.profile.auth_scheme != "anthropic_x_api_key":
            raise ValueError(
                f"Provider '{context.provider}' has incompatible auth scheme "
                f"'{context.profile.auth_scheme}' for anthropic_messages"
            )
        import anthropic

        from mclaw.agent.transports.anthropic_messages import AnthropicMessagesTransport

        client = anthropic.Anthropic(
            api_key=api_key,
            base_url=context.base_url,
            max_retries=0,
            timeout=_DEFAULT_TRANSPORT_TIMEOUT,
        )
        return AnthropicMessagesTransport(context, client)

    raise ValueError(
        f"Provider '{context.provider}' has unsupported api_mode '{context.api_mode}'"
    )
