# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Structured events shared by runtime and TUI."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from threading import RLock
from typing import Any


class RuntimeStatus(StrEnum):
    """Agent-turn states that frontends can render without runtime internals."""

    IDLE = "idle"
    REQUESTING = "requesting"
    STREAMING = "streaming"
    TOOLS = "tools"
    DELEGATING = "delegating"
    AGGREGATING = "aggregating"
    WAITING_FOR_USER = "waiting_for_user"
    DONE = "done"
    INTERRUPTED = "interrupted"
    ERROR = "error"


class EventType(StrEnum):
    """Stable event names emitted across runtime, TUI, and channel adapters."""

    APP_STARTED = "app.started"
    STATUS_CHANGED = "status.changed"
    USER_MESSAGE = "user.message"
    ASSISTANT_DELTA = "assistant.delta"
    ASSISTANT_MESSAGE = "assistant.message"
    TOOL_STARTED = "tool.started"
    TOOL_FINISHED = "tool.finished"
    COMMAND_ECHO = "command.echo"
    RUNTIME_MESSAGE = "runtime.message"
    STATUS_SNAPSHOT = "status.snapshot"
    PANEL_SHOW = "panel.show"
    CONFIRMATION_REQUESTED = "confirmation.requested"
    ROLLBACK_UPDATED = "rollback.updated"
    PROVIDER_UPDATED = "provider.updated"
    SESSION_CLOSED = "session.closed"
    ERROR = "error"


@dataclass(frozen=True)
class MClawEvent:
    """Timestamped event envelope passed through the synchronous event bus."""

    type: EventType | str
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


EventHandler = Callable[[MClawEvent], None]


class EventBus:
    """Small synchronous event bus used as the runtime/UI seam.

    The bus is deliberately framework-neutral: handlers can be local terminal
    interactive shells, tests, or non-interactive channel adapters.
    """

    def __init__(self) -> None:
        self._handlers: list[EventHandler] = []
        self._lock = RLock()

    def subscribe(self, handler: EventHandler) -> Callable[[], None]:
        """Register a handler and return an idempotent unsubscribe callback."""
        with self._lock:
            self._handlers.append(handler)

        def unsubscribe() -> None:
            with self._lock:
                try:
                    self._handlers.remove(handler)
                except ValueError:
                    pass

        return unsubscribe

    def emit(self, event_type: EventType | str, **payload: Any) -> MClawEvent:
        """Publish one event to a handler snapshot; handlers share its payload dict."""
        event = MClawEvent(type=event_type, payload=payload)
        with self._lock:
            handlers = list(self._handlers)
        for handler in handlers:
            handler(event)
        return event
