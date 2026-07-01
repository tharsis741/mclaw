# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Render interactive confirmations for security-sensitive CLI flows."""

from __future__ import annotations

from typing import Callable

from mclaw.cli.runtime.panels import (
    PanelModel,
    command_block,
    key_value_block,
    section_block,
    spacer_block,
    text_block,
)
from mclaw.cli.tui.console import MClawConsole
from mclaw.cli.tui.panel_renderer import render_panel_model


class ConfirmRenderer:
    """Build confirmation panels while keeping input collection outside the renderer."""

    def __init__(
        self,
        *,
        printer: Callable[[str], None],
        box_factory: Callable,
        console_factory: Callable[[], MClawConsole] = MClawConsole,
        panel_sink: Callable[[PanelModel], None] | None = None,
    ):
        self._printer = printer
        self._box_factory = box_factory
        self._console_factory = console_factory
        self._panel_sink = panel_sink

    def render_skill_import_confirmation(self, confirmation: dict) -> None:
        """Show Skill import review details with a risk-toned confirmation panel."""
        skill_name = confirmation.get("skill_name") or confirmation.get("name") or "(unknown)"
        risk = confirmation.get("risk_level") or "unknown"
        security_review = confirmation.get("security_review")
        if not isinstance(security_review, dict):
            security_review = {}
        summary = confirmation.get("summary") or security_review.get("summary") or "Skill 包已准备好启用。"
        findings = confirmation.get("findings")
        if not isinstance(findings, list):
            findings = security_review.get("findings")
        if not isinstance(findings, list):
            findings = []

        def finding_label(item: object) -> str:
            if not isinstance(item, dict):
                return str(item)
            severity = str(item.get("severity") or "").strip()
            pattern = str(item.get("pattern_id") or "").strip()
            file = str(item.get("file") or "").strip()
            line = item.get("line")
            location = file if line in (None, "") else f"{file}:{line}"
            pieces = [part for part in (severity, location, pattern) if part]
            return " ".join(pieces) or str(item.get("description") or item)

        rows = [
            ("名称", str(skill_name)),
            ("风险", str(risk)),
            ("摘要", str(summary)),
        ]
        for idx, finding in enumerate(findings[:3], 1):
            rows.append((f"风险项 {idx}", finding_label(finding)))

        panel = PanelModel(
            title="Skill 安装确认",
            namespace="confirmation",
            tone=_risk_tone(risk),
            blocks=(
                text_block("M-Claw 已准备好待启用的 Skill 包。"),
                spacer_block(),
                key_value_block(rows),
                spacer_block(),
                section_block("操作"),
                command_block([
                    ("Y", "启用 Skill"),
                    ("N / Esc", "取消"),
                ]),
            ),
        )
        if self._panel_sink is not None:
            self._panel_sink(panel)
            return
        render_panel_model(
            panel,
            printer=self._printer,
            box=self._box_factory(),
        )

    def render_secret_request(self, request: dict) -> None:
        """Show scoped credential requests without exposing secret values to model output."""
        needs = request.get("needs") if isinstance(request, dict) else None
        if isinstance(needs, list):
            needs = [item for item in needs if isinstance(item, dict)]
        else:
            need = request.get("need") if isinstance(request, dict) else {}
            needs = [need] if isinstance(need, dict) else []
        required_for = str(request.get("required_for") or "").strip()
        requested = []
        for item in needs:
            env_var = str(item.get("env_var") or "").strip().upper()
            if not env_var:
                continue
            purpose = str(item.get("purpose") or item.get("provider") or "").strip()
            state = str(item.get("state") or "").strip()
            if state == "authorize":
                state_label = "授权已保存的密钥"
            elif state == "refresh":
                state_label = "替换已保存的密钥"
            else:
                state_label = "输入缺失密钥"
            requested.append((env_var, state_label, purpose, state))
        needs_input = any(state != "authorize" for _env_var, _state_label, _purpose, state in requested)

        rows = [("作用域", required_for)]
        for index, (env_var, state_label, purpose, _state) in enumerate(requested, 1):
            value = state_label if not purpose else f"{state_label} - {purpose}"
            rows.append((f"凭据 {index}", f"{env_var} ({value})"))

        commands = (
            [("粘贴密钥", "单个密钥"), ("KEY=value; KEY2=value", "多个密钥"), ("/skip / Esc", "跳过")]
            if needs_input
            else [("Y / Enter", "授权全部密钥"), ("N / Esc", "跳过")]
        )
        panel = PanelModel(
            title="凭据请求",
            namespace="confirmation",
            tone="warning",
            blocks=(
                text_block("M-Claw 需要作用域受限的凭据，明文不会返回给模型。"),
                spacer_block(),
                key_value_block(rows),
                spacer_block(),
                section_block("操作"),
                command_block(commands),
            ),
        )
        if self._panel_sink is not None:
            self._panel_sink(panel)
            return
        render_panel_model(
            panel,
            printer=self._printer,
            box=self._box_factory(),
        )


def _risk_tone(risk: str) -> str:
    return {
        "安全": "success",
        "存疑": "warning",
        "危险": "danger",
    }.get(str(risk), "warning")
