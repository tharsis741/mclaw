# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Event types shared between M-Claw and the optional pet sidecar."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import time
from typing import Any, Dict


class PetState(str, Enum):
    """Animation states understood by pet manifests and runtime events."""

    IDLE = "idle"
    RUNNING = "running"
    REVIEW = "review"
    WAITING = "waiting"
    WAVING = "waving"
    FAILED = "failed"
    JUMPING = "jumping"
    RUNNING_LEFT = "running-left"
    RUNNING_RIGHT = "running-right"
    SLEEPING = "sleeping"
    READING = "reading"
    TYPING = "typing"
    CARRYING = "carrying"


class PetEventType(str, Enum):
    """Runtime event names that can be mirrored to the pet sidecar."""

    APP_STARTED = "app_started"
    APP_EXITING = "app_exiting"
    TURN_STARTED = "turn_started"
    MODEL_STREAMING = "model_streaming"
    TOOL_STARTED = "tool_started"
    TOOL_FINISHED = "tool_finished"
    STATUS_CHANGED = "status_changed"
    WAITING_FOR_USER = "waiting_for_user"
    TURN_COMPLETED = "turn_completed"
    TURN_FAILED = "turn_failed"
    TURN_INTERRUPTED = "turn_interrupted"
    BACKGROUND_PROCESS_UPDATED = "background_process_updated"
    BACKGROUND_PROCESS_COMPLETED = "background_process_completed"
    DELEGATION_STARTED = "delegation_started"
    DELEGATION_TASK_STARTED = "delegation_task_started"
    DELEGATION_TASK_TOOL = "delegation_task_tool"
    DELEGATION_TASK_COMPLETED = "delegation_task_completed"
    DELEGATION_TASK_FAILED = "delegation_task_failed"
    DELEGATION_COMPLETED = "delegation_completed"


LOW_PRIORITY_EVENTS = {
    PetEventType.MODEL_STREAMING.value,
    PetEventType.STATUS_CHANGED.value,
}


@dataclass
class PetEvent:
    """Serializable event envelope sent across the controller-to-sidecar queue."""

    type: str
    state: str | None = None
    text: str = ""
    session_id: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        """Return the queue-safe representation used by multiprocessing."""
        return {
            "type": self.type,
            "state": self.state,
            "text": self.text,
            "session_id": self.session_id,
            "payload": self.payload,
            "ts": self.ts,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PetEvent":
        """Reconstruct an event from a sidecar queue payload."""
        return cls(
            type=str(data.get("type") or ""),
            state=data.get("state"),
            text=str(data.get("text") or ""),
            session_id=str(data.get("session_id") or ""),
            payload=dict(data.get("payload") or {}),
            ts=float(data.get("ts") or time.time()),
        )


