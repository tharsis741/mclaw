"""Background memory and Skill-evolution review orchestration."""

from __future__ import annotations

import contextlib
import copy
import json
import logging
import os
import sys
import threading
from typing import Any

from mclaw.prompts import background as background_prompts
from mclaw.tools.dispatch import tool_dispatch_policy

logger = logging.getLogger(__name__)

MEMORY_REVIEW_PROMPT = background_prompts.MEMORY_REVIEW_PROMPT
EVOLUTION_REVIEW_PROMPT = background_prompts.EVOLUTION_REVIEW_PROMPT
COMBINED_REVIEW_PROMPT = background_prompts.COMBINED_REVIEW_PROMPT

REVIEW_MEMORY_TOOL_WHITELIST = {"memory_read", "memory_add", "memory_replace", "memory_remove"}
REVIEW_TOOL_WHITELIST = REVIEW_MEMORY_TOOL_WHITELIST | {"skills_list", "skill_tree", "skill_view", "skill_manage"}
REVIEW_SKILL_ACTION_WHITELIST = {
    "create",
    "edit",
    "patch",
    "delete",
    "write_file",
    "remove_file",
    "evolution_update",
}


def _review_agent_factory(parent: Any) -> Any:
    """Return the active class/factory for the parent agent.

    Tests and embedders sometimes patch the agent class on its defining module.
    Resolving through the module preserves that hook while keeping this module
    independent from mclaw.agent.core imports.
    """
    agent_cls = parent.__class__
    module = sys.modules.get(getattr(agent_cls, "__module__", ""))
    if module is not None:
        return getattr(module, getattr(agent_cls, "__name__", ""), agent_cls)
    return agent_cls


def _prompt_for(review_memory: bool, review_skills: bool) -> str:
    return background_prompts.build_background_review_prompt(
        review_memory=review_memory,
        review_skills=review_skills,
    )


def _parent_system_prompt(parent: Any, messages_snapshot: list[dict[str, Any]]) -> str:
    for msg in messages_snapshot:
        if isinstance(msg, dict) and msg.get("role") == "system":
            return str(msg.get("content") or "")
    for msg in getattr(parent, "messages", []) or []:
        if isinstance(msg, dict) and msg.get("role") == "system":
            return str(msg.get("content") or "")
    return str(getattr(parent, "system_prompt", "") or "")


def _tool_name(tool_def: dict[str, Any]) -> str:
    return str((tool_def.get("function") or {}).get("name") or "")


def _restrict_review_agent_tools(review_agent: Any) -> None:
    tools = getattr(review_agent, "tools", []) or []
    if not isinstance(tools, list):
        tools = []
    filtered: list[dict[str, Any]] = []
    action_order = [
        "create",
        "edit",
        "patch",
        "delete",
        "write_file",
        "remove_file",
        "evolution_update",
    ]
    for tool_def in tools:
        if not isinstance(tool_def, dict):
            continue
        name = _tool_name(tool_def)
        if name not in REVIEW_TOOL_WHITELIST:
            continue
        cloned = copy.deepcopy(tool_def)
        if name == "skill_manage":
            try:
                properties = cloned["function"]["parameters"]["properties"]
                properties["action"]["enum"] = [
                    action for action in action_order if action in REVIEW_SKILL_ACTION_WHITELIST
                ]
            except Exception:
                pass
        filtered.append(cloned)
    review_agent.tools = filtered
    review_agent.valid_tool_names = {_tool_name(tool_def) for tool_def in filtered}


def _tool_result_signature(msg: dict[str, Any]) -> str:
    content = msg.get("content", "")
    try:
        return json.dumps(json.loads(content), sort_keys=True, ensure_ascii=False)
    except (json.JSONDecodeError, TypeError):
        return str(content)


