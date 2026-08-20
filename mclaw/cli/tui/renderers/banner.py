# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Render the startup banner and enabled toolset summary for the CLI."""

from __future__ import annotations

import os
import shutil
from typing import Callable

from rich.panel import Panel
from rich.table import Table
from rich.text import Text as RichText

from mclaw.cli.tui.console import MClawConsole
from mclaw.cli.tui.theme import ACCENT_COLOR, ACCENT_DIM, ACCENT_LIGHT, BANNER_TEXT_COLOR, TUI_BRAND_TITLE


class BannerRenderer:
    """Build the branded startup panel without leaking Rich layout details upstream."""

    def __init__(
        self,
        *,
        box_factory: Callable,
        logo: str,
    ):
        self._box_factory = box_factory
        self._logo = logo

    def _enabled_toolset_rows(self, agent) -> list[tuple[str, str, str, list[str]]]:
        """Return enabled tools grouped by display toolset for the startup banner."""
        from mclaw.tools.registry import registry
        from mclaw.tools.toolsets import TOOLSETS

        if not agent or not getattr(agent, "valid_tool_names", None):
            return []

        grouped: dict[str, list[str]] = {}
        for name in sorted(agent.valid_tool_names):
            toolset = registry.get_toolset_for_tool(name) or "other"
            grouped.setdefault(toolset, []).append(name)

        ordered_toolsets = [name for name in TOOLSETS if name in grouped and TOOLSETS[name].get("kind") != "preset"]
        ordered_toolsets.extend(sorted(name for name in grouped if name not in TOOLSETS))

        rows: list[tuple[str, str, str, list[str]]] = []
        for toolset in ordered_toolsets:
            raw_tools = grouped[toolset]
            meta = TOOLSETS.get(toolset, {})
            # Keep declared tool ordering for known toolsets, then append registry-only tools.
            declared_tools = [
                name for name in meta.get("tools", [])
                if name in raw_tools
            ]
            extra_tools = sorted(name for name in raw_tools if name not in declared_tools)
            tools = declared_tools + extra_tools
            display = meta.get("display", {}) if isinstance(meta.get("display"), dict) else {}
            summary = str(display.get("summary_zh") or meta.get("description") or "").strip()
            emoji = str(display.get("emoji") or "").strip()
            rows.append((toolset, summary, emoji, tools))
        return rows

    @staticmethod
    def _toolset_label(emoji: str, toolset: str) -> str:
        """Keep one visible gap after glyphs whose font ink spans the next cell."""

        if not emoji:
            return toolset
        # Kaihong M-Terminal renders U+2709 across the following cell even
        # without an emoji variation selector, hiding the normal one-cell gap.
        gap = "  " if emoji == "✉" else " "
        return f"{emoji}{gap}{toolset}"

    def render(self, *, model: str, provider: str, session_id: str, agent) -> None:
        """Print startup context for the active model, session, workspace, and tools."""
        term_w = shutil.get_terminal_size().columns
        cc = MClawConsole()
        model_short = model.split("/")[-1] if "/" in model else model
        cwd = os.environ.get("TERMINAL_CWD") or os.getcwd()
        sid_short = session_id[:12] if len(session_id) > 12 else session_id

        if term_w >= 60:
            logo_lines = [
                line for line in self._logo.strip("\n").split("\n")
            ]

            logo_table = Table.grid(padding=(0, 1), pad_edge=False)
            logo_table.add_column("logo")

            for logo_line in logo_lines:
                logo_table.add_row(RichText.from_markup(logo_line))

            cc.print(logo_table)
            cc.print(f"  [{ACCENT_LIGHT} bold]自进化空间智能体[/]")
            cc.print(f"  [{ACCENT_DIM}]Self-Evolving Robot Intelligence[/]")
            cc.print("")

        content = Table.grid(pad_edge=False)
        content.add_column("content")

        layout = Table.grid(padding=(0, 3), pad_edge=False)
        layout.add_column("left", width=min(34, term_w // 2 - 2))
        layout.add_column("right")

        info_table = Table(box=None, show_header=False, padding=(0, 1))
        info_table.add_column("label", style=f"bold {ACCENT_COLOR}")
        info_table.add_column("value", style=BANNER_TEXT_COLOR)
        info_table.add_row("会话", sid_short)
        info_table.add_row("模型", model_short)
        info_table.add_row("供应商", provider or "自动")
        info_table.add_row("目录", cwd)

        tool_table = Table(box=None, show_header=False, padding=(0, 1))
        tool_table.add_column("name", style=f"bold {ACCENT_LIGHT}", width=20)
        tool_table.add_column("desc", style=ACCENT_DIM)

        toolset_rows = self._enabled_toolset_rows(agent)
        if toolset_rows:
            def _clean_desc(d: str) -> str:
                for sep in ("（", "("):
                    if sep in d:
                        d = d.split(sep)[0]
                d = d.strip()
                if len(d) > 24:
                    d = d[:22] + "..."
                return d

            for toolset, desc, emoji, tools in toolset_rows:
                label = self._toolset_label(emoji, toolset)
                if len(label) > 20:
                    label = label[:17] + "..."
                detail = _clean_desc(desc) or f"{len(tools)} tools"
                tool_table.add_row(label, detail)
        else:
            tool_table.add_row("—", "暂无工具")

        layout.add_row(info_table, tool_table)
        content.add_row(layout)

        panel_width = min(max(term_w - 2, 64), 96)
        help_text = RichText(
            "/help 查看命令 · Ctrl+D 退出 · Ctrl+U 清空输入框 · "
            "Ctrl+C 终止任务 · F8 切换语音模式",
            style="dim",
        )
        content.add_row("")
        content.add_row(help_text)

        cc.print(Panel(
            content,
            border_style=ACCENT_COLOR,
            box=self._box_factory(),
            padding=(1, 2),
            width=panel_width,
            title=f"[bold {ACCENT_LIGHT}] {TUI_BRAND_TITLE} [/]",
            title_align="center",
        ))
