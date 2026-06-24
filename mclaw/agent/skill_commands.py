"""CLI helpers for Skills slash commands."""

from __future__ import annotations

import json
import re
import textwrap
from typing import Any

from mclaw.tools.skill_tools.read_tools import skills_list


_SKILLS_DESC_WIDTH = 76


def _compact_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _description_lines(text: str, *, width: int = _SKILLS_DESC_WIDTH) -> list[str]:
    compact = _compact_text(text)
    if not compact:
        return ["暂无描述。"]
    wrapped = textwrap.wrap(
        compact,
        width=width,
        break_long_words=True,
        break_on_hyphens=False,
    )
    return wrapped or [compact]


def _format_skills_list_payload(payload: dict[str, Any]) -> str:
    skills = payload.get("skills") or []
    count = payload.get("count", len(skills))

    lines: list[str] = [f"已启用 Skills：{count} 个", ""]
    if not skills:
        lines.append("暂无可用技能。")
    else:
        for skill in sorted(skills, key=lambda item: str(item.get("name") or "")):
            name = str(skill.get("name") or "unknown").strip() or "unknown"
            lines.append(f"/{name}")
            for desc_line in _description_lines(
                str(skill.get("short_description") or skill.get("description") or "")
            ):
                lines.append(f"  {desc_line}")
            lines.append("")

    hint = _compact_text(str(payload.get("hint") or ""))
    if hint:
        if lines and lines[-1] != "":
            lines.append("")
        lines.append(hint)
        lines.append("使用 /skill install <source> 安装外部 Skill。")
        lines.append("使用 /skill creation <brief> 创建新 Skill。")

    return "\n".join(lines).rstrip()


def format_skills_response(raw_output: str) -> str:
    """Convert raw JSON tool output into readable multiline text."""
    text = (raw_output or "").strip()
    if not text:
        return "Skills 命令没有返回内容。"

    try:
        payload = json.loads(text)
    except Exception:
        return text

    if not isinstance(payload, dict):
        return json.dumps(payload, ensure_ascii=False, indent=2)

    if payload.get("error") or payload.get("success") is False:
        return f"错误: {payload.get('error') or payload}"

    if isinstance(payload.get("skills"), list):
        return _format_skills_list_payload(payload)

    return json.dumps(payload, ensure_ascii=False, indent=2)


def handle_skills_command(raw_args: str, *, format_output: bool = False) -> str:
    """Handle /skills and /skills list."""
    args = (raw_args or "").strip()
    if not args:
        raw = skills_list()
        return format_skills_response(raw) if format_output else raw

    parts = args.split(maxsplit=1)
    cmd = parts[0].lower()
    if cmd == "list":
        raw = skills_list()
        return format_skills_response(raw) if format_output else raw

    raw = json.dumps(
        {
            "success": False,
            "error": "Unknown /skills subcommand. Use: /skills or /skills list",
        },
        ensure_ascii=False,
    )
    return format_skills_response(raw) if format_output else raw
