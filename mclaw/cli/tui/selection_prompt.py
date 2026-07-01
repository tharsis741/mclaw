# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Reusable prompt_toolkit selection prompts for setup and trust gates."""

from __future__ import annotations

from prompt_toolkit import Application
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style

from mclaw.cli.tui.assets import MCLAW_LOGO
from mclaw.cli.tui.console import MClawConsole


def prompt_workspace_risk_confirmation(workspace: str) -> bool:
    """Ask whether M-Claw may access the current workspace before startup continues."""
    selected = {"index": 0}

    console = MClawConsole()
    console.print(MCLAW_LOGO)

    def fragments() -> StyleAndTextTuples:
        items = [
            "1. 允许并记住当前工作区",
            "2. 拒绝并退出",
        ]
        result: StyleAndTextTuples = [
            ("class:title", "M-Claw 风险确认\n"),
            ("", "M-Claw 可能读取、修改或执行当前工作区内容。请选择：\n\n"),
        ]
        for index, label in enumerate(items):
            if index == selected["index"]:
                result.append(("class:selected", f"> {label}\n"))
            else:
                result.append(("", f"  {label}\n"))
        result.extend([
            ("", "\n"),
            ("class:hint", "↑/↓ 切换，Enter 确认，Esc 拒绝\n"),
        ])
        return result

    control = FormattedTextControl(fragments, focusable=True)
    kb = KeyBindings()

    @kb.add("up")
    def _(event):
        selected["index"] = (selected["index"] - 1) % 2
        event.app.invalidate()

    @kb.add("down")
    def _(event):
        selected["index"] = (selected["index"] + 1) % 2
        event.app.invalidate()

    @kb.add("enter")
    def _(event):
        event.app.exit(result=selected["index"] == 0)

    @kb.add("y")
    def _(event):
        event.app.exit(result=True)

    @kb.add("n")
    @kb.add("escape")
    @kb.add("c-c")
    def _(event):
        event.app.exit(result=False)

    app = Application(
        layout=Layout(HSplit([Window(control, wrap_lines=True)]), focused_element=control),
        key_bindings=kb,
        style=Style.from_dict({
            "title": "bold #67e8f9",
            "selected": "bold #60a5fa",
            "hint": "#94a3b8",
        }),
        full_screen=False,
        mouse_support=False,
    )
    return bool(app.run())


def _compact_select_description(description: str, limit: int = 64) -> str:
    """Keep metadata descriptions narrow enough for non-fullscreen prompts."""
    text = " ".join(str(description or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _builtin_skill_description(description: str) -> str:
    return _compact_select_description(description)


def _multi_select_item_fragments(item: dict, *, checked: bool, current: bool) -> StyleAndTextTuples:
    """Build one prompt_toolkit row with separate style spans for cursor and checkbox state."""
    prefix = ">" if current else " "
    category = f" [{item['category']}]" if item["category"] else ""
    desc = f" - {item['description']}" if item["description"] else ""
    check = "x" if checked else " "
    name_style = "class:item-name-current" if current else "class:item-name"
    return [
        ("class:cursor", f"{prefix} "),
        ("class:checkbox-box", "["),
        ("class:checkbox-x" if checked else "class:checkbox-empty", check),
        ("class:checkbox-box", "] "),
        (name_style, item["label"]),
        ("class:item-category", category),
        ("class:item-description", desc),
        ("", "\n"),
    ]


def prompt_builtin_skill_selection(skills: list[dict]) -> list[str]:
    """Let the user choose setup-time built-in Skills with keyboard navigation."""
    return prompt_multi_select(
        "M-Claw 内置技能",
        [
            {
                "id": str(item.get("name") or "").strip(),
                "label": str(item.get("name") or "").strip(),
                "description": _builtin_skill_description(
                    str(item.get("short_description") or "").strip(),
                ),
                "category": str(item.get("category") or "").strip(),
            }
            for item in skills
            if str(item.get("name") or "").strip()
        ],
        hint="选择这次初始化要启用的内置技能。",
    )


def prompt_multi_select(
    title: str,
    items: list[dict],
    *,
    hint: str = "",
    default_selected: list[str] | None = None,
    max_visible_items: int = 20,
) -> list[str]:
    """Run a compact keyboard-driven multi-select prompt and return selected ids."""
    normalized = [
        {
            "id": str(item.get("id") or item.get("name") or "").strip(),
            "label": str(item.get("label") or item.get("name") or item.get("id") or "").strip(),
            "description": _compact_select_description(str(item.get("description") or "").strip()),
            "category": str(item.get("category") or "").strip(),
        }
        for item in items
        if str(item.get("id") or item.get("name") or "").strip()
    ]
    if not normalized:
        return []

    selected = {"index": 0, "ids": set(default_selected or [])}

    def fragments() -> StyleAndTextTuples:
        result: StyleAndTextTuples = [("class:title", f"{title}\n")]
        if hint:
            result.append(("", f"{hint}\n\n"))
        visible_count = max(1, min(max_visible_items, len(normalized)))
        if len(normalized) > visible_count:
            half = visible_count // 2
            # Keep the current row near the middle while clamping to the item bounds.
            start = max(0, min(selected["index"] - half, len(normalized) - visible_count))
            end = start + visible_count
            result.append(("class:hint", f"显示 {start + 1}-{end} / {len(normalized)}，↑/↓ 查看全部\n\n"))
        else:
            start = 0
            end = len(normalized)

        for index, item in enumerate(normalized[start:end], start):
            item_id = item["id"]
            result.extend(_multi_select_item_fragments(
                item,
                checked=item_id in selected["ids"],
                current=index == selected["index"],
            ))
        result.extend([
            ("", "\n"),
            ("class:hint", "↑/↓ 移动，Tab/Space 勾选，Enter 确认，Esc 跳过\n"),
        ])
        return result

    control = FormattedTextControl(fragments, focusable=True)
    kb = KeyBindings()

    @kb.add("up")
    def _(event):
        selected["index"] = (selected["index"] - 1) % len(normalized)
        event.app.invalidate()

    @kb.add("down")
    def _(event):
        selected["index"] = (selected["index"] + 1) % len(normalized)
        event.app.invalidate()

    @kb.add("tab")
    @kb.add("space")
    def _(event):
        item_id = normalized[selected["index"]]["id"]
        if item_id in selected["ids"]:
            selected["ids"].remove(item_id)
        else:
            selected["ids"].add(item_id)
        event.app.invalidate()

    @kb.add("enter")
    def _(event):
        event.app.exit(result=[item["id"] for item in normalized if item["id"] in selected["ids"]])

    @kb.add("escape")
    @kb.add("c-c")
    def _(event):
        event.app.exit(result=[])

    app = Application(
        layout=Layout(HSplit([Window(control, wrap_lines=False)]), focused_element=control),
        key_bindings=kb,
        style=Style.from_dict({
            "title": "bold #67e8f9",
            "selected": "bold #60a5fa",
            "cursor": "bold #60a5fa",
            "checkbox-box": "#f8fafc",
            "checkbox-empty": "#f8fafc",
            "checkbox-x": "bold #22c55e",
            "item-name": "bold #7dd3fc",
            "item-name-current": "bold #ffffff",
            "item-category": "#94a3b8",
            "item-description": "#cbd5e1",
            "hint": "#94a3b8",
        }),
        full_screen=False,
        mouse_support=False,
    )
    return list(app.run() or [])
