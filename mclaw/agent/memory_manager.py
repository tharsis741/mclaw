# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Memory manager for M-Claw."""

from __future__ import annotations

import json
import logging
from typing import Any

from mclaw.agent.memory_provider import MemoryProvider
from mclaw.tools.registry import tool_error

logger = logging.getLogger(__name__)


class MemoryManager:
    """Orchestrates active memory providers for prompt context and tool routing."""

    def __init__(self) -> None:
        self._providers: list[MemoryProvider] = []
        self._tool_to_provider: dict[str, MemoryProvider] = {}
        self._has_external = False

    @staticmethod
    def _provider_available(provider: MemoryProvider) -> bool:
        try:
            return bool(provider.is_available())
        except Exception as e:
            logger.warning("Memory provider '%s' availability check failed: %s", provider.name, e)
            return False

    def add_provider(self, provider: MemoryProvider) -> None:
        """Register a provider while keeping external memory ownership singular."""
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
        self._rebuild_tool_routes()

    @property
    def providers(self) -> list[MemoryProvider]:
        return list(self._providers)

    def initialize(self, session_id: str = "", **kwargs) -> None:
        """Initialize available providers and rebuild tool ownership routes."""
        for provider in self._providers:
            if self._provider_available(provider):
                provider.initialize(session_id=session_id, **kwargs)
        self._rebuild_tool_routes()

    def _active_providers(self) -> list[MemoryProvider]:
        return [
            provider
            for provider in self._providers
            if self._provider_available(provider)
        ]

    def _rebuild_tool_routes(self) -> None:
        routes: dict[str, MemoryProvider] = {}
        for provider in self._active_providers():
            for schema in self._provider_tool_schemas(provider):
                tool_name = schema.get("function", {}).get("name", "")
                if tool_name and tool_name not in routes:
                    routes[tool_name] = provider
        self._tool_to_provider = routes

    def build_system_prompt(self) -> str:
        parts = []
        for provider in self._active_providers():
            try:
                block = provider.system_prompt_block()
                if block and block.strip():
                    parts.append(block)
            except Exception as e:
                logger.warning("Memory provider '%s' system_prompt_block failed: %s", provider.name, e)
        return "\n\n".join(parts)

    def prefetch_all(self, query: str, *, session_id: str = "") -> str:
        parts = []
        for provider in self._active_providers():
            try:
                block = provider.prefetch(query, session_id=session_id)
                if block and block.strip():
                    parts.append(block)
            except Exception as e:
                logger.debug("Memory provider '%s' prefetch failed: %s", provider.name, e)
        return "\n\n".join(parts)

    @staticmethod
    def _provider_tool_schemas(provider: MemoryProvider) -> list[dict[str, Any]]:
        try:
            schemas = provider.get_tool_schemas()
        except Exception as e:
            logger.warning("Memory provider '%s' tool schema discovery failed: %s", provider.name, e)
            return []
        return schemas if isinstance(schemas, list) else []

    def get_all_tool_schemas(self) -> list[dict[str, Any]]:
        result = []
        seen = set()
        for provider in self._active_providers():
            for schema in self._provider_tool_schemas(provider):
                name = schema.get("function", {}).get("name", "")
                if name and name not in seen:
                    result.append(schema)
                    seen.add(name)
        return result

    def get_all_tool_names(self) -> set[str]:
        self._rebuild_tool_routes()
        return set(self._tool_to_provider.keys())

    def has_tool(self, tool_name: str) -> bool:
        self._rebuild_tool_routes()
        return tool_name in self._tool_to_provider

    def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs) -> str:
        """Dispatch one memory tool call to the provider that owns its schema."""
        self._rebuild_tool_routes()
        provider = self._tool_to_provider.get(tool_name)
        if provider is None:
            return tool_error(f"No memory provider handles tool '{tool_name}'", success=False)
        try:
            result = provider.handle_tool_call(tool_name, args, **kwargs)
            self._run_write_hooks(tool_name, result)
            return result
        except Exception as e:
            logger.error("Memory tool call failed for %s via %s: %s", tool_name, provider.name, e)
            return tool_error(f"Memory provider '{provider.name}' failed: {type(e).__name__}: {e}", success=False)

    def _run_write_hooks(self, tool_name: str, result: str) -> None:
        """Refresh providers after successful memory write tools."""
        try:
            from mclaw.tools.memory_tool import MEMORY_WRITE_TOOL_NAMES
        except Exception as e:
            logger.debug("Memory write hook metadata unavailable: %s", e)
            return

        if tool_name not in MEMORY_WRITE_TOOL_NAMES:
            return
        try:
            data = json.loads(result)
        except json.JSONDecodeError as e:
            logger.debug("Memory write result was not JSON, hook skipped: %s", e)
            return
        if isinstance(data, dict) and data.get("success"):
            self.on_memory_write()

    def on_memory_write(self) -> None:
        for provider in self._providers:
            hook = getattr(provider, "on_memory_write", None)
            if callable(hook):
                try:
                    hook()
                except Exception as e:
                    logger.debug("Memory provider '%s' on_memory_write failed: %s", provider.name, e)
