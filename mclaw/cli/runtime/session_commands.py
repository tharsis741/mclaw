# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Runtime coordination for interactive session slash commands."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


RESUME_LATEST_SESSION = "__latest__"


@dataclass(frozen=True)
class RuntimeSessionCommandHooks:
    """Host operations used by UI-neutral session command flows."""

    current_session_id: Callable[[], str]
    set_current_session_id: Callable[[str], None]
    resolve_session_id: Callable[[str], str | None]
    get_messages_as_conversation: Callable[[str], list[dict[str, Any]]]
    end_session: Callable[[str, str], None]
    reopen_session: Callable[[str], None]
    init_agent: Callable[[], None]
    set_agent_messages: Callable[[list[dict[str, Any]]], None]
    get_session: Callable[[str], dict[str, Any] | None]
    set_session_title: Callable[[str, str], bool]
    export_session: Callable[[str], dict[str, Any] | None]
    save_session_export: Callable[[str, dict[str, Any]], str]
    render_notice: Callable[[str, str, str, str], None]
    switch_session_lock: Callable[[str], bool] | None = None


class RuntimeSessionCommandCoordinator:
    """Handles session slash commands without depending on a concrete TUI."""

    def __init__(self, hooks: RuntimeSessionCommandHooks) -> None:
        self.hooks = hooks

    def handle_resume(self, raw_args: str) -> None:
        """Restore a session and replace the live agent context with its history."""
        session_ref = str(raw_args or "").strip()
        if not session_ref:
            self.hooks.render_notice("M-Claw 会话", "用法: /resume <会话ID或前缀>", "", "warning")
            return

        resolved = self.hooks.resolve_session_id(session_ref)
        if not resolved:
            self.hooks.render_notice("M-Claw 会话", f"未找到会话: {session_ref}", "", "danger")
            return

        if resolved != self.hooks.current_session_id() and self.hooks.switch_session_lock:
            # Session locks move before state mutation so two TUIs cannot attach
            # to the same persisted conversation.
            if not self.hooks.switch_session_lock(resolved):
                return

        history = self.hooks.get_messages_as_conversation(resolved)
        self.hooks.end_session(self.hooks.current_session_id(), "user_resume")
        self.hooks.set_current_session_id(resolved)
        self.hooks.reopen_session(resolved)
        self.hooks.init_agent()
        self.hooks.set_agent_messages(history)

        session = self.hooks.get_session(resolved)
        title = str(session.get("title", "") if session else "")
        self.hooks.render_notice(
            "M-Claw 会话",
            f"已恢复会话: {resolved[:16]} {title}",
            f"已加载 {len(history)} 条消息",
            "success",
        )

    def handle_title(self, raw_args: str) -> None:
        """Show or update the title stored with the current session."""
        title = str(raw_args or "").strip()
        session_id = self.hooks.current_session_id()
        if title:
            try:
                if self.hooks.set_session_title(session_id, title):
                    self.hooks.render_notice("M-Claw 会话标题", f"标题已设置: {title}", "", "success")
                else:
                    self.hooks.render_notice("M-Claw 会话标题", "未找到会话。", "", "warning")
            except ValueError as exc:
                self.hooks.render_notice("M-Claw 会话标题", str(exc), "", "danger")
            return

        session = self.hooks.get_session(session_id)
        if session and session.get("title"):
            self.hooks.render_notice("M-Claw 会话标题", f"标题: {session['title']}", "", "info")
        else:
            self.hooks.render_notice("M-Claw 会话标题", "暂无标题。用法: /title <名称>", "", "warning")

    def handle_save(self, raw_args: str = "") -> None:
        """Export the current session through the host persistence hooks."""
        if str(raw_args or "").strip():
            self.hooks.render_notice("M-Claw 会话导出", "用法: /save", "", "warning")
            return

        session_id = self.hooks.current_session_id()
        export = self.hooks.export_session(session_id)
        if not export:
            self.hooks.render_notice("M-Claw 会话导出", "暂无会话数据可保存。", "", "warning")
            return

        out_file = self.hooks.save_session_export(session_id, export)
        self.hooks.render_notice("M-Claw 会话导出", f"已保存至: {out_file}", "", "success")
