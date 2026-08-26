# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Strict owner-loop runtime health model and persistent publisher."""

from __future__ import annotations

from datetime import datetime, timezone
import re
import threading
from types import MappingProxyType
from typing import Any, Callable, Mapping, NoReturn
import uuid

from .endpoint_lock import DsoftbusEndpointLock, EndpointLockError
from .protocol import (
    DISPATCH_QUEUE_BYTES_MAX,
    DISPATCH_QUEUE_MAX,
    GLOBAL_SEND_QUEUE_BYTES_MAX,
    GLOBAL_SEND_QUEUE_MAX,
    IDEMPOTENCY_WAITER_BYTES_MAX,
    IDEMPOTENCY_WAITER_MAX,
    MAX_OPEN_PEERS,
    NATIVE_EVENT_BYTES_MAX,
    NATIVE_EVENT_CAP,
    PARENT_COMMAND_QUEUE_BYTES_MAX,
    PARENT_COMMAND_QUEUE_MAX,
    PARENT_EVENT_BYTES_MAX,
    PARENT_EVENT_CAP,
    PARENT_RESPONSE_ROUTE_BYTES_MAX,
    PARENT_RESPONSE_ROUTE_MAX,
    PEER_REGISTRY_MAX,
    REMOTE_CONTEXT_BYTES_MAX,
    REMOTE_CONTEXT_MAX,
    RESOURCE_RUNTIME_HEALTH_BYTES_MAX,
    RESPONSE_CACHE_BYTES_MAX,
    RESPONSE_CACHE_CAP,
    RPC_ERROR_CODES,
    SOCKET_CONTROL_SEND_BYTES_MAX,
    SOCKET_CONTROL_SEND_MAX,
    TRANSIENT_SOCKET_CAP,
    WORKER_EVENT_BYTES_MAX,
    WORKER_EVENT_CAP,
    ProtocolError,
    canonical_json_bytes,
    canonical_uuid4,
    strict_json_loads,
)


RUNTIME_HEALTH_SCHEMA = "mclaw.dsoftbus.runtime-health/v1"
RUNTIME_HEALTH_STATES = frozenset(
    {"STARTING", "READY", "DEGRADED", "STOPPING", "STOPPED"}
)
DEGRADED_REASON_PRIORITY = (
    "PRODUCT_INTEGRATION_UNVERIFIED",
    "MAIN_ABI_CONTAMINATED",
    "WORKER_START_FAILED",
    "WORKER_PROTOCOL_ERROR",
    "WORKER_RESTART_EXHAUSTED",
    "NODE_SNAPSHOT_OVERFLOW",
    "LISTENER_START_FAILED",
    "HEALTH_INVARIANT_VIOLATION",
)
PROVIDER_READINESS_CODES = frozenset(
    {
        "",
        "PROVIDER_MISSING",
        "TRANSPORT_FENCE_UNSUPPORTED",
        "PROVIDER_SYNC_FAILED",
    }
)
_UUID_EPOCH = re.compile(r"^[0-9a-f]{12}$")
_UTC_SECONDS = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
_MAX_COUNTER = 2**63 - 1
_MAX_UINT32 = 2**32 - 1

