# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Read, generate, and update mclaw_skill.yaml sidecars."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from mclaw.agent.skill_utils import parse_frontmatter
from mclaw.skills_hub.schema import validate_skill_yaml


def now_iso() -> str:
    """Return the local timestamp format used by Skill sidecars."""

    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def extract_short_description(skill_md: str, fallback: str) -> str:
    """Derive a short sidecar description from SKILL.md frontmatter."""

    frontmatter, _ = parse_frontmatter(skill_md or "")
    description = str(frontmatter.get("description") or "").strip().strip("'\"")
    return description or fallback


def build_skill_yaml(
    *,
    name: str,
    short_description: str,
    source_type: str,
    source_original: str = "",
    actor: str,
    status: str,
    created_at: str | None = None,
    updated_at: str | None = None,
) -> dict[str, Any]:
    """Build and validate the canonical mclaw_skill.yaml payload."""

    ts = created_at or now_iso()
    data = {
        "schema_version": 1,
        "name": name,
        "short_description": short_description,
        "source": {
            "type": source_type,
            "original": source_original or "",
        },
        "created_at": ts,
        "updated_at": updated_at or ts,
        "created_by": actor,
        "updated_by": actor,
        "status": status,
    }
    return validate_skill_yaml(data, dir_name=name)


def read_skill_yaml(skill_dir: Path) -> dict[str, Any]:
    """Read a Skill metadata sidecar and validate it against the directory name."""

    path = skill_dir / "mclaw_skill.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else None
    expected_name = str((data or {}).get("name") or skill_dir.name)
    if skill_dir.name != "skill":
        expected_name = skill_dir.name
    return validate_skill_yaml(data or {}, dir_name=expected_name)


def write_skill_yaml(skill_dir: Path, data: dict[str, Any]) -> None:
    """Validate and persist the mclaw_skill.yaml sidecar for one Skill."""

    expected_name = str(data.get("name") or skill_dir.name)
    if skill_dir.name != "skill":
        expected_name = skill_dir.name
    validate_skill_yaml(data, dir_name=expected_name)
    path = skill_dir / "mclaw_skill.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def touch_skill_yaml(skill_dir: Path, *, actor: str) -> dict[str, Any]:
    """Update modification metadata after a managed Skill content change."""

    data = read_skill_yaml(skill_dir)
    data["updated_at"] = now_iso()
    data["updated_by"] = actor
    write_skill_yaml(skill_dir, data)
    return data


def mark_enabled(skill_dir: Path, *, actor: str = "install") -> dict[str, Any]:
    """Mark a prepared Skill as enabled after it has moved into the active root."""

    data = read_skill_yaml(skill_dir)
    data["status"] = "enabled"
    data["updated_at"] = now_iso()
    data["updated_by"] = actor
    write_skill_yaml(skill_dir, data)
    return data
