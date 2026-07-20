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
    detail: str = ""
    active_tools: set[str] = field(default_factory=set)
    stream_text: str = ""
    stream_started: bool = False
    turn_started_at: datetime | None = None
    last_turn_duration: float = 0.0
    last_result: dict[str, Any] | None = None

    def set_status(self, status: RuntimeStatus, detail: str = "") -> None:
        """Set the canonical lifecycle state and its compact display detail."""
        self.status = status
        self.detail = str(detail or "")

    def begin_turn(self) -> None:
        """Reset per-turn rendering state and mark the runtime as requesting."""
        self.set_status(RuntimeStatus.REQUESTING, "Preparing request")
        self.active_tools.clear()
        self.stream_text = ""
        self.stream_started = False
        self.last_result = None
        self.turn_started_at = datetime.now()

    def append_stream_delta(self, text: str) -> int:
        """Record streamed assistant text and return the total buffered length."""
        self.active_tools.clear()
        self.stream_started = True
        self.stream_text += text
        char_count = len(self.stream_text)
        self.set_status(RuntimeStatus.STREAMING, f"Streaming {char_count} chars")
        return char_count

    def begin_tools(self, tool_name: str) -> None:
        """Move the runtime into tool execution and track the active tool name."""
        self.set_status(RuntimeStatus.TOOLS)
        if tool_name:
            self.active_tools.add(tool_name)

    def finish_tools(self) -> None:
        """Clear tool state while the next model response is requested."""
        self.active_tools.clear()
        self.set_status(RuntimeStatus.REQUESTING, "Waiting for response")

    def reset_stream(self) -> None:
        """Reset the per-round stream buffer without changing lifecycle state."""
        self.stream_text = ""
        self.stream_started = False

    def finish_turn(self, result: dict[str, Any] | None = None) -> float:
        """Finalize turn state, keeping the latest result for status snapshots."""
        if result is not None:
            self.last_result = result
        final_result = self.last_result or {}
        if self.turn_started_at:
            self.last_turn_duration = (datetime.now() - self.turn_started_at).total_seconds()
            self.turn_started_at = None
        self.active_tools.clear()
        if final_result.get("pending_skill_import_confirmation"):
            self.set_status(RuntimeStatus.WAITING_FOR_USER, "Waiting for skill confirmation")
        elif final_result.get("interrupted"):
            self.set_status(RuntimeStatus.INTERRUPTED, "Interrupted")
        elif final_result.get("error"):
            self.set_status(RuntimeStatus.ERROR, str(final_result.get("error") or "Error"))
        elif final_result.get("stop_reason") == "max_iterations":
            self.set_status(RuntimeStatus.ERROR, "Iteration limit reached")
        elif final_result.get("stop_reason") == "timeout":
            self.set_status(RuntimeStatus.ERROR, "Timed out")
        else:
            self.set_status(RuntimeStatus.DONE)
        return self.last_turn_duration

    def fail_turn(self, error: str) -> None:
        """Store a minimal failed-result payload and mark the runtime errored."""
        self.last_result = {"error": error, "completed": False}
        self.active_tools.clear()
        self.set_status(RuntimeStatus.ERROR, error)
