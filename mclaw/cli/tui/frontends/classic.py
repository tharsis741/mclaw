# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""prompt_toolkit frontend helpers for the classic TUI."""

from __future__ import annotations

from collections.abc import Callable

from prompt_toolkit.application.current import get_app
from prompt_toolkit.layout import HSplit, Window
from prompt_toolkit.layout.containers import Container
from prompt_toolkit.layout.controls import FormattedTextControl, UIContent, UIControl
from prompt_toolkit.utils import get_cwidth

from mclaw.cli.slash_completer import slash_token_before_cursor


def _trim_to_width(text: str, width: int) -> str:
    """Trim display text by terminal cell width rather than Python character count."""
    if width <= 0:
        return ""
    result: list[str] = []
    used = 0
    for char in text:
        char_width = get_cwidth(char)
        if used + char_width > width:
            break
        result.append(char)
        used += char_width
    return "".join(result)


def _pad_to_width(text: str, width: int) -> str:
    """Right-pad text to a terminal cell width for prompt_toolkit fragments."""
    return text + " " * max(0, width - get_cwidth(text))


class SlashCompletionMenuControl(UIControl):
    """Render slash completions without prompt_toolkit's input preview."""

    def __init__(self, selected_index: Callable[[], int], *, max_height: int = 18):
        self._selected_index = selected_index
        self._max_height = max_height

    def is_focusable(self) -> bool:
        return False

    def _visible_items(self):
        """Return the scroll window for completions only while editing a slash token."""
        buffer = get_app().current_buffer
        state = buffer.complete_state
        if not state or not state.completions or slash_token_before_cursor(buffer.document) is None:
            return [], 0, 0, 0

        completions = state.completions
        selected = max(0, min(self._selected_index(), len(completions) - 1))
        start = min(max(0, selected - 1), max(0, len(completions) - self._max_height))
        visible = completions[start:start + self._max_height]
        return visible, selected, start, len(completions)

    def preferred_width(self, max_available_width: int) -> int | None:
        """Reserve enough space for command and metadata columns without overlaying input."""
        visible, _selected, _start, _total = self._visible_items()
        if not visible:
            return min(max_available_width, 1)
        command_width = self._command_width(visible, max_available_width)
        meta_width = self._meta_width(command_width, max_available_width)
        return min(max_available_width, command_width + meta_width)

    def preferred_height(self, width: int, max_available_height: int, wrap_lines: bool, get_line_prefix) -> int | None:
        return min(max_available_height, self._max_height)

    def create_content(self, width: int, height: int) -> UIContent:
        """Render completion rows as fixed-width command and metadata cells."""
        visible, selected, start, _total = self._visible_items()
        command_width = self._command_width(visible, width)
        meta_width = self._meta_width(command_width, width)

        def get_line(line_number: int):
            if line_number >= len(visible):
                return [("", " ")]
            completion = visible[line_number]
            index = start + line_number
            is_current = index == selected
            cmd_style = "class:completion-menu.completion.current" if is_current else "class:completion-menu.completion"
            meta_style = "class:completion-menu.meta.current" if is_current else "class:completion-menu.meta"
            command_text = _pad_to_width(_trim_to_width(completion.display_text, command_width), command_width)
            meta_text = _pad_to_width(_trim_to_width(completion.display_meta_text, meta_width), meta_width)
            return [(cmd_style, command_text), (meta_style, meta_text)]

        return UIContent(
            get_line=get_line,
            line_count=min(height, self._max_height),
            show_cursor=False,
        )

    @staticmethod
    def _command_width(completions, max_width: int) -> int:
        if not completions:
            return 0
        natural = max(get_cwidth(completion.display_text) for completion in completions) + 2
        return min(max(18, natural), max(18, min(30, max_width // 3)))

    @staticmethod
    def _meta_width(command_width: int, max_width: int) -> int:
        return max(0, min(80, max_width - command_width))


def build_classic_root_container(
    *,
    status_fragments: Callable,
    status_height: Callable[[], int],
    input_area: Container,
    completion_selected_index: Callable[[], int],
) -> Container:
    """Build the classic scrollback layout.

    Completion UI is a fixed four-row tray below the composer. The reserved
    tray keeps the composer position stable while avoiding Float overlays that
    can cover the input in classic non-fullscreen mode.

    Classic keeps terminal scrollback as the transcript. A placeholder body would
    redraw over Rich-rendered answers on every prompt_toolkit refresh.
    """

    status_bar = Window(
        content=FormattedTextControl(status_fragments),
        height=status_height,
        wrap_lines=False,
    )
    completion_menu = Window(
        content=SlashCompletionMenuControl(completion_selected_index, max_height=4),
        height=4,
        char=" ",
        dont_extend_width=True,
        dont_extend_height=True,
        always_hide_cursor=True,
    )
    return HSplit([
        status_bar,
        input_area,
        completion_menu,
    ])
