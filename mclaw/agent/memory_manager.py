# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Coordinate memory providers and safe memory context rendering.

The manager keeps provider registration separate from prompt assembly, so
memory sources can expose tools, build system context, and refresh snapshots
without leaking provider-specific details into the agent loop.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from mclaw.agent.memory_provider import MemoryProvider
from mclaw.tools.registry import tool_error

logger = logging.getLogger(__name__)


def sanitize_context(text: str) -> str:
    from mclaw.tools.memory_tool import MemoryStore

    return MemoryStore.sanitize_context(text or "")


def build_memory_context_block(raw_context: str) -> str:
    from mclaw.tools.memory_tool import MemoryStore

    return MemoryStore.build_memory_context_block(raw_context or "")


class StreamingContextScrubber:
    """Small stateful scrubber for streamed memory-context fragments."""

    def __init__(self) -> None:
        self._tail = ""

    def feed(self, chunk: str) -> str:
        text = self._tail + (chunk or "")
        clean = sanitize_context(text)
        self._tail = text[-32:]
        if self._tail and self._tail in clean:
            return clean[: -len(self._tail)]
        return clean

    def flush(self) -> str:
        tail = sanitize_context(self._tail)
        self._tail = ""
        return tail


class MemoryManager:
    """Orchestrates active memory providers for prompt context and tool routing."""

    def __init__(self) -> None:
        self._providers: List[MemoryProvider] = []
        self._tool_to_provider: Dict[str, MemoryProvider] = {}
        self._has_external = False

    def add_provider(self, provider: MemoryProvider) -> None:
        is_builtin = provider.name == "builtin"
        if not is_builtin and self._has_external:
            logger.warning(
                "Rejected memory provider '%s' because another external provider is already registered",
                provider.name,
            )
            return
        if not is_builtin:
            self._has_external = True

        self._providers.append(provider)
        for schema in provider.get_tool_schemas():
            tool_name = schema.get("function", {}).get("name", "")
            if tool_name and tool_name not in self._tool_to_provider:
                self._tool_to_provider[tool_name] = provider

    @property
    def providers(self) -> List[MemoryProvider]:
        return list(self._providers)

    def initialize(self, session_id: str = "", **kwargs) -> None:
        for provider in self._providers:
            if provider.is_available():
                provider.initialize(session_id=session_id, **kwargs)

    def build_system_prompt(self) -> str:
        parts = []
        for provider in self._providers:
            try:
                block = provider.system_prompt_block()
                if block and block.strip():
                    parts.append(block)
            except Exception as e:
                logger.warning("Memory provider '%s' system_prompt_block failed: %s", provider.name, e)
        return "\n\n".join(parts)

    def prefetch_all(self, query: str, *, session_id: str = "") -> str:
        parts = []
        for provider in self._providers:
            try:
                block = provider.prefetch(query, session_id=session_id)
                if block and block.strip():
                    parts.append(block)
            except Exception as e:
                logger.debug("Memory provider '%s' prefetch failed: %s", provider.name, e)
        return "\n\n".join(parts)

    def get_all_tool_schemas(self):
        result = []
        seen = set()
        for provider in self._providers:
            for schema in provider.get_tool_schemas():
                name = schema.get("function", {}).get("name", "")
                if name and name not in seen:
                    result.append(schema)
                    seen.add(name)
        return result

    def get_all_tool_names(self) -> set:
        return set(self._tool_to_provider.keys())

    def has_tool(self, tool_name: str) -> bool:
        return tool_name in self._tool_to_provider

    def handle_tool_call(self, tool_name: str, args: dict, **kwargs) -> str:
        provider = self._tool_to_provider.get(tool_name)
        if provider is None:
            return tool_error(f"No memory provider handles tool '{tool_name}'")
        try:
            result = provider.handle_tool_call(tool_name, args, **kwargs)
            try:
                from mclaw.tools.memory_tool import MEMORY_WRITE_TOOL_NAMES
                import json

                data = json.loads(result)
                if tool_name in MEMORY_WRITE_TOOL_NAMES and data.get("success"):
                    self.on_memory_write()
            except Exception:
                pass
            return result
        except Exception as e:
            logger.error("Memory tool call failed for %s via %s: %s", tool_name, provider.name, e)
            return tool_error(f"Memory provider '{provider.name}' failed: {type(e).__name__}: {e}")

    def on_memory_write(self) -> None:
        for provider in self._providers:
            hook = getattr(provider, "on_memory_write", None)
            if callable(hook):
                try:
                    hook()
                except Exception as e:
                    logger.debug("Memory provider '%s' on_memory_write failed: %s", provider.name, e)
