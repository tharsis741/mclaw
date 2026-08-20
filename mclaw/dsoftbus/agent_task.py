# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Long-lived A2A Task execution for authenticated DSoftBus peers."""

from __future__ import annotations

import asyncio
import copy
from collections import OrderedDict, deque
from collections.abc import Mapping
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
import time
from types import MappingProxyType
from typing import Any
import uuid

from . import protocol
from .a2a import (
    A2AError,
    CoreMethodCall,
    build_artifact_update,
    build_status_update,
    build_task,
    task_state_is_terminal,
)
from .agent_message import (
    AgentMessageError,
    ConversationKey,
    RemoteTurnExecutor,
    RemoteTurnRequest,
)
from .task_store import DsoftbusTaskStore, TaskStoreError


_DEVICE_ID = re.compile(r"^urn:mclaw:device:oh:[0-9a-f]{64}$")
_RATE_WINDOW_S = 60.0
_TOKEN_WINDOW_S = 3600.0
_TERMINAL_SENTINEL = object()


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return copy.deepcopy(value)


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze(item) for key, item in value.items()}
        )
    if isinstance(value, (tuple, list)):
        return tuple(_freeze(item) for item in value)
    return copy.deepcopy(value)


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            _plain(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, UnicodeEncodeError, ValueError) as error:
        raise AgentMessageError("INVALID_PARAMS") from error


def _uuid4(value: Any, label: str) -> str:
    try:
        return protocol.canonical_uuid4(value, label)
    except protocol.ProtocolError as error:
        raise AgentMessageError("INVALID_PARAMS") from error


def _new_uuid(factory: Any, label: str) -> str:
    return _uuid4(str(factory()), label)


@dataclass(eq=False, slots=True)
class TaskSubscription:
    """One bounded ordered stream attached to an active remote Task."""

    task_id: str
    context_id: str
    _queue: asyncio.Queue[Any] = field(
        default_factory=lambda: asyncio.Queue(
            maxsize=protocol.TASK_STREAM_QUEUE_MAX
        ),
        repr=False,
    )
    _closed: bool = False
    _overflowed: bool = False

    async def next_event(self) -> Mapping[str, Any] | None:
        item = await self._queue.get()
        self._queue.task_done()
        if item is _TERMINAL_SENTINEL:
            self._closed = True
            if self._overflowed:
                raise AgentMessageError(
                    "CAPACITY_BUSY",
                    outcome_unknown=True,
                )
            return None
        return _freeze(item)

    @property
    def overflowed(self) -> bool:
        return self._overflowed

    def _publish(self, event: Mapping[str, Any]) -> bool:
        if self._closed:
            return False
        raw = _canonical(event)
        if len(raw) > protocol.TASK_STREAM_ITEM_BYTES_MAX or self._queue.full():
            self._overflowed = True
            self._close()
            return False
        self._queue.put_nowait(_freeze(event))
        return True

    def _close(self) -> None:
        if self._closed:
            return
        while self._queue.full():
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except asyncio.QueueEmpty:  # pragma: no cover - same-loop invariant
                break
        self._queue.put_nowait(_TERMINAL_SENTINEL)


@dataclass(slots=True)
class _TaskRecord:
    task_id: str
    context_id: str
    message_id: str
    peer_device_id: str
    peer_runtime_instance_id: str
    conversation_key: ConversationKey
    request_digest: str
    text: str
    history: list[dict[str, Any]]
    task: Mapping[str, Any]
    item_bytes: int
    subscribers: set[TaskSubscription] = field(default_factory=set)
    execution: asyncio.Task[None] | None = None
    token_reservation: int = 0
    cancel_requested: bool = False
    peer_pending_reserved: bool = False
    cache_bytes: int = protocol.REMOTE_FRAME_MAX
    cache_expires_at: float | None = None

    @property
    def session_id(self) -> str:
        return self.conversation_key.session_id


@dataclass(slots=True)
class _TaskContext:
    history: list[dict[str, Any]] = field(default_factory=list)
    byte_length: int = 2
    last_used: float = 0.0


