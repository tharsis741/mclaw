# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Isolated OpenHarmony DSoftBus Native worker.

The module is import-safe on non-OpenHarmony hosts.  ``ctypes.CDLL`` is only
constructed by the Native owner thread after the pure-Python profile and
environment preflight has completed.
"""

from __future__ import annotations

import base64
from collections import deque
from dataclasses import dataclass
import hashlib
import math
import os
from pathlib import Path
import platform
import queue
import stat
import sys
import threading
import time
from types import MappingProxyType
from typing import Any, BinaryIO, Callable, Mapping, NoReturn, Protocol
import uuid

from .baseline import (
    BaselineError,
    LINUX_CAPABILITY_NAMES,
    ObservedRuntimeIdentity,
    RuntimeProfile,
    load_runtime_profile,
    preflight_runtime_profile,
)
from .protocol import (
    CLIENT_SERVICE_NAME,
    COMMAND_BURST_MAX,
    IPC_LINE_MAX,
    NATIVE_ABI_VERSION,
    NATIVE_EVENT_BYTES_MAX,
    NATIVE_EVENT_CAP,
    NATIVE_EVENT_BURST_MAX,
    NODE_SNAPSHOT_BYTES_MAX,
    NODE_SNAPSHOT_MAX,
    NODE_SNAPSHOT_PAGE_BYTES_MAX,
    NODE_SNAPSHOT_PAGE_MAX,
    NODE_SNAPSHOT_TTL_S,
    REMOTE_FRAME_MAX,
    RESPONSE_BURST_MAX,
    SERVICE_NAME,
    SOFTBUS_PACKAGE_NAME,
    WORKER_COMMAND_BYTES_MAX,
    WORKER_COMMAND_CAP,
    WORKER_EVENT_BYTES_MAX,
    WORKER_EVENT_CAP,
    WORKER_RESPONSE_BYTES_MAX,
    WORKER_RESPONSE_CAP,
    ProtocolError,
    WorkerCommand,
    canonical_json_bytes,
    decode_strict_base64,
    encode_ipc_object,
    parse_worker_command,
)


_PROFILE_ENV = "MCLAW_DSOFTBUS_PROFILE"
_PROFILE_SHA_ENV = "MCLAW_DSOFTBUS_EXPECTED_PROFILE_SHA256"
_TOKEN_HASH_ENV = "MCLAW_DSOFTBUS_TOKEN_ID_HASH"
_TOKEN_PROCESS_ENV = "MCLAW_DSOFTBUS_TOKEN_PROCESS_NAME"
_PROBE_PID_ENV = "MCLAW_DSOFTBUS_PROBE_PID"
_HEX64 = frozenset("0123456789abcdef")
_SHA256_TAG_PREFIX = "sha256:"
_STOP = object()
_DSB_OK = 0
_DSB_E_INVALID = -1
_DSB_E_NATIVE = -2
_DSB_E_TIMEOUT = -3
_DSB_E_CLOSED = -4
_DSB_E_OVERFLOW = -5
_DSB_E_TOO_LARGE = -6
_DSB_E_INCOMPATIBLE = -7
_DSB_E_BUSY = -8
_DSB_EVENT_NONE = 0
_DSB_EVENT_NODE_ONLINE = 1
_DSB_EVENT_NODE_OFFLINE = 2
_DSB_EVENT_BOUND = 3
_DSB_EVENT_BYTES = 4
_DSB_EVENT_CLOSED = 5
_DSB_EVENT_OVERFLOW = 6
_DSB_EVENT_FATAL = 7
_DSB_EVENT_DEVICE_DISCOVERED = 8
_DSB_EVENT_DEVICE_DISCOVERY_FAILED = 9
_DSB_EVENT_DEVICE_BIND_RESULT = 10
_POLL_MAX_MS = 50
_EVENT_OVERFLOW_RESERVE_BYTES = IPC_LINE_MAX
_DEVICE_MANAGER_DEVICE_MAX = 256
_DEVICE_MANAGER_DEVICE_ID_MAX = 96
_DEVICE_MANAGER_DEVICE_NAME_MAX = 127
_DEVICE_MANAGER_NETWORK_ID_MAX = 96

class WorkerFailure(RuntimeError):
    """A stable, non-sensitive worker failure."""

    def __init__(self, code: str, *, native_code: int = 0) -> None:
        super().__init__(code)
        self.code = code
        self.native_code = native_code


def _fail(code: str, *, native_code: int = 0) -> NoReturn:
    raise WorkerFailure(code, native_code=native_code)


def _is_hex64(value: str) -> bool:
    return len(value) == 64 and all(character in _HEX64 for character in value)


def _read_small_text(path: Path, *, maximum: int = 65_536) -> str:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise WorkerFailure("IDENTITY_READ_FAILED") from error
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode) or status.st_size > maximum:
            _fail("IDENTITY_READ_FAILED")
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > maximum:
            _fail("IDENTITY_READ_FAILED")
    finally:
        os.close(descriptor)
    try:
        return raw.decode("utf-8").rstrip("\x00\r\n")
    except UnicodeDecodeError as error:
        raise WorkerFailure("IDENTITY_READ_FAILED") from error


def _capability_set() -> tuple[str, ...]:
    status = _read_small_text(Path("/proc/self/status"))
    values: dict[str, str] = {}
    for line in status.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            values[key] = value.strip()
    encoded = values.get("CapEff", "")
    if not encoded or any(character not in "0123456789abcdefABCDEF" for character in encoded):
        _fail("CAPABILITY_ATTESTATION_FAILED")
    mask = int(encoded, 16)
    if mask >> len(LINUX_CAPABILITY_NAMES):
        _fail("CAPABILITY_ATTESTATION_FAILED")
    return tuple(
        name
        for bit, name in enumerate(LINUX_CAPABILITY_NAMES)
        if mask & (1 << bit)
    )


def _supplementary_gids() -> tuple[int, ...]:
    values = os.getgroups()
    if any(type(value) is not int or value < 0 or value > 2**32 - 1 for value in values):
        _fail("GROUP_ATTESTATION_FAILED")
    if len(values) != len(set(values)):
        _fail("GROUP_ATTESTATION_FAILED")
    return tuple(sorted(values))


def _validate_environment(profile: RuntimeProfile) -> None:
    closure = profile.document["runtimeClosure"]
    expected_loader_path = (
        str(Path(closure["libraries"]["systemLibcxx"]["path"]).parent),
        str(Path(closure["libraries"]["shim"]["path"]).parent),
        *closure["python"]["releaseLibraryDirs"],
    )
    actual_loader = os.environ.get("LD_LIBRARY_PATH", "")
    actual_parts = tuple(actual_loader.split(":"))
    if actual_parts != expected_loader_path:
        _fail("LOADER_ENVIRONMENT_MISMATCH")
    if any(
        not part.startswith("/")
        or part in {"/", ".", ".."}
        or "/../" in part
        or "/./" in part
        for part in actual_parts
    ) or len(actual_parts) != len(set(actual_parts)):
        _fail("LOADER_ENVIRONMENT_MISMATCH")
    if os.environ.get("LD_PRELOAD") not in {None, ""}:
        _fail("LOADER_ENVIRONMENT_MISMATCH")
    for key, value in os.environ.items():
        if key.startswith("PYTHON") and value:
            _fail("PYTHON_ENVIRONMENT_MISMATCH")
    if sys.flags.isolated != 1 or sys.flags.no_site != 1:
        _fail("PYTHON_ENVIRONMENT_MISMATCH")


def _load_profile() -> RuntimeProfile:
    profile_text = os.environ.get(_PROFILE_ENV, "")
    expected_sha = os.environ.get(_PROFILE_SHA_ENV, "")
    if not profile_text.startswith("/") or not _is_hex64(expected_sha):
        _fail("PROFILE_ENVIRONMENT_INVALID")
    observed = ObservedRuntimeIdentity(
        api_level=23,
        abi="arm64-v8a",
        machine=platform.machine(),
        uid=os.getuid(),
        gid=os.getgid(),
        selinux_context=_read_small_text(Path("/proc/self/attr/current"), maximum=256),
    )
    try:
        preflight_runtime_profile(
            profile_text,
            expected_profile_sha256=expected_sha,
            observed=observed,
        )
        profile = load_runtime_profile(profile_text)
    except BaselineError as error:
        raise WorkerFailure(error.code) from error
    if profile.sha256 != expected_sha:
        _fail("PROFILE_HASH_MISMATCH")
    _validate_environment(profile)
    return profile


def _hash_regular(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise WorkerFailure("WORKER_MAPS_MISMATCH") from error
    digest = hashlib.sha256()
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            _fail("WORKER_MAPS_MISMATCH")
        while True:
            block = os.read(descriptor, 1_048_576)
            if not block:
                break
            digest.update(block)
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            _fail("WORKER_MAPS_MISMATCH")
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _mapped_files() -> dict[str, int]:
    text = _read_small_text(Path("/proc/self/maps"), maximum=4_194_304)
    result: dict[str, int] = {}
    for line in text.splitlines():
        parts = line.split(maxsplit=5)
        if len(parts) != 6:
            continue
        pathname = parts[5]
        if not pathname.startswith("/") or pathname.endswith(" (deleted)"):
            continue
        try:
            resolved = str(Path(pathname).resolve(strict=True))
        except OSError as error:
            raise WorkerFailure("WORKER_MAPS_MISMATCH") from error
        result[resolved] = result.get(resolved, 0) + 1
    return result


def _maps_gate(
    profile: RuntimeProfile,
    *,
    include_softbus: bool,
    include_device_manager: bool = False,
) -> tuple[str, tuple[Mapping[str, Any], ...]]:
    closure = profile.document["runtimeClosure"]
    required: dict[str, Mapping[str, Any]] = {
        "bundledLibcxx": closure["libraries"]["bundledLibcxx"],
        "permission": closure["libraries"]["permission"],
        "shim": closure["libraries"]["shim"],
        "systemLibcxx": closure["libraries"]["systemLibcxx"],
    }
    if include_softbus:
        softbus = closure.get("softbus") or profile.document["softbus"]["library"]
        required["softbus"] = {
            "resolvedPath": softbus.get("resolvedPath", softbus["path"]),
            "sha256": softbus["sha256"],
        }
    if include_device_manager:
        required["cjBindFfi"] = closure["libraries"]["cjBindFfi"]
        required["cjBindNative"] = closure["libraries"]["cjBindNative"]
        required["deviceManagerFfi"] = closure["libraries"]["deviceManagerFfi"]
    maps = _mapped_files()
    records: list[Mapping[str, Any]] = []
    for label in sorted(required):
        artifact = required[label]
        expected_path = str(Path(artifact["resolvedPath"]).resolve(strict=True))
        if expected_path not in maps:
            _fail("WORKER_MAPS_MISMATCH")
        actual_sha = _hash_regular(Path(expected_path))
        if actual_sha != artifact["sha256"]:
            _fail("WORKER_MAPS_MISMATCH")
        records.append(
            MappingProxyType(
                {
                    "mapSegments": maps[expected_path],
                    "name": label,
                    "path": expected_path,
                    "sha256": actual_sha,
                }
            )
        )
    release_libcxx = str(
        Path(closure["libraries"]["releaseLibcxx"]["resolvedPath"]).resolve(
            strict=True
        )
    )
    if release_libcxx in maps:
        _fail("WORKER_ABI_CONTAMINATED")
    rendered = canonical_json_bytes([dict(record) for record in records])
    return hashlib.sha256(rendered).hexdigest(), tuple(records)


@dataclass(frozen=True, slots=True)
class NativeNode:
    network_id: str
    device_name: str
    device_type_id: int


@dataclass(frozen=True, slots=True)
class NativeSnapshot:
    nodes: tuple[NativeNode, ...]
    replay_after_seq: int
    replay_through_seq: int


@dataclass(frozen=True, slots=True)
class NativeTrustedDevice:
    device_id: str
    device_name: str
    device_type_id: int
    network_id: str


@dataclass(frozen=True, slots=True)
class NativeEvent:
    event_type: str
    socket: int = -1
    status: int = _DSB_OK
    native_code: int = 0
    stable_code: str = ""
    mtu: int = 0
    node_event_seq: int = 0
    network_id: str = ""
    device_name: str = ""
    device_type_id: int = 0
    data: bytes = b""
    dropped_count: int = 0
    dropped_bytes: int = 0


def _bounded_text(
    value: Any, label: str, *, minimum: int, maximum: int
) -> str:
    if not isinstance(value, str) or "\x00" in value:
        _fail("NATIVE_DATA_INVALID")
    try:
        byte_length = len(value.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise WorkerFailure("NATIVE_DATA_INVALID") from error
    if byte_length < minimum or byte_length > maximum:
        _fail("NATIVE_DATA_INVALID")
    return value


def _bounded_int(value: Any, minimum: int, maximum: int) -> int:
    if type(value) is not int or value < minimum or value > maximum:
        _fail("NATIVE_DATA_INVALID")
    return value


def _native_failure_code(status: int, operation: str) -> str:
    if status == _DSB_E_BUSY:
        return "CAPACITY_BUSY"
    if status == _DSB_E_TOO_LARGE:
        return "FRAME_TOO_LARGE"
    if status == _DSB_E_INCOMPATIBLE:
        return "BINDING_INCOMPATIBLE"
    if status == _DSB_E_TIMEOUT:
        return "NATIVE_TIMEOUT"
    if status == _DSB_E_CLOSED:
        return "SOCKET_CLOSED"
    if status == _DSB_E_OVERFLOW and operation == "snapshot_nodes":
        return "NODE_SNAPSHOT_OVERFLOW"
    if status == _DSB_E_OVERFLOW:
        return "NATIVE_OVERFLOW"
    return "NATIVE_ERROR"


def _raise_native_status(status: int, native_code: int, operation: str) -> None:
    if status == _DSB_OK:
        return
    if type(status) is not int or status < -(2**31) or status > 2**31 - 1:
        _fail("NATIVE_STATUS_INVALID")
    _fail(_native_failure_code(status, operation), native_code=native_code)


class NativeBackend(Protocol):
    maps_sha256: str

    def hello_result(self) -> Mapping[str, Any]: ...

    def start(self) -> Mapping[str, Any]: ...

    def snapshot_nodes(self) -> NativeSnapshot: ...

    def get_node_udid(self, network_id: str) -> str: ...

    def start_device_discovery(self) -> None: ...

    def stop_device_discovery(
        self,
    ) -> tuple[tuple[NativeTrustedDevice, ...], int]: ...

    def begin_device_bind(self, device_id_sha256: str) -> None: ...

    def device_bind_status(self, device_id_sha256: str) -> tuple[str, int]: ...

    def list_trusted_devices(self) -> tuple[NativeTrustedDevice, ...]: ...

    def unbind_device(self, network_id: str) -> str: ...

    def listen(self, service_name: str) -> int: ...

    def connect(
        self,
        local_service_name: str,
        peer_service_name: str,
        network_id: str,
    ) -> tuple[int, int]: ...

    def send_bytes(self, socket: int, data: bytes) -> int: ...

    def close_socket(self, socket: int) -> None: ...

    def poll(self, timeout_ms: int) -> NativeEvent | None: ...

    def stop(self) -> Mapping[str, Any]: ...


class RealNativeBackend:
    """ctypes owner for one worker epoch."""

    def __init__(self, profile: RuntimeProfile, worker_epoch: str) -> None:
        if threading.current_thread().name != "mclaw-dsoftbus-native-owner":
            _fail("NATIVE_OWNER_THREAD_MISMATCH")
        import ctypes

        self._ctypes = ctypes
        self._profile = profile
        self._worker_epoch = worker_epoch
        self._owner_ident = threading.get_ident()
        self._started = False
        self._stopped = False
        self._context = ctypes.c_void_p()
        self._library: Any | None = None
        self._device_manager_maps_verified = False
        self._device_discovery_active = False
        self._device_discovery_failure: int | None = None
        self._discovered_devices: dict[str, NativeTrustedDevice] = {}
        self._device_bind_results: dict[str, tuple[str, int]] = {}
        self._sockets: set[int] = set()
        self._closing_sockets: set[int] = set()
        self._local_udid = ""
        self._identity: Mapping[str, Any] = MappingProxyType({})
        self.maps_sha256 = ""
        try:
            self._load()
        except Exception:
            self._destroy()
            raise

    def _assert_owner(self) -> None:
        if (
            threading.get_ident() != self._owner_ident
            or threading.current_thread().name != "mclaw-dsoftbus-native-owner"
        ):
            _fail("NATIVE_OWNER_THREAD_MISMATCH")

    def _require_active(self, *, started: bool = True) -> None:
        self._assert_owner()
        if self._stopped or not self._context.value:
            _fail("WORKER_STOPPED")
        if started and not self._started:
            _fail("INVALID_WORKER_STATE")

    def _load(self) -> None:
        ctypes = self._ctypes
        closure = self._profile.document["runtimeClosure"]
        shim = closure["libraries"]["shim"]["resolvedPath"]
        mode = getattr(os, "RTLD_NOW", 2) | getattr(os, "RTLD_LOCAL", 0)
        try:
            library = ctypes.CDLL(shim, mode=mode)
        except OSError as error:
            raise WorkerFailure("NATIVE_LOAD_FAILED") from error
        self._library = library
        self._bind_abi()
        _maps_gate(self._profile, include_softbus=False)
        if library.dsb_abi_version() != NATIVE_ABI_VERSION:
            _fail("NATIVE_ABI_MISMATCH")

        identity = self._identity_type()
        native_code = ctypes.c_int32()
        status = library.dsb_attest_process_identity(
            ctypes.byref(identity), ctypes.byref(native_code)
        )
        sealed = closure["identity"]
        token_hash = os.environ.get(_TOKEN_HASH_ENV, "")
        if (
            status != 0
            or identity.uid != sealed["uid"]
            or identity.gid != sealed["gid"]
            or identity.token_id != 0
            or identity.distributed_data_sync_granted != 1
            or sealed["publicTokenIdAvailable"] is not False
            or token_hash != sealed["sealedTokenIdHash"]
            or os.environ.get(_TOKEN_PROCESS_ENV) != sealed["processName"]
        ):
            _fail("NATIVE_IDENTITY_MISMATCH", native_code=native_code.value)

        expected_groups = tuple(sealed["supplementaryGids"])
        expected_capabilities = tuple(sealed["capabilitySet"])
        groups = _supplementary_gids()
        capabilities = _capability_set()
        if groups != expected_groups or capabilities != expected_capabilities:
            _fail("NATIVE_IDENTITY_MISMATCH")
        self._identity = MappingProxyType(
            {
                "capabilitySet": list(capabilities),
                "distributedDataSyncGranted": True,
                "gid": identity.gid,
                "selinuxDomain": _read_small_text(
                    Path("/proc/self/attr/current"), maximum=256
                ),
                "supplementaryGids": list(groups),
                "tokenIdHash": token_hash,
                "uid": identity.uid,
            }
        )

        native_code = ctypes.c_int32()
        status = library.dsb_create(
            SOFTBUS_PACKAGE_NAME.encode("ascii"),
            NATIVE_EVENT_CAP,
            NATIVE_EVENT_BYTES_MAX,
            closure["softbusSocketCap"],
            ctypes.byref(self._context),
            ctypes.byref(native_code),
        )
        if status != 0 or not self._context.value:
            _fail("NATIVE_CREATE_FAILED", native_code=native_code.value)
        self.maps_sha256, _ = _maps_gate(self._profile, include_softbus=True)

        buffer = ctypes.create_string_buffer(65)
        native_code = ctypes.c_int32()
        status = library.dsb_probe_local_udid(
            buffer, len(buffer), ctypes.byref(native_code)
        )
        if status != 0:
            _fail("LOCAL_UDID_PROBE_FAILED", native_code=native_code.value)
        raw_udid = bytes(buffer).split(b"\0", 1)[0]
        try:
            local_udid = raw_udid.decode("utf-8")
        except UnicodeDecodeError as error:
            raise WorkerFailure("LOCAL_UDID_PROBE_FAILED") from error
        if not 1 <= len(raw_udid) <= 64 or "\x00" in local_udid:
            _fail("LOCAL_UDID_PROBE_FAILED")
        self._local_udid = local_udid

    def _bind_abi(self) -> None:
        ctypes = self._ctypes
        library = self._library
        assert library is not None
        self._identity_type = _make_dsb_process_identity_type(ctypes)
        if (
            ctypes.sizeof(self._identity_type) != 24
            or self._identity_type.uid.offset != 0
            or self._identity_type.gid.offset != 4
            or self._identity_type.token_id.offset != 8
            or self._identity_type.distributed_data_sync_granted.offset != 16
            or self._identity_type.reserved.offset != 17
        ):
            _fail("NATIVE_ABI_LAYOUT_MISMATCH")
        self._node_type = _make_dsb_node_type(ctypes)
        if (
            ctypes.sizeof(self._node_type) != 196
            or self._node_type.network_id.offset != 0
            or self._node_type.device_name.offset != 65
            or self._node_type.device_type_id.offset != 194
        ):
            _fail("NATIVE_ABI_LAYOUT_MISMATCH")
        self._trusted_device_type = _make_dsb_trusted_device_type(ctypes)
        if (
            ctypes.sizeof(self._trusted_device_type) != 326
            or self._trusted_device_type.device_id.offset != 0
            or self._trusted_device_type.device_name.offset != 97
            or self._trusted_device_type.device_type_id.offset != 226
            or self._trusted_device_type.network_id.offset != 228
        ):
            _fail("NATIVE_ABI_LAYOUT_MISMATCH")
        library.dsb_abi_version.argtypes = []
        library.dsb_abi_version.restype = ctypes.c_uint32
        library.dsb_attest_process_identity.argtypes = [
            ctypes.POINTER(self._identity_type),
            ctypes.POINTER(ctypes.c_int32),
        ]
        library.dsb_attest_process_identity.restype = ctypes.c_int32
        library.dsb_probe_local_udid.argtypes = [
            ctypes.POINTER(ctypes.c_char),
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_int32),
        ]
        library.dsb_probe_local_udid.restype = ctypes.c_int32
        library.dsb_create.argtypes = [
            ctypes.c_char_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_int32),
        ]
        library.dsb_create.restype = ctypes.c_int32
        library.dsb_start_node_events.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int32),
        ]
        library.dsb_start_node_events.restype = ctypes.c_int32
        library.dsb_stop_node_events.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int32),
        ]
        library.dsb_stop_node_events.restype = ctypes.c_int32
        library.dsb_snapshot_nodes.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.POINTER(self._node_type)),
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.POINTER(ctypes.c_int32),
        ]
        library.dsb_snapshot_nodes.restype = ctypes.c_int32
        library.dsb_release_nodes.argtypes = [ctypes.POINTER(self._node_type)]
        library.dsb_release_nodes.restype = None
        library.dsb_get_node_udid.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.POINTER(ctypes.c_char),
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_int32),
        ]
        library.dsb_get_node_udid.restype = ctypes.c_int32
        library.dsb_start_device_discovery.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int32),
        ]
        library.dsb_start_device_discovery.restype = ctypes.c_int32
        library.dsb_stop_device_discovery.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int32),
        ]
        library.dsb_stop_device_discovery.restype = ctypes.c_int32
        library.dsb_list_discovered_devices.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.POINTER(self._trusted_device_type)),
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_int32),
            ctypes.POINTER(ctypes.c_int32),
        ]
        library.dsb_list_discovered_devices.restype = ctypes.c_int32
        library.dsb_begin_device_bind.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.POINTER(ctypes.c_int32),
        ]
        library.dsb_begin_device_bind.restype = ctypes.c_int32
        library.dsb_list_trusted_devices.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.POINTER(self._trusted_device_type)),
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_int32),
        ]
        library.dsb_list_trusted_devices.restype = ctypes.c_int32
        library.dsb_release_trusted_devices.argtypes = [
            ctypes.POINTER(self._trusted_device_type)
        ]
        library.dsb_release_trusted_devices.restype = None
        library.dsb_unbind_device.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.POINTER(ctypes.c_int32),
        ]
        library.dsb_unbind_device.restype = ctypes.c_int32
        library.dsb_listen.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.POINTER(ctypes.c_int32),
            ctypes.POINTER(ctypes.c_int32),
        ]
        library.dsb_listen.restype = ctypes.c_int32
        library.dsb_connect.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.POINTER(ctypes.c_int32),
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_int32),
        ]
        library.dsb_connect.restype = ctypes.c_int32
        library.dsb_get_mtu_size.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int32,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_int32),
        ]
        library.dsb_get_mtu_size.restype = ctypes.c_int32
        library.dsb_send_bytes.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int32,
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_int32),
        ]
        library.dsb_send_bytes.restype = ctypes.c_int32
        library.dsb_close_socket.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int32,
            ctypes.POINTER(ctypes.c_int32),
        ]
        library.dsb_close_socket.restype = ctypes.c_int32
        library.dsb_poll.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        library.dsb_poll.restype = ctypes.c_int32
        for name, restype in (
            ("dsb_event_type", ctypes.c_int32),
            ("dsb_event_socket", ctypes.c_int32),
            ("dsb_event_code", ctypes.c_int32),
            ("dsb_event_native_code", ctypes.c_int32),
            ("dsb_event_mtu", ctypes.c_uint32),
            ("dsb_event_node_seq", ctypes.c_uint64),
            ("dsb_event_device_id", ctypes.c_void_p),
            ("dsb_event_network_id", ctypes.c_void_p),
            ("dsb_event_device_name", ctypes.c_void_p),
            ("dsb_event_device_type_id", ctypes.c_uint16),
            ("dsb_event_data", ctypes.c_void_p),
            ("dsb_event_data_len", ctypes.c_uint32),
        ):
            function = getattr(library, name)
            function.argtypes = [ctypes.c_void_p]
            function.restype = restype
        library.dsb_release_event.argtypes = [ctypes.c_void_p]
        library.dsb_release_event.restype = None
        library.dsb_destroy.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
        library.dsb_destroy.restype = None

    def hello_result(self) -> Mapping[str, Any]:
        self._require_active(started=False)
        return {
            "identity": dict(self._identity),
            "localUdid": self._local_udid,
            "nativeAbiVersion": NATIVE_ABI_VERSION,
            "socketCap": self._profile.document["runtimeClosure"]["softbusSocketCap"],
            "workerEpoch": self._worker_epoch,
        }

    def start(self) -> Mapping[str, Any]:
        self._require_active(started=False)
        if not self._started:
            native_code = self._ctypes.c_int32()
            status = self._library.dsb_start_node_events(
                self._context, self._ctypes.byref(native_code)
            )
            if status != _DSB_OK:
                _fail("NATIVE_START_FAILED", native_code=native_code.value)
            self._started = True
        return {"nodeEventsStarted": True}

    def _decode_node_field(
        self,
        node: Any,
        *,
        offset: int,
        capacity: int,
        minimum: int,
        maximum: int,
    ) -> str:
        raw = self._ctypes.string_at(
            self._ctypes.addressof(node) + offset, capacity
        )
        terminator = raw.find(b"\0")
        if terminator < 0:
            _fail("NATIVE_DATA_INVALID")
        try:
            value = raw[:terminator].decode("utf-8")
        except UnicodeDecodeError as error:
            raise WorkerFailure("NATIVE_DATA_INVALID") from error
        return _bounded_text(
            value,
            "native node field",
            minimum=minimum,
            maximum=maximum,
        )

    def _decode_event_text(
        self,
        pointer: int | None,
        *,
        capacity: int,
        minimum: int,
        maximum: int,
    ) -> str:
        if not pointer:
            if minimum == 0:
                return ""
            _fail("NATIVE_EVENT_INVALID")
        raw = bytearray()
        for offset in range(capacity):
            byte = self._ctypes.c_ubyte.from_address(pointer + offset).value
            if byte == 0:
                break
            raw.append(byte)
        else:
            _fail("NATIVE_EVENT_INVALID")
        try:
            value = bytes(raw).decode("utf-8")
        except UnicodeDecodeError as error:
            raise WorkerFailure("NATIVE_EVENT_INVALID") from error
        return _bounded_text(
            value,
            "native event text",
            minimum=minimum,
            maximum=maximum,
        )

    def snapshot_nodes(self) -> NativeSnapshot:
        self._require_active()
        ctypes = self._ctypes
        nodes = ctypes.POINTER(self._node_type)()
        count = ctypes.c_uint32()
        replay_after = ctypes.c_uint64()
        replay_through = ctypes.c_uint64()
        native_code = ctypes.c_int32()
        status = self._library.dsb_snapshot_nodes(
            self._context,
            ctypes.byref(nodes),
            ctypes.byref(count),
            ctypes.byref(replay_after),
            ctypes.byref(replay_through),
            ctypes.byref(native_code),
        )
        try:
            _raise_native_status(status, native_code.value, "snapshot_nodes")
            if count.value > NODE_SNAPSHOT_MAX or (
                count.value > 0 and not bool(nodes)
            ):
                _fail("NODE_SNAPSHOT_OVERFLOW")
            converted: list[NativeNode] = []
            for index in range(count.value):
                node = nodes[index]
                converted.append(
                    NativeNode(
                        network_id=self._decode_node_field(
                            node,
                            offset=self._node_type.network_id.offset,
                            capacity=65,
                            minimum=1,
                            maximum=64,
                        ),
                        device_name=self._decode_node_field(
                            node,
                            offset=self._node_type.device_name.offset,
                            capacity=128,
                            minimum=0,
                            maximum=127,
                        ),
                        device_type_id=int(node.device_type_id),
                    )
                )
            return NativeSnapshot(
                nodes=tuple(converted),
                replay_after_seq=int(replay_after.value),
                replay_through_seq=int(replay_through.value),
            )
        finally:
            if bool(nodes):
                self._library.dsb_release_nodes(nodes)

    def get_node_udid(self, network_id: str) -> str:
        self._require_active()
        network_id = _bounded_text(
            network_id, "networkId", minimum=1, maximum=64
        )
        buffer = self._ctypes.create_string_buffer(65)
        native_code = self._ctypes.c_int32()
        status = self._library.dsb_get_node_udid(
            self._context,
            network_id.encode("utf-8"),
            buffer,
            len(buffer),
            self._ctypes.byref(native_code),
        )
        _raise_native_status(status, native_code.value, "get_node_udid")
        raw = bytes(buffer).split(b"\0", 1)[0]
        try:
            value = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise WorkerFailure("NATIVE_DATA_INVALID") from error
        return _bounded_text(value, "udid", minimum=1, maximum=64)

    def _verify_device_manager_maps(self) -> None:
        if not self._device_manager_maps_verified:
            self.maps_sha256, _ = _maps_gate(
                self._profile,
                include_softbus=True,
                include_device_manager=True,
            )
            self._device_manager_maps_verified = True

    def _device_snapshot(
        self, *, discovered: bool
    ) -> tuple[tuple[NativeTrustedDevice, ...], int]:
        self._require_active()
        ctypes = self._ctypes
        devices = ctypes.POINTER(self._trusted_device_type)()
        count = ctypes.c_uint32()
        failure_code = ctypes.c_int32()
        native_code = ctypes.c_int32()
        if discovered:
            status = self._library.dsb_list_discovered_devices(
                self._context,
                ctypes.byref(devices),
                ctypes.byref(count),
                ctypes.byref(failure_code),
                ctypes.byref(native_code),
            )
        else:
            status = self._library.dsb_list_trusted_devices(
                self._context,
                ctypes.byref(devices),
                ctypes.byref(count),
                ctypes.byref(native_code),
            )
        try:
            _raise_native_status(
                status,
                native_code.value,
                "list_discovered_devices" if discovered else "list_trusted_devices",
            )
            if count.value > _DEVICE_MANAGER_DEVICE_MAX or (
                count.value > 0 and not bool(devices)
            ):
                _fail("DEVICE_MANAGER_DATA_INVALID")
            converted: list[NativeTrustedDevice] = []
            seen: set[str] = set()
            for index in range(count.value):
                item = devices[index]
                device_id = self._decode_node_field(
                    item,
                    offset=self._trusted_device_type.device_id.offset,
                    capacity=97,
                    minimum=1,
                    maximum=_DEVICE_MANAGER_DEVICE_ID_MAX,
                )
                if device_id in seen:
                    _fail("DEVICE_TARGET_AMBIGUOUS")
                seen.add(device_id)
                converted.append(
                    NativeTrustedDevice(
                        device_id=device_id,
                        device_name=self._decode_node_field(
                            item,
                            offset=self._trusted_device_type.device_name.offset,
                            capacity=128,
                            minimum=0,
                            maximum=_DEVICE_MANAGER_DEVICE_NAME_MAX,
                        ),
                        device_type_id=_bounded_int(
                            int(item.device_type_id), 0, 2**16 - 1
                        ),
                        network_id=self._decode_node_field(
                            item,
                            offset=self._trusted_device_type.network_id.offset,
                            capacity=97,
                            minimum=0 if discovered else 1,
                            maximum=_DEVICE_MANAGER_NETWORK_ID_MAX,
                        ),
                    )
                )
            return tuple(converted), int(failure_code.value)
        finally:
            if bool(devices):
                self._library.dsb_release_trusted_devices(devices)

    def start_device_discovery(self) -> None:
        self._require_active()
        if self._device_discovery_active:
            _fail("DEVICE_DISCOVERY_BUSY")
        self._discovered_devices.clear()
        self._device_discovery_failure = None
        native_code = self._ctypes.c_int32()
        status = self._library.dsb_start_device_discovery(
            self._context, self._ctypes.byref(native_code)
        )
        _raise_native_status(status, native_code.value, "start_device_discovery")
        try:
            self._verify_device_manager_maps()
        except Exception:
            ignored = self._ctypes.c_int32()
            self._library.dsb_stop_device_discovery(
                self._context, self._ctypes.byref(ignored)
            )
            raise
        self._device_discovery_active = True

    def stop_device_discovery(
        self,
    ) -> tuple[tuple[NativeTrustedDevice, ...], int]:
        self._require_active()
        if not self._device_discovery_active:
            _fail("DEVICE_DISCOVERY_INACTIVE")
        native_code = self._ctypes.c_int32()
        status = self._library.dsb_stop_device_discovery(
            self._context, self._ctypes.byref(native_code)
        )
        self._device_discovery_active = False
        _raise_native_status(status, native_code.value, "stop_device_discovery")
        devices, failure_code = self._device_snapshot(discovered=True)
        self._discovered_devices = {
            hashlib.sha256(device.device_id.encode("utf-8")).hexdigest(): device
            for device in devices
        }
        self._device_discovery_failure = failure_code or None
        return devices, failure_code

    def begin_device_bind(self, device_id_sha256: str) -> None:
        self._require_active()
        if not _is_hex64(device_id_sha256):
            _fail("INVALID_REQUEST")
        target = self._discovered_devices.get(device_id_sha256)
        if target is None:
            _fail("DEVICE_NOT_FOUND")
        current = self._device_bind_results.get(device_id_sha256)
        if current is not None and current[0] == "pending":
            _fail("DEVICE_BIND_BUSY")
        native_code = self._ctypes.c_int32()
        status = self._library.dsb_begin_device_bind(
            self._context,
            target.device_id.encode("utf-8"),
            self._ctypes.byref(native_code),
        )
        _raise_native_status(status, native_code.value, "begin_device_bind")
        self._device_bind_results[device_id_sha256] = ("pending", 0)

    def device_bind_status(self, device_id_sha256: str) -> tuple[str, int]:
        self._require_active()
        if not _is_hex64(device_id_sha256):
            _fail("INVALID_REQUEST")
        value = self._device_bind_results.get(device_id_sha256)
        if value is None:
            _fail("DEVICE_BIND_NOT_FOUND")
        return value

    def list_trusted_devices(self) -> tuple[NativeTrustedDevice, ...]:
        devices, _ = self._device_snapshot(discovered=False)
        self._verify_device_manager_maps()
        return devices

    def unbind_device(self, network_id: str) -> str:
        self._require_active()
        normalized = _bounded_text(
            network_id,
            "deviceManager.networkId",
            minimum=1,
            maximum=_DEVICE_MANAGER_NETWORK_ID_MAX,
        )
        matches = [
            device
            for device in self.list_trusted_devices()
            if device.network_id == normalized
        ]
        if not matches:
            _fail("DEVICE_NOT_FOUND")
        if len(matches) != 1:
            _fail("DEVICE_TARGET_AMBIGUOUS")
        target = matches[0]
        native_code = self._ctypes.c_int32()
        status = self._library.dsb_unbind_device(
            self._context,
            target.device_id.encode("utf-8"),
            self._ctypes.byref(native_code),
        )
        _raise_native_status(status, native_code.value, "unbind_device")
        return hashlib.sha256(target.device_id.encode("utf-8")).hexdigest()

    def listen(self, service_name: str) -> int:
        self._require_active()
        if service_name != SERVICE_NAME:
            _fail("INVALID_SERVICE_NAME")
        socket = self._ctypes.c_int32(-1)
        native_code = self._ctypes.c_int32()
        status = self._library.dsb_listen(
            self._context,
            service_name.encode("ascii"),
            self._ctypes.byref(socket),
            self._ctypes.byref(native_code),
        )
        _raise_native_status(status, native_code.value, "listen")
        handle = _bounded_int(socket.value, 0, 2**31 - 1)
        if handle in self._sockets or handle in self._closing_sockets:
            _fail("NATIVE_SOCKET_REUSED")
        self._sockets.add(handle)
        return handle

    def connect(
        self,
        local_service_name: str,
        peer_service_name: str,
        network_id: str,
    ) -> tuple[int, int]:
        self._require_active()
        if (
            local_service_name != CLIENT_SERVICE_NAME
            or peer_service_name != SERVICE_NAME
        ):
            _fail("INVALID_SERVICE_NAME")
        network_id = _bounded_text(
            network_id, "networkId", minimum=1, maximum=64
        )
        socket = self._ctypes.c_int32(-1)
        mtu = self._ctypes.c_uint32()
        native_code = self._ctypes.c_int32()
        status = self._library.dsb_connect(
            self._context,
            local_service_name.encode("ascii"),
            peer_service_name.encode("ascii"),
            network_id.encode("utf-8"),
            self._ctypes.byref(socket),
            self._ctypes.byref(mtu),
            self._ctypes.byref(native_code),
        )
        _raise_native_status(status, native_code.value, "connect")
        handle = _bounded_int(socket.value, 0, 2**31 - 1)
        negotiated_mtu = _bounded_int(mtu.value, 1, 2**32 - 1)
        if handle in self._sockets or handle in self._closing_sockets:
            _fail("NATIVE_SOCKET_REUSED")
        self._sockets.add(handle)
        return handle, negotiated_mtu

    def send_bytes(self, socket: int, data: bytes) -> int:
        self._require_active()
        socket = _bounded_int(socket, 0, 2**31 - 1)
        if socket not in self._sockets:
            _fail("SOCKET_NOT_OWNED")
        if not isinstance(data, bytes) or not 1 <= len(data) <= REMOTE_FRAME_MAX:
            _fail("FRAME_TOO_LARGE")
        native_code = self._ctypes.c_int32()
        native_buffer = (self._ctypes.c_uint8 * len(data)).from_buffer_copy(data)
        status = self._library.dsb_send_bytes(
            self._context,
            socket,
            native_buffer,
            len(data),
            self._ctypes.byref(native_code),
        )
        _raise_native_status(status, native_code.value, "send_bytes")
        return len(data)

    def close_socket(self, socket: int) -> None:
        self._require_active()
        socket = _bounded_int(socket, 0, 2**31 - 1)
        if socket not in self._sockets:
            _fail("SOCKET_NOT_OWNED")
        native_code = self._ctypes.c_int32()
        status = self._library.dsb_close_socket(
            self._context, socket, self._ctypes.byref(native_code)
        )
        _raise_native_status(status, native_code.value, "close_socket")
        self._sockets.remove(socket)
        self._closing_sockets.add(socket)

    def poll(self, timeout_ms: int) -> NativeEvent | None:
        self._require_active()
        timeout_ms = _bounded_int(timeout_ms, 0, _POLL_MAX_MS)
        ctypes = self._ctypes
        pointer = ctypes.c_void_p()
        status = self._library.dsb_poll(
            self._context, timeout_ms, ctypes.byref(pointer)
        )
        if status == _DSB_E_TIMEOUT:
            if pointer.value:
                _fail("NATIVE_EVENT_INVALID")
            return None
        _raise_native_status(status, 0, "poll")
        if not pointer.value:
            _fail("NATIVE_EVENT_INVALID")
        try:
            event_type = int(self._library.dsb_event_type(pointer))
            socket = int(self._library.dsb_event_socket(pointer))
            event_status = int(self._library.dsb_event_code(pointer))
            native_code = int(self._library.dsb_event_native_code(pointer))
            mtu = int(self._library.dsb_event_mtu(pointer))
            node_seq = int(self._library.dsb_event_node_seq(pointer))
            if event_type == _DSB_EVENT_DEVICE_DISCOVERED:
                if (
                    socket != -1
                    or event_status != _DSB_OK
                    or native_code != 0
                    or mtu != 0
                    or node_seq != 0
                ):
                    _fail("NATIVE_EVENT_INVALID")
                self._decode_event_text(
                    self._library.dsb_event_device_id(pointer),
                    capacity=97,
                    minimum=1,
                    maximum=_DEVICE_MANAGER_DEVICE_ID_MAX,
                )
                self._decode_event_text(
                    self._library.dsb_event_device_name(pointer),
                    capacity=128,
                    minimum=0,
                    maximum=_DEVICE_MANAGER_DEVICE_NAME_MAX,
                )
                self._decode_event_text(
                    self._library.dsb_event_network_id(pointer),
                    capacity=97,
                    minimum=0,
                    maximum=_DEVICE_MANAGER_NETWORK_ID_MAX,
                )
                _bounded_int(
                    int(self._library.dsb_event_device_type_id(pointer)),
                    0,
                    2**16 - 1,
                )
                return None
            if event_type == _DSB_EVENT_DEVICE_DISCOVERY_FAILED:
                if (
                    socket != -1
                    or event_status != _DSB_E_NATIVE
                    or mtu != 0
                    or node_seq != 0
                ):
                    _fail("NATIVE_EVENT_INVALID")
                self._device_discovery_failure = _bounded_int(
                    native_code, -(2**31), 2**31 - 1
                )
                return None
            if event_type == _DSB_EVENT_DEVICE_BIND_RESULT:
                if socket != -1 or mtu != 0 or node_seq != 0:
                    _fail("NATIVE_EVENT_INVALID")
                device_id = self._decode_event_text(
                    self._library.dsb_event_device_id(pointer),
                    capacity=97,
                    minimum=1,
                    maximum=_DEVICE_MANAGER_DEVICE_ID_MAX,
                )
                digest = hashlib.sha256(device_id.encode("utf-8")).hexdigest()
                current = self._device_bind_results.get(digest)
                if current is None or current[0] != "pending":
                    _fail("NATIVE_EVENT_INVALID")
                if event_status == _DSB_OK and native_code == 0:
                    self._device_bind_results[digest] = ("bound", 0)
                elif event_status == _DSB_E_NATIVE:
                    self._device_bind_results[digest] = (
                        "failed",
                        _bounded_int(native_code, -(2**31), 2**31 - 1),
                    )
                else:
                    _fail("NATIVE_EVENT_INVALID")
                return None
            if event_type in {_DSB_EVENT_NODE_ONLINE, _DSB_EVENT_NODE_OFFLINE}:
                return NativeEvent(
                    event_type=(
                        "node-online"
                        if event_type == _DSB_EVENT_NODE_ONLINE
                        else "node-offline"
                    ),
                    node_event_seq=node_seq,
                    network_id=self._decode_event_text(
                        self._library.dsb_event_network_id(pointer),
                        capacity=65,
                        minimum=1,
                        maximum=64,
                    ),
                    device_name=self._decode_event_text(
                        self._library.dsb_event_device_name(pointer),
                        capacity=128,
                        minimum=0,
                        maximum=127,
                    ),
                    device_type_id=int(
                        self._library.dsb_event_device_type_id(pointer)
                    ),
                )
            if event_type == _DSB_EVENT_BOUND:
                network_id = self._decode_event_text(
                    self._library.dsb_event_network_id(pointer),
                    capacity=65,
                    minimum=1,
                    maximum=64,
                )
                if (
                    socket < 0
                    or socket in self._sockets
                    or socket in self._closing_sockets
                ):
                    _fail("NATIVE_EVENT_INVALID")
                if mtu == 0:
                    mtu_value = ctypes.c_uint32()
                    mtu_native_code = ctypes.c_int32()
                    mtu_status = self._library.dsb_get_mtu_size(
                        self._context,
                        socket,
                        ctypes.byref(mtu_value),
                        ctypes.byref(mtu_native_code),
                    )
                    _raise_native_status(
                        mtu_status, mtu_native_code.value, "get_mtu_size"
                    )
                    mtu = int(mtu_value.value)
                _bounded_int(mtu, 1, 2**32 - 1)
                self._closing_sockets.discard(socket)
                self._sockets.add(socket)
                return NativeEvent(
                    event_type="bound",
                    socket=socket,
                    mtu=mtu,
                    network_id=network_id,
                )
            if event_type == _DSB_EVENT_BYTES:
                length = int(self._library.dsb_event_data_len(pointer))
                data_pointer = self._library.dsb_event_data(pointer)
                if (
                    socket not in self._sockets
                    or not data_pointer
                    or length < 1
                    or length > REMOTE_FRAME_MAX
                ):
                    _fail("NATIVE_EVENT_INVALID")
                return NativeEvent(
                    event_type="bytes",
                    socket=socket,
                    data=ctypes.string_at(data_pointer, length),
                )
            if event_type == _DSB_EVENT_CLOSED:
                if socket not in self._sockets and socket not in self._closing_sockets:
                    _fail("NATIVE_EVENT_INVALID")
                self._sockets.discard(socket)
                self._closing_sockets.discard(socket)
                return NativeEvent(
                    event_type="closed",
                    socket=socket,
                    status=event_status,
                    native_code=native_code,
                )
            if event_type == _DSB_EVENT_OVERFLOW:
                return NativeEvent(
                    event_type="overflow",
                    status=event_status,
                    dropped_count=max(1, mtu),
                    dropped_bytes=node_seq,
                )
            if event_type == _DSB_EVENT_FATAL:
                if socket < -1:
                    _fail("NATIVE_EVENT_INVALID")
                return NativeEvent(
                    event_type="fatal",
                    socket=socket,
                    status=event_status,
                    native_code=native_code,
                )
            _fail("NATIVE_EVENT_INVALID")
        finally:
            self._library.dsb_release_event(pointer)

    def _destroy(self) -> None:
        if self._library is None or not self._context.value:
            return
        if self._started:
            native_code = self._ctypes.c_int32()
            self._library.dsb_stop_node_events(
                self._context, self._ctypes.byref(native_code)
            )
            self._started = False
        self._library.dsb_destroy(self._ctypes.byref(self._context))
        self._device_discovery_active = False
        self._discovered_devices.clear()
        self._device_bind_results.clear()
        self._sockets.clear()
        self._closing_sockets.clear()

    def stop(self) -> Mapping[str, Any]:
        self._assert_owner()
        if not self._stopped:
            self._destroy()
            self._stopped = True
        return {"stopped": True}


def _make_dsb_process_identity_type(ctypes_module: Any) -> Any:
    class DsbProcessIdentity(ctypes_module.Structure):
        _fields_ = [
            ("uid", ctypes_module.c_uint32),
            ("gid", ctypes_module.c_uint32),
            ("token_id", ctypes_module.c_uint64),
            ("distributed_data_sync_granted", ctypes_module.c_uint8),
            ("reserved", ctypes_module.c_uint8 * 7),
        ]

    return DsbProcessIdentity


def _make_dsb_node_type(ctypes_module: Any) -> Any:
    class DsbNode(ctypes_module.Structure):
        _fields_ = [
            ("network_id", ctypes_module.c_char * 65),
            ("device_name", ctypes_module.c_char * 128),
            ("device_type_id", ctypes_module.c_uint16),
        ]

    return DsbNode


def _make_dsb_trusted_device_type(ctypes_module: Any) -> Any:
    class DsbTrustedDevice(ctypes_module.Structure):
        _fields_ = [
            ("device_id", ctypes_module.c_char * 97),
            ("device_name", ctypes_module.c_char * 128),
            ("device_type_id", ctypes_module.c_uint16),
            ("network_id", ctypes_module.c_char * 97),
        ]

    return DsbTrustedDevice


@dataclass(frozen=True, slots=True)
class _QueuedCommand:
    command: WorkerCommand
    line_bytes: int


@dataclass(slots=True)
class _SnapshotState:
    snapshot_id: str
    nodes: tuple[Mapping[str, Any], ...]
    replay_after_seq: int
    replay_through_seq: int
    created_at: float
    next_index: int = 0
    expected_cursor: str | None = None


class _SnapshotPager:
    def __init__(
        self,
        *,
        worker_epoch: str,
        monotonic: Callable[[], float],
    ) -> None:
        self._worker_epoch = worker_epoch
        self._monotonic = monotonic
        self._state: _SnapshotState | None = None

    def _now(self) -> float:
        value = self._monotonic()
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            _fail("SNAPSHOT_CLOCK_INVALID")
        return float(value)

    def release(self) -> None:
        self._state = None

    def _normalize(self, snapshot: NativeSnapshot) -> tuple[Mapping[str, Any], ...]:
        if not isinstance(snapshot, NativeSnapshot):
            _fail("NATIVE_DATA_INVALID")
        _bounded_int(snapshot.replay_after_seq, 0, 2**64 - 1)
        _bounded_int(snapshot.replay_through_seq, 0, 2**64 - 1)
        if snapshot.replay_after_seq > snapshot.replay_through_seq:
            _fail("NATIVE_DATA_INVALID")
        if len(snapshot.nodes) > NODE_SNAPSHOT_MAX:
            _fail("NODE_SNAPSHOT_OVERFLOW")
        nodes: list[Mapping[str, Any]] = []
        for native_node in snapshot.nodes:
            if not isinstance(native_node, NativeNode):
                _fail("NATIVE_DATA_INVALID")
            nodes.append(
                MappingProxyType(
                    {
                        "deviceName": _bounded_text(
                            native_node.device_name,
                            "deviceName",
                            minimum=0,
                            maximum=127,
                        ),
                        "deviceTypeId": _bounded_int(
                            native_node.device_type_id, 0, 2**16 - 1
                        ),
                        "networkId": _bounded_text(
                            native_node.network_id,
                            "networkId",
                            minimum=1,
                            maximum=64,
                        ),
                    }
                )
            )
        if len(canonical_json_bytes([dict(node) for node in nodes])) > NODE_SNAPSHOT_BYTES_MAX:
            _fail("NODE_SNAPSHOT_OVERFLOW")
        return tuple(nodes)

    def begin(self, snapshot: NativeSnapshot) -> Mapping[str, Any]:
        self.release()
        nodes = self._normalize(snapshot)
        self._state = _SnapshotState(
            snapshot_id=str(uuid.uuid4()),
            nodes=nodes,
            replay_after_seq=snapshot.replay_after_seq,
            replay_through_seq=snapshot.replay_through_seq,
            created_at=self._now(),
        )
        return self._next_page(snapshot_id=self._state.snapshot_id, cursor=None)

    def continue_page(self, snapshot_id: str, cursor: str) -> Mapping[str, Any]:
        return self._next_page(snapshot_id=snapshot_id, cursor=cursor)

    def _cursor(self, state: _SnapshotState, next_index: int) -> str:
        material = (
            self._worker_epoch
            + "\0"
            + state.snapshot_id
            + "\0"
            + str(next_index)
        ).encode("utf-8")
        return hashlib.sha256(material).hexdigest()

    def _next_page(
        self, *, snapshot_id: str, cursor: str | None
    ) -> Mapping[str, Any]:
        state = self._state
        if state is None:
            _fail("SNAPSHOT_NOT_FOUND")
        current = self._now()
        if current < state.created_at:
            self.release()
            _fail("SNAPSHOT_CLOCK_INVALID")
        if current - state.created_at >= NODE_SNAPSHOT_TTL_S:
            self.release()
            _fail("SNAPSHOT_EXPIRED")
        if snapshot_id != state.snapshot_id or cursor != state.expected_cursor:
            _fail("SNAPSHOT_CURSOR_INVALID")
        start = state.next_index
        maximum_end = min(len(state.nodes), start + NODE_SNAPSHOT_PAGE_MAX)
        end = maximum_end
        result: Mapping[str, Any] | None = None
        while end >= start:
            next_cursor = self._cursor(state, end) if end < len(state.nodes) else ""
            candidate = {
                "nextCursor": next_cursor,
                "nodes": [dict(node) for node in state.nodes[start:end]],
                "replayAfterSeq": state.replay_after_seq,
                "replayThroughSeq": state.replay_through_seq,
                "snapshotId": state.snapshot_id,
            }
            if len(canonical_json_bytes(candidate)) <= NODE_SNAPSHOT_PAGE_BYTES_MAX:
                result = MappingProxyType(candidate)
                break
            end -= 1
        if result is None or (end == start and start < len(state.nodes)):
            self.release()
            _fail("NODE_SNAPSHOT_OVERFLOW")
        state.next_index = end
        state.expected_cursor = result["nextCursor"] or None
        if not result["nextCursor"]:
            self.release()
        return result


class _BoundedQueue:
    def __init__(self, *, count_cap: int, byte_cap: int) -> None:
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=count_cap)
        self._byte_cap = byte_cap
        self._bytes = 0
        self._lock = threading.Lock()

    def put(self, value: Any, size: int) -> None:
        if type(size) is not int or size < 0 or size > self._byte_cap:
            _fail("WORKER_QUEUE_CAPACITY")
        with self._lock:
            if self._bytes + size > self._byte_cap:
                _fail("WORKER_QUEUE_CAPACITY")
            self._bytes += size
        try:
            self._queue.put_nowait((value, size))
        except queue.Full as error:
            with self._lock:
                self._bytes -= size
            raise WorkerFailure("WORKER_QUEUE_CAPACITY") from error

    def get(self, timeout: float | None = None) -> tuple[Any, int]:
        value, size = self._queue.get(timeout=timeout)
        with self._lock:
            self._bytes -= size
            if self._bytes < 0:
                _fail("WORKER_QUEUE_ACCOUNTING")
        return value, size

    def get_nowait(self) -> tuple[Any, int]:
        value, size = self._queue.get_nowait()
        with self._lock:
            self._bytes -= size
            if self._bytes < 0:
                _fail("WORKER_QUEUE_ACCOUNTING")
        return value, size

    def empty(self) -> bool:
        return self._queue.empty()


class _OutputQueues:
    def __init__(self) -> None:
        self._responses: deque[bytes] = deque()
        self._events: deque[bytes] = deque()
        self._response_bytes = 0
        self._event_bytes = 0
        self._terminal_event_queued = False
        self._finished = False
        self._response_burst = 0
        self._condition = threading.Condition()

    def enqueue_response(self, raw: bytes) -> None:
        with self._condition:
            if self._finished:
                _fail("WORKER_OUTPUT_CLOSED")
            if (
                len(self._responses) >= WORKER_RESPONSE_CAP
                or len(raw) > WORKER_RESPONSE_BYTES_MAX
                or self._response_bytes > WORKER_RESPONSE_BYTES_MAX - len(raw)
            ):
                _fail("WORKER_RESPONSE_CAPACITY_FATAL")
            self._responses.append(raw)
            self._response_bytes += len(raw)
            self._condition.notify()

    def enqueue_event(self, raw: bytes, *, terminal: bool = False) -> bool:
        with self._condition:
            if self._finished:
                return False
            if terminal and self._terminal_event_queued:
                return True
            count_limit = WORKER_EVENT_CAP if terminal else WORKER_EVENT_CAP - 1
            byte_limit = (
                WORKER_EVENT_BYTES_MAX
                if terminal
                else WORKER_EVENT_BYTES_MAX - _EVENT_OVERFLOW_RESERVE_BYTES
            )
            if (
                len(self._events) >= count_limit
                or len(raw) > byte_limit
                or self._event_bytes > byte_limit - len(raw)
            ):
                return False
            if terminal:
                self._terminal_event_queued = True
            self._events.append(raw)
            self._event_bytes += len(raw)
            self._condition.notify()
            return True

    def finish(self) -> None:
        with self._condition:
            self._finished = True
            self._condition.notify_all()

    def next(self) -> bytes | object:
        with self._condition:
            while not self._responses and not self._events and not self._finished:
                self._condition.wait()
            if self._responses and (
                self._response_burst < RESPONSE_BURST_MAX or not self._events
            ):
                raw = self._responses.popleft()
                self._response_bytes -= len(raw)
                self._response_burst += 1
                return raw
            if self._events:
                raw = self._events.popleft()
                self._event_bytes -= len(raw)
                self._response_burst = 0
                return raw
            if self._responses:
                raw = self._responses.popleft()
                self._response_bytes -= len(raw)
                self._response_burst += 1
                return raw
            return _STOP

def _write_all(descriptor: int, raw: bytes) -> None:
    view = memoryview(raw)
    offset = 0
    while offset < len(view):
        try:
            written = os.write(descriptor, view[offset:])
        except InterruptedError:
            continue
        if written <= 0:
            _fail("IPC_WRITE_FAILED")
        offset += written


class WorkerProcess:
    """Three-thread NDJSON worker process for one immutable epoch."""

    def __init__(
        self,
        *,
        profile: RuntimeProfile,
        stdin: BinaryIO,
        stdout_fd: int,
        stderr_fd: int,
        backend_factory: Callable[[RuntimeProfile, str], NativeBackend],
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._profile = profile
        self._stdin = stdin
        self._stdout_fd = stdout_fd
        self._stderr_fd = stderr_fd
        self._backend_factory = backend_factory
        self._epoch = str(uuid.uuid4())
        self._monotonic = monotonic
        self._commands = _BoundedQueue(
            count_cap=WORKER_COMMAND_CAP,
            byte_cap=WORKER_COMMAND_BYTES_MAX,
        )
        self._outputs = _OutputQueues()
        self._snapshot = _SnapshotPager(
            worker_epoch=self._epoch,
            monotonic=monotonic,
        )
        self._reader_done = threading.Event()
        self._reader_fatal = threading.Event()
        self._diagnostic_lock = threading.Lock()
        self._exit_code = 0

    def _diagnostic(self, code: str, **fields: Any) -> None:
        value = {"code": code, "kind": "worker-diagnostic", **fields}
        raw = canonical_json_bytes(value)
        if len(raw) > 4_096:
            raw = b'{"code":"DIAGNOSTIC_TOO_LARGE","kind":"worker-diagnostic"}\n'
        with self._diagnostic_lock:
            try:
                _write_all(self._stderr_fd, raw)
            except WorkerFailure:
                self._exit_code = 74

    def _reader(self) -> None:
        try:
            while True:
                line = self._stdin.readline(IPC_LINE_MAX + 1)
                if line == b"":
                    return
                if len(line) > IPC_LINE_MAX or not line.endswith(b"\n"):
                    self._diagnostic("IPC_PROTOCOL_FATAL")
                    self._reader_fatal.set()
                    self._exit_code = 76
                    return
                try:
                    command = parse_worker_command(line)
                except ProtocolError:
                    self._diagnostic("IPC_PROTOCOL_FATAL")
                    self._reader_fatal.set()
                    self._exit_code = 76
                    return
                self._commands.put(_QueuedCommand(command, len(line)), len(line))
        except WorkerFailure as error:
            self._diagnostic(error.code)
            self._reader_fatal.set()
            self._exit_code = 76
        except Exception:
            self._diagnostic("IPC_READ_FAILED")
            self._reader_fatal.set()
            self._exit_code = 74
        finally:
            self._reader_done.set()
            try:
                self._commands.put(_STOP, 0)
            except WorkerFailure:
                # STOP has a separate Event fallback when the normal command
                # queue is already full.
                pass

    def _response(
        self,
        command: WorkerCommand,
        *,
        result: Mapping[str, Any] | None = None,
        error: WorkerFailure | None = None,
    ) -> bytes:
        if (result is None) == (error is None):
            raise AssertionError("response requires exactly one result or error")
        value: dict[str, Any] = {"id": command.command_id, "v": 1}
        if result is not None:
            value.update({"ok": True, "result": dict(result)})
        else:
            assert error is not None
            value.update(
                {
                    "error": {
                        "code": error.code,
                        "nativeCode": error.native_code,
                    },
                    "ok": False,
                }
            )
        return encode_ipc_object(value)

    def _hello_result(
        self, backend: NativeBackend, value: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        if not isinstance(value, Mapping) or frozenset(value) != frozenset(
            {
                "identity",
                "localUdid",
                "nativeAbiVersion",
                "socketCap",
                "workerEpoch",
            }
        ):
            _fail("NATIVE_DATA_INVALID")
        if value["workerEpoch"] != self._epoch:
            _fail("NATIVE_DATA_INVALID")
        if value["nativeAbiVersion"] != NATIVE_ABI_VERSION:
            _fail("NATIVE_DATA_INVALID")
        local_udid = _bounded_text(
            value["localUdid"], "localUdid", minimum=1, maximum=64
        )
        socket_cap = _bounded_int(value["socketCap"], 1, 2**32 - 1)
        identity = value["identity"]
        identity_keys = frozenset(
            {
                "capabilitySet",
                "distributedDataSyncGranted",
                "gid",
                "selinuxDomain",
                "supplementaryGids",
                "tokenIdHash",
                "uid",
            }
        )
        if not isinstance(identity, Mapping) or frozenset(identity) != identity_keys:
            _fail("NATIVE_DATA_INVALID")
        if identity["distributedDataSyncGranted"] is not True:
            _fail("NATIVE_DATA_INVALID")
        uid = _bounded_int(identity["uid"], 0, 2**32 - 1)
        gid = _bounded_int(identity["gid"], 0, 2**32 - 1)
        groups = identity["supplementaryGids"]
        if not isinstance(groups, list):
            _fail("NATIVE_DATA_INVALID")
        normalized_groups = [
            _bounded_int(group, 0, 2**32 - 1) for group in groups
        ]
        if (
            len(normalized_groups) != len(set(normalized_groups))
            or normalized_groups != sorted(normalized_groups)
        ):
            _fail("NATIVE_DATA_INVALID")
        capabilities = identity["capabilitySet"]
        if not isinstance(capabilities, list):
            _fail("NATIVE_DATA_INVALID")
        normalized_capabilities = [
            _bounded_text(
                capability,
                "capability",
                minimum=1,
                maximum=64,
            )
            for capability in capabilities
        ]
        if len(normalized_capabilities) != len(set(normalized_capabilities)):
            _fail("NATIVE_DATA_INVALID")
        token_hash = identity["tokenIdHash"]
        if (
            not isinstance(token_hash, str)
            or not token_hash.startswith(_SHA256_TAG_PREFIX)
            or not _is_hex64(token_hash[len(_SHA256_TAG_PREFIX) :])
        ):
            _fail("NATIVE_DATA_INVALID")
        selinux_domain = _bounded_text(
            identity["selinuxDomain"],
            "selinuxDomain",
            minimum=1,
            maximum=256,
        )
        return MappingProxyType(
            {
                "identity": {
                    "capabilitySet": normalized_capabilities,
                    "distributedDataSyncGranted": True,
                    "gid": gid,
                    "selinuxDomain": selinux_domain,
                    "supplementaryGids": normalized_groups,
                    "tokenIdHash": token_hash,
                    "uid": uid,
                },
                "localUdid": local_udid,
                "nativeAbiVersion": NATIVE_ABI_VERSION,
                "socketCap": socket_cap,
                "workerEpoch": self._epoch,
            }
        )

    def _dispatch(
        self,
        backend: NativeBackend,
        command: WorkerCommand,
        phase: str,
    ) -> tuple[Mapping[str, Any], str, bool]:
        operation = command.operation
        args = command.args
        if phase == "expect-hello" and operation != "hello":
            _fail("HELLO_REQUIRED")
        if operation == "hello":
            if phase != "expect-hello":
                _fail("INVALID_WORKER_STATE")
            result = self._hello_result(backend, backend.hello_result())
            return result, "hello-complete", False
        if operation == "stop":
            if phase == "expect-hello":
                _fail("HELLO_REQUIRED")
            result = backend.stop()
            if dict(result) != {"stopped": True}:
                _fail("NATIVE_DATA_INVALID")
            self._snapshot.release()
            return MappingProxyType({"stopped": True}), "stopped", True
        if operation == "start":
            if phase not in {"hello-complete", "started"}:
                _fail("INVALID_WORKER_STATE")
            result = backend.start()
            if dict(result) != {"nodeEventsStarted": True}:
                _fail("NATIVE_DATA_INVALID")
            return MappingProxyType({"nodeEventsStarted": True}), "started", False
        if phase != "started":
            _fail("INVALID_WORKER_STATE")
        if operation == "snapshot_nodes":
            if not args:
                return self._snapshot.begin(backend.snapshot_nodes()), phase, False
            return (
                self._snapshot.continue_page(
                    str(args["snapshotId"]), str(args["cursor"])
                ),
                phase,
                False,
            )
        if operation == "get_node_udid":
            udid = _bounded_text(
                backend.get_node_udid(str(args["networkId"])),
                "udid",
                minimum=1,
                maximum=64,
            )
            return MappingProxyType({"udid": udid}), phase, False
        if operation == "start_device_discovery":
            backend.start_device_discovery()
            return MappingProxyType({"started": True}), phase, False
        if operation == "stop_device_discovery":
            native_devices, failure_native_code = (
                backend.stop_device_discovery()
            )
            if (
                not isinstance(native_devices, tuple)
                or len(native_devices) > _DEVICE_MANAGER_DEVICE_MAX
                or type(failure_native_code) is not int
                or not -(2**31) <= failure_native_code <= 2**31 - 1
            ):
                _fail("NATIVE_DATA_INVALID")
            devices: list[dict[str, Any]] = []
            digests: set[str] = set()
            for native_device in native_devices:
                if not isinstance(native_device, NativeTrustedDevice):
                    _fail("NATIVE_DATA_INVALID")
                device_id = _bounded_text(
                    native_device.device_id,
                    "deviceId",
                    minimum=1,
                    maximum=_DEVICE_MANAGER_DEVICE_ID_MAX,
                )
                digest = hashlib.sha256(device_id.encode("utf-8")).hexdigest()
                if digest in digests:
                    _fail("DEVICE_TARGET_AMBIGUOUS")
                digests.add(digest)
                _bounded_text(
                    native_device.network_id,
                    "networkId",
                    minimum=0,
                    maximum=_DEVICE_MANAGER_NETWORK_ID_MAX,
                )
                devices.append(
                    {
                        "deviceIdSha256": digest,
                        "deviceName": _bounded_text(
                            native_device.device_name,
                            "deviceName",
                            minimum=0,
                            maximum=_DEVICE_MANAGER_DEVICE_NAME_MAX,
                        ),
                        "deviceTypeId": _bounded_int(
                            native_device.device_type_id, 0, 2**16 - 1
                        ),
                    }
                )
            devices.sort(key=lambda item: str(item["deviceIdSha256"]))
            return (
                MappingProxyType(
                    {
                        "devices": devices,
                        "failureNativeCode": (
                            failure_native_code
                            if failure_native_code != 0
                            else None
                        ),
                        "stopped": True,
                    }
                ),
                phase,
                False,
            )
        if operation == "begin_device_bind":
            digest = str(args["deviceIdSha256"])
            backend.begin_device_bind(digest)
            return (
                MappingProxyType(
                    {"binding": True, "deviceIdSha256": digest}
                ),
                phase,
                False,
            )
        if operation == "get_device_bind_status":
            digest = str(args["deviceIdSha256"])
            status, native_code = backend.device_bind_status(digest)
            if status not in {"pending", "bound", "failed"}:
                _fail("NATIVE_DATA_INVALID")
            if (
                type(native_code) is not int
                or not -(2**31) <= native_code <= 2**31 - 1
                or (status in {"pending", "bound"} and native_code != 0)
                or (status == "failed" and native_code == 0)
            ):
                _fail("NATIVE_DATA_INVALID")
            return (
                MappingProxyType(
                    {
                        "deviceIdSha256": digest,
                        "nativeCode": native_code,
                        "status": status,
                    }
                ),
                phase,
                False,
            )
        if operation == "list_trusted_devices":
            native_devices = backend.list_trusted_devices()
            if not isinstance(native_devices, tuple) or len(native_devices) > 256:
                _fail("NATIVE_DATA_INVALID")
            devices: list[dict[str, Any]] = []
            network_ids: set[str] = set()
            for native_device in native_devices:
                if not isinstance(native_device, NativeTrustedDevice):
                    _fail("NATIVE_DATA_INVALID")
                device_id = _bounded_text(
                    native_device.device_id,
                    "deviceId",
                    minimum=1,
                    maximum=_DEVICE_MANAGER_DEVICE_ID_MAX,
                )
                network_id = _bounded_text(
                    native_device.network_id,
                    "networkId",
                    minimum=1,
                    maximum=_DEVICE_MANAGER_NETWORK_ID_MAX,
                )
                if network_id in network_ids:
                    _fail("DEVICE_TARGET_AMBIGUOUS")
                network_ids.add(network_id)
                devices.append(
                    {
                        "deviceIdSha256": hashlib.sha256(
                            device_id.encode("utf-8")
                        ).hexdigest(),
                        "deviceName": _bounded_text(
                            native_device.device_name,
                            "deviceName",
                            minimum=0,
                            maximum=_DEVICE_MANAGER_DEVICE_NAME_MAX,
                        ),
                        "deviceTypeId": _bounded_int(
                            native_device.device_type_id, 0, 2**16 - 1
                        ),
                        "networkId": network_id,
                    }
                )
            devices.sort(key=lambda device: str(device["networkId"]))
            return MappingProxyType({"devices": devices}), phase, False
        if operation == "unbind_device":
            digest = backend.unbind_device(str(args["networkId"]))
            if not isinstance(digest, str) or not _is_hex64(digest):
                _fail("NATIVE_DATA_INVALID")
            return (
                MappingProxyType(
                    {"deviceIdSha256": digest, "unbound": True}
                ),
                phase,
                False,
            )
        if operation == "listen":
            socket = _bounded_int(
                backend.listen(str(args["serviceName"])), 0, 2**31 - 1
            )
            return MappingProxyType({"socket": socket}), phase, False
        if operation == "connect":
            socket, mtu = backend.connect(
                str(args["serviceName"]),
                str(args["peerServiceName"]),
                str(args["networkId"]),
            )
            return (
                MappingProxyType(
                    {
                        "mtu": _bounded_int(mtu, 1, 2**32 - 1),
                        "socket": _bounded_int(socket, 0, 2**31 - 1),
                    }
                ),
                phase,
                False,
            )
        if operation == "send_bytes":
            data = decode_strict_base64(args["data"], maximum=REMOTE_FRAME_MAX)
            sent = _bounded_int(
                backend.send_bytes(int(args["socket"]), data),
                0,
                REMOTE_FRAME_MAX,
            )
            if sent != len(data):
                _fail("NATIVE_DATA_INVALID")
            return MappingProxyType({"sentBytes": sent}), phase, False
        if operation == "close_socket":
            backend.close_socket(int(args["socket"]))
            return MappingProxyType({"closed": True}), phase, False
        _fail("INVALID_WORKER_STATE")

    def _event_object(
        self, event: NativeEvent, last_node_sequence: int
    ) -> tuple[bytes, int, bool]:
        if not isinstance(event, NativeEvent):
            _fail("NATIVE_EVENT_INVALID")
        event_type = event.event_type
        data: dict[str, Any]
        terminal = False
        if event_type in {"node-online", "node-offline"}:
            sequence = _bounded_int(event.node_event_seq, 1, 2**64 - 1)
            if sequence <= last_node_sequence:
                _fail("NATIVE_EVENT_SEQUENCE_INVALID")
            if (
                event.socket != -1
                or event.status != _DSB_OK
                or event.native_code != 0
                or event.stable_code
                or event.mtu != 0
                or event.data
                or event.dropped_count != 0
                or event.dropped_bytes != 0
            ):
                _fail("NATIVE_EVENT_INVALID")
            data = {
                "deviceName": _bounded_text(
                    event.device_name, "deviceName", minimum=0, maximum=127
                ),
                "deviceTypeId": _bounded_int(
                    event.device_type_id, 0, 2**16 - 1
                ),
                "networkId": _bounded_text(
                    event.network_id, "networkId", minimum=1, maximum=64
                ),
                "nodeEventSeq": sequence,
            }
            last_node_sequence = sequence
        elif event_type == "bound":
            if (
                event.status != _DSB_OK
                or event.native_code != 0
                or event.stable_code
                or event.node_event_seq != 0
                or event.device_name
                or event.device_type_id != 0
                or event.data
                or event.dropped_count != 0
                or event.dropped_bytes != 0
            ):
                _fail("NATIVE_EVENT_INVALID")
            data = {
                "mtu": _bounded_int(event.mtu, 1, 2**32 - 1),
                "networkId": _bounded_text(
                    event.network_id, "networkId", minimum=1, maximum=64
                ),
                "socket": _bounded_int(event.socket, 0, 2**31 - 1),
            }
        elif event_type == "bytes":
            if (
                event.status != _DSB_OK
                or event.native_code != 0
                or event.stable_code
                or event.mtu != 0
                or event.node_event_seq != 0
                or event.network_id
                or event.device_name
                or event.device_type_id != 0
                or event.dropped_count != 0
                or event.dropped_bytes != 0
                or not isinstance(event.data, bytes)
                or not 1 <= len(event.data) <= REMOTE_FRAME_MAX
            ):
                _fail("NATIVE_EVENT_INVALID")
            data = {
                "data": base64.b64encode(event.data).decode("ascii"),
                "socket": _bounded_int(event.socket, 0, 2**31 - 1),
            }
        elif event_type == "closed":
            if (
                event.stable_code
                or event.mtu != 0
                or event.node_event_seq != 0
                or event.network_id
                or event.device_name
                or event.device_type_id != 0
                or event.data
                or event.dropped_count != 0
                or event.dropped_bytes != 0
            ):
                _fail("NATIVE_EVENT_INVALID")
            status_code = (
                "SOCKET_CLOSED"
                if event.status == _DSB_OK
                else _native_failure_code(event.status, "event")
            )
            data = {
                "code": status_code,
                "nativeCode": _bounded_int(
                    event.native_code, -(2**31), 2**31 - 1
                ),
                "scope": "socket",
                "socket": _bounded_int(event.socket, 0, 2**31 - 1),
            }
        elif event_type == "overflow":
            if (
                event.socket != -1
                or event.native_code != 0
                or event.stable_code
                or event.mtu != 0
                or event.node_event_seq != 0
                or event.network_id
                or event.device_name
                or event.device_type_id != 0
                or event.data
            ):
                _fail("NATIVE_EVENT_INVALID")
            data = {
                "droppedBytes": _bounded_int(event.dropped_bytes, 0, 2**64 - 1),
                "droppedCount": _bounded_int(event.dropped_count, 1, 2**32 - 1),
            }
            terminal = True
        elif event_type == "fatal":
            if (
                event.mtu != 0
                or event.node_event_seq != 0
                or event.network_id
                or event.device_name
                or event.device_type_id != 0
                or event.data
                or event.dropped_count != 0
                or event.dropped_bytes != 0
            ):
                _fail("NATIVE_EVENT_INVALID")
            code = event.stable_code or _native_failure_code(event.status, "event")
            if (
                not isinstance(code, str)
                or not 1 <= len(code) <= 64
                or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ_" for character in code)
            ):
                _fail("NATIVE_EVENT_INVALID")
            data = {
                "code": code,
                "nativeCode": _bounded_int(
                    event.native_code, -(2**31), 2**31 - 1
                ),
                "scope": "epoch" if event.socket == -1 else "socket",
            }
            if event.socket != -1:
                data["socket"] = _bounded_int(event.socket, 0, 2**31 - 1)
            terminal = True
        else:
            _fail("NATIVE_EVENT_INVALID")
        raw = encode_ipc_object(
            {
                "data": data,
                "event": event_type,
                "v": 1,
                "workerEpoch": self._epoch,
            }
        )
        return raw, last_node_sequence, terminal

    def _enqueue_epoch_fatal(self, code: str, *, native_code: int = 0) -> None:
        event = NativeEvent(
            event_type="fatal",
            status=_DSB_E_NATIVE,
            native_code=native_code,
            stable_code=code,
        )
        raw, _, _ = self._event_object(event, 0)
        if not self._outputs.enqueue_event(raw, terminal=True):
            self._diagnostic("WORKER_EVENT_CAPACITY_FATAL")

    def _enqueue_native_event(
        self, event: NativeEvent, last_node_sequence: int
    ) -> tuple[int, bool]:
        raw, last_node_sequence, terminal = self._event_object(
            event, last_node_sequence
        )
        if self._outputs.enqueue_event(raw, terminal=terminal):
            return last_node_sequence, terminal
        overflow = NativeEvent(
            event_type="overflow",
            status=_DSB_E_OVERFLOW,
            dropped_count=1,
            dropped_bytes=len(raw),
        )
        overflow_raw, last_node_sequence, _ = self._event_object(
            overflow, last_node_sequence
        )
        if not self._outputs.enqueue_event(overflow_raw, terminal=True):
            self._diagnostic("WORKER_EVENT_CAPACITY_FATAL")
        return last_node_sequence, True

    def _owner(self) -> None:
        backend: NativeBackend | None = None
        startup_error: WorkerFailure | None = None
        phase = "expect-hello"
        last_node_sequence = 0
        try:
            try:
                backend = self._backend_factory(self._profile, self._epoch)
                if os.environ.get(_PROBE_PID_ENV) == "1":
                    self._diagnostic(
                        "WORKER_READY",
                        mapsSha256=backend.maps_sha256,
                        pid=os.getpid(),
                        workerEpoch=self._epoch,
                    )
            except WorkerFailure as error:
                startup_error = error
                self._diagnostic(error.code)
            except Exception:
                startup_error = WorkerFailure("WORKER_STARTUP_FAILED")
                self._diagnostic("WORKER_STARTUP_FAILED")

            input_done = False
            while True:
                if self._reader_fatal.is_set():
                    break
                processed = 0
                stop_after = False
                while processed < COMMAND_BURST_MAX:
                    try:
                        if (
                            processed == 0
                            and phase != "started"
                            and not self._reader_done.is_set()
                        ):
                            item, _ = self._commands.get(timeout=0.05)
                        else:
                            item, _ = self._commands.get_nowait()
                    except queue.Empty:
                        break
                    if item is _STOP:
                        input_done = True
                        break
                    queued: _QueuedCommand = item
                    command = queued.command
                    processed += 1
                    try:
                        if (
                            phase == "expect-hello"
                            and command.operation != "hello"
                        ):
                            _fail("HELLO_REQUIRED")
                        if startup_error is not None:
                            raise startup_error
                        assert backend is not None
                        result, phase, stop_after = self._dispatch(
                            backend, command, phase
                        )
                        response = self._response(command, result=result)
                    except WorkerFailure as error:
                        response = self._response(command, error=error)
                        if phase == "expect-hello" or startup_error is not None:
                            stop_after = True
                            if self._exit_code == 0:
                                self._exit_code = 70
                    except Exception:
                        error = WorkerFailure("NATIVE_BACKEND_FAILED")
                        response = self._response(command, error=error)
                        stop_after = True
                        if self._exit_code == 0:
                            self._exit_code = 70
                    try:
                        self._outputs.enqueue_response(response)
                    except WorkerFailure as error:
                        self._diagnostic(error.code)
                        stop_after = True
                        if self._exit_code == 0:
                            self._exit_code = 70
                    if stop_after or self._reader_fatal.is_set():
                        break
                if stop_after or self._reader_fatal.is_set():
                    break
                if phase == "started":
                    assert backend is not None
                    poll_timeout = 0 if processed else _POLL_MAX_MS
                    for event_index in range(NATIVE_EVENT_BURST_MAX):
                        try:
                            event = backend.poll(
                                poll_timeout if event_index == 0 else 0
                            )
                            if event is None:
                                break
                            last_node_sequence, terminal = self._enqueue_native_event(
                                event, last_node_sequence
                            )
                        except WorkerFailure as error:
                            self._enqueue_epoch_fatal(
                                error.code, native_code=error.native_code
                            )
                            terminal = True
                        except Exception:
                            self._enqueue_epoch_fatal("NATIVE_BACKEND_FAILED")
                            terminal = True
                        if terminal:
                            if self._exit_code == 0:
                                self._exit_code = 70
                            stop_after = True
                            break
                    if stop_after:
                        break
                if input_done or (
                    self._reader_done.is_set() and self._commands.empty()
                ):
                    break
        finally:
            self._snapshot.release()
            if backend is not None and phase != "stopped":
                try:
                    backend.stop()
                except Exception:
                    self._diagnostic("NATIVE_STOP_FAILED")
                    if self._exit_code == 0:
                        self._exit_code = 70
            self._outputs.finish()

    def _writer(self) -> None:
        try:
            while True:
                item = self._outputs.next()
                if item is _STOP:
                    return
                assert isinstance(item, bytes)
                _write_all(self._stdout_fd, item)
        except Exception:
            self._exit_code = 74

    def run(self) -> int:
        reader = threading.Thread(
            target=self._reader,
            name="mclaw-dsoftbus-stdin-reader",
            daemon=True,
        )
        owner = threading.Thread(
            target=self._owner,
            name="mclaw-dsoftbus-native-owner",
            daemon=False,
        )
        writer = threading.Thread(
            target=self._writer,
            name="mclaw-dsoftbus-stdout-writer",
            daemon=False,
        )
        for thread in (reader, owner, writer):
            thread.start()
        # A successful stop command ends the owner and writer while stdin can
        # still be an open parent pipe.  The isolated bootstrap terminates via
        # os._exit immediately after this return, so the reader is deliberately
        # a daemon and is never allowed to hold shutdown open.
        owner.join()
        writer.join()
        return self._exit_code


def main() -> int:
    try:
        profile = _load_profile()
    except WorkerFailure as error:
        raw = canonical_json_bytes(
            {"code": error.code, "kind": "worker-diagnostic"}
        )
        try:
            _write_all(sys.stderr.fileno(), raw)
        except Exception:
            pass
        return 78
    process = WorkerProcess(
        profile=profile,
        stdin=sys.stdin.buffer,
        stdout_fd=sys.stdout.fileno(),
        stderr_fd=sys.stderr.fileno(),
        backend_factory=RealNativeBackend,
    )
    return process.run()


__all__ = [
    "NativeBackend",
    "NativeEvent",
    "NativeNode",
    "NativeSnapshot",
    "RealNativeBackend",
    "WorkerFailure",
    "WorkerProcess",
    "main",
]
