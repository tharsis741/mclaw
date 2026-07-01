# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Classic TUI adapter for UI-neutral panel models.

The runtime builds panel models without depending on Rich. This adapter is the
single place that maps those model blocks and semantic cell roles into classic
terminal renderables.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from rich.markdown import Markdown as RichMarkdown
from rich.text import Text as RichText

from mclaw.cli.runtime.panels import PanelBlock, PanelCell, PanelModel
from mclaw.cli.tui.components import (
    body_text,
    command_table,
    data_table,
    key_value_table,
    muted_text,
    render_panel,
    section_title,
)
from mclaw.cli.tui.theme import ACCENT_DIM, ACCENT_LIGHT, BANNER_TEXT_COLOR, DANGER, INFO, SUCCESS, VALUE_COLOR, WARNING

logger = logging.getLogger(__name__)


_TONE_BORDER = {
    "success": SUCCESS,
    "warning": WARNING,
    "danger": DANGER,
    "info": INFO,
}


def render_panel_model(
    panel: PanelModel,
    *,
    printer: Callable[[str], None] | None = None,
    run_external_output: Callable[[Callable[[], None]], None] | None = None,
    box=None,
    border_style: str | None = None,
) -> None:
    """Render a UI-neutral panel model through the classic Rich TUI pipeline."""
    render_panel(
        printer=printer,
        title=panel.title,
        items=tuple(_render_block(block) for block in panel.blocks),
        border_style=border_style or _TONE_BORDER.get(panel.tone, INFO),
        box=box,
        run_external_output=run_external_output,
    )


def _render_block(block: PanelBlock):
    """Map a panel block kind to the matching Rich renderable."""
    if block.kind == "spacer":
        return ""
    if block.kind == "section":
        return section_title(block.text)
    if block.kind == "key_value":
        return key_value_table(list(block.rows))
    if block.kind == "commands":
        return command_table(list(block.rows))
    if block.kind == "table":
        return data_table(
            [_column_options(column) for column in block.columns],
            [tuple(_render_cell(cell) for cell in row) for row in block.table_rows],
        )
    if block.kind == "text":
        return _render_text_block(block)
    return body_text(block.text)


def _column_options(column):
    """Translate model column options to Rich Table.add_column kwargs."""
    options = {
        "style": _column_style(column.role),
        "overflow": column.overflow,
    }
    if column.no_wrap:
        options["no_wrap"] = True
    if column.justify and column.justify != "left":
        options["justify"] = column.justify
    if column.width is not None:
        options["width"] = column.width
    return (column.label, options)


def _column_style(role: str) -> str:
    return {
        "primary": BANNER_TEXT_COLOR,
        "muted": ACCENT_DIM,
        "accent": ACCENT_LIGHT,
        "default": BANNER_TEXT_COLOR,
    }.get(role, BANNER_TEXT_COLOR)


def _render_cell(cell: PanelCell):
    """Map semantic cell roles to classic TUI text styling."""
    style = {
        "primary": BANNER_TEXT_COLOR,
        "muted": ACCENT_DIM,
        "accent": ACCENT_LIGHT,
        "success": SUCCESS,
        "warning": WARNING,
        "danger": DANGER,
        "info": INFO,
        "default": VALUE_COLOR,
    }.get(cell.role, VALUE_COLOR)
    return RichText(cell.text, style=f"bold {style}" if cell.role in {"success", "warning", "danger", "accent"} else style)


def _render_text_block(block: PanelBlock):
    """Render text blocks with markdown/ANSI parsing and plain-text fallback."""
    if block.text_format == "markdown":
        try:
            return RichMarkdown(block.text)
        except Exception as exc:
            logger.debug("Markdown panel block render failed: %s", exc)
            return body_text(block.text)
    if block.text_format == "ansi":
        try:
            return RichText.from_ansi(block.text)
        except Exception as exc:
            logger.debug("ANSI panel block render failed: %s", exc)
            return body_text(block.text)
    if block.muted:
        return muted_text(block.text)
    return body_text(block.text)
