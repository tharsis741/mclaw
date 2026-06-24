"""Skill registry for slash command discovery and caching."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict

from mclaw.cli.runtime.commands import builtin_command_names
from mclaw.skills_hub import skill_store

logger = logging.getLogger(__name__)

_BUILTIN_COMMANDS = builtin_command_names()


@dataclass
class SkillMeta:
    name: str
    description: str
    category: str
    path: Path


class SkillRegistry:
    """Lightweight in-memory registry of enabled Skills for slash commands."""

    def __init__(self):
        self._skills: Dict[str, SkillMeta] = {}
        self._last_scan: float = 0

    def refresh(self) -> None:
        """Scan the enabled Skill root and update cache."""
        self._skills = {}
        for item in skill_store.list_skills():
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            if name in _BUILTIN_COMMANDS:
                logger.warning("Skill '%s' conflicts with built-in slash command, skipped", name)
                continue
            root = Path(str(item.get("path") or ""))
            self._skills[name] = SkillMeta(
                name=name,
                description=str(item.get("short_description") or item.get("description") or "").strip(),
                category="general",
                path=root / "SKILL.md",
            )
        self._last_scan = time.time()
        logger.debug("SkillRegistry refreshed: %d skills", len(self._skills))

    def list_skills(self):
        return sorted(self._skills.values(), key=lambda s: s.name)

    def get_skill(self, name: str) -> SkillMeta | None:
        return self._skills.get(name)

    def invalidate(self) -> None:
        """Mark cache as stale so next access triggers refresh."""
        self._last_scan = 0

    @property
    def builtin_commands(self):
        return _BUILTIN_COMMANDS


_global_registry: SkillRegistry | None = None


def get_skill_registry() -> SkillRegistry:
    """Return the global SkillRegistry singleton."""
    global _global_registry
    if _global_registry is None:
        _global_registry = SkillRegistry()
    return _global_registry
