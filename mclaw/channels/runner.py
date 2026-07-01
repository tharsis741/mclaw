# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared runner that bridges channel messages into agent sessions.

Channel runtimes use this boundary to create or resume sessions, forward user
messages, and send final or intermediate responses back through channel-specific
outbound handlers.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from mclaw.channels.base import AgentTurnResult, ChannelMessage
from mclaw.state import SessionDB

logger = logging.getLogger(__name__)

ReplyCallback = Callable[[ChannelMessage, AgentTurnResult], Awaitable[None]]
AgentEventCallback = Callable[[str, dict[str, Any]], Awaitable[None]]

if TYPE_CHECKING:
    from mclaw.agent.core import MClaw


class AgentRunner:
    """Create, cache, and serialize MClaw turns for channel runtimes."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str = "",
        api_mode: str = "chat_completions",
        provider: str = "",
        config: dict | None = None,
        enabled_toolsets: list[str] | None = None,
        session_db: SessionDB | None = None,
        platform: str = "channel",
        max_cached_agents: int = 128,
        cache_idle_ttl_seconds: float = 3600.0,
    ) -> None:
        self.model = model
        self.api_key = api_key
        self.base_url = base_url
        self.api_mode = api_mode
        self.provider = provider
        self.config = config or {}
        self.enabled_toolsets = enabled_toolsets or self.config.get("toolsets", ["mclaw-required"])
        self.session_db = session_db or SessionDB()
        self.platform = platform
        self.max_cached_agents = max_cached_agents
        self.cache_idle_ttl_seconds = cache_idle_ttl_seconds

        self._agents: OrderedDict[str, tuple["MClaw", float]] = OrderedDict()
        self._locks: dict[str, asyncio.Lock] = {}
        self._pending: dict[str, ChannelMessage] = {}
        self._active_agents: dict[str, "MClaw"] = {}
        self._cancelled_sessions: set[str] = set()
        self._event_callbacks: dict[str, tuple[asyncio.AbstractEventLoop, AgentEventCallback]] = {}

    def get_status(self, session_id: str) -> str:
        """Return the coarse execution state for a channel session."""
        if session_id in self._active_agents:
            return "running"
        if session_id in self._pending:
            return "queued"
        return "idle"

    def interrupt(self, session_id: str) -> bool:
        """Cancel active work and discard queued input for one session."""
        self._pending.pop(session_id, None)
        agent = self._active_agents.get(session_id)
        if not agent:
            return False
        self._cancelled_sessions.add(session_id)
        agent.interrupt()
        return True

    def interrupt_all(self) -> int:
        """Interrupt all active or queued channel sessions."""
        interrupted = 0
        for session_id in list(set(self._pending) | set(self._active_agents)):
            if self.interrupt(session_id):
                interrupted += 1
            else:
                self._pending.pop(session_id, None)
        return interrupted

    def reset_session(self, session_id: str) -> bool:
        """Cancel active/pending work and evict cached agent for a superseded session."""
        had_work = session_id in self._pending or session_id in self._active_agents
        self._pending.pop(session_id, None)
        agent = self._active_agents.get(session_id)
        if agent:
            self._cancelled_sessions.add(session_id)
            agent.interrupt()
        else:
            self._cancelled_sessions.discard(session_id)
            self._agents.pop(session_id, None)
        return had_work

    def bind_session_events(
        self,
        *,
        session_id: str,
        loop: asyncio.AbstractEventLoop,
        callback: AgentEventCallback,
    ) -> None:
        """Attach an async event sink for agent streaming callbacks."""
        self._event_callbacks[session_id] = (loop, callback)

    async def handle_message(
        self,
        *,
        message: ChannelMessage,
        session_id: str,
        conversation_history: list[dict] | None,
        reply_callback: ReplyCallback,
        extra_system: str = "",
    ) -> AgentTurnResult:
        """Serialize turns per session while coalescing one pending message."""
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        if lock.locked():
            self._pending[session_id] = message
            return AgentTurnResult(session_id=session_id, queued=True)

        last_result = AgentTurnResult(session_id=session_id)
        async with lock:
            current: ChannelMessage | None = message
            history = conversation_history
            while current is not None:
                last_result = await self._run_single_turn(
                    message=current,
                    session_id=session_id,
                    conversation_history=history,
                    extra_system=extra_system,
                )
                if session_id in self._cancelled_sessions:
                    self._cancelled_sessions.discard(session_id)
                    self._pending.pop(session_id, None)
                    break
                await reply_callback(current, last_result)
                history = last_result.raw_result.get("messages") or history
                current = self._pending.pop(session_id, None)
        return last_result

    async def _run_single_turn(
        self,
        *,
        message: ChannelMessage,
        session_id: str,
        conversation_history: list[dict] | None,
        extra_system: str = "",
    ) -> AgentTurnResult:
        """Run one MClaw conversation turn on a worker thread."""
        agent = self._get_or_create_agent(session_id=session_id)
        self._active_agents[session_id] = agent
        try:
            result = await asyncio.to_thread(
                agent.run_conversation,
                user_message=message.text,
                conversation_history=conversation_history,
                disable_tools=False,
                extra_system=extra_system,
            )
            return AgentTurnResult(
                session_id=session_id,
                final_response=str(result.get("final_response") or ""),
                interrupted=bool(result.get("interrupted")),
                raw_result=result,
            )
        except Exception as exc:
            logger.exception("channel agent turn failed session=%s: %s", session_id, exc)
            return AgentTurnResult(session_id=session_id, error=str(exc))
        finally:
            self._active_agents.pop(session_id, None)
            if session_id in self._cancelled_sessions:
                self._agents.pop(session_id, None)
            else:
                self._touch_agent(session_id, agent)

    def _get_or_create_agent(self, *, session_id: str) -> "MClaw":
        """Return an existing session agent or create one bound to channel config."""
        from mclaw.agent.core import MClaw

        now = time.time()
        self._evict_idle(now)
        cached = self._agents.get(session_id)
        if cached:
            agent, _ = cached
            self._agents.move_to_end(session_id)
            self._agents[session_id] = (agent, now)
            return agent
        agent = MClaw(
            model=self.model,
            api_key=self.api_key,
            base_url=self.base_url,
            api_mode=self.api_mode,
            provider=self.provider,
            session_db=self.session_db,
            session_id=session_id,
            enabled_toolsets=self.enabled_toolsets,
            max_iterations=int(self.config.get("agent", {}).get("max_turns", 90)),
            platform=self.platform,
            config=self.config,
            event_callback=lambda event, _session_id=session_id: self._emit_agent_event(_session_id, event),
        )
        self._agents[session_id] = (agent, now)
        self._enforce_cache_cap()
        return agent

    def _emit_agent_event(self, session_id: str, event: dict[str, Any]) -> None:
        """Bridge synchronous agent callbacks back into the channel event loop."""
        bound = self._event_callbacks.get(session_id)
        if not bound:
            return
        loop, callback = bound
        if loop.is_closed():
            return
        future = asyncio.run_coroutine_threadsafe(callback(session_id, event), loop)
        future.add_done_callback(lambda fut: self._log_event_callback_error(session_id, fut))

    def _log_event_callback_error(self, session_id: str, future: asyncio.Future) -> None:
        try:
            future.result()
        except Exception as exc:
            logger.warning("channel agent event callback failed session=%s: %s", session_id, exc)

    def _touch_agent(self, session_id: str, agent: "MClaw") -> None:
        """Mark a cached agent as recently used."""
        self._agents[session_id] = (agent, time.time())
        self._agents.move_to_end(session_id)

    def _evict_idle(self, now: float) -> None:
        """Drop inactive cached agents whose idle TTL has expired."""
        if self.cache_idle_ttl_seconds <= 0:
            return
        stale = [
            sid for sid, (_agent, ts) in self._agents.items()
            if sid not in self._active_agents and now - ts > self.cache_idle_ttl_seconds
        ]
        for sid in stale:
            self._agents.pop(sid, None)

    def _enforce_cache_cap(self) -> None:
        """Keep the agent cache under its configured capacity."""
        checked = 0
        while len(self._agents) > self.max_cached_agents and checked < len(self._agents):
            sid, _ = next(iter(self._agents.items()))
            if sid in self._active_agents:
                self._agents.move_to_end(sid)
                checked += 1
                continue
            self._agents.pop(sid, None)
            checked = 0
