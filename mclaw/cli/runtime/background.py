# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Background process event coordination for interactive runtimes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RuntimeBackgroundHooks:
    """Host operations used by the background event coordinator."""

    pop_completion_event: Callable[[], dict[str, Any] | None]
    emit_process_completed: Callable[[str, dict[str, Any]], None]
    emit_process_updated: Callable[[str, dict[str, Any]], None]
    render_watcher_notice: Callable[[str], None]
    begin_background_turn: Callable[[str], None]
    run_background_turn: Callable[[str], None]
    finish_background_turn: Callable[[], None]


class RuntimeBackgroundCoordinator:
    """Turns background process completion events into agent follow-up turns."""

    def __init__(self, hooks: RuntimeBackgroundHooks) -> None:
        self.hooks = hooks

    def drain_completion_events(self) -> None:
        while True:
            event = self.hooks.pop_completion_event()
            if event is None:
                return
            self._handle_event(event)

    def _handle_event(self, event: dict[str, Any]) -> None:
        event_type = str(event.get("event_type") or "process_complete")
        cmd = str(event.get("command") or "")[:60]
        if event_type in {"process_complete", "watcher_complete"}:
            self.hooks.emit_process_completed(cmd, event)
        elif event_type == "watcher_update":
            self.hooks.emit_process_updated(cmd, event)

        notice = self._notice_for_event(event, event_type, cmd)
        self.hooks.render_watcher_notice(notice)
        self.hooks.begin_background_turn(notice)
        try:
            self.hooks.run_background_turn(notice)
        finally:
            self.hooks.finish_background_turn()

    @staticmethod
    def _notice_for_event(event: dict[str, Any], event_type: str, cmd: str) -> str:
        session_id = str(event.get("session_id") or "")
        tail = str(event.get("output") or "")
        if event_type == "watcher_update":
            uptime = event.get("uptime_seconds")
            return (
                f"后台进程运行中 (session_id={session_id}, uptime={uptime}s)\n"
                f"命令: {cmd}\n"
                f"最新输出:\n{tail}"
            )
        if event_type == "watcher_complete":
            exit_code = event.get("exit_code")
            return (
                f"后台进程已完成 (watcher, session_id={session_id}, exit_code={exit_code})\n"
                f"命令: {cmd}\n"
                f"最新输出:\n{tail}"
            )
        exit_code = event.get("exit_code")
        return (
            f"后台进程已完成 (session_id={session_id}, exit_code={exit_code})\n"
            f"命令: {cmd}\n"
            f"最新输出:\n{tail}"
        )
