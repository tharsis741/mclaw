"""Runtime coordination for informational slash commands."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class RuntimeInfoCommandHooks:
    """Host operations used by UI-neutral informational commands."""

    push_to_talk_label: Callable[[], str]
    run_doctor: Callable[[], Any]
    format_doctor: Callable[[Any], str]
    current_agent: Callable[[], Any]
    current_model: Callable[[], str]
    session_start: Callable[[], datetime]
    list_history_sessions: Callable[[], list[dict[str, Any]]]
    handle_skills_command: Callable[[str], str]
    render_help: Callable[[str], None]
    render_doctor: Callable[[str], None]
    render_usage: Callable[[Any, str, datetime], None]
    render_history: Callable[[list[dict[str, Any]]], None]
    render_skills_output: Callable[[str], None]


class RuntimeInfoCommandCoordinator:
    """Handles help, diagnostics, usage, history, and skills commands."""

    def __init__(self, hooks: RuntimeInfoCommandHooks) -> None:
        self.hooks = hooks

    def handle_help(self) -> None:
        self.hooks.render_help(self.hooks.push_to_talk_label())

    def handle_doctor(self, raw_args: str = "") -> None:
        arg = str(raw_args or "").strip().lower()
        if arg:
            self.hooks.render_doctor("doctor 不接受参数，请使用 /doctor。")
            return
        result = self.hooks.run_doctor()
        self.hooks.render_doctor(self.hooks.format_doctor(result))

    def handle_usage(self) -> None:
        self.hooks.render_usage(
            self.hooks.current_agent(),
            self.hooks.current_model(),
            self.hooks.session_start(),
        )

    def handle_history(self) -> None:
        self.hooks.render_history(self.hooks.list_history_sessions())

    def handle_skills(self, raw_args: str = "") -> None:
        self.hooks.render_skills_output(self.hooks.handle_skills_command(str(raw_args or "").strip()))
