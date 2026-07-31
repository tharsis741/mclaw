# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Context rollback coordination helpers."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def reset_agent_runtime_context(agent: Any) -> None:
    """Clear cached per-turn state after persisted chat history is rewound."""
    if not agent:
        return
    compressor = getattr(agent, "context_compressor", None)
    if compressor:
        for name, value in [
            ("_previous_summary", None),
            ("_compressed_this_turn", False),
            ("compression_count", 0),
            ("last_prompt_tokens", 0),
            ("last_completion_tokens", 0),
            ("last_total_tokens", 0),
            ("display_context_tokens", None),
            ("display_context_estimated", False),
            ("_display_estimate_emitted", False),
            ("last_compression_outcome", "not_attempted"),
        ]:
            if hasattr(compressor, name):
                try:
                    setattr(compressor, name, value)
                except Exception:
                    logger.debug("Context rollback could not reset compressor field %s", name, exc_info=True)
    if hasattr(agent, "_recalled_memory"):
        agent._recalled_memory = ""


class ContextRollbackManager:
    """Keep persisted chat context and live agent state aligned with filesystem rollback."""

    def __init__(self, session_db: Any = None, agent: Any = None):
        self.session_db = session_db
        self.agent = agent

    def apply(
        self,
        *,
        session_id: str,
        marker_message_id: int | None,
        mode: str,
        checkpoint_hash: str | None = None,
        operation_id: str | None = None,
        turn_id: str | None = None,
        rollback_id: str | None = None,
        metadata: dict | None = None,
        scope: str = "operation",
    ) -> dict[str, Any]:
        """Invalidate messages affected by a rollback without touching filesystem state."""
        mode = (mode or "soft").lower()
        scope = (scope or "operation").lower()
        if mode in {"off", "fs-only"}:
            return {"skipped": True, "reason": mode, "invalidated": 0, "rollback_id": None}
        if mode not in {"soft", "strict"}:
            return {"skipped": True, "reason": f"invalid context mode: {mode}", "invalidated": 0, "rollback_id": None}
        if scope not in {"operation", "tail"}:
            return {"skipped": True, "reason": f"invalid context scope: {scope}", "invalidated": 0, "rollback_id": None}
        if marker_message_id is None:
            return {"skipped": True, "reason": "missing marker", "invalidated": 0, "rollback_id": None}
        if not self.session_db:
            return {"skipped": True, "reason": "no session db", "invalidated": 0, "rollback_id": None}
        if mode == "soft" and scope == "operation":
            result = self.session_db.invalidate_operation_context(
                session_id,
                marker_message_id,
                turn_id=turn_id,
                operation_id=operation_id,
                rollback_id=rollback_id,
                reason="filesystem rollback",
                mode=mode,
                checkpoint_hash=checkpoint_hash,
                metadata=metadata or {},
            )
        else:
            result = self.session_db.invalidate_messages_after(
                session_id,
                marker_message_id,
                rollback_id=rollback_id,
                reason="filesystem rollback",
                mode=mode,
                checkpoint_hash=checkpoint_hash,
                operation_id=operation_id,
                metadata=metadata or {},
            )
        if self.agent_matches_session(session_id):
            try:
                self.reload_agent_messages(session_id)
                self.reset_runtime_context()
            except Exception as exc:
                logger.warning("Context was rolled back but the live agent could not be refreshed: %s", exc)
                return {"skipped": False, **result, "refresh_error": str(exc)}
        return {"skipped": False, **result}

    def restore(self, rollback_id: str, session_id: str | None = None) -> dict[str, Any]:
        """Re-enable messages invalidated by an earlier context rollback transaction."""
        return self.restore_many([rollback_id], session_id=session_id)

    def restore_many(self, rollback_ids: list[str], session_id: str | None = None) -> dict[str, Any]:
        """Re-enable several rollback records atomically and refresh the agent once."""
        rollback_ids = list(dict.fromkeys(value for value in rollback_ids if value))
        if not rollback_ids or not self.session_db:
            return {"restored": 0}
        restore_many = getattr(self.session_db, "restore_context_rollbacks", None)
        if callable(restore_many):
            restored = restore_many(rollback_ids)
        elif len(rollback_ids) == 1:
            restored = self.session_db.restore_context_rollback(rollback_ids[0])
        else:
            raise RuntimeError("Session database does not support atomic context restore")
        refresh_error = None
        if session_id and self.agent_matches_session(session_id):
            try:
                self.reload_agent_messages(session_id)
                self.reset_runtime_context()
            except Exception as exc:
                refresh_error = str(exc)
                logger.warning("Context was restored but the live agent could not be refreshed: %s", exc)
        result = {"restored": restored}
        if refresh_error:
            result["refresh_error"] = refresh_error
        return result

    def reapply_many(self, rollback_ids: list[str], session_id: str | None = None) -> dict[str, Any]:
        """Re-apply restored rollback records atomically during recovery."""
        rollback_ids = list(dict.fromkeys(value for value in rollback_ids if value))
        if not rollback_ids or not self.session_db:
            return {"reapplied": 0}
        reapply_many = getattr(self.session_db, "reapply_context_rollbacks", None)
        if not callable(reapply_many):
            raise RuntimeError("Session database does not support atomic context reapply")
        reapplied = reapply_many(rollback_ids)
        refresh_error = None
        if session_id and self.agent_matches_session(session_id):
            try:
                self.reload_agent_messages(session_id)
                self.reset_runtime_context()
            except Exception as exc:
                refresh_error = str(exc)
                logger.warning("Context was re-applied but the live agent could not be refreshed: %s", exc)
        result = {"reapplied": reapplied}
        if refresh_error:
            result["refresh_error"] = refresh_error
        return result

    def agent_matches_session(self, session_id: str) -> bool:
        """Return whether the live agent should be refreshed for a session change."""
        if not self.agent:
            return False
        agent_session_id = getattr(self.agent, "session_id", None)
        return not agent_session_id or agent_session_id == session_id

    def reload_agent_messages(self, session_id: str) -> None:
        """Reload the live conversation cache from the authoritative session DB."""
        if not self.agent or not self.session_db:
            return
        messages = self.session_db.get_messages_as_conversation(session_id)
        self.agent.messages = messages
        if hasattr(self.agent, "session_user_messages"):
            self.agent.session_user_messages = sum(
                1 for msg in messages if isinstance(msg, dict) and msg.get("role") == "user"
            )
        if (
            hasattr(self.agent, "_tool_call_ids_pending_visibility")
            and hasattr(self.session_db, "get_pending_tool_call_ids")
        ):
            self.agent._tool_call_ids_pending_visibility = (
                self.session_db.get_pending_tool_call_ids(session_id)
            )
            if hasattr(self.agent, "_tool_visibility_state_reliable"):
                self.agent._tool_visibility_state_reliable = True

    def reset_runtime_context(self) -> None:
        """Reset live-only state that depends on the previous message list."""
        reset_agent_runtime_context(self.agent)
