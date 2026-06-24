# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Read, validate, and match Skill metadata.

The helpers centralize frontmatter parsing, platform compatibility checks, and
filesystem discovery for Skills so prompt assembly and slash commands share the
same metadata interpretation.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

import yaml

from mclaw.cli.config import load_config
from mclaw.constants import get_mclaw_home

PLATFORM_MAP = {
    "macos": "darwin",
    "linux": "linux",
    "windows": "win32",
}

EXCLUDED_SKILL_DIRS = frozenset((".git", ".github", ".hub"))


def parse_frontmatter(content: str) -> Tuple[Dict[str, Any], str]:
    """Parse YAML frontmatter from Markdown content."""
    content = content.lstrip("\ufeff")
    frontmatter: Dict[str, Any] = {}
    body = content

    if not content.startswith("---"):
        return frontmatter, body

    end_match = re.search(r"\n---\s*\n", content[3:])
    if not end_match:
        return frontmatter, body

    yaml_content = content[3 : end_match.start() + 3]
    body = content[end_match.end() + 3 :]

    try:
        parsed = yaml.safe_load(yaml_content)
        if isinstance(parsed, dict):
            frontmatter = parsed
    except Exception:
        for line in yaml_content.strip().split("\n"):
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            frontmatter[key.strip()] = value.strip()

    return frontmatter, body


def skill_matches_platform(frontmatter: Dict[str, Any]) -> bool:
    """Return True when a Skill frontmatter platform constraint matches."""
    platforms = frontmatter.get("platforms")
    if not platforms:
        return True
    if not isinstance(platforms, list):
        platforms = [platforms]

    current = sys.platform
    for platform in platforms:
        normalized = str(platform).lower().strip()
        mapped = PLATFORM_MAP.get(normalized, normalized)
        if current.startswith(mapped):
            return True
    return False


def _normalize_string_set(values: Any) -> Set[str]:
    if values is None:
        return set()
    if isinstance(values, str):
        values = [values]
    return {str(v).strip() for v in values if str(v).strip()}


def get_disabled_skill_names(platform: str | None = None, config: dict | None = None) -> Set[str]:
    """Read disabled Skill names from config."""
    if config is None:
        try:
            config = load_config()
        except Exception:
            return set()

    skills_cfg = config.get("skills")
    if not isinstance(skills_cfg, dict):
        return set()

    resolved_platform = platform or os.getenv("MCLAW_PLATFORM")
    if resolved_platform:
        platform_disabled = (skills_cfg.get("platform_disabled") or {}).get(resolved_platform)
        if platform_disabled is not None:
            return _normalize_string_set(platform_disabled)

    return _normalize_string_set(skills_cfg.get("disabled"))


def iter_skill_index_files(skills_dir: Path, filename: str = "SKILL.md"):
    """Yield immediate-child skill index files only."""
    if not skills_dir.exists():
        return
    matches = []
    for child in skills_dir.iterdir():
        if child.name in EXCLUDED_SKILL_DIRS or not child.is_dir():
            continue
        candidate = child / filename
        if candidate.is_file():
            matches.append(candidate)
    for path in sorted(matches, key=lambda p: str(p.relative_to(skills_dir))):
        yield path


def extract_skill_conditions(frontmatter: Dict[str, Any]) -> Dict[str, List]:
    """Extract conditional activation metadata from frontmatter."""
    metadata = frontmatter.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    mclaw_meta = metadata.get("mclaw") or {}
    if not isinstance(mclaw_meta, dict):
        mclaw_meta = {}
    return {
        "fallback_for_toolsets": mclaw_meta.get("fallback_for_toolsets", []),
        "requires_toolsets": mclaw_meta.get("requires_toolsets", []),
        "fallback_for_tools": mclaw_meta.get("fallback_for_tools", []),
        "requires_tools": mclaw_meta.get("requires_tools", []),
    }


def render_skill_vars(content: str, skill_dir: Path, skill_name: str = "") -> str:
    """Render safe Skill text variables."""
    replacements = {
        "{{SKILL_DIR}}": str(skill_dir.resolve()),
        "{{SKILL_NAME}}": skill_name or skill_dir.name,
        "{{MCLAW_HOME}}": "M-Claw home",
        "{{USER_HOME}}": str(Path.home().resolve()),
    }
    for placeholder, value in replacements.items():
        content = content.replace(placeholder, value)
    return content
