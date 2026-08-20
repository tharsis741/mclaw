# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Product construction for the interactive DSoftBus Runtime.

The user path prepares one device-local Profile under ``MCLAW_HOME``. Native
libraries are loaded exclusively by the isolated Worker after the Runtime owns
its endpoint.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from . import protocol
from .agent_message import AgentMessageError
from .device_context import DeviceContextError
from .discovery_resources import DiscoveryOwnerResources
from .owner import DsoftbusOwnerLoopDriver
from .provider_readiness import resolve_provider_readiness
from .runtime import (
    DsoftbusRuntime,
    EndpointLockFactory,
    _default_endpoint_lock_factory,
)
from .worker_supervisor import WorkerSupervisorError

_UNVERIFIED_SOCKET_CAP = 1 + protocol.TRANSIENT_SOCKET_CAP + 1


class ProductActivationError(RuntimeError):
    """A local Profile preparation failure that leaves normal M-Claw usable."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class ProductRuntimeInputs:
    """Non-discoverable launch inputs selected by the product launcher."""

    profile_path: str = ""
    profile_sha256: str = ""
    python_preload_path: str = ""
    socket_cap: int = _UNVERIFIED_SOCKET_CAP
    status_code: str = "PRODUCT_INPUT_MISSING"
    current_boot_id: str = ""
    raw_token_id: str = field(default="", repr=False)

    @property
    def ready(self) -> bool:
        return self.status_code == ""

    @classmethod
    def from_local_state(
        cls,
        state_root: str | os.PathLike[str],
    ) -> "ProductRuntimeInputs":
        """Prepare the installed device closure without a deployment descriptor."""

        from .runtime_profile import RuntimeProfileError, prepare_runtime_profile

        try:
            prepared = prepare_runtime_profile(state_root)
        except RuntimeProfileError as error:
            return cls(status_code=error.code)
        profile = prepared.profile
        closure = profile.document["runtimeClosure"]
        return cls(
            profile_path=str(profile.path),
            profile_sha256=profile.sha256,
            python_preload_path=closure["python"]["dynamicLibpython"]["path"],
            raw_token_id=prepared.raw_token_id,
            socket_cap=closure["softbusSocketCap"],
            current_boot_id=prepared.current_boot_id,
            status_code="",
        )


def _zero_health_updates() -> Mapping[str, Any]:
    values = {operation: 0 for operation in protocol.WORKER_OPERATIONS}
    return MappingProxyType(
        {
            "connectedPeerCount": 0,
            "listenerReady": False,
            "listenerSocketCount": 0,
            "openSocketCount": 0,
            "parentCommandQueueBytes": 0,
            "parentCommandQueueCount": 0,
            "parentEventBytes": 0,
            "parentEventDepth": 0,
            "parentResponseRouteBytes": 0,
            "parentResponseRouteCount": 0,
            "peerCount": 0,
            "peerRegistryDropped": 0,
            "productIntegrationVerified": False,
            "readyPeerCount": 0,
            "restartCount": 0,
            "retainedPeerSocketCount": 0,
            "stateFreshPeerCount": 0,
            "transientSocketCount": 0,
            "workerAlive": False,
            "workerEpoch": None,
            "workerPid": None,
            "workerStartTimeTicks": None,
            "workerEventBytes": 0,
            "workerEventDepth": 0,
            # Kept only in the local diagnostic cache, never merged into health.
            "_operationCounts": MappingProxyType(values),
        }
    )


def _load_product_profile(path: str) -> Any:
    from .baseline import load_runtime_profile

    return load_runtime_profile(path)


def _load_product_manifest_template() -> Any:
    from .manifest import load_local_manifest_template

    return load_local_manifest_template()


def _build_discovery_resources(
    profile: Any,
    raw_token_id: str,
    manifest_template: Any,
    provider_runtime: Any | None,
    *,
    config: Mapping[str, Any] | None = None,
    agent_workspace_root: str | Path | None = None,
    pairing_state_path: str | Path | None = None,
    task_state_root: str | Path | None = None,
    current_boot_id: str | None = None,
) -> DiscoveryOwnerResources:
    from .worker_supervisor import (
        ProfileWorkerLauncher,
        WorkerIdentityExpectation,
        WorkerSupervisor,
    )

    launcher = ProfileWorkerLauncher(
        profile=profile,
        raw_token_id=raw_token_id,
        expected_boot_id=current_boot_id,
    )
    supervisor = WorkerSupervisor(
        launcher=launcher,
        identity=WorkerIdentityExpectation.from_profile(
            profile,
        ),
    )
    message_config: dict[str, Any] | None = None
    if config is not None:
        dsoftbus = config.get("dsoftbus")
        if not isinstance(dsoftbus, Mapping):
            raise ValueError("DSOFTBUS_CONFIG_INVALID")
        message_config = {
            name: dsoftbus[name]
            for name in (
                "accept_remote_messages",
                "global_requests_per_minute",
                "per_peer_requests_per_minute",
                "remote_token_budget_per_hour",
            )
        }
    provider_ready, provider_code = resolve_provider_readiness(provider_runtime)
    return DiscoveryOwnerResources(
        supervisor=supervisor,
        manifest_template=manifest_template,
        provider_ready=provider_ready,
        provider_readiness_code=provider_code,
        provider_runtime=provider_runtime,
        message_config=message_config,
        agent_config=config,
        agent_workspace_root=agent_workspace_root,
        pairing_state_path=pairing_state_path,
        task_state_root=task_state_root,
        provider_readiness=resolve_provider_readiness,
    )


class ProductDiscoveryOwnerResources:
    """Deferred Profile-to-supervisor adapter owned by one Runtime owner loop."""

    def __init__(
        self,
        *,
        inputs: ProductRuntimeInputs,
        provider_runtime: Any | None = None,
        profile_loader: Callable[[str], Any] = _load_product_profile,
        manifest_loader: Callable[[], Any] = _load_product_manifest_template,
        discovery_factory: Callable[
            [Any, str, Any, Any | None], DiscoveryOwnerResources
        ]
        | None = None,
    ) -> None:
        if not isinstance(inputs, ProductRuntimeInputs):
            raise TypeError("inputs must be ProductRuntimeInputs")
        self._inputs = inputs
        self._provider_runtime = provider_runtime
        self._profile_loader = profile_loader
        self._manifest_loader = manifest_loader
        if discovery_factory is None:
            def _default_discovery_factory(
                profile: Any,
                raw_token_id: str,
                manifest_template: Any,
                current_provider_runtime: Any | None,
            ) -> DiscoveryOwnerResources:
                return _build_discovery_resources(
                    profile,
                    raw_token_id,
                    manifest_template,
                    current_provider_runtime,
                    current_boot_id=inputs.current_boot_id or None,
                )

            self._discovery_factory = _default_discovery_factory
        else:
            self._discovery_factory = discovery_factory
        self._owner_thread_id: int | None = None
        self._delegate: DiscoveryOwnerResources | None = None
        self._manifest_template: Any | None = None
        self._manifest_read_count = 0
        self._stopped = False
        self._health_change_callback: Callable[[Mapping[str, Any]], None] | None = None
        self._cache_lock = threading.Lock()
        self._cached_diagnostic: Mapping[str, Any] = MappingProxyType(
            {
                "activeThreadCount": 0,
                "agentSessionTaskCount": 0,
                "dispatchExecutionCount": 0,
                "dispatchQueueBytes": 0,
                "dispatchQueueCount": 0,
                "inflightMessageCount": 0,
                "localTurnCount": 0,
                "operationCounts": MappingProxyType(
                    {operation: 0 for operation in protocol.WORKER_OPERATIONS}
                ),
                "agentCardGenerationCount": 0,
                "listenerGenerationCount": 0,
                "manifestDescriptorGenerationCount": 0,
                "manifestPhaseBComplete": False,
                "manifestTemplateLoaded": False,
                "manifestTemplateReadCount": 0,
                "peerCount": 0,
                "productInputCode": inputs.status_code,
                "profileConfigured": inputs.ready,
                "publicManifestGenerationCount": 0,
                "remoteAccepted": 0,
                "remoteBudgetUsed": 0,
                "remoteRejectedByCode": MappingProxyType({}),
                "responseCacheCount": 0,
                "stateEpochFrozen": False,
                "tokenReserved": 0,
                "workerAlive": False,
            }
        )

    def _require_owner(self, *, establish: bool = False) -> None:
        current = threading.get_ident()
        if self._owner_thread_id is None and establish:
            self._owner_thread_id = current
        if self._owner_thread_id != current:
            raise RuntimeError("DISCOVERY_RESOURCE_OWNER_MISMATCH")

    @staticmethod
    def _public_health(values: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
        source = _zero_health_updates() if values is None else values
        return MappingProxyType(
            {key: value for key, value in source.items() if not key.startswith("_")}
        )

    def _update_cache(self, *, product_input_code: str = "") -> None:
        delegate = self._delegate
        with self._cache_lock:
            manifest_loaded = self._manifest_template is not None
            manifest_read_count = self._manifest_read_count
        if delegate is None:
            zero = _zero_health_updates()
            diagnostic = {
                "activeThreadCount": 0,
                "agentSessionTaskCount": 0,
                "dispatchExecutionCount": 0,
                "dispatchQueueBytes": 0,
                "dispatchQueueCount": 0,
                "inflightMessageCount": 0,
                "localTurnCount": 0,
                "agentCardGenerationCount": 0,
                "listenerGenerationCount": 0,
                "manifestDescriptorGenerationCount": 0,
                "manifestPhaseBComplete": False,
                "manifestTemplateLoaded": manifest_loaded,
                "manifestTemplateReadCount": manifest_read_count,
                "operationCounts": dict(zero["_operationCounts"]),
                "peerCount": 0,
                "productInputCode": product_input_code or self._inputs.status_code,
                "profileConfigured": self._inputs.ready,
                "publicManifestGenerationCount": 0,
                "remoteAccepted": 0,
                "remoteBudgetUsed": 0,
                "remoteRejectedByCode": {},
                "responseCacheCount": 0,
                "stateEpochFrozen": False,
                "tokenReserved": 0,
                "workerAlive": False,
            }
        else:
            current = delegate.cached_diagnostic()
            publication = delegate.publication_snapshot()
            diagnostic = {
                "activeThreadCount": current["activeThreadCount"],
                "agentSessionTaskCount": current.get("agentSessionTaskCount", 0),
                "dispatchExecutionCount": current.get(
                    "dispatchExecutionCount", 0
                ),
                "dispatchQueueBytes": current.get("dispatchQueueBytes", 0),
                "dispatchQueueCount": current.get("dispatchQueueCount", 0),
                "inflightMessageCount": current.get("inflightMessageCount", 0),
                "localTurnCount": current.get("localTurnCount", 0),
                "agentCardGenerationCount": publication[
                    "agentCardGenerationCount"
                ],
                "listenerGenerationCount": publication[
                    "listenerGenerationCount"
                ],
                "manifestDescriptorGenerationCount": publication[
                    "manifestDescriptorGenerationCount"
                ],
                "manifestPhaseBComplete": publication[
                    "manifestPhaseBComplete"
                ],
                "manifestTemplateLoaded": manifest_loaded,
                "manifestTemplateReadCount": manifest_read_count,
                "operationCounts": dict(current["operationCounts"]),
                "peerCount": current["peerCount"],
                "productInputCode": product_input_code,
                "profileConfigured": True,
                "publicManifestGenerationCount": publication[
                    "publicManifestGenerationCount"
                ],
                "remoteAccepted": current.get("remoteAccepted", 0),
                "remoteBudgetUsed": current.get("remoteBudgetUsed", 0),
                "remoteRejectedByCode": dict(
                    current.get("remoteRejectedByCode", {})
                ),
                "responseCacheCount": current.get("responseCacheCount", 0),
                "stateEpochFrozen": publication["stateEpochFrozen"],
                "tokenReserved": current.get("tokenReserved", 0),
                "workerAlive": current["workerAlive"],
            }
        with self._cache_lock:
            self._cached_diagnostic = MappingProxyType(
                {
                    **diagnostic,
                    "operationCounts": MappingProxyType(
                        dict(diagnostic["operationCounts"])
                    ),
                    "remoteRejectedByCode": MappingProxyType(
                        dict(diagnostic["remoteRejectedByCode"])
                    ),
                }
            )

    def _on_delegate_health_change(self, updates: Mapping[str, Any]) -> None:
        """Refresh product diagnostics before forwarding one owner-loop update."""

        self._require_owner()
        self._update_cache()
        callback = self._health_change_callback
        if callback is not None:
            forwarded = dict(self._public_health(updates))
            for key in ("_lifecycleState", "_degradedReasons"):
                if key in updates:
                    forwarded[key] = updates[key]
            callback(MappingProxyType(forwarded))

    def set_health_change_callback(
        self,
        callback: Callable[[Mapping[str, Any]], None] | None,
    ) -> None:
        """Attach the lifecycle owner's semantic health publisher."""

        self._require_owner()
        if callback is not None and not callable(callback):
            raise TypeError("health callback must be callable or None")
        self._health_change_callback = callback
        delegate = self._delegate
        if delegate is not None:
            delegate.set_health_change_callback(
                self._on_delegate_health_change if callback is not None else None
            )

    async def start(self, runtime_instance_id: str) -> Mapping[str, Any]:
        self._require_owner(establish=True)
        if not self._inputs.ready:
            self._update_cache()
            return MappingProxyType(
                {
                    "degradedReasons": ("WORKER_START_FAILED",),
                    "healthUpdates": self._public_health(),
                    "state": "DEGRADED",
                }
            )
        try:
            profile = self._profile_loader(self._inputs.profile_path)
            if (
                profile.sha256 != self._inputs.profile_sha256
                or profile.document["runtimeClosure"]["softbusSocketCap"]
                != self._inputs.socket_cap
                or profile.document["runtimeClosure"]["python"][
                    "dynamicLibpython"
                ]["path"]
                != self._inputs.python_preload_path
            ):
                raise ProductActivationError("PRODUCT_INPUT_MISMATCH")
            from .manifest import LocalManifestTemplate

            with self._cache_lock:
                self._manifest_read_count += 1
            manifest_template = self._manifest_loader()
            if not isinstance(manifest_template, LocalManifestTemplate):
                raise RuntimeError("MANIFEST_TEMPLATE_INVALID")
            with self._cache_lock:
                self._manifest_template = manifest_template
            delegate = self._discovery_factory(
                profile,
                self._inputs.raw_token_id,
                manifest_template,
                self._provider_runtime,
            )
            self._delegate = delegate
            outcome = await delegate.start(runtime_instance_id)
        except Exception as error:
            code = getattr(error, "code", "WORKER_START_FAILED")
            self._update_cache(product_input_code=str(code))
            return MappingProxyType(
                {
                    "degradedReasons": ("WORKER_START_FAILED",),
                    "healthUpdates": self._public_health(),
                    "state": "DEGRADED",
                }
            )
        self._update_cache()
        return outcome

    async def begin_shutdown(self) -> Mapping[str, Any] | None:
        self._require_owner()
        if self._stopped:
            return self._public_health()
        if self._delegate is None:
            return self._public_health()
        result = await self._delegate.begin_shutdown()
        self._update_cache()
        return result

    async def stop(self, deadline: Callable[[], float]) -> Mapping[str, Any] | None:
        self._require_owner()
        if self._stopped:
            return self._public_health()
        if self._delegate is None:
            self._stopped = True
            return self._public_health()
        result = await self._delegate.stop(deadline)
        self._stopped = True
        self._update_cache()
        return result

    async def update_provider_runtime(self, context: Any | None) -> Mapping[str, Any] | None:
        self._require_owner()
        self._provider_runtime = context
        if self._delegate is None:
            return self._public_health()
        result = await self._delegate.update_provider_runtime(context)
        self._update_cache()
        return result

    def local_turn_started(self, token: str) -> None:
        self._require_owner()
        if self._delegate is not None:
            self._delegate.local_turn_started(token)

    def local_turn_finished(self, token: str) -> None:
        self._require_owner()
        if self._delegate is not None:
            self._delegate.local_turn_finished(token)

    def emergency_reap(self) -> None:
        delegate = self._delegate
        if delegate is not None:
            delegate.emergency_reap()
        self._update_cache()

    def cached_diagnostic(self) -> Mapping[str, Any]:
        """Return product metadata plus the delegate's latest bounded cache."""

        with self._cache_lock:
            value = self._cached_diagnostic
            delegate = self._delegate
        current: Mapping[str, Any] = value
        if delegate is not None:
            current = delegate.cached_diagnostic()
        return MappingProxyType(
            {
                "activeThreadCount": current.get(
                    "activeThreadCount", value["activeThreadCount"]
                ),
                "agentSessionTaskCount": current.get(
                    "agentSessionTaskCount", value["agentSessionTaskCount"]
                ),
                "dispatchExecutionCount": current.get(
                    "dispatchExecutionCount", value["dispatchExecutionCount"]
                ),
                "dispatchQueueBytes": current.get(
                    "dispatchQueueBytes", value["dispatchQueueBytes"]
                ),
                "dispatchQueueCount": current.get(
                    "dispatchQueueCount", value["dispatchQueueCount"]
                ),
                "inflightMessageCount": current.get(
                    "inflightMessageCount", value["inflightMessageCount"]
                ),
                "localTurnCount": current.get(
                    "localTurnCount", value["localTurnCount"]
                ),
                "agentCardGenerationCount": value["agentCardGenerationCount"],
                "listenerGenerationCount": value["listenerGenerationCount"],
                "manifestDescriptorGenerationCount": value[
                    "manifestDescriptorGenerationCount"
                ],
                "manifestPhaseBComplete": value["manifestPhaseBComplete"],
                "manifestTemplateLoaded": value["manifestTemplateLoaded"],
                "manifestTemplateReadCount": value["manifestTemplateReadCount"],
                "operationCounts": MappingProxyType(
                    dict(current.get("operationCounts", value["operationCounts"]))
                ),
                "peerCount": current.get("peerCount", value["peerCount"]),
                "productInputCode": value["productInputCode"],
                "profileConfigured": value["profileConfigured"],
                "publicManifestGenerationCount": value[
                    "publicManifestGenerationCount"
                ],
                "remoteAccepted": current.get(
                    "remoteAccepted", value["remoteAccepted"]
                ),
                "remoteBudgetUsed": current.get(
                    "remoteBudgetUsed", value["remoteBudgetUsed"]
                ),
                "remoteRejectedByCode": MappingProxyType(
                    dict(
                        current.get(
                            "remoteRejectedByCode",
                            value["remoteRejectedByCode"],
                        )
                    )
                ),
                "responseCacheCount": current.get(
                    "responseCacheCount", value["responseCacheCount"]
                ),
                "stateEpochFrozen": value["stateEpochFrozen"],
                "tokenReserved": current.get(
                    "tokenReserved", value["tokenReserved"]
                ),
                "workerAlive": current.get("workerAlive", value["workerAlive"]),
            }
        )

    def cached_public_peers(self) -> tuple[Mapping[str, Any], ...]:
        """Expose only the delegate's redacted, identity-verified peer cache."""

        delegate = self._delegate
        if delegate is None:
            return ()
        return delegate.cached_public_peers()

    def list_trusted_devices(self) -> tuple[Mapping[str, Any], ...]:
        """Enumerate redacted DeviceManager targets on the owner thread."""

        self._require_owner()
        delegate = self._delegate
        if delegate is None:
            raise WorkerSupervisorError("WORKER_NOT_READY")
        return delegate.list_trusted_devices()

    async def discover_devices(self) -> tuple[Mapping[str, Any], ...]:
        """Run one M-Claw DeviceManager scan through the active Worker."""

        self._require_owner()
        delegate = self._delegate
        if delegate is None:
            raise WorkerSupervisorError("WORKER_NOT_READY")
        return await delegate.discover_devices()

    async def pair_device(self, device_id_sha256: str) -> Mapping[str, Any]:
        """Begin one user-approved system bind and await its result."""

        self._require_owner()
        delegate = self._delegate
        if delegate is None:
            raise WorkerSupervisorError("WORKER_NOT_READY")
        return await delegate.pair_device(device_id_sha256)

    async def unbind_device(self, device_id_sha256: str) -> Mapping[str, Any]:
        """Remove one explicitly selected trust relationship."""

        self._require_owner()
        delegate = self._delegate
        if delegate is None:
            raise WorkerSupervisorError("WORKER_NOT_READY")
        return await delegate.unbind_device(device_id_sha256)

    def cached_device_context(self, device_id: str) -> Mapping[str, Any]:
        """Expose one delegate cache entry without crossing into owner I/O."""

        delegate = self._delegate
        if delegate is None:
            raise DeviceContextError("PEER_NOT_READY")
        return delegate.cached_device_context(device_id)

    async def refresh_device_context(self, device_id: str) -> Mapping[str, Any]:
        """Run one bounded refresh inside the existing owner generation."""

        self._require_owner()
        delegate = self._delegate
        if delegate is None:
            raise DeviceContextError("PEER_NOT_READY")
        return await delegate.refresh_device_context(device_id)

    async def run_agent_task(
        self,
        device_id: str,
        text: str,
        *,
        context_id: str | None,
        message_id: str,
        event_sink: Callable[[Mapping[str, Any]], Any] | None = None,
    ) -> Mapping[str, Any]:
        """Route one streamed Task through the existing product owner."""

        self._require_owner()
        delegate = self._delegate
        if delegate is None:
            raise AgentMessageError("PEER_NOT_READY")
        return await delegate.run_agent_task(
            device_id,
            text,
            context_id=context_id,
            message_id=message_id,
            event_sink=event_sink,
        )

    def manifest_phase_snapshot(self) -> Mapping[str, Any]:
        """Expose non-sensitive Stage 4 construction counters for tests/Doctor."""

        with self._cache_lock:
            loaded = self._manifest_template is not None
            read_count = self._manifest_read_count
        delegate = self._delegate
        publication = (
            delegate.publication_snapshot()
            if delegate is not None
            else MappingProxyType({})
        )
        return MappingProxyType(
            {
                "agentCardGenerationCount": publication.get(
                    "agentCardGenerationCount", 0
                ),
                "listenerGenerationCount": publication.get(
                    "listenerGenerationCount", 0
                ),
                "manifestDescriptorGenerationCount": publication.get(
                    "manifestDescriptorGenerationCount", 0
                ),
                "manifestTemplateLoaded": loaded,
                "manifestTemplateReadCount": read_count,
                "publicManifestGenerationCount": publication.get(
                    "publicManifestGenerationCount", 0
                ),
            }
        )


