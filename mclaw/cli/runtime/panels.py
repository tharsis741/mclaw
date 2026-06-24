# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""UI-neutral panel models for interactive command output."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


PanelTone = Literal["info", "success", "warning", "danger"]
PanelBlockKind = Literal["text", "section", "key_value", "table", "commands", "spacer"]
PanelTextFormat = Literal["plain", "markdown", "ansi"]
PanelColumnRole = Literal["primary", "muted", "accent", "default"]
PanelCellRole = Literal["primary", "muted", "accent", "success", "warning", "danger", "info", "default"]


@dataclass(frozen=True)
class PanelCell:
    text: str
    role: PanelCellRole = "default"


@dataclass(frozen=True)
class PanelColumn:
    label: str
    role: PanelColumnRole = "default"
    no_wrap: bool = False
    justify: str = "left"
    width: int | None = None
    overflow: str = "fold"


@dataclass(frozen=True)
class PanelBlock:
    kind: PanelBlockKind
    text: str = ""
    text_format: PanelTextFormat = "plain"
    rows: tuple[tuple[str, str], ...] = ()
    columns: tuple[PanelColumn, ...] = ()
    table_rows: tuple[tuple[PanelCell, ...], ...] = ()
    muted: bool = False


@dataclass(frozen=True)
class PanelModel:
    title: str
    blocks: tuple[PanelBlock, ...] = field(default_factory=tuple)
    tone: PanelTone = "info"
    namespace: str = "command"

    def with_block(self, block: PanelBlock) -> "PanelModel":
        return PanelModel(
            title=self.title,
            blocks=(*self.blocks, block),
            tone=self.tone,
            namespace=self.namespace,
        )


def text_block(text: object, *, muted: bool = False, text_format: PanelTextFormat = "plain") -> PanelBlock:
    return PanelBlock(
        kind="text",
        text="" if text is None else str(text),
        text_format=text_format,
        muted=muted,
    )


def markdown_block(text: object) -> PanelBlock:
    return text_block(text, text_format="markdown")


def ansi_block(text: object) -> PanelBlock:
    return text_block(text, text_format="ansi")


def panel_cell(text: object, role: PanelCellRole = "default") -> PanelCell:
    return PanelCell(text="" if text is None else str(text), role=role)


def section_block(title: object) -> PanelBlock:
    return PanelBlock(kind="section", text="" if title is None else str(title))


def spacer_block() -> PanelBlock:
    return PanelBlock(kind="spacer")


def key_value_block(rows: list[tuple[object, object]] | tuple[tuple[object, object], ...]) -> PanelBlock:
    return PanelBlock(kind="key_value", rows=tuple((str(k), "" if v is None else str(v)) for k, v in rows))


def command_block(rows: list[tuple[object, object]] | tuple[tuple[object, object], ...]) -> PanelBlock:
    return PanelBlock(kind="commands", rows=tuple((str(k), "" if v is None else str(v)) for k, v in rows))


def table_block(
    columns: list[PanelColumn] | tuple[PanelColumn, ...],
    rows: list[tuple[object, ...]] | tuple[tuple[object, ...], ...],
) -> PanelBlock:
    return PanelBlock(
        kind="table",
        columns=tuple(columns),
        table_rows=tuple(
            tuple(value if isinstance(value, PanelCell) else panel_cell(value) for value in row)
            for row in rows
        ),
    )


def notice_panel(
    title: str,
    message: object,
    *,
    detail: object | None = None,
    tone: PanelTone = "info",
    namespace: str = "command",
) -> PanelModel:
    rows: list[tuple[object, object]] = [("状态", message)]
    if detail:
        rows.append(("说明", detail))
    return PanelModel(
        title=title,
        blocks=(key_value_block(rows),),
        tone=tone,
        namespace=namespace,
    )
