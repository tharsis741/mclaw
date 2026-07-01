# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Skill package manager tool.

All Skill writes go through the skills_hub service layer. This module only
normalizes tool arguments, returns JSON, and exposes the tool schema.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from mclaw.skills_hub import install_service, skill_store
from mclaw.tools.registry import registry, tool_error

logger = logging.getLogger(__name__)

SKILL_MANAGE_ACTION_ORDER = (
    "create_scaffold",
    "create",
    "edit",
    "patch",
    "delete",
    "validate",
    "write_file",
    "remove_file",
    "security_review",
    "install_prepare",
    "enable_drafting",
    "cancel_drafting",
    "evolution_update",
)


def _json(result: dict[str, Any]) -> str:
    return json.dumps(result, ensure_ascii=False)


def _normalize_initial_evolution(value: Any) -> dict[str, Any] | None:
    """Validate optional initial evolution sections before store-layer writes."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("initial_evolution must be an object.")
    return value


def skill_manage(
    action: str,
    name: str = "",
    skill_md: str | None = None,
    short_description: str | None = None,
    initial_evolution: dict[str, Any] | None = None,
    file_path: str | None = None,
    content: str | None = None,
    encoding: str | None = None,
    overwrite: bool = False,
    old_text: str | None = None,
    new_text: str | None = None,
    source: str | None = None,
    user_intent: str | None = None,
    drafting_id: str | None = None,
    section: str | None = None,
    operation: str | None = None,
    actor: str = "main_agent",
) -> str:
    """Route model-callable Skill mutations through the service layer.

    This public tool is the single JSON boundary for Skill writes, validation,
    draft installation, security review, and evolution updates. Store and
    install services enforce filesystem policy; the tool adds action routing,
    audit actor propagation, and model-friendly error serialization.
    """
    action = str(action or "").strip()
    try:
        if action == "create_scaffold":
            result = skill_store.create_skill_scaffold(
                name=name,
                short_description=short_description or "",
                user_intent=user_intent,
                actor=actor,
            )
        elif action == "create":
            result = skill_store.create_skill(
                name=name,
                skill_md=skill_md or "",
                short_description=short_description or "",
                initial_evolution=_normalize_initial_evolution(initial_evolution),
                actor=actor,
            )
        elif action == "edit":
            result = skill_store.edit_skill(name=name, skill_md=skill_md or "", actor=actor)
        elif action == "patch":
            result = skill_store.patch_skill(
                name=name,
                old_text=old_text or "",
                new_text=new_text or "",
                actor=actor,
            )
        elif action == "write_file":
            result = skill_store.write_skill_file(
                name=name,
                file_path=file_path or "",
                content=content or "",
                encoding=encoding or "text",
                overwrite=bool(overwrite),
                actor=actor,
            )
        elif action == "remove_file":
            result = skill_store.remove_skill_file(
                name=name,
                file_path=file_path or "",
                actor=actor,
            )
        elif action == "delete":
            result = skill_store.delete_skill(name=name)
        elif action == "validate":
            result = skill_store.validate_skill(name=name)
        elif action == "install_prepare":
            result = install_service.install_prepare(source or "", user_intent=user_intent)
        elif action == "enable_drafting":
            result = install_service.enable_drafting(drafting_id or "")
        elif action == "cancel_drafting":
            result = install_service.cancel_drafting(drafting_id or "")
        elif action == "evolution_update":
            result = skill_store.evolution_update(
                name=name,
                section=section or "",
                operation=operation or "",
                content=content,
                old_text=old_text,
            )
        elif action == "security_review":
            result = install_service.security_review(drafting_id or "")
        else:
            return tool_error(
                f"Unknown action. Use: {', '.join(SKILL_MANAGE_ACTION_ORDER)}",
                success=False,
            )
        return _json(result)
    except Exception as exc:
        logger.debug("skill_manage failed action=%s: %s", action, exc, exc_info=True)
        return tool_error(str(exc), success=False)


SKILL_MANAGE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "skill_manage",
        "description": (
            "Scaffold, validate, create, install, enable, edit, patch, delete, and maintain local M-Claw Skills. "
            "All Skill writes must use this tool. create_scaffold initializes a minimal editable Skill; "
            "install_prepare prepares GitHub, ClawHub, or local directory Skills and returns one "
            "skill_enable_drafting confirmation payload plus dependency_hints. install_prepare never "
            "collects secrets; use secret_request_many later when a Skill actually needs a key."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": list(SKILL_MANAGE_ACTION_ORDER),
                },
                "name": {"type": "string", "description": "Enabled Skill name."},
                "skill_md": {
                    "type": "string",
                    "description": (
                        "Full SKILL.md content for create/edit. Must include YAML frontmatter "
                        "with name and description."
                    ),
                },
                "short_description": {
                    "type": "string",
                    "description": "Chinese short description for agent_created skills.",
                },
                "initial_evolution": {
                    "type": "object",
                    "description": (
                        "Initial skill_evolution.json sections. Each section must be an array of strings."
                    ),
                    "properties": {
                        "adaptation_summary": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "user_preferences": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "known_failures": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "runtime_notes": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "additionalProperties": False,
                },
                "file_path": {
                    "type": "string",
                    "description": (
                        "Relative path to a non-system file under the Skill root. "
                        "Do not target SKILL.md, mclaw_skill.yaml, skill_evolution.json, "
                        "install_audit.json, security_review.json, or skill_evolution.json.lock."
                    ),
                },
                "content": {
                    "type": "string",
                    "description": "Content for write_file or evolution_update append/replace.",
                },
                "encoding": {"type": "string", "enum": ["text", "base64"]},
                "overwrite": {"type": "boolean"},
                "old_text": {"type": "string", "description": "Unique old text for patch/replace/remove."},
                "new_text": {"type": "string", "description": "Replacement text for patch."},
                "source": {"type": "string", "description": "GitHub, ClawHub, or local Skill folder source."},
                "user_intent": {
                    "type": "string",
                    "description": "Original user install or creation intent.",
                },
                "drafting_id": {"type": "string", "description": "skill_drafting_* id."},
                "section": {
                    "type": "string",
                    "enum": [
                        "adaptation_summary",
                        "user_preferences",
                        "known_failures",
                        "runtime_notes",
                    ],
                },
                "operation": {"type": "string", "enum": ["append", "replace", "remove"]},
            },
            "required": ["action"],
        },
    },
}


def _handle_skill_manage(args: dict[str, Any], **kw) -> str:
    """Resolve dispatch context before invoking the model-facing manage API."""
    actor = str(kw.get("execution_actor") or "main_agent")
    parent_agent = kw.get("parent_agent")
    if parent_agent is not None:
        actor = str(getattr(parent_agent, "_execution_actor", actor) or actor)
    return skill_manage(
        action=args.get("action", ""),
        name=args.get("name", ""),
        skill_md=args.get("skill_md"),
        short_description=args.get("short_description"),
        initial_evolution=args.get("initial_evolution"),
        file_path=args.get("file_path"),
        content=args.get("content"),
        encoding=args.get("encoding"),
        overwrite=args.get("overwrite", False),
        old_text=args.get("old_text"),
        new_text=args.get("new_text"),
        source=args.get("source"),
        user_intent=args.get("user_intent"),
        drafting_id=args.get("drafting_id"),
        section=args.get("section"),
        operation=args.get("operation"),
        actor=actor,
    )


registry.register(
    name="skill_manage",
    toolset="skills",
    schema=SKILL_MANAGE_SCHEMA,
    handler=_handle_skill_manage,
    description="Manage M-Claw Skills",
    emoji="🧩",
)
