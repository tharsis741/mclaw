# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Central registry for all M-Claw tools.

Each tool file calls ``registry.register()`` at module level to declare its
schema, handler, toolset membership, and availability check.
"""

import json
import logging
from collections.abc import Callable

logger = logging.getLogger(__name__)


class ToolEntry:
    """Registered tool metadata used for schema exposure and handler dispatch."""

    __slots__ = (
        "name", "toolset", "schema", "handler", "check_fn", "diagnose_fn",
        "requires_env", "is_async", "description", "emoji",
        "max_result_size_chars",
        "async_timeout_seconds",
    )

    def __init__(self, name, toolset, schema, handler, check_fn=None, diagnose_fn=None,
                 requires_env=None, is_async=False, description="", emoji="",
                 max_result_size_chars=None, async_timeout_seconds=None):
        self.name = name
        self.toolset = toolset
        self.schema = schema
        self.handler = handler
        self.check_fn = check_fn
        self.diagnose_fn = diagnose_fn
        self.requires_env = requires_env
        self.is_async = is_async
        self.description = description
        self.emoji = emoji
        self.max_result_size_chars = max_result_size_chars
        self.async_timeout_seconds = async_timeout_seconds


class ToolRegistry:
    """Singleton registry that collects tool schemas + handlers from tool files."""

    def __init__(self):
        self._tools: dict[str, ToolEntry] = {}

    def _ensure_discovered(self) -> None:
        try:
            from mclaw.tools.dispatch import _discover_tools
            _discover_tools()
        except Exception as exc:
            logger.warning("Tool discovery failed: %s", exc, exc_info=True)

    def register(
        self, name: str, toolset: str, schema: dict, handler: Callable,
        check_fn: Callable | None = None, diagnose_fn: Callable | None = None, requires_env: list | None = None,
        is_async: bool = False, description: str = "", emoji: str = "",
        max_result_size_chars: int | float | None = None,
        async_timeout_seconds: int | float | None = None,
    ):
        """Register one callable tool and its model-visible schema."""
        existing = self._tools.get(name)
        if existing and existing.toolset != toolset:
            logger.warning(
                "Tool name collision: '%s' (toolset '%s') overwritten by '%s'",
                name, existing.toolset, toolset,
            )
        self._tools[name] = ToolEntry(
            name=name, toolset=toolset, schema=schema, handler=handler,
            check_fn=check_fn, diagnose_fn=diagnose_fn, requires_env=requires_env or [],
            is_async=is_async,
            description=description or schema.get("description", ""),
            emoji=emoji, max_result_size_chars=max_result_size_chars,
            async_timeout_seconds=async_timeout_seconds,
        )

    def get_definitions(self, tool_names: set[str], config: dict | None = None) -> list[dict]:
        """Return schemas for enabled and currently available tools only."""
        self._ensure_discovered()
        result = []
        check_results: dict[Callable, bool] = {}
        for name in sorted(tool_names):
            entry = self._tools.get(name)
            if not entry:
                continue
            if entry.check_fn:
                if entry.check_fn not in check_results:
                    try:
                        if config is not None:
                            try:
                                check_results[entry.check_fn] = bool(entry.check_fn(config=config))
                            except TypeError:
                                check_results[entry.check_fn] = bool(entry.check_fn())
                        else:
                            check_results[entry.check_fn] = bool(entry.check_fn())
                    except Exception:
                        logger.debug("Tool availability check failed for %s", entry.name, exc_info=True)
                        check_results[entry.check_fn] = False
                if not check_results[entry.check_fn]:
                    continue
            fn_def = dict(entry.schema.get("function", {}))
            fn_def["name"] = entry.name
            result.append({
                "type": "function",
                "function": fn_def,
            })

        return result


    def _entry_diagnostic(self, entry: ToolEntry, config: dict | None = None) -> dict:
        if entry.diagnose_fn:
            try:
                try:
                    data = entry.diagnose_fn(config=config)
                except TypeError:
                    data = entry.diagnose_fn()
                if isinstance(data, dict):
                    available = bool(data.get("available", data.get("ok", False)))
                    return {
                        "tool": entry.name,
                        "toolset": entry.toolset,
                        "available": available,
                        "reason": str(data.get("reason") or ("available" if available else "unavailable")),
                        "fix": str(data.get("fix") or ""),
                        "requirements": list(entry.requires_env or []),
                    }
            except Exception as exc:
                logger.debug("Tool diagnostic failed for %s: %s", entry.name, exc)
                return {
                    "tool": entry.name,
                    "toolset": entry.toolset,
                    "available": False,
                    "reason": f"diagnostic failed: {type(exc).__name__}: {exc}",
                    "fix": "Check tool configuration and dependencies.",
                    "requirements": list(entry.requires_env or []),
                }

        available = True
        reason = "available"
        fix = ""
        if entry.check_fn:
            try:
                try:
                    available = bool(entry.check_fn(config=config))
                except TypeError:
                    available = bool(entry.check_fn())
            except Exception as exc:
                available = False
                reason = f"requirement check failed: {type(exc).__name__}: {exc}"
                fix = "Check tool configuration and dependencies."
        if not available and reason == "available":
            reason = "requirement check returned false"
        return {
            "tool": entry.name,
            "toolset": entry.toolset,
            "available": available,
            "reason": reason,
            "fix": fix,
            "requirements": list(entry.requires_env or []),
        }

    def get_tool_diagnostics(self, tool_names: set[str] | None = None, config: dict | None = None) -> list[dict]:
        """Return user-facing availability diagnostics for selected tools."""
        self._ensure_discovered()
        names = sorted(tool_names or set(self._tools.keys()))
        diagnostics = []
        for name in names:
            entry = self._tools.get(name)
            if entry:
                diagnostics.append(self._entry_diagnostic(entry, config=config))
        return diagnostics

    def dispatch(self, name: str, args: dict, **kwargs) -> str:
        """Invoke a registered handler and normalize unexpected failures to JSON."""
        entry = self._tools.get(name)
        if not entry:
            return json.dumps({"error": f"Unknown tool: {name}"})
        try:
            if entry.is_async:
                from mclaw.tools.dispatch import _run_async
                return _run_async(
                    entry.handler(args, **kwargs),
                    parent_agent=kwargs.get("parent_agent"),
                    timeout_seconds=entry.async_timeout_seconds,
                )
            return entry.handler(args, **kwargs)
        except Exception as e:
            if getattr(e, "termination_fence", None) is not None:
                raise
            logger.exception("Tool %s dispatch error: %s", name, e)
            return json.dumps({"error": f"Tool execution failed: {type(e).__name__}: {e}"})

    def get_description(self, name: str) -> str:
        """Return the tool's short description, or empty string if not found."""
        self._ensure_discovered()
        entry = self._tools.get(name)
        return entry.description if entry else ""

    def get_toolset_for_tool(self, name: str) -> str | None:
        self._ensure_discovered()
        entry = self._tools.get(name)
        return entry.toolset if entry else None

    def get_max_result_size(self, name: str, default: int | float | None = None) -> int | float | None:
        """Return per-tool max result size, or default if not set."""
        self._ensure_discovered()
        entry = self._tools.get(name)
        if entry and entry.max_result_size_chars is not None:
            return entry.max_result_size_chars
        return default


registry = ToolRegistry()


def tool_error(message, **extra) -> str:
    """Build a JSON error payload for tool handlers."""
    result = {"error": str(message)}
    if extra:
        result.update(extra)
    return json.dumps(result, ensure_ascii=False)


def tool_result(data=None, **kwargs) -> str:
    """Build a JSON success payload for tool handlers."""
    if data is not None:
        return json.dumps(data, ensure_ascii=False)
    return json.dumps(kwargs, ensure_ascii=False)
