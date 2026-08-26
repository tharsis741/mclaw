# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Long-lived A2A Task execution for authenticated DSoftBus peers."""

from __future__ import annotations

import asyncio
import base64
import copy
from collections import OrderedDict, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import hashlib
import json
import logging
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
    task_state_closes_stream,
    task_state_is_terminal,
)
from .agent_message import (
    AgentMessageError,
    ConversationKey,
    RemoteTurnExecutor,
    RemoteTurnRequest,
)
from .task_files import (
    InboundTaskFileStore,
    TaskFileError,
    TaskInputByteBudget,
    TaskInputDescriptor,
    normalize_task_input_manifest,
)
from .a2a_media import task_input_manifest_from_parts
from .task_store import DsoftbusTaskStore, TaskStoreError
from .task_input_request import (
    TaskInputRequestError,
    normalize_input_request,
)
from .workspace import (
    DsoftbusWorkspace,
    RemoteWorkspaceError,
    TaskWorkspacePaths,
)


_DEVICE_ID = re.compile(r"^urn:mclaw:device:oh:[0-9a-f]{64}$")
_RATE_WINDOW_S = 60.0
_TOKEN_WINDOW_S = 3600.0
_TERMINAL_SENTINEL = object()
logger = logging.getLogger(__name__)


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return copy.deepcopy(value)


def _task_input_context(
    work_root: Path,
    descriptors: tuple[TaskInputDescriptor, ...],
    *,
    heading: str,
) -> str:
    """Describe verified B-local files without leaking an A-local path."""

    if not descriptors:
        return ""
    lines = [heading]
    image_paths: list[str] = []
    unsupported_media: list[str] = []
    for descriptor in descriptors:
        local_path = str((work_root / Path(descriptor.relative_path)).resolve())
        lines.append(
            f"- {local_path} [{descriptor.media_type}, "
            f"{descriptor.byte_length} bytes]"
        )
        if descriptor.media_type.startswith("image/"):
            image_paths.append(local_path)
        elif descriptor.media_type.startswith(("audio/", "video/")):
            unsupported_media.append(local_path)
    lines.append(
        "以上文件是本任务在本设备上的工作副本，可以按照任务要求读取或修改。"
    )
    if image_paths:
        lines.append(
            "需要理解图像内容时，对相应绝对路径调用 "
            "vision_analyze；不要仅依据文件名猜测图像内容。"
        )
    if unsupported_media:
        lines.append(
            "音频或视频已作为本地文件提供；仅在本机存在相应分析工具时处理，"
            "否则明确说明当前只能访问文件字节。"
        )
    return "\n".join(lines)


def _task_input_attachments(
    work_root: Path | None,
    descriptors: tuple[TaskInputDescriptor, ...],
) -> tuple[Any, ...]:
    """Map A2A media Parts to the shared B-local channel representation."""

    if work_root is None or not descriptors:
        return ()
    from mclaw.channels.base import (
        AttachmentKind,
        AttachmentOrigin,
        ChannelAttachment,
    )

    attachments: list[ChannelAttachment] = []
    for descriptor in descriptors:
        if descriptor.media_type.startswith("image/"):
            kind = AttachmentKind.IMAGE
        elif descriptor.media_type.startswith("audio/"):
            kind = AttachmentKind.AUDIO
        elif descriptor.media_type.startswith("video/"):
            kind = AttachmentKind.VIDEO
        else:
            kind = AttachmentKind.FILE
        attachments.append(
            ChannelAttachment(
                kind=kind,
                origin=AttachmentOrigin.DSOFTBUS,
                path=str(
                    (work_root / Path(descriptor.relative_path)).resolve()
                ),
                filename=descriptor.filename,
                mime_type=descriptor.media_type,
                size_bytes=descriptor.byte_length,
                metadata={
                    "input_id": descriptor.input_id,
                    "relative_path": descriptor.relative_path,
                    "sha256": descriptor.sha256,
                    "managed_cache": False,
                },
            )
        )
    return tuple(attachments)


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
    input_byte_budget: TaskInputByteBudget
    workspace_path: str = ""
    system_context: str = ""
    workspace_paths: TaskWorkspacePaths | None = None
    input_store: InboundTaskFileStore | None = None
    input_descriptors: tuple[TaskInputDescriptor, ...] = ()
    source_scopes: tuple[Mapping[str, Any], ...] = ()
    source_client: Any | None = None
    input_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    subscribers: set[TaskSubscription] = field(default_factory=set)
    execution: asyncio.Task[None] | None = None
    token_reservation: int = 0
    cancel_requested: bool = False
    peer_pending_reserved: bool = False
    cache_bytes: int = protocol.REMOTE_FRAME_MAX
    cache_expires_at: float | None = None
    result_acknowledged: bool = False
    dispatch_reserved: bool = False
    enqueued: bool = False
    input_sequence: int = 0
    cancel_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    @property
    def session_id(self) -> str:
        return self.conversation_key.task_session_id(self.task_id)


@dataclass(slots=True)
class _TaskContext:
    history: list[dict[str, Any]] = field(default_factory=list)
    byte_length: int = 2


