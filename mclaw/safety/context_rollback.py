"""Context rollback coordination helpers."""

from __future__ import annotations

from typing import Any, Dict, Optional


class ContextRollbackManager:
    """Soft-invalidate or restore chat context around filesystem rollback."""

    def __init__(self, session_db: Any = None, agent: Any = None):
        self.session_db = session_db
        self.agent = agent

    def apply(
        self,
        *,
        session_id: str,
        marker_message_id: Optional[int],
        mode: str,
        checkpoint_hash: str = None,
        operation_id: str = None,
        turn_id: str = None,
        metadata: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        mode = (mode or "soft").lower()
        if mode in {"off", "none", "false", "0", "fs-only"}:
            return {"skipped": True, "reason": "fs-only", "invalidated": 0, "rollback_id": None}
        if marker_message_id is None:
            return {"skipped": True, "reason": "missing marker", "invalidated": 0, "rollback_id": None}
        if not self.session_db:
            return {"skipped": True, "reason": "no session db", "invalidated": 0, "rollback_id": None}
        if mode == "soft" and hasattr(self.session_db, "invalidate_operation_context"):
            result = self.session_db.invalidate_operation_context(
                session_id,
                marker_message_id,
                turn_id=turn_id,
                operation_id=operation_id,
                reason="filesystem rollback",
                rollback_mode=mode,
                checkpoint_hash=checkpoint_hash,
                metadata=metadata or {},
            )
        elif hasattr(self.session_db, "invalidate_messages_after"):
            result = self.session_db.invalidate_messages_after(
                session_id,
                marker_message_id,
                reason="filesystem rollback",
                rollback_mode=mode,
                checkpoint_hash=checkpoint_hash,
                operation_id=operation_id,
                metadata=metadata or {},
            )
        else:
            return {"skipped": True, "reason": "no session rollback api", "invalidated": 0, "rollback_id": None}
        if self.agent_matches_session(session_id):
            self.reload_agent_messages(session_id)
            self.reset_runtime_context()
        return {"skipped": False, **result}

    def restore(self, rollback_id: str, session_id: str = None) -> Dict[str, Any]:
        if not rollback_id or not self.session_db or not hasattr(self.session_db, "restore_context_rollback"):
            return {"restored": 0}
        restored = self.session_db.restore_context_rollback(rollback_id)
        if session_id and self.agent_matches_session(session_id):
            self.reload_agent_messages(session_id)
            self.reset_runtime_context()
        return {"restored": restored}

    def agent_matches_session(self, session_id: str) -> bool:
        if not self.agent:
            return False
        agent_session_id = getattr(self.agent, "session_id", None)
        return not agent_session_id or agent_session_id == session_id

    def reload_agent_messages(self, session_id: str) -> None:
        if not self.agent or not self.session_db or not hasattr(self.session_db, "get_messages_as_conversation"):
            return
        self.agent.messages = self.session_db.get_messages_as_conversation(session_id)

    def reset_runtime_context(self) -> None:
        if not self.agent:
            return
        compressor = getattr(self.agent, "context_compressor", None)
        if compressor:
            for name, value in [
                ("_previous_summary", None),
                ("_compressed_this_turn", False),
                ("compression_count", 0),
                ("last_prompt_tokens", 0),
                ("last_completion_tokens", 0),
            ]:
                if hasattr(compressor, name):
                    try:
                        setattr(compressor, name, value)
                    except Exception:
                        pass
        if hasattr(self.agent, "_recalled_memory"):
            self.agent._recalled_memory = ""
