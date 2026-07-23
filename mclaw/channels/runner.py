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
import threading
import time
from collections import OrderedDict
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from mclaw.channels.base import AgentTurnResult, ChannelMessage
from mclaw.providers.resolver import restore_session_runtime_context
from mclaw.providers.runtime import ProviderRuntimeContext
from mclaw.state import SessionDB
from mclaw.tools.interrupt import get_cancel_id, safe_cancel_trace

logger = logging.getLogger(__name__)

_CANCEL_UNWIND_GRACE_SECONDS = 1.0

ReplyCallback = Callable[[ChannelMessage, AgentTurnResult], Awaitable[None]]
AgentEventCallback = Callable[[str, dict[str, Any]], Awaitable[None]]

if TYPE_CHECKING:
    from mclaw.agent.core import MClaw


def _consume_task_exception(task: asyncio.Task) -> None:
    """Observe exceptions from an isolated worker that nobody will await again."""
    if task.cancelled():
        return
    try:
        task.exception()
    except asyncio.CancelledError:
        pass


class AgentRunner:
    """Create, cache, and serialize MClaw turns for channel runtimes."""

    def __init__(
        self,
        *,
        provider_runtime: ProviderRuntimeContext,
        config: dict | None = None,
        enabled_toolsets: list[str] | None = None,
        session_db: SessionDB | None = None,
        platform: str = "channel",
        max_cached_agents: int = 128,
        cache_idle_ttl_seconds: float = 3600.0,
    ) -> None:
        self.startup_provider_runtime = provider_runtime
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
        self._closing_sessions: dict[str, asyncio.Event] = {}
        self._closing_agents: dict[str, "MClaw"] = {}
        self._cancelled_sessions: set[str] = set()
        self._event_callbacks: dict[str, tuple[asyncio.AbstractEventLoop, AgentEventCallback]] = {}

    def get_status(self, session_id: str) -> str:
        """Return the coarse execution state for a channel session."""
        if session_id in self._active_agents:
            return "running"
        if session_id in self._closing_sessions:
            return "stopping"
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
        event = getattr(agent, "current_turn_cancel_event", lambda: None)()
        agent.interrupt()
        safe_cancel_trace(
            lambda: logger.warning(
                "[CANCEL_TRACE] channel_cancel_request cancel_id=%s session=%s "
                "trigger=channel_stop",
                get_cancel_id(event),
                session_id,
            )
        )
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

    def session_replacement_block_reason(self, session_id: str) -> str | None:
        """Refuse a new session id until the superseded session is safe to release."""
        agent = self._active_agents.get(session_id)
        if agent is None:
            agent = self._closing_agents.get(session_id)
        if agent is None:
            cached = self._agents.get(session_id)
            agent = cached[0] if cached else None
        if agent is not None:
            block_reason = getattr(agent, "_replacement_block_reason", None)
            if callable(block_reason):
                reason = block_reason()
                if reason:
                    return str(reason)
        closing_event = self._closing_sessions.get(session_id)
        if closing_event is not None and not closing_event.is_set():
            return (
                "The previous turn is still shutting down; wait for cleanup "
                "before creating a new session"
            )
        return None

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
        agent: MClaw | None = None
        cancel_event = None
        worker_task: asyncio.Task | None = None
        worker_finished = threading.Event()
        discard_agent = False
        try:
            closing_error = await self._wait_for_closing_session(session_id)
            if closing_error:
                return AgentTurnResult(session_id=session_id, error=closing_error)
            agent = self._get_or_create_agent(session_id=session_id)
            begin_turn = getattr(agent, "begin_turn", None)
            if callable(begin_turn):
                cancel_event = begin_turn()
            self._active_agents[session_id] = agent

            def _run_agent() -> dict:
                try:
                    return agent.run_conversation(
                        user_message=message.text,
                        conversation_history=conversation_history,
                        disable_tools=False,
                        extra_system=extra_system,
                    )
                finally:
                    worker_finished.set()

            worker_task = asyncio.create_task(asyncio.to_thread(_run_agent))
            worker_task.add_done_callback(_consume_task_exception)
            try:
                result = await asyncio.shield(worker_task)
            except asyncio.CancelledError:
                discard_agent = True
                self._pending.pop(session_id, None)
                agent.interrupt()
                safe_cancel_trace(
                    lambda: logger.warning(
                        "[CANCEL_TRACE] channel_cancel_request cancel_id=%s session=%s "
                        "trigger=asyncio_task_cancel",
                        get_cancel_id(cancel_event),
                        session_id,
                    )
                )
                if hasattr(agent, "_event_callback"):
                    agent._event_callback = None
                try:
                    await asyncio.wait_for(
                        asyncio.shield(worker_task),
                        timeout=_CANCEL_UNWIND_GRACE_SECONDS,
                    )
                except asyncio.TimeoutError:
                    pass
                except Exception:
                    # Preserve the caller's cancellation; the abandoned worker
                    # is isolated below and its result is intentionally dropped.
                    pass
                raise
            return AgentTurnResult(
                session_id=session_id,
                final_response=(
                    str(result.get("abort_message") or "")
                    if result.get("abort_reason")
                    in {"tool_timeout", "tool_completion_unknown"}
                    else str(result.get("final_response") or "")
                ),
                interrupted=bool(result.get("interrupted")),
                raw_result=result,
            )
        except Exception as exc:
            logger.exception("channel agent turn failed session=%s: %s", session_id, exc)
            return AgentTurnResult(session_id=session_id, error=str(exc))
        finally:
            self._active_agents.pop(session_id, None)
            if agent is not None:
                end_turn = getattr(agent, "end_turn", None)
                deferred_end = discard_agent and not worker_finished.is_set()
                if not deferred_end and cancel_event is not None and callable(end_turn):
                    end_turn(cancel_event)

                has_outstanding = getattr(agent, "_has_outstanding_turn_workers", None)
                outstanding = bool(has_outstanding()) if callable(has_outstanding) else False
                if deferred_end or outstanding:
                    self._start_closing_session_fence(
                        session_id=session_id,
                        agent=agent,
                        worker_finished=worker_finished,
                        end_turn=(
                            (lambda: end_turn(cancel_event))
                            if deferred_end
                            and cancel_event is not None
                            and callable(end_turn)
                            else None
                        ),
                    )

                if discard_agent or session_id in self._cancelled_sessions:
                    self._agents.pop(session_id, None)
                else:
                    self._touch_agent(session_id, agent)
                if discard_agent:
                    self._cancelled_sessions.discard(session_id)

    def _start_closing_session_fence(
        self,
        *,
        session_id: str,
        agent: "MClaw",
        worker_finished: threading.Event,
        end_turn: Callable[[], None] | None,
    ) -> None:
        """Keep a session closed until its abandoned worker and operations drain."""
        closing_sessions = getattr(self, "_closing_sessions", None)
        if closing_sessions is None:
            closing_sessions = {}
            self._closing_sessions = closing_sessions
        closing_event = asyncio.Event()
        closing_sessions[session_id] = closing_event
        closing_agents = getattr(self, "_closing_agents", None)
        if closing_agents is None:
            closing_agents = {}
            self._closing_agents = closing_agents
        closing_agents[session_id] = agent
        loop = asyncio.get_running_loop()

        def _finish_close() -> None:
            worker_finished.wait()
            if end_turn is not None:
                try:
                    end_turn()
                except Exception:
                    safe_cancel_trace(
                        lambda: logger.exception(
                            "channel abandoned turn cleanup failed session=%s",
                            session_id,
                        )
                    )
            drained = getattr(agent, "_turn_workers_drained", None)
            if isinstance(drained, threading.Event):
                drained.wait()
            else:
                has_outstanding = getattr(agent, "_has_outstanding_turn_workers", None)
                while callable(has_outstanding) and has_outstanding():
                    time.sleep(0.05)
            try:
                loop.call_soon_threadsafe(
                    self._release_closing_session,
                    session_id,
                    closing_event,
                )
            except RuntimeError:
                # The owning event loop has already shut down.
                pass

        threading.Thread(
            target=_finish_close,
            daemon=True,
            name="mclaw-channel-cancel-cleanup",
        ).start()
        safe_cancel_trace(
            lambda: logger.warning(
                "[CANCEL_TRACE] channel_fence_start cancel_id=%s session=%s workers=%s",
                get_cancel_id(getattr(agent, "_workspace_abort_event", None)),
                session_id,
                getattr(agent, "_turn_worker_log_snapshot", lambda: "[]")(),
            )
        )

    async def _wait_for_closing_session(self, session_id: str) -> str | None:
        """Wait for transient cleanup, but fail fast on a persistent safety fence."""
        while True:
            closing_event = getattr(self, "_closing_sessions", {}).get(session_id)
            if closing_event is None or closing_event.is_set():
                return None
            closing_agent = getattr(self, "_closing_agents", {}).get(session_id)
            persistent_reason = getattr(
                closing_agent,
                "_persistent_turn_worker_reason",
                None,
            )
            if callable(persistent_reason):
                reason = persistent_reason()
                if reason:
                    safe_cancel_trace(
                        lambda: logger.warning(
                            "[CANCEL_TRACE] channel_fence_block cancel_id=%s session=%s "
                            "persistent=true reason=%s",
                            get_cancel_id(
                                getattr(closing_agent, "_workspace_abort_event", None)
                            ),
                            session_id,
                            reason,
                        )
                    )
                    return reason
            try:
                await asyncio.wait_for(closing_event.wait(), timeout=0.1)
            except asyncio.TimeoutError:
                continue
            return None

    def _release_closing_session(
        self,
        session_id: str,
        closing_event: asyncio.Event,
    ) -> None:
        """Wake queued work only after the abandoned OS worker has exited."""
        closing_sessions = getattr(self, "_closing_sessions", {})
        if closing_sessions.get(session_id) is not closing_event:
            return
        closing_sessions.pop(session_id, None)
        getattr(self, "_closing_agents", {}).pop(session_id, None)
        closing_event.set()
        safe_cancel_trace(
            lambda: logger.info(
                "[CANCEL_TRACE] channel_fence_release session=%s",
                session_id,
            )
        )

    def _get_or_create_agent(self, *, session_id: str) -> "MClaw":
        """Return an existing session agent or create one bound to channel config."""
        from mclaw.agent.core import MClaw

        now = time.time()
        self._evict_idle(now)
        provider_runtime = self._runtime_for_session(session_id)
        cached = self._agents.get(session_id)
        if cached:
            agent, _ = cached
            if agent.provider_runtime.fingerprint() == provider_runtime.fingerprint():
                self._agents.move_to_end(session_id)
                self._agents[session_id] = (agent, now)
                return agent
            self._agents.pop(session_id, None)
        agent = MClaw(
            provider_runtime=provider_runtime,
            session_db=self.session_db,
            session_id=session_id,
            enabled_toolsets=self.enabled_toolsets,
            platform=self.platform,
            config=self.config,
            event_callback=lambda event, _session_id=session_id: self._emit_agent_event(_session_id, event),
        )
        self._agents[session_id] = (agent, now)
        self._enforce_cache_cap()
        return agent

    def _runtime_for_session(self, session_id: str) -> ProviderRuntimeContext:
        """Restore session model state before consulting the in-memory agent cache."""
        snapshot = self.session_db.get_model_config(session_id)
        row = self.session_db.get_session(session_id) or {}
        return restore_session_runtime_context(
            snapshot,
            config=self.config,
            row_model=str(row.get("model") or ""),
            fallback_context=self.startup_provider_runtime,
        )

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
