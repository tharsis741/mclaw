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
import concurrent.futures
import copy
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


class _Unset:
    __slots__ = ()


_UNSET = _Unset()


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
        session_db: SessionDB | None | _Unset = _UNSET,
        platform: str = "channel",
        max_cached_agents: int = 128,
        cache_idle_ttl_seconds: float = 3600.0,
        inbound_pipeline: InboundCapabilityPipeline | None = None,
        max_pending_messages: int = _DEFAULT_MAX_PENDING_MESSAGES,
        max_inbound_downloads: int = _DEFAULT_MAX_INBOUND_DOWNLOADS,
        agent_system_prompt: str = "",
        agent_workspace_root: str | None = None,
        skip_memory: bool = False,
        disable_tools: bool = False,
        advance_background_review: bool = True,
        call_source: str = "channel",
        apply_inbound_pipeline: bool = True,
    ) -> None:
        for name, value in (
            ("skip_memory", skip_memory),
            ("disable_tools", disable_tools),
            ("advance_background_review", advance_background_review),
            ("apply_inbound_pipeline", apply_inbound_pipeline),
        ):
            if type(value) is not bool:
                raise TypeError(f"{name} must be a bool")
        if not isinstance(agent_system_prompt, str):
            raise TypeError("agent_system_prompt must be a string")
        if agent_workspace_root is not None and not isinstance(
            agent_workspace_root, str
        ):
            raise TypeError("agent_workspace_root must be a string or None")
        if not isinstance(call_source, str) or not call_source:
            raise TypeError("call_source must be a non-empty string")
        self.startup_provider_runtime = provider_runtime
        self.config = config or {}
        self.enabled_toolsets = enabled_toolsets or self.config.get("toolsets", ["mclaw-required"])
        self._owns_session_db = session_db is _UNSET
        self.session_db = SessionDB() if self._owns_session_db else session_db
        self.platform = platform
        self.max_cached_agents = max_cached_agents
        self.cache_idle_ttl_seconds = cache_idle_ttl_seconds
        self.inbound_pipeline = (
            (
                inbound_pipeline
                if inbound_pipeline is not None
                else InboundCapabilityPipeline()
            )
            if apply_inbound_pipeline
            else None
        )
        self.max_pending_messages = max(1, int(max_pending_messages))
        self.max_inbound_downloads = max(1, int(max_inbound_downloads))
        self.agent_system_prompt = agent_system_prompt
        self.agent_workspace_root = (
            str(Path(agent_workspace_root)) if agent_workspace_root else None
        )
        self.skip_memory = skip_memory
        self.disable_tools = disable_tools
        self.advance_background_review = advance_background_review
        self.call_source = call_source
        self.apply_inbound_pipeline = apply_inbound_pipeline

        self._agents: OrderedDict[str, tuple["MClaw", float]] = OrderedDict()
        self._locks: dict[str, asyncio.Lock] = {}
        # Legacy tests and embedders may still assign a single ChannelMessage
        # directly. Queue helpers below normalize that value on first use.
        self._pending: dict[str, deque[ChannelMessage] | ChannelMessage] = {}
        self._active_agents: dict[str, "MClaw"] = {}
        self._closing_sessions: dict[str, asyncio.Event] = {}
        self._closing_agents: dict[str, "MClaw"] = {}
        self._retiring_sessions: set[str] = set()
        self._retiring_agents: dict[str, "MClaw"] = {}
        self._cancelled_sessions: set[str] = set()
        self._event_callbacks: dict[str, tuple[asyncio.AbstractEventLoop, AgentEventCallback]] = {}
        self._event_callback_futures: dict[
            str, set[concurrent.futures.Future[Any]]
        ] = {}
        self._event_callback_lock = threading.Lock()
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

    async def cancel_and_reap(
        self,
        session_id: str,
        *,
        task_id: str,
        deadline: float,
    ) -> bool:
        """Cancel one remote turn and confirm its terminal process scope is gone."""
        if self.call_source != "dsoftbus":
            raise RuntimeError("cancel_and_reap is reserved for remote DSoftBus turns")
        self.interrupt(session_id)

        from mclaw.tools.process_registry import process_registry

        def terminate_owned_processes() -> dict[str, Any]:
            return process_registry.terminate_scope(
                task_id=task_id,
                session_key=session_id,
            )

        initial = await asyncio.to_thread(terminate_owned_processes)
        while not self._session_is_idle(session_id):
            remaining = float(deadline) - time.monotonic()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(0.02, remaining))

        # Close the spawn-vs-cancel window: after the Agent and every tool worker
        # have drained, prove once more that the Task owns no surviving process.
        final = await asyncio.to_thread(terminate_owned_processes)
        return bool(
            initial.get("termination_confirmed") is True
            and final.get("termination_confirmed") is True
        )

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
        self._retiring_session_set().add(session_id)
        if not agent and not task:
            self._cancelled_sessions.discard(session_id)
            self._try_retire_session(session_id)
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
        with self._event_callback_lock:
            self._event_callback_futures.setdefault(session_id, set())

    async def flush_session_events(self, session_id: str) -> None:
        """Wait until every event emitted before this fence has been handled."""

        while True:
            with self._event_callback_lock:
                pending = tuple(
                    future
                    for future in self._event_callback_futures.get(
                        session_id, set()
                    )
                    if not future.done()
                )
            if not pending:
                return
            await asyncio.gather(
                *(asyncio.wrap_future(future) for future in pending),
                return_exceptions=True,
            )

    def unbind_session_events(self, session_id: str) -> None:
        """Remove one event sink after its callbacks have crossed the flush fence."""

        self._event_callbacks.pop(session_id, None)
        with self._event_callback_lock:
            futures = self._event_callback_futures.get(session_id, set())
            if not any(not future.done() for future in futures):
                self._event_callback_futures.pop(session_id, None)

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
        deadline_monotonic: float | None = None,
        enqueue_if_busy: bool = True,
        workspace_path: str | None = None,
    ) -> AgentTurnResult:
        """Serialize turns per session and drain its bounded pending FIFO."""
        if type(enqueue_if_busy) is not bool:
            raise TypeError("enqueue_if_busy must be a bool")
        if workspace_path is not None and (
            not isinstance(workspace_path, str) or not workspace_path.strip()
        ):
            raise TypeError("workspace_path must be a non-empty string or None")
        if deadline_monotonic is not None and (
            isinstance(deadline_monotonic, bool)
            or not isinstance(deadline_monotonic, (int, float))
        ):
            raise TypeError("deadline_monotonic must be a number or None")
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
            if not enqueue_if_busy:
                self.release_ingress(ingress_reservation)
                self._discard_message_cache(message)
                return AgentTurnResult(
                    session_id=session_id,
                    error="RUNNER_BUSY",
                    raw_result={"runner_busy": True},
                )
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
                    deadline_monotonic=deadline_monotonic,
                    workspace_path=workspace_path,
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
            if session_id in getattr(self, "_retiring_sessions", set()):
                self._try_retire_session(session_id)
        return last_result

    async def _run_single_turn(
        self,
        *,
        message: ChannelMessage,
        session_id: str,
        conversation_history: list[dict] | None,
        extra_system: str = "",
        deadline_monotonic: float | None = None,
        workspace_path: str | None = None,
    ) -> AgentTurnResult:
        """Run one MClaw conversation turn on a worker thread."""
        agent: MClaw | None = None
        cancel_event = None
        worker_task: asyncio.Task | None = None
        worker_finished = threading.Event()
        discard_agent = False
        try:
            closing_error = await self._wait_for_closing_session(
                session_id, deadline=deadline_monotonic
            )
            if closing_error:
                return AgentTurnResult(session_id=session_id, error=closing_error)
            if (
                deadline_monotonic is not None
                and time.monotonic() >= float(deadline_monotonic)
            ):
                return AgentTurnResult(
                    session_id=session_id,
                    error="DEADLINE_EXCEEDED",
                    raw_result={"deadline_exceeded": True},
                )
            agent = (
                self._get_or_create_agent(session_id=session_id)
                if workspace_path is None
                else self._get_or_create_agent(
                    session_id=session_id,
                    workspace_path=workspace_path,
                )
            )
            begin_turn = getattr(agent, "begin_turn", None)
            if callable(begin_turn):
                cancel_event = begin_turn()
            self._active_agents[session_id] = agent

            def _run_agent() -> dict:
                try:
                    return agent.run_conversation(
                        user_message=message.text,
                        conversation_history=copy.deepcopy(conversation_history),
                        disable_tools=getattr(self, "disable_tools", False),
                        extra_system=extra_system,
                        advance_background_review=getattr(
                            self, "advance_background_review", True
                        ),
                        call_source=getattr(self, "call_source", "channel"),
                        deadline_monotonic=deadline_monotonic,
                        cancel_event=cancel_event,
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
            if getattr(self, "call_source", "channel") == "dsoftbus":
                logger.error(
                    "remote agent turn failed type=%s",
                    type(exc).__name__,
                )
                code = str(getattr(exc, "code", "PROVIDER_ERROR"))
                if code not in {
                    "AGENT_TOOLS_FORBIDDEN",
                    "DEADLINE_EXCEEDED",
                    "REMOTE_PROVIDER_UNAVAILABLE",
                }:
                    code = "PROVIDER_ERROR"
                return AgentTurnResult(session_id=session_id, error=code)
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
                    self._retiring_session_set().add(session_id)
                    self._retiring_agent_map()[session_id] = agent
                    getattr(self, "_agents", {}).pop(session_id, None)
                else:
                    self._touch_agent(session_id, agent)
                if session_id in self._retiring_session_set():
                    self._try_retire_session(session_id)
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

    async def _wait_for_closing_session(
        self,
        session_id: str,
        *,
        deadline: float | None = None,
    ) -> str | None:
        """Wait for transient cleanup, but fail fast on a persistent safety fence."""
        while True:
            if deadline is not None and time.monotonic() >= float(deadline):
                return "DEADLINE_EXCEEDED"
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
                timeout = 0.1
                if deadline is not None:
                    timeout = min(timeout, max(0.0, float(deadline) - time.monotonic()))
                if timeout <= 0:
                    return "DEADLINE_EXCEEDED"
                await asyncio.wait_for(closing_event.wait(), timeout=timeout)
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
        closing_agent = getattr(self, "_closing_agents", {}).pop(session_id, None)
        if closing_agent is not None and session_id in self._retiring_session_set():
            self._retiring_agent_map()[session_id] = closing_agent
        closing_event.set()
        safe_cancel_trace(
            lambda: logger.info(
                "[CANCEL_TRACE] channel_fence_release session=%s",
                session_id,
            )
        )
        if session_id in self._retiring_session_set():
            self._try_retire_session(session_id)

    def _get_or_create_agent(
        self,
        *,
        session_id: str,
        workspace_path: str | None = None,
    ) -> "MClaw":
        """Return an existing session agent or create one bound to channel config."""
        from mclaw.agent.core import MClaw

        now = time.time()
        self._evict_idle(now)
        provider_runtime = self._runtime_for_session(session_id)
        requested_workspace = (
            None
            if workspace_path is None
            else str(Path(workspace_path).expanduser().resolve())
        )
        cached = self._agents.get(session_id)
        if cached:
            agent, _ = cached
            cached_workspace = str(
                Path(str(getattr(agent, "workspace_path", "") or "."))
                .expanduser()
                .resolve()
            )
            workspace_matches = (
                requested_workspace is None
                or cached_workspace == requested_workspace
            )
            if (
                workspace_matches
                and agent.provider_runtime.fingerprint()
                == provider_runtime.fingerprint()
            ):
                self._agents.move_to_end(session_id)
                self._agents[session_id] = (agent, now)
                return agent
            self._retiring_session_set().add(session_id)
            if not self._try_retire_session(session_id):
                raise RuntimeError("Cached channel Agent could not be retired safely")
        agent = MClaw(
            provider_runtime=provider_runtime,
            session_db=self.session_db,
            session_id=session_id,
            enabled_toolsets=self.enabled_toolsets,
            platform=self.platform,
            system_prompt=self.agent_system_prompt,
            workspace=(
                requested_workspace
                if requested_workspace is not None
                else self._workspace_for_session(session_id)
            ),
            skip_memory=self.skip_memory,
            config=self.config,
            event_callback=lambda event, _session_id=session_id: self._emit_agent_event(_session_id, event),
        )
        self._agents[session_id] = (agent, now)
        self._enforce_cache_cap()
        return agent

    def _runtime_for_session(self, session_id: str) -> ProviderRuntimeContext:
        """Restore session model state before consulting the in-memory agent cache."""
        if self.session_db is None:
            if self.startup_provider_runtime is None:
                raise RuntimeError("REMOTE_PROVIDER_UNAVAILABLE")
            return self.startup_provider_runtime
        snapshot = self.session_db.get_model_config(session_id)
        row = self.session_db.get_session(session_id) or {}
        return restore_session_runtime_context(
            snapshot,
            config=self.config,
            row_model=str(row.get("model") or ""),
            fallback_context=self.startup_provider_runtime,
        )

    def _workspace_for_session(self, session_id: str) -> str | None:
        """Derive and, for tool-enabled remote turns, securely create a workspace."""

        root = self.agent_workspace_root
        if root is None:
            return None
        suffix = session_id.removeprefix("dsoftbus:")[:32]
        workspace = Path(root) / suffix
        if self.call_source == "dsoftbus" and not self.disable_tools:
            from mclaw.dsoftbus.workspace import ensure_remote_workspace

            workspace = ensure_remote_workspace(root, suffix)
        return str(workspace)

    def update_provider_runtime(
        self, context: ProviderRuntimeContext | None
    ) -> int:
        """Publish the next-turn Provider and retire idle agents on mismatch."""

        if context is not None and not isinstance(context, ProviderRuntimeContext):
            raise TypeError("context must be a ProviderRuntimeContext or None")
        self.startup_provider_runtime = context
        retired = 0
        for session_id, (agent, _timestamp) in tuple(
            getattr(self, "_agents", {}).items()
        ):
            current = getattr(agent, "provider_runtime", None)
            same = bool(
                context is not None
                and isinstance(current, ProviderRuntimeContext)
                and current.fingerprint() == context.fingerprint()
            )
            if same:
                continue
            self._retiring_session_set().add(session_id)
            if self._try_retire_session(session_id):
                retired += 1
        return retired

    def _emit_agent_event(self, session_id: str, event: dict[str, Any]) -> None:
        """Bridge synchronous agent callbacks back into the channel event loop."""
        bound = self._event_callbacks.get(session_id)
        if not bound:
            return
        loop, callback = bound
        if loop.is_closed():
            return
        future = asyncio.run_coroutine_threadsafe(callback(session_id, event), loop)
        with self._event_callback_lock:
            self._event_callback_futures.setdefault(session_id, set()).add(
                future
            )

        def _completed(completed: concurrent.futures.Future[Any]) -> None:
            with self._event_callback_lock:
                current = self._event_callback_futures.get(session_id)
                if current is not None:
                    current.discard(completed)
                    if not current and session_id not in self._event_callbacks:
                        self._event_callback_futures.pop(session_id, None)
            self._log_event_callback_error(session_id, completed)

        future.add_done_callback(_completed)

    def _log_event_callback_error(
        self,
        session_id: str,
        future: concurrent.futures.Future[Any],
    ) -> None:
        try:
            future.result()
        except Exception as exc:
            if self.call_source == "dsoftbus":
                logger.warning(
                    "remote agent event callback failed type=%s",
                    type(exc).__name__,
                )
            else:
                logger.warning(
                    "channel agent event callback failed session=%s: %s",
                    session_id,
                    exc,
                )

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
            self._retiring_session_set().add(sid)
            self._try_retire_session(sid)

    def _enforce_cache_cap(self) -> None:
        """Keep the agent cache under its configured capacity."""
        checked = 0
        while len(self._agents) > self.max_cached_agents and checked < len(self._agents):
            sid, _ = next(iter(self._agents.items()))
            if sid in self._active_agents:
                self._agents.move_to_end(sid)
                checked += 1
                continue
            self._retiring_session_set().add(sid)
            if self._try_retire_session(sid):
                checked = 0
            else:
                self._agents.move_to_end(sid)
                checked += 1

    def _close_agent(self, agent: "MClaw", deadline: float | None) -> bool:
        close = getattr(agent, "close", None)
        if not callable(close):
            return True
        try:
            parameters = inspect.signature(close).parameters
        except (TypeError, ValueError):
            parameters = {}
        try:
            if "deadline" in parameters or any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in parameters.values()
            ):
                result = close(deadline=deadline)
            else:
                result = close()
        except BaseException as error:
            if self.call_source == "dsoftbus":
                logger.warning(
                    "remote Agent close failed type=%s",
                    type(error).__name__,
                )
            else:
                logger.warning("channel Agent close failed", exc_info=True)
            return False
        return result is not False

    def _retiring_session_set(self) -> set[str]:
        retiring = getattr(self, "_retiring_sessions", None)
        if retiring is None:
            retiring = set()
            self._retiring_sessions = retiring
        return retiring

    def _retiring_agent_map(self) -> dict[str, "MClaw"]:
        agents = getattr(self, "_retiring_agents", None)
        if agents is None:
            agents = {}
            self._retiring_agents = agents
        return agents

    def _session_is_idle(self, session_id: str) -> bool:
        if session_id in getattr(self, "_active_agents", {}):
            return False
        closing = getattr(self, "_closing_sessions", {}).get(session_id)
        if closing is not None and not closing.is_set():
            return False
        task = getattr(self, "_session_tasks", {}).get(session_id)
        if task is not None and not task.done():
            return False
        pending = getattr(self, "_pending", {}).get(session_id)
        if isinstance(pending, ChannelMessage):
            return False
        if isinstance(pending, (deque, list, tuple)) and pending:
            return False
        ingress = getattr(self, "_ingress_queues", {}).get(session_id)
        if ingress and any(not item.released for item in ingress):
            return False
        lock = getattr(self, "_locks", {}).get(session_id)
        if lock is not None and lock.locked():
            return False
        cached = getattr(self, "_agents", {}).get(session_id)
        agent = (
            cached[0]
            if cached
            else self._retiring_agent_map().get(session_id)
            or getattr(self, "_closing_agents", {}).get(session_id)
        )
        has_outstanding = getattr(agent, "_has_outstanding_turn_workers", None)
        return not (callable(has_outstanding) and has_outstanding())

    def _all_sessions_idle(self) -> bool:
        sessions = (
            set(getattr(self, "_agents", {}))
            | set(getattr(self, "_active_agents", {}))
            | set(getattr(self, "_closing_sessions", {}))
            | set(getattr(self, "_session_tasks", {}))
            | set(getattr(self, "_pending", {}))
            | set(getattr(self, "_ingress_queues", {}))
            | set(getattr(self, "_locks", {}))
        )
        return all(self._session_is_idle(session_id) for session_id in sessions)

    def _try_retire_session(
        self,
        session_id: str,
        *,
        deadline: float | None = None,
    ) -> bool:
        """Close and forget one idle cached Agent without dropping its strong reference."""
        retiring = self._retiring_session_set()
        retiring.add(session_id)
        if not self._session_is_idle(session_id):
            return False
        agents = getattr(self, "_agents", {})
        cached = agents.get(session_id)
        closing_agent = getattr(self, "_closing_agents", {}).get(session_id)
        retiring_agent = self._retiring_agent_map().get(session_id)
        agent = cached[0] if cached else retiring_agent or closing_agent
        effective_deadline = deadline if deadline is not None else time.monotonic() + 5.0
        if agent is not None and not self._close_agent(agent, effective_deadline):
            return False
        if cached is not None and agents.get(session_id) is cached:
            agents.pop(session_id, None)
        self._retiring_agent_map().pop(session_id, None)
        getattr(self, "_locks", {}).pop(session_id, None)
        getattr(self, "_cancelled_sessions", set()).discard(session_id)
        getattr(self, "_event_callbacks", {}).pop(session_id, None)
        with getattr(self, "_event_callback_lock", threading.Lock()):
            event_futures = getattr(self, "_event_callback_futures", {})
            if not any(
                not future.done()
                for future in event_futures.get(session_id, set())
            ):
                event_futures.pop(session_id, None)
        pending_map = getattr(self, "_pending", {})
        pending = pending_map.get(session_id)
        if not pending:
            pending_map.pop(session_id, None)
        ingress_map = getattr(self, "_ingress_queues", {})
        ingress = ingress_map.get(session_id)
        if not ingress:
            ingress_map.pop(session_id, None)
        getattr(self, "_ingress_generations", {}).pop(session_id, None)
        task_map = getattr(self, "_session_tasks", {})
        task = task_map.get(session_id)
        if task is None or task.done():
            task_map.pop(session_id, None)
        closing_map = getattr(self, "_closing_sessions", {})
        closing = closing_map.get(session_id)
        if closing is None or closing.is_set():
            closing_map.pop(session_id, None)
            getattr(self, "_closing_agents", {}).pop(session_id, None)
        retiring.discard(session_id)
        return True

    async def wait_for_idle(self, *, deadline: float) -> bool:
        """Wait for every active, queued, ingress, lock, and closing fence to drain."""
        while True:
            for session_id in tuple(getattr(self, "_retiring_sessions", set())):
                self._try_retire_session(session_id, deadline=deadline)
            if self._all_sessions_idle():
                return True
            remaining = float(deadline) - time.monotonic()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(0.02, remaining))

    async def forget_session(
        self,
        session_id: str,
        *,
        deadline: float | None = None,
    ) -> bool:
        """Interrupt, drain, close, and remove all state owned for one session."""
        effective_deadline = deadline if deadline is not None else time.monotonic() + 5.0
        self._retiring_session_set().add(session_id)
        self.interrupt(session_id)
        self._clear_pending(session_id)
        self._cancel_ingress(session_id)
        while not self._session_is_idle(session_id):
            remaining = float(effective_deadline) - time.monotonic()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(0.02, remaining))
        return self._try_retire_session(session_id, deadline=effective_deadline)

    async def dispose_all(
        self,
        *,
        deadline: float,
        close_owned_session_db: bool = True,
    ) -> bool:
        """Drain every session and close only resources owned by this runner."""
        sessions = (
            set(getattr(self, "_agents", {}))
            | set(getattr(self, "_active_agents", {}))
            | set(getattr(self, "_closing_sessions", {}))
            | set(getattr(self, "_closing_agents", {}))
            | set(getattr(self, "_session_tasks", {}))
            | set(getattr(self, "_pending", {}))
            | set(getattr(self, "_ingress_queues", {}))
            | set(getattr(self, "_locks", {}))
            | set(getattr(self, "_event_callbacks", {}))
            | set(getattr(self, "_retiring_agents", {}))
            | set(getattr(self, "_retiring_sessions", set()))
        )
        self._retiring_session_set().update(sessions)
        self.interrupt_all()
        for session_id in sessions:
            self._clear_pending(session_id)
            self._cancel_ingress(session_id)
        if not await self.wait_for_idle(deadline=deadline):
            return False
        success = True
        for session_id in tuple(self._retiring_session_set()):
            success = self._try_retire_session(session_id, deadline=deadline) and success
        db = getattr(self, "session_db", None)
        if success and close_owned_session_db and getattr(self, "_owns_session_db", False) and db is not None:
            close_with_deadline = getattr(db, "close_with_deadline", None)
            if callable(close_with_deadline):
                success = bool(close_with_deadline(deadline))
            else:
                close = getattr(db, "close", None)
                if callable(close) and time.monotonic() < float(deadline):
                    close()
                else:
                    success = False
            if success:
                self.session_db = None
        return success
