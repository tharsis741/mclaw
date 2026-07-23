# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Local Skill list, tree, view, and external search tools."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from mclaw.skills_hub import search
from mclaw.skills_hub.models import ExternalSkill
from mclaw.skills_hub.skill_store import list_skills, tree_skill, view_skill
from mclaw.tools.cancellation import cancellation_checkpoint
from mclaw.tools.interrupt import get_interrupt_event
from mclaw.tools.registry import registry, tool_error

logger = logging.getLogger(__name__)


def _contains_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in str(text or ""))


def _normalize_skill_key(value: str) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text)
    return text.strip("-")


def _infer_search_intent(query: str, results: list[ExternalSkill]) -> str:
    """Classify whether a ClawHub query names a Skill or describes a capability."""
    key = _normalize_skill_key(query)
    if not key:
        return "capability"
    for skill in results:
        if key in {
            _normalize_skill_key(skill.name),
            _normalize_skill_key(skill.slug),
        }:
            return "named_skill"
    return "capability"


def _serialize_external_skill(skill: ExternalSkill) -> dict[str, Any]:
    """Convert external search results into the tool display contract."""
    description = str(skill.description or "").strip()
    description_zh = description if _contains_cjk(description) else ""
    source_ref = str(skill.url or "").strip()
    if not source_ref and skill.source == "clawhub" and skill.author and skill.slug:
        source_ref = f"https://clawhub.ai/{skill.author}/{skill.slug}"
    if not source_ref:
        source_ref = skill.slug
    return {
        "name": skill.name,
        "slug": skill.slug,
        "description": description,
        "description_zh": description_zh,
        "description_needs_translation": not bool(description_zh),
        "source": skill.source,
        "url": skill.url,
        "downloads": skill.downloads,
        "stars": skill.stars,
        "platforms": skill.platforms,
        "install_command": (
            f"skill_manage(action='install_prepare', source='{source_ref}')"
            if source_ref else ""
        ),
    }


def skills_list(task_id: str | None = None) -> str:
    """List enabled local Skill packages without reading their payloads."""
    try:
        skills = list_skills()
        return json.dumps(
            {
                "success": True,
                "skills": skills,
                "count": len(skills),
                "hint": "使用 /<skill-name> <任务> 调用已启用 Skill。",
            },
            ensure_ascii=False,
        )
    except Exception as exc:
        return tool_error(str(exc), success=False)


def skill_view(name: str, file_path: str | None = None, task_id: str | None = None) -> str:
    """View Skill metadata or one allowed non-sidecar file under the Skill root."""
    try:
        if not name:
            return tool_error("Skill name is required.", success=False)
        return json.dumps(view_skill(name, file_path=file_path), ensure_ascii=False)
    except Exception as exc:
        available = [item["name"] for item in list_skills()[:20]]
        return json.dumps(
            {
                "success": False,
                "error": str(exc),
                "available_skills": available,
            },
            ensure_ascii=False,
        )


def skill_tree(name: str, max_entries: int = 500, task_id: str | None = None) -> str:
    """List one Skill's file tree without exposing file contents."""
    try:
        if not name:
            return tool_error("Skill name is required.", success=False)
        return json.dumps(tree_skill(name, max_entries=max_entries), ensure_ascii=False)
    except Exception as exc:
        available = [item["name"] for item in list_skills()[:20]]
        return json.dumps(
            {
                "success": False,
                "error": str(exc),
                "available_skills": available,
            },
            ensure_ascii=False,
        )


