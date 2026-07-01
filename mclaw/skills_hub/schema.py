# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Schema validation for Skill package sidecars and drafting manifests."""

from __future__ import annotations

from typing import Any

from mclaw.skills_hub.paths import validate_drafting_id, validate_skill_name

EVOLUTION_SECTIONS = (
    "adaptation_summary",
    "user_preferences",
    "known_failures",
    "runtime_notes",
)
SOURCE_TYPES = {"agent_created", "github", "clawhub", "local", "bundled"}
ACTORS = {"install", "main_agent", "background_review", "bundled"}
SKILL_YAML_FIELDS = frozenset(
    {
        "schema_version",
        "name",
        "short_description",
        "source",
        "created_at",
        "updated_at",
        "created_by",
        "updated_by",
        "status",
    }
)
MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "drafting_id",
        "skill_name",
        "source",
        "status",
        "created_at",
        "updated_at",
        "security_review_path",
        "package_path",
        "user_intent",
        "dependency_hints",
    }
)


class SkillSchemaError(ValueError):
    """Raised when a Skill package sidecar is invalid."""


def is_chinese_text(text: str) -> bool:
    """Return whether text contains Chinese characters for local description policy."""
    return any("\u4e00" <= ch <= "\u9fff" for ch in str(text or ""))


def validate_skill_yaml(data: dict[str, Any], *, dir_name: str | None = None) -> dict[str, Any]:
    """Validate the enabled/prepared Skill metadata sidecar."""
    if not isinstance(data, dict):
        raise SkillSchemaError("mclaw_skill.yaml must be a mapping.")
    extra = sorted(set(data) - SKILL_YAML_FIELDS)
    if extra:
        raise SkillSchemaError(f"Unsupported mclaw_skill.yaml fields: {', '.join(extra)}")
    if data.get("schema_version") != 1:
        raise SkillSchemaError("schema_version must be 1.")
    name = validate_skill_name(str(data.get("name") or ""))
    if dir_name and name != dir_name:
        raise SkillSchemaError("mclaw_skill.yaml name must match the skill directory.")
    short_description = str(data.get("short_description") or "").strip()
    if not short_description:
        raise SkillSchemaError("short_description is required.")
    source = data.get("source")
    if not isinstance(source, dict):
        raise SkillSchemaError("source must be a mapping.")
    source_type = str(source.get("type") or "").strip()
    if source_type not in SOURCE_TYPES:
        raise SkillSchemaError(f"Invalid source.type: {source_type}")
    if "original" not in source:
        raise SkillSchemaError("source.original is required.")
    if source_type == "agent_created" and not is_chinese_text(short_description):
        raise SkillSchemaError("agent_created short_description must be Chinese.")
    for field in ("created_at", "updated_at"):
        if not str(data.get(field) or "").strip():
            raise SkillSchemaError(f"{field} is required.")
    for field in ("created_by", "updated_by"):
        if str(data.get(field) or "").strip() not in ACTORS:
            raise SkillSchemaError(f"{field} must be one of: {', '.join(sorted(ACTORS))}")
    if str(data.get("status") or "").strip() not in {"enabled", "prepared"}:
        raise SkillSchemaError("status must be enabled or prepared.")
    return data


def validate_evolution(data: dict[str, Any]) -> dict[str, list[str]]:
    """Validate and dedupe the mutable Skill evolution note sections."""
    if not isinstance(data, dict):
        raise SkillSchemaError("skill_evolution.json must be an object.")
    extra = sorted(set(data) - set(EVOLUTION_SECTIONS))
    if extra:
        raise SkillSchemaError(f"Unsupported skill_evolution.json fields: {', '.join(extra)}")
    result: dict[str, list[str]] = {}
    for section in EVOLUTION_SECTIONS:
        value = data.get(section, [])
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise SkillSchemaError(f"{section} must be an array of strings.")
        result[section] = list(dict.fromkeys(item.strip() for item in value if item.strip()))
    return result


def validate_manifest(data: dict[str, Any]) -> dict[str, Any]:
    """Validate an install drafting manifest before confirmation actions use it."""
    if not isinstance(data, dict):
        raise SkillSchemaError("import_manifest.yaml must be a mapping.")
    extra = sorted(set(data) - MANIFEST_FIELDS)
    if extra:
        raise SkillSchemaError(f"Unsupported import_manifest.yaml fields: {', '.join(extra)}")
    if data.get("schema_version") != 1:
        raise SkillSchemaError("manifest schema_version must be 1.")
    validate_drafting_id(str(data.get("drafting_id") or ""))
    if str(data.get("status") or "") not in {"prepared", "blocked"}:
        raise SkillSchemaError("manifest status must be prepared or blocked.")
    validate_skill_name(str(data.get("skill_name") or ""))
    if str(data.get("package_path") or "") != "skill":
        raise SkillSchemaError("manifest package_path must be skill.")
    if str(data.get("security_review_path") or "") != "security_review.json":
        raise SkillSchemaError("manifest security_review_path must be security_review.json.")
    source = data.get("source")
    if not isinstance(source, dict) or str(source.get("type") or "") not in SOURCE_TYPES:
        raise SkillSchemaError("manifest source is invalid.")
    if "original" not in source:
        raise SkillSchemaError("manifest source.original is required.")
    for field in ("created_at", "updated_at"):
        if not str(data.get(field) or "").strip():
            raise SkillSchemaError(f"manifest {field} is required.")
    if not isinstance(data.get("user_intent", ""), str):
        raise SkillSchemaError("manifest user_intent must be a string.")
    if not isinstance(data.get("dependency_hints"), list):
        raise SkillSchemaError("manifest dependency_hints must be a list.")
    return data
