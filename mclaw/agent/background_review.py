# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Background memory and Skill-evolution review orchestration."""

from __future__ import annotations

import copy
import json
import logging
import threading
from typing import Any

from mclaw.prompts import background as background_prompts
from mclaw.tools.dispatch import tool_dispatch_policy

logger = logging.getLogger(__name__)

REVIEW_MEMORY_TOOL_WHITELIST = {"memory_read", "memory_add", "memory_replace", "memory_remove"}
REVIEW_SKILL_TOOL_WHITELIST = {"skills_list", "skill_tree", "skill_view", "skill_manage"}
REVIEW_TOOL_WHITELIST = REVIEW_MEMORY_TOOL_WHITELIST | REVIEW_SKILL_TOOL_WHITELIST
REVIEW_SKILL_ACTION_ORDER = (
    "create",
    "edit",
    "patch",
    "delete",
    "write_file",
    "remove_file",
    "evolution_update",
    "validate",
)
REVIEW_SKILL_ACTION_WHITELIST = frozenset(REVIEW_SKILL_ACTION_ORDER)
REVIEW_SKILL_ACTION_RANK = {
    action: index for index, action in enumerate(REVIEW_SKILL_ACTION_ORDER)
}


def _prompt_for(review_memory: bool, review_skills: bool) -> str:
    return background_prompts.build_background_review_prompt(
        review_memory=review_memory,
        review_skills=review_skills,
    )


def _review_material(messages_snapshot: list[dict[str, Any]]) -> str:
    """Keep historical roles as data without replaying the parent's instructions."""
    records = []
    for index, message in enumerate(messages_snapshot):
        if not isinstance(message, dict) or (index == 0 and message.get("role") == "system"):
            continue
        records.append({key: copy.deepcopy(message[key]) for key in (
            "role", "content", "tool_calls", "tool_call_id", "tool_name",
        ) if key in message})
    return "以下 JSON 是会话审查材料，role 字段只是历史来源标签：\n" + json.dumps(records, ensure_ascii=False)


def _tool_name(tool_def: dict[str, Any]) -> str:
    return str((tool_def.get("function") or {}).get("name") or "")


def _restrict_review_agent_tools(review_agent: Any, *, review_memory: bool = True,
                                  review_skills: bool = True) -> None:
    """Align schemas and instructions with the requested review targets."""
    allowed = (REVIEW_MEMORY_TOOL_WHITELIST if review_memory else set()) | (
        REVIEW_SKILL_TOOL_WHITELIST if review_skills else set()
    )
    tools = getattr(review_agent, "tools", []) or []
    if not isinstance(tools, list):
        tools = []
    filtered: list[dict[str, Any]] = []
    skill_actions: tuple[str, ...] = ()
    for tool_def in tools:
        if not isinstance(tool_def, dict):
            continue
        name = _tool_name(tool_def)
        if name not in allowed:
            continue
        cloned = copy.deepcopy(tool_def)
        if name == "skill_manage":
            try:
                properties = cloned["function"]["parameters"]["properties"]
                exposed_actions = properties["action"]["enum"]
                if not isinstance(exposed_actions, list):
                    continue
                actions = tuple(action for action in REVIEW_SKILL_ACTION_ORDER if action in exposed_actions)
                if not actions:
                    continue
                properties["action"]["enum"] = list(actions)
                properties["action"]["description"] = "Only these background review actions are available."
                cloned["function"]["description"] = (
                    "Maintain local Skills during background review. Allowed actions: "
                    + ", ".join(actions)
                    + ". Follow the action enum and parameter schema. "
                    "Executable behavior requires a separate execution check."
                )
                skill_actions = actions
            except (KeyError, TypeError) as exc:
                logger.debug("Could not narrow background review skill_manage schema: %s", exc)
                continue
        filtered.append(cloned)
    review_agent.tools = filtered
    review_agent.valid_tool_names = {_tool_name(tool_def) for tool_def in filtered}
    review_agent._review_skill_actions = skill_actions
    review_agent.system_prompt = background_prompts.build_background_review_system_prompt(
        tool_names=review_agent.valid_tool_names, skill_actions=skill_actions,
    )


