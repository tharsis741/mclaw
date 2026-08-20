# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Owner-loop resources for identity-bound SoftBus discovery.

The isolated Worker may complete ``hello`` before this adapter has a public
identity, but no Native callback, snapshot, listener, or connection operation
is admitted until all local publications have been frozen successfully.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import math
import random
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from . import protocol
from .a2a import (
    A2AError,
    CoreMethodCall,
    RequestEnvelope,
    task_state_is_terminal,
    validate_core_method,
    validate_stream_response,
    validate_task,
)
from .agent_message import (
    AgentMessageError,
    LazyAgentRunnerTurnExecutor,
    RemoteTurnExecutor,
)
from .agent_task import DsoftbusTaskDispatcher, TaskSubscription
from .task_store import TaskStoreError
from .binding import LocalBindingIdentity, derive_public_agent_id
from .device_context import (
    DeviceContextError,
    LocalDeviceStateService,
    RemoteDeviceContextStore,
)
from .presence import DiscoveredNode, InMemoryPresenceAdapter, PresenceError
from .pairing_ownership import PairingOwnershipError, PairingOwnershipStore
from .provider_readiness import resolve_provider_readiness
from .publication import (
    LocalPublications,
    PublicationError,
    freeze_local_publications,
)
from .softbus_binding import (
    ApplicationResponse,
    OutboundFrame,
    SoftBusA2ABinding,
    SoftBusBindingError,
    SoftBusSendScheduler,
)
from .worker_supervisor import WorkerSupervisor, WorkerSupervisorError

if TYPE_CHECKING:
    from .manifest import LocalManifestTemplate


_SNAPSHOT_RECONCILE_TIMEOUT_S = float(protocol.NODE_SNAPSHOT_TTL_S)
_HEX64 = frozenset("0123456789abcdef")


def _is_hex64(value: str) -> bool:
    return len(value) == 64 and all(character in _HEX64 for character in value)


def _default_provider_readiness(context: Any | None) -> tuple[bool, str]:
    return resolve_provider_readiness(context)


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return copy.deepcopy(value)


def _freeze_public(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_public(item) for key, item in value.items()}
        )
    if isinstance(value, (tuple, list)):
        return tuple(_freeze_public(item) for item in value)
    return copy.deepcopy(value)


@dataclass(frozen=True, slots=True)
class _Connection:
    device_id: str
    generation: int
    mtu: int
    network_id: str
    socket: int
    binding: SoftBusA2ABinding


@dataclass(frozen=True, slots=True)
class _ReconnectAttempt:
    failures: int
    next_attempt: float


@dataclass(frozen=True, slots=True)
class _ApplicationWaiter:
    future: asyncio.Future[ApplicationResponse]
    method: str


@dataclass(frozen=True, slots=True)
class _ApplicationStreamWaiter:
    queue: asyncio.Queue[ApplicationResponse | BaseException]
    method: str
    request_id: str
    socket: int


