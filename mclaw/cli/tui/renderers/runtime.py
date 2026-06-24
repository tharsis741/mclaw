# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Runtime notification rendering for the interactive TUI."""

from __future__ import annotations

from collections.abc import Callable

from mclaw.cli.colors import Colors
from mclaw.cli.runtime.events import EventType


RST = Colors.RESET
DIM = Colors.DIM


class RuntimeRenderer:
    """Render transient runtime messages that should not become full panels."""

    def __init__(
        self,
        *,
        printer: Callable[[str], None],
        event_sink: Callable[[EventType, dict], None] | None = None,
    ):
        self._printer = printer
        self._event_sink = event_sink

    def line(self, text: str = "") -> None:
        if self._event_sink is not None:
            self._event_sink(EventType.RUNTIME_MESSAGE, {"level": "info", "message": text})
            return
        self._printer(text)

    def warning(self, message: str, *, leading_newline: bool = False) -> None:
        if self._event_sink is not None:
            self._event_sink(EventType.RUNTIME_MESSAGE, {"level": "warning", "message": message})
            return
        prefix = "\n" if leading_newline else ""
        self._printer(f"{prefix}  {Colors.YELLOW}{message}{RST}")

    def error(self, message: str, *, leading_newline: bool = False) -> None:
        if self._event_sink is not None:
            self._event_sink(EventType.RUNTIME_MESSAGE, {"level": "error", "message": message})
            return
        prefix = "\n" if leading_newline else ""
        self._printer(f"{prefix}  {Colors.RED}{message}{RST}")

    def dim(self, message: str, *, leading_newline: bool = False) -> None:
        if self._event_sink is not None:
            self._event_sink(EventType.RUNTIME_MESSAGE, {"level": "muted", "message": message})
            return
        prefix = "\n" if leading_newline else ""
        self._printer(f"{prefix}  {DIM}{message}{RST}")

    def interrupted(self, symbol: str, *, leading_newline: bool = False) -> None:
        self.warning(f"{symbol} 已中断", leading_newline=leading_newline)

    def no_subagents(self) -> None:
        self._printer("\n  (无子代理任务)")

    def background_processes_stopped(self, count: int) -> None:
        self.dim(f"Stopped {count} background process(es).")

    def watcher_notice(self, symbol: str, notice: str) -> None:
        self.dim(f"{symbol} {notice}", leading_newline=True)

    def key_input_cancelled(self) -> None:
        self.dim("密钥输入已取消。")

    def command_echo(self, symbol: str, command: str) -> None:
        if self._event_sink is not None:
            self._event_sink(EventType.RUNTIME_MESSAGE, {"level": "command", "message": command, "symbol": symbol})
            return
        self._printer(f"\n  {symbol}  {command}")

    def user_message(self, marker: str, user_input: str) -> None:
        if self._event_sink is not None:
            self._event_sink(EventType.RUNTIME_MESSAGE, {"level": "user", "message": user_input, "symbol": marker})
            return
        self._printer(f"\n\033[48;2;27;40;56m\033[38;2;139;168;198m{marker} {user_input}\033[0m")
