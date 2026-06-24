# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Interactive runtime state independent of any TUI framework."""

from __future__ import annotations

import queue
import time
from dataclasses import dataclass, field
from .events import EventBus
from .session import RuntimeSessionState


@dataclass
class InteractiveRuntime:
    """Mutable runtime control state shared by interactive shells."""

    event_bus: EventBus = field(default_factory=EventBus)
    session_state: RuntimeSessionState = field(default_factory=RuntimeSessionState)
    pending_input: queue.Queue = field(default_factory=queue.Queue)
    should_exit: bool = False
    agent_running: bool = False
    force_exit_no_flush: bool = False
    last_interrupt_at: float = 0.0

    def submit_text(self, text: str) -> None:
        if text:
            self.pending_input.put(text)

    def request_exit(self, *, force_no_flush: bool = False) -> None:
        if force_no_flush:
            self.force_exit_no_flush = True
        self.should_exit = True

    def interrupt_window_hit(self, *, now: float | None = None, seconds: float = 2.0) -> bool:
        current = time.time() if now is None else now
        if current - float(self.last_interrupt_at or 0.0) < seconds:
            return True
        self.last_interrupt_at = current
        return False
