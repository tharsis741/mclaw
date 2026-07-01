# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Composer helpers for the classic prompt_toolkit TUI."""

from __future__ import annotations

from dataclasses import dataclass


COMPLETION_TRAY_HEIGHT = 4
FOLDED_PASTE_LINE_THRESHOLD = 5


def normalize_paste_text(text: str) -> str:
    """Normalize terminal paste line endings before inserting into the buffer."""
    return str(text or "").replace("\r\n", "\n").replace("\r", "\n")


def count_logical_lines(text: str) -> int:
    """Count real pasted lines, independent of terminal wrapping."""
    if not text:
        return 0
    return text.count("\n") + 1


def folded_paste_placeholder(line_count: int) -> str:
    """Create the visible token that stands in for a multi-line paste."""
    return f"[输入行数过多，已折叠{line_count}行!]"


@dataclass(frozen=True)
class FoldedPasteSegment:
    """Remember one folded paste placeholder and the text it hides."""

    placeholder: str
    text: str


class FoldedPasteStore:
    """Track folded paste placeholders and restore their original text on submit.

    Editing inside a folded placeholder is intentionally unsupported. If the
    placeholder text is changed or removed, it will no longer expand.
    """

    def __init__(self, *, threshold: int = FOLDED_PASTE_LINE_THRESHOLD):
        self.threshold = threshold
        self._segments: list[FoldedPasteSegment] = []

    def fold_for_display(self, text: str) -> str:
        normalized = normalize_paste_text(text)
        line_count = count_logical_lines(normalized)
        if line_count <= self.threshold:
            return normalized

        placeholder = folded_paste_placeholder(line_count)
        self._segments.append(FoldedPasteSegment(placeholder=placeholder, text=normalized))
        return placeholder

    def expand(self, display_text: str) -> str:
        """Restore unchanged placeholders before the composer submits text."""
        expanded = str(display_text or "")
        for segment in self._segments:
            if segment.placeholder in expanded:
                expanded = expanded.replace(segment.placeholder, segment.text, 1)
        return expanded

    def clear(self) -> None:
        self._segments.clear()


@dataclass
class HistoryNavigationState:
    """Track whether non-empty composer text is still a history preview."""

    active: bool = False
    suppress_text_changed: bool = False

    def deactivate(self) -> None:
        self.active = False

    def on_text_changed(self, _buffer=None) -> None:
        if not self.suppress_text_changed:
            self.deactivate()

    def history_backward(self, buffer, *, count: int = 1) -> None:
        self._navigate_history(buffer, lambda: buffer.history_backward(count=count))

    def history_forward(self, buffer, *, count: int = 1) -> None:
        self._navigate_history(buffer, lambda: buffer.history_forward(count=count))

    def _navigate_history(self, buffer, action) -> None:
        """Run prompt_toolkit history movement without treating the preview as an edit."""
        before = (getattr(buffer, "working_index", None), getattr(buffer, "text", ""))
        self.suppress_text_changed = True
        try:
            action()
        finally:
            self.suppress_text_changed = False
        after = (getattr(buffer, "working_index", None), getattr(buffer, "text", ""))
        self.active = bool(after != before or getattr(buffer, "text", ""))


def clear_composer_buffer(
    buffer,
    paste_store: FoldedPasteStore | None = None,
    history_state: HistoryNavigationState | None = None,
) -> None:
    """Clear all visible and folded composer input."""
    buffer.reset()
    if paste_store is not None:
        paste_store.clear()
    if history_state is not None:
        history_state.deactivate()


def move_cursor_or_history_up(
    buffer,
    *,
    count: int = 1,
    history_state: HistoryNavigationState | None = None,
) -> None:
    """Move in edited text; continue history navigation while previewing history."""
    if history_state is not None and history_state.active:
        history_state.history_backward(buffer, count=count)
        return
    if getattr(buffer, "text", ""):
        if buffer.document.cursor_position_row > 0:
            buffer.cursor_up(count=count)
        return
    if history_state is not None:
        history_state.history_backward(buffer, count=count)
    else:
        buffer.history_backward(count=count)


def move_cursor_or_history_down(
    buffer,
    *,
    count: int = 1,
    history_state: HistoryNavigationState | None = None,
) -> None:
    """Move in edited text; continue history navigation while previewing history."""
    if history_state is not None and history_state.active:
        history_state.history_forward(buffer, count=count)
        return
    if getattr(buffer, "text", ""):
        if buffer.document.cursor_position_row < buffer.document.line_count - 1:
            buffer.cursor_down(count=count)
        return
    if history_state is not None:
        history_state.history_forward(buffer, count=count)
    else:
        buffer.history_forward(count=count)