def create_product_runtime(
    *,
    provider_runtime: Any | None,
    config: Mapping[str, Any],
    workspace: str | Path,
    state_root: str | Path | None = None,
    require_activation: bool = False,
    endpoint_lock_factory: EndpointLockFactory = _default_endpoint_lock_factory,
    monotonic: Callable[[], float] = time.monotonic,
    uuid_factory: Callable[[], uuid.UUID] = uuid.uuid4,
) -> DsoftbusRuntime:
    """Create the product Runtime from device-local state."""

    if state_root is None:
        from mclaw.constants import get_mclaw_home

        state_root = get_mclaw_home() / "dsoftbus"
    state_root_path = Path(state_root)
    inputs = ProductRuntimeInputs.from_local_state(state_root_path)
    if require_activation and not inputs.ready:
        raise ProductActivationError(inputs.status_code)

    def _local_manifest_loader() -> Any:
        from .manifest import load_local_manifest_template

        return load_local_manifest_template(state_root_path / "device.yaml")

    def _product_discovery_factory(
        profile: Any,
        raw_token_id: str,
        manifest_template: Any,
        current_provider_runtime: Any | None,
    ) -> DiscoveryOwnerResources:
        return _build_discovery_resources(
            profile,
            raw_token_id,
            manifest_template,
            current_provider_runtime,
            config=config,
            agent_workspace_root=state_root_path / "agent-contexts",
            pairing_state_path=state_root_path / "paired-devices.json",
            task_state_root=state_root_path,
            current_boot_id=inputs.current_boot_id or None,
        )

    resources = ProductDiscoveryOwnerResources(
        inputs=inputs,
        provider_runtime=provider_runtime,
        manifest_loader=_local_manifest_loader,
        discovery_factory=_product_discovery_factory,
    )
    driver = DsoftbusOwnerLoopDriver(
        config=config,
        provider_runtime=provider_runtime,
        socket_cap=inputs.socket_cap,
        resources=resources,
        provider_readiness=resolve_provider_readiness,
        monotonic=monotonic,
        resource_start_timeout=protocol.RUNTIME_START_TIMEOUT_S,
    )
    return DsoftbusRuntime(
        provider_runtime=provider_runtime,
        config=config,
        workspace=workspace,
        state_root=state_root_path,
        driver=driver,
        endpoint_lock_factory=endpoint_lock_factory,
        monotonic=monotonic,
        uuid_factory=uuid_factory,
    )


__all__ = [
    "ProductDiscoveryOwnerResources",
    "ProductActivationError",
    "ProductRuntimeInputs",
    "create_product_runtime",
]
