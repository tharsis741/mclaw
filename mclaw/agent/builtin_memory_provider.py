"""Built-in file-backed memory provider for M-Claw."""

from __future__ import annotations

import copy
from mclaw.agent.memory_provider import MemoryProvider
from mclaw.tools.memory_tool import MEMORY_TOOL_SCHEMAS, handle_memory_tool_call
from mclaw.tools.registry import tool_error


class BuiltinMemoryProvider(MemoryProvider):
    def __init__(
        self,
        memory_store=None,
        memory_enabled: bool = True,
        user_profile_enabled: bool = True,
    ):
        self._store = memory_store
        self._memory_enabled = memory_enabled
        self._user_profile_enabled = user_profile_enabled

    @property
    def name(self) -> str:
        return "builtin"

    def is_available(self) -> bool:
        return self._store is not None

    def initialize(self, session_id: str = "", **kwargs) -> None:
        if self._store is not None:
            self._store.load_from_disk()

    def system_prompt_block(self) -> str:
        if not self._store:
            return ""

        parts = []
        if self._memory_enabled:
            block = self._store.format_for_system_prompt("memory")
            if block:
                parts.append(block)
        if self._user_profile_enabled:
            block = self._store.format_for_system_prompt("user")
            if block:
                parts.append(block)
        return "\n\n".join(parts)

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if not self._store:
            return ""
        return self._store.prefetch(query)

    def on_memory_write(self) -> None:
        if self._store is not None:
            self._store.load_from_disk()

    def get_tool_schemas(self):
        targets = []
        if self._memory_enabled:
            targets.append("memory")
        if self._user_profile_enabled:
            targets.append("user")
        if not targets:
            return []

        schemas = copy.deepcopy(MEMORY_TOOL_SCHEMAS)
        for schema in schemas:
            target_schema = schema["function"]["parameters"]["properties"].get("target")
            if not target_schema:
                continue
            target_schema["enum"] = targets
            if targets == ["memory"]:
                target_schema["description"] = "Only 'memory' is enabled."
            elif targets == ["user"]:
                target_schema["description"] = "Only 'user' profile memory is enabled."
        return schemas

    def handle_tool_call(self, tool_name: str, args: dict, **kwargs) -> str:
        if tool_name not in {schema["function"]["name"] for schema in MEMORY_TOOL_SCHEMAS}:
            return super().handle_tool_call(tool_name, args, **kwargs)
        target = args.get("target", "memory")
        if target == "memory" and not self._memory_enabled:
            return tool_error("Memory target 'memory' is disabled by configuration.", success=False)
        if target == "user" and not self._user_profile_enabled:
            return tool_error("Memory target 'user' is disabled by configuration.", success=False)
        return handle_memory_tool_call(tool_name, args, store=self._store)
