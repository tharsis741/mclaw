# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Strict device-local Runtime Profile loading and read-only preflight."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import stat
from types import MappingProxyType
from typing import Any, Mapping, NoReturn, Sequence

from .protocol import ProtocolError, canonical_json_bytes, strict_json_loads

RUNTIME_PROFILE_FILENAME = "runtime-profile.json"
RUNTIME_PROFILE_SCHEMA = "mclaw.dsoftbus.runtime-profile"
RUNTIME_PROFILE_BYTES_MAX = 262_144
REMOTE_SOFTBUS_LIBRARY = "/system/lib64/platformsdk/libsoftbus_client.z.so"
DEPLOYMENT_ROOT = "/data/local/release/opt/mclaw-dsoftbus"
PYTHON_EXECUTABLE = "/data/local/release/bin/python3"
PYTHON_RELEASE_LIBRARY_DIRS = (
    "/data/local/release/usr/lib",
    "/data/local/release/usr/lib64",
)
PYTHON_DYNAMIC_LIBRARY = "/data/local/release/usr/lib/libpython3.12.so.1.0"
RELEASE_LIBCXX_LIBRARY = "/data/local/release/usr/lib/libc++.so.1.0"
SHIM_LIBRARY = f"{DEPLOYMENT_ROOT}/current/lib/libmclaw_dsoftbus_oh61.so"
BUNDLED_LIBCXX_LIBRARY = f"{DEPLOYMENT_ROOT}/current/lib/libc++_shared.so"
SYSTEM_LIBCXX_LIBRARY = "/system/lib64/chipset-sdk-sp/libc++.so"
PERMISSION_LIBRARY = "/system/lib64/ndk/libability_access_control.so"
DEVICE_MANAGER_FFI_LIBRARY = (
    "/system/lib64/platformsdk/libcj_distributed_device_manager_ffi.z.so"
)
CJ_BIND_FFI_LIBRARY = "/system/lib64/platformsdk/libcj_bind_ffi.z.so"
CJ_BIND_NATIVE_LIBRARY = "/system/lib64/platformsdk/libcj_bind_native.z.so"
TOKEN_LAUNCHER = (
    "/data/local/release/opt/mclaw-dsoftbus-token-launcher/current/bin/"
    "mclaw_token_launcher"
)
DISTRIBUTED_DATASYNC_PERMISSION = "ohos.permission.DISTRIBUTED_DATASYNC"
SOFTBUS_SOCKET_CAP_MAX = 4096
WORKER_CODE_MANIFEST_FILENAME = "worker-code-manifest.json"
WORKER_CODE_MANIFEST_SCHEMA = "mclaw.dsoftbus.worker-code-manifest"
WORKER_BOOTSTRAP_RELATIVE_PATH = "mclaw/dsoftbus/worker_bootstrap.py"
WORKER_MODULE_RELATIVE_PATHS = (
    "mclaw/__init__.py",
    "mclaw/dsoftbus/__init__.py",
    "mclaw/dsoftbus/baseline.py",
    "mclaw/dsoftbus/protocol.py",
    "mclaw/dsoftbus/worker.py",
    WORKER_BOOTSTRAP_RELATIVE_PATH,
)

