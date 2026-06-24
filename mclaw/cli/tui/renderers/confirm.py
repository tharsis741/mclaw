"""Interactive confirmation prompt rendering."""

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
        skill_name = confirmation.get("skill_name") or confirmation.get("name") or "(unknown)"
        risk = confirmation.get("risk_level") or "unknown"
        security_review = confirmation.get("security_review")
        if not isinstance(security_review, dict):
            security_review = {}
        summary = _localized_summary(
            confirmation.get("summary")
            or security_review.get("summary")
            or "Skill 包已准备好启用。",
            skill_name=str(skill_name),
        )
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
                state_label = "Authorize existing key"
            elif state == "refresh":
                state_label = "Replace existing key"
            else:
                state_label = "Enter missing key"
            requested.append((env_var, state_label, purpose, state))
        needs_input = any(state != "authorize" for _env_var, _state_label, _purpose, state in requested)

        rows = [("Scope", required_for)]
        for index, (env_var, state_label, purpose, _state) in enumerate(requested, 1):
            value = state_label if not purpose else f"{state_label} - {purpose}"
            rows.append((f"Secret {index}", f"{env_var} ({value})"))

        commands = (
            [("Paste value", "Single key"), ("KEY=value; KEY2=value", "Multiple keys"), ("/skip / Esc", "Skip")]
            if needs_input
            else [("Y / Enter", "Authorize all keys"), ("N / Esc", "Skip")]
        )
        panel = PanelModel(
            title="Secret request",
            namespace="confirmation",
            tone="warning",
            blocks=(
                text_block("M-Claw needs a scoped secret. Plaintext will not be returned to the model."),
                spacer_block(),
                key_value_block(rows),
                spacer_block(),
                section_block("Action"),
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


def _localized_summary(summary: object, *, skill_name: str) -> str:
    text = str(summary or "").strip()
    if text == "Skill package is prepared for enablement.":
        return "Skill 包已准备好启用。"
    if (
        text.startswith("Prepared Skill '")
        and text.endswith("for enablement. Security review saved in drafting package.")
    ):
        return f"Skill '{skill_name}' 已准备好启用，安全审查已保存到草稿包。"
    if text == "Security policy blocked this Skill.":
        return "安全策略已拒绝安装该 Skill。"
    return text
