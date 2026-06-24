"""Central registry for all M-Claw tools.

Each tool file calls ``registry.register()`` at module level to declare its
schema, handler, toolset membership, and availability check.
"""

import json
import logging
from typing import Callable, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


class ToolEntry:
    __slots__ = (
        "name", "toolset", "schema", "handler", "check_fn", "diagnose_fn",
        "requires_env", "is_async", "description", "emoji",
        "max_result_size_chars",
    )

    def __init__(self, name, toolset, schema, handler, check_fn=None, diagnose_fn=None,
                 requires_env=None, is_async=False, description="", emoji="",
                 max_result_size_chars=None):
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


class ToolRegistry:
    """Singleton registry that collects tool schemas + handlers from tool files."""

    def __init__(self):
        self._tools: Dict[str, ToolEntry] = {}
        self._toolset_checks: Dict[str, Callable] = {}

    def _ensure_discovered(self) -> None:
        try:
            from mclaw.tools.dispatch import _discover_tools
            _discover_tools()
        except Exception:
            pass

    def register(
        self, name: str, toolset: str, schema: dict, handler: Callable,
        check_fn: Callable = None, diagnose_fn: Callable = None, requires_env: list = None,
        is_async: bool = False, description: str = "", emoji: str = "",
        max_result_size_chars: int | float | None = None,
    ):
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
        )
        if check_fn and toolset not in self._toolset_checks:
            self._toolset_checks[toolset] = check_fn

    def deregister(self, name: str) -> None:
        entry = self._tools.pop(name, None)
        if entry is None:
            return
        if entry.toolset in self._toolset_checks and not any(
            e.toolset == entry.toolset for e in self._tools.values()
        ):
            self._toolset_checks.pop(entry.toolset, None)

    def get_definitions(self, tool_names: Set[str], quiet: bool = False, config: dict | None = None) -> List[dict]:
        self._ensure_discovered()
        result = []
        check_results: Dict[Callable, bool] = {}
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

    def get_tool_diagnostics(self, tool_names: Set[str] | None = None, config: dict | None = None) -> List[dict]:
        self._ensure_discovered()
        names = sorted(tool_names or set(self._tools.keys()))
        diagnostics = []
        for name in names:
            entry = self._tools.get(name)
            if entry:
                diagnostics.append(self._entry_diagnostic(entry, config=config))
        return diagnostics

    def get_toolset_diagnostics(self, config: dict | None = None) -> Dict[str, dict]:
        self._ensure_discovered()
        grouped: Dict[str, dict] = {}
        for item in self.get_tool_diagnostics(config=config):
            ts = item["toolset"]
            state = grouped.setdefault(ts, {"available": True, "tools": [], "unavailable": [], "requirements": []})
            state["tools"].append(item["tool"])
            for req in item.get("requirements") or []:
                if req not in state["requirements"]:
                    state["requirements"].append(req)
            if not item.get("available"):
                state["available"] = False
                state["unavailable"].append(item)
        return grouped

    def dispatch(self, name: str, args: dict, **kwargs) -> str:
        entry = self._tools.get(name)
        if not entry:
            return json.dumps({"error": f"Unknown tool: {name}"})
        try:
            if entry.is_async:
                from mclaw.tools.dispatch import _run_async
                return _run_async(entry.handler(args, **kwargs))
            return entry.handler(args, **kwargs)
        except Exception as e:
            logger.exception("Tool %s dispatch error: %s", name, e)
            return json.dumps({"error": f"Tool execution failed: {type(e).__name__}: {e}"})

    def get_all_tool_names(self) -> List[str]:
        self._ensure_discovered()
        return sorted(self._tools.keys())

    def get_schema(self, name: str) -> Optional[dict]:
        self._ensure_discovered()
        entry = self._tools.get(name)
        return entry.schema if entry else None

    def get_description(self, name: str) -> str:
        """Return the tool's short description (Chinese), or empty string if not found."""
        self._ensure_discovered()
        entry = self._tools.get(name)
        return entry.description if entry else ""

    def get_toolset_for_tool(self, name: str) -> Optional[str]:
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

    def get_emoji(self, name: str, default: str = "⚡") -> str:
        self._ensure_discovered()
        entry = self._tools.get(name)
        return (entry.emoji if entry and entry.emoji else default)

    def get_tool_to_toolset_map(self) -> Dict[str, str]:
        self._ensure_discovered()
        return {name: e.toolset for name, e in self._tools.items()}

    def is_toolset_available(self, toolset: str) -> bool:
        self._ensure_discovered()
        check = self._toolset_checks.get(toolset)
        if not check:
            return True
        try:
            return bool(check())
        except Exception:
            return False

    def check_toolset_requirements(self) -> Dict[str, bool]:
        self._ensure_discovered()
        toolsets = set(e.toolset for e in self._tools.values())
        return {ts: self.is_toolset_available(ts) for ts in sorted(toolsets)}

    def get_available_toolsets(self) -> Dict[str, dict]:
        self._ensure_discovered()
        diagnostics = self.get_toolset_diagnostics()
        toolsets: Dict[str, dict] = {}
        for ts, info in diagnostics.items():
            toolsets[ts] = {
                "available": bool(info.get("available")),
                "tools": sorted(info.get("tools") or []),
                "description": "",
                "requirements": sorted(info.get("requirements") or []),
                "unavailable": info.get("unavailable") or [],
            }
        return toolsets


registry = ToolRegistry()


def tool_error(message, **extra) -> str:
    result = {"error": str(message)}
    if extra:
        result.update(extra)
    return json.dumps(result, ensure_ascii=False)


def tool_result(data=None, **kwargs) -> str:
    if data is not None:
        return json.dumps(data, ensure_ascii=False)
    return json.dumps(kwargs, ensure_ascii=False)
