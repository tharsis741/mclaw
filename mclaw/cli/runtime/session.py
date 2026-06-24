# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Runtime state primitives for interactive M-Claw sessions."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .events import RuntimeStatus


@dataclass
class RuntimeSessionState:
    """UI-neutral state for a single interactive runtime."""

    status: RuntimeStatus = RuntimeStatus.IDLE
    active_tools: set[str] = field(default_factory=set)
    stream_text: str = ""
    stream_started: bool = False
    turn_started_at: datetime | None = None
    last_turn_duration: float = 0.0
    last_result: dict[str, Any] | None = None

    def begin_turn(self) -> None:
        self.status = RuntimeStatus.REQUESTING
        self.active_tools.clear()
        self.stream_text = ""
        self.stream_started = False
        self.last_result = None
        self.turn_started_at = datetime.now()

    def append_stream_delta(self, text: str) -> int:
        self.status = RuntimeStatus.STREAMING
        self.stream_started = True
        self.stream_text += text
        return len(self.stream_text)

    def begin_tools(self, tool_name: str) -> None:
        self.status = RuntimeStatus.TOOLS
        if tool_name:
            self.active_tools.add(tool_name)

    def finish_tools(self) -> None:
        self.active_tools.clear()
        if self.status == RuntimeStatus.TOOLS:
            self.status = RuntimeStatus.STREAMING

    def finish_turn(self, result: dict[str, Any] | None = None) -> float:
        self.last_result = result or self.last_result
        if self.turn_started_at:
            self.last_turn_duration = (datetime.now() - self.turn_started_at).total_seconds()
            self.turn_started_at = None
        self.active_tools.clear()
        self.status = RuntimeStatus.DONE
        return self.last_turn_duration

    def fail_turn(self, error: str) -> None:
        self.last_result = {"error": error, "completed": False}
        self.active_tools.clear()
        self.status = RuntimeStatus.ERROR
