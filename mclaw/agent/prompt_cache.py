# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Provider-neutral prompt cache intent contract."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from mclaw.providers.runtime import ProviderRuntimeContext


@dataclass(frozen=True)
class PromptCachePlan:
    enabled: bool = False
    system_message_index: int | None = None
    prefix_hash: str = ""
    conversation_key: str = ""


def _sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_prompt_cache_plan(
    *,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    context: ProviderRuntimeContext,
    session_id: str,
    enabled: bool,
) -> PromptCachePlan:
    """Describe the stable request prefix without including per-turn context."""
    if not isinstance(context, ProviderRuntimeContext):
        raise TypeError("context must be a ProviderRuntimeContext")

    system_message_index = next(
        (index for index, message in enumerate(messages) if message.get("role") == "system"),
        None,
    )
    system_message = (
        messages[system_message_index]
        if system_message_index is not None
        else None
    )
    return PromptCachePlan(
        enabled=bool(enabled),
        system_message_index=system_message_index,
        prefix_hash=_sha256({"system_message": system_message, "tools": tools}),
        conversation_key=_sha256(
            {
                "provider": context.provider,
                "api_mode": context.api_mode,
                "model": context.profile.normalize_model(context.model),
                "base_url": context.safe_base_url,
                "session_id": str(session_id or ""),
            }
        ),
    )