def summarize_background_review_actions(
    review_messages: list[dict[str, Any]],
    prior_snapshot: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Summarize successful Skill writes created by this review pass only."""
    action_order = {
        "create": 0,
        "edit": 1,
        "patch": 2,
        "delete": 3,
        "write_file": 4,
        "remove_file": 5,
        "evolution_update": 6,
    }
    prior_tool_results = {
        _tool_result_signature(msg)
        for msg in (prior_snapshot or [])
        if isinstance(msg, dict) and msg.get("role") == "tool"
    }
    actions_by_name: dict[str, set[str]] = {}

    for msg in review_messages:
        if not isinstance(msg, dict) or msg.get("role") != "tool":
            continue
        if _tool_result_signature(msg) in prior_tool_results:
            continue
        try:
            data = json.loads(msg.get("content", "{}"))
        except (json.JSONDecodeError, TypeError):
            continue
        if not data.get("success"):
            continue
        action = str(data.get("action") or "").strip()
        if action not in action_order:
            continue
        name = str(data.get("name") or data.get("skill_name") or "").strip()
        if not name:
            continue
        actions_by_name.setdefault(name, set()).add(action)

    if not actions_by_name:
        return None

    def _status_label(actions: set[str]) -> str:
        if "delete" in actions:
            return "\u5df2\u5220\u9664"
        if "create" in actions and (actions - {"create"}):
            return "\u5df2\u521b\u5efa\u5e76\u5b8c\u5584"
        if "create" in actions:
            return "\u5df2\u521b\u5efa"
        if "evolution_update" in actions:
            return "\u5df2\u66f4\u65b0\u7ecf\u9a8c"
        if actions & {"edit", "patch", "write_file", "remove_file"}:
            return "\u5df2\u66f4\u65b0"
        return "\u5df2\u4fdd\u5b58"

    skills = [
        {
            "name": name,
            "actions": sorted(actions, key=lambda item: action_order[item]),
            "status": _status_label(actions),
        }
        for name, actions in sorted(actions_by_name.items())
    ]
    summary = "\uff1b".join(f"{item['name']}\uff08{item['status']}\uff09" for item in skills)
    return {
        "summary": summary,
        "text": f"\u6280\u80fd\u5df2\u4fdd\u5b58\uff1a{summary}",
        "skills": skills,
    }


def spawn_background_review(
    parent: Any,
    *,
    messages_snapshot: list[dict[str, Any]],
    review_memory: bool = False,
    review_skills: bool = False,
) -> None:
    """Start an isolated background review thread for memory and Skill evolution."""
    prompt = _prompt_for(review_memory, review_skills)
    session_id = getattr(parent, "session_id", None)
    logger.info(
        "Background review scheduled: session=%s memory=%s skills=%s messages=%d",
        session_id,
        review_memory,
        review_skills,
        len(messages_snapshot or []),
    )

    def _run_review() -> None:
        review_agent = None
        try:
            logger.info(
                "Background review started: session=%s memory=%s skills=%s",
                session_id,
                review_memory,
                review_skills,
            )
            with open(os.devnull, "w") as devnull, contextlib.redirect_stdout(devnull), contextlib.redirect_stderr(devnull):
                review_agent = _review_agent_factory(parent)(
                    model=parent.model,
                    api_key=parent.api_key,
                    base_url=parent.base_url,
                    api_mode=parent.api_mode,
                    provider=parent.provider,
                    session_db=None,
                    session_id=getattr(parent, "session_id", None),
                    system_prompt=_parent_system_prompt(parent, messages_snapshot),
                    enabled_toolsets=parent.enabled_toolsets,
                    config=getattr(parent, "config", None),
                    skip_context_files=True,
                )
                if hasattr(parent, "session_start"):
                    review_agent.session_start = parent.session_start
                review_agent._turns_since_memory_review = 0
                review_agent._memory_review_round = 0
                review_agent._turns_since_evolution_review = 0
                review_agent._evolution_review_round = 0
                review_agent._execution_actor = "background_review"
                review_agent._memory_store = getattr(parent, "_memory_store", None)
                review_agent._memory_manager = getattr(parent, "_memory_manager", None)
                _restrict_review_agent_tools(review_agent)

                with tool_dispatch_policy(
                    tool_whitelist=REVIEW_TOOL_WHITELIST,
                    action_whitelist={"skill_manage": REVIEW_SKILL_ACTION_WHITELIST},
                ):
                    review_agent.run_conversation(
                        user_message=prompt,
                        conversation_history=messages_snapshot,
                    )

            if review_skills and review_agent is not None:
                skill_summary = summarize_background_review_actions(
                    review_agent.messages,
                    prior_snapshot=messages_snapshot,
                )
                if skill_summary:
                    logger.info("Skill review saved: %s", skill_summary["summary"])
                    display_text = skill_summary["text"]
                    parent._emit_event(
                        {
                            "type": "skills_saved",
                            "summary": skill_summary["summary"],
                            "text": display_text,
                            "skills": skill_summary["skills"],
                        }
                    )
                    print_fn = getattr(parent, "_print_fn", None)
                    if print_fn:
                        try:
                            print_fn(f"  {display_text}")
                        except Exception:
                            pass
                else:
                    logger.info("Background skill review completed: nothing to save")
            logger.info(
                "Background review completed: session=%s memory=%s skills=%s",
                session_id,
                review_memory,
                review_skills,
            )
        except Exception as exc:
            logger.warning("Background review failed: session=%s error=%s", session_id, exc, exc_info=True)
        finally:
            if review_memory:
                try:
                    parent._refresh_memory_snapshot()
                    logger.info("Background memory review refreshed parent memory snapshot")
                except Exception as refresh_exc:
                    logger.debug("Background memory review snapshot refresh failed: %s", refresh_exc)

    threading.Thread(target=_run_review, daemon=True).start()