LINUX_CAPABILITY_NAMES = (
    "CAP_CHOWN",
    "CAP_DAC_OVERRIDE",
    "CAP_DAC_READ_SEARCH",
    "CAP_FOWNER",
    "CAP_FSETID",
    "CAP_KILL",
    "CAP_SETGID",
    "CAP_SETUID",
    "CAP_SETPCAP",
    "CAP_LINUX_IMMUTABLE",
    "CAP_NET_BIND_SERVICE",
    "CAP_NET_BROADCAST",
    "CAP_NET_ADMIN",
    "CAP_NET_RAW",
    "CAP_IPC_LOCK",
    "CAP_IPC_OWNER",
    "CAP_SYS_MODULE",
    "CAP_SYS_RAWIO",
    "CAP_SYS_CHROOT",
    "CAP_SYS_PTRACE",
    "CAP_SYS_PACCT",
    "CAP_SYS_ADMIN",
    "CAP_SYS_BOOT",
    "CAP_SYS_NICE",
    "CAP_SYS_RESOURCE",
    "CAP_SYS_TIME",
    "CAP_SYS_TTY_CONFIG",
    "CAP_MKNOD",
    "CAP_LEASE",
    "CAP_AUDIT_WRITE",
    "CAP_AUDIT_CONTROL",
    "CAP_SETFCAP",
    "CAP_MAC_OVERRIDE",
    "CAP_MAC_ADMIN",
    "CAP_SYSLOG",
    "CAP_WAKE_ALARM",
    "CAP_BLOCK_SUSPEND",
    "CAP_AUDIT_READ",
    "CAP_PERFMON",
    "CAP_BPF",
    "CAP_CHECKPOINT_RESTORE",
)

REQUIRED_SOFTBUS_EXPORTS = (
    "Bind",
    "BindAsync",
    "FreeNodeInfo",
    "GetAllNodeDeviceInfo",
    "GetLocalNodeDeviceInfo",
    "GetMtuSize",
    "GetNodeKeyInfo",
    "Listen",
    "RegNodeDeviceStateCb",
    "SendBytes",
    "SendBytesAsync",
    "Shutdown",
    "Socket",
    "UnregNodeDeviceStateCb",
)

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_SHA256_TAG = re.compile(r"^sha256:[0-9a-f]{64}$")
_MODE = re.compile(r"^0[0-7]{3}$")


