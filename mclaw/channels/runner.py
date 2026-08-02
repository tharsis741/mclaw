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
import inspect
import logging
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from mclaw.channels.base import AgentTurnResult, ChannelMessage
from mclaw.channels.inbound_pipeline import (
    InboundCapabilityError,
    InboundCapabilityPipeline,
)
from mclaw.providers.resolver import restore_session_runtime_context
from mclaw.providers.runtime import ProviderRuntimeContext
from mclaw.state import SessionDB
from mclaw.tools.interrupt import get_cancel_id, safe_cancel_trace

logger = logging.getLogger(__name__)

_CANCEL_UNWIND_GRACE_SECONDS = 1.0
_DEFAULT_MAX_PENDING_MESSAGES = 8
_DEFAULT_MAX_INBOUND_DOWNLOADS = 4
_PENDING_QUEUE_FULL_ERROR = "CHANNEL_QUEUE_FULL"
_PENDING_QUEUE_FULL_MESSAGE = "当前会话消息积压过多，请稍后重试。"

ReplyCallback = Callable[[ChannelMessage, AgentTurnResult], Awaitable[None]]
AgentEventCallback = Callable[[str, dict[str, Any]], Awaitable[None]]

if TYPE_CHECKING:
    from mclaw.agent.core import MClaw


@dataclass(slots=True)
class IngressReservation:
    """Preserve channel arrival order across concurrent media downloads."""

    session_id: str
    generation: int
    ready: asyncio.Event
    released: bool = False
    cancelled: bool = False


class IngressQueueFullError(RuntimeError):
    """Raised before media download when a session has no ingress capacity."""


def _consume_task_exception(task: asyncio.Task) -> None:
    """Observe exceptions from an isolated worker that nobody will await again."""
    if task.cancelled():
        return
    try:
        task.exception()
    except asyncio.CancelledError:
        pass


