# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Owner-confined in-memory state for one authenticated SoftBus binding."""

from __future__ import annotations

import copy
from dataclasses import dataclass
import re
import secrets
from types import MappingProxyType
from typing import Any, Callable, Mapping, NoReturn

from . import protocol
from .a2a import (
    A2AError,
    AgentCard,
    CoreMethodCall,
    validate_agent_card,
    validate_core_method,
)
from .manifest import ManifestDescriptor, validate_manifest_descriptor


_DEVICE_ID = re.compile(r"^urn:mclaw:device:oh:([0-9a-f]{64})$")
_AGENT_ID = re.compile(r"^urn:mclaw:agent:([0-9a-f]{64})$")
_NONCE = re.compile(r"^[0-9a-f]{64}$")
_MAX_GENERATION = 2**63 - 1


class BindingPhase(str):
    UNBOUND = "UNBOUND"
    BINDING = "BINDING"
    BINDING_OPEN = "BINDING_OPEN"
    CARD_VERIFYING = "CARD_VERIFYING"
    READY = "READY"
    CLOSED = "CLOSED"


class BindingError(RuntimeError):
    """Stable state-machine failure for one connection generation."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _fail(code: str) -> NoReturn:
    raise BindingError(code)


def derive_public_agent_id(device_id: str) -> str:
    match = _DEVICE_ID.fullmatch(device_id) if isinstance(device_id, str) else None
    if match is None:
        _fail("BINDING_INPUT_INVALID")
    return f"urn:mclaw:agent:{match.group(1)}"


def _validate_identity(device_id: Any, agent_id: Any) -> tuple[str, str]:
    device = _DEVICE_ID.fullmatch(device_id) if isinstance(device_id, str) else None
    agent = _AGENT_ID.fullmatch(agent_id) if isinstance(agent_id, str) else None
    if device is None or agent is None or device.group(1) != agent.group(1):
        _fail("BINDING_INPUT_INVALID")
    return device_id, agent_id


@dataclass(frozen=True, slots=True)
class LocalBindingIdentity:
    device_id: str
    agent_id: str
    runtime_instance_id: str
    manifest: ManifestDescriptor

    @classmethod
    def create(
        cls,
        *,
        device_id: str,
        agent_id: str,
        runtime_instance_id: str,
        manifest: ManifestDescriptor | Mapping[str, Any],
    ) -> "LocalBindingIdentity":
        normalized_device, normalized_agent = _validate_identity(device_id, agent_id)
        try:
            normalized_runtime = protocol.canonical_uuid4(
                runtime_instance_id, "runtimeInstanceId"
            )
        except protocol.ProtocolError as error:
            raise BindingError("BINDING_INPUT_INVALID") from error
        try:
            normalized_manifest = (
                manifest
                if isinstance(manifest, ManifestDescriptor)
                else validate_manifest_descriptor(dict(manifest))
            )
        except Exception as error:
            raise BindingError("BINDING_INPUT_INVALID") from error
        return cls(
            normalized_device,
            normalized_agent,
            normalized_runtime,
            normalized_manifest,
        )


@dataclass(frozen=True, slots=True)
class PeerBindingIdentity:
    device_id: str
    agent_id: str
    runtime_instance_id: str
    manifest: ManifestDescriptor


class InMemoryBinding:
    """Handshake and Card verification for a single Socket generation.

    The caller supplies identities already derived from the authenticated
    Socket.  Peer JSON claims are consistency checks only and can never replace
    those values.
    """

    def __init__(
        self,
        *,
        local: LocalBindingIdentity,
        authenticated_peer_device_id: str,
        authenticated_peer_agent_id: str,
        initiator: bool,
        connection_generation: int,
        nonce_factory: Callable[[int], str] = secrets.token_hex,
    ) -> None:
        if not isinstance(local, LocalBindingIdentity):
            raise TypeError("local must be LocalBindingIdentity")
        peer_device, peer_agent = _validate_identity(
            authenticated_peer_device_id, authenticated_peer_agent_id
        )
        if peer_device == local.device_id:
            _fail("BINDING_INPUT_INVALID")
        if type(initiator) is not bool:
            raise TypeError("initiator must be bool")
        if (
            type(connection_generation) is not int
            or not 1 <= connection_generation <= _MAX_GENERATION
        ):
            _fail("BINDING_INPUT_INVALID")
        self._local = local
        self._peer_device_id = peer_device
        self._peer_agent_id = peer_agent
        self._initiator = initiator
        self._generation = connection_generation
        self._nonce_factory = nonce_factory
        self._phase = BindingPhase.BINDING
        self._connection_nonce: str | None = None
        self._peer_identity: PeerBindingIdentity | None = None
        self._peer_card: AgentCard | None = None
        self._local_open_sent = False
        self._local_open_acked = False
        self._peer_open_seen = False
        self._peer_open_accepted = False
        if initiator:
            nonce = nonce_factory(32)
            if not isinstance(nonce, str) or _NONCE.fullmatch(nonce) is None:
                _fail("BINDING_NONCE_INVALID")
            self._connection_nonce = nonce

    @property
    def phase(self) -> str:
        return self._phase

    @property
    def connection_generation(self) -> int:
        return self._generation

    @property
    def peer_identity(self) -> PeerBindingIdentity | None:
        return self._peer_identity

    @property
    def peer_card(self) -> AgentCard | None:
        return self._peer_card

    @property
    def initiator_device_id(self) -> str:
        return self._local.device_id if self._initiator else self._peer_device_id

    def _require_open_phase(self) -> None:
        if self._phase == BindingPhase.CLOSED:
            _fail("STALE_GENERATION")
        if self._phase != BindingPhase.BINDING:
            _fail("BINDING_INCOMPATIBLE")

    def make_local_open(self) -> Mapping[str, Any]:
        """Create this side's sole open request in the required order."""

        self._require_open_phase()
        if self._local_open_sent:
            _fail("BINDING_INCOMPATIBLE")
        if not self._initiator and not self._peer_open_accepted:
            _fail("PEER_NOT_READY")
        nonce = self._connection_nonce
        if nonce is None:
            _fail("PEER_NOT_READY")
        value = {
            "deviceId": self._local.device_id,
            "agentId": self._local.agent_id,
            "runtimeInstanceId": self._local.runtime_instance_id,
            "initiatorDeviceId": self.initiator_device_id,
            "connectionNonce": nonce,
            "bindingVersion": protocol.BINDING_VERSION,
            "a2aVersion": protocol.A2A_PROTOCOL_VERSION,
            "manifest": dict(self._local.manifest.as_mapping()),
        }
        try:
            validated = validate_core_method("mclaw.binding.open", value)
        except A2AError as error:
            raise BindingError(error.reason) from error
        if not isinstance(validated, CoreMethodCall):
            _fail("BINDING_INCOMPATIBLE")
        self._local_open_sent = True
        return validated.params

    def accept_local_open_ack(self, result: Any) -> None:
        """Accept the peer's exact acknowledgement of this side's open."""

        self._require_open_phase()
        if (
            not self._local_open_sent
            or self._local_open_acked
            or not isinstance(result, dict)
            or frozenset(result) != frozenset({"accepted"})
            or result["accepted"] is not True
        ):
            self.close()
            _fail("BINDING_INCOMPATIBLE")
        if self._initiator and self._peer_open_seen:
            self.close()
            _fail("BINDING_INCOMPATIBLE")
        if not self._initiator and not self._peer_open_accepted:
            self.close()
            _fail("BINDING_INCOMPATIBLE")
        self._local_open_acked = True
        self._advance_after_open()

    def accept_peer_open(self, params: Any) -> Mapping[str, bool]:
        """Validate peer claims against the Socket-derived principal."""

        self._require_open_phase()
        if self._peer_open_seen:
            self.close()
            _fail("BINDING_INCOMPATIBLE")
        if self._initiator:
            if not self._local_open_sent or not self._local_open_acked:
                _fail("PEER_NOT_READY")
        elif self._local_open_sent:
            self.close()
            _fail("BINDING_INCOMPATIBLE")
        try:
            call = validate_core_method("mclaw.binding.open", params)
        except A2AError as error:
            reason = (
                "BINDING_INCOMPATIBLE"
                if error.reason in {"INVALID_PARAMS", "VERSION_NOT_SUPPORTED"}
                else error.reason
            )
            self.close()
            raise BindingError(reason) from error
        if not isinstance(call, CoreMethodCall):
            _fail("BINDING_INCOMPATIBLE")
        value = call.params
        expected_initiator = self.initiator_device_id
        if (
            value["deviceId"] != self._peer_device_id
            or value["agentId"] != self._peer_agent_id
            or value["initiatorDeviceId"] != expected_initiator
        ):
            self.close()
            _fail("BINDING_INCOMPATIBLE")
        nonce = value["connectionNonce"]
        if self._initiator:
            if nonce != self._connection_nonce:
                self.close()
                _fail("BINDING_INCOMPATIBLE")
        else:
            self._connection_nonce = nonce
        try:
            descriptor = validate_manifest_descriptor(dict(value["manifest"]))
            peer_runtime = protocol.canonical_uuid4(
                value["runtimeInstanceId"], "runtimeInstanceId"
            )
        except Exception as error:
            self.close()
            raise BindingError("BINDING_INCOMPATIBLE") from error
        self._peer_open_seen = True
        self._peer_open_accepted = True
        self._peer_identity = PeerBindingIdentity(
            self._peer_device_id,
            self._peer_agent_id,
            peer_runtime,
            descriptor,
        )
        self._advance_after_open()
        return MappingProxyType({"accepted": True})

    def _advance_after_open(self) -> None:
        if (
            self._local_open_sent
            and self._local_open_acked
            and self._peer_open_seen
            and self._peer_open_accepted
        ):
            self._phase = BindingPhase.BINDING_OPEN

    def method_allowed(self, method: str) -> bool:
        if not isinstance(method, str) or self._phase == BindingPhase.CLOSED:
            return False
        if self._phase in {BindingPhase.UNBOUND, BindingPhase.BINDING}:
            return method == "mclaw.binding.open"
        if self._phase in {BindingPhase.BINDING_OPEN, BindingPhase.CARD_VERIFYING}:
            return method in {"mclaw.binding.open", "mclaw.agentCard.get"}
        return self._phase == BindingPhase.READY

    def require_method(self, method: str) -> None:
        if self._phase == BindingPhase.CLOSED:
            _fail("STALE_GENERATION")
        if not self.method_allowed(method):
            _fail("PEER_NOT_READY")

    def begin_card_verification(self) -> None:
        if self._phase != BindingPhase.BINDING_OPEN:
            _fail("PEER_NOT_READY")
        if self._peer_identity is None:
            _fail("BINDING_INCOMPATIBLE")
        self._phase = BindingPhase.CARD_VERIFYING

    def complete_card_verification(self, value: AgentCard | Mapping[str, Any]) -> AgentCard:
        if self._phase != BindingPhase.CARD_VERIFYING or self._peer_identity is None:
            _fail("PEER_NOT_READY")
        try:
            card = (
                value
                if isinstance(value, AgentCard)
                else validate_agent_card(
                    copy.deepcopy(dict(value)),
                    expected_device_id=self._peer_device_id,
                    expected_agent_id=self._peer_agent_id,
                )
            )
        except A2AError as error:
            self.close()
            raise BindingError("BINDING_INCOMPATIBLE") from error
        if (
            card.device_id != self._peer_device_id
            or card.agent_id != self._peer_agent_id
        ):
            self.close()
            _fail("BINDING_INCOMPATIBLE")
        self._peer_card = card
        self._phase = BindingPhase.READY
        return card

    def close(self) -> None:
        self._phase = BindingPhase.CLOSED

    def public_snapshot(self) -> Mapping[str, Any]:
        """Bounded diagnostics with no networkId, UDID or nonce."""

        peer = self._peer_identity
        return MappingProxyType(
            {
                "connectionGeneration": self._generation,
                "initiator": self._initiator,
                "localOpenSent": self._local_open_sent,
                "localOpenAcked": self._local_open_acked,
                "peerOpenSeen": self._peer_open_seen,
                "peerOpenAccepted": self._peer_open_accepted,
                "peerDeviceId": self._peer_device_id,
                "peerRuntimeInstanceId": (
                    None if peer is None else peer.runtime_instance_id
                ),
                "phase": self._phase,
            }
        )


__all__ = [
    "BindingError",
    "BindingPhase",
    "InMemoryBinding",
    "LocalBindingIdentity",
    "PeerBindingIdentity",
    "derive_public_agent_id",
]