RUNTIME_HEALTH_KEYS = (
    "schemaVersion",
    "generatedAt",
    "snapshotSequence",
    "runtimeInstanceId",
    "mainPid",
    "mainStartTimeTicks",
    "workerPid",
    "workerStartTimeTicks",
    "state",
    "primaryErrorCode",
    "degradedReasons",
    "workerAlive",
    "workerEpoch",
    "restartCount",
    "listenerReady",
    "listenerSocketCount",
    "openSocketCount",
    "retainedPeerSocketCount",
    "transientSocketCount",
    "socketCap",
    "effectivePeerCap",
    "connectedPeerCount",
    "peerCount",
    "peerRegistryCap",
    "peerRegistryDropped",
    "readyPeerCount",
    "stateFreshPeerCount",
    "remoteInferenceEnabled",
    "providerReady",
    "providerReadinessCode",
    "productIntegrationVerified",
    "remoteAccepted",
    "remoteRejected",
    "remoteRejectedByCode",
    "remoteRateLimited",
    "remoteBudgetUsed",
    "remoteBudgetLimit",
    "nativeEventDepth",
    "nativeEventBytes",
    "workerEventDepth",
    "workerEventBytes",
    "parentEventDepth",
    "parentEventBytes",
    "parentCommandQueueCount",
    "parentCommandQueueBytes",
    "parentResponseRouteCount",
    "parentResponseRouteBytes",
    "sendBusinessQueueCount",
    "sendBusinessQueueBytes",
    "sendControlQueueCount",
    "sendControlQueueBytes",
    "idempotencyWaiterCount",
    "idempotencyWaiterBytes",
    "dispatchQueueCount",
    "dispatchQueueBytes",
    "agentSessionTaskCount",
    "agentIngressReservationCount",
    "agentPendingCount",
    "remoteContextCount",
    "remoteContextBytes",
    "responseCacheCount",
    "responseCacheBytes",
    "stateReadRateLimited",
    "sendQueueOverflowCount",
    "eventOverflowCount",
)
_KEY_SET = frozenset(RUNTIME_HEALTH_KEYS)

_BOOLEAN_FIELDS = frozenset(
    {
        "workerAlive",
        "listenerReady",
        "remoteInferenceEnabled",
        "providerReady",
        "productIntegrationVerified",
    }
)
_OPTIONAL_POSITIVE_FIELDS = frozenset(
    {"workerPid", "workerStartTimeTicks"}
)
_STRING_FIELDS = frozenset(
    {
        "schemaVersion",
        "generatedAt",
        "runtimeInstanceId",
        "state",
        "primaryErrorCode",
        "providerReadinessCode",
    }
)
_IMMUTABLE_UPDATE_FIELDS = frozenset(
    {
        "schemaVersion",
        "generatedAt",
        "snapshotSequence",
        "runtimeInstanceId",
        "mainPid",
        "mainStartTimeTicks",
        "socketCap",
        "effectivePeerCap",
        "peerRegistryCap",
        "remoteInferenceEnabled",
        "remoteBudgetLimit",
        "state",
        "primaryErrorCode",
        "degradedReasons",
    }
)
_CAPS = MappingProxyType(
    {
        "nativeEventDepth": NATIVE_EVENT_CAP,
        "nativeEventBytes": NATIVE_EVENT_BYTES_MAX,
        "workerEventDepth": WORKER_EVENT_CAP,
        "workerEventBytes": WORKER_EVENT_BYTES_MAX,
        "parentEventDepth": PARENT_EVENT_CAP,
        "parentEventBytes": PARENT_EVENT_BYTES_MAX,
        "parentCommandQueueCount": PARENT_COMMAND_QUEUE_MAX,
        "parentCommandQueueBytes": PARENT_COMMAND_QUEUE_BYTES_MAX,
        "parentResponseRouteCount": PARENT_RESPONSE_ROUTE_MAX,
        "parentResponseRouteBytes": PARENT_RESPONSE_ROUTE_BYTES_MAX,
        "sendBusinessQueueCount": GLOBAL_SEND_QUEUE_MAX,
        "sendBusinessQueueBytes": GLOBAL_SEND_QUEUE_BYTES_MAX,
        "idempotencyWaiterCount": IDEMPOTENCY_WAITER_MAX,
        "idempotencyWaiterBytes": IDEMPOTENCY_WAITER_BYTES_MAX,
        "dispatchQueueCount": DISPATCH_QUEUE_MAX,
        "dispatchQueueBytes": DISPATCH_QUEUE_BYTES_MAX,
        "remoteContextCount": REMOTE_CONTEXT_MAX,
        "remoteContextBytes": REMOTE_CONTEXT_BYTES_MAX,
        "responseCacheCount": RESPONSE_CACHE_CAP,
        "responseCacheBytes": RESPONSE_CACHE_BYTES_MAX,
    }
)
_WORKER_DEAD_ZERO_FIELDS = frozenset(
    {
        "listenerSocketCount",
        "openSocketCount",
        "retainedPeerSocketCount",
        "transientSocketCount",
        "connectedPeerCount",
        "readyPeerCount",
        "stateFreshPeerCount",
    }
)
_STOPPED_ZERO_FIELDS = frozenset(
    {
        *_WORKER_DEAD_ZERO_FIELDS,
        "peerCount",
        "nativeEventDepth",
        "nativeEventBytes",
        "workerEventDepth",
        "workerEventBytes",
        "parentEventDepth",
        "parentEventBytes",
        "parentCommandQueueCount",
        "parentCommandQueueBytes",
        "parentResponseRouteCount",
        "parentResponseRouteBytes",
        "sendBusinessQueueCount",
        "sendBusinessQueueBytes",
        "sendControlQueueCount",
        "sendControlQueueBytes",
        "idempotencyWaiterCount",
        "idempotencyWaiterBytes",
        "dispatchQueueCount",
        "dispatchQueueBytes",
        "agentSessionTaskCount",
        "agentIngressReservationCount",
        "agentPendingCount",
        "remoteContextCount",
        "remoteContextBytes",
        "responseCacheCount",
        "responseCacheBytes",
    }
)