def _current_task_or_none() -> asyncio.Task[Any] | None:
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


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
        inbound_pipeline: InboundCapabilityPipeline | None = None,
        max_pending_messages: int = _DEFAULT_MAX_PENDING_MESSAGES,
        max_inbound_downloads: int = _DEFAULT_MAX_INBOUND_DOWNLOADS,
    ) -> None:
        self.startup_provider_runtime = provider_runtime
        self.config = config or {}
        self.enabled_toolsets = enabled_toolsets or self.config.get("toolsets", ["mclaw-required"])
        self.session_db = session_db or SessionDB()
        self.platform = platform
        self.max_cached_agents = max_cached_agents
        self.cache_idle_ttl_seconds = cache_idle_ttl_seconds
        self.inbound_pipeline = (
            inbound_pipeline if inbound_pipeline is not None else InboundCapabilityPipeline()
        )
        self.max_pending_messages = max(1, int(max_pending_messages))
        self.max_inbound_downloads = max(1, int(max_inbound_downloads))

        self._agents: OrderedDict[str, tuple["MClaw", float]] = OrderedDict()
        self._locks: dict[str, asyncio.Lock] = {}
        # Legacy tests and embedders may still assign a single ChannelMessage
        # directly. Queue helpers below normalize that value on first use.
        self._pending: dict[str, deque[ChannelMessage] | ChannelMessage] = {}
        self._active_agents: dict[str, "MClaw"] = {}
        self._closing_sessions: dict[str, asyncio.Event] = {}
        self._closing_agents: dict[str, "MClaw"] = {}
        self._cancelled_sessions: set[str] = set()
        self._event_callbacks: dict[str, tuple[asyncio.AbstractEventLoop, AgentEventCallback]] = {}
        self._ingress_queues: dict[str, deque[IngressReservation]] = {}
        self._ingress_generations: dict[str, int] = {}
        self._session_tasks: dict[str, asyncio.Task[Any]] = {}
        self._inbound_download_semaphore = asyncio.Semaphore(self.max_inbound_downloads)

    def reserve_ingress(self, session_id: str) -> IngressReservation:
        """Reserve bounded arrival order before an adapter downloads media."""
        queues = getattr(self, "_ingress_queues", None)
        if queues is None:
            queues = {}
            self._ingress_queues = queues
        queue = queues.setdefault(session_id, deque())
        pending = self._pending_count(session_id)
        lock = self._locks.get(session_id)
        # One additional reservation may occupy the currently free active slot.
        limit = self._pending_capacity() if lock and lock.locked() else self._pending_capacity() + 1
        if pending + len(queue) >= limit:
            raise IngressQueueFullError(_PENDING_QUEUE_FULL_MESSAGE)

        generations = getattr(self, "_ingress_generations", None)
        if generations is None:
            generations = {}
            self._ingress_generations = generations
        reservation = IngressReservation(
            session_id=session_id,
            generation=generations.get(session_id, 0),
            ready=asyncio.Event(),
        )
        queue.append(reservation)
        if len(queue) == 1:
            reservation.ready.set()
        return reservation

    def release_ingress(self, reservation: IngressReservation | None) -> None:
        """Release one reservation after it is admitted or abandoned."""
        if reservation is None or reservation.released:
            return
        reservation.released = True
        self._advance_ingress(reservation.session_id)

    async def await_ingress(self, reservation: IngressReservation) -> bool:
        """Wait until a reservation reaches the head, returning false if cancelled."""
        await reservation.ready.wait()
        generations = getattr(self, "_ingress_generations", {})
        return (
            not reservation.cancelled
            and not reservation.released
            and reservation.generation == generations.get(reservation.session_id, 0)
        )

    async def run_bounded_media_download(
        self,
        operation: Callable[[], Awaitable[Any]],
    ) -> Any:
        """Limit aggregate channel media downloads for this runtime."""
        semaphore = getattr(self, "_inbound_download_semaphore", None)
        if semaphore is None:
            maximum = max(1, int(getattr(self, "max_inbound_downloads", _DEFAULT_MAX_INBOUND_DOWNLOADS)))
            semaphore = asyncio.Semaphore(maximum)
            self._inbound_download_semaphore = semaphore
        async with semaphore:
            return await operation()

    def _advance_ingress(self, session_id: str) -> None:
        queues = getattr(self, "_ingress_queues", {})
        queue = queues.get(session_id)
        if not queue:
            queues.pop(session_id, None)
            return
        while queue and queue[0].released:
            queue.popleft()
        if queue:
            queue[0].ready.set()
        else:
            queues.pop(session_id, None)

    def _cancel_ingress(self, session_id: str) -> bool:
        queues = getattr(self, "_ingress_queues", {})
        queue = queues.pop(session_id, deque())
        generations = getattr(self, "_ingress_generations", None)
        if generations is None:
            generations = {}
            self._ingress_generations = generations
        generations[session_id] = generations.get(session_id, 0) + 1
        for reservation in queue:
            reservation.cancelled = True
            reservation.released = True
            reservation.ready.set()
        return bool(queue)

    def get_status(self, session_id: str) -> str:
        """Return the coarse execution state for a channel session."""
        if session_id in self._active_agents or session_id in getattr(self, "_session_tasks", {}):
            return "running"
        if session_id in self._closing_sessions:
            return "stopping"
        if session_id in self._pending or session_id in getattr(self, "_ingress_queues", {}):
            return "queued"
        return "idle"

    def interrupt(self, session_id: str) -> bool:
        """Cancel active work and discard queued input for one session."""
        had_pending = self._clear_pending(session_id)
        had_ingress = self._cancel_ingress(session_id)
        task = getattr(self, "_session_tasks", {}).get(session_id)
        agent = self._active_agents.get(session_id)
        had_work = bool(had_pending or had_ingress or task or agent)
        if not had_work:
            return False
        if task or agent:
            self._cancelled_sessions.add(session_id)
        if agent:
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
        current = _current_task_or_none()
        if task is not None and task is not current and not task.done():
            task.cancel()
        return had_work

    def interrupt_all(self) -> int:
        """Interrupt all active or queued channel sessions."""
        interrupted = 0
        sessions = (
            set(self._pending)
            | set(self._active_agents)
            | set(getattr(self, "_ingress_queues", {}))
            | set(getattr(self, "_session_tasks", {}))
        )
        for session_id in list(sessions):
            if self.interrupt(session_id):
                interrupted += 1
        return interrupted

    def reset_session(self, session_id: str) -> bool:
        """Cancel active/pending work and evict cached agent for a superseded session."""
        had_pending = self._clear_pending(session_id)
        had_ingress = self._cancel_ingress(session_id)
        task = getattr(self, "_session_tasks", {}).get(session_id)
        agent = self._active_agents.get(session_id)
        had_work = bool(had_pending or had_ingress or task or agent)
        if agent:
            self._cancelled_sessions.add(session_id)
            agent.interrupt()
        if task is not None and task is not _current_task_or_none() and not task.done():
            self._cancelled_sessions.add(session_id)
            task.cancel()
        if not agent and not task:
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

    def _pending_capacity(self) -> int:
        """Return a valid queue capacity for initialized and legacy runners."""
        raw = getattr(self, "max_pending_messages", _DEFAULT_MAX_PENDING_MESSAGES)
        try:
            return max(1, int(raw))
        except (TypeError, ValueError):
            return _DEFAULT_MAX_PENDING_MESSAGES

    def _pending_count(self, session_id: str) -> int:
        current = self._pending.get(session_id)
        if isinstance(current, ChannelMessage):
            return 1
        if isinstance(current, (deque, list, tuple)):
            return sum(isinstance(item, ChannelMessage) for item in current)
        return 0

    def _clear_pending(self, session_id: str) -> bool:
        """Discard a session FIFO and remove any M-Claw-owned cached files."""
        current = self._pending.pop(session_id, None)
        if isinstance(current, ChannelMessage):
            messages = [current]
        elif isinstance(current, (deque, list, tuple)):
            messages = [item for item in current if isinstance(item, ChannelMessage)]
        else:
            messages = []
        for message in messages:
            self._discard_message_cache(message)
        return bool(messages)

    @staticmethod
    def _discard_message_cache(message: ChannelMessage) -> None:
        """Delete only files explicitly marked as M-Claw-managed channel cache."""
        for attachment in message.attachments:
            metadata = attachment.metadata if isinstance(attachment.metadata, dict) else {}
            if metadata.get("managed_cache") is not True or not attachment.path:
                continue
            try:
                Path(attachment.path).unlink(missing_ok=True)
            except OSError:
                logger.debug(
                    "discarded channel attachment cleanup failed channel=%s",
                    message.source.channel,
                )

    def _enqueue_pending(self, session_id: str, message: ChannelMessage) -> bool:
        """Append one pending message without discarding older accepted work."""
        current = self._pending.get(session_id)
        if isinstance(current, deque):
            queue = current
        elif isinstance(current, ChannelMessage):
            queue = deque([current])
        elif isinstance(current, (list, tuple)):
            queue = deque(item for item in current if isinstance(item, ChannelMessage))
        else:
            queue = deque()
        if len(queue) >= self._pending_capacity():
            return False
        queue.append(message)
        self._pending[session_id] = queue
        return True

    def _dequeue_pending(self, session_id: str) -> ChannelMessage | None:
        """Remove the oldest pending message, accepting the legacy single-slot shape."""
        current = self._pending.get(session_id)
        if isinstance(current, ChannelMessage):
            self._pending.pop(session_id, None)
            return current
        if isinstance(current, deque):
            if not current:
                self._pending.pop(session_id, None)
                return None
            message = current.popleft()
            if not current:
                self._pending.pop(session_id, None)
            return message
        if isinstance(current, list):
            if not current:
                self._pending.pop(session_id, None)
                return None
            message = current.pop(0)
            if not current:
                self._pending.pop(session_id, None)
            return message if isinstance(message, ChannelMessage) else None
        self._pending.pop(session_id, None)
        return None

    async def _process_inbound_message(self, message: ChannelMessage) -> ChannelMessage:
        """Run the injected pipeline while retaining compatibility with simple fakes."""
        pipeline = getattr(self, "inbound_pipeline", None)
        if pipeline is None:
            return message
        processor = getattr(pipeline, "process", None)
        if not callable(processor):
            if not callable(pipeline):
                raise TypeError("Inbound pipeline is not callable")
            processor = pipeline
        outcome = processor(message)
        if inspect.isawaitable(outcome):
            outcome = await outcome
        if not isinstance(outcome, ChannelMessage):
            raise TypeError("Inbound pipeline must return ChannelMessage")
        return outcome

    async def handle_message(
        self,
        *,
        message: ChannelMessage,
        session_id: str,
        conversation_history: list[dict] | None,
        reply_callback: ReplyCallback,
        extra_system: str = "",
        ingress_reservation: IngressReservation | None = None,
    ) -> AgentTurnResult:
        """Serialize turns per session and drain its bounded pending FIFO."""
        if ingress_reservation is not None:
            if ingress_reservation.session_id != session_id:
                self._discard_message_cache(message)
                self.release_ingress(ingress_reservation)
                raise ValueError("Ingress reservation belongs to a different session")
            try:
                admitted = await self.await_ingress(ingress_reservation)
            except BaseException:
                self._discard_message_cache(message)
                self.release_ingress(ingress_reservation)
                raise
            if not admitted:
                self._discard_message_cache(message)
                self.release_ingress(ingress_reservation)
                return AgentTurnResult(session_id=session_id, interrupted=True)

        lock = self._locks.setdefault(session_id, asyncio.Lock())
        if lock.locked():
            if self._enqueue_pending(session_id, message):
                self.release_ingress(ingress_reservation)
                return AgentTurnResult(session_id=session_id, queued=True)
            result = AgentTurnResult(
                session_id=session_id,
                final_response=_PENDING_QUEUE_FULL_MESSAGE,
                error=_PENDING_QUEUE_FULL_ERROR,
                raw_result={"queue_full": True},
            )
            self.release_ingress(ingress_reservation)
            self._discard_message_cache(message)
            await reply_callback(message, result)
            return result

        last_result = AgentTurnResult(session_id=session_id)
        try:
            await lock.acquire()
        except BaseException:
            self._discard_message_cache(message)
            self.release_ingress(ingress_reservation)
            raise
        if ingress_reservation is not None:
            try:
                admitted = await self.await_ingress(ingress_reservation)
            except BaseException:
                self._discard_message_cache(message)
                self.release_ingress(ingress_reservation)
                lock.release()
                raise
            if not admitted:
                self._discard_message_cache(message)
                self.release_ingress(ingress_reservation)
                lock.release()
                return AgentTurnResult(session_id=session_id, interrupted=True)
        self.release_ingress(ingress_reservation)
        session_tasks = getattr(self, "_session_tasks", None)
        if session_tasks is None:
            session_tasks = {}
            self._session_tasks = session_tasks
        owner_task = _current_task_or_none()
        if owner_task is not None:
            session_tasks[session_id] = owner_task
        current: ChannelMessage | None = message
        try:
            history = conversation_history
            while current is not None:
                try:
                    processed = await self._process_inbound_message(current)
                except asyncio.CancelledError:
                    self._discard_message_cache(current)
                    raise
                except InboundCapabilityError as exc:
                    logger.warning(
                        "channel inbound capability failed session=%s capability=%s "
                        "code=%s",
                        session_id,
                        exc.capability or "?",
                        exc.code,
                    )
                    last_result = AgentTurnResult(
                        session_id=session_id,
                        final_response=exc.safe_message,
                        error=exc.code,
                        raw_result={
                            "capability_error": {
                                "code": exc.code,
                                "capability": exc.capability,
                            }
                        },
                    )
                    self._discard_message_cache(current)
                    await reply_callback(current, last_result)
                    current = self._dequeue_pending(session_id)
                    continue
                except Exception as exc:
                    capability_error = InboundCapabilityError(detail=str(exc))
                    logger.exception(
                        "channel inbound processing failed session=%s",
                        session_id,
                    )
                    last_result = AgentTurnResult(
                        session_id=session_id,
                        final_response=capability_error.safe_message,
                        error=capability_error.code,
                        raw_result={
                            "capability_error": {
                                "code": capability_error.code,
                                "capability": capability_error.capability,
                            }
                        },
                    )
                    self._discard_message_cache(current)
                    await reply_callback(current, last_result)
                    current = self._dequeue_pending(session_id)
                    continue
                last_result = await self._run_single_turn(
                    message=processed,
                    session_id=session_id,
                    conversation_history=history,
                    extra_system=extra_system,
                )
                if session_id in self._cancelled_sessions:
                    self._cancelled_sessions.discard(session_id)
                    self._discard_message_cache(current)
                    self._clear_pending(session_id)
                    break
                await reply_callback(processed, last_result)
                history = last_result.raw_result.get("messages") or history
                current = self._dequeue_pending(session_id)
        except BaseException:
            if current is not None:
                self._discard_message_cache(current)
            self._clear_pending(session_id)
            self._cancelled_sessions.discard(session_id)
            raise
        finally:
            if owner_task is not None and session_tasks.get(session_id) is owner_task:
                session_tasks.pop(session_id, None)
            lock.release()
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
                self._clear_pending(session_id)
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