def skill_search(
    query: str,
    task_id: str | None = None,
    *,
    parent_agent=None,
) -> str:
    """Search ClawHub and return install suggestions that still require consent."""
    cancel_event = get_interrupt_event()
    try:
        cancellation_checkpoint(cancel_event)
        search_kwargs = {"cancel_event": cancel_event}
        if parent_agent is not None:
            search_kwargs["parent_agent"] = parent_agent
        results = search(query, **search_kwargs)
        cancellation_checkpoint(cancel_event)
        serialized = []
        for item in results:
            cancellation_checkpoint(cancel_event)
            serialized.append(_serialize_external_skill(item))
        search_intent = _infer_search_intent(query, results)
        cancellation_checkpoint(cancel_event)
        return json.dumps(
            {
                "success": True,
                "query": query,
                "search_intent": search_intent,
                "count": len(serialized),
                "results": serialized,
                "display_contract": {
                    "language": "zh-CN",
                    "mode": "capability_recommendation"
                    if search_intent == "capability"
                    else "named_skill_lookup",
                    "instructions": [
                        "只有用户明确要求搜索外部 Skill 时才使用 skill_search。",
                        "展示候选后必须获得用户明确同意，才能调用 skill_manage(action='install_prepare')。",
                    ],
                },
            },
            ensure_ascii=False,
        )
    except InterruptedError as exc:
        return tool_error(
            str(exc),
            success=False,
            status="cancelled",
            interrupted=True,
        )
    except Exception as exc:
        try:
            cancellation_checkpoint(cancel_event)
        except InterruptedError as cancel_exc:
            return tool_error(
                str(cancel_exc),
                success=False,
                status="cancelled",
                interrupted=True,
            )
        return tool_error(str(exc), success=False)


SKILLS_LIST_SCHEMA = {
    "type": "function",
    "function": {
        "name": "skills_list",
        "description": "List enabled local Skills from the M-Claw managed Skills directory (name + short_description).",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

SKILL_VIEW_SCHEMA = {
    "type": "function",
    "function": {
        "name": "skill_view",
        "description": (
            "Load one Skill's mclaw_skill.yaml, SKILL.md, and full skill_evolution.json. "
            "With file_path, read one non-sidecar file under the Skill root."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Skill name."},
                "file_path": {
                    "type": "string",
                    "description": "Optional relative path to a non-sidecar file under the Skill root.",
                },
            },
            "required": ["name"],
        },
    },
}

SKILL_TREE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "skill_tree",
        "description": (
            "List one Skill's file tree without reading file contents. "
            "Use this before skill_view(name, file_path) or skill_manage(write_file/remove_file) "
            "when you need to discover Skill-internal paths."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Skill name."},
                "max_entries": {
                    "type": "integer",
                    "description": "Maximum number of file tree entries to return. Defaults to 500.",
                },
            },
            "required": ["name"],
        },
    },
}

SKILL_SEARCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "skill_search",
        "description": "Search ClawHub for external skills. This does not search the general web.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
}


registry.register(
    name="skills_list",
    toolset="skills",
    schema=SKILLS_LIST_SCHEMA,
    handler=lambda args, **kw: skills_list(task_id=kw.get("task_id")),
    description="List available Skills",
    emoji="📎",
)

registry.register(
    name="skill_view",
    toolset="skills",
    schema=SKILL_VIEW_SCHEMA,
    handler=lambda args, **kw: skill_view(
        args.get("name", ""), file_path=args.get("file_path"), task_id=kw.get("task_id")
    ),
    description="View Skill content",
    emoji="📄",
)

registry.register(
    name="skill_tree",
    toolset="skills",
    schema=SKILL_TREE_SCHEMA,
    handler=lambda args, **kw: skill_tree(
        args.get("name", ""),
        max_entries=args.get("max_entries", 500),
        task_id=kw.get("task_id"),
    ),
    description="List Skill file tree",
    emoji="🌳",
)

registry.register(
    name="skill_search",
    toolset="skills",
    schema=SKILL_SEARCH_SCHEMA,
    handler=lambda args, **kw: skill_search(
        query=args.get("query", ""),
        task_id=kw.get("task_id"),
        parent_agent=kw.get("parent_agent"),
    ),
    description="Search ClawHub Skills",
    emoji="🔎",
)
