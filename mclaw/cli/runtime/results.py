# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Runtime coordination for agent turn result handling."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RuntimeTurnResultHooks:
    """Host operations used after an agent turn returns a result dict."""

    stream_text: Callable[[], str]
    stream_started: Callable[[], bool]
    render_response: Callable[[str], None]
    render_interrupted: Callable[[], None]
    emit_waiting_for_skill_confirmation: Callable[[], None]
    remember_pending_skill_confirmation: Callable[[dict[str, Any]], None]
    render_skill_confirmation: Callable[[dict[str, Any]], None]
    handle_pending_delegate: Callable[[dict[str, Any]], bool]
    log_skill_confirmation_pending: Callable[[dict[str, Any]], None]
    invalidate_skill_registry: Callable[[], None] = lambda: None


class RuntimeTurnResultCoordinator:
    """Interprets a completed agent turn result and dispatches follow-up work."""

    def __init__(self, hooks: RuntimeTurnResultHooks) -> None:
        self.hooks = hooks

    def handle_result(self, result: dict[str, Any]) -> None:
        """Render terminal output and dispatch post-turn continuation intents."""
        display_text = self.select_display_text(
            result,
            stream_text=self.hooks.stream_text(),
            stream_started=self.hooks.stream_started(),
        )
        if display_text:
            self.hooks.render_response(display_text)

        if result.get("interrupted"):
            self.hooks.render_interrupted()
            return

        if result.get("skills_changed"):
            self.hooks.invalidate_skill_registry()

        if result.get("pending_skill_import_confirmation"):
            confirmation = result.get("confirmation_data", {}) or {}
            self.hooks.emit_waiting_for_skill_confirmation()
            self.hooks.remember_pending_skill_confirmation(confirmation)
            self.hooks.log_skill_confirmation_pending(confirmation)
            self.hooks.render_skill_confirmation(confirmation)
            return

        if result.get("pending_delegate"):
            self.hooks.handle_pending_delegate(result)

    @staticmethod
    def select_display_text(result: dict[str, Any], *, stream_text: str, stream_started: bool) -> str:
        """Choose the text to render after a turn, preserving confirmation prompts."""
        response = str(result.get("final_response") or "")
        streamed = str(stream_text or "").strip() if stream_started else ""
        if result.get("pending_skill_import_confirmation") or result.get("pending_delegate"):
            return response
        if response:
            return response
        return streamed