class DsoftbusTaskDispatcher:
    """Bounded FIFO whose admitted executions live until a terminal state.

    Admission, queue and provider-operation limits remain bounded.  There is
    intentionally no hidden wall-clock deadline around a Task: cancellation,
    an explicit operation timeout, Runtime shutdown, or a terminal provider
    outcome ends it.
    """

    def __init__(
        self,
        *,
        config: Mapping[str, Any],
        executor: RemoteTurnExecutor,
        provider_runtime: Any | None,
        provider_ready: bool,
        task_store: DsoftbusTaskStore | None = None,
        state_root: str | Path | None = None,
        monotonic: Any = time.monotonic,
        uuid_factory: Any = uuid.uuid4,
    ) -> None:
        if not isinstance(config, Mapping):
            raise TypeError("config must be a mapping")
        expected = {
            "accept_remote_messages",
            "global_requests_per_minute",
            "per_peer_requests_per_minute",
            "remote_token_budget_per_hour",
        }
        source = dict(config)
        if set(source) != expected:
            raise ValueError("remote task config keys are invalid")
        if (
            type(source["accept_remote_messages"]) is not bool
            or type(source["global_requests_per_minute"]) is not int
            or type(source["per_peer_requests_per_minute"]) is not int
            or type(source["remote_token_budget_per_hour"]) is not int
            or not 1 <= source["per_peer_requests_per_minute"] <= 60
            or not source["per_peer_requests_per_minute"]
            <= source["global_requests_per_minute"]
            <= 240
            or not 1_000 <= source["remote_token_budget_per_hour"] <= 10_000_000
        ):
            raise ValueError("remote task config values are invalid")
        for name in (
            "execute",
            "estimate_budget",
            "update_provider_runtime",
            "interrupt",
            "cancel_and_reap",
            "forget_session",
            "dispose",
        ):
            if not callable(getattr(executor, name, None)):
                raise TypeError("executor does not implement the remote turn contract")
        if type(provider_ready) is not bool or (
            provider_ready and provider_runtime is None
        ):
            raise ValueError("provider readiness pair is invalid")
        self._accept_remote = source["accept_remote_messages"]
        self._global_rate_limit = source["global_requests_per_minute"]
        self._peer_rate_limit = source["per_peer_requests_per_minute"]
        self._token_budget = source["remote_token_budget_per_hour"]
        self._executor = executor
        self._provider_runtime = provider_runtime
        self._provider_ready = provider_ready
        self._monotonic = monotonic
        self._uuid_factory = uuid_factory
        self._store = task_store or DsoftbusTaskStore(state_root)
        self._store.recover_interrupted_owned_tasks()
        self._queue: asyncio.Queue[_TaskRecord] = asyncio.Queue(
            maxsize=protocol.DISPATCH_QUEUE_MAX
        )
        self._queue_bytes = 0
        self._records: OrderedDict[str, _TaskRecord] = OrderedDict()
        self._request_index: dict[tuple[str, str, str], tuple[str, str]] = {}
        self._contexts: OrderedDict[ConversationKey, _TaskContext] = OrderedDict()
        self._stale_peer_runtimes: set[tuple[str, str]] = set()
        self._peer_pending: dict[tuple[str, str], int] = {}
        self._global_rate: deque[float] = deque()
        self._peer_rates: dict[str, deque[float]] = {}
        self._token_charges: deque[tuple[float, int]] = deque()
        self._token_reserved = 0
        self._local_turn_tokens: set[str] = set()
        self._local_turn_idle = asyncio.Event()
        self._local_turn_idle.set()
        self._worker: asyncio.Task[None] | None = None
        self._accepting = True
        self._stopped = False
        self._execution_count = 0
        self._execution_active = 0
        self._remote_accepted = 0
        self._rejected: dict[str, int] = {}

    @property
    def task_store(self) -> DsoftbusTaskStore:
        return self._store

    @staticmethod
    def _validate_peer(device_id: Any, runtime_id: Any) -> tuple[str, str]:
        if not isinstance(device_id, str) or _DEVICE_ID.fullmatch(device_id) is None:
            raise AgentMessageError("INVALID_PARAMS")
        return device_id, _uuid4(runtime_id, "peerRuntimeInstanceId")

    def update_provider_runtime(self, context: Any | None, *, provider_ready: bool) -> None:
        if type(provider_ready) is not bool or (provider_ready and context is None):
            raise ValueError("provider readiness pair is invalid")
        self._provider_runtime = context
        self._provider_ready = provider_ready
        self._executor.update_provider_runtime(context)

    @staticmethod
    def _local_turn_token(token: Any) -> str:
        if not isinstance(token, str) or not token or "\x00" in token:
            raise ValueError("local turn token is invalid")
        try:
            size = len(token.encode("utf-8"))
        except UnicodeEncodeError as error:
            raise ValueError("local turn token is invalid") from error
        if size > 256:
            raise ValueError("local turn token is invalid")
        return token

    def local_turn_started(self, token: str) -> None:
        token = self._local_turn_token(token)
        self._local_turn_tokens.add(token)
        self._local_turn_idle.clear()

    def local_turn_finished(self, token: str) -> None:
        token = self._local_turn_token(token)
        self._local_turn_tokens.discard(token)
        if not self._local_turn_tokens:
            self._local_turn_idle.set()

    def record_rejection(self, reason: str) -> None:
        if reason not in protocol.RPC_ERROR_CODES:
            reason = "INTERNAL_ERROR"
        self._rejected[reason] = self._rejected.get(reason, 0) + 1

    def _fail(self, reason: str, *, outcome_unknown: bool = False) -> None:
        self.record_rejection(reason)
        raise AgentMessageError(reason, outcome_unknown=outcome_unknown)

    def _purge_rate(self, now: float) -> None:
        cutoff = now - _RATE_WINDOW_S
        while self._global_rate and self._global_rate[0] <= cutoff:
            self._global_rate.popleft()
        for device_id, values in tuple(self._peer_rates.items()):
            while values and values[0] <= cutoff:
                values.popleft()
            if not values:
                del self._peer_rates[device_id]

    def _charge_rate(self, device_id: str, now: float) -> None:
        self._purge_rate(now)
        peer = self._peer_rates.setdefault(device_id, deque())
        if len(self._global_rate) >= self._global_rate_limit or len(peer) >= self._peer_rate_limit:
            self._fail("RATE_LIMITED")
        self._global_rate.append(now)
        peer.append(now)

    def _token_used(self, now: float) -> int:
        cutoff = now - _TOKEN_WINDOW_S
        while self._token_charges and self._token_charges[0][0] <= cutoff:
            self._token_charges.popleft()
        return sum(value for _timestamp, value in self._token_charges)

    def _reserve_tokens(self, amount: int) -> None:
        if type(amount) is not int or amount <= 0:
            self._fail("INTERNAL_ERROR")
        if self._token_used(self._monotonic()) + self._token_reserved > self._token_budget - amount:
            self._fail("REMOTE_BUDGET_EXCEEDED")
        self._token_reserved += amount

    def _settle_tokens(self, record: _TaskRecord, raw: Mapping[str, Any]) -> None:
        reserved = record.token_reservation
        if reserved <= 0:
            return
        self._token_reserved -= reserved
        record.token_reservation = 0
        amount = reserved
        usage = raw.get("token_usage")
        if isinstance(usage, Mapping):
            input_tokens = usage.get("input_tokens")
            output_tokens = usage.get("output_tokens")
            if (
                type(input_tokens) is int
                and input_tokens > 0
                and type(output_tokens) is int
                and output_tokens > 0
            ):
                amount = input_tokens + output_tokens
        amount = min(
            amount,
            max(0, self._token_budget - self._token_used(self._monotonic())),
        )
        if amount:
            self._token_charges.append((self._monotonic(), amount))

    def _forfeit_token_reservation(self, record: _TaskRecord) -> None:
        reserved = record.token_reservation
        if reserved <= 0:
            return
        self._token_reserved -= reserved
        record.token_reservation = 0
        amount = min(
            reserved,
            max(0, self._token_budget - self._token_used(self._monotonic())),
        )
        if amount:
            self._token_charges.append((self._monotonic(), amount))

    def start(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(
                self._consume(), name="mclaw-dsoftbus-agent-task-dispatch"
            )

    def _context_active(self, key: ConversationKey) -> bool:
        return any(
            record.conversation_key == key
            and not task_state_is_terminal(record.task["status"]["state"])
            for record in self._records.values()
        )

    @staticmethod
    def _record_peer_key(record: _TaskRecord) -> tuple[str, str]:
        return (
            record.peer_device_id,
            record.peer_runtime_instance_id,
        )

    def _drop_cached_record(self, record: _TaskRecord) -> None:
        if self._records.get(record.task_id) is not record:
            return
        if not task_state_is_terminal(record.task["status"]["state"]):
            raise AssertionError("active Task cannot leave the response cache")
        del self._records[record.task_id]
        request_key = (
            record.peer_device_id,
            record.peer_runtime_instance_id,
            record.message_id,
        )
        indexed = self._request_index.get(request_key)
        if indexed is not None and indexed[1] == record.task_id:
            del self._request_index[request_key]

    def _purge_response_cache(self, now: float) -> None:
        for record in tuple(self._records.values()):
            expires_at = record.cache_expires_at
            if expires_at is None or expires_at > now:
                continue
            self._drop_cached_record(record)

    def _response_cache_usage(
        self,
        peer_key: tuple[str, str] | None = None,
    ) -> tuple[int, int]:
        records = (
            self._records.values()
            if peer_key is None
            else (
                record
                for record in self._records.values()
                if self._record_peer_key(record) == peer_key
            )
        )
        count = 0
        byte_length = 0
        for record in records:
            count += 1
            byte_length += record.cache_bytes
        return count, byte_length

    def _ensure_response_cache_capacity(
        self,
        peer_key: tuple[str, str],
    ) -> None:
        self._purge_response_cache(self._monotonic())
        global_count, global_bytes = self._response_cache_usage()
        peer_count, peer_bytes = self._response_cache_usage(peer_key)
        amount = protocol.REMOTE_FRAME_MAX
        if (
            global_count >= protocol.RESPONSE_CACHE_CAP
            or global_bytes > protocol.RESPONSE_CACHE_BYTES_MAX - amount
            or peer_count >= protocol.PER_PEER_RESPONSE_CACHE_CAP
            or peer_bytes > protocol.PER_PEER_RESPONSE_CACHE_BYTES_MAX - amount
        ):
            self._fail("CAPACITY_BUSY")

    def _finalize_response_cache(self, record: _TaskRecord) -> None:
        if self._records.get(record.task_id) is not record:
            return
        actual_bytes = len(_canonical(record.task))
        if (
            actual_bytes > protocol.REMOTE_FRAME_MAX
            or actual_bytes > protocol.RESPONSE_CACHE_BYTES_MAX
            or actual_bytes > protocol.PER_PEER_RESPONSE_CACHE_BYTES_MAX
        ):
            # The authoritative Task already lives in DsoftbusTaskStore.  An
            # object too large for one bounded replay slot must not remain in
            # the in-memory cache.
            self._drop_cached_record(record)
            return
        record.cache_bytes = actual_bytes
        record.cache_expires_at = (
            self._monotonic() + float(protocol.RESPONSE_CACHE_TTL_S)
        )
        self._records.move_to_end(record.task_id)

    def _context_counts(self, peer_key: tuple[str, str]) -> tuple[int, int]:
        count = 0
        byte_length = 0
        for key, context in self._contexts.items():
            if (key.peer_device_id, key.peer_runtime_instance_id) == peer_key:
                count += 1
                byte_length += context.byte_length
        return count, byte_length

    def _forget_stale_runtime_if_empty(self, peer_key: tuple[str, str]) -> None:
        if any(
            (key.peer_device_id, key.peer_runtime_instance_id) == peer_key
            for key in self._contexts
        ):
            return
        self._stale_peer_runtimes.discard(peer_key)

    async def _retire_context(
        self,
        key: ConversationKey,
        *,
        deadline: float,
    ) -> bool:
        context = self._contexts.get(key)
        if context is None:
            self._forget_stale_runtime_if_empty(
                (key.peer_device_id, key.peer_runtime_instance_id)
            )
            return True
        if self._context_active(key):
            return False
        retired = await self._executor.forget_session(key.session_id, deadline)
        current = self._contexts.get(key)
        if not retired or current is not context or self._context_active(key):
            return False
        del self._contexts[key]
        # Terminal Tasks are persisted and replayed from DsoftbusTaskStore.  Do
        # not let their in-memory records retain an evicted conversation history.
        for record in self._records.values():
            if (
                record.conversation_key == key
                and task_state_is_terminal(record.task["status"]["state"])
            ):
                record.history.clear()
        self._forget_stale_runtime_if_empty(
            (key.peer_device_id, key.peer_runtime_instance_id)
        )
        return True

    async def _evict_idle_context(
        self,
        peer_key: tuple[str, str] | None,
        *,
        deadline: float,
    ) -> bool:
        for key in tuple(self._contexts):
            key_peer = (key.peer_device_id, key.peer_runtime_instance_id)
            if peer_key is not None and key_peer != peer_key:
                continue
            if self._context_active(key):
                continue
            if await self._retire_context(key, deadline=deadline):
                return True
        return False

    async def _retire_replaced_peer_contexts(
        self,
        peer_device_id: str,
        current_runtime_id: str,
        *,
        deadline: float,
    ) -> None:
        replaced = {
            (key.peer_device_id, key.peer_runtime_instance_id)
            for key in self._contexts
            if key.peer_device_id == peer_device_id
            and key.peer_runtime_instance_id != current_runtime_id
        }
        self._stale_peer_runtimes.update(replaced)
        for key in tuple(self._contexts):
            key_peer = (key.peer_device_id, key.peer_runtime_instance_id)
            if key_peer not in replaced or self._context_active(key):
                continue
            if not await self._retire_context(key, deadline=deadline):
                self._fail("CAPACITY_BUSY")

    async def _retire_terminal_stale_context(self, record: _TaskRecord) -> None:
        peer_key = (record.peer_device_id, record.peer_runtime_instance_id)
        if peer_key not in self._stale_peer_runtimes:
            return
        await self._retire_context(
            record.conversation_key,
            deadline=self._monotonic() + float(protocol.CONTROL_TIMEOUT_S),
        )

    async def _ensure_context_capacity(
        self,
        peer_key: tuple[str, str],
        *,
        deadline: float,
    ) -> None:
        while True:
            peer_count, peer_bytes = self._context_counts(peer_key)
            total_bytes = sum(value.byte_length for value in self._contexts.values())
            if (
                peer_count < protocol.PER_PEER_REMOTE_CONTEXT_MAX
                and peer_bytes + 2 <= protocol.PER_PEER_REMOTE_CONTEXT_BYTES_MAX
                and len(self._contexts) < protocol.REMOTE_CONTEXT_MAX
                and total_bytes + 2 <= protocol.REMOTE_CONTEXT_BYTES_MAX
            ):
                return
            if (
                (
                    peer_count >= protocol.PER_PEER_REMOTE_CONTEXT_MAX
                    or peer_bytes + 2
                    > protocol.PER_PEER_REMOTE_CONTEXT_BYTES_MAX
                )
                and await self._evict_idle_context(peer_key, deadline=deadline)
            ):
                continue
            if await self._evict_idle_context(None, deadline=deadline):
                continue
            self._fail("CAPACITY_BUSY")

    @staticmethod
    def _validate_history(value: Any) -> tuple[list[dict[str, Any]], int]:
        if not isinstance(value, list) or any(
            not isinstance(item, Mapping) for item in value
        ):
            raise AgentMessageError("INVALID_AGENT_RESPONSE")
        try:
            raw = _canonical(value)
            history = json.loads(raw)
        except (AgentMessageError, json.JSONDecodeError) as error:
            raise AgentMessageError("INVALID_AGENT_RESPONSE") from error
        while (
            len(history) > protocol.REMOTE_CONTEXT_MESSAGE_MAX
            or len(raw) > protocol.REMOTE_CONTEXT_UTF8_MAX
        ):
            assistant_index = next(
                (
                    index
                    for index, item in enumerate(history)
                    if item.get("role") == "assistant"
                    and any(
                        prior.get("role") == "user"
                        for prior in history[:index]
                    )
                ),
                None,
            )
            if assistant_index is None:
                raise AgentMessageError("INVALID_AGENT_RESPONSE")
            del history[: assistant_index + 1]
            try:
                raw = _canonical(history)
            except AgentMessageError as error:
                raise AgentMessageError("INVALID_AGENT_RESPONSE") from error
        return history, len(raw)

    async def _ensure_history_capacity(
        self,
        key: ConversationKey,
        byte_length: int,
        *,
        deadline: float,
    ) -> None:
        context = self._contexts.get(key)
        if context is None:
            raise AgentMessageError("STALE_GENERATION", outcome_unknown=True)
        peer_key = (key.peer_device_id, key.peer_runtime_instance_id)
        while True:
            _peer_count, peer_bytes = self._context_counts(peer_key)
            total_bytes = sum(value.byte_length for value in self._contexts.values())
            peer_over = (
                peer_bytes - context.byte_length + byte_length
                > protocol.PER_PEER_REMOTE_CONTEXT_BYTES_MAX
            )
            global_over = (
                total_bytes - context.byte_length + byte_length
                > protocol.REMOTE_CONTEXT_BYTES_MAX
            )
            if not peer_over and not global_over:
                return
            if peer_over and await self._evict_idle_context(
                peer_key, deadline=deadline
            ):
                continue
            if global_over and await self._evict_idle_context(
                None, deadline=deadline
            ):
                continue
            raise AgentMessageError("CAPACITY_BUSY", outcome_unknown=True)

    def _commit_context_history(
        self,
        key: ConversationKey,
        history: list[dict[str, Any]],
        byte_length: int,
    ) -> None:
        context = self._contexts.get(key)
        if context is None:
            raise AgentMessageError("STALE_GENERATION", outcome_unknown=True)
        context.history[:] = history
        context.byte_length = byte_length
        context.last_used = self._monotonic()
        self._contexts.move_to_end(key)

    def _request_identity(
        self,
        peer_device_id: str,
        peer_runtime_instance_id: str,
        call: CoreMethodCall,
    ) -> tuple[str, str | None, str, str]:
        if call.method not in {"SendMessage", "SendStreamingMessage"}:
            self._fail("INVALID_PARAMS")
        message = call.params.get("message")
        if not isinstance(message, Mapping):
            self._fail("INVALID_PARAMS")
        message_id = _uuid4(message.get("messageId"), "messageId")
        context = message.get("contextId")
        context_id = None if context is None else _uuid4(context, "contextId")
        if not isinstance(call.normalized_text, str) or not call.normalized_text:
            self._fail("INVALID_PARAMS")
        digest = hashlib.sha256(
            b"mclaw-a2a-task\0"
            + peer_device_id.encode("utf-8")
            + b"\0"
            + peer_runtime_instance_id.encode("ascii")
            + b"\0"
            + _canonical(call.params)
        ).hexdigest()
        return message_id, context_id, call.normalized_text, digest

    def _new_subscription(self, record: _TaskRecord) -> TaskSubscription:
        subscription = self._task_subscription(record.task)
        record.subscribers.add(subscription)
        if task_state_is_terminal(record.task["status"]["state"]):
            record.subscribers.discard(subscription)
        return subscription

    @staticmethod
    def _task_subscription(task: Mapping[str, Any]) -> TaskSubscription:
        """Create a replay stream for an in-memory or persisted Task."""

        subscription = TaskSubscription(
            str(task["id"]),
            str(task["contextId"]),
        )
        subscription._publish({"task": task})
        if task_state_is_terminal(task["status"]["state"]):
            subscription._close()
        return subscription

    async def submit(
        self,
        *,
        peer_device_id: str,
        peer_runtime_instance_id: str,
        call: CoreMethodCall,
        subscribe: bool,
    ) -> tuple[Mapping[str, Any], TaskSubscription | None]:
        peer_device_id, peer_runtime_instance_id = self._validate_peer(
            peer_device_id, peer_runtime_instance_id
        )
        if not self._accepting or self._stopped:
            self._fail("RUNTIME_STOPPING")
        if not self._accept_remote:
            self._fail("REMOTE_INFERENCE_DISABLED")
        if not self._provider_ready or self._provider_runtime is None:
            self._fail("REMOTE_PROVIDER_UNAVAILABLE")
        message_id, requested_context, text, digest = self._request_identity(
            peer_device_id, peer_runtime_instance_id, call
        )
        request_key = (peer_device_id, peer_runtime_instance_id, message_id)
        indexed = self._request_index.get(request_key)
        if indexed is None:
            try:
                persisted = self._store.find_task_by_request(
                    "owned",
                    peer_device_id,
                    peer_runtime_instance_id,
                    message_id,
                )
            except TaskStoreError:
                self._fail("INTERNAL_ERROR")
                raise AssertionError("unreachable")
            if persisted is not None:
                metadata = persisted.get("metadata", {})
                prior_digest = (
                    str(metadata.get("mclaw.requestDigest", ""))
                    if isinstance(metadata, Mapping)
                    else ""
                )
                if prior_digest != digest:
                    self._fail("INVALID_REQUEST")
                indexed = (prior_digest, str(persisted["id"]))
                self._request_index[request_key] = indexed
        if indexed is not None:
            prior_digest, task_id = indexed
            if prior_digest != digest:
                self._fail("INVALID_REQUEST")
            record = self._records.get(task_id)
            task = (
                record.task
                if record is not None
                else self._store.get_task("owned", peer_device_id, task_id)
            )
            if task is None:
                self._fail("TASK_NOT_FOUND")
            if not subscribe:
                subscription = None
            elif record is not None:
                subscription = self._new_subscription(record)
            else:
                subscription = self._task_subscription(task)
            return task, subscription
        peer_key = (peer_device_id, peer_runtime_instance_id)
        if self._peer_pending.get(peer_key, 0) >= protocol.PER_PEER_DISPATCH_PENDING_MAX:
            self._fail("CAPACITY_BUSY")
        self._charge_rate(peer_device_id, self._monotonic())
        context_deadline = self._monotonic() + float(protocol.CONTROL_TIMEOUT_S)
        await self._retire_replaced_peer_contexts(
            peer_device_id,
            peer_runtime_instance_id,
            deadline=context_deadline,
        )
        if requested_context is None:
            await self._ensure_context_capacity(
                peer_key,
                deadline=context_deadline,
            )
            context_id = _new_uuid(self._uuid_factory, "contextId")
            conversation = ConversationKey(
                peer_device_id, peer_runtime_instance_id, context_id
            )
            context = _TaskContext(last_used=self._monotonic())
            self._contexts[conversation] = context
            history = context.history
        else:
            context_id = requested_context
            conversation = ConversationKey(
                peer_device_id, peer_runtime_instance_id, context_id
            )
            context = self._contexts.get(conversation)
            if context is None:
                self._fail("CONTEXT_NOT_FOUND")
            context.last_used = self._monotonic()
            self._contexts.move_to_end(conversation)
            history = context.history
        try:
            self._ensure_response_cache_capacity(peer_key)
        except AgentMessageError:
            if requested_context is None:
                self._contexts.pop(conversation, None)
            raise
        task_id = _new_uuid(self._uuid_factory, "taskId")
        user_message = _plain(call.params["message"])
        user_message["contextId"] = context_id
        user_message["taskId"] = task_id
        task = build_task(
            task_id=task_id,
            context_id=context_id,
            state="TASK_STATE_SUBMITTED",
            history=(user_message,),
            metadata={
                "mclaw.requestMessageId": message_id,
                "mclaw.peerRuntimeInstanceId": peer_runtime_instance_id,
                "mclaw.requestDigest": digest,
            },
        )
        item_bytes = len(_canonical({"task": task, "text": text, "history": history}))
        if (
            self._queue.full()
            or item_bytes > protocol.DISPATCH_QUEUE_BYTES_MAX
            or self._queue_bytes > protocol.DISPATCH_QUEUE_BYTES_MAX - item_bytes
        ):
            if requested_context is None:
                self._contexts.pop(conversation, None)
            self._fail("CAPACITY_BUSY")
        record = _TaskRecord(
            task_id=task_id,
            context_id=context_id,
            message_id=message_id,
            peer_device_id=peer_device_id,
            peer_runtime_instance_id=peer_runtime_instance_id,
            conversation_key=conversation,
            request_digest=digest,
            text=text,
            history=history,
            task=task,
            item_bytes=item_bytes,
        )
        try:
            self._store.put_task("owned", peer_device_id, task)
        except TaskStoreError as error:
            if requested_context is None:
                self._contexts.pop(conversation, None)
            self._fail("INTERNAL_ERROR")
            raise AssertionError from error
        self._records[task_id] = record
        self._request_index[request_key] = (digest, task_id)
        self._peer_pending[peer_key] = self._peer_pending.get(peer_key, 0) + 1
        record.peer_pending_reserved = True
        self._queue_bytes += item_bytes
        self._queue.put_nowait(record)
        self._remote_accepted += 1
        self.start()
        return task, self._new_subscription(record) if subscribe else None

    def _publish(self, record: _TaskRecord, event: Mapping[str, Any]) -> None:
        for subscription in tuple(record.subscribers):
            if not subscription._publish(event):
                record.subscribers.discard(subscription)

    def _close_streams(self, record: _TaskRecord) -> None:
        for subscription in tuple(record.subscribers):
            subscription._close()
        record.subscribers.clear()

    def _release_peer_pending(self, record: _TaskRecord) -> None:
        if not record.peer_pending_reserved:
            return
        peer_key = (
            record.peer_device_id,
            record.peer_runtime_instance_id,
        )
        pending = self._peer_pending.get(peer_key, 0)
        if pending <= 0:
            raise AssertionError("task peer reservation is missing")
        if pending == 1:
            self._peer_pending.pop(peer_key, None)
        else:
            self._peer_pending[peer_key] = pending - 1
        record.peer_pending_reserved = False

    def _save_task(self, record: _TaskRecord, task: Mapping[str, Any]) -> None:
        record.task = _freeze(task)
        try:
            self._store.put_task("owned", record.peer_device_id, record.task)
        except TaskStoreError as error:
            raise AgentMessageError("INTERNAL_ERROR") from error

    def _status_message(
        self,
        record: _TaskRecord,
        text: str,
        *,
        metadata: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return _freeze(
            {
                "messageId": _new_uuid(self._uuid_factory, "status.messageId"),
                "contextId": record.context_id,
                "taskId": record.task_id,
                "role": "ROLE_AGENT",
                "parts": [{"text": text}],
                "metadata": _plain(metadata),
            }
        )

    async def _agent_event(
        self, record: _TaskRecord, event: Mapping[str, Any]
    ) -> None:
        if record.cancel_requested:
            return
        content = event.get("content")
        source = event.get("content_source")
        if (
            event.get("type") != "assistant.message"
            or event.get("is_final") is True
            or not isinstance(content, str)
            or not content
            or source not in {"content", "reasoning_content"}
        ):
            return
        message = self._status_message(
            record,
            content,
            metadata={
                "mclaw.agentEvent": {
                    "type": "assistant.message",
                    "contentSource": source,
                }
            },
        )
        update = build_status_update(
            task_id=record.task_id,
            context_id=record.context_id,
            state="TASK_STATE_WORKING",
            message=message,
        )
        self._publish(record, update)

    async def _mark_terminal(
        self,
        record: _TaskRecord,
        *,
        state: str,
        failure_reason: str = "",
    ) -> None:
        metadata = dict(record.task.get("metadata", {}))
        if failure_reason:
            metadata["mclaw.failureReason"] = failure_reason
        task = build_task(
            task_id=record.task_id,
            context_id=record.context_id,
            state=state,
            history=record.task.get("history", ()),
            artifacts=record.task.get("artifacts", ()),
            metadata=metadata,
        )
        self._save_task(record, task)
        self._release_peer_pending(record)
        self._publish(
            record,
            build_status_update(
                task_id=record.task_id,
                context_id=record.context_id,
                state=state,
                metadata=(
                    {"mclaw.failureReason": failure_reason}
                    if failure_reason
                    else None
                ),
            ),
        )
        self._close_streams(record)
        self._finalize_response_cache(record)
        await self._retire_terminal_stale_context(record)

    async def _complete(self, record: _TaskRecord, raw: Mapping[str, Any]) -> None:
        if record.cancel_requested:
            return
        if raw.get("interrupted") is True:
            await self._mark_terminal(
                record,
                state="TASK_STATE_CANCELED",
                failure_reason="AGENT_INTERRUPTED",
            )
            return
        error = raw.get("error")
        if error:
            reason = str(error)
            if reason not in protocol.RPC_ERROR_CODES:
                reason = "PROVIDER_ERROR"
            await self._mark_terminal(
                record,
                state="TASK_STATE_FAILED",
                failure_reason=reason,
            )
            return
        text = raw.get("final_response")
        if raw.get("completed") is not True or not isinstance(text, str) or not text:
            raise AgentMessageError("INVALID_AGENT_RESPONSE")
        if len(text.encode("utf-8")) > 24_576:
            raise AgentMessageError("INVALID_AGENT_RESPONSE")
        normalized_history, history_bytes = self._validate_history(
            raw.get("messages")
        )
        await self._ensure_history_capacity(
            record.conversation_key,
            history_bytes,
            deadline=self._monotonic() + float(protocol.CONTROL_TIMEOUT_S),
        )
        result_artifact = {
            "artifactId": _new_uuid(self._uuid_factory, "artifactId"),
            "name": "mclaw-result.txt",
            "description": "Final output produced by the remote M-Claw task.",
            "parts": [{"text": text, "mediaType": "text/plain"}],
            "metadata": {
                "mclaw.requestMessageId": record.message_id,
                "mclaw.artifactRole": "final-response",
            },
        }
        artifacts: list[Mapping[str, Any]] = [result_artifact]
        additional = raw.get("artifacts")
        if additional is not None:
            if not isinstance(additional, (tuple, list)) or len(additional) > 31:
                raise AgentMessageError("INVALID_AGENT_RESPONSE")
            for value in additional:
                if not isinstance(value, Mapping):
                    raise AgentMessageError("INVALID_AGENT_RESPONSE")
                candidate = _plain(value)
                if "artifactId" not in candidate:
                    candidate["artifactId"] = _new_uuid(
                        self._uuid_factory, "artifactId"
                    )
                try:
                    normalized = build_artifact_update(
                        task_id=record.task_id,
                        context_id=record.context_id,
                        artifact=candidate,
                    )["artifactUpdate"]["artifact"]
                except A2AError as error:
                    raise AgentMessageError(
                        "INVALID_AGENT_RESPONSE"
                    ) from error
                artifacts.append(normalized)
        artifact_ids = [str(value["artifactId"]) for value in artifacts]
        if len(artifact_ids) != len(set(artifact_ids)):
            raise AgentMessageError("INVALID_AGENT_RESPONSE")
        try:
            for artifact in artifacts:
                self._store.persist_artifact(
                    "owned",
                    record.peer_device_id,
                    record.task_id,
                    artifact,
                )
        except TaskStoreError as error:
            code = (
                "INVALID_AGENT_RESPONSE"
                if error.code
                in {
                    "ARTIFACT_CONFLICT",
                    "ARTIFACT_ID_INVALID",
                    "ARTIFACT_INVALID",
                    "ARTIFACT_PART_UNSUPPORTED",
                    "ARTIFACT_TOO_LARGE",
                }
                else "INTERNAL_ERROR"
            )
            raise AgentMessageError(code) from error
        self._commit_context_history(
            record.conversation_key,
            normalized_history,
            history_bytes,
        )
        agent_message = {
            "messageId": _new_uuid(self._uuid_factory, "history.messageId"),
            "contextId": record.context_id,
            "taskId": record.task_id,
            "role": "ROLE_AGENT",
            "parts": [{"text": text}],
            "metadata": {"mclaw.requestMessageId": record.message_id},
        }
        history = list(record.task.get("history", ())) + [agent_message]
        task = build_task(
            task_id=record.task_id,
            context_id=record.context_id,
            state="TASK_STATE_COMPLETED",
            history=history,
            artifacts=artifacts,
            metadata=record.task.get("metadata", {}),
        )
        self._save_task(record, task)
        self._release_peer_pending(record)
        for artifact in artifacts:
            self._publish(
                record,
                build_artifact_update(
                    task_id=record.task_id,
                    context_id=record.context_id,
                    artifact=artifact,
                ),
            )
        self._publish(
            record,
            build_status_update(
                task_id=record.task_id,
                context_id=record.context_id,
                state="TASK_STATE_COMPLETED",
            ),
        )
        self._close_streams(record)
        self._finalize_response_cache(record)
        await self._retire_terminal_stale_context(record)

    async def _execute(self, record: _TaskRecord) -> None:
        while self._local_turn_tokens and not record.cancel_requested:
            await self._local_turn_idle.wait()
        if record.cancel_requested:
            return
        if not self._accepting:
            await self._mark_terminal(
                record,
                state="TASK_STATE_FAILED",
                failure_reason="RUNTIME_STOPPING",
            )
            return
        provider = self._provider_runtime
        if not self._provider_ready or provider is None:
            await self._mark_terminal(
                record,
                state="TASK_STATE_FAILED",
                failure_reason="REMOTE_PROVIDER_UNAVAILABLE",
            )
            return
        request = RemoteTurnRequest(
            conversation_key=record.conversation_key,
            # Tasks have no implicit wall-clock deadline. ``None`` is the
            # model transport's explicit no-deadline value; non-finite
            # deadlines are rejected before an HTTP call is attempted.
            deadline_monotonic=None,
            history=tuple(_freeze(item) for item in record.history),
            message_id=record.message_id,
            provider_runtime=provider,
            text=record.text,
            task_id=record.task_id,
            event_sink=lambda event: self._agent_event(record, event),
        )
        estimate = self._executor.estimate_budget(request)
        self._reserve_tokens(estimate)
        record.token_reservation = estimate
        working = build_task(
            task_id=record.task_id,
            context_id=record.context_id,
            state="TASK_STATE_WORKING",
            history=record.task.get("history", ()),
            artifacts=record.task.get("artifacts", ()),
            metadata=record.task.get("metadata", {}),
        )
        self._save_task(record, working)
        self._publish(
            record,
            build_status_update(
                task_id=record.task_id,
                context_id=record.context_id,
                state="TASK_STATE_WORKING",
            ),
        )
        self._execution_count += 1
        self._execution_active += 1
        try:
            raw = await self._executor.execute(request)
        except asyncio.CancelledError:
            self._forfeit_token_reservation(record)
            if not record.cancel_requested:
                await self._mark_terminal(
                    record,
                    state="TASK_STATE_CANCELED",
                    failure_reason="AGENT_INTERRUPTED",
                )
            raise
        except Exception as error:  # noqa: BLE001 - provider isolation boundary
            self._forfeit_token_reservation(record)
            reason = str(getattr(error, "code", "PROVIDER_ERROR"))
            if reason not in protocol.RPC_ERROR_CODES:
                reason = "PROVIDER_ERROR"
            await self._mark_terminal(
                record,
                state="TASK_STATE_FAILED",
                failure_reason=reason,
            )
            return
        finally:
            self._execution_active -= 1
        if not isinstance(raw, Mapping):
            await self._mark_terminal(
                record,
                state="TASK_STATE_FAILED",
                failure_reason="INVALID_AGENT_RESPONSE",
            )
            return
        self._settle_tokens(record, raw)
        try:
            await self._complete(record, raw)
        except AgentMessageError as error:
            await self._mark_terminal(
                record,
                state="TASK_STATE_FAILED",
                failure_reason=error.code,
            )

    async def _consume(self) -> None:
        while True:
            try:
                record = await self._queue.get()
            except asyncio.CancelledError:
                return
            self._queue_bytes -= record.item_bytes
            self._queue.task_done()
            if record.cancel_requested:
                continue
            task = asyncio.create_task(
                self._execute(record),
                name=f"mclaw-dsoftbus-task-{record.task_id}",
            )
            record.execution = task
            try:
                await task
            except asyncio.CancelledError:
                if self._stopped:
                    return
            except Exception as error:  # noqa: BLE001 - task isolation boundary
                if not task_state_is_terminal(record.task["status"]["state"]):
                    reason = str(getattr(error, "code", "PROVIDER_ERROR"))
                    if reason not in protocol.RPC_ERROR_CODES:
                        reason = "PROVIDER_ERROR"
                    self._forfeit_token_reservation(record)
                    await self._mark_terminal(
                        record,
                        state="TASK_STATE_FAILED",
                        failure_reason=reason,
                    )
            finally:
                record.execution = None

    def get_task(
        self,
        peer_device_id: str,
        peer_runtime_instance_id: str,
        task_id: str,
    ) -> Mapping[str, Any]:
        peer_device_id, peer_runtime_instance_id = self._validate_peer(
            peer_device_id, peer_runtime_instance_id
        )
        task_id = _uuid4(task_id, "taskId")
        task = self._store.get_task("owned", peer_device_id, task_id)
        if task is None or task.get("metadata", {}).get(
            "mclaw.peerRuntimeInstanceId"
        ) != peer_runtime_instance_id:
            self._fail("TASK_NOT_FOUND")
        return task

    def list_tasks(
        self,
        peer_device_id: str,
        peer_runtime_instance_id: str,
        *,
        context_id: str | None,
        state: str | None,
        page_size: int,
        include_artifacts: bool,
    ) -> Mapping[str, Any]:
        peer_device_id, peer_runtime_instance_id = self._validate_peer(
            peer_device_id, peer_runtime_instance_id
        )
        tasks = self._store.list_tasks(
            "owned",
            peer_device_id,
            context_id=context_id,
            state=state,
            limit=100,
        )
        filtered = [
            task
            for task in tasks
            if task.get("metadata", {}).get("mclaw.peerRuntimeInstanceId")
            == peer_runtime_instance_id
        ]
        total = len(filtered)
        selected: list[Mapping[str, Any]] = []
        for task in filtered[:page_size]:
            value = _plain(task)
            if not include_artifacts:
                value.pop("artifacts", None)
            selected.append(_freeze(value))
        return _freeze(
            {
                "tasks": selected,
                "nextPageToken": "",
                "pageSize": page_size,
                "totalSize": total,
            }
        )

    async def cancel_task(
        self,
        peer_device_id: str,
        peer_runtime_instance_id: str,
        task_id: str,
    ) -> Mapping[str, Any]:
        task = self.get_task(
            peer_device_id, peer_runtime_instance_id, task_id
        )
        if task_state_is_terminal(task["status"]["state"]):
            self._fail("TASK_NOT_CANCELABLE")
        record = self._records.get(task_id)
        if record is None:
            self._fail("TASK_NOT_CANCELABLE")
        await self._cancel_record(
            record,
            failure_reason="AGENT_INTERRUPTED",
            deadline=self._monotonic() + float(protocol.CONTROL_TIMEOUT_S),
        )
        return record.task

    async def _cancel_record(
        self,
        record: _TaskRecord,
        *,
        failure_reason: str,
        deadline: float,
    ) -> bool:
        """Cancel execution and publish a terminal state only after OS cleanup."""
        record.cancel_requested = True
        execution = record.execution
        if execution is not None and not execution.done():
            execution.cancel()
        cleanup_confirmed = await self._executor.cancel_and_reap(
            record.session_id,
            record.task_id,
            deadline,
        )
        await self._mark_terminal(
            record,
            state=(
                "TASK_STATE_CANCELED"
                if cleanup_confirmed
                else "TASK_STATE_FAILED"
            ),
            failure_reason=(failure_reason if cleanup_confirmed else "INTERNAL_ERROR"),
        )
        return cleanup_confirmed

    def subscribe_task(
        self,
        peer_device_id: str,
        peer_runtime_instance_id: str,
        task_id: str,
    ) -> TaskSubscription:
        task = self.get_task(
            peer_device_id, peer_runtime_instance_id, task_id
        )
        if task_state_is_terminal(task["status"]["state"]):
            self._fail("UNSUPPORTED_OPERATION")
        record = self._records.get(task_id)
        if record is None:
            self._fail("TASK_NOT_FOUND")
        return self._new_subscription(record)

    def detach(self, subscription: TaskSubscription) -> None:
        record = self._records.get(subscription.task_id)
        if record is not None:
            record.subscribers.discard(subscription)
        subscription._close()

    def generation_closed(
        self,
        _peer_device_id: str,
        _peer_runtime_instance_id: str,
        _generation: int,
    ) -> None:
        # A SoftBus stream is a subscription, not Task ownership.  The Task
        # remains active so the caller can GetTask + SubscribeToTask after a
        # generation reconnects.
        return None

    def begin_shutdown(self) -> None:
        self._accepting = False

    async def drain(self, deadline: float) -> bool:
        self._accepting = False
        self._stopped = True
        for record in tuple(self._records.values()):
            if task_state_is_terminal(record.task["status"]["state"]):
                continue
            await self._cancel_record(
                record,
                failure_reason="RUNTIME_STOPPING",
                deadline=deadline,
            )
        worker = self._worker
        if worker is not None and not worker.done():
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        self._worker = None
        while True:
            try:
                queued = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._queue_bytes -= queued.item_bytes
            self._queue.task_done()
        if self._queue_bytes != 0 or self._peer_pending:
            raise AssertionError("task shutdown reservations were not released")
        self._records.clear()
        self._request_index.clear()
        self._contexts.clear()
        self._stale_peer_runtimes.clear()
        self._local_turn_tokens.clear()
        self._local_turn_idle.set()
        success = await self._executor.dispose(deadline)
        self._store.close()
        return success

    def diagnostic_snapshot(self) -> Mapping[str, Any]:
        now = self._monotonic()
        self._purge_response_cache(now)
        used = self._token_used(now)
        rejected = dict(sorted(self._rejected.items()))
        context_bytes = sum(
            context.byte_length for context in self._contexts.values()
        )
        budget_used = min(
            self._token_budget,
            used + self._token_reserved,
        )
        return MappingProxyType(
            {
                "activeThreadCount": 0,
                "agentIngressReservationCount": 0,
                "agentPendingCount": 0,
                "agentSessionTaskCount": self._execution_active,
                "completedMessageCount": sum(
                    1
                    for record in self._records.values()
                    if task_state_is_terminal(record.task["status"]["state"])
                ),
                "contextBytes": context_bytes,
                "contextCount": len(self._contexts),
                "dispatchExecutionCount": self._execution_count,
                "dispatchQueueBytes": self._queue_bytes,
                "dispatchQueueCount": self._queue.qsize(),
                "idempotencyWaiterBytes": 0,
                "idempotencyWaiterCount": 0,
                "inflightMessageCount": sum(
                    1
                    for record in self._records.values()
                    if not task_state_is_terminal(record.task["status"]["state"])
                ),
                "lateProviderResultCount": 0,
                "localTurnCount": len(self._local_turn_tokens),
                "remoteAccepted": self._remote_accepted,
                "remoteBudgetLimit": self._token_budget,
                "remoteBudgetUsed": budget_used,
                "remoteRateLimited": rejected.get("RATE_LIMITED", 0),
                "remoteRejected": sum(rejected.values()),
                "remoteRejectedByCode": MappingProxyType(rejected),
                "responseCacheBytes": self._response_cache_usage()[1],
                "responseCacheCount": len(self._records),
                "retiringContextCount": sum(
                    1
                    for key in self._contexts
                    if (
                        key.peer_device_id,
                        key.peer_runtime_instance_id,
                    )
                    in self._stale_peer_runtimes
                ),
                "tokenBudgetUsed": budget_used,
                "tokenReserved": self._token_reserved,
            }
        )


__all__ = [
    "DsoftbusTaskDispatcher",
    "TaskSubscription",
]
