"""Skill runtime prompt builders."""

from __future__ import annotations

from typing import Any


def build_skill_invocation_system(
    skill_name: str,
    skill_content: str,
    user_intent: str = "",
) -> str:
    """Build the system-context block injected for slash-invoked Skills."""
    intent = str(user_intent or "").strip()
    intent_block = f"\nUser Intent:\n{intent}\n" if intent else ""
    return (
        f"[Skill: {skill_name}]\n\n"
        "用户选择此 Skill 处理当前任务。\n"
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
    """Build the runtime context recorded after a Skill install confirmation."""

    def _format_source(value: Any) -> str:
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
    drafting_id = confirmation.get("drafting_id") or confirmation.get("staging_id") or ""
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
