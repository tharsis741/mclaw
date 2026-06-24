# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Render Skill command output through the shared TUI panel boundary."""

from __future__ import annotations

from typing import Callable

from mclaw.cli.runtime.panels import PanelModel, text_block
from mclaw.cli.tui.panel_renderer import render_panel_model


class SkillsRenderer:
    def __init__(
        self,
        *,
        run_external_output: Callable,
        box_factory: Callable,
        panel_sink: Callable[[PanelModel], None] | None = None,
    ):
        self._run_external_output = run_external_output
        self._box_factory = box_factory
        self._panel_sink = panel_sink

    def render_skills_output(self, text: str) -> None:
        panel = PanelModel(
            title="M-Claw Skills",
            namespace="skills",
            blocks=(text_block(text or "无输出"),),
        )
        if self._panel_sink is not None:
            self._panel_sink(panel)
            return
        render_panel_model(
            panel,
            box=self._box_factory(),
            run_external_output=self._run_external_output,
        )