class RuntimeHealthError(RuntimeError):
    """Stable health validation or publication failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _fail(code: str, cause: BaseException | None = None) -> NoReturn:
    error = RuntimeHealthError(code)
    if cause is None:
        raise error
    raise error from cause


def _strict_integer(value: Any, *, positive: bool = False, maximum: int = _MAX_COUNTER) -> int:
    minimum = 1 if positive else 0
    if type(value) is not int or not minimum <= value <= maximum:
        _fail("RUNTIME_HEALTH_INVALID")
    return value


def _utc_now(now: Callable[[], datetime]) -> str:
    value = now()
    if not isinstance(value, datetime) or value.tzinfo is None:
        _fail("RUNTIME_HEALTH_CLOCK_INVALID")
    normalized = value.astimezone(timezone.utc).replace(microsecond=0)
    return normalized.isoformat().replace("+00:00", "Z")


def _validate_utc(value: Any) -> None:
    if not isinstance(value, str) or _UTC_SECONDS.fullmatch(value) is None:
        _fail("RUNTIME_HEALTH_INVALID")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as error:
        _fail("RUNTIME_HEALTH_INVALID", error)
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        _fail("RUNTIME_HEALTH_INVALID")


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def worker_epoch_digest(private_epoch: uuid.UUID | str) -> str:
    import hashlib

    try:
        parsed = private_epoch if isinstance(private_epoch, uuid.UUID) else uuid.UUID(private_epoch)
    except (ValueError, AttributeError) as error:
        _fail("WORKER_EPOCH_INVALID", error)
    if parsed.version != 4 or str(parsed) != str(private_epoch):
        _fail("WORKER_EPOCH_INVALID")
    return hashlib.sha256(parsed.bytes).hexdigest()[:12]


def validate_runtime_health(value: Mapping[str, Any]) -> None:
    """Validate the complete persistent health object and all invariants."""
    if not isinstance(value, Mapping) or frozenset(value) != _KEY_SET:
        _fail("RUNTIME_HEALTH_INVALID")
    if value["schemaVersion"] != RUNTIME_HEALTH_SCHEMA:
        _fail("RUNTIME_HEALTH_INVALID")
    _validate_utc(value["generatedAt"])
    _strict_integer(value["snapshotSequence"], positive=True)
    try:
        canonical_uuid4(value["runtimeInstanceId"], "runtimeInstanceId")
    except ProtocolError as error:
        _fail("RUNTIME_HEALTH_INVALID", error)
    _strict_integer(value["mainPid"], positive=True, maximum=2**31 - 1)
    _strict_integer(value["mainStartTimeTicks"], positive=True)

    for field in _BOOLEAN_FIELDS:
        if type(value[field]) is not bool:
            _fail("RUNTIME_HEALTH_INVALID")
    for field in _STRING_FIELDS:
        if not isinstance(value[field], str):
            _fail("RUNTIME_HEALTH_INVALID")

    worker_pid = value["workerPid"]
    worker_ticks = value["workerStartTimeTicks"]
    if (worker_pid is None) != (worker_ticks is None):
        _fail("RUNTIME_HEALTH_INVALID")
    if worker_pid is not None:
        _strict_integer(worker_pid, positive=True, maximum=2**31 - 1)
        _strict_integer(worker_ticks, positive=True)
        if worker_pid == value["mainPid"]:
            _fail("RUNTIME_HEALTH_INVALID")
    epoch = value["workerEpoch"]
    if epoch is not None and (
        not isinstance(epoch, str) or _UUID_EPOCH.fullmatch(epoch) is None
    ):
        _fail("RUNTIME_HEALTH_INVALID")
    if (epoch is None) != (worker_pid is None):
        _fail("RUNTIME_HEALTH_INVALID")

    reasons = value["degradedReasons"]
    if not isinstance(reasons, (list, tuple)):
        _fail("RUNTIME_HEALTH_INVALID")
    normalized_reasons = tuple(reasons)
    if (
        any(not isinstance(reason, str) for reason in normalized_reasons)
        or len(normalized_reasons) != len(set(normalized_reasons))
        or tuple(
            reason
            for reason in DEGRADED_REASON_PRIORITY
            if reason in normalized_reasons
        )
        != normalized_reasons
    ):
        _fail("RUNTIME_HEALTH_INVALID")
    state = value["state"]
    if state not in RUNTIME_HEALTH_STATES:
        _fail("RUNTIME_HEALTH_INVALID")
    if state == "DEGRADED":
        if not normalized_reasons or value["primaryErrorCode"] != normalized_reasons[0]:
            _fail("RUNTIME_HEALTH_INVALID")
    elif normalized_reasons or value["primaryErrorCode"] != "":
        _fail("RUNTIME_HEALTH_INVALID")

    provider_code = value["providerReadinessCode"]
    if provider_code not in PROVIDER_READINESS_CODES:
        _fail("RUNTIME_HEALTH_INVALID")
    if value["providerReady"] != (provider_code == ""):
        _fail("RUNTIME_HEALTH_INVALID")

    rejected = value["remoteRejectedByCode"]
    if not isinstance(rejected, Mapping):
        _fail("RUNTIME_HEALTH_INVALID")
    rejected_sum = 0
    for code, count in rejected.items():
        if code not in RPC_ERROR_CODES:
            _fail("RUNTIME_HEALTH_INVALID")
        rejected_sum += _strict_integer(count)
        if rejected_sum > _MAX_COUNTER:
            _fail("RUNTIME_HEALTH_INVALID")

    excluded = {
        *_STRING_FIELDS,
        *_BOOLEAN_FIELDS,
        *_OPTIONAL_POSITIVE_FIELDS,
        "workerEpoch",
        "degradedReasons",
        "remoteRejectedByCode",
        "remoteBudgetLimit",
    }
    for field in _KEY_SET - excluded:
        _strict_integer(value[field])
    _strict_integer(value["socketCap"], positive=True, maximum=_MAX_UINT32)
    _strict_integer(value["effectivePeerCap"], positive=True, maximum=_MAX_UINT32)
    if value["socketCap"] < 1 + TRANSIENT_SOCKET_CAP + 1:
        _fail("RUNTIME_HEALTH_INVALID")
    if value["effectivePeerCap"] != min(
        MAX_OPEN_PEERS, value["socketCap"] - 1 - TRANSIENT_SOCKET_CAP
    ):
        _fail("RUNTIME_HEALTH_INVALID")
    if value["peerRegistryCap"] != PEER_REGISTRY_MAX:
        _fail("RUNTIME_HEALTH_INVALID")
    if value["listenerSocketCount"] not in {0, 1}:
        _fail("RUNTIME_HEALTH_INVALID")
    if value["openSocketCount"] != (
        value["listenerSocketCount"]
        + value["retainedPeerSocketCount"]
        + value["transientSocketCount"]
    ):
        _fail("RUNTIME_HEALTH_INVALID")
    if value["transientSocketCount"] > TRANSIENT_SOCKET_CAP:
        _fail("RUNTIME_HEALTH_INVALID")
    if not (
        value["connectedPeerCount"]
        <= value["retainedPeerSocketCount"]
        <= value["effectivePeerCap"]
    ):
        _fail("RUNTIME_HEALTH_INVALID")
    if not (
        value["stateFreshPeerCount"]
        <= value["readyPeerCount"]
        <= value["connectedPeerCount"]
        <= value["peerCount"]
        <= value["peerRegistryCap"]
    ):
        _fail("RUNTIME_HEALTH_INVALID")

    if value["workerAlive"]:
        if worker_pid is None or epoch is None:
            _fail("RUNTIME_HEALTH_INVALID")
    else:
        if value["listenerReady"] or any(value[field] != 0 for field in _WORKER_DEAD_ZERO_FIELDS):
            _fail("RUNTIME_HEALTH_INVALID")
    if value["listenerReady"] and (
        not value["workerAlive"] or value["listenerSocketCount"] != 1
    ):
        _fail("RUNTIME_HEALTH_INVALID")
    if state == "READY" and (
        not value["productIntegrationVerified"]
        or not value["workerAlive"]
        or not value["listenerReady"]
        or value["listenerSocketCount"] != 1
    ):
        _fail("RUNTIME_HEALTH_INVALID")

    remote_budget_limit = value["remoteBudgetLimit"]
    if remote_budget_limit is not None:
        if (
            type(remote_budget_limit) is not int
            or not 1_000 <= remote_budget_limit <= 10_000_000
            or value["remoteBudgetUsed"] > remote_budget_limit
        ):
            _fail("RUNTIME_HEALTH_INVALID")
    if value["remoteRejected"] != rejected_sum:
        _fail("RUNTIME_HEALTH_INVALID")
    if value["remoteRateLimited"] != rejected.get("RATE_LIMITED", 0):
        _fail("RUNTIME_HEALTH_INVALID")
    if value["remoteRateLimited"] > value["remoteRejected"]:
        _fail("RUNTIME_HEALTH_INVALID")

    for field, cap in _CAPS.items():
        if value[field] > cap:
            _fail("RUNTIME_HEALTH_INVALID")
    if value["sendControlQueueCount"] > (
        value["retainedPeerSocketCount"] * SOCKET_CONTROL_SEND_MAX
    ):
        _fail("RUNTIME_HEALTH_INVALID")
    if value["sendControlQueueBytes"] > (
        value["retainedPeerSocketCount"] * SOCKET_CONTROL_SEND_BYTES_MAX
    ):
        _fail("RUNTIME_HEALTH_INVALID")
    if (
        value["agentIngressReservationCount"] != 0
        or value["agentPendingCount"] != 0
        or value["agentSessionTaskCount"] > 1
    ):
        _fail("RUNTIME_HEALTH_INVALID")
    if state == "STOPPED":
        if value["workerAlive"] or value["listenerReady"]:
            _fail("RUNTIME_HEALTH_INVALID")
        if any(value[field] != 0 for field in _STOPPED_ZERO_FIELDS):
            _fail("RUNTIME_HEALTH_INVALID")


def parse_runtime_health(raw: bytes) -> Mapping[str, Any]:
    """Strictly parse canonical persistent health bytes."""
    try:
        value = strict_json_loads(
            raw,
            max_bytes=RESOURCE_RUNTIME_HEALTH_BYTES_MAX,
            require_canonical=True,
            require_object=True,
        )
    except ProtocolError as error:
        _fail("RUNTIME_HEALTH_INVALID", error)
    validate_runtime_health(value)
    return _freeze(value)


def _initial_values(
    *,
    runtime_instance_id: str,
    main_pid: int,
    main_start_time_ticks: int,
    socket_cap: int,
    remote_inference_enabled: bool,
    remote_budget_limit: int | None,
    provider_ready: bool,
    provider_readiness_code: str,
) -> dict[str, Any]:
    effective_peer_cap = min(MAX_OPEN_PEERS, socket_cap - 1 - TRANSIENT_SOCKET_CAP)
    values: dict[str, Any] = {
        key: 0 for key in RUNTIME_HEALTH_KEYS
    }
    values.update(
        {
            "schemaVersion": RUNTIME_HEALTH_SCHEMA,
            "generatedAt": "",
            "snapshotSequence": 0,
            "runtimeInstanceId": runtime_instance_id,
            "mainPid": main_pid,
            "mainStartTimeTicks": main_start_time_ticks,
            "workerPid": None,
            "workerStartTimeTicks": None,
            "state": "STARTING",
            "primaryErrorCode": "",
            "degradedReasons": [],
            "workerAlive": False,
            "workerEpoch": None,
            "listenerReady": False,
            "socketCap": socket_cap,
            "effectivePeerCap": effective_peer_cap,
            "peerRegistryCap": PEER_REGISTRY_MAX,
            "remoteInferenceEnabled": remote_inference_enabled,
            "providerReady": provider_ready,
            "providerReadinessCode": provider_readiness_code,
            "productIntegrationVerified": False,
            "remoteRejectedByCode": {},
            "remoteBudgetLimit": remote_budget_limit,
        }
    )
    return values


class RuntimeHealthPublisher:
    """Owner-thread-only sequence model and atomic persistent producer."""

    def __init__(
        self,
        *,
        endpoint: DsoftbusEndpointLock,
        runtime_instance_id: str,
        config: Mapping[str, Any],
        socket_cap: int,
        provider_ready: bool,
        provider_readiness_code: str,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        uuid_factory: Callable[[], uuid.UUID] = uuid.uuid4,
    ) -> None:
        if threading.current_thread() is threading.main_thread():
            # Main-thread use is never valid in the product.  Tests that need a
            # publisher run it in an explicit owner thread as production does.
            _fail("HEALTH_OWNER_THREAD_REQUIRED")
        if not isinstance(config, Mapping) or not isinstance(config.get("dsoftbus"), Mapping):
            _fail("RUNTIME_HEALTH_CONFIG_INVALID")
        dsoftbus = config["dsoftbus"]
        remote_enabled = dsoftbus.get("accept_remote_messages")
        remote_budget = dsoftbus.get("remote_token_budget_per_hour")
        if type(remote_enabled) is not bool:
            _fail("RUNTIME_HEALTH_CONFIG_INVALID")
        if remote_budget is not None:
            _strict_integer(remote_budget, positive=True)
        _strict_integer(socket_cap, positive=True, maximum=_MAX_UINT32)
        if socket_cap < 1 + TRANSIENT_SOCKET_CAP + 1:
            _fail("RUNTIME_HEALTH_CONFIG_INVALID")
        if (
            type(provider_ready) is not bool
            or not isinstance(provider_readiness_code, str)
            or provider_readiness_code not in PROVIDER_READINESS_CODES
            or provider_ready != (provider_readiness_code == "")
        ):
            _fail("RUNTIME_HEALTH_CONFIG_INVALID")
        holder = endpoint.holder
        if holder["runtimeInstanceId"] != runtime_instance_id:
            _fail("ENDPOINT_LOCK_OWNERSHIP_LOST")

        self._owner_thread_id = threading.get_ident()
        self._endpoint = endpoint
        self._runtime_instance_id = runtime_instance_id
        self._now = now
        self._uuid_factory = uuid_factory
        self._values = _initial_values(
            runtime_instance_id=runtime_instance_id,
            main_pid=holder["pid"],
            main_start_time_ticks=holder["startTimeTicks"],
            socket_cap=socket_cap,
            remote_inference_enabled=remote_enabled,
            remote_budget_limit=remote_budget,
            provider_ready=provider_ready,
            provider_readiness_code=provider_readiness_code,
        )
        self._snapshot: Mapping[str, Any] | None = None
        self._broken = False

    @property
    def owner_thread_id(self) -> int:
        return self._owner_thread_id

    def _require_owner(self) -> None:
        if threading.get_ident() != self._owner_thread_id:
            _fail("HEALTH_OWNER_THREAD_REQUIRED")
        if self._broken:
            _fail("HEALTH_PUBLISH_DISABLED")

    def publish(
        self,
        state: str,
        *,
        degraded_reasons: tuple[str, ...] = (),
        updates: Mapping[str, Any] | None = None,
        provider_ready: bool | None = None,
        provider_readiness_code: str | None = None,
    ) -> Mapping[str, Any]:
        """Validate, atomically publish, then commit one immutable snapshot."""
        self._require_owner()
        candidate = dict(self._values)
        if updates is not None:
            if not isinstance(updates, Mapping) or any(
                key not in _KEY_SET or key in _IMMUTABLE_UPDATE_FIELDS
                for key in updates
            ):
                _fail("RUNTIME_HEALTH_UPDATE_INVALID")
            for key, value in updates.items():
                candidate[key] = dict(value) if key == "remoteRejectedByCode" else value
        if (provider_ready is None) != (provider_readiness_code is None):
            _fail("RUNTIME_HEALTH_UPDATE_INVALID")
        if provider_ready is not None:
            candidate["providerReady"] = provider_ready
            candidate["providerReadinessCode"] = provider_readiness_code
        candidate["state"] = state
        candidate["degradedReasons"] = list(degraded_reasons)
        candidate["primaryErrorCode"] = degraded_reasons[0] if degraded_reasons else ""
        if state == "STOPPED":
            candidate["workerAlive"] = False
            candidate["listenerReady"] = False
            for field in _STOPPED_ZERO_FIELDS:
                candidate[field] = 0
        sequence = candidate["snapshotSequence"] + 1
        if sequence > _MAX_COUNTER:
            _fail("RUNTIME_HEALTH_SEQUENCE_EXHAUSTED")
        candidate["snapshotSequence"] = sequence
        candidate["generatedAt"] = _utc_now(self._now)
        validate_runtime_health(candidate)
        raw = canonical_json_bytes(candidate)
        if len(raw) > RESOURCE_RUNTIME_HEALTH_BYTES_MAX:
            _fail("RUNTIME_HEALTH_BYTES_INVALID")
        token = self._uuid_factory()
        if not isinstance(token, uuid.UUID) or token.version != 4:
            _fail("RUNTIME_HEALTH_TEMP_NAME_INVALID")
        try:
            self._endpoint.publish_owned_runtime_health(
                raw, temp_name=f".runtime-health.{token.hex}.tmp"
            )
        except EndpointLockError as error:
            self._broken = True
            _fail("HEALTH_PUBLISH_FAILED", error)
        self._values = candidate
        self._snapshot = _freeze(candidate)
        return self.snapshot()

    def snapshot(self) -> Mapping[str, Any]:
        self._require_owner()
        if self._snapshot is None:
            _fail("RUNTIME_HEALTH_NOT_PUBLISHED")
        return _freeze(dict(self._snapshot))


__all__ = [
    "DEGRADED_REASON_PRIORITY",
    "PROVIDER_READINESS_CODES",
    "RUNTIME_HEALTH_KEYS",
    "RUNTIME_HEALTH_SCHEMA",
    "RUNTIME_HEALTH_STATES",
    "RuntimeHealthError",
    "RuntimeHealthPublisher",
    "parse_runtime_health",
    "validate_runtime_health",
    "worker_epoch_digest",
]
