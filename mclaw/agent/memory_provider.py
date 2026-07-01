# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Abstract interfaces for M-Claw memory providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class MemoryProvider(ABC):
    """Contract for memory backends that can add prompt context and tools."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Stable provider name."""

    @abstractmethod
    def is_available(self) -> bool:
        """Return whether the provider can be used."""

    @abstractmethod
    def initialize(self, session_id: str = "", **kwargs) -> None:
        """Prepare the provider for use."""

    def system_prompt_block(self) -> str:
        """Static memory guidance for the system prompt."""
        return ""

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Return recalled context for the current user query."""
        return ""

    @abstractmethod
    def get_tool_schemas(self) -> list[dict[str, Any]]:
        """Return tool schemas owned by this provider."""

    def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs) -> str:
        """Handle a memory tool call owned by this provider."""
        raise NotImplementedError(f"Provider {self.name} does not handle tool {tool_name}")
