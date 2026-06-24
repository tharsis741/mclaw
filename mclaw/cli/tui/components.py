"""Shared M-Claw TUI building blocks."""

from __future__ import annotations

import shutil
import re
from collections.abc import Callable, Iterable, Sequence

from rich.console import Group
from rich.markup import escape
from rich.panel import Panel
from rich.protocol import is_renderable
from rich.table import Table
from rich.text import Text as RichText

from mclaw.cli.tui.console import MClawConsole
from mclaw.cli.tui.theme import (
    ACCENT_COLOR,
    ACCENT_DIM,
    ACCENT_LIGHT,
    BANNER_TEXT_COLOR,
    COMMAND_ARG_COLOR,
    COMMAND_COLOR,
    COMMAND_EXAMPLE_COLOR,
    DANGER,
    INFO,
    MUTED,
    SUCCESS,
    VALUE_COLOR,
    WARNING,
    select_box,
)


def terminal_panel_width(*, fallback: int = 96, min_width: int = 64, max_width: int = 104) -> int:
    columns = shutil.get_terminal_size((fallback, 24)).columns
    return min(max(columns - 2, min_width), max_width)


def render_panel(
    *,
    printer: Callable[[str], None] | None = None,
    title: str,
    items: Sequence,
    border_style: str = ACCENT_COLOR,
    box=None,
    run_external_output: Callable[[Callable[[], None]], None] | None = None,
    width: int | None = None,
) -> None:
    """Render a branded M-Claw panel through the prompt_toolkit-safe printer."""

    def _print_panel() -> None:
        MClawConsole(printer).print(
            build_panel(
                title=title,
                items=items,
                border_style=border_style,
                box=box,
                width=width,
            )
        )

    if run_external_output is not None:
        run_external_output(_print_panel)
    else:
        _print_panel()


def build_panel(
    *,
    title,
    items: Sequence,
    border_style: str = ACCENT_COLOR,
    box=None,
    width: int | None = None,
    title_align: str = "center",
    padding: tuple[int, int] = (1, 2),
) -> Panel:
    return Panel(
        Group(*items),
        title=title if not isinstance(title, str) else f"[bold {ACCENT_LIGHT}] {title} [/]",
        title_align=title_align,
        border_style=border_style,
        box=box or select_box(),
        padding=padding,
        width=width or terminal_panel_width(),
        expand=False,
    )


def key_value_table(rows: Iterable[tuple[str, object]], *, key_style: str = ACCENT_COLOR) -> Table:
    table = Table.grid(padding=(0, 2), pad_edge=False)
    table.add_column(style=f"bold {key_style}", no_wrap=True)
    table.add_column(style=VALUE_COLOR, overflow="fold")
    for key, value in rows:
        table.add_row(_safe_cell(key), "" if value is None else _safe_cell(value))
    return table


def command_table(rows: Iterable[tuple[str, object]]) -> Table:
    table = Table.grid(padding=(0, 3), pad_edge=False)
    table.add_column(no_wrap=True)
    table.add_column(style=MUTED, overflow="fold")
    for command, description in rows:
        table.add_row(_command_cell(command), "" if description is None else _description_cell(description))
    return table


def data_table(columns: Sequence[tuple[str, dict]], rows: Iterable[Sequence[object]]) -> Table:
    table = Table(box=None, show_header=True, header_style=f"bold {ACCENT_LIGHT}", padding=(0, 2))
    for name, options in columns:
        table.add_column(name, **options)
    for row in rows:
        table.add_row(*["" if value is None else _safe_cell(value) for value in row])
    return table


def section_title(text: str) -> RichText:
    return RichText(text, style=f"bold {ACCENT_LIGHT}")


def muted_text(text: str) -> RichText:
    return RichText(text, style=ACCENT_DIM)


def body_text(text: str) -> RichText:
    return RichText(text, style=VALUE_COLOR)


def vertical_stack(*items):
    return Group(*items)


def status_label(state: str, labels: dict[str, tuple[str, str]] | None = None) -> RichText:
    palette = labels or {
        "success": ("成功", SUCCESS),
        "warning": ("注意", WARNING),
        "danger": ("错误", DANGER),
        "info": ("信息", INFO),
        "muted": ("普通", MUTED),
    }
    label, style = palette.get(str(state), (str(state), MUTED))
    return RichText(label, style=f"bold {style}")


def _safe_cell(value):
    if isinstance(value, str):
        return escape(value)
    if is_renderable(value):
        return value
    return escape(str(value))


def _command_cell(value):
    if not isinstance(value, str):
        return _safe_cell(value)
    text = value
    stripped = text.strip()
    if stripped.startswith("示例：") or stripped.startswith("示例:"):
        return RichText(text, style=COMMAND_EXAMPLE_COLOR)
    if stripped.startswith("/"):
        return _highlight_command(text)
    return RichText(text, style=MUTED)


def _description_cell(value):
    if not isinstance(value, str):
        return _safe_cell(value)
    if not value:
        return ""
    return RichText(value, style=MUTED)


_COMMAND_TOKEN_RE = re.compile(r"(<[^>]+>|--[A-Za-z0-9_-]+|/[A-Za-z0-9_-]+)")


def _highlight_command(command: str) -> RichText:
    if "<" not in command and "--" not in command:
        return RichText(command, style=f"bold {COMMAND_COLOR}")

    rich = RichText()
    pos = 0
    for match in _COMMAND_TOKEN_RE.finditer(command):
        if match.start() > pos:
            rich.append(command[pos:match.start()], style=VALUE_COLOR)
        token = match.group(0)
        if token.startswith("/"):
            rich.append(token, style=f"bold {COMMAND_COLOR}")
        elif token.startswith("--"):
            rich.append(token, style=f"bold {ACCENT_LIGHT}")
        else:
            rich.append(token, style=f"bold {COMMAND_ARG_COLOR}")
        pos = match.end()
    if pos < len(command):
        rich.append(command[pos:], style=VALUE_COLOR)
    return rich