class DiscoveryOwnerResources:
    """Own the verified Worker, publications, discovery, and listener epoch."""

    def __init__(
        self,
        *,
        supervisor: WorkerSupervisor,
        manifest_template: LocalManifestTemplate | None = None,
        initial_nodes: Sequence[DiscoveredNode | Mapping[str, Any]] = (),
        provider_ready: bool = False,
        provider_readiness_code: str = "PROVIDER_MISSING",
        provider_runtime: Any | None = None,
        message_config: Mapping[str, Any] | None = None,
        agent_config: Mapping[str, Any] | None = None,
        agent_workspace_root: str | Path | None = None,
        message_executor: RemoteTurnExecutor | None = None,
        provider_readiness: Callable[[Any | None], tuple[bool, str]] = (
            _default_provider_readiness
        ),
        monotonic: Callable[[], float] = time.monotonic,
        reconnect_random: Callable[[], float] = random.random,
        pairing_state_path: str | Path | None = None,
        task_state_root: str | Path | None = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
        discovery_window_s: float = protocol.DEVICE_DISCOVERY_WINDOW_S,
        bind_timeout_s: float = protocol.DEVICE_BIND_TIMEOUT_S,
        bind_poll_interval_s: float = protocol.DEVICE_BIND_POLL_INTERVAL_S,
    ) -> None:
        if manifest_template is not None and initial_nodes:
            raise ValueError("product discovery cannot inject initial_nodes")
        if (
            type(provider_ready) is not bool
            or not isinstance(provider_readiness_code, str)
            or provider_ready != (provider_readiness_code == "")
            or provider_readiness_code
            not in {
                "",
                "PROVIDER_MISSING",
                "TRANSPORT_FENCE_UNSUPPORTED",
                "PROVIDER_SYNC_FAILED",
            }
        ):
            raise ValueError("provider readiness pair is invalid")
        self._supervisor = supervisor
        self._manifest_template = manifest_template
        self._initial_nodes = copy.deepcopy(tuple(initial_nodes))
        self._provider_ready = provider_ready
        self._provider_readiness_code = provider_readiness_code
        self._provider_runtime = provider_runtime
        if not callable(provider_readiness):
            raise TypeError("provider_readiness must be callable")
        self._provider_readiness = provider_readiness
        self._monotonic = monotonic
        if not callable(reconnect_random):
            raise TypeError("reconnect_random must be callable")
        self._reconnect_random = reconnect_random
        if not callable(sleep):
            raise TypeError("sleep must be callable")
        durations = (discovery_window_s, bind_timeout_s, bind_poll_interval_s)
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or value < 0
            for value in durations
        ) or bind_timeout_s <= 0 or bind_poll_interval_s <= 0:
            raise ValueError("device management durations are invalid")
        self._sleep = sleep
        self._discovery_window_s = float(discovery_window_s)
        self._bind_timeout_s = float(bind_timeout_s)
        self._bind_poll_interval_s = float(bind_poll_interval_s)
        self._pairing_store = (
            None
            if pairing_state_path is None
            else PairingOwnershipStore(pairing_state_path)
        )
        self._task_dispatcher: DsoftbusTaskDispatcher | None = None
        if message_config is not None:
            if not isinstance(message_config, Mapping):
                raise TypeError("message_config must be a mapping")
            executor = message_executor
            if executor is None:
                if not isinstance(agent_config, Mapping) or agent_workspace_root is None:
                    raise ValueError(
                        "agent_config and agent_workspace_root are required"
                    )
                executor = LazyAgentRunnerTurnExecutor(
                    provider_runtime=provider_runtime,
                    config=agent_config,
                    workspace_root=agent_workspace_root,
                )
            self._task_dispatcher = DsoftbusTaskDispatcher(
                config=message_config,
                executor=executor,
                provider_runtime=provider_runtime,
                provider_ready=provider_ready,
                state_root=task_state_root,
                monotonic=monotonic,
            )
        self._presence: InMemoryPresenceAdapter | None = None
        self._publications: LocalPublications | None = None
        self._current_card: Any | None = None
        self._listener_socket: int | None = None
        self._connections_by_socket: dict[int, _Connection] = {}
        self._socket_by_device: dict[str, int] = {}
        self._connection_generation_by_device: dict[str, int] = {}
        self._connection_state_by_device: dict[str, str] = {}
        self._reconnect_attempt_by_device: dict[str, _ReconnectAttempt] = {}
        self._connection_admission_open = True
        self._send_scheduler = SoftBusSendScheduler()
        self._device_contexts = RemoteDeviceContextStore(monotonic=monotonic)
        self._state_service: LocalDeviceStateService | None = None
        self._context_generation_started: set[tuple[int, int]] = set()
        self._context_fetch_tasks: dict[str, asyncio.Task[None]] = {}
        self._inbound_application_tasks: dict[
            tuple[int, str], asyncio.Task[None]
        ] = {}
        self._inbound_message_fingerprints: dict[tuple[int, str], str] = {}
        self._application_waiters: dict[
            tuple[int, str], _ApplicationWaiter | _ApplicationStreamWaiter
        ] = {}
        self._outbound_task_ids: dict[str, set[str]] = {}
        self._outbound_cancel_attempts: dict[
            tuple[str, str], asyncio.Task[bool]
        ] = {}
        self._event_task: asyncio.Task[None] | None = None
        self._event_failure_code = ""
        self._owner_thread_id: int | None = None
        self._started = False
        self._stopped = False
        self._cache_lock = threading.Lock()
        self._health_change_callback: Callable[[Mapping[str, Any]], None] | None = None
        self._last_notified_health: dict[str, Any] | None = None
        self._last_notified_lifecycle: tuple[str, tuple[str, ...]] | None = None
        self._publication_counts = {
            "agentCardGenerationCount": 0,
            "listenerGenerationCount": 0,
            "manifestDescriptorGenerationCount": 0,
            "publicManifestGenerationCount": 0,
        }
        self._cached_diagnostic: Mapping[str, Any] = MappingProxyType({})
        self._cached_health_updates: Mapping[str, Any] = MappingProxyType({})
        self._cached_public_peers: tuple[Mapping[str, Any], ...] = ()
        self._cached_device_contexts: dict[str, Mapping[str, Any]] = {}

    def _require_owner(self, *, establish: bool = False) -> None:
        current = threading.get_ident()
        if self._owner_thread_id is None and establish:
            self._owner_thread_id = current
        if self._owner_thread_id != current:
            raise RuntimeError("DISCOVERY_RESOURCE_OWNER_MISMATCH")

    def _public_peer_rows(self) -> tuple[Mapping[str, Any], ...]:
        presence = self._presence
        if presence is None:
            return ()
        rows: list[Mapping[str, Any]] = []
        for value in presence.public_peers():
            row = dict(value)
            device_id = str(row["deviceId"])
            row.update(dict(self._device_contexts.public_summary(device_id)))
            socket = self._socket_by_device.get(device_id)
            if socket is not None and socket in self._connections_by_socket:
                connection = self._connections_by_socket[socket]
                binding = connection.binding.public_snapshot()
                peer_card = connection.binding.peer_card
                row["connectionState"] = "OPEN"
                row["agentAvailability"] = connection.binding.phase
                row["agentId"] = derive_public_agent_id(device_id)
                row["connectionGeneration"] = connection.generation
                row["peerRuntimeInstanceId"] = binding["peerRuntimeInstanceId"]
                row["agentCardSha256"] = (
                    None
                    if peer_card is None
                    else hashlib.sha256(peer_card.canonical_bytes).hexdigest()
                )
                if peer_card is not None:
                    row["agentCardAvailable"] = True
                identity = connection.binding.peer_identity
                if identity is not None:
                    row["_mclawProvenance"] = MappingProxyType(
                        {
                            "kind": "peer",
                            "source": "mclaw.dsoftbus.runtime",
                            "peerDeviceId": connection.device_id,
                            "peerRuntimeInstanceId": identity.runtime_instance_id,
                            "connectionGeneration": connection.generation,
                            "receivedVia": "softbus",
                            "verifiedBinding": peer_card is not None,
                        }
                    )
            elif device_id in self._connection_state_by_device:
                row["connectionState"] = self._connection_state_by_device[device_id]
            rows.append(MappingProxyType(row))
        return tuple(rows)

    def _health_updates(self) -> Mapping[str, Any]:
        values = dict(self._supervisor.health_updates())
        if self._presence is not None:
            values.update(self._presence.health_updates())
        else:
            values.update(
                {
                    "connectedPeerCount": 0,
                    "peerCount": 0,
                    "peerRegistryDropped": 0,
                    "readyPeerCount": 0,
                "stateFreshPeerCount": 0,
                }
            )
        retained = len(self._connections_by_socket)
        ready = sum(
            1
            for connection in self._connections_by_socket.values()
            if connection.binding.ready
        )
        listener_count = int(self._listener_socket is not None)
        send_diagnostic = self._send_scheduler.diagnostic_snapshot()
        message_diagnostic = (
            self._task_dispatcher.diagnostic_snapshot()
            if self._task_dispatcher is not None
            else {}
        )
        values.update(
            {
                "agentIngressReservationCount": message_diagnostic.get(
                    "agentIngressReservationCount", 0
                ),
                "agentPendingCount": message_diagnostic.get(
                    "agentPendingCount", 0
                ),
                "agentSessionTaskCount": message_diagnostic.get(
                    "agentSessionTaskCount", 0
                ),
                "connectedPeerCount": retained,
                "dispatchQueueBytes": message_diagnostic.get(
                    "dispatchQueueBytes", 0
                ),
                "dispatchQueueCount": message_diagnostic.get(
                    "dispatchQueueCount", 0
                ),
                "idempotencyWaiterBytes": message_diagnostic.get(
                    "idempotencyWaiterBytes", 0
                ),
                "idempotencyWaiterCount": message_diagnostic.get(
                    "idempotencyWaiterCount", 0
                ),
                "listenerReady": self._listener_socket is not None,
                "listenerSocketCount": listener_count,
                "openSocketCount": listener_count + retained,
                "productIntegrationVerified": (
                    self._publications is not None and self._listener_socket is not None
                ),
                "readyPeerCount": ready,
                "remoteAccepted": message_diagnostic.get("remoteAccepted", 0),
                "remoteBudgetUsed": message_diagnostic.get(
                    "remoteBudgetUsed", 0
                ),
                "remoteContextBytes": message_diagnostic.get("contextBytes", 0),
                "remoteContextCount": message_diagnostic.get("contextCount", 0),
                "remoteRateLimited": message_diagnostic.get(
                    "remoteRateLimited", 0
                ),
                "remoteRejected": message_diagnostic.get("remoteRejected", 0),
                "remoteRejectedByCode": dict(
                    message_diagnostic.get("remoteRejectedByCode", {})
                ),
                "responseCacheBytes": message_diagnostic.get(
                    "responseCacheBytes", 0
                ),
                "responseCacheCount": message_diagnostic.get(
                    "responseCacheCount", 0
                ),
                "stateFreshPeerCount": self._device_contexts.state_fresh_count(),
                "retainedPeerSocketCount": retained,
                **dict(send_diagnostic),
                "transientSocketCount": 0,
            }
        )
        return MappingProxyType(values)

    def _cache_diagnostic(
        self,
        *,
        lifecycle_state: str | None = None,
        degraded_reasons: Sequence[str] = (),
    ) -> None:
        if lifecycle_state is not None and lifecycle_state not in {
            "READY",
            "DEGRADED",
        }:
            raise ValueError("invalid lifecycle state")
        normalized_reasons = tuple(degraded_reasons)
        if lifecycle_state == "READY" and normalized_reasons:
            raise ValueError("READY cannot carry degraded reasons")
        if lifecycle_state == "DEGRADED" and not normalized_reasons:
            raise ValueError("DEGRADED requires a reason")
        if lifecycle_state is None and normalized_reasons:
            raise ValueError("reasons require an explicit lifecycle state")
        supervisor = self._supervisor.diagnostic_snapshot()
        peers = self._public_peer_rows()
        health = MappingProxyType(dict(self._health_updates()))
        message_diagnostic = (
            self._task_dispatcher.diagnostic_snapshot()
            if self._task_dispatcher is not None
            else {}
        )
        diagnostic = MappingProxyType(
            {
                "activeThreadCount": supervisor["activeThreadCount"],
                "agentSessionTaskCount": message_diagnostic.get(
                    "agentSessionTaskCount", 0
                ),
                "dispatchExecutionCount": message_diagnostic.get(
                    "dispatchExecutionCount", 0
                ),
                "dispatchQueueBytes": message_diagnostic.get(
                    "dispatchQueueBytes", 0
                ),
                "dispatchQueueCount": message_diagnostic.get(
                    "dispatchQueueCount", 0
                ),
                "eventFailureCode": self._event_failure_code,
                "inflightMessageCount": message_diagnostic.get(
                    "inflightMessageCount", 0
                ),
                "localTurnCount": message_diagnostic.get("localTurnCount", 0),
                "operationCounts": MappingProxyType(
                    dict(supervisor["operationCounts"])
                ),
                "peerCount": len(peers),
                "remoteAccepted": message_diagnostic.get("remoteAccepted", 0),
                "remoteBudgetUsed": message_diagnostic.get(
                    "remoteBudgetUsed", 0
                ),
                "remoteRejectedByCode": MappingProxyType(
                    dict(message_diagnostic.get("remoteRejectedByCode", {}))
                ),
                "responseCacheCount": message_diagnostic.get(
                    "responseCacheCount", 0
                ),
                "tokenReserved": message_diagnostic.get("tokenReserved", 0),
                "workerAlive": supervisor["workerAlive"],
            }
        )
        contexts: dict[str, Mapping[str, Any]] = {}
        for peer in peers:
            device_id = str(peer["deviceId"])
            try:
                contexts[device_id] = self._device_contexts.snapshot(device_id)
            except DeviceContextError:
                continue
        with self._cache_lock:
            self._cached_diagnostic = diagnostic
            self._cached_health_updates = health
            self._cached_public_peers = peers
            self._cached_device_contexts = contexts
            callback = self._health_change_callback
            plain_health = dict(health)
            lifecycle = (
                None
                if lifecycle_state is None
                else (lifecycle_state, normalized_reasons)
            )
            changed = (
                plain_health != self._last_notified_health
                or (
                    lifecycle is not None
                    and lifecycle != self._last_notified_lifecycle
                )
            )
            if callback is not None and changed:
                self._last_notified_health = plain_health
                if lifecycle is not None:
                    self._last_notified_lifecycle = lifecycle
        if callback is not None and changed:
            if lifecycle_state is None:
                callback(health)
            else:
                callback(
                    MappingProxyType(
                        {
                            **dict(health),
                            "_degradedReasons": normalized_reasons,
                            "_lifecycleState": lifecycle_state,
                        }
                    )
                )

    def set_health_change_callback(
        self,
        callback: Callable[[Mapping[str, Any]], None] | None,
    ) -> None:
        """Attach the owner publisher after the initial lifecycle publication."""

        self._require_owner()
        if callback is not None and not callable(callback):
            raise TypeError("health callback must be callable or None")
        with self._cache_lock:
            self._health_change_callback = callback
            if callback is None:
                return
            health = self._cached_health_updates
            plain_health = dict(health)
            changed = plain_health != self._last_notified_health
            if changed:
                self._last_notified_health = plain_health
        if changed:
            callback(health)

    @staticmethod
    def _degraded_reason(error: BaseException, *, startup_step: str) -> str:
        code = str(getattr(error, "code", ""))
        if code == "NODE_SNAPSHOT_OVERFLOW":
            return "NODE_SNAPSHOT_OVERFLOW"
        if startup_step == "listener":
            return "LISTENER_START_FAILED"
        if code in {
            "PARENT_EVENT_CAPACITY_FATAL",
            "PRESENCE_EVENT_SEQUENCE_INVALID",
            "SNAPSHOT_RECONCILE_FAILED",
            "WORKER_PROTOCOL_ERROR",
        }:
            return "WORKER_PROTOCOL_ERROR"
        if code == "WORKER_RESTART_EXHAUSTED":
            return "WORKER_RESTART_EXHAUSTED"
        return "WORKER_START_FAILED"

    def _drain_reconcile_events(
        self, target: list[Mapping[str, Any]]
    ) -> None:
        while True:
            event = self._supervisor.pop_event()
            if event is None:
                return
            if event["event"] not in {"node-online", "node-offline"}:
                raise WorkerSupervisorError("WORKER_PROTOCOL_ERROR")
            target.append(event)

    def _collect_snapshot(self) -> tuple[DiscoveredNode, ...]:
        deadline = self._monotonic() + _SNAPSHOT_RECONCILE_TIMEOUT_S
        raw_nodes: list[Mapping[str, Any]] = []
        buffered_events: list[Mapping[str, Any]] = []
        page_bytes = 0
        snapshot_id = ""
        cursor = ""
        cursors: set[str] = set()
        replay_after: int | None = None
        replay_through: int | None = None
        while True:
            if self._monotonic() >= deadline:
                raise WorkerSupervisorError("SNAPSHOT_RECONCILE_FAILED")
            page = self._supervisor.snapshot_nodes_page(
                snapshot_id=snapshot_id, cursor=cursor
            )
            self._drain_reconcile_events(buffered_events)
            current_snapshot_id = str(page["snapshotId"])
            current_after = int(page["replayAfterSeq"])
            current_through = int(page["replayThroughSeq"])
            if not snapshot_id:
                snapshot_id = current_snapshot_id
                replay_after = current_after
                replay_through = current_through
            elif (
                current_snapshot_id != snapshot_id
                or current_after != replay_after
                or current_through != replay_through
            ):
                raise WorkerSupervisorError("SNAPSHOT_RECONCILE_FAILED")
            page_bytes += len(protocol.canonical_json_bytes(_plain(page)))
            if page_bytes > protocol.NODE_SNAPSHOT_BYTES_MAX:
                raise WorkerSupervisorError("NODE_SNAPSHOT_OVERFLOW")
            for node in page["nodes"]:
                if len(raw_nodes) >= protocol.NODE_SNAPSHOT_MAX:
                    raise WorkerSupervisorError("NODE_SNAPSHOT_OVERFLOW")
                raw_nodes.append(MappingProxyType(dict(node)))
            next_cursor = str(page["nextCursor"])
            if not next_cursor:
                break
            if next_cursor in cursors:
                raise WorkerSupervisorError("SNAPSHOT_RECONCILE_FAILED")
            cursors.add(next_cursor)
            cursor = next_cursor

        assert replay_after is not None and replay_through is not None
        while not all(
            sequence in {
                int(event["data"]["nodeEventSeq"])
                for event in buffered_events
            }
            for sequence in range(replay_after + 1, replay_through + 1)
        ):
            event = self._supervisor.wait_event(deadline)
            if event is None or event["event"] not in {"node-online", "node-offline"}:
                raise WorkerSupervisorError("SNAPSHOT_RECONCILE_FAILED")
            buffered_events.append(event)

        discovered: list[DiscoveredNode] = []
        for node in raw_nodes:
            try:
                udid = self._supervisor.get_node_udid(str(node["networkId"]))
            except WorkerSupervisorError:
                if not self._supervisor.health_updates()["workerAlive"]:
                    raise
                continue
            discovered.append(
                DiscoveredNode(
                    network_id=str(node["networkId"]),
                    udid=udid,
                    device_name=str(node["deviceName"]),
                    device_type_id=int(node["deviceTypeId"]),
                )
            )
            self._drain_reconcile_events(buffered_events)

        ordered_events = sorted(
            (
                event
                for event in buffered_events
                if int(event["data"]["nodeEventSeq"]) > replay_after
            ),
            key=lambda event: int(event["data"]["nodeEventSeq"]),
        )
        sequences = [int(event["data"]["nodeEventSeq"]) for event in ordered_events]
        if sequences != list(range(replay_after + 1, replay_after + 1 + len(sequences))):
            raise WorkerSupervisorError("SNAPSHOT_RECONCILE_FAILED")

        assert self._presence is not None
        self._presence.apply_snapshot_barrier(
            discovered, replay_after_seq=replay_after
        )
        for event in ordered_events:
            data = event["data"]
            sequence = int(data["nodeEventSeq"])
            if event["event"] == "node-offline":
                self._presence.node_offline(
                    str(data["networkId"]), node_event_seq=sequence
                )
                continue
            try:
                udid = self._supervisor.get_node_udid(str(data["networkId"]))
            except WorkerSupervisorError:
                if not self._supervisor.health_updates()["workerAlive"]:
                    raise
                self._presence.discard_node_event(node_event_seq=sequence)
                continue
            self._presence.node_online(
                DiscoveredNode(
                    network_id=str(data["networkId"]),
                    udid=udid,
                    device_name=str(data["deviceName"]),
                    device_type_id=int(data["deviceTypeId"]),
                ),
                node_event_seq=sequence,
            )
        return tuple(discovered)

    def _register_connection(
        self,
        *,
        device_id: str,
        initiator: bool,
        network_id: str,
        socket: int,
        mtu: int,
    ) -> None:
        if not self._connection_admission_open or self._stopped:
            try:
                self._supervisor.close_socket(socket)
            except WorkerSupervisorError:
                pass
            return
        if mtu < protocol.MIN_NEGOTIATED_FRAME:
            self._supervisor.close_socket(socket)
            return
        publications = self._publications
        current_card = self._current_card
        if publications is None or current_card is None:
            self._supervisor.close_socket(socket)
            return
        existing_socket = self._socket_by_device.get(device_id)
        if existing_socket is not None:
            existing = self._connections_by_socket.get(existing_socket)
            if (
                existing is not None
                and existing.socket == socket
                and existing.network_id == network_id
                and existing.mtu == mtu
            ):
                return
            self._supervisor.close_socket(socket)
            return
        generation = self._connection_generation_by_device.get(device_id, 0) + 1
        if generation > 2**63 - 1:
            self._supervisor.close_socket(socket)
            return
        try:
            local = LocalBindingIdentity.create(
                device_id=publications.device_id,
                agent_id=publications.agent_id,
                runtime_instance_id=publications.runtime_instance_id,
                manifest=publications.manifest.descriptor,
            )
            binding = SoftBusA2ABinding(
                local=local,
                local_card=current_card,
                authenticated_peer_device_id=device_id,
                authenticated_peer_agent_id=derive_public_agent_id(device_id),
                initiator=initiator,
                connection_generation=generation,
                negotiated_mtu=mtu,
            )
            self._send_scheduler.register(socket, generation)
            connection = _Connection(
                device_id,
                generation,
                mtu,
                network_id,
                socket,
                binding,
            )
            self._connections_by_socket[socket] = connection
            self._socket_by_device[device_id] = socket
            self._connection_generation_by_device[device_id] = generation
            for frame in binding.start():
                self._send_scheduler.enqueue(socket, generation, frame)
            self._drain_send_scheduler()
            if self._connections_by_socket.get(socket) is connection:
                self._connection_state_by_device.pop(device_id, None)
                self._reconnect_attempt_by_device.pop(device_id, None)
        except (SoftBusBindingError, ValueError):
            self._send_scheduler.unregister(socket, generation)
            self._connections_by_socket.pop(socket, None)
            if self._socket_by_device.get(device_id) == socket:
                del self._socket_by_device[device_id]
            try:
                self._supervisor.close_socket(socket)
            except WorkerSupervisorError:
                pass
            self._connection_state_by_device[device_id] = "RECONNECTING"
            candidate = self._candidate(device_id)
            if candidate is not None and candidate.action == "INITIATE":
                self._schedule_connect_retry(device_id)

    def _reconnect_delay(self, failures: int) -> float:
        if type(failures) is not int or failures < 1:
            raise WorkerSupervisorError("WORKER_PROTOCOL_ERROR")
        try:
            random_value = float(self._reconnect_random())
        except (TypeError, ValueError, OverflowError) as error:
            raise WorkerSupervisorError("WORKER_PROTOCOL_ERROR") from error
        if not 0.0 <= random_value <= 1.0:
            raise WorkerSupervisorError("WORKER_PROTOCOL_ERROR")
        exponent = min(failures - 1, 62)
        nominal = min(
            float(protocol.RECONNECT_MAX_S),
            float(protocol.RECONNECT_BASE_S) * (2**exponent),
        )
        return min(
            float(protocol.RECONNECT_MAX_S),
            nominal * (0.8 + 0.4 * random_value),
        )

    def _candidate(self, device_id: str) -> Any | None:
        presence = self._presence
        if presence is None:
            return None
        return next(
            (
                candidate
                for candidate in presence.connection_candidates()
                if candidate.device_id == device_id
            ),
            None,
        )

    def _schedule_connect(
        self,
        device_id: str,
        *,
        immediate: bool,
        reconnecting: bool,
    ) -> None:
        if not self._connection_admission_open or self._stopped:
            return
        candidate = self._candidate(device_id)
        if candidate is None or not candidate.admitted:
            self._reconnect_attempt_by_device.pop(device_id, None)
            self._connection_state_by_device.pop(device_id, None)
            return
        self._connection_state_by_device[device_id] = (
            "RECONNECTING" if reconnecting else "CONNECTING"
        )
        if candidate.action != "INITIATE" or device_id in self._socket_by_device:
            self._reconnect_attempt_by_device.pop(device_id, None)
            return
        current = self._reconnect_attempt_by_device.get(device_id)
        failures = 0 if current is None else current.failures
        self._reconnect_attempt_by_device[device_id] = _ReconnectAttempt(
            failures=failures,
            next_attempt=self._monotonic() if immediate else (
                self._monotonic() + self._reconnect_delay(max(1, failures))
            ),
        )

    def _schedule_connect_retry(self, device_id: str) -> None:
        current = self._reconnect_attempt_by_device.get(device_id)
        failures = 1 if current is None else current.failures + 1
        self._reconnect_attempt_by_device[device_id] = _ReconnectAttempt(
            failures=failures,
            next_attempt=self._monotonic() + self._reconnect_delay(failures),
        )

    def _initialize_connection_candidates(self) -> None:
        presence = self._presence
        if presence is None:
            return
        candidates = presence.connection_candidates()
        current_devices = {candidate.device_id for candidate in candidates}
        for device_id in tuple(self._connection_state_by_device):
            if device_id not in current_devices:
                self._connection_state_by_device.pop(device_id, None)
                self._reconnect_attempt_by_device.pop(device_id, None)
        for candidate in candidates:
            if not candidate.admitted:
                continue
            self._schedule_connect(
                candidate.device_id,
                immediate=True,
                reconnecting=(
                    candidate.device_id in self._connection_generation_by_device
                ),
            )
        self._run_due_connect_attempts()

    def _run_due_connect_attempts(self) -> None:
        if not self._connection_admission_open or self._stopped:
            return
        now = self._monotonic()
        due = tuple(
            device_id
            for device_id, attempt in sorted(
                self._reconnect_attempt_by_device.items()
            )
            if attempt.next_attempt <= now
        )
        for device_id in due:
            self._connect_candidate(device_id)

    def _connect_candidate(self, device_id: str) -> None:
        assert self._presence is not None
        candidate = self._candidate(device_id)
        if (
            not self._connection_admission_open
            or candidate is None
            or not candidate.admitted
            or candidate.action != "INITIATE"
            or device_id in self._socket_by_device
        ):
            self._reconnect_attempt_by_device.pop(device_id, None)
            return
        try:
            result = self._supervisor.connect(candidate.network_id)
        except WorkerSupervisorError:
            if not self._supervisor.health_updates()["workerAlive"]:
                raise
            self._schedule_connect_retry(device_id)
            return
        self._register_connection(
            device_id=candidate.device_id,
            initiator=True,
            network_id=candidate.network_id,
            socket=int(result["socket"]),
            mtu=int(result["mtu"]),
        )

    def _system_state_health_snapshot(self) -> Mapping[str, Any]:
        worker_alive = bool(self._supervisor.health_updates()["workerAlive"])
        return MappingProxyType(
            {
                "state": (
                    "READY"
                    if (
                        worker_alive
                        and self._listener_socket is not None
                        and not self._stopped
                    )
                    else "DEGRADED"
                ),
                "providerReady": self._provider_ready,
            }
        )

    @staticmethod
    def _application_reason(error: BaseException) -> str:
        code = str(getattr(error, "code", "INTERNAL_ERROR"))
        return code if code in protocol.RPC_ERROR_CODES else "INTERNAL_ERROR"

    def _current_connection(
        self, *, device_id: str, socket: int, generation: int
    ) -> _Connection:
        connection = self._connections_by_socket.get(socket)
        if (
            connection is None
            or connection.device_id != device_id
            or connection.generation != generation
            or not connection.binding.ready
        ):
            raise DeviceContextError("STALE_GENERATION")
        return connection

    async def _request_application(
        self,
        connection: _Connection,
        method: str,
        params: Mapping[str, Any],
        *,
        extensions: tuple[str, ...] = (protocol.DEVICE_CONTEXT_EXTENSION_URI,),
        timeout: float = float(protocol.CONTROL_TIMEOUT_S),
    ) -> ApplicationResponse:
        request = connection.binding.request_application(
            method,
            params,
            extensions=extensions,
        )
        key = (connection.socket, request.request_id)
        if key in self._application_waiters:
            raise DeviceContextError("INTERNAL_ERROR")
        waiter = asyncio.get_running_loop().create_future()
        record = _ApplicationWaiter(waiter, method)
        self._application_waiters[key] = record
        try:
            self._enqueue_outbound(connection, (request.frame,))
            self._drain_send_scheduler()
            # Worker operation counts and send-queue reservations change on
            # the outbound path even when the peer cannot return an event.
            # Refresh the bounded cache here so health/diagnostic readers do
            # not wait for unrelated inbound traffic to observe that send.
            self._cache_diagnostic()
            return await asyncio.wait_for(
                waiter,
                timeout=timeout,
            )
        except TimeoutError as error:
            if method in {
                "SendMessage",
                "SendStreamingMessage",
                "SubscribeToTask",
                "CancelTask",
                "GetTask",
            }:
                raise AgentMessageError(
                    "DEADLINE_EXCEEDED", outcome_unknown=True
                ) from error
            raise DeviceContextError("DEADLINE_EXCEEDED") from error
        finally:
            if self._application_waiters.get(key) is record:
                del self._application_waiters[key]

    async def _open_application_stream(
        self,
        connection: _Connection,
        method: str,
        params: Mapping[str, Any],
    ) -> tuple[_ApplicationStreamWaiter, ApplicationResponse]:
        request = connection.binding.request_application(
            method,
            params,
            extensions=(),
        )
        key = (connection.socket, request.request_id)
        if key in self._application_waiters:
            raise AgentMessageError("INTERNAL_ERROR")
        record = _ApplicationStreamWaiter(
            asyncio.Queue(maxsize=64),
            method,
            request.request_id,
            connection.socket,
        )
        self._application_waiters[key] = record
        try:
            self._enqueue_outbound(connection, (request.frame,))
            self._drain_send_scheduler()
            self._cache_diagnostic()
            item = await asyncio.wait_for(
                record.queue.get(),
                timeout=float(protocol.CONTROL_TIMEOUT_S),
            )
            record.queue.task_done()
            if isinstance(item, BaseException):
                raise item
            return record, item
        except TimeoutError as error:
            if self._application_waiters.get(key) is record:
                del self._application_waiters[key]
            raise AgentMessageError(
                "DEADLINE_EXCEEDED", outcome_unknown=True
            ) from error
        except BaseException:
            if self._application_waiters.get(key) is record:
                del self._application_waiters[key]
            raise

    async def _next_application_stream(
        self,
        record: _ApplicationStreamWaiter,
    ) -> ApplicationResponse:
        item = await record.queue.get()
        record.queue.task_done()
        if isinstance(item, BaseException):
            raise item
        return item

    def _detach_application_stream(
        self,
        record: _ApplicationStreamWaiter,
    ) -> None:
        key = (record.socket, record.request_id)
        if self._application_waiters.get(key) is record:
            del self._application_waiters[key]

    async def _fetch_device_context(
        self, *, device_id: str, socket: int, generation: int
    ) -> None:
        current_task = asyncio.current_task()
        active_method = "mclaw.deviceManifest.get"
        try:
            connection = self._current_connection(
                device_id=device_id, socket=socket, generation=generation
            )
            identity = connection.binding.peer_identity
            if identity is None:
                raise DeviceContextError("PEER_NOT_READY")
            manifest_params = self._device_contexts.manifest_condition(device_id)
            manifest_response = await self._request_application(
                connection,
                "mclaw.deviceManifest.get",
                manifest_params,
            )
            connection = self._current_connection(
                device_id=device_id, socket=socket, generation=generation
            )
            if manifest_response.error_reason is not None:
                self._device_contexts.record_error(
                    device_id=device_id,
                    runtime_instance_id=identity.runtime_instance_id,
                    generation=generation,
                    method=manifest_response.method,
                    reason=manifest_response.error_reason,
                )
                return
            self._device_contexts.accept_manifest_result(
                device_id=device_id,
                runtime_instance_id=identity.runtime_instance_id,
                generation=generation,
                result=manifest_response.result,
            )

            active_method = "mclaw.deviceState.get"
            state_response = await self._request_application(
                connection,
                "mclaw.deviceState.get",
                {},
            )
            self._current_connection(
                device_id=device_id, socket=socket, generation=generation
            )
            if state_response.error_reason is not None:
                self._device_contexts.record_error(
                    device_id=device_id,
                    runtime_instance_id=identity.runtime_instance_id,
                    generation=generation,
                    method=state_response.method,
                    reason=state_response.error_reason,
                )
                return
            self._device_contexts.accept_state_result(
                device_id=device_id,
                runtime_instance_id=identity.runtime_instance_id,
                generation=generation,
                result=state_response.result,
            )
        except asyncio.CancelledError:
            raise
        except DeviceContextError as error:
            connection = self._connections_by_socket.get(socket)
            identity = None if connection is None else connection.binding.peer_identity
            if identity is not None and connection.generation == generation:
                try:
                    self._device_contexts.record_error(
                        device_id=device_id,
                        runtime_instance_id=identity.runtime_instance_id,
                        generation=generation,
                        method=active_method,
                        reason=self._application_reason(error),
                    )
                except DeviceContextError:
                    pass
        finally:
            if self._context_fetch_tasks.get(device_id) is current_task:
                del self._context_fetch_tasks[device_id]
            self._cache_diagnostic()

    def _begin_context_fetch(self, connection: _Connection) -> None:
        if not connection.binding.ready:
            return
        key = (connection.socket, connection.generation)
        identity = connection.binding.peer_identity
        card = connection.binding.peer_card
        if identity is None or card is None:
            return
        if key not in self._context_generation_started:
            self._device_contexts.begin_generation(
                device_id=connection.device_id,
                runtime_instance_id=identity.runtime_instance_id,
                generation=connection.generation,
                descriptor=identity.manifest,
                agent_card=card.document,
            )
            self._context_generation_started.add(key)
        task = self._context_fetch_tasks.get(connection.device_id)
        if task is None or task.done():
            self._context_fetch_tasks[connection.device_id] = asyncio.create_task(
                self._fetch_device_context(
                    device_id=connection.device_id,
                    socket=connection.socket,
                    generation=connection.generation,
                ),
                name="mclaw-dsoftbus-device-context-fetch",
            )

    def _handle_application_response(
        self, connection: _Connection, response: ApplicationResponse
    ) -> None:
        key = (connection.socket, response.request_id)
        record = self._application_waiters.get(key)
        if isinstance(record, _ApplicationWaiter):
            self._application_waiters.pop(key, None)
            if not record.future.done():
                record.future.set_result(response)
            return
        if isinstance(record, _ApplicationStreamWaiter):
            if record.queue.full():
                self._application_waiters.pop(key, None)
                self._close_connection(
                    connection.socket,
                    waiter_code="CAPACITY_BUSY",
                    waiter_outcome_unknown=True,
                )
                return
            record.queue.put_nowait(response)
            if response.stream_end:
                self._application_waiters.pop(key, None)

    async def _serve_state_request(
        self,
        *,
        socket: int,
        generation: int,
        request: RequestEnvelope,
        params: Mapping[str, Any],
    ) -> None:
        key = (socket, request.request_id)
        try:
            connection = self._connections_by_socket.get(socket)
            service = self._state_service
            if (
                connection is None
                or connection.generation != generation
                or service is None
            ):
                return
            try:
                state = await service.get_state(connection.device_id, params)
                completed = connection.binding.complete_application_request(
                    request,
                    result={"state": _plain(state.document)},
                )
            except BaseException as error:
                if isinstance(error, asyncio.CancelledError):
                    raise
                completed = connection.binding.complete_application_request(
                    request,
                    error_reason=self._application_reason(error),
                )
            current = self._connections_by_socket.get(socket)
            if current is None or current.generation != generation:
                return
            self._enqueue_outbound(current, completed.outbound)
            if completed.close_generation:
                self._close_connection(socket)
                return
            self._drain_send_scheduler()
            self._cache_diagnostic()
        finally:
            self._inbound_application_tasks.pop(key, None)

    @staticmethod
    def _close_after_send(
        frames: Sequence[OutboundFrame],
    ) -> tuple[OutboundFrame, ...]:
        return tuple(
            OutboundFrame(
                frame.data,
                queue=frame.queue,
                close_after_send=True,
                response_reservation_id=frame.response_reservation_id,
            )
            for frame in frames
        )

    async def _serve_task_request(
        self,
        *,
        socket: int,
        generation: int,
        request: RequestEnvelope,
        call: CoreMethodCall,
    ) -> None:
        key = (socket, request.request_id)
        current_task = asyncio.current_task()
        dispatcher = self._task_dispatcher
        subscription: TaskSubscription | None = None
        first_stream_frame = True
        try:
            connection = self._connections_by_socket.get(socket)
            if (
                dispatcher is None
                or connection is None
                or connection.generation != generation
                or not connection.binding.ready
            ):
                return
            identity = connection.binding.peer_identity
            if identity is None:
                return
            try:
                if request.method in {"SendMessage", "SendStreamingMessage"}:
                    task, subscription = await dispatcher.submit(
                        peer_device_id=connection.device_id,
                        peer_runtime_instance_id=identity.runtime_instance_id,
                        call=call,
                        subscribe=request.method == "SendStreamingMessage",
                    )
                    if request.method == "SendMessage":
                        completed = connection.binding.complete_application_request(
                            request,
                            result={"task": _plain(task)},
                        )
                        self._enqueue_outbound(connection, completed.outbound)
                        self._drain_send_scheduler()
                        self._cache_diagnostic()
                        return
                elif request.method == "GetTask":
                    task = dispatcher.get_task(
                        connection.device_id,
                        identity.runtime_instance_id,
                        str(call.params["id"]),
                    )
                    completed = connection.binding.complete_application_request(
                        request, result=_plain(task)
                    )
                    self._enqueue_outbound(connection, completed.outbound)
                    self._drain_send_scheduler()
                    return
                elif request.method == "ListTasks":
                    result = dispatcher.list_tasks(
                        connection.device_id,
                        identity.runtime_instance_id,
                        context_id=call.params.get("contextId"),
                        state=call.params.get("status"),
                        page_size=int(call.params["pageSize"]),
                        include_artifacts=bool(
                            call.params.get("includeArtifacts", False)
                        ),
                    )
                    completed = connection.binding.complete_application_request(
                        request, result=_plain(result)
                    )
                    self._enqueue_outbound(connection, completed.outbound)
                    self._drain_send_scheduler()
                    return
                elif request.method == "CancelTask":
                    task = await dispatcher.cancel_task(
                        connection.device_id,
                        identity.runtime_instance_id,
                        str(call.params["id"]),
                    )
                    completed = connection.binding.complete_application_request(
                        request, result=_plain(task)
                    )
                    self._enqueue_outbound(connection, completed.outbound)
                    self._drain_send_scheduler()
                    return
                elif request.method == "SubscribeToTask":
                    subscription = dispatcher.subscribe_task(
                        connection.device_id,
                        identity.runtime_instance_id,
                        str(call.params["id"]),
                    )
                else:
                    raise AgentMessageError("METHOD_NOT_FOUND")

                assert subscription is not None
                while True:
                    event = await subscription.next_event()
                    if event is None:
                        break
                    connection = self._connections_by_socket.get(socket)
                    if connection is None or connection.generation != generation:
                        return
                    completed = connection.binding.complete_application_stream_event(
                        request,
                        result=event,
                        first=first_stream_frame,
                    )
                    self._enqueue_outbound(connection, completed.outbound)
                    first_stream_frame = False
                    self._drain_send_scheduler()
                    self._cache_diagnostic()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - RPC isolation boundary
                connection = self._connections_by_socket.get(socket)
                if connection is None or connection.generation != generation:
                    return
                reason = self._application_reason(error)
                outcome_unknown = bool(
                    getattr(error, "outcome_unknown", False)
                )
                if request.method in {
                    "SendStreamingMessage",
                    "SubscribeToTask",
                }:
                    completed = connection.binding.complete_application_stream_error(
                        request,
                        error_reason=reason,
                        first=first_stream_frame,
                        outcome_unknown=outcome_unknown,
                    )
                else:
                    completed = connection.binding.complete_application_request(
                        request,
                        error_reason=reason,
                        outcome_unknown=outcome_unknown,
                    )
                self._enqueue_outbound(connection, completed.outbound)
                self._drain_send_scheduler()
                self._cache_diagnostic()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - generation fails closed
            self._close_connection(socket)
        finally:
            if subscription is not None and dispatcher is not None:
                dispatcher.detach(subscription)
            if self._inbound_application_tasks.get(key) is current_task:
                del self._inbound_application_tasks[key]
                self._inbound_message_fingerprints.pop(key, None)

    def _handle_application_request(
        self,
        connection: _Connection,
        request: RequestEnvelope,
        *,
        wire_sha256: str = "",
    ) -> tuple[OutboundFrame, ...]:
        key = (connection.socket, request.request_id)
        task_methods = {
            "SendMessage",
            "SendStreamingMessage",
            "GetTask",
            "ListTasks",
            "CancelTask",
            "SubscribeToTask",
        }
        if request.method in task_methods and key in self._inbound_application_tasks:
            if self._inbound_message_fingerprints.get(key) == wire_sha256:
                return ()
            task = self._inbound_application_tasks.get(key)
            if task is not None:
                task.cancel()
            identity = connection.binding.peer_identity
            if identity is not None and self._task_dispatcher is not None:
                self._task_dispatcher.record_rejection("INVALID_REQUEST")
                self._task_dispatcher.generation_closed(
                    connection.device_id,
                    identity.runtime_instance_id,
                    connection.generation,
                )
            rejected = connection.binding.complete_application_request(
                request,
                error_reason="INVALID_REQUEST",
            )
            return self._close_after_send(rejected.outbound)
        validation_error = ""
        call: CoreMethodCall | None = None
        try:
            validated = validate_core_method(request.method, request.params)
            if isinstance(validated, CoreMethodCall):
                call = validated
            else:
                validation_error = "INVALID_PARAMS"
        except A2AError as error:
            validation_error = error.reason
        try:
            self._send_scheduler.reserve_response(
                connection.socket,
                connection.generation,
                request.request_id,
                connection.binding.response_reservation_bytes,
            )
        except SoftBusBindingError as error:
            if error.code not in {"CAPACITY_BUSY", "RUNTIME_STOPPING"}:
                raise
            if request.method in task_methods and self._task_dispatcher is not None:
                self._task_dispatcher.record_rejection(error.code)
            return connection.binding.reject_application_request(
                request,
                error.code,
            ).outbound
        if validation_error:
            if request.method in task_methods and self._task_dispatcher is not None:
                self._task_dispatcher.record_rejection(validation_error)
            return connection.binding.complete_application_request(
                request,
                error_reason=validation_error,
            ).outbound
        assert call is not None
        if request.method in {
            "mclaw.deviceManifest.get",
            "mclaw.deviceState.get",
        } and not connection.binding.device_context_extension_allowed(request):
            return connection.binding.complete_application_request(
                request,
                error_reason="EXTENSION_SUPPORT_REQUIRED",
            ).outbound
        if request.method == "mclaw.deviceManifest.get":
            publications = self._publications
            if publications is None:
                return connection.binding.complete_application_request(
                    request,
                    error_reason="PEER_NOT_READY",
                ).outbound
            descriptor = publications.manifest.descriptor
            if call.params:
                result: Mapping[str, Any] = {
                    "notModified": {
                        "revision": descriptor.revision,
                        "digest": descriptor.digest,
                    }
                } if (
                    call.params["ifRevision"] == descriptor.revision
                    and call.params["ifDigest"] == descriptor.digest
                ) else {"manifest": _plain(publications.manifest.document)}
            else:
                result = {"manifest": _plain(publications.manifest.document)}
            return connection.binding.complete_application_request(
                request,
                result=result,
            ).outbound
        if request.method == "mclaw.deviceState.get":
            if key in self._inbound_application_tasks:
                return connection.binding.complete_application_request(
                    request,
                    error_reason="INVALID_REQUEST",
                ).outbound
            self._inbound_application_tasks[key] = asyncio.create_task(
                self._serve_state_request(
                    socket=connection.socket,
                    generation=connection.generation,
                    request=request,
                    params=call.params,
                ),
                name="mclaw-dsoftbus-device-state-request",
            )
            return ()
        if request.method in task_methods:
            if self._task_dispatcher is None:
                return connection.binding.complete_application_request(
                    request,
                    error_reason=(
                        "REMOTE_PROVIDER_UNAVAILABLE"
                        if not self._provider_ready
                        else "PEER_NOT_READY"
                    ),
                ).outbound
            task = asyncio.create_task(
                self._serve_task_request(
                    socket=connection.socket,
                    generation=connection.generation,
                    request=request,
                    call=call,
                ),
                name="mclaw-dsoftbus-agent-task-request",
            )
            self._inbound_application_tasks[key] = task
            self._inbound_message_fingerprints[key] = wire_sha256
            return ()
        return connection.binding.complete_application_request(
            request,
            error_reason="PEER_NOT_READY",
        ).outbound

    def _fail_application_waiters(
        self,
        *,
        socket: int | None,
        code: str,
        outcome_unknown: bool,
    ) -> None:
        if code not in protocol.RPC_ERROR_CODES or type(outcome_unknown) is not bool:
            raise ValueError("application waiter failure is invalid")
        for key, record in tuple(self._application_waiters.items()):
            if socket is not None and key[0] != socket:
                continue
            if isinstance(record, _ApplicationWaiter) and not record.future.done():
                error: BaseException
                if record.method in {
                    "SendMessage",
                    "SendStreamingMessage",
                    "SubscribeToTask",
                    "CancelTask",
                    "GetTask",
                }:
                    error = AgentMessageError(
                        code,
                        outcome_unknown=outcome_unknown,
                    )
                else:
                    error = DeviceContextError(code)
                record.future.set_exception(error)
            elif isinstance(record, _ApplicationStreamWaiter):
                error = AgentMessageError(
                    code,
                    outcome_unknown=outcome_unknown,
                )
                if not record.queue.full():
                    record.queue.put_nowait(error)
            del self._application_waiters[key]

    def _close_connection(
        self,
        socket: int,
        *,
        close_native: bool = True,
        reconnect: bool = True,
        waiter_code: str = "STALE_GENERATION",
        waiter_outcome_unknown: bool = True,
    ) -> None:
        connection = self._connections_by_socket.pop(socket, None)
        if connection is None:
            return
        if self._socket_by_device.get(connection.device_id) == socket:
            del self._socket_by_device[connection.device_id]
        identity = connection.binding.peer_identity
        if identity is not None and self._task_dispatcher is not None:
            self._task_dispatcher.generation_closed(
                connection.device_id,
                identity.runtime_instance_id,
                connection.generation,
            )
        connection.binding.close()
        self._device_contexts.mark_disconnected(
            connection.device_id, connection.generation
        )
        self._context_generation_started.discard(
            (connection.socket, connection.generation)
        )
        context_task = self._context_fetch_tasks.pop(connection.device_id, None)
        if context_task is not None:
            context_task.cancel()
        for key, task in tuple(self._inbound_application_tasks.items()):
            if key[0] == socket:
                task.cancel()
                del self._inbound_application_tasks[key]
                self._inbound_message_fingerprints.pop(key, None)
        self._fail_application_waiters(
            socket=socket,
            code=waiter_code,
            outcome_unknown=waiter_outcome_unknown,
        )
        if self._state_service is not None:
            self._state_service.release_peer(connection.device_id)
        try:
            self._send_scheduler.unregister(socket, connection.generation)
        except SoftBusBindingError:
            pass
        if close_native:
            try:
                self._supervisor.close_socket(socket)
            except WorkerSupervisorError:
                pass
        if reconnect:
            self._schedule_connect(
                connection.device_id,
                immediate=True,
                reconnecting=True,
            )

    def _enqueue_outbound(
        self, connection: _Connection, frames: Sequence[OutboundFrame]
    ) -> None:
        for frame in frames:
            self._send_scheduler.enqueue(
                connection.socket,
                connection.generation,
                frame,
            )

    def _drain_send_scheduler(self) -> None:
        while True:
            try:
                completion = self._send_scheduler.drain_one(
                    self._supervisor.send_bytes
                )
            except SoftBusBindingError as error:
                if error.socket is not None:
                    self._close_connection(error.socket)
                if not self._supervisor.health_updates()["workerAlive"]:
                    raise WorkerSupervisorError("WORKER_PROTOCOL_ERROR") from error
                return
            if completion is None:
                return
            if completion.close_after_send:
                self._close_connection(completion.socket)

    def _handle_binding_bytes(self, socket: int, encoded_data: Any) -> None:
        connection = self._connections_by_socket.get(socket)
        if connection is None:
            try:
                self._supervisor.close_socket(socket)
            except WorkerSupervisorError:
                pass
            return
        try:
            raw = protocol.decode_strict_base64(encoded_data)
            was_ready = connection.binding.ready
            result = connection.binding.receive(raw)
            outbound = list(result.outbound)
            if result.application_request is not None:
                outbound.extend(
                    self._handle_application_request(
                        connection,
                        result.application_request,
                        wire_sha256=hashlib.sha256(raw).hexdigest(),
                    )
                )
            if result.application_response is not None:
                self._handle_application_response(
                    connection, result.application_response
                )
            self._enqueue_outbound(connection, outbound)
            if result.close_generation:
                self._close_connection(socket)
                return
            self._drain_send_scheduler()
            if not was_ready and connection.binding.ready:
                self._begin_context_fetch(connection)
        except (protocol.ProtocolError, SoftBusBindingError) as error:
            self._event_failure_code = str(
                getattr(error, "code", "BINDING_INCOMPATIBLE")
            )
            self._close_connection(socket)

    def _process_event(self, event: Mapping[str, Any]) -> None:
        if self._presence is None:
            raise WorkerSupervisorError("WORKER_PROTOCOL_ERROR")
        event_type = str(event["event"])
        data = event["data"]
        if event_type == "node-online":
            sequence = int(data["nodeEventSeq"])
            try:
                udid = self._supervisor.get_node_udid(str(data["networkId"]))
            except WorkerSupervisorError:
                if not self._supervisor.health_updates()["workerAlive"]:
                    raise
                self._presence.discard_node_event(node_event_seq=sequence)
                return
            transition = self._presence.node_online(
                DiscoveredNode(
                    network_id=str(data["networkId"]),
                    udid=udid,
                    device_name=str(data["deviceName"]),
                    device_type_id=int(data["deviceTypeId"]),
                ),
                node_event_seq=sequence,
            )
            if transition.admitted:
                self._schedule_connect(
                    transition.device_id,
                    immediate=True,
                    reconnecting=(
                        transition.device_id
                        in self._connection_generation_by_device
                    ),
                )
                self._run_due_connect_attempts()
            return
        if event_type == "node-offline":
            transition = self._presence.node_offline(
                str(data["networkId"]), node_event_seq=int(data["nodeEventSeq"])
            )
            if transition.device_id:
                socket = self._socket_by_device.get(transition.device_id)
                if socket is not None:
                    self._close_connection(socket, reconnect=False)
                self._connection_state_by_device.pop(transition.device_id, None)
                self._reconnect_attempt_by_device.pop(transition.device_id, None)
            return
        if event_type == "bound":
            device_id = self._presence.device_id_for_network(str(data["networkId"]))
            if device_id is None:
                self._supervisor.close_socket(int(data["socket"]))
                return
            candidates = {
                candidate.device_id: candidate
                for candidate in self._presence.connection_candidates()
            }
            candidate = candidates[device_id]
            self._register_connection(
                device_id=device_id,
                initiator=candidate.action == "INITIATE",
                network_id=str(data["networkId"]),
                socket=int(data["socket"]),
                mtu=int(data["mtu"]),
            )
            return
        if event_type == "closed":
            self._close_connection(int(data["socket"]), close_native=False)
            return
        if event_type == "bytes":
            self._handle_binding_bytes(int(data["socket"]), data["data"])
            return
        raise WorkerSupervisorError("WORKER_PROTOCOL_ERROR")

    def _drain_runtime_events(self) -> None:
        while True:
            event = self._supervisor.pop_event()
            if event is None:
                self._drain_send_scheduler()
                return
            self._process_event(event)

    def _invalidate_failed_worker_epoch(self) -> None:
        """Drop every object whose authority came from the failed Worker."""

        for socket in tuple(self._connections_by_socket):
            self._close_connection(
                socket,
                close_native=False,
                reconnect=False,
            )
        self._listener_socket = None
        self._send_scheduler.clear()
        self._socket_by_device.clear()
        self._connection_state_by_device.clear()
        self._reconnect_attempt_by_device.clear()
        for task in tuple(self._context_fetch_tasks.values()):
            task.cancel()
        self._context_fetch_tasks.clear()
        for task in tuple(self._inbound_application_tasks.values()):
            task.cancel()
        self._inbound_application_tasks.clear()
        self._inbound_message_fingerprints.clear()
        self._fail_application_waiters(
            socket=None,
            code="STALE_GENERATION",
            outcome_unknown=True,
        )
        self._context_generation_started.clear()
        self._device_contexts.clear()
        if self._presence is not None:
            self._presence.clear()

    def _recover_worker_epoch(self) -> None:
        """Rebuild discovery and every Native-owned handle after one epoch dies."""

        publications = self._publications
        presence = self._presence
        if publications is None or presence is None:
            raise WorkerSupervisorError("WORKER_RESTART_NOT_ALLOWED")
        verified = self._supervisor.recover()
        if (
            verified.public_device_id != publications.device_id
            or verified.public_agent_id != publications.agent_id
        ):
            raise WorkerSupervisorError("WORKER_IDENTITY_MISMATCH")
        presence.reset_worker_epoch(verified.worker_epoch)
        self._supervisor.complete_manifest_phase_b(publications.gate_mapping())
        self._supervisor.start_node_events()
        self._collect_snapshot()
        self._listener_socket = self._supervisor.listen()
        if self._publication_counts["listenerGenerationCount"] >= 2**63 - 1:
            raise WorkerSupervisorError("WORKER_PROTOCOL_ERROR")
        self._publication_counts["listenerGenerationCount"] += 1
        self._initialize_connection_candidates()
        self._drain_runtime_events()

    async def _event_pump(self) -> None:
        while not self._stopped:
            try:
                self._drain_runtime_events()
                self._run_due_connect_attempts()
                self._cache_diagnostic()
                await asyncio.sleep(0.02)
            except asyncio.CancelledError:
                raise
            except BaseException as error:
                self._event_failure_code = str(
                    getattr(error, "code", "WORKER_PROTOCOL_ERROR")
                )
                self._invalidate_failed_worker_epoch()
                self._cache_diagnostic(
                    lifecycle_state="DEGRADED",
                    degraded_reasons=(
                        self._degraded_reason(error, startup_step="worker"),
                    ),
                )
                try:
                    self._recover_worker_epoch()
                except BaseException as recovery_error:
                    self._event_failure_code = str(
                        getattr(
                            recovery_error,
                            "code",
                            "WORKER_PROTOCOL_ERROR",
                        )
                    )
                    self._cache_diagnostic(
                        lifecycle_state="DEGRADED",
                        degraded_reasons=(
                            self._degraded_reason(
                                recovery_error,
                                startup_step="worker",
                            ),
                        ),
                    )
                    return
                self._event_failure_code = ""
                self._cache_diagnostic(
                    lifecycle_state="READY",
                    degraded_reasons=(),
                )

    def _rollback_started_worker(self) -> None:
        self._connection_admission_open = False
        self._listener_socket = None
        self._current_card = None
        self._send_scheduler.clear()
        self._connections_by_socket.clear()
        self._socket_by_device.clear()
        self._connection_state_by_device.clear()
        self._reconnect_attempt_by_device.clear()
        if self._state_service is not None:
            self._state_service.close()
            self._state_service = None
        self._device_contexts.clear()
        self._context_generation_started.clear()
        if self._presence is not None:
            self._presence.clear()
        try:
            self._supervisor.begin_shutdown()
        except BaseException:
            pass
        try:
            self._supervisor.stop(self._monotonic() + 2.0)
        except BaseException:
            self._supervisor.emergency_reap()

    async def start(self, runtime_instance_id: str) -> Mapping[str, Any]:
        self._require_owner(establish=True)
        if self._started:
            raise RuntimeError("DISCOVERY_RESOURCE_ALREADY_STARTED")
        self._started = True
        startup_step = "worker"
        try:
            if self._pairing_store is not None:
                self._pairing_store.load()
            verified = self._supervisor.start()
            presence = InMemoryPresenceAdapter(
                local_device_id=verified.public_device_id,
                socket_cap=verified.socket_cap,
            )
            presence.reset_worker_epoch(verified.worker_epoch)
            self._presence = presence

            # A manifest-less adapter remains useful for the lower-level
            # supervisor/presence tests, but it cannot cross the product gate.
            if self._manifest_template is None:
                presence.apply_snapshot(self._initial_nodes)
                self._cache_diagnostic()
                return MappingProxyType(
                    {
                        "degradedReasons": ("PRODUCT_INTEGRATION_UNVERIFIED",),
                        "healthUpdates": self._health_updates(),
                        "state": "DEGRADED",
                    }
                )

            startup_step = "publication"
            publications = freeze_local_publications(
                template=self._manifest_template,
                verified_device_id=verified.public_device_id,
                verified_agent_id=verified.public_agent_id,
                runtime_instance_id=runtime_instance_id,
                provider_ready=self._provider_ready,
                provider_readiness_code=self._provider_readiness_code,
            )
            self._publications = publications
            self._current_card = publications.card_preflight
            self._state_service = LocalDeviceStateService(
                template=self._manifest_template,
                manifest=publications.manifest,
                runtime_instance_id=runtime_instance_id,
                health_snapshot=self._system_state_health_snapshot,
                monotonic=self._monotonic,
            )
            self._publication_counts.update(
                {
                    "agentCardGenerationCount": 1,
                    "manifestDescriptorGenerationCount": 1,
                    "publicManifestGenerationCount": 1,
                }
            )
            self._supervisor.complete_manifest_phase_b(publications.gate_mapping())

            startup_step = "snapshot"
            self._supervisor.start_node_events()
            self._collect_snapshot()

            startup_step = "listener"
            self._listener_socket = self._supervisor.listen()
            self._publication_counts["listenerGenerationCount"] = 1
            self._initialize_connection_candidates()
            self._drain_runtime_events()
            self._supervisor.enable_recovery_after_ready()
            self._event_task = asyncio.create_task(
                self._event_pump(), name="mclaw-dsoftbus-event-pump"
            )
        except (
            DeviceContextError,
            PublicationError,
            PresenceError,
            PairingOwnershipError,
            WorkerSupervisorError,
        ) as error:
            reason = self._degraded_reason(error, startup_step=startup_step)
            self._rollback_started_worker()
            self._cache_diagnostic()
            return MappingProxyType(
                {
                    "degradedReasons": (reason,),
                    "healthUpdates": self._health_updates(),
                    "state": "DEGRADED",
                }
            )
        self._cache_diagnostic()
        return MappingProxyType(
            {
                "degradedReasons": (),
                "healthUpdates": self._health_updates(),
                "state": "READY",
            }
        )

    async def begin_shutdown(self) -> Mapping[str, Any] | None:
        self._require_owner()
        if self._stopped:
            return self._health_updates()
        self._connection_admission_open = False
        self._connection_state_by_device.clear()
        self._reconnect_attempt_by_device.clear()
        if self._presence is not None:
            self._presence.begin_shutdown()
        if self._task_dispatcher is not None:
            self._task_dispatcher.begin_shutdown()
        await self._cancel_tracked_outbound_tasks()
        for task in tuple(self._context_fetch_tasks.values()):
            task.cancel()
        # Keep the already established response path open until ``stop`` has
        # terminalized admitted Tasks.  Otherwise a Task can be persisted as
        # failed/canceled locally while its final status is rejected by the
        # send scheduler and the caller waits forever.
        self._cache_diagnostic()
        return self._health_updates()

    async def stop(
        self, deadline: Callable[[], float]
    ) -> Mapping[str, Any] | None:
        self._require_owner()
        if self._stopped:
            return self._health_updates()
        await self._cancel_tracked_outbound_tasks()
        self._stopped = True
        self._connection_admission_open = False
        self._connection_state_by_device.clear()
        self._reconnect_attempt_by_device.clear()
        absolute_deadline = deadline()
        if self._task_dispatcher is not None:
            await self._task_dispatcher.drain(absolute_deadline)
        for task in tuple(self._context_fetch_tasks.values()):
            task.cancel()
        pending_tasks = tuple(self._context_fetch_tasks.values()) + tuple(
            self._inbound_application_tasks.values()
        )
        if pending_tasks:
            remaining = max(0.0, absolute_deadline - self._monotonic())
            _done, pending = await asyncio.wait(
                pending_tasks,
                timeout=remaining,
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        self._context_fetch_tasks.clear()
        self._inbound_application_tasks.clear()
        self._inbound_message_fingerprints.clear()
        self._outbound_task_ids.clear()
        self._outbound_cancel_attempts.clear()
        # All Task stream producers have now either sent their terminal item
        # or were bounded by the Runtime stop deadline.
        self._send_scheduler.begin_shutdown()
        task = self._event_task
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._event_task = None
        for socket in tuple(self._connections_by_socket):
            self._close_connection(
                socket,
                reconnect=False,
                waiter_code="RUNTIME_STOPPING",
                waiter_outcome_unknown=True,
            )
        self._send_scheduler.clear()
        self._socket_by_device.clear()
        self._listener_socket = None
        self._current_card = None
        if self._state_service is not None:
            self._state_service.close()
            self._state_service = None
        self._device_contexts.clear()
        self._context_generation_started.clear()
        if self._presence is not None:
            self._presence.clear()
        self._supervisor.stop(absolute_deadline)
        self._cache_diagnostic()
        return self._health_updates()

    async def update_provider_runtime(
        self, context: Any | None
    ) -> Mapping[str, Any] | None:
        self._require_owner()
        provider_ready, provider_code = self._provider_readiness(context)
        try:
            if self._task_dispatcher is not None:
                self._task_dispatcher.update_provider_runtime(
                    context,
                    provider_ready=provider_ready,
                )
        except Exception:
            self._provider_runtime = None
            self._provider_ready = False
            self._provider_readiness_code = "PROVIDER_SYNC_FAILED"
            raise
        self._provider_runtime = context
        self._provider_ready = provider_ready
        self._provider_readiness_code = provider_code
        if self._publications is not None:
            self._current_card = self._publications.build_agent_card(
                provider_ready=self._provider_ready,
                provider_readiness_code=self._provider_readiness_code,
            )
            self._publication_counts["agentCardGenerationCount"] += 1
        return self._health_updates()

    def local_turn_started(self, token: str) -> None:
        self._require_owner()
        if self._task_dispatcher is not None:
            self._task_dispatcher.local_turn_started(token)
            self._cache_diagnostic()

    def local_turn_finished(self, token: str) -> None:
        self._require_owner()
        if self._task_dispatcher is not None:
            self._task_dispatcher.local_turn_finished(token)
            self._cache_diagnostic()

    def emergency_reap(self) -> None:
        self._stopped = True
        self._connection_admission_open = False
        self._connection_state_by_device.clear()
        self._reconnect_attempt_by_device.clear()
        self._supervisor.emergency_reap()

    def cached_diagnostic(self) -> Mapping[str, Any]:
        """Return a non-sensitive snapshot without crossing mutable state."""

        with self._cache_lock:
            value = self._cached_diagnostic
            return MappingProxyType(
                {
                    "activeThreadCount": value.get("activeThreadCount", 0),
                    "agentSessionTaskCount": value.get("agentSessionTaskCount", 0),
                    "dispatchExecutionCount": value.get(
                        "dispatchExecutionCount", 0
                    ),
                    "dispatchQueueBytes": value.get("dispatchQueueBytes", 0),
                    "dispatchQueueCount": value.get("dispatchQueueCount", 0),
                    "eventFailureCode": value.get("eventFailureCode", ""),
                    "inflightMessageCount": value.get(
                        "inflightMessageCount", 0
                    ),
                    "localTurnCount": value.get("localTurnCount", 0),
                    "operationCounts": MappingProxyType(
                        dict(value.get("operationCounts", {}))
                    ),
                    "peerCount": value.get("peerCount", 0),
                    "remoteAccepted": value.get("remoteAccepted", 0),
                    "remoteBudgetUsed": value.get("remoteBudgetUsed", 0),
                    "remoteRejectedByCode": MappingProxyType(
                        dict(value.get("remoteRejectedByCode", {}))
                    ),
                    "responseCacheCount": value.get("responseCacheCount", 0),
                    "tokenReserved": value.get("tokenReserved", 0),
                    "workerAlive": value.get("workerAlive", False),
                }
            )

    def cached_public_peers(self) -> tuple[Mapping[str, Any], ...]:
        with self._cache_lock:
            return tuple(MappingProxyType(dict(value)) for value in self._cached_public_peers)

    async def discover_devices(self) -> tuple[Mapping[str, Any], ...]:
        """Run one bounded DeviceManager scan and return redacted candidates."""

        self._require_owner()
        if self._stopped or not self._started:
            raise WorkerSupervisorError("WORKER_NOT_READY")
        self._supervisor.start_device_discovery()
        stopped = False
        try:
            await self._sleep(self._discovery_window_s)
            result = self._supervisor.stop_device_discovery()
            stopped = True
        finally:
            if not stopped:
                try:
                    self._supervisor.stop_device_discovery()
                except BaseException:
                    pass
        devices = tuple(
            MappingProxyType(
                {
                    "deviceIdSha256": str(item["deviceIdSha256"]),
                    "deviceName": str(item["deviceName"]),
                    "deviceTypeId": int(item["deviceTypeId"]),
                }
            )
            for item in result["devices"]
        )
        self._cache_diagnostic()
        return devices

    async def pair_device(self, device_id_sha256: str) -> Mapping[str, Any]:
        """Begin a user-confirmed system bind and await its bounded outcome."""

        self._require_owner()
        if not isinstance(device_id_sha256, str) or not _is_hex64(
            device_id_sha256
        ):
            raise WorkerSupervisorError("INVALID_PARAMS")
        store = self._pairing_store
        if store is not None:
            store.add(device_id_sha256)
        try:
            begun = self._supervisor.begin_device_bind(device_id_sha256)
        except BaseException:
            if store is not None and store.contains(device_id_sha256):
                store.remove(device_id_sha256)
            raise
        if (
            begun.get("binding") is not True
            or begun.get("deviceIdSha256") != device_id_sha256
        ):
            if store is not None and store.contains(device_id_sha256):
                store.remove(device_id_sha256)
            raise WorkerSupervisorError("WORKER_PROTOCOL_ERROR")

        deadline = self._monotonic() + self._bind_timeout_s
        while True:
            status = self._supervisor.get_device_bind_status(device_id_sha256)
            state = str(status["status"])
            native_code = int(status["nativeCode"])
            if state == "bound":
                self._cache_diagnostic()
                return MappingProxyType(
                    {
                        "bound": True,
                        "deviceIdSha256": device_id_sha256,
                        "nativeCode": 0,
                        "status": "bound",
                    }
                )
            if state == "failed":
                if store is not None and store.contains(device_id_sha256):
                    store.remove(device_id_sha256)
                self._cache_diagnostic()
                return MappingProxyType(
                    {
                        "bound": False,
                        "deviceIdSha256": device_id_sha256,
                        "nativeCode": native_code,
                        "status": "failed",
                    }
                )
            if self._monotonic() >= deadline:
                raise WorkerSupervisorError(
                    "DEVICE_BIND_TIMEOUT", outcome_unknown=True
                )
            await self._sleep(
                min(
                    self._bind_poll_interval_s,
                    max(0.0, deadline - self._monotonic()),
                )
            )

    def list_trusted_devices(self) -> tuple[Mapping[str, Any], ...]:
        """Enumerate DeviceManager targets without exposing raw system IDs."""

        self._require_owner()
        presence = self._presence
        managed = (
            None
            if self._pairing_store is None
            else set(self._pairing_store.digests())
        )
        rows: list[Mapping[str, Any]] = []
        seen: set[str] = set()
        for item in self._supervisor.list_trusted_devices():
            digest = str(item["deviceIdSha256"])
            if digest in seen:
                raise WorkerSupervisorError("DEVICE_TARGET_AMBIGUOUS")
            seen.add(digest)
            if managed is not None and digest not in managed:
                continue
            network_id = str(item["networkId"])
            public_device_id = ""
            if presence is not None and len(network_id.encode("utf-8")) <= 64:
                public_device_id = presence.device_id_for_network(network_id) or ""
            rows.append(
                MappingProxyType(
                    {
                        "deviceIdSha256": digest,
                        "deviceName": str(item["deviceName"]),
                        "deviceTypeId": int(item["deviceTypeId"]),
                        "online": bool(public_device_id),
                        "publicDeviceId": public_device_id,
                    }
                )
            )
        rows.sort(key=lambda row: str(row["deviceIdSha256"]))
        self._cache_diagnostic()
        return tuple(rows)

    async def unbind_device(self, device_id_sha256: str) -> Mapping[str, Any]:
        """Unbind one uniquely selected DeviceManager target on the owner loop."""

        self._require_owner()
        if (
            not isinstance(device_id_sha256, str)
            or not _is_hex64(device_id_sha256)
        ):
            raise WorkerSupervisorError("INVALID_PARAMS")
        if self._pairing_store is not None and not self._pairing_store.contains(
            device_id_sha256
        ):
            raise WorkerSupervisorError("DEVICE_NOT_MANAGED")
        trusted = self._supervisor.list_trusted_devices()
        matches = [
            item
            for item in trusted
            if str(item["deviceIdSha256"]) == device_id_sha256
        ]
        if not matches:
            raise WorkerSupervisorError("DEVICE_NOT_FOUND")
        if len(matches) != 1:
            raise WorkerSupervisorError("DEVICE_TARGET_AMBIGUOUS")
        target = matches[0]
        network_id = str(target["networkId"])
        presence = self._presence
        public_device_id = ""
        if presence is not None and len(network_id.encode("utf-8")) <= 64:
            public_device_id = presence.device_id_for_network(network_id) or ""

        result = self._supervisor.unbind_device(network_id)
        if (
            result.get("unbound") is not True
            or result.get("deviceIdSha256") != device_id_sha256
        ):
            raise WorkerSupervisorError("WORKER_PROTOCOL_ERROR")
        remaining = {
            str(item["deviceIdSha256"])
            for item in self._supervisor.list_trusted_devices()
        }
        if device_id_sha256 in remaining:
            raise WorkerSupervisorError("DEVICE_UNBIND_UNCONFIRMED")
        if self._pairing_store is not None:
            self._pairing_store.remove(device_id_sha256)

        if public_device_id:
            socket = self._socket_by_device.get(public_device_id)
            if socket is not None:
                self._close_connection(socket, reconnect=False)
            self._connection_state_by_device.pop(public_device_id, None)
            self._reconnect_attempt_by_device.pop(public_device_id, None)
        if presence is not None and len(network_id.encode("utf-8")) <= 64:
            presence.trust_revoked(network_id)
        self._cache_diagnostic()
        return MappingProxyType(
            {
                "deviceIdSha256": device_id_sha256,
                "publicDeviceId": public_device_id,
                "unbound": True,
            }
        )

    def cached_device_context(self, device_id: str) -> Mapping[str, Any]:
        """Return one immutable verified cache entry without owner-loop I/O."""

        with self._cache_lock:
            value = self._cached_device_contexts.get(device_id)
            if value is None:
                raise DeviceContextError("PEER_NOT_READY")
            return _freeze_public(value)

    async def refresh_device_context(self, device_id: str) -> Mapping[str, Any]:
        """Refresh Manifest/State on the current generation and return its cache."""

        self._require_owner()
        socket = self._socket_by_device.get(device_id)
        connection = None if socket is None else self._connections_by_socket.get(socket)
        if connection is None or not connection.binding.ready:
            raise DeviceContextError("PEER_NOT_READY")
        task = self._context_fetch_tasks.get(device_id)
        if task is None or task.done():
            self._begin_context_fetch(connection)
            task = self._context_fetch_tasks.get(device_id)
        if task is None:
            raise DeviceContextError("PEER_NOT_READY")
        try:
            await asyncio.wait_for(
                asyncio.shield(task), timeout=float(protocol.CONTROL_TIMEOUT_S)
            )
        except TimeoutError as error:
            raise DeviceContextError("DEADLINE_EXCEEDED") from error
        self._cache_diagnostic()
        return self._device_contexts.snapshot(device_id)

    async def _wait_for_task_connection(self, device_id: str) -> _Connection:
        """Wait for a verified reconnect; cancellation is the caller's deadline."""

        while not self._stopped:
            socket = self._socket_by_device.get(device_id)
            connection = (
                None if socket is None else self._connections_by_socket.get(socket)
            )
            if connection is not None and connection.binding.ready:
                return connection
            await self._sleep(0.25)
        raise AgentMessageError("RUNTIME_STOPPING")

    async def _perform_outbound_task_cancel(
        self,
        device_id: str,
        task_id: str,
    ) -> bool:
        """Perform one bounded A2A cancellation request."""

        dispatcher = self._task_dispatcher
        if dispatcher is None:
            return False
        try:
            mirrored = dispatcher.task_store.get_task(
                "received", device_id, task_id
            )
        except TaskStoreError:
            return False
        if (
            mirrored is not None
            and mirrored["status"]["state"] == "TASK_STATE_CANCELED"
        ):
            return True

        socket = self._socket_by_device.get(device_id)
        connection = (
            None if socket is None else self._connections_by_socket.get(socket)
        )
        if connection is None or not connection.binding.ready:
            return False
        try:
            response = await self._request_application(
                connection,
                "CancelTask",
                {"id": task_id},
                extensions=(),
                timeout=float(protocol.CONTROL_TIMEOUT_S),
            )
        except (asyncio.CancelledError, Exception):
            return False
        if response.error_reason is not None:
            return False
        try:
            task = validate_task(
                response.result,
                expected_task_id=task_id,
            )
        except A2AError:
            # A forged success is a binding violation, not a successful
            # cancellation acknowledgement.
            self._close_connection(connection.socket)
            return False
        if task["status"]["state"] != "TASK_STATE_CANCELED":
            return False
        try:
            # CancelTask returns the peer's authoritative terminal Task.  The
            # initiator must persist it as well; otherwise Ctrl+C stops the
            # remote work while its local mirror remains WORKING forever.
            dispatcher.task_store.put_task("received", device_id, task)
        except TaskStoreError:
            return False
        return True

    async def _cancel_outbound_task(
        self,
        device_id: str,
        task_id: str,
    ) -> bool:
        """Share one cancellation attempt across interrupt and shutdown paths."""

        key = (device_id, task_id)
        attempt = self._outbound_cancel_attempts.get(key)
        if attempt is not None and attempt.done():
            try:
                if attempt.result() is True:
                    return True
            except BaseException:
                pass
            if self._outbound_cancel_attempts.get(key) is attempt:
                self._outbound_cancel_attempts.pop(key, None)
            attempt = None
        if attempt is None:
            attempt = asyncio.create_task(
                self._perform_outbound_task_cancel(device_id, task_id)
            )
            self._outbound_cancel_attempts[key] = attempt
        try:
            confirmed = bool(await asyncio.shield(attempt))
        except asyncio.CancelledError:
            raise
        except BaseException:
            confirmed = False
        if not confirmed and self._outbound_cancel_attempts.get(key) is attempt:
            self._outbound_cancel_attempts.pop(key, None)
        return confirmed

    def _discard_tracked_outbound_task(
        self,
        device_id: str,
        task_id: str,
    ) -> None:
        tracked = self._outbound_task_ids.get(device_id)
        if tracked is not None:
            tracked.discard(task_id)
            if not tracked:
                self._outbound_task_ids.pop(device_id, None)
        key = (device_id, task_id)
        attempt = self._outbound_cancel_attempts.get(key)
        if attempt is None:
            return
        if attempt.done():
            self._outbound_cancel_attempts.pop(key, None)
            return

        def _release(done: asyncio.Task[bool]) -> None:
            current = self._outbound_cancel_attempts.get(key)
            still_tracked = task_id in self._outbound_task_ids.get(
                device_id, set()
            )
            if current is done and not still_tracked:
                self._outbound_cancel_attempts.pop(key, None)

        attempt.add_done_callback(_release)

    async def _cancel_tracked_outbound_tasks(self) -> None:
        pending = tuple(
            (device_id, task_id)
            for device_id, task_ids in self._outbound_task_ids.items()
            for task_id in tuple(task_ids)
        )
        if not pending:
            return
        results = await asyncio.gather(
            *(
                self._cancel_outbound_task(device_id, task_id)
                for device_id, task_id in pending
            ),
            return_exceptions=True,
        )
        for (device_id, task_id), result in zip(pending, results, strict=True):
            if result is True:
                self._discard_tracked_outbound_task(device_id, task_id)

    async def run_agent_task(
        self,
        device_id: str,
        text: str,
        *,
        context_id: str | None,
        message_id: str,
        event_sink: Callable[[Mapping[str, Any]], Any] | None = None,
    ) -> Mapping[str, Any]:
        """Run one persistent remote Task and mirror its Agent messages locally."""

        self._require_owner()
        try:
            normalized_message_id = protocol.canonical_uuid4(
                message_id, "messageId"
            )
            normalized_context_id = (
                None
                if context_id is None
                else protocol.canonical_uuid4(context_id, "contextId")
            )
        except protocol.ProtocolError as error:
            raise AgentMessageError("INVALID_PARAMS") from error
        params: dict[str, Any] = {
            "message": {
                "messageId": normalized_message_id,
                "role": "ROLE_USER",
                "parts": [{"text": text}],
            }
        }
        if normalized_context_id is not None:
            params["message"]["contextId"] = normalized_context_id
        dispatcher = self._task_dispatcher
        if dispatcher is None:
            raise AgentMessageError("PEER_NOT_READY")
        store = dispatcher.task_store
        task_id: str | None = None
        effective_context_id = normalized_context_id
        current_task: Mapping[str, Any] | None = None
        receipts: dict[str, Mapping[str, Any]] = {}
        final_text = ""
        active_stream: _ApplicationStreamWaiter | None = None
        peer_runtime_instance_id = ""
        connection_generation = 0
        retryable_transport_codes = frozenset(
            {
                "STALE_GENERATION",
                "PEER_NOT_READY",
                "DEADLINE_EXCEEDED",
                "WORKER_PROTOCOL_ERROR",
                "WORKER_RESTART_EXHAUSTED",
            }
        )

        def _is_final_response_artifact(artifact: Mapping[str, Any]) -> bool:
            metadata = artifact.get("metadata", {})
            return (
                isinstance(metadata, Mapping)
                and metadata.get("mclaw.artifactRole") == "final-response"
            ) or artifact.get("name") == "mclaw-result.txt"

        def _capture_final_text(artifact: Mapping[str, Any]) -> None:
            nonlocal final_text
            if not _is_final_response_artifact(artifact):
                return
            text_parts = [
                str(part["text"])
                for part in artifact.get("parts", ())
                if isinstance(part, Mapping) and isinstance(part.get("text"), str)
            ]
            if len(text_parts) != 1 or not text_parts[0]:
                raise AgentMessageError("INVALID_AGENT_RESPONSE")
            final_text = text_parts[0]

        def _error_code(error: BaseException) -> str:
            return str(getattr(error, "code", "INTERNAL_ERROR"))

        def _raise_task_error(error: BaseException) -> None:
            if isinstance(error, AgentMessageError):
                raise error
            code = _error_code(error)
            if code not in protocol.RPC_ERROR_CODES:
                code = "PEER_NOT_READY" if code.startswith("WORKER_") else "INTERNAL_ERROR"
            raise AgentMessageError(
                code,
                outcome_unknown=bool(
                    getattr(error, "outcome_unknown", False)
                ),
            ) from error

        def _persist_task(task: Mapping[str, Any]) -> None:
            nonlocal current_task, task_id, effective_context_id, final_text
            try:
                first_task = task_id is None
                normalized = validate_task(
                    task,
                    expected_task_id=task_id,
                    expected_context_id=effective_context_id,
                )
                task_id = str(normalized["id"])
                effective_context_id = str(normalized["contextId"])
                store.put_task("received", device_id, normalized)
                current_task = normalized
                if first_task:
                    self._outbound_task_ids.setdefault(device_id, set()).add(
                        task_id
                    )
                for artifact in normalized.get("artifacts", ()):
                    artifact_id = str(artifact["artifactId"])
                    if artifact_id not in receipts:
                        receipts[artifact_id] = store.persist_artifact(
                            "received",
                            device_id,
                            task_id,
                            artifact,
                        )
                    _capture_final_text(artifact)
            except (A2AError, TaskStoreError) as error:
                reason = str(getattr(error, "reason", "INVALID_AGENT_RESPONSE"))
                raise AgentMessageError(reason) from error

        async def _consume(response: ApplicationResponse) -> bool:
            nonlocal current_task, final_text
            if response.error_reason is not None:
                raise AgentMessageError(
                    response.error_reason,
                    outcome_unknown=response.outcome_unknown,
                )
            try:
                event = validate_stream_response(
                    response.result,
                    expected_task_id=task_id,
                    expected_context_id=effective_context_id,
                )
            except A2AError as error:
                raise AgentMessageError(error.reason) from error
            if "task" in event:
                _persist_task(event["task"])
            elif "statusUpdate" in event:
                if current_task is None or task_id is None:
                    raise AgentMessageError("INVALID_AGENT_RESPONSE")
                update = event["statusUpdate"]
                value = _plain(current_task)
                value["status"] = _plain(update["status"])
                if isinstance(update.get("metadata"), Mapping):
                    task_metadata = dict(value.get("metadata", {}))
                    task_metadata.update(_plain(update["metadata"]))
                    value["metadata"] = task_metadata
                _persist_task(value)
                message = update["status"].get("message")
                metadata = (
                    message.get("metadata", {})
                    if isinstance(message, Mapping)
                    else {}
                )
                descriptor = (
                    metadata.get("mclaw.agentEvent", {})
                    if isinstance(metadata, Mapping)
                    else {}
                )
                if (
                    event_sink is not None
                    and isinstance(message, Mapping)
                    and isinstance(descriptor, Mapping)
                    and descriptor.get("type") == "assistant.message"
                    and descriptor.get("contentSource")
                    in {"content", "reasoning_content"}
                    and isinstance(message.get("parts"), (tuple, list))
                    and len(message["parts"]) == 1
                    and isinstance(message["parts"][0].get("text"), str)
                ):
                    delivered = event_sink(
                        MappingProxyType(
                            {
                                "type": "assistant.message",
                                "content": message["parts"][0]["text"],
                                "content_source": descriptor["contentSource"],
                                "is_final": False,
                                "origin": "dsoftbus",
                                "remote_device_id": device_id,
                                "remote_task_id": task_id,
                            }
                        )
                    )
                    if hasattr(delivered, "__await__"):
                        await delivered
            elif "artifactUpdate" in event:
                if current_task is None or task_id is None:
                    raise AgentMessageError("INVALID_AGENT_RESPONSE")
                update = event["artifactUpdate"]
                if update.get("append") is True:
                    raise AgentMessageError("UNSUPPORTED_OPERATION")
                artifact = update["artifact"]
                try:
                    receipt = store.persist_artifact(
                        "received",
                        device_id,
                        task_id,
                        artifact,
                    )
                except TaskStoreError as error:
                    raise AgentMessageError("INVALID_AGENT_RESPONSE") from error
                receipts[str(artifact["artifactId"])] = receipt
                value = _plain(current_task)
                artifacts = [
                    item
                    for item in value.get("artifacts", [])
                    if item.get("artifactId") != artifact["artifactId"]
                ]
                artifacts.append(_plain(artifact))
                value["artifacts"] = artifacts
                _persist_task(value)
                _capture_final_text(artifact)
            else:
                raise AgentMessageError("INVALID_AGENT_RESPONSE")
            return response.stream_end

        def _terminal_result() -> Mapping[str, Any]:
            if current_task is None or task_id is None:
                raise AgentMessageError("INVALID_AGENT_RESPONSE")
            state = str(current_task["status"]["state"])
            if state != "TASK_STATE_COMPLETED":
                metadata = current_task.get("metadata", {})
                reason = (
                    metadata.get("mclaw.failureReason", "")
                    if isinstance(metadata, Mapping)
                    else ""
                )
                if not reason:
                    status_message = current_task["status"].get("message", {})
                    status_metadata = (
                        status_message.get("metadata", {})
                        if isinstance(status_message, Mapping)
                        else {}
                    )
                    reason = status_metadata.get("mclaw.failureReason", "")
                if reason not in protocol.RPC_ERROR_CODES:
                    reason = (
                        "AGENT_INTERRUPTED"
                        if state == "TASK_STATE_CANCELED"
                        else "PROVIDER_ERROR"
                    )
                raise AgentMessageError(str(reason))
            if not final_text:
                raise AgentMessageError("INVALID_AGENT_RESPONSE")
            return MappingProxyType(
                {
                    "success": True,
                    "device_id": device_id,
                    "context_id": effective_context_id,
                    "message_id": normalized_message_id,
                    "task_id": task_id,
                    "task_state": state,
                    "text": final_text,
                    "artifacts": tuple(receipts.values()),
                    "_mclawProvenance": MappingProxyType(
                        {
                            "kind": "peer",
                            "source": "mclaw.dsoftbus.runtime",
                            "peerDeviceId": device_id,
                            "peerRuntimeInstanceId": peer_runtime_instance_id,
                            "connectionGeneration": connection_generation,
                            "receivedVia": "softbus",
                            "verifiedBinding": True,
                        }
                    ),
                    "_untrustedRemoteData": True,
                }
            )

        method = "SendStreamingMessage"
        stream_params: Mapping[str, Any] = params
        try:
            while True:
                connection = await self._wait_for_task_connection(device_id)
                identity = connection.binding.peer_identity
                if identity is None or connection.binding.peer_card is None:
                    raise AgentMessageError("PEER_NOT_READY")
                peer_runtime_instance_id = identity.runtime_instance_id
                connection_generation = connection.generation
                if method == "GetTask":
                    assert task_id is not None
                    try:
                        response = await self._request_application(
                            connection,
                            "GetTask",
                            {"id": task_id},
                            extensions=(),
                            timeout=float(protocol.CONTROL_TIMEOUT_S),
                        )
                        if response.error_reason is not None:
                            raise AgentMessageError(
                                response.error_reason,
                                outcome_unknown=response.outcome_unknown,
                            )
                        _persist_task(
                            validate_task(
                                response.result,
                                expected_task_id=task_id,
                                expected_context_id=effective_context_id,
                            )
                        )
                        if task_state_is_terminal(
                            current_task["status"]["state"]  # type: ignore[index]
                        ):
                            return _terminal_result()
                        method = "SubscribeToTask"
                        stream_params = {"id": task_id}
                    except (
                        AgentMessageError,
                        DeviceContextError,
                        SoftBusBindingError,
                        WorkerSupervisorError,
                    ) as error:
                        code = _error_code(error)
                        if code not in retryable_transport_codes and not (
                            code == "CAPACITY_BUSY" and task_id is not None
                        ):
                            _raise_task_error(error)
                        await self._sleep(0.25)
                        continue
                try:
                    active_stream, response = await self._open_application_stream(
                        connection,
                        method,
                        stream_params,
                    )
                    while True:
                        ended = await _consume(response)
                        if ended:
                            return _terminal_result()
                        response = await self._next_application_stream(active_stream)
                except asyncio.CancelledError:
                    raise
                except (
                    AgentMessageError,
                    DeviceContextError,
                    SoftBusBindingError,
                    WorkerSupervisorError,
                ) as error:
                    code = _error_code(error)
                    if code not in retryable_transport_codes and not (
                        code == "CAPACITY_BUSY" and task_id is not None
                    ):
                        _raise_task_error(error)
                    method = "GetTask" if task_id is not None else "SendStreamingMessage"
                    stream_params = {"id": task_id} if task_id is not None else params
                    await self._sleep(0.25)
                finally:
                    if active_stream is not None:
                        self._detach_application_stream(active_stream)
                        active_stream = None
        except asyncio.CancelledError:
            if task_id is not None:
                try:
                    await asyncio.shield(
                        self._cancel_outbound_task(device_id, task_id)
                    )
                except BaseException:
                    pass
            raise
        finally:
            if task_id is not None:
                self._discard_tracked_outbound_task(device_id, task_id)

    def publication_snapshot(self) -> Mapping[str, Any]:
        with self._cache_lock:
            counts = dict(self._publication_counts)
        return MappingProxyType(
            {
                **counts,
                "manifestPhaseBComplete": self._publications is not None,
                "stateEpochFrozen": self._publications is not None,
            }
        )


__all__ = ["DiscoveryOwnerResources"]
