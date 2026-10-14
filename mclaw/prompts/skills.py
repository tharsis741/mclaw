# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Skill runtime prompt builders."""

from __future__ import annotations

import json
import re
from typing import Any


SKILL_AUTHORING_STORAGE_GUIDANCE = (
    "SKILL.md 只保存 frontmatter 和可复用流程正文。"
    "skill_view 的 content 是正文，mclaw_skill 和 skill_evolution 是独立字段；"
    "创建者、更新时间和状态由运行时维护，不要复制到正文。"
    "经验通过 initial_evolution 或 evolution_update 保存。"
)


def build_skill_runtime_content(view: dict[str, Any]) -> str:
    """Include saved experience for invocation without copying audit metadata."""
    content = str(view.get("content") or "")
    evolution = view.get("skill_evolution") or {}
    if any(evolution.values()):
        content += (
            "\n\n[运行时提供的 Skill 经验，独立于 SKILL.md]\n"
            + json.dumps(evolution, ensure_ascii=False, indent=2)
            + "\n[Skill 经验结束]"
        )
    return content


def strip_skill_invocations(content: str, *, name: str | None = None) -> str:
    """Remove task-scoped Skill blocks, including legacy blocks in the base prompt."""
    text = content.strip()
    header = re.match(r"^\[Skill: ([^\]\r\n]+)\]\r?\n", text)
    if header and text.endswith("[End Skill]"):
        # Skill examples can themselves contain delimiter text. A standalone
        # invocation is one envelope, not a sequence of inner instructions.
        return "" if name is None or header.group(1) == name else text
    if name is not None:
        # Legacy blocks were appended together to the base prompt. Clear that
        # entire suffix only when projecting a new task, not one inner example.
        return text
    pattern = r"(?ms)^\[Skill: [^\]\r\n]+\]\r?\n.*^\[End Skill\](?:\r?\n|$)"
    return re.sub(pattern, "", content).strip()


def prepare_skill_contexts_for_turn(messages: list[dict]) -> tuple[list[dict], list[dict]]:
    """Expire historical selections and consume fresh legacy slash selections.

    Queue hosts restore the active task from immutable snapshots at the model
    boundary. Hosts without a queue can explicitly select a Skill for one turn.
    """
    history, pending = [], []
    for message in messages:
        content = message.get("content")
        if message.get("role") == "system" and isinstance(content, str):
            if message.get("_mclaw_pending_skill"):
                pending.append({key: value for key, value in message.items() if key != "_mclaw_pending_skill"})
                continue
            stripped = strip_skill_invocations(content)
            if stripped != content.strip():
                if stripped:
                    history.append({**message, "content": stripped})
                continue
        history.append(message)
    return history, pending


def build_skill_invocation_system(
    skill_name: str,
    skill_content: str,
    user_intent: str = "",
) -> str:
    """Build the model-only context block injected for slash-invoked Skills.

    This prompt carries the selected Skill instructions into the current turn
    without mutating Skill metadata or the user's installed Skill files.
    """
    intent = str(user_intent or "").strip()
    intent_block = f"\nUser Intent:\n{intent}\n" if intent else ""
    return (
        f"[Skill: {skill_name}]\n\n"
        "用户选择此 Skill 处理当前任务；后续用户补充或纠正优先。\n"
        f"{intent_block}\n"
        "Skill Instructions:\n"
        f"{skill_content}\n\n"
        "[End Skill]"
    )


def build_skill_import_confirmation_context(
    action: str,
    confirmation: dict[str, Any],
    result: dict[str, Any],
) -> str:
    """Build runtime context that summarizes a completed Skill install decision.

    The returned text is conversation state for the next model turn, so it
    includes audit and security-review pointers while avoiding raw artifact
    contents.
    """

    def _format_source(value: Any) -> str:
        """Collapse structured source metadata into a compact audit label."""
        if isinstance(value, dict):
            source_type = str(value.get("type") or "").strip()
            original = str(value.get("original") or "").strip()
            if source_type and original:
                return f"{source_type}: {original}"
            return source_type or original
        return str(value or "").strip()

    skill_name = (
        result.get("name")
        or confirmation.get("skill_name")
        or confirmation.get("name")
        or "(unknown)"
    )
    drafting_id = confirmation.get("drafting_id") or ""
    path = str(result.get("path") or "").strip()
    error = str(result.get("error") or "").strip()
    source = _format_source(result.get("source") or confirmation.get("source"))
    user_intent = str(result.get("user_intent") or confirmation.get("user_intent") or "").strip()
    risk_level = str(result.get("risk_level") or confirmation.get("risk_level") or "").strip()
    verdict = str(result.get("verdict") or confirmation.get("verdict") or "").strip()
    security_review_path = str(
        result.get("security_review_path")
        or confirmation.get("security_review_path")
        or ""
    ).strip()
    audit_path = str(result.get("audit_path") or confirmation.get("audit_path") or "").strip()

    lines = [
        "[M-Claw runtime: skill_import_confirmation]",
        "Skill 安装确认流程已结束。",
        f"- action: {action}",
        f"- skill: {skill_name}",
    ]
    if drafting_id:
        lines.append(f"- drafting_id: {drafting_id}")
    if source:
        lines.append(f"- source: {source}")
    if user_intent:
        lines.append(f"- user_intent: {user_intent}")
    if risk_level:
        lines.append(f"- risk_level: {risk_level}")
    if verdict:
        lines.append(f"- verdict: {verdict}")
    if security_review_path:
        lines.append(f"- security_review_path: {security_review_path}")
    if audit_path:
        lines.append(f"- audit_path: {audit_path}")

    if action == "enable_drafting" and result.get("success"):
        lines.append("- result: 用户已确认启用，Skill 已安装完成。")
        if path:
            lines.append(f"- path: {path}")
        lines.append("后续对话按该 Skill 已启用处理。")
    elif action == "cancel_drafting" and result.get("success"):
        lines.append("- result: 用户已取消启用，Skill 草稿已清理。")
        lines.append("后续对话按该安装请求已取消处理。")
    else:
        lines.append("- result: Skill 安装确认处理失败。")
        if error:
            lines.append(f"- error: {error}")
        lines.append("后续对话按该安装请求未完成处理。")

    return "\n".join(lines)