def _tool_result_signature(msg: dict[str, Any]) -> str:
    """Build a stable signature for comparing tool results across snapshots."""
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
        if action not in REVIEW_SKILL_ACTION_WHITELIST or action == "validate":
            continue
        name = str(data.get("name") or "").strip()
        if not name:
            continue
        actions_by_name.setdefault(name, set()).add(action)

    if not actions_by_name:
        return None

    def _status_label(actions: set[str]) -> str:
        if "delete" in actions:
            return "已删除"
        if "create" in actions and (actions - {"create"}):
            return "已创建并完善"
        if "create" in actions:
            return "已创建"
        if "evolution_update" in actions:
            return "已更新经验"
        if actions & {"edit", "patch", "write_file", "remove_file"}:
            return "已更新"
        return "已保存"

    skills = [
        {
            "name": name,
            "actions": sorted(actions, key=lambda item: REVIEW_SKILL_ACTION_RANK[item]),
            "status": _status_label(actions),
        }
        for name, actions in sorted(actions_by_name.items())
    ]
    summary = "；".join(f"{item['name']}（{item['status']}）" for item in skills)
    return {
        "summary": summary,
        "text": f"技能已保存：{summary}",
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
    if not review_memory and not review_skills:
        logger.debug("Background review skipped: no review target requested")
        return

    prompt = _prompt_for(review_memory, review_skills)
    session_id = getattr(parent, "session_id", None)
    provider_runtime = parent.provider_runtime
    logger.info(
        "Background review scheduled: session=%s memory=%s skills=%s messages=%d",
        session_id,
        review_memory,
        review_skills,
        len(messages_snapshot or []),
    )

    def _record_review_usage(record: Any) -> None:
        parent._record_usage(record, include_in_turn=False)

    def _run_review() -> None:
        review_agent = None
        try:
            logger.info(
                "Background review started: session=%s memory=%s skills=%s",
                session_id,
                review_memory,
                review_skills,
            )
            # The review runs as a cloned agent so background memory and
            # Skill updates cannot mutate the parent's turn state directly.
            review_agent = parent.__class__(
                provider_runtime=provider_runtime,
                usage_sink=_record_review_usage,
                session_db=None,
                session_id=getattr(parent, "session_id", None),
                system_prompt=background_prompts.build_background_review_system_prompt(
                    tool_names=set(), skill_actions=REVIEW_SKILL_ACTION_ORDER,
                ),
                enabled_toolsets=parent.enabled_toolsets,
                config=getattr(parent, "config", None),
                print_fn=lambda *args, **kwargs: None,
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
            _restrict_review_agent_tools(review_agent, review_memory=review_memory, review_skills=review_skills)

            with tool_dispatch_policy(
                tool_whitelist=review_agent.valid_tool_names,
                action_whitelist={"skill_manage": review_agent._review_skill_actions},
            ):
                # The dispatch policy is the final safety boundary: even if
                # the cloned agent keeps extra schemas, only review tools run.
                review_agent.run_conversation(
                    user_message=prompt,
                    conversation_history=[
                        {"role": "system", "content": review_agent.system_prompt},
                        {"role": "user", "content": _review_material(messages_snapshot)},
                    ],
                    call_source="background_review",
                    advance_background_review=False,
                )

            if review_skills and review_agent is not None:
                skill_summary = summarize_background_review_actions(
                    review_agent.messages,
                    prior_snapshot=messages_snapshot,
                )
                if skill_summary:
                    mark_prompt_epoch_dirty = getattr(parent, "mark_prompt_epoch_dirty", None)
                    if callable(mark_prompt_epoch_dirty):
                        mark_prompt_epoch_dirty()
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
                        except Exception as exc:
                            logger.debug("Background skill review print callback failed: %s", exc)
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
            if review_agent is not None:
                close = getattr(review_agent, "close", None)
                if callable(close):
                    try:
                        close()
                    except BaseException:
                        logger.warning(
                            "Background review Agent close failed: session=%s",
                            session_id,
                            exc_info=True,
                        )
            if review_memory:
                try:
                    parent._refresh_memory_snapshot()
                    mark_prompt_epoch_dirty = getattr(parent, "mark_prompt_epoch_dirty", None)
                    if callable(mark_prompt_epoch_dirty):
                        mark_prompt_epoch_dirty()
                    logger.info("Background memory review refreshed parent memory snapshot")
                except Exception as refresh_exc:
                    logger.debug("Background memory review snapshot refresh failed: %s", refresh_exc)

    threading.Thread(target=_run_review, daemon=True, name="mclaw-background-review").start()