class BaselineError(RuntimeError):
    """Stable Runtime Profile or read-only preflight failure."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _fail(detail: str, *, code: str = "PROFILE_INVALID") -> NoReturn:
    raise BaselineError(code, detail)


def _exact_object(value: Any, keys: frozenset[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(f"{label} must be an object")
    actual = frozenset(value)
    if actual != keys:
        _fail(
            f"{label} exact keys mismatch "
            f"missing={sorted(keys - actual)} extra={sorted(actual - keys)}"
        )
    return value


def _string(value: Any, label: str, *, minimum: int = 1, maximum: int = 4096) -> str:
    if not isinstance(value, str):
        _fail(f"{label} must be a string")
    length = len(value.encode("utf-8"))
    if length < minimum or length > maximum or "\x00" in value:
        _fail(f"{label} UTF-8 length is outside {minimum}..{maximum}")
    return value


def _integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or value < minimum or value > maximum:
        _fail(f"{label} must be an integer in {minimum}..{maximum}")
    return value


def _sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or _HEX64.fullmatch(value) is None:
        _fail(f"{label} must be 64 lowercase hexadecimal characters")
    return value


def _read_regular_no_follow(
    path: Path,
    *,
    maximum: int,
    label: str,
    validation_code: str = "PROFILE_INVALID",
    read_code: str = "PROFILE_READ_FAILED",
) -> bytes:
    try:
        before_path = path.lstat()
    except OSError as error:
        raise BaselineError(read_code, f"cannot stat {label}") from error
    if stat.S_ISLNK(before_path.st_mode) or not stat.S_ISREG(before_path.st_mode):
        _fail(f"{label} must be a no-follow regular file", code=validation_code)
    if before_path.st_size < 1 or before_path.st_size > maximum:
        _fail(
            f"{label} byte length is outside 1..{maximum}", code=validation_code
        )

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise BaselineError(read_code, f"cannot open {label}") from error
    try:
        before_fd = os.fstat(descriptor)
        if not stat.S_ISREG(before_fd.st_mode):
            _fail(f"{label} descriptor is not a regular file", code=validation_code)
        if (
            before_fd.st_dev != before_path.st_dev
            or before_fd.st_ino != before_path.st_ino
            or before_fd.st_size != before_path.st_size
        ):
            _fail(f"{label} path identity changed before read", code=validation_code)
        chunks: list[bytes] = []
        remaining = before_fd.st_size
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                _fail(
                    f"{label} ended before its declared byte length",
                    code=validation_code,
                )
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            _fail(f"{label} grew during read", code=validation_code)
        after_fd = os.fstat(descriptor)
        identity_before = (
            before_fd.st_dev,
            before_fd.st_ino,
            before_fd.st_size,
            before_fd.st_mtime_ns,
        )
        identity_after = (
            after_fd.st_dev,
            after_fd.st_ino,
            after_fd.st_size,
            after_fd.st_mtime_ns,
        )
        if identity_before != identity_after:
            _fail(f"{label} changed during read", code=validation_code)
        try:
            after_path = path.lstat()
        except OSError as error:
            raise BaselineError(read_code, f"cannot restat {label}") from error
        if (
            stat.S_ISLNK(after_path.st_mode)
            or after_path.st_dev != before_fd.st_dev
            or after_path.st_ino != before_fd.st_ino
            or after_path.st_size != before_fd.st_size
            or after_path.st_mtime_ns != before_fd.st_mtime_ns
        ):
            _fail(f"{label} path identity changed during read", code=validation_code)
        return b"".join(chunks)
    finally:
        os.close(descriptor)

def _validate_generated_at(value: Any) -> None:
    rendered = _string(value, "generatedAt", maximum=64)
    if not rendered.endswith("Z"):
        _fail("generatedAt must be UTC with Z suffix")
    try:
        parsed = datetime.fromisoformat(rendered[:-1] + "+00:00")
    except ValueError as error:
        raise BaselineError("PROFILE_INVALID", "generatedAt is invalid") from error
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        _fail("generatedAt must be UTC")

def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value

@dataclass(frozen=True, slots=True)
class RuntimeProfile:
    """Device-local activation Profile generated by the installed M-Claw."""

    path: Path
    sha256: str
    byte_length: int
    document: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ObservedRuntimeIdentity:
    api_level: int
    abi: str
    machine: str
    uid: int
    gid: int
    selinux_context: str


@dataclass(frozen=True, slots=True)
class RuntimePreflightResult:
    status: str
    profile_sha256: str
    softbus_sha256: str
    softbus_socket_cap: int

_RUNTIME_PROFILE_ROOT_KEYS = frozenset(
    {
        "generatedAt",
        "runtimeClosure",
        "schema",
        "softbus",
        "systemFingerprint",
        "target",
    }
)
_RUNTIME_ARTIFACT_KEYS = frozenset(
    {"byteLength", "gid", "mode", "path", "resolvedPath", "sha256", "uid"}
)
_RUNTIME_WORKER_FILE_KEYS = _RUNTIME_ARTIFACT_KEYS | frozenset({"relativePath"})


def _runtime_absolute_path(value: Any, label: str) -> str:
    text = _string(value, label, maximum=4096)
    parsed = PurePosixPath(text)
    if not parsed.is_absolute() or str(parsed) != text or any(
        part in {"", ".", ".."} for part in parsed.parts
    ):
        _fail(f"{label} must be a canonical absolute POSIX path")
    return text


def _runtime_string_list(
    value: Any,
    label: str,
    *,
    maximum_items: int,
    allowed: frozenset[str] | None = None,
) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > maximum_items:
        _fail(f"{label} must be a bounded array")
    result = tuple(_string(item, f"{label} item", maximum=128) for item in value)
    if len(result) != len(set(result)):
        _fail(f"{label} must not contain duplicates")
    if allowed is not None and any(item not in allowed for item in result):
        _fail(f"{label} contains an unsupported value")
    return result


def _runtime_integer_list(value: Any, label: str) -> tuple[int, ...]:
    if not isinstance(value, list) or len(value) > 64:
        _fail(f"{label} must be a bounded array")
    result = tuple(_integer(item, f"{label} item", 0, 2**32 - 1) for item in value)
    if result != tuple(sorted(set(result))):
        _fail(f"{label} must be unique and ascending")
    return result


def _validate_local_runtime_artifact(
    value: Any,
    label: str,
    *,
    worker_file: bool = False,
) -> dict[str, Any]:
    keys = _RUNTIME_WORKER_FILE_KEYS if worker_file else _RUNTIME_ARTIFACT_KEYS
    artifact = _exact_object(value, keys, label)
    _runtime_absolute_path(artifact["path"], f"{label}.path")
    _runtime_absolute_path(artifact["resolvedPath"], f"{label}.resolvedPath")
    _integer(artifact["byteLength"], f"{label}.byteLength", 1, 2**31 - 1)
    _sha256(artifact["sha256"], f"{label}.sha256")
    mode = _string(artifact["mode"], f"{label}.mode", maximum=4)
    if _MODE.fullmatch(mode) is None:
        _fail(f"{label}.mode must be a four-digit octal mode")
    _integer(artifact["uid"], f"{label}.uid", 0, 2**32 - 1)
    _integer(artifact["gid"], f"{label}.gid", 0, 2**32 - 1)
    if worker_file:
        relative = _string(
            artifact["relativePath"], f"{label}.relativePath", maximum=1024
        )
        parsed = PurePosixPath(relative)
        if parsed.is_absolute() or str(parsed) != relative or any(
            part in {"", ".", ".."} for part in parsed.parts
        ):
            _fail(f"{label}.relativePath must be canonical and relative")
    return artifact


def _runtime_tree_sha256(files: Sequence[Mapping[str, Any]]) -> str:
    digest = hashlib.sha256()
    for record in files:
        digest.update(str(record["relativePath"]).encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(record["byteLength"]).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(record["sha256"]).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _validate_runtime_profile_document(profile: dict[str, Any]) -> None:
    _exact_object(profile, _RUNTIME_PROFILE_ROOT_KEYS, "runtime profile")
    if profile["schema"] != RUNTIME_PROFILE_SCHEMA:
        _fail("unsupported runtime profile schema")
    _validate_generated_at(profile["generatedAt"])

    target = _exact_object(
        profile["target"],
        frozenset(
            {
                "abi",
                "apiLevel",
                "capabilitySet",
                "gid",
                "machine",
                "pythonVersion",
                "selinuxContext",
                "supplementaryGids",
                "uid",
                "versions",
            }
        ),
        "runtime profile target",
    )
    if (
        _integer(target["apiLevel"], "target.apiLevel", 1, 10_000) != 23
        or _string(target["abi"], "target.abi", maximum=32) != "arm64-v8a"
        or _string(target["machine"], "target.machine", maximum=32) != "aarch64"
        or not _string(
            target["pythonVersion"], "target.pythonVersion", maximum=32
        ).startswith("3.12.")
    ):
        _fail("runtime profile target is not the supported OH 6.1 runtime")
    _integer(target["uid"], "target.uid", 0, 2**32 - 1)
    _integer(target["gid"], "target.gid", 0, 2**32 - 1)
    target_groups = _runtime_integer_list(
        target["supplementaryGids"], "target.supplementaryGids"
    )
    target_capabilities = _runtime_string_list(
        target["capabilitySet"],
        "target.capabilitySet",
        maximum_items=len(LINUX_CAPABILITY_NAMES),
        allowed=frozenset(LINUX_CAPABILITY_NAMES),
    )
    _string(target["selinuxContext"], "target.selinuxContext", maximum=256)
    versions = _exact_object(
        target["versions"],
        frozenset(
            {
                "const.ohos.fullname",
                "const.ohos.version",
                "const.product.software.version",
            }
        ),
        "target.versions",
    )
    for key in sorted(versions):
        _string(versions[key], f"target.versions.{key}", maximum=256)

    closure = _exact_object(
        profile["runtimeClosure"],
        frozenset({"identity", "libraries", "python", "softbus", "softbusSocketCap"}),
        "runtimeClosure",
    )
    socket_cap = _integer(
        closure["softbusSocketCap"], "runtimeClosure.softbusSocketCap", 4, SOFTBUS_SOCKET_CAP_MAX
    )
    identity = _exact_object(
        closure["identity"],
        frozenset(
            {
                "capabilitySet",
                "gid",
                "launcher",
                "permission",
                "permissionGranted",
                "processName",
                "publicTokenIdAvailable",
                "sealedTokenIdHash",
                "selinuxContext",
                "supplementaryGids",
                "uid",
            }
        ),
        "runtimeClosure.identity",
    )
    if (
        identity["uid"] != target["uid"]
        or identity["gid"] != target["gid"]
        or tuple(identity["supplementaryGids"]) != target_groups
        or tuple(identity["capabilitySet"]) != target_capabilities
        or identity["selinuxContext"] != target["selinuxContext"]
        or identity["processName"] != "mclaw"
        or identity["permission"] != DISTRIBUTED_DATASYNC_PERMISSION
        or identity["permissionGranted"] is not True
        or identity["publicTokenIdAvailable"] is not False
        or not isinstance(identity["sealedTokenIdHash"], str)
        or _SHA256_TAG.fullmatch(identity["sealedTokenIdHash"]) is None
    ):
        _fail("runtime profile identity does not match its target")
    _validate_local_runtime_artifact(identity["launcher"], "runtimeClosure.identity.launcher")
    if identity["launcher"]["path"] != TOKEN_LAUNCHER:
        _fail("runtime profile token launcher path is not the product value")

    libraries = _exact_object(
        closure["libraries"],
        frozenset(
            {
                "bundledLibcxx",
                "cjBindFfi",
                "cjBindNative",
                "deviceManagerFfi",
                "permission",
                "releaseLibcxx",
                "shim",
                "systemLibcxx",
            }
        ),
        "runtimeClosure.libraries",
    )
    expected_libraries = {
        "bundledLibcxx": BUNDLED_LIBCXX_LIBRARY,
        "cjBindFfi": CJ_BIND_FFI_LIBRARY,
        "cjBindNative": CJ_BIND_NATIVE_LIBRARY,
        "deviceManagerFfi": DEVICE_MANAGER_FFI_LIBRARY,
        "permission": PERMISSION_LIBRARY,
        "releaseLibcxx": RELEASE_LIBCXX_LIBRARY,
        "shim": SHIM_LIBRARY,
        "systemLibcxx": SYSTEM_LIBCXX_LIBRARY,
    }
    for name, expected_path in expected_libraries.items():
        artifact = _validate_local_runtime_artifact(
            libraries[name], f"runtimeClosure.libraries.{name}"
        )
        if artifact["path"] != expected_path:
            _fail(f"runtimeClosure.libraries.{name}.path is not the product value")

    python = _exact_object(
        closure["python"],
        frozenset({"deployment", "dynamicLibpython", "executable", "releaseLibraryDirs"}),
        "runtimeClosure.python",
    )
    executable = _validate_local_runtime_artifact(
        python["executable"], "runtimeClosure.python.executable"
    )
    dynamic_libpython = _validate_local_runtime_artifact(
        python["dynamicLibpython"], "runtimeClosure.python.dynamicLibpython"
    )
    if (
        executable["path"] != PYTHON_EXECUTABLE
        or dynamic_libpython["path"] != PYTHON_DYNAMIC_LIBRARY
        or tuple(python["releaseLibraryDirs"]) != PYTHON_RELEASE_LIBRARY_DIRS
    ):
        _fail("runtime profile Python closure is not the product value")

    deployment = _exact_object(
        python["deployment"],
        frozenset(
            {
                "files",
                "manifest",
                "moduleTreeSha256",
                "workerBootstrapFile",
                "workerBootstrapSha256",
            }
        ),
        "runtimeClosure.python.deployment",
    )
    module_tree_sha = _sha256(
        deployment["moduleTreeSha256"],
        "runtimeClosure.python.deployment.moduleTreeSha256",
    )
    manifest = _validate_local_runtime_artifact(
        deployment["manifest"], "runtimeClosure.python.deployment.manifest"
    )
    manifest_path = PurePosixPath(manifest["path"])
    release_root = manifest_path.parent
    if (
        manifest_path.name != WORKER_CODE_MANIFEST_FILENAME
        or release_root.name != module_tree_sha
    ):
        _fail("runtime profile worker manifest layout is invalid")
    files_value = deployment["files"]
    if not isinstance(files_value, list) or len(files_value) != len(
        WORKER_MODULE_RELATIVE_PATHS
    ):
        _fail("runtime profile worker file set is invalid")
    files = [
        _validate_local_runtime_artifact(
            item, f"runtimeClosure.python.deployment.files[{index}]", worker_file=True
        )
        for index, item in enumerate(files_value)
    ]
    relatives = tuple(record["relativePath"] for record in files)
    if relatives != tuple(sorted(WORKER_MODULE_RELATIVE_PATHS)):
        _fail("runtime profile worker file paths are invalid")
    for record in files:
        expected_path = release_root / "site" / PurePosixPath(record["relativePath"])
        if record["path"] != str(expected_path):
            _fail("runtime profile worker file layout is invalid")
    if _runtime_tree_sha256(files) != module_tree_sha:
        _fail("runtime profile worker tree digest is invalid")
    bootstrap = next(
        record
        for record in files
        if record["relativePath"] == WORKER_BOOTSTRAP_RELATIVE_PATH
    )
    if (
        deployment["workerBootstrapFile"] != bootstrap["path"]
        or deployment["workerBootstrapSha256"] != bootstrap["sha256"]
    ):
        _fail("runtime profile worker bootstrap identity is invalid")

    softbus = _exact_object(
        profile["softbus"],
        frozenset({"library", "requiredExports", "transportAcl"}),
        "runtime profile softbus",
    )
    softbus_library = _validate_local_runtime_artifact(
        softbus["library"], "runtime profile softbus.library"
    )
    transport_acl = _exact_object(
        softbus["transportAcl"],
        frozenset(
            {
                "actions",
                "applicationType",
                "artifact",
                "deviceIdType",
                "packageName",
                "regexp",
                "securityLevel",
                "sessionNames",
                "uid",
            }
        ),
        "runtime profile softbus.transportAcl",
    )
    _validate_local_runtime_artifact(
        transport_acl["artifact"], "runtime profile softbus.transportAcl.artifact"
    )
    if (
        softbus_library["path"] != REMOTE_SOFTBUS_LIBRARY
        or closure["softbus"] != softbus_library
        or tuple(softbus["requiredExports"]) != REQUIRED_SOFTBUS_EXPORTS
        or transport_acl["sessionNames"] != [
            "mclaw.a2a.v1",
            "mclaw.a2a.v1.client",
        ]
        or transport_acl["regexp"] is not False
        or transport_acl["deviceIdType"] != "NETWORKID"
        or transport_acl["securityLevel"] != "public"
        or transport_acl["applicationType"] != "native_app"
        or transport_acl["uid"] != 0
        or transport_acl["packageName"] != "mclaw"
        or transport_acl["actions"] != "create,open"
    ):
        _fail("runtime profile SoftBus closure is not the product value")
    if socket_cap < 1 + 1 + 2:
        _fail("runtime profile Socket cap is below the product minimum")

    fingerprint_source = {
        "runtimeClosure": profile["runtimeClosure"],
        "softbus": profile["softbus"],
        "target": profile["target"],
    }
    expected_fingerprint = "sha256:" + hashlib.sha256(
        canonical_json_bytes(fingerprint_source)
    ).hexdigest()
    if profile["systemFingerprint"] != expected_fingerprint:
        _fail("runtime profile system fingerprint mismatch")


def load_runtime_profile(path: str | os.PathLike[str]) -> RuntimeProfile:
    """Load one canonical device-local activation Profile."""

    profile_path = Path(path)
    if not profile_path.is_absolute() or profile_path.name != RUNTIME_PROFILE_FILENAME:
        _fail(
            f"runtime profile path must be absolute and end in {RUNTIME_PROFILE_FILENAME}"
        )
    raw = _read_regular_no_follow(
        profile_path,
        maximum=RUNTIME_PROFILE_BYTES_MAX,
        label="runtime profile",
    )
    try:
        value = strict_json_loads(
            raw,
            max_bytes=RUNTIME_PROFILE_BYTES_MAX,
            require_canonical=True,
            require_object=True,
        )
    except ProtocolError as error:
        raise BaselineError("PROFILE_INVALID", error.detail) from error
    profile = _exact_object(value, _RUNTIME_PROFILE_ROOT_KEYS, "runtime profile")
    _validate_runtime_profile_document(profile)
    return RuntimeProfile(
        path=profile_path.absolute(),
        sha256=hashlib.sha256(raw).hexdigest(),
        byte_length=len(raw),
        document=_freeze(profile),
    )


def _verify_runtime_profile_artifact(
    record: Mapping[str, Any],
    label: str,
) -> bytes:
    try:
        selected = Path(str(record["path"])).resolve(strict=True)
    except OSError as error:
        raise BaselineError("RUNTIME_CLOSURE_MISMATCH", f"cannot resolve {label}") from error
    expected = Path(str(record["resolvedPath"]))
    if selected != expected:
        _fail(f"{label} resolved path changed", code="RUNTIME_CLOSURE_MISMATCH")
    raw = _read_regular_no_follow(
        expected,
        maximum=int(record["byteLength"]),
        label=label,
        validation_code="RUNTIME_CLOSURE_MISMATCH",
        read_code="RUNTIME_CLOSURE_MISMATCH",
    )
    if (
        len(raw) != record["byteLength"]
        or hashlib.sha256(raw).hexdigest() != record["sha256"]
    ):
        _fail(f"{label} bytes changed", code="RUNTIME_CLOSURE_MISMATCH")
    try:
        metadata = expected.stat(follow_symlinks=False)
    except OSError as error:
        raise BaselineError("RUNTIME_CLOSURE_MISMATCH", f"cannot stat {label}") from error
    if (
        f"0{stat.S_IMODE(metadata.st_mode):03o}" != record["mode"]
        or getattr(metadata, "st_uid", record["uid"]) != record["uid"]
        or getattr(metadata, "st_gid", record["gid"]) != record["gid"]
    ):
        _fail(f"{label} filesystem identity changed", code="RUNTIME_CLOSURE_MISMATCH")
    return raw


def preflight_runtime_profile(
    path: str | os.PathLike[str],
    *,
    expected_profile_sha256: str,
    observed: ObservedRuntimeIdentity,
) -> RuntimePreflightResult:
    """Verify the local Profile and current closure without loading Native DSOs."""

    if _HEX64.fullmatch(expected_profile_sha256) is None:
        _fail("expected profile SHA-256 is invalid", code="PROFILE_HASH_MISMATCH")
    profile = load_runtime_profile(path)
    if profile.sha256 != expected_profile_sha256:
        _fail("runtime profile SHA-256 mismatch", code="PROFILE_HASH_MISMATCH")
    target = profile.document["target"]
    expected_identity = (
        target["apiLevel"],
        target["abi"],
        target["machine"],
        target["uid"],
        target["gid"],
        target["selinuxContext"],
    )
    actual_identity = (
        observed.api_level,
        observed.abi,
        observed.machine,
        observed.uid,
        observed.gid,
        observed.selinux_context,
    )
    if actual_identity != expected_identity:
        _fail("runtime target/identity differs from the profile", code="TARGET_IDENTITY_MISMATCH")

    closure = profile.document["runtimeClosure"]
    records: list[tuple[str, Mapping[str, Any]]] = [
        ("SoftBus client library", profile.document["softbus"]["library"]),
        (
            "SoftBus transport ACL",
            profile.document["softbus"]["transportAcl"]["artifact"],
        ),
        ("Python executable", closure["python"]["executable"]),
        ("dynamic libpython", closure["python"]["dynamicLibpython"]),
        ("token launcher", closure["identity"]["launcher"]),
    ]
    records.extend(
        (f"runtime library {name}", record)
        for name, record in closure["libraries"].items()
    )
    deployment = closure["python"]["deployment"]
    records.append(("worker code manifest", deployment["manifest"]))
    records.extend(
        (f"worker code file {record['relativePath']}", record)
        for record in deployment["files"]
    )
    verified: dict[str, bytes] = {}
    for label, record in records:
        verified[str(record["path"])] = _verify_runtime_profile_artifact(record, label)

    manifest_record = deployment["manifest"]
    manifest_raw = verified[str(manifest_record["path"])]
    try:
        manifest = strict_json_loads(
            manifest_raw,
            max_bytes=65_536,
            require_canonical=True,
            require_object=True,
        )
    except ProtocolError as error:
        raise BaselineError("WORKER_CODE_MISMATCH", error.detail) from error
    expected_manifest = {
        "bootstrap": WORKER_BOOTSTRAP_RELATIVE_PATH,
        "files": [
            {
                "byteLength": record["byteLength"],
                "path": record["relativePath"],
                "sha256": record["sha256"],
            }
            for record in deployment["files"]
        ],
        "packageRoot": "site",
        "schema": WORKER_CODE_MANIFEST_SCHEMA,
        "treeSha256": deployment["moduleTreeSha256"],
    }
    if manifest != expected_manifest:
        _fail("worker code manifest differs from Profile", code="WORKER_CODE_MISMATCH")

    return RuntimePreflightResult(
        status="runtime-closure-ready",
        profile_sha256=profile.sha256,
        softbus_sha256=profile.document["softbus"]["library"]["sha256"],
        softbus_socket_cap=closure["softbusSocketCap"],
    )


__all__ = [
    "BaselineError",
    "BUNDLED_LIBCXX_LIBRARY",
    "CJ_BIND_FFI_LIBRARY",
    "CJ_BIND_NATIVE_LIBRARY",
    "DEVICE_MANAGER_FFI_LIBRARY",
    "DISTRIBUTED_DATASYNC_PERMISSION",
    "LINUX_CAPABILITY_NAMES",
    "ObservedRuntimeIdentity",
    "PERMISSION_LIBRARY",
    "PYTHON_DYNAMIC_LIBRARY",
    "PYTHON_EXECUTABLE",
    "PYTHON_RELEASE_LIBRARY_DIRS",
    "RELEASE_LIBCXX_LIBRARY",
    "REMOTE_SOFTBUS_LIBRARY",
    "REQUIRED_SOFTBUS_EXPORTS",
    "RUNTIME_PROFILE_BYTES_MAX",
    "RUNTIME_PROFILE_FILENAME",
    "RUNTIME_PROFILE_SCHEMA",
    "RuntimePreflightResult",
    "RuntimeProfile",
    "SHIM_LIBRARY",
    "SYSTEM_LIBCXX_LIBRARY",
    "TOKEN_LAUNCHER",
    "WORKER_BOOTSTRAP_RELATIVE_PATH",
    "WORKER_CODE_MANIFEST_FILENAME",
    "WORKER_CODE_MANIFEST_SCHEMA",
    "WORKER_MODULE_RELATIVE_PATHS",
    "load_runtime_profile",
    "preflight_runtime_profile",
]