@dataclass(frozen=True, slots=True)
class _LeaseReplay:
    sequence: int
    task_ids: tuple[str, ...]
    result: Mapping[str, Any]


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
        workspace: DsoftbusWorkspace | None = None,
        source_client_factory: Callable[..., Any] | None = None,
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
            or not 1 <= source["per_peer_requests_per_minute"] <= 60
            or not source["per_peer_requests_per_minute"]
            <= source["global_requests_per_minute"]
            <= 240
            or (
                source["remote_token_budget_per_hour"] is not None
                and (
                    type(source["remote_token_budget_per_hour"]) is not int
                    or not 1_000
                    <= source["remote_token_budget_per_hour"]
                    <= 10_000_000
                )
            )
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
        if workspace is not None and not isinstance(workspace, DsoftbusWorkspace):
            raise TypeError("workspace must be a DsoftbusWorkspace or None")
        self._workspace = workspace
        self._store = task_store or DsoftbusTaskStore(
            state_root,
            artifact_workspace=workspace,
        )
        if source_client_factory is not None and not callable(source_client_factory):
            raise TypeError("source_client_factory must be callable or None")
        self._source_client_factory = source_client_factory
        self._store.recover_interrupted_owned_tasks()
        self._queue: asyncio.Queue[_TaskRecord] = asyncio.Queue(
            maxsize=protocol.DISPATCH_QUEUE_MAX
        )
        self._queue_bytes = 0
        self._dispatch_pending_count = 0
        self._records: OrderedDict[str, _TaskRecord] = OrderedDict()
        self._request_index: dict[tuple[str, str, str], tuple[str, str]] = {}
        self._contexts: OrderedDict[ConversationKey, _TaskContext] = OrderedDict()
        self._stale_peer_runtimes: set[tuple[str, str]] = set()
        self._suspect_peer_runtimes: set[tuple[str, str]] = set()
        self._current_peer_runtime: dict[str, str] = {}
        self._owner_leases: dict[tuple[str, str, str], float] = {}
        self._lease_replays: dict[tuple[str, str], _LeaseReplay] = {}
        self._peer_pending: dict[tuple[str, str], int] = {}
        self._global_rate: deque[float] = deque()
        self._peer_rates: dict[str, deque[float]] = {}
        self._token_charges: deque[tuple[float, int]] = deque()
        self._token_reserved = 0
        self._local_turn_tokens: set[str] = set()
        self._local_turn_idle = asyncio.Event()
        self._local_turn_idle.set()
        self._worker: asyncio.Task[None] | None = None
        self._lease_worker: asyncio.Task[None] | None = None
        self._accepting = True
        self._stopped = False
        self._execution_count = 0
        self._execution_active = 0
        self._remote_accepted = 0
        self._rejected: dict[str, int] = {}
        self._lease_renewed = 0
        self._lease_expired = 0
        self._runtime_replaced = 0
        now = self._monotonic()
        for peer_device_id, task in self._store.list_owned_nonterminal_tasks():
            metadata = task.get("metadata", {})
            runtime_id = (
                metadata.get("mclaw.peerRuntimeInstanceId")
                if isinstance(metadata, Mapping)
                else None
            )
            try:
                peer_device_id, runtime_id = self._validate_peer(
                    peer_device_id, runtime_id
                )
                task_id = _uuid4(task.get("id"), "taskId")
            except AgentMessageError as error:
                raise TaskStoreError("TASK_STATE_CORRUPT") from error
            self._owner_leases[
                (peer_device_id, runtime_id, task_id)
            ] = now + float(protocol.TASK_OWNER_LEASE_TIMEOUT_S)

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
        if (
            self._token_budget is not None
            and self._token_used(self._monotonic()) + self._token_reserved
            > self._token_budget - amount
        ):
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
        if self._token_budget is not None:
            amount = min(
                amount,
                max(
                    0,
                    self._token_budget
                    - self._token_used(self._monotonic()),
                ),
            )
        if amount:
            self._token_charges.append((self._monotonic(), amount))

    def _forfeit_token_reservation(self, record: _TaskRecord) -> None:
        reserved = record.token_reservation
        if reserved <= 0:
            return
        self._token_reserved -= reserved
        record.token_reservation = 0
        amount = reserved
        if self._token_budget is not None:
            amount = min(
                reserved,
                max(
                    0,
                    self._token_budget
                    - self._token_used(self._monotonic()),
                ),
            )
        if amount:
            self._token_charges.append((self._monotonic(), amount))

    def start(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(
                self._consume(), name="mclaw-dsoftbus-agent-task-dispatch"
            )
        if self._lease_worker is None or self._lease_worker.done():
            self._lease_worker = asyncio.create_task(
                self._sweep_owner_leases(),
                name="mclaw-dsoftbus-task-owner-lease",
            )

    @staticmethod
    def _lease_key(record: _TaskRecord) -> tuple[str, str, str]:
        return (
            record.peer_device_id,
            record.peer_runtime_instance_id,
            record.task_id,
        )

    def _renew_record_lease(self, record: _TaskRecord) -> None:
        self._owner_leases[self._lease_key(record)] = (
            self._monotonic() + float(protocol.TASK_OWNER_LEASE_TIMEOUT_S)
        )

    def _forget_record_lease(self, record: _TaskRecord) -> None:
        self._owner_leases.pop(self._lease_key(record), None)

    async def _clear_orphan_workspace(
        self,
        peer_device_id: str,
        task_id: str,
        *,
        record: _TaskRecord | None,
    ) -> None:
        """Release bytes that no owner Runtime can acknowledge later."""

        if self._workspace is not None:
            try:
                self._workspace.clear_task(
                    "executing", peer_device_id, task_id
                )
                self._workspace.clear_artifacts(
                    "produced", peer_device_id, task_id
                )
            except RemoteWorkspaceError as error:
                raise AgentMessageError("INTERNAL_ERROR") from error
        self._store.forget_artifact_transfers(
            "owned", peer_device_id, task_id
        )
        if record is not None:
            record.result_acknowledged = True

    async def _terminalize_owner_lease(
        self,
        key: tuple[str, str, str],
        *,
        failure_reason: str,
    ) -> bool:
        """Cancel one active or persisted Task after ownership is lost."""

        peer_device_id, peer_runtime_instance_id, task_id = key
        self._owner_leases.pop(key, None)
        record = self._records.get(task_id)
        if (
            record is not None
            and self._lease_key(record) == key
            and not task_state_is_terminal(record.task["status"]["state"])
        ):
            cleanup_confirmed = await self._cancel_record(
                record,
                failure_reason=failure_reason,
                deadline=(
                    self._monotonic() + float(protocol.CONTROL_TIMEOUT_S)
                ),
            )
            if cleanup_confirmed:
                await self._clear_orphan_workspace(
                    peer_device_id,
                    task_id,
                    record=record,
                )
            return True
        try:
            task = self._store.cancel_owned_nonterminal_task(
                peer_device_id,
                peer_runtime_instance_id,
                task_id,
                failure_reason,
            )
        except TaskStoreError as error:
            raise AgentMessageError("INTERNAL_ERROR") from error
        if task is None:
            return False
        await self._clear_orphan_workspace(
            peer_device_id,
            task_id,
            record=None,
        )
        return True

    async def expire_owner_leases(self, now: float | None = None) -> int:
        """Cancel all Tasks whose authenticated owner stopped renewing."""

        current = self._monotonic() if now is None else float(now)
        expired = tuple(
            key
            for key, deadline in self._owner_leases.items()
            if deadline <= current
        )
        if not expired:
            return 0
        results = await asyncio.gather(
            *(
                self._terminalize_owner_lease(
                    key,
                    failure_reason="OWNER_LEASE_EXPIRED",
                )
                for key in expired
                if self._owner_leases.get(key, current + 1.0) <= current
            )
        )
        count = sum(result is True for result in results)
        self._lease_expired += count
        return count

    async def _sweep_owner_leases(self) -> None:
        while not self._stopped:
            now = self._monotonic()
            try:
                await self.expire_owner_leases(now)
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001 - keep the owner watchdog alive
                logger.exception("DSoftBus Task owner lease sweep failed")
            if self._stopped:
                return
            now = self._monotonic()
            next_deadline = min(
                self._owner_leases.values(),
                default=now + float(protocol.TASK_OWNER_LEASE_RENEW_INTERVAL_S),
            )
            delay = max(
                0.05,
                min(
                    float(protocol.TASK_OWNER_LEASE_RENEW_INTERVAL_S),
                    next_deadline - now,
                ),
            )
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return

    async def peer_runtime_ready(
        self,
        peer_device_id: str,
        peer_runtime_instance_id: str,
    ) -> int:
        """Reconcile Task ownership when an authenticated Runtime binds.

        A socket generation reconnect with the same Runtime identity is only a
        transport recovery.  A different Runtime identity on the same device
        cannot inherit the predecessor's live Tasks.
        """

        peer_device_id, peer_runtime_instance_id = self._validate_peer(
            peer_device_id, peer_runtime_instance_id
        )
        self._current_peer_runtime[peer_device_id] = peer_runtime_instance_id
        for key in tuple(self._lease_replays):
            if key[0] == peer_device_id and key[1] != peer_runtime_instance_id:
                self._lease_replays.pop(key, None)
        self._suspect_peer_runtimes.discard(
            (peer_device_id, peer_runtime_instance_id)
        )
        replaced = tuple(
            key
            for key in self._owner_leases
            if key[0] == peer_device_id
            and key[1] != peer_runtime_instance_id
        )
        if not replaced:
            return 0
        self._stale_peer_runtimes.update(
            (key[0], key[1]) for key in replaced
        )
        results = await asyncio.gather(
            *(
                self._terminalize_owner_lease(
                    key,
                    failure_reason="OWNER_RUNTIME_REPLACED",
                )
                for key in replaced
            )
        )
        count = sum(result is True for result in results)
        self._runtime_replaced += count
        await self._retire_replaced_peer_contexts(
            peer_device_id,
            peer_runtime_instance_id,
            deadline=(
                self._monotonic() + float(protocol.CONTROL_TIMEOUT_S)
            ),
        )
        return count

    async def renew_owner_leases(
        self,
        peer_device_id: str,
        peer_runtime_instance_id: str,
        *,
        sequence: int,
        task_ids: tuple[str, ...] | list[str],
    ) -> Mapping[str, Any]:
        """Apply one replay-safe bounded renewal batch from the Task owner."""

        peer_device_id, peer_runtime_instance_id = self._validate_peer(
            peer_device_id, peer_runtime_instance_id
        )
        if type(sequence) is not int or not 1 <= sequence <= 2**63 - 1:
            self._fail("INVALID_PARAMS")
        if (
            not isinstance(task_ids, (tuple, list))
            or not 1 <= len(task_ids) <= protocol.TASK_OWNER_LEASE_BATCH_MAX
        ):
            self._fail("INVALID_PARAMS")
        normalized = tuple(_uuid4(task_id, "taskId") for task_id in task_ids)
        if len(set(normalized)) != len(normalized):
            self._fail("INVALID_PARAMS")
        current_runtime = self._current_peer_runtime.get(peer_device_id)
        if (
            current_runtime is not None
            and current_runtime != peer_runtime_instance_id
        ):
            self._fail("STALE_GENERATION")
        self._current_peer_runtime.setdefault(
            peer_device_id, peer_runtime_instance_id
        )
        replay_key = (peer_device_id, peer_runtime_instance_id)
        prior = self._lease_replays.get(replay_key)
        if prior is not None:
            if sequence < prior.sequence or (
                sequence == prior.sequence and normalized != prior.task_ids
            ):
                self._fail("INVALID_REQUEST")
            if sequence == prior.sequence:
                return prior.result
        now = self._monotonic()
        await self.expire_owner_leases(now)
        renewed: list[str] = []
        unavailable: list[str] = []
        deadline = now + float(protocol.TASK_OWNER_LEASE_TIMEOUT_S)
        for task_id in normalized:
            key = (peer_device_id, peer_runtime_instance_id, task_id)
            if key not in self._owner_leases:
                unavailable.append(task_id)
                continue
            self._owner_leases[key] = deadline
            renewed.append(task_id)
        result = _freeze(
            {
                "sequence": sequence,
                "renewedTaskIds": renewed,
                "unavailableTaskIds": unavailable,
                "leaseSeconds": protocol.TASK_OWNER_LEASE_TIMEOUT_S,
            }
        )
        self._lease_replays[replay_key] = _LeaseReplay(
            sequence=sequence,
            task_ids=normalized,
            result=result,
        )
        self._suspect_peer_runtimes.discard(replay_key)
        self._lease_renewed += len(renewed)
        return result

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
        if not isinstance(value, (tuple, list)) or any(
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

    def _continuation_identity(
        self,
        peer_device_id: str,
        peer_runtime_instance_id: str,
        call: CoreMethodCall,
    ) -> tuple[str, str, str, str, str, str]:
        if call.method not in {"SendMessage", "SendStreamingMessage"}:
            self._fail("INVALID_PARAMS")
        message = call.params.get("message")
        if not isinstance(message, Mapping):
            self._fail("INVALID_PARAMS")
        message_id = _uuid4(message.get("messageId"), "messageId")
        context_id = _uuid4(message.get("contextId"), "contextId")
        task_id = _uuid4(message.get("taskId"), "taskId")
        metadata = message.get("metadata")
        if not isinstance(metadata, Mapping):
            self._fail("INPUT_REQUEST_MISMATCH")
        request_id = _uuid4(
            metadata.get("mclaw.inputRequestId"),
            "inputRequestId",
        )
        if not isinstance(call.normalized_text, str):
            self._fail("INVALID_PARAMS")
        digest = hashlib.sha256(
            b"mclaw-a2a-task-continuation\0"
            + peer_device_id.encode("utf-8")
            + b"\0"
            + peer_runtime_instance_id.encode("ascii")
            + b"\0"
            + _canonical(call.params)
        ).hexdigest()
        return (
            message_id,
            context_id,
            task_id,
            request_id,
            call.normalized_text,
            digest,
        )

    @staticmethod
    def _input_manifest(call: CoreMethodCall) -> Mapping[str, Any] | None:
        message = call.params.get("message")
        if not isinstance(message, Mapping):
            return None
        try:
            return task_input_manifest_from_parts(message.get("parts", ()))
        except TaskFileError as error:
            raise AgentMessageError(error.code) from error

    def _new_subscription(self, record: _TaskRecord) -> TaskSubscription:
        subscription = self._task_subscription(record.task)
        record.subscribers.add(subscription)
        if task_state_closes_stream(record.task["status"]["state"]):
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
        if task_state_closes_stream(task["status"]["state"]):
            subscription._close()
        return subscription

    async def _rehydrate_waiting_record(
        self,
        *,
        peer_device_id: str,
        peer_runtime_instance_id: str,
        task: Mapping[str, Any],
        wait: Mapping[str, Any],
    ) -> _TaskRecord:
        """Restore private INPUT_REQUIRED state after a Runtime restart."""

        task_id = _uuid4(task.get("id"), "taskId")
        context_id = _uuid4(task.get("contextId"), "contextId")
        history, history_bytes = self._validate_history(wait.get("history"))
        system_context = wait.get("systemContext", "")
        if not isinstance(system_context, str) or len(
            system_context.encode("utf-8")
        ) > protocol.REMOTE_CONTEXT_UTF8_MAX:
            self._fail("INTERNAL_ERROR")
        raw_scopes = wait.get("sourceScopes", ())
        if not isinstance(raw_scopes, (tuple, list)):
            self._fail("INTERNAL_ERROR")
        try:
            source_scopes = (
                tuple(
                    normalize_task_input_manifest(
                        {"files": [], "sourceScopes": list(raw_scopes)}
                    )["sourceScopes"]
                )
                if raw_scopes
                else ()
            )
        except TaskFileError:
            self._fail("INTERNAL_ERROR")
            raise AssertionError("unreachable")
        raw_manifest = wait.get("inputManifest")
        descriptors: tuple[TaskInputDescriptor, ...] = ()
        if raw_manifest is not None:
            try:
                manifest = normalize_task_input_manifest(raw_manifest)
                descriptors = tuple(
                    TaskInputDescriptor.from_wire(value)
                    for value in manifest["files"]
                )
            except TaskFileError:
                self._fail("INTERNAL_ERROR")
                raise AssertionError("unreachable")
        descriptor_bytes = sum(value.byte_length for value in descriptors)
        input_bytes_used = wait.get("inputBytesUsed", descriptor_bytes)
        if (
            type(input_bytes_used) is not int
            or not descriptor_bytes <= input_bytes_used
            or input_bytes_used > protocol.TASK_INPUT_TASK_BYTES_MAX
        ):
            self._fail("INTERNAL_ERROR")
        try:
            input_byte_budget = TaskInputByteBudget(input_bytes_used)
        except TaskFileError:
            self._fail("INTERNAL_ERROR")
            raise AssertionError("unreachable")
        if self._workspace is None:
            self._fail("INTERNAL_ERROR")
        try:
            task_paths = self._workspace.ensure_task(
                "executing", peer_device_id, task_id
            )
        except RemoteWorkspaceError:
            self._fail("INTERNAL_ERROR")
            raise AssertionError("unreachable")
        if task_paths.work is None:
            self._fail("INTERNAL_ERROR")
        try:
            input_store = (
                InboundTaskFileStore(task_paths, descriptors)
                if raw_manifest is not None
                else None
            )
        except TaskFileError:
            self._fail("INTERNAL_ERROR")
            raise AssertionError("unreachable")
        conversation = ConversationKey(
            peer_device_id,
            peer_runtime_instance_id,
            context_id,
        )
        if conversation not in self._contexts:
            await self._ensure_context_capacity(
                (peer_device_id, peer_runtime_instance_id),
                deadline=self._monotonic() + float(protocol.CONTROL_TIMEOUT_S),
            )
            self._contexts[conversation] = _TaskContext(
                history=copy.deepcopy(history),
                byte_length=history_bytes,
            )
        source_client: Any | None = None
        if source_scopes:
            if self._source_client_factory is None:
                self._fail("INTERNAL_ERROR")
            try:
                source_client = self._source_client_factory(
                    peer_device_id=peer_device_id,
                    task_id=task_id,
                    workspace=task_paths,
                    source_scopes=source_scopes,
                    input_byte_budget=input_byte_budget,
                )
            except Exception as error:
                self._fail("INTERNAL_ERROR")
                raise AssertionError("unreachable") from error
        metadata = task.get("metadata", {})
        if not isinstance(metadata, Mapping):
            self._fail("INTERNAL_ERROR")
        sequence = wait.get("sequence")
        if type(sequence) is not int or sequence <= 0:
            self._fail("INTERNAL_ERROR")
        text = wait.get("continuationText", "")
        if not isinstance(text, str):
            self._fail("INTERNAL_ERROR")
        record = _TaskRecord(
            task_id=task_id,
            context_id=context_id,
            message_id=str(metadata.get("mclaw.requestMessageId", "")),
            peer_device_id=peer_device_id,
            peer_runtime_instance_id=peer_runtime_instance_id,
            conversation_key=conversation,
            request_digest=str(metadata.get("mclaw.requestDigest", "")),
            text=text,
            history=history,
            task=task,
            item_bytes=len(_canonical({"task": task, "history": history})),
            input_byte_budget=input_byte_budget,
            workspace_path=str(task_paths.work.resolve()),
            system_context=system_context,
            workspace_paths=task_paths,
            input_store=input_store,
            input_descriptors=descriptors,
            source_scopes=source_scopes,
            source_client=source_client,
            input_sequence=sequence,
        )
        self._records[task_id] = record
        self._renew_record_lease(record)
        return record

    def _reserve_continuation_dispatch(self, record: _TaskRecord) -> None:
        if record.dispatch_reserved or record.enqueued:
            return
        peer_key = (
            record.peer_device_id,
            record.peer_runtime_instance_id,
        )
        if self._peer_pending.get(peer_key, 0) >= protocol.PER_PEER_DISPATCH_PENDING_MAX:
            self._fail("CAPACITY_BUSY")
        item_bytes = len(
            _canonical(
                {
                    "task": record.task,
                    "text": record.text,
                    "history": record.history,
                }
            )
        )
        if (
            self._dispatch_pending_count >= protocol.DISPATCH_QUEUE_MAX
            or item_bytes > protocol.DISPATCH_QUEUE_BYTES_MAX
            or self._queue_bytes > protocol.DISPATCH_QUEUE_BYTES_MAX - item_bytes
        ):
            self._fail("CAPACITY_BUSY")
        record.item_bytes = item_bytes
        self._peer_pending[peer_key] = self._peer_pending.get(peer_key, 0) + 1
        record.peer_pending_reserved = True
        self._queue_bytes += item_bytes
        self._dispatch_pending_count += 1
        record.dispatch_reserved = True

    def _resume_record(self, record: _TaskRecord) -> None:
        """Atomically publish WORKING before an accepted continuation executes."""

        if record.enqueued:
            return
        if record.input_store is not None and not record.input_store.ready:
            self._fail("TRANSFER_CONFLICT")
        state = str(record.task["status"]["state"])
        if state == "TASK_STATE_INPUT_REQUIRED":
            task = build_task(
                task_id=record.task_id,
                context_id=record.context_id,
                state="TASK_STATE_WORKING",
                history=record.task.get("history", ()),
                artifacts=record.task.get("artifacts", ()),
                metadata=record.task.get("metadata", {}),
            )
            self._save_task(record, task)
            self._publish(
                record,
                build_status_update(
                    task_id=record.task_id,
                    context_id=record.context_id,
                    state="TASK_STATE_WORKING",
                ),
            )
        elif state not in {"TASK_STATE_SUBMITTED", "TASK_STATE_WORKING"}:
            self._fail("TASK_NOT_INPUT_REQUIRED")
        self._queue.put_nowait(record)
        record.enqueued = True
        self.start()

    async def _continue_task(
        self,
        *,
        peer_device_id: str,
        peer_runtime_instance_id: str,
        call: CoreMethodCall,
        subscribe: bool,
    ) -> tuple[Mapping[str, Any], TaskSubscription | None]:
        await self.expire_owner_leases()
        (
            message_id,
            context_id,
            task_id,
            request_id,
            text,
            digest,
        ) = self._continuation_identity(
            peer_device_id,
            peer_runtime_instance_id,
            call,
        )
        task = self.get_task(
            peer_device_id,
            peer_runtime_instance_id,
            task_id,
        )
        lease_key = (
            peer_device_id,
            peer_runtime_instance_id,
            task_id,
        )
        if lease_key not in self._owner_leases:
            self._fail("TASK_NOT_INPUT_REQUIRED")
        self._owner_leases[lease_key] = (
            self._monotonic() + float(protocol.TASK_OWNER_LEASE_TIMEOUT_S)
        )
        if str(task["contextId"]) != context_id:
            self._fail("INPUT_REQUEST_MISMATCH")
        try:
            wait = self._store.get_input_wait(peer_device_id, task_id)
        except TaskStoreError:
            self._fail("INTERNAL_ERROR")
            raise AssertionError("unreachable")
        if wait is None or wait.get("requestId") != request_id:
            self._fail("INPUT_REQUEST_MISMATCH")
        accepted_message_id = wait.get("acceptedMessageId", "")
        accepted_digest = wait.get("acceptedDigest", "")
        if accepted_message_id:
            if accepted_message_id != message_id or accepted_digest != digest:
                self._fail("INPUT_REQUEST_MISMATCH")
            record = self._records.get(task_id)
            if record is None and task["status"]["state"] == "TASK_STATE_INPUT_REQUIRED":
                record = await self._rehydrate_waiting_record(
                    peer_device_id=peer_device_id,
                    peer_runtime_instance_id=peer_runtime_instance_id,
                    task=task,
                    wait=wait,
                )
                self._reserve_continuation_dispatch(record)
                if wait.get("inputManifest") is None:
                    self._resume_record(record)
            subscription = (
                self._new_subscription(record)
                if subscribe and record is not None
                else self._task_subscription(task)
                if subscribe
                else None
            )
            return task, subscription
        if task["status"]["state"] != "TASK_STATE_INPUT_REQUIRED":
            self._fail("TASK_NOT_INPUT_REQUIRED")
        try:
            request = normalize_input_request(wait.get("request"))
        except TaskInputRequestError:
            self._fail("INTERNAL_ERROR")
            raise AssertionError("unreachable")
        manifest = self._input_manifest(call)
        actual_kinds: set[str] = set()
        if text:
            actual_kinds.add("text")
        descriptors: tuple[TaskInputDescriptor, ...] = ()
        new_scopes: tuple[Mapping[str, Any], ...] = ()
        if manifest is not None:
            descriptors = tuple(
                TaskInputDescriptor.from_wire(value)
                for value in manifest["files"]
            )
            new_scopes = tuple(manifest["sourceScopes"])
            if descriptors:
                actual_kinds.add("file")
            if new_scopes:
                actual_kinds.add("directory")
        if not actual_kinds or not actual_kinds.issubset(set(request["accepts"])):
            self._fail("INPUT_REQUEST_MISMATCH")
        self._charge_rate(peer_device_id, self._monotonic())
        record = self._records.get(task_id)
        if record is None:
            record = await self._rehydrate_waiting_record(
                peer_device_id=peer_device_id,
                peer_runtime_instance_id=peer_runtime_instance_id,
                task=task,
                wait=wait,
            )
        if record.workspace_paths is None or record.workspace_paths.work is None:
            self._fail("INTERNAL_ERROR")
        context_lines: list[str] = []
        if descriptors:
            context_lines.append(
                _task_input_context(
                    record.workspace_paths.work,
                    descriptors,
                    heading=(
                        "对端为本任务补充了输入文件；完成校验后可处理以下工作副本："
                    ),
                )
            )
        if new_scopes:
            context_lines.extend(
                [
                    "对端为本任务补充了只读目录范围：",
                    *(
                        f"- {scope['name']}（scopeId: {scope['scopeId']}）"
                        for scope in new_scopes
                    ),
                ]
            )
        candidate_system_context = record.system_context
        if context_lines:
            candidate_system_context = (
                f"{record.system_context}\n\n" + "\n".join(context_lines)
            ).strip()
        if (
            len(candidate_system_context.encode("utf-8"))
            > protocol.REMOTE_CONTEXT_UTF8_MAX
        ):
            self._fail("TASK_INPUT_INVALID")
        if manifest is not None:
            known_input_ids = {
                descriptor.input_id
                for descriptor in record.input_descriptors
            }
            if any(
                descriptor.input_id in known_input_ids
                for descriptor in descriptors
            ):
                self._fail("TASK_INPUT_INVALID")
            known_scope_ids = {
                str(scope["scopeId"]) for scope in record.source_scopes
            }
            if (
                len(record.source_scopes) + len(new_scopes)
                > protocol.TASK_INPUT_PATH_MAX
                or any(
                    str(scope["scopeId"]) in known_scope_ids
                    for scope in new_scopes
                )
            ):
                self._fail("TASK_INPUT_INVALID")
            if (
                new_scopes
                and record.source_client is None
                and self._source_client_factory is None
            ):
                self._fail("INTERNAL_ERROR")
            new_input_bytes = sum(
                descriptor.byte_length for descriptor in descriptors
            )
            reserved = False
            created_source_client: Any | None = None
            try:
                candidate_input_store = InboundTaskFileStore(
                    record.workspace_paths,
                    descriptors,
                )
                record.input_byte_budget.reserve(
                    new_input_bytes,
                    error_code="TASK_INPUT_TOO_LARGE",
                )
                reserved = True
                if new_scopes:
                    if record.source_client is None:
                        assert self._source_client_factory is not None
                        created_source_client = self._source_client_factory(
                            peer_device_id=peer_device_id,
                            task_id=task_id,
                            workspace=record.workspace_paths,
                            source_scopes=(*record.source_scopes, *new_scopes),
                            input_byte_budget=record.input_byte_budget,
                        )
                    else:
                        record.source_client.add_scopes(new_scopes)
            except TaskFileError as error:
                if reserved:
                    record.input_byte_budget.release(new_input_bytes)
                close = getattr(created_source_client, "close", None)
                if callable(close):
                    close()
                self._fail(error.code)
            except Exception:
                if reserved:
                    record.input_byte_budget.release(new_input_bytes)
                close = getattr(created_source_client, "close", None)
                if callable(close):
                    close()
                self._fail("INTERNAL_ERROR")
            record.input_store = candidate_input_store
            record.input_descriptors = (
                *record.input_descriptors,
                *descriptors,
            )
            if created_source_client is not None:
                record.source_client = created_source_client
            record.source_scopes = (*record.source_scopes, *new_scopes)
        record.system_context = candidate_system_context
        record.text = text or "对端已提供请求的补充文件或目录范围。"
        record.message_id = message_id
        user_message = _plain(call.params["message"])
        history = [*record.task.get("history", ()), user_message]
        history = history[-protocol.TASK_HISTORY_MAX :]
        task = build_task(
            task_id=record.task_id,
            context_id=record.context_id,
            state="TASK_STATE_INPUT_REQUIRED",
            history=history,
            artifacts=record.task.get("artifacts", ()),
            status_message=record.task["status"].get("message"),
            metadata=record.task.get("metadata", {}),
        )
        self._save_task(record, task)
        updated_wait = {
            key: _plain(value)
            for key, value in wait.items()
            if key != "requestId"
        }
        updated_wait.update(
            {
                "acceptedMessageId": message_id,
                "acceptedDigest": digest,
                "continuationText": record.text,
                "inputManifest": None if manifest is None else _plain(manifest),
                "inputBytesUsed": record.input_byte_budget.used_bytes,
                "systemContext": record.system_context,
                "sourceScopes": [_plain(value) for value in record.source_scopes],
            }
        )
        try:
            self._store.put_input_wait(
                peer_device_id,
                task_id,
                request_id,
                updated_wait,
            )
        except TaskStoreError:
            self._fail("INTERNAL_ERROR")
        self._reserve_continuation_dispatch(record)
        if manifest is None:
            self._resume_record(record)
        subscription = self._new_subscription(record) if subscribe else None
        return record.task, subscription

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
        message = call.params.get("message")
        if isinstance(message, Mapping) and message.get("taskId") not in {
            None,
            "",
        }:
            return await self._continue_task(
                peer_device_id=peer_device_id,
                peer_runtime_instance_id=peer_runtime_instance_id,
                call=call,
                subscribe=subscribe,
            )
        message_id, requested_context, text, digest = self._request_identity(
            peer_device_id, peer_runtime_instance_id, call
        )
        input_manifest = self._input_manifest(call)
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
            context = _TaskContext()
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
            self._dispatch_pending_count >= protocol.DISPATCH_QUEUE_MAX
            or item_bytes > protocol.DISPATCH_QUEUE_BYTES_MAX
            or self._queue_bytes > protocol.DISPATCH_QUEUE_BYTES_MAX - item_bytes
        ):
            if requested_context is None:
                self._contexts.pop(conversation, None)
            self._fail("CAPACITY_BUSY")
        workspace_path = ""
        system_context = ""
        task_paths: TaskWorkspacePaths | None = None
        if self._workspace is not None:
            try:
                task_paths = self._workspace.ensure_task(
                    "executing",
                    peer_device_id,
                    task_id,
                )
                if task_paths.work is None:
                    raise RemoteWorkspaceError(
                        "Executing task workspace is incomplete"
                    )
            except RemoteWorkspaceError:
                if requested_context is None:
                    self._contexts.pop(conversation, None)
                self._fail("INTERNAL_ERROR")
                raise AssertionError("unreachable")
            workspace_path = str(task_paths.work.resolve())
            system_context = (
                "本任务使用独立的可信设备协作工作目录。\n"
                f"当前工作目录：{workspace_path}\n"
                "仅在该目录中处理本任务的工作副本；不要把本设备路径当作对端路径。"
            )
        input_store: InboundTaskFileStore | None = None
        descriptors: tuple[TaskInputDescriptor, ...] = ()
        source_scopes: tuple[Mapping[str, Any], ...] = ()
        source_client: Any | None = None
        input_byte_budget = TaskInputByteBudget()
        if input_manifest is not None:
            if task_paths is None:
                if requested_context is None:
                    self._contexts.pop(conversation, None)
                self._fail("INTERNAL_ERROR")
            try:
                descriptors = tuple(
                    TaskInputDescriptor.from_wire(value)
                    for value in input_manifest["files"]
                )
                input_store = InboundTaskFileStore(task_paths, descriptors)
                source_scopes = tuple(input_manifest["sourceScopes"])
                input_byte_budget = TaskInputByteBudget(
                    sum(value.byte_length for value in descriptors)
                )
            except TaskFileError as error:
                if requested_context is None:
                    self._contexts.pop(conversation, None)
                self._fail(error.code)
                raise AssertionError("unreachable")
            if descriptors:
                input_context = _task_input_context(
                    task_paths.work,
                    descriptors,
                    heading=(
                        "本任务包含来自可信设备端的输入文件。"
                        "这些文件已由框架接收并完成长度及 SHA-256 校验；"
                        "可处理文件："
                    ),
                )
                system_context = (
                    f"{system_context}\n\n"
                    f"{input_context}"
                )
            if source_scopes and self._source_client_factory is None:
                if requested_context is None:
                    self._contexts.pop(conversation, None)
                self._fail("INTERNAL_ERROR")
            if source_scopes:
                assert self._source_client_factory is not None
                try:
                    source_client = self._source_client_factory(
                        peer_device_id=peer_device_id,
                        task_id=task_id,
                        workspace=task_paths,
                        source_scopes=source_scopes,
                        input_byte_budget=input_byte_budget,
                    )
                except Exception as error:
                    if requested_context is None:
                        self._contexts.pop(conversation, None)
                    self._fail("INTERNAL_ERROR")
                    raise AssertionError("unreachable") from error
                scope_lines = "\n".join(
                    f"- {value['name']}（scopeId: {value['scopeId']}）"
                    for value in source_scopes
                )
                system_context = (
                    f"{system_context}\n\n"
                    "本任务包含来自可信设备端的只读目录范围：\n"
                    f"{scope_lines}\n"
                    "目录内容不会自动复制。需要了解结构、搜索或取得明确文件时，"
                    "分别使用 dsoft_bus_source_list、dsoft_bus_source_search、"
                    "dsoft_bus_source_fetch。取得的文件会保存为本任务工作副本。"
                )
        if len(system_context.encode("utf-8")) > protocol.REMOTE_CONTEXT_UTF8_MAX:
            close = getattr(source_client, "close", None)
            if callable(close):
                close()
            if requested_context is None:
                self._contexts.pop(conversation, None)
            self._fail("TASK_INPUT_INVALID")
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
            input_byte_budget=input_byte_budget,
            workspace_path=workspace_path,
            system_context=system_context,
            workspace_paths=task_paths,
            input_store=input_store,
            input_descriptors=descriptors,
            source_scopes=source_scopes,
            source_client=source_client,
        )
        try:
            self._store.put_task("owned", peer_device_id, task)
        except TaskStoreError as error:
            close = getattr(source_client, "close", None)
            if callable(close):
                close()
            if self._workspace is not None:
                try:
                    self._workspace.clear_task(
                        "executing", peer_device_id, task_id
                    )
                except RemoteWorkspaceError:
                    pass
            if requested_context is None:
                self._contexts.pop(conversation, None)
            self._fail("INTERNAL_ERROR")
            raise AssertionError from error
        self._records[task_id] = record
        self._renew_record_lease(record)
        self._request_index[request_key] = (digest, task_id)
        self._peer_pending[peer_key] = self._peer_pending.get(peer_key, 0) + 1
        record.peer_pending_reserved = True
        self._queue_bytes += item_bytes
        self._dispatch_pending_count += 1
        record.dispatch_reserved = True
        if input_manifest is None:
            self._queue.put_nowait(record)
            record.enqueued = True
        self._remote_accepted += 1
        self.start()
        return task, self._new_subscription(record) if subscribe else None

    def _release_dispatch_reservation(self, record: _TaskRecord) -> None:
        if not record.dispatch_reserved:
            return
        self._queue_bytes -= record.item_bytes
        self._dispatch_pending_count -= 1
        record.dispatch_reserved = False

    def _task_input_record(
        self,
        peer_device_id: str,
        peer_runtime_instance_id: str,
        task_id: str,
    ) -> _TaskRecord:
        task = self.get_task(
            peer_device_id,
            peer_runtime_instance_id,
            task_id,
        )
        if task_state_is_terminal(task["status"]["state"]):
            self._fail("TASK_NOT_FOUND")
        record = self._records.get(str(task["id"]))
        if record is None or record.input_store is None:
            self._fail("TASK_INPUT_INVALID")
        return record

    async def begin_task_input(
        self,
        peer_device_id: str,
        peer_runtime_instance_id: str,
        task_id: str,
        input_id: str,
    ) -> Mapping[str, Any]:
        record = self._task_input_record(
            peer_device_id, peer_runtime_instance_id, task_id
        )
        assert record.input_store is not None
        try:
            async with record.input_lock:
                next_offset = await asyncio.to_thread(
                    record.input_store.begin, input_id
                )
        except TaskFileError as error:
            self._fail(error.code)
        return _freeze({"nextOffset": next_offset})

    async def append_task_input(
        self,
        peer_device_id: str,
        peer_runtime_instance_id: str,
        task_id: str,
        input_id: str,
        offset: int,
        raw: bytes,
    ) -> Mapping[str, Any]:
        record = self._task_input_record(
            peer_device_id, peer_runtime_instance_id, task_id
        )
        assert record.input_store is not None
        try:
            async with record.input_lock:
                next_offset = await asyncio.to_thread(
                    record.input_store.append,
                    input_id,
                    offset,
                    raw,
                )
        except TaskFileError as error:
            self._fail(error.code)
        return _freeze({"nextOffset": next_offset})

    async def commit_task_input(
        self,
        peer_device_id: str,
        peer_runtime_instance_id: str,
        task_id: str,
        input_id: str,
    ) -> Mapping[str, Any]:
        record = self._task_input_record(
            peer_device_id, peer_runtime_instance_id, task_id
        )
        assert record.input_store is not None
        try:
            async with record.input_lock:
                receipt = await asyncio.to_thread(
                    record.input_store.commit,
                    input_id,
                )
        except TaskFileError as error:
            self._fail(error.code)
        return _freeze({**_plain(receipt), "ready": record.input_store.ready})

    async def finish_task_inputs(
        self,
        peer_device_id: str,
        peer_runtime_instance_id: str,
        task_id: str,
    ) -> Mapping[str, Any]:
        """Release one Task to the Agent only after every declared input is ready."""

        task = self.get_task(
            peer_device_id, peer_runtime_instance_id, task_id
        )
        record = self._records.get(str(task["id"]))
        if record is None or record.input_store is None:
            self._fail("TASK_INPUT_INVALID")
        if record.enqueued:
            return _freeze({"ready": True})
        if task_state_is_terminal(task["status"]["state"]):
            self._fail("TRANSFER_CONFLICT")
        async with record.input_lock:
            if not record.input_store.ready:
                self._fail("TRANSFER_CONFLICT")
            if not record.enqueued:
                self._resume_record(record)
        return _freeze({"ready": True})

    async def abort_task_input(
        self,
        peer_device_id: str,
        peer_runtime_instance_id: str,
        task_id: str,
        input_id: str,
    ) -> Mapping[str, Any]:
        record = self._task_input_record(
            peer_device_id, peer_runtime_instance_id, task_id
        )
        assert record.input_store is not None
        try:
            async with record.input_lock:
                aborted = await asyncio.to_thread(
                    record.input_store.abort,
                    input_id,
                )
        except TaskFileError as error:
            self._fail(error.code)
        return _freeze({"aborted": aborted})

    def _publish(self, record: _TaskRecord, event: Mapping[str, Any]) -> None:
        for subscription in tuple(record.subscribers):
            if not subscription._publish(event):
                record.subscribers.discard(subscription)

    def _close_streams(self, record: _TaskRecord) -> None:
        for subscription in tuple(record.subscribers):
            subscription._close()
        record.subscribers.clear()

    @staticmethod
    def _close_source_client(record: _TaskRecord) -> None:
        close = getattr(record.source_client, "close", None)
        if callable(close):
            close()

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

    async def _mark_input_required(
        self,
        record: _TaskRecord,
        raw: Mapping[str, Any],
    ) -> None:
        """Persist a resumable Agent turn and close only its current stream."""

        try:
            request = normalize_input_request(raw.get("input_request"))
        except TaskInputRequestError as error:
            raise AgentMessageError("INVALID_AGENT_RESPONSE") from error
        normalized_history, history_bytes = self._validate_history(
            raw.get("messages")
        )
        await self._ensure_history_capacity(
            record.conversation_key,
            history_bytes,
            deadline=self._monotonic() + float(protocol.CONTROL_TIMEOUT_S),
        )
        self._commit_context_history(
            record.conversation_key,
            normalized_history,
            history_bytes,
        )
        record.history = normalized_history
        record.input_sequence += 1
        request_id = _new_uuid(self._uuid_factory, "inputRequestId")
        public_request = {
            "requestId": request_id,
            "sequence": record.input_sequence,
            "message": request["message"],
            "accepts": list(request["accepts"]),
        }
        status_message = self._status_message(
            record,
            f"需要补充信息：{request['message']}",
            metadata={"mclaw.inputRequest": public_request},
        )
        artifacts = list(record.task.get("artifacts", ()))
        prior_artifact_ids = {
            str(value["artifactId"]) for value in artifacts
        }
        additional = raw.get("artifacts")
        if additional is not None:
            if not isinstance(additional, (tuple, list)):
                raise AgentMessageError("INVALID_AGENT_RESPONSE")
            known = {str(value["artifactId"]) for value in artifacts}
            for value in additional:
                if not isinstance(value, Mapping):
                    raise AgentMessageError("INVALID_AGENT_RESPONSE")
                candidate = _plain(value)
                if "artifactId" not in candidate:
                    candidate["artifactId"] = _new_uuid(
                        self._uuid_factory,
                        "artifactId",
                    )
                try:
                    artifact = build_artifact_update(
                        task_id=record.task_id,
                        context_id=record.context_id,
                        artifact=candidate,
                    )["artifactUpdate"]["artifact"]
                except A2AError as error:
                    raise AgentMessageError(
                        "INVALID_AGENT_RESPONSE"
                    ) from error
                artifact_id = str(artifact["artifactId"])
                if artifact_id in known:
                    raise AgentMessageError("INVALID_AGENT_RESPONSE")
                known.add(artifact_id)
                artifacts.append(artifact)
        if len(artifacts) > protocol.TASK_ARTIFACT_MAX - 1:
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
            raise AgentMessageError("INVALID_AGENT_RESPONSE") from error
        history = [*record.task.get("history", ()), status_message]
        history = history[-protocol.TASK_HISTORY_MAX :]
        task = build_task(
            task_id=record.task_id,
            context_id=record.context_id,
            state="TASK_STATE_INPUT_REQUIRED",
            history=history,
            artifacts=artifacts,
            status_message=status_message,
            metadata=record.task.get("metadata", {}),
        )
        self._save_task(record, task)
        wait = {
            "sequence": record.input_sequence,
            "request": {
                "message": request["message"],
                "accepts": list(request["accepts"]),
            },
            "history": normalized_history,
            "systemContext": record.system_context,
            "sourceScopes": [_plain(value) for value in record.source_scopes],
            "acceptedMessageId": "",
            "acceptedDigest": "",
            "continuationText": "",
            "inputManifest": None,
            "inputBytesUsed": record.input_byte_budget.used_bytes,
        }
        try:
            self._store.put_input_wait(
                record.peer_device_id,
                record.task_id,
                request_id,
                wait,
            )
        except TaskStoreError as error:
            raise AgentMessageError("INTERNAL_ERROR") from error
        record.input_store = None
        self._release_peer_pending(record)
        for artifact in artifacts:
            if str(artifact["artifactId"]) not in prior_artifact_ids:
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
                state="TASK_STATE_INPUT_REQUIRED",
                message=status_message,
            ),
        )
        self._close_streams(record)

    async def _mark_terminal(
        self,
        record: _TaskRecord,
        *,
        state: str,
        failure_reason: str = "",
    ) -> None:
        self._close_source_client(record)
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
        self._forget_record_lease(record)
        try:
            self._store.delete_input_wait(
                record.peer_device_id,
                record.task_id,
            )
        except TaskStoreError as error:
            raise AgentMessageError("INTERNAL_ERROR") from error
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
        if raw.get("pending_task_input") is True:
            await self._mark_input_required(record, raw)
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
        artifacts: list[Mapping[str, Any]] = [
            *record.task.get("artifacts", ()),
            result_artifact,
        ]
        additional = raw.get("artifacts")
        if additional is not None:
            if (
                not isinstance(additional, (tuple, list))
                or len(additional)
                > protocol.TASK_ARTIFACT_MAX - len(artifacts)
            ):
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
        history = (
            list(record.task.get("history", ())) + [agent_message]
        )[-protocol.TASK_HISTORY_MAX :]
        task = build_task(
            task_id=record.task_id,
            context_id=record.context_id,
            state="TASK_STATE_COMPLETED",
            history=history,
            artifacts=artifacts,
            metadata=record.task.get("metadata", {}),
        )
        self._save_task(record, task)
        self._forget_record_lease(record)
        try:
            self._store.delete_input_wait(
                record.peer_device_id,
                record.task_id,
            )
        except TaskStoreError as error:
            raise AgentMessageError("INTERNAL_ERROR") from error
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
        self._close_source_client(record)
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
            workspace_path=record.workspace_path,
            system_context=record.system_context,
            attachments=_task_input_attachments(
                record.workspace_paths.work
                if record.workspace_paths is not None
                else None,
                record.input_descriptors,
            ),
            source_client=record.source_client,
            event_sink=lambda event: self._agent_event(record, event),
        )
        estimate = self._executor.estimate_budget(request)
        self._reserve_tokens(estimate)
        record.token_reservation = estimate
        if record.task["status"]["state"] != "TASK_STATE_WORKING":
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
            logger.exception(
                "Remote Task executor failed task_id=%s code=%s",
                record.task_id,
                reason,
            )
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
            self._release_dispatch_reservation(record)
            record.enqueued = False
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
                current = asyncio.current_task()
                if self._stopped or (
                    current is not None and current.cancelling()
                ):
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

    async def open_task_artifact(
        self,
        peer_device_id: str,
        peer_runtime_instance_id: str,
        task_id: str,
        artifact_id: str,
        transfer_id: str,
    ) -> Mapping[str, Any]:
        """Open one immutable produced Artifact transfer for its owning peer."""

        task = self.get_task(peer_device_id, peer_runtime_instance_id, task_id)
        if task["status"]["state"] not in {
            "TASK_STATE_COMPLETED",
            "TASK_STATE_INPUT_REQUIRED",
        }:
            self._fail("ARTIFACT_NOT_FOUND")
        try:
            record = self._store.get_artifact_transfer(
                "owned",
                peer_device_id,
                task_id,
                transfer_id,
                artifact_id=artifact_id,
            )
            from .task_artifact import _hash_regular_file

            length, sha256 = await asyncio.to_thread(
                _hash_regular_file,
                Path(str(record["localPath"])),
            )
        except TaskStoreError as error:
            self._fail(
                error.code
                if error.code in protocol.RPC_ERROR_CODES
                else "ARTIFACT_IO_ERROR"
            )
            raise AssertionError("unreachable")
        except Exception as error:
            code = str(getattr(error, "code", "ARTIFACT_IO_ERROR"))
            self._fail(
                code if code in protocol.RPC_ERROR_CODES else "ARTIFACT_IO_ERROR"
            )
            raise AssertionError("unreachable") from error
        if length != record["byteLength"] or sha256 != record["sha256"]:
            self._fail("ARTIFACT_CHANGED")
        return _freeze(
            {
                "transferId": transfer_id,
                "artifactId": artifact_id,
                "filename": record["filename"],
                "mediaType": record["contentMediaType"],
                "byteLength": length,
                "sha256": sha256,
            }
        )

    def read_task_artifact(
        self,
        peer_device_id: str,
        peer_runtime_instance_id: str,
        task_id: str,
        transfer_id: str,
        offset: int,
    ) -> Mapping[str, Any]:
        """Read one application-frame-sized chunk from a produced Artifact."""

        task = self.get_task(peer_device_id, peer_runtime_instance_id, task_id)
        if task["status"]["state"] not in {
            "TASK_STATE_COMPLETED",
            "TASK_STATE_INPUT_REQUIRED",
        }:
            self._fail("ARTIFACT_NOT_FOUND")
        try:
            record, raw = self._store.read_artifact_transfer(
                "owned",
                peer_device_id,
                task_id,
                transfer_id,
                offset,
            )
        except TaskStoreError as error:
            self._fail(
                error.code
                if error.code in protocol.RPC_ERROR_CODES
                else "ARTIFACT_IO_ERROR"
            )
            raise AssertionError("unreachable")
        next_offset = offset + len(raw)
        return _freeze(
            {
                "transferId": transfer_id,
                "offset": offset,
                "nextOffset": next_offset,
                "data": base64.b64encode(raw).decode("ascii"),
                "eof": next_offset == record["byteLength"],
            }
        )

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
        if record is None and task["status"]["state"] == "TASK_STATE_INPUT_REQUIRED":
            try:
                wait = self._store.get_input_wait(peer_device_id, task_id)
            except TaskStoreError:
                self._fail("INTERNAL_ERROR")
                raise AssertionError("unreachable")
            if wait is not None:
                record = await self._rehydrate_waiting_record(
                    peer_device_id=peer_device_id,
                    peer_runtime_instance_id=peer_runtime_instance_id,
                    task=task,
                    wait=wait,
                )
        if record is None:
            self._fail("TASK_NOT_CANCELABLE")
        await self._cancel_record(
            record,
            failure_reason="AGENT_INTERRUPTED",
            deadline=self._monotonic() + float(protocol.CONTROL_TIMEOUT_S),
        )
        return record.task

    async def acknowledge_task_result(
        self,
        peer_device_id: str,
        peer_runtime_instance_id: str,
        task_id: str,
    ) -> Mapping[str, Any]:
        """Release one terminal Task's execution workspace after peer receipt."""

        peer_device_id, _peer_runtime_instance_id = self._validate_peer(
            peer_device_id, peer_runtime_instance_id
        )
        task_id = _uuid4(task_id, "taskId")
        # A terminal acknowledgement is deletion-only and may be replayed by
        # the same authenticated device after its M-Claw Runtime restarts.
        # GetTask, Artifact reads and execution remain runtime-generation bound.
        task = self._store.get_task("owned", peer_device_id, task_id)
        if task is None:
            self._fail("TASK_NOT_FOUND")
        if not task_state_is_terminal(task["status"]["state"]):
            self._fail("TASK_NOT_CANCELABLE")
        record = self._records.get(task_id)
        if record is not None:
            if record.result_acknowledged:
                return record.task
            deadline = self._monotonic() + float(protocol.CONTROL_TIMEOUT_S)
            if not await self._executor.forget_session(record.session_id, deadline):
                self._fail("INTERNAL_ERROR")
        if self._workspace is not None:
            try:
                self._workspace.clear_task(
                    "executing",
                    peer_device_id,
                    task_id,
                )
                self._workspace.clear_artifacts(
                    "produced",
                    peer_device_id,
                    task_id,
                )
            except RemoteWorkspaceError:
                self._fail("INTERNAL_ERROR")
        self._store.forget_artifact_transfers(
            "owned", peer_device_id, task_id
        )
        if record is not None:
            record.result_acknowledged = True
        return task

    async def _cancel_record(
        self,
        record: _TaskRecord,
        *,
        failure_reason: str,
        deadline: float,
    ) -> bool:
        """Cancel execution and publish a terminal state only after OS cleanup."""
        async with record.cancel_lock:
            if task_state_is_terminal(record.task["status"]["state"]):
                return record.task["status"]["state"] == "TASK_STATE_CANCELED"
            record.cancel_requested = True
            self._release_dispatch_reservation(record)
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
                failure_reason=(
                    failure_reason if cleanup_confirmed else "INTERNAL_ERROR"
                ),
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
        if task_state_closes_stream(task["status"]["state"]):
            return self._task_subscription(task)
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
        peer_device_id: str,
        peer_runtime_instance_id: str,
        _generation: int,
    ) -> None:
        # A SoftBus stream is a subscription, not Task ownership.  The Task
        # remains active so the caller can GetTask + SubscribeToTask after a
        # generation reconnects.
        try:
            peer = self._validate_peer(
                peer_device_id, peer_runtime_instance_id
            )
        except AgentMessageError:
            return
        if any(key[:2] == peer for key in self._owner_leases):
            self._suspect_peer_runtimes.add(peer)

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
        for key in tuple(self._owner_leases):
            await self._terminalize_owner_lease(
                key,
                failure_reason="RUNTIME_STOPPING",
            )
        worker = self._worker
        if worker is not None and not worker.done():
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        self._worker = None
        lease_worker = self._lease_worker
        if lease_worker is not None and not lease_worker.done():
            lease_worker.cancel()
            await asyncio.gather(lease_worker, return_exceptions=True)
        self._lease_worker = None
        while True:
            try:
                queued = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._release_dispatch_reservation(queued)
            self._queue.task_done()
        if (
            self._queue_bytes != 0
            or self._dispatch_pending_count != 0
            or self._peer_pending
        ):
            raise AssertionError("task shutdown reservations were not released")
        self._records.clear()
        self._request_index.clear()
        self._contexts.clear()
        self._stale_peer_runtimes.clear()
        self._suspect_peer_runtimes.clear()
        self._current_peer_runtime.clear()
        self._owner_leases.clear()
        self._lease_replays.clear()
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
        budget_used = used + self._token_reserved
        if self._token_budget is not None:
            budget_used = min(self._token_budget, budget_used)
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
                "dispatchPendingCount": self._dispatch_pending_count,
                "idempotencyWaiterBytes": 0,
                "idempotencyWaiterCount": 0,
                "inflightMessageCount": sum(
                    1
                    for record in self._records.values()
                    if not task_state_is_terminal(record.task["status"]["state"])
                ),
                "lateProviderResultCount": 0,
                "ownerLeaseCount": len(self._owner_leases),
                "ownerLeaseExpired": self._lease_expired,
                "ownerLeaseRenewed": self._lease_renewed,
                "ownerRuntimeReplaced": self._runtime_replaced,
                "ownerSuspectRuntimeCount": len(self._suspect_peer_runtimes),
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
