# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Per-session coordination of Skill saves and periodic background reviews."""

from __future__ import annotations

import copy
import json
import logging
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SkillReviewRequest:
    messages: list[dict[str, Any]]
    save_generation: int
    consumed_turns: int
    review_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    session_id: str | None = None
    trigger_task_id: str | None = None
    trigger_response_id: str | None = None


class SkillReviewState:
    """Serialize counter updates and reserve at most one review per session."""

    def __init__(self, *, session_id: str | None = None) -> None:
        self._lock = threading.Lock()
        self._turns = 0
        self._save_generation = 0
        self._running: SkillReviewRequest | None = None
        self._pending: list[dict[str, Any]] | None = None
        self._pending_origin: tuple[str | None, str | None] = (None, None)
        self._closed = False
        self._session_id = session_id
        self._state_id = uuid.uuid4().hex
        self._log_sequence = 0

    def _snapshot(self) -> dict[str, Any]:
        """Caller holds the state lock; never include conversation content."""
        return {
            "turns": self._turns,
            "save_generation": self._save_generation,
            "running_review_id": self._running.review_id if self._running else None,
            "pending": self._pending is not None,
            "pending_message_count": len(self._pending) if self._pending is not None else 0,
            "closed": self._closed,
        }

    def snapshot(self) -> dict[str, Any]:
        """Return one coherent, content-free diagnostic snapshot."""
        with self._lock:
            return self._snapshot()

    def _log(self, event: str, before: dict[str, Any], **details: Any) -> None:
        """Log within the lock so before/after values share an atomic transition."""
        self._log_sequence += 1
        logger.info("[SKILL REVIEW] %s", json.dumps({
            "event": event, "session_id": self._session_id,
            "state_id": self._state_id, "sequence": self._log_sequence,
            "before": before, "after": self._snapshot(), **details,
        }, ensure_ascii=True, separators=(",", ":")))

    @property
    def turns(self) -> int:
        with self._lock:
            return self._turns

    @turns.setter
    def turns(self, value: int) -> None:
        with self._lock:
            before = self._snapshot()
            self._turns = value
            if before["turns"] != value:
                self._log("counter_set", before)

    @property
    def save_generation(self) -> int:
        with self._lock:
            return self._save_generation

    def saved(self, *, source: str = "main_agent", action: str = "",
              review_id: str | None = None) -> None:
        """A committed save starts a new interval and supersedes pending work."""
        with self._lock:
            before = self._snapshot()
            self._save_generation += 1
            self._turns = 0
            self._pending = None
            self._log("saved", before, source=source, action=action, review_id=review_id)

    def close(self) -> None:
        """A retiring session must not launch further queued reviews."""
        with self._lock:
            before = self._snapshot()
            self._closed = True
            self._pending = None
            self._log("closed", before)

    def complete_turn(self, *, generation: int, interval: int, messages: list[dict[str, Any]],
                      task_id: str | None = None, response_id: str | None = None) -> bool:
        with self._lock:
            before = self._snapshot()
            # A foreground or background save during this turn already reset
            # its interval; finishing that same turn must not add it back.
            if self._closed or interval <= 0 or generation != self._save_generation:
                reason = "closed" if self._closed else "disabled" if interval <= 0 else "save_during_turn"
                self._log("turn_skipped", before, reason=reason, interval=interval, turn_generation=generation)
                return False
            self._turns += 1
            if self._turns < interval:
                self._log("turn_completed", before, interval=interval, queued=False)
                return False
            self._pending = copy.deepcopy(messages)
            self._pending_origin = (task_id, response_id)
            self._log("turn_completed", before, interval=interval, queued=True)
            return True

    def reserve(self, messages: list[dict[str, Any]], *, pending_only: bool = False,
                task_id: str | None = None, response_id: str | None = None) -> SkillReviewRequest | None:
        with self._lock:
            before = self._snapshot()
            if self._closed:
                self._log("review_deferred", before, reason="closed")
                return None
            if not pending_only:
                self._pending = copy.deepcopy(messages)
                self._pending_origin = (task_id, response_id)
            request = self._reserve_pending()
            self._log("review_reserved" if request else "review_deferred", before,
                      reason="reserved" if request else "busy" if self._running else "no_pending")
            return request

    def _reserve_pending(self) -> SkillReviewRequest | None:
        """Called only while holding the state lock."""
        if self._closed or self._running is not None or self._pending is None:
            return None
        request = SkillReviewRequest(self._pending, self._save_generation, self._turns,
                                     session_id=self._session_id,
                                     trigger_task_id=self._pending_origin[0],
                                     trigger_response_id=self._pending_origin[1])
        self._running = request
        self._pending = None
        # Even a review that finds nothing starts the next N-turn interval.
        self._turns = 0
        return request

    def finish(self, request: SkillReviewRequest, *, started: bool = True) -> SkillReviewRequest | None:
        with self._lock:
            before = self._snapshot()
            if self._running is not request:
                self._log("finish_ignored", before, review_id=request.review_id)
                return None
            self._running = None
            if self._closed:
                self._log("review_finished", before, review_id=request.review_id, reason="closed")
                return None
            if not started:
                # Do not undo a save that raced with a failed thread launch.
                if request.save_generation == self._save_generation:
                    self._turns += request.consumed_turns
                    if self._pending is None:
                        self._pending = request.messages
                        self._pending_origin = (request.trigger_task_id, request.trigger_response_id)
                # Retry at the next completed turn, never in a failure loop.
                self._log("launch_failed", before, review_id=request.review_id,
                          restored=request.save_generation == self._save_generation)
                return None
            next_request = self._reserve_pending()
            self._log("review_finished", before, review_id=request.review_id,
                      next_review_id=next_request.review_id if next_request else None)
            return next_request


_STATE_INIT_LOCK = threading.Lock()


def get_skill_review_state(agent: Any) -> SkillReviewState:
    """Also support lightweight agents initialized without the full constructor."""
    with _STATE_INIT_LOCK:
        state = getattr(agent, "_skill_review_state", None)
        if state is None:
            state = SkillReviewState(session_id=getattr(agent, "session_id", None))
            agent._skill_review_state = state
        return state
