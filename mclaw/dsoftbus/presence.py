# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Bounded in-memory device presence and connection planning.

This adapter deliberately owns no SoftBus handle.  It lets the Runtime prove
snapshot/event reconciliation, public identity derivation, generation fences,
and deterministic connection admission before the Native ``start`` operation
is opened by the Manifest freeze stage.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from types import MappingProxyType
from typing import Any, Iterable, Mapping, NoReturn, Sequence

from . import protocol


# This byte string is a deployed identity hash-domain constant.  Its spelling
# is part of public device identity compatibility, not a development iteration.
_DEVICE_ID_DOMAIN = b"mclaw-device-v1\0"
_DEVICE_ID_RE = re.compile(r"^urn:mclaw:device:oh:[0-9a-f]{64}$")
_MAX_COUNTER = 2**63 - 1


class PresenceError(RuntimeError):
    """Stable presence/reconciliation failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _fail(code: str) -> NoReturn:
    raise PresenceError(code)


def _bounded_text(value: Any, label: str, minimum: int, maximum: int) -> str:
    try:
        return protocol.bounded_utf8(value, label, minimum, maximum)
    except protocol.ProtocolError as error:
        raise PresenceError("PRESENCE_INPUT_INVALID") from error


def _bounded_int(value: Any, label: str, minimum: int, maximum: int) -> int:
    try:
        return protocol.bounded_integer(value, label, minimum, maximum)
    except protocol.ProtocolError as error:
        raise PresenceError("PRESENCE_INPUT_INVALID") from error


def derive_public_device_id(udid: str) -> str:
    """Derive the stable public pseudonym without exposing the raw UDID."""

    normalized = _bounded_text(udid, "udid", 1, 64)
    digest = hashlib.sha256(_DEVICE_ID_DOMAIN + normalized.encode("utf-8")).hexdigest()
    return f"urn:mclaw:device:oh:{digest}"


def _validate_public_device_id(value: Any) -> str:
    if not isinstance(value, str) or _DEVICE_ID_RE.fullmatch(value) is None:
        _fail("PRESENCE_INPUT_INVALID")
    return value


@dataclass(frozen=True, slots=True)
class DiscoveredNode:
    """Sensitive in-memory discovery input; never returned by the public view."""

    network_id: str
    udid: str
    device_name: str
    device_type_id: int

    @classmethod
    def from_value(cls, value: DiscoveredNode | Mapping[str, Any]) -> DiscoveredNode:
        if isinstance(value, cls):
            candidate = value
        elif isinstance(value, Mapping) and frozenset(value) == frozenset(
            {"deviceName", "deviceTypeId", "networkId", "udid"}
        ):
            candidate = cls(
                network_id=value["networkId"],
                udid=value["udid"],
                device_name=value["deviceName"],
                device_type_id=value["deviceTypeId"],
            )
        else:
            _fail("PRESENCE_INPUT_INVALID")
        return cls(
            network_id=_bounded_text(candidate.network_id, "networkId", 1, 64),
            udid=_bounded_text(candidate.udid, "udid", 1, 64),
            device_name=_bounded_text(candidate.device_name, "deviceName", 0, 127),
            device_type_id=_bounded_int(
                candidate.device_type_id, "deviceTypeId", 0, 2**16 - 1
            ),
        )


@dataclass(frozen=True, slots=True)
class PresenceTransition:
    admitted: bool
    device_id: str
    generation: int | None
    presence: str


@dataclass(frozen=True, slots=True)
class PresenceConnectionCandidate:
    """Owner-private connection input; never included in public snapshots."""

    device_id: str
    network_id: str
    generation: int
    action: str
    admitted: bool


@dataclass(frozen=True, slots=True)
class _PeerRecord:
    device_id: str
    network_id: str
    udid: str
    device_name: str
    device_type_id: int
    generation: int


class InMemoryPresenceAdapter:
    """Owner-confined registry for at most 64 currently online peers."""

    def __init__(self, *, local_device_id: str, socket_cap: int) -> None:
        self._local_device_id = _validate_public_device_id(local_device_id)
        if type(socket_cap) is not int or not 4 <= socket_cap <= 2**32 - 1:
            _fail("PRESENCE_INPUT_INVALID")
        self._socket_cap = socket_cap
        self._effective_peer_cap = min(
            protocol.MAX_OPEN_PEERS,
            socket_cap - 1 - protocol.TRANSIENT_SOCKET_CAP,
        )
        self._records: dict[str, _PeerRecord] = {}
        self._generation = 0
        self._last_node_sequence = 0
        self._worker_epoch: str | None = None
        self._dropped = 0
        self._accepting = True

    @property
    def local_device_id(self) -> str:
        return self._local_device_id

    @property
    def effective_peer_cap(self) -> int:
        return self._effective_peer_cap

    def _next_generation(self) -> int:
        if self._generation >= _MAX_COUNTER:
            _fail("PRESENCE_GENERATION_EXHAUSTED")
        self._generation += 1
        return self._generation

    def _record_drop(self, count: int) -> None:
        if type(count) is not int or count < 0 or self._dropped > _MAX_COUNTER - count:
            _fail("PRESENCE_COUNTER_EXHAUSTED")
        self._dropped += count

    def reset_worker_epoch(self, worker_epoch: str) -> int:
        """Invalidate all prior discovery state for a newly verified Worker epoch."""

        try:
            normalized = protocol.canonical_uuid4(worker_epoch, "workerEpoch")
        except protocol.ProtocolError as error:
            raise PresenceError("PRESENCE_INPUT_INVALID") from error
        removed = len(self._records)
        self._records.clear()
        self._last_node_sequence = 0
        self._worker_epoch = normalized
        self._accepting = True
        return removed

    def _require_epoch(self) -> None:
        if self._worker_epoch is None:
            _fail("PRESENCE_EPOCH_REQUIRED")
        if not self._accepting:
            _fail("RUNTIME_STOPPING")

    def _normalize_nodes(
        self, values: Iterable[DiscoveredNode | Mapping[str, Any]]
    ) -> list[tuple[str, DiscoveredNode]]:
        if isinstance(values, (str, bytes, bytearray, Mapping)):
            _fail("PRESENCE_INPUT_INVALID")
        items: list[DiscoveredNode | Mapping[str, Any]] = []
        try:
            for index, value in enumerate(values):
                if index >= protocol.NODE_SNAPSHOT_MAX:
                    _fail("NODE_SNAPSHOT_OVERFLOW")
                items.append(value)
        except TypeError as error:
            raise PresenceError("PRESENCE_INPUT_INVALID") from error
        normalized: list[tuple[str, DiscoveredNode]] = []
        device_ids: set[str] = set()
        network_ids: set[str] = set()
        for value in items:
            node = DiscoveredNode.from_value(value)
            device_id = derive_public_device_id(node.udid)
            if (
                device_id == self._local_device_id
                or device_id in device_ids
                or node.network_id in network_ids
            ):
                _fail("PRESENCE_INPUT_INVALID")
            device_ids.add(device_id)
            network_ids.add(node.network_id)
            normalized.append((device_id, node))
        normalized.sort(key=lambda item: item[0])
        return normalized

    def apply_snapshot(
        self, values: Sequence[DiscoveredNode | Mapping[str, Any]]
    ) -> tuple[PresenceTransition, ...]:
        """Replace the online set using sorted, deterministic 64-peer admission."""

        self._require_epoch()
        normalized = self._normalize_nodes(values)
        admitted = normalized[: protocol.PEER_REGISTRY_MAX]
        rejected = normalized[protocol.PEER_REGISTRY_MAX :]
        previous = self._records
        replacement: dict[str, _PeerRecord] = {}
        transitions: list[PresenceTransition] = []
        for device_id, node in admitted:
            existing = previous.get(device_id)
            if (
                existing is not None
                and existing.network_id == node.network_id
                and existing.udid == node.udid
            ):
                generation = existing.generation
            else:
                generation = self._next_generation()
            replacement[device_id] = _PeerRecord(
                device_id=device_id,
                network_id=node.network_id,
                udid=node.udid,
                device_name=node.device_name,
                device_type_id=node.device_type_id,
                generation=generation,
            )
            transitions.append(
                PresenceTransition(True, device_id, generation, "ONLINE")
            )
        self._records = replacement
        self._record_drop(len(rejected))
        transitions.extend(
            PresenceTransition(False, device_id, None, "ONLINE")
            for device_id, _ in rejected
        )
        return tuple(transitions)

    def apply_snapshot_barrier(
        self,
        values: Sequence[DiscoveredNode | Mapping[str, Any]],
        *,
        replay_after_seq: int,
    ) -> tuple[PresenceTransition, ...]:
        """Commit a snapshot at its first watermark before exact event replay."""

        self._require_epoch()
        watermark = _bounded_int(
            replay_after_seq, "replayAfterSeq", 0, 2**64 - 1
        )
        if watermark < self._last_node_sequence:
            _fail("PRESENCE_EVENT_SEQUENCE_INVALID")
        transitions = self.apply_snapshot(values)
        self._last_node_sequence = watermark
        return transitions

    def _advance_event_sequence(self, value: Any) -> int:
        self._require_epoch()
        sequence = _bounded_int(value, "nodeEventSeq", 1, 2**64 - 1)
        if sequence != self._last_node_sequence + 1:
            _fail("PRESENCE_EVENT_SEQUENCE_INVALID")
        self._last_node_sequence = sequence
        return sequence

    def node_online(
        self,
        value: DiscoveredNode | Mapping[str, Any],
        *,
        node_event_seq: int,
    ) -> PresenceTransition:
        self._advance_event_sequence(node_event_seq)
        node = DiscoveredNode.from_value(value)
        device_id = derive_public_device_id(node.udid)
        if device_id == self._local_device_id:
            _fail("PRESENCE_INPUT_INVALID")
        generation = self._next_generation()
        candidate = _PeerRecord(
            device_id=device_id,
            network_id=node.network_id,
            udid=node.udid,
            device_name=node.device_name,
            device_type_id=node.device_type_id,
            generation=generation,
        )
        contenders = dict(self._records)
        contenders[device_id] = candidate
        keep = sorted(contenders)[: protocol.PEER_REGISTRY_MAX]
        if device_id not in keep:
            self._record_drop(1)
            return PresenceTransition(False, device_id, None, "ONLINE")
        displaced = set(contenders) - set(keep)
        if displaced:
            self._record_drop(len(displaced))
        self._records = {key: contenders[key] for key in keep}
        return PresenceTransition(True, device_id, generation, "ONLINE")

    def discard_node_event(self, *, node_event_seq: int) -> None:
        """Advance a verified event whose peer identity could not be resolved."""

        self._advance_event_sequence(node_event_seq)
        self._record_drop(1)

    def node_offline(
        self,
        network_id: str,
        *,
        node_event_seq: int,
        refresh_nodes: Sequence[DiscoveredNode | Mapping[str, Any]] | None = None,
    ) -> PresenceTransition:
        self._advance_event_sequence(node_event_seq)
        normalized_network_id = _bounded_text(network_id, "networkId", 1, 64)
        matches = [
            record
            for record in self._records.values()
            if record.network_id == normalized_network_id
        ]
        if len(matches) > 1:
            _fail("PRESENCE_STATE_CORRUPT")
        if matches:
            record = matches[0]
            del self._records[record.device_id]
            transition = PresenceTransition(
                True, record.device_id, record.generation, "OFFLINE"
            )
        else:
            transition = PresenceTransition(False, "", None, "OFFLINE")
        if refresh_nodes is not None:
            self.apply_snapshot(refresh_nodes)
        return transition

    def connection_plan(self) -> tuple[Mapping[str, Any], ...]:
        """Return a public, deterministic plan without opening any Socket."""

        ordered = sorted(self._records.values(), key=lambda item: item.device_id)
        result: list[Mapping[str, Any]] = []
        for index, record in enumerate(ordered):
            admitted = index < self._effective_peer_cap
            result.append(
                MappingProxyType(
                    {
                        "action": (
                            "INITIATE"
                            if self._local_device_id < record.device_id
                            else "AWAIT_INBOUND"
                        ),
                        "admitted": admitted,
                        "deviceId": record.device_id,
                        "generation": record.generation,
                        "reason": "" if admitted else "CAPACITY_BUSY",
                    }
                )
            )
        return tuple(result)

    def connection_candidates(self) -> tuple[PresenceConnectionCandidate, ...]:
        """Return bounded raw network IDs exclusively to the owner transport."""

        plans = {str(plan["deviceId"]): plan for plan in self.connection_plan()}
        return tuple(
            PresenceConnectionCandidate(
                device_id=record.device_id,
                network_id=record.network_id,
                generation=record.generation,
                action=str(plans[record.device_id]["action"]),
                admitted=bool(plans[record.device_id]["admitted"]),
            )
            for record in sorted(self._records.values(), key=lambda item: item.device_id)
        )

    def device_id_for_network(self, network_id: str) -> str | None:
        """Resolve a Socket-derived network ID inside the owner privacy boundary."""

        normalized = _bounded_text(network_id, "networkId", 1, 64)
        matches = [
            record.device_id
            for record in self._records.values()
            if record.network_id == normalized
        ]
        if len(matches) > 1:
            _fail("PRESENCE_STATE_CORRUPT")
        return matches[0] if matches else None

    def trust_revoked(self, network_id: str) -> PresenceTransition:
        """Remove one record after DeviceManager confirms local trust removal.

        This transition is independent of the Native node-event sequence.  A
        later ``node-offline`` event is still consumed normally, and a new
        ``node-online`` event after an explicit re-pair may create a fresh
        generation.
        """

        normalized = _bounded_text(network_id, "networkId", 1, 64)
        matches = [
            record
            for record in self._records.values()
            if record.network_id == normalized
        ]
        if len(matches) > 1:
            _fail("PRESENCE_STATE_CORRUPT")
        if not matches:
            return PresenceTransition(False, "", None, "OFFLINE")
        record = matches[0]
        del self._records[record.device_id]
        return PresenceTransition(True, record.device_id, record.generation, "OFFLINE")

    def public_peers(self) -> tuple[Mapping[str, Any], ...]:
        plans = {plan["deviceId"]: plan for plan in self.connection_plan()}
        return tuple(
            MappingProxyType(
                {
                    "agentAvailability": "UNAVAILABLE",
                    "connectionAdmitted": plans[record.device_id]["admitted"],
                    "connectionAction": plans[record.device_id]["action"],
                    "connectionState": "CLOSED",
                    "deviceContextAvailability": "UNKNOWN",
                    "deviceId": record.device_id,
                    "deviceName": record.device_name,
                    "devicePresence": "ONLINE",
                    "deviceTypeId": record.device_type_id,
                    "generation": record.generation,
                }
            )
            for record in sorted(self._records.values(), key=lambda item: item.device_id)
        )

    def begin_shutdown(self) -> None:
        self._accepting = False

    def clear(self) -> None:
        self._records.clear()
        self._last_node_sequence = 0
        self._worker_epoch = None
        self._accepting = False

    def health_updates(self) -> Mapping[str, int]:
        return MappingProxyType(
            {
                "connectedPeerCount": 0,
                "peerCount": len(self._records),
                "peerRegistryDropped": self._dropped,
                "readyPeerCount": 0,
                "stateFreshPeerCount": 0,
            }
        )


__all__ = [
    "DiscoveredNode",
    "InMemoryPresenceAdapter",
    "PresenceError",
    "PresenceConnectionCandidate",
    "PresenceTransition",
    "derive_public_device_id",
]
