# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Cross-platform public surface for the M-Claw DSoftBus Runtime.

Exports are resolved lazily so importing the isolated Worker package does not
pull parent-process lifecycle modules into the Worker code closure.  Native
code remains owned exclusively by :mod:`mclaw.dsoftbus.worker`.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any


_EXPORTS = {
    "ActiveRuntimeError": ("active", "ActiveRuntimeError"),
    "BaselineError": ("baseline", "BaselineError"),
    "CLIENT_SERVICE_NAME": ("protocol", "CLIENT_SERVICE_NAME"),
    "DiscoveryOwnerResources": (
        "discovery_resources",
        "DiscoveryOwnerResources",
    ),
    "DeviceContextError": ("device_context", "DeviceContextError"),
    "DeviceState": ("device_context", "DeviceState"),
    "DiscoveredNode": ("presence", "DiscoveredNode"),
    "DsoftbusEndpointLock": ("endpoint_lock", "DsoftbusEndpointLock"),
    "DsoftbusOwnerLoopDriver": ("owner", "DsoftbusOwnerLoopDriver"),
    "DsoftbusRuntime": ("runtime", "DsoftbusRuntime"),
    "DsoftbusRuntimeError": ("runtime", "DsoftbusRuntimeError"),
    "EndpointLockError": ("endpoint_lock", "EndpointLockError"),
    "IPC_LINE_MAX": ("protocol", "IPC_LINE_MAX"),
    "InMemoryPresenceAdapter": ("presence", "InMemoryPresenceAdapter"),
    "LocalDeviceStateService": (
        "device_context",
        "LocalDeviceStateService",
    ),
    "NATIVE_ABI_VERSION": ("protocol", "NATIVE_ABI_VERSION"),
    "ObservedRuntimeIdentity": ("baseline", "ObservedRuntimeIdentity"),
    "OwnerLoopError": ("owner", "OwnerLoopError"),
    "PROTOCOL_BINDING": ("protocol", "PROTOCOL_BINDING"),
    "PresenceError": ("presence", "PresenceError"),
    "ProductDiscoveryOwnerResources": (
        "product",
        "ProductDiscoveryOwnerResources",
    ),
    "ProductRuntimeInputs": ("product", "ProductRuntimeInputs"),
    "ProfileWorkerLauncher": ("worker_supervisor", "ProfileWorkerLauncher"),
    "ProtocolError": ("protocol", "ProtocolError"),
    "RuntimeHealthError": ("health", "RuntimeHealthError"),
    "RuntimeHealthPublisher": ("health", "RuntimeHealthPublisher"),
    "RUNTIME_PROFILE_FILENAME": ("baseline", "RUNTIME_PROFILE_FILENAME"),
    "RUNTIME_PROFILE_SCHEMA": ("baseline", "RUNTIME_PROFILE_SCHEMA"),
    "RuntimeProfile": ("baseline", "RuntimeProfile"),
    "RuntimePreflightResult": ("baseline", "RuntimePreflightResult"),
    "RuntimeState": ("runtime", "RuntimeState"),
    "RemoteDeviceContextStore": (
        "device_context",
        "RemoteDeviceContextStore",
    ),
    "SERVICE_NAME": ("protocol", "SERVICE_NAME"),
    "SOFTBUS_PACKAGE_NAME": ("protocol", "SOFTBUS_PACKAGE_NAME"),
    "SubprocessWorkerLauncher": (
        "worker_supervisor",
        "SubprocessWorkerLauncher",
    ),
    "WorkerIdentityExpectation": (
        "worker_supervisor",
        "WorkerIdentityExpectation",
    ),
    "WorkerSupervisor": ("worker_supervisor", "WorkerSupervisor"),
    "WorkerSupervisorError": (
        "worker_supervisor",
        "WorkerSupervisorError",
    ),
    "can_enter_discovery_only": ("entrypoint", "can_enter_discovery_only"),
    "clear_active_runtime": ("active", "clear_active_runtime"),
    "create_product_runtime": ("product", "create_product_runtime"),
    "derive_public_device_id": ("presence", "derive_public_device_id"),
    "get_active_runtime": ("active", "get_active_runtime"),
    "has_persisted_provider_selection": (
        "entrypoint",
        "has_persisted_provider_selection",
    ),
    "install_active_runtime": ("active", "install_active_runtime"),
    "is_discovery_only_candidate": (
        "entrypoint",
        "is_discovery_only_candidate",
    ),
    "load_runtime_profile": ("baseline", "load_runtime_profile"),
    "parse_runtime_health": ("health", "parse_runtime_health"),
    "preflight_runtime_profile": ("baseline", "preflight_runtime_profile"),
    "validate_runtime_health": ("health", "validate_runtime_health"),
    "validate_device_state": ("device_context", "validate_device_state"),
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = target
    value = getattr(import_module(f"{__name__}.{module_name}"), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
