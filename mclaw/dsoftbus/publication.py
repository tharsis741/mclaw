# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Immutable local publications created after verified Worker identity.

The product-owned YAML template is deliberately insufficient to create any
network-visible object.  This module is the single transition that combines
that template with the identity returned by the isolated Worker.  Callers must
complete this transition before enabling Native node callbacks or opening a
SoftBus listener.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from . import protocol
from .a2a import AgentCard, build_agent_card, encode_success_frame
from .binding import derive_public_agent_id
from .manifest import LocalManifestTemplate, PublicManifest, build_public_manifest


_PREFLIGHT_REQUEST_ID = "00000000-0000-4000-8000-000000000001"


class PublicationError(RuntimeError):
    """Stable failure while freezing the current Runtime publication set."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def _preflight_result(method: str, value: Mapping[str, Any]) -> None:
    frame = encode_success_frame(
        _PREFLIGHT_REQUEST_ID,
        value,
        negotiated_mtu=protocol.REMOTE_FRAME_MAX,
        phase="CARD_VERIFYING" if method == "agentCard" else "READY",
    )
    if frame.data is None or frame.close_generation or frame.terminal_reason:
        raise PublicationError("PUBLICATION_FRAME_TOO_LARGE")


@dataclass(frozen=True, slots=True)
class LocalPublications:
    """One Runtime instance's identity-bound, immutable public foundation."""

    runtime_instance_id: str
    device_id: str
    agent_id: str
    manifest: PublicManifest
    card_preflight: AgentCard

    @property
    def state_epoch(self) -> str:
        return self.runtime_instance_id

    def build_agent_card(
        self, *, provider_ready: bool, provider_readiness_code: str
    ) -> AgentCard:
        """Inject the current Provider hint and repeat the shared frame gate."""

        card = build_agent_card(
            device_id=self.device_id,
            agent_id=self.agent_id,
            provider_ready=provider_ready,
            provider_readiness_code=provider_readiness_code,
        )
        _preflight_result("agentCard", {"agentCard": _plain(card.document)})
        return card

    def gate_mapping(self) -> Mapping[str, Any]:
        """Return the non-sensitive identity/descriptor gate for the supervisor."""

        return MappingProxyType(
            {
                "agentId": self.agent_id,
                "deviceId": self.device_id,
                "manifest": self.manifest.descriptor.as_mapping(),
                "runtimeInstanceId": self.runtime_instance_id,
            }
        )


def freeze_local_publications(
    *,
    template: LocalManifestTemplate,
    verified_device_id: str,
    verified_agent_id: str,
    runtime_instance_id: str,
    provider_ready: bool = False,
    provider_readiness_code: str = "PROVIDER_MISSING",
) -> LocalPublications:
    """Create all static public objects, or fail before Native ``start``."""

    if not isinstance(template, LocalManifestTemplate):
        raise TypeError("template must be LocalManifestTemplate")
    try:
        normalized_runtime = protocol.canonical_uuid4(
            runtime_instance_id, "runtimeInstanceId"
        )
        expected_agent_id = derive_public_agent_id(verified_device_id)
    except Exception as error:
        raise PublicationError("PUBLICATION_IDENTITY_INVALID") from error
    if verified_agent_id != expected_agent_id:
        raise PublicationError("PUBLICATION_IDENTITY_MISMATCH")
    try:
        manifest = build_public_manifest(template, verified_device_id)
        card = build_agent_card(
            device_id=verified_device_id,
            agent_id=verified_agent_id,
            provider_ready=provider_ready,
            provider_readiness_code=provider_readiness_code,
        )
        _preflight_result("manifest", {"manifest": _plain(manifest.document)})
        _preflight_result("agentCard", {"agentCard": _plain(card.document)})
    except PublicationError:
        raise
    except Exception as error:
        raise PublicationError("PUBLICATION_INVALID") from error
    return LocalPublications(
        runtime_instance_id=normalized_runtime,
        device_id=verified_device_id,
        agent_id=verified_agent_id,
        manifest=manifest,
        card_preflight=card,
    )


__all__ = [
    "LocalPublications",
    "PublicationError",
    "freeze_local_publications",
]
