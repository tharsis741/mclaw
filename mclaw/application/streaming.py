# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Bound stream persistence work and send only changes to connected views."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any


class EventBatcher:
    """Persist ordered activity together, flushing before control/result boundaries."""

    def __init__(self, emit: Callable[[dict], None], interval: float = 0.025) -> None:
        self.emit = emit
        self.interval = interval
        self.pending: list[dict] = []
        self.timer: asyncio.TimerHandle | None = None
        self.failure: Exception | None = None

    def push(self, event: dict) -> None:
        if self.failure is not None:
            raise self.failure
        if event.get("type") in {"request", "request_resolved", "supplement_applied", "skill_install_resolved"}:
            self.flush()
            self.emit(event)
            return
        self.pending.append(event)
        if len(self.pending) >= 64:
            self.flush()
        elif self.timer is None:
            self.timer = asyncio.get_running_loop().call_later(self.interval, self._scheduled_flush)

    def _scheduled_flush(self) -> None:
        try:
            self.flush()
        except Exception as exc:
            # Surface persistence failures on the owning run, including its result boundary.
            self.failure = exc

    def flush(self) -> None:
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None
        if self.failure is not None:
            raise self.failure
        events, self.pending = self.pending, []
        if events:
            self.emit(events[0] if len(events) == 1 else {"type": "batch", "events": events})


def snapshot_changes(previous: Any, current: Any, path: list | None = None) -> list[dict]:
    """Diff public JSON snapshots, including append-only text and nested child streams."""
    path = [] if path is None else path
    if previous == current:
        return []
    if isinstance(previous, dict) and isinstance(current, dict):
        changes = [{"op": "remove", "path": [*path, key]} for key in previous if key not in current]
        for key, value in current.items():
            if key not in previous:
                changes.append({"op": "set", "path": [*path, key], "value": value})
            else:
                changes.extend(snapshot_changes(previous[key], value, [*path, key]))
        return changes
    if isinstance(previous, list) and isinstance(current, list):
        if len(previous) <= len(current):
            changes = []
            for index, value in enumerate(previous):
                changes.extend(snapshot_changes(value, current[index], [*path, index]))
            if len(current) > len(previous):
                changes.append({"op": "append", "path": path, "value": current[len(previous):]})
            return changes
    if isinstance(previous, str) and isinstance(current, str) and current.startswith(previous):
        return [{"op": "append", "path": path, "value": current[len(previous):]}]
    return [{"op": "set", "path": path, "value": current}]
