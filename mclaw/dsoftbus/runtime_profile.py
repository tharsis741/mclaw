# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Device-local DSoftBus activation Profile collection and refresh.

The user path never consumes a host-generated deployment descriptor.  This
module derives the current runtime closure from the installed Kaihong runtime,
materializes the isolated Python Worker bundle from the installed M-Claw, and
atomically maintains ``MCLAW_HOME/dsoftbus/runtime-profile.json``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import stat
import time
from types import MappingProxyType
from typing import Any, Mapping, NoReturn
import uuid

from mclaw.runtime.bootstrap import BootstrapPathResolver

from . import baseline as baseline_contract
from .baseline import (
    BUNDLED_LIBCXX_LIBRARY,
    CJ_BIND_FFI_LIBRARY,
    CJ_BIND_NATIVE_LIBRARY,
    DEVICE_MANAGER_FFI_LIBRARY,
    DISTRIBUTED_DATASYNC_PERMISSION,
    LINUX_CAPABILITY_NAMES,
    ObservedRuntimeIdentity,
    PERMISSION_LIBRARY,
    PYTHON_DYNAMIC_LIBRARY,
    PYTHON_EXECUTABLE,
    PYTHON_RELEASE_LIBRARY_DIRS,
    RELEASE_LIBCXX_LIBRARY,
    REMOTE_SOFTBUS_LIBRARY,
    REQUIRED_SOFTBUS_EXPORTS,
    RUNTIME_PROFILE_BYTES_MAX,
    RUNTIME_PROFILE_FILENAME,
    RUNTIME_PROFILE_SCHEMA,
    SHIM_LIBRARY,
    SYSTEM_LIBCXX_LIBRARY,
    TOKEN_LAUNCHER,
    WORKER_BOOTSTRAP_RELATIVE_PATH,
    WORKER_CODE_MANIFEST_FILENAME,
    WORKER_CODE_MANIFEST_SCHEMA,
    WORKER_MODULE_RELATIVE_PATHS,
    BaselineError,
    RuntimeProfile,
    load_runtime_profile,
    preflight_runtime_profile,
)
from .protocol import canonical_json_bytes
from .manifest import (
    MANIFEST_SCHEMA,
    LocalManifestTemplate,
    ManifestError,
    encode_local_manifest,
    parse_local_manifest_template,
)


TOKEN_DATABASE = Path("/data/service/el0/access_token/nativetoken.json")
SOFTBUS_TRANSPORT_ACL = Path(
    "/system/etc/communication/softbus/softbus_trans_permission.json"
)
DEFAULT_SOCKET_CAP = 16
PRODUCT_SUPPLEMENTARY_GIDS = (1006, 1007, 2000, 3009)
_TOKEN_DOMAIN = b"mclaw-dsoftbus-token-id\0"
_VERSION_KEYS = (
    "const.ohos.fullname",
    "const.ohos.version",
    "const.product.software.version",
)
_DEVICE_PARAMETER_KEYS = (
    "const.product.manufacturer",
    "const.product.brand",
    "const.product.name",
    "const.product.model",
    "const.ohos.apiversion",
    "const.product.cpu.abilist",
)
_SYSTEM_PARAMETER_KEYS = (*_VERSION_KEYS, *_DEVICE_PARAMETER_KEYS)
_PARAMETER_PLACEHOLDERS = frozenset(
    {"default", "generic", "n/a", "none", "null", "unknown", "undefined", "unset"}
)
_GENERIC_PRODUCT_MODELS = frozenset({"generic", "ohos", "openharmony"})
_OH61_VERSION = re.compile(r"(?<!\d)6\.1(?!\d)")
_VERSION_COMPONENT = re.compile(
    r"(?<![0-9A-Za-z])"
    r"(?P<version>[0-9]+(?:\.[0-9A-Za-z]+){1,7}(?:[-+][0-9A-Za-z._-]+)?)"
    r"(?![0-9A-Za-z])"
)
_MAX_INT64 = 2**63 - 1
_PROFILE_SOURCE_MAX = 1_073_741_824
_WORKER_SOURCE_MAX = 1_048_576
_TOKEN_DATABASE_MAX = 1_048_576


class RuntimeProfileError(RuntimeError):
    """Stable, non-sensitive local activation failure."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code if not detail else f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _fail(code: str, detail: str = "") -> NoReturn:
    raise RuntimeProfileError(code, detail)


@dataclass(frozen=True, slots=True)
class PreparedRuntimeProfile:
    profile: RuntimeProfile
    raw_token_id: str
    current_boot_id: str


@dataclass(frozen=True, slots=True)
class _OhosDeviceIdentity:
    manufacturer: str
    model: str
    display_name: str
    os_name: str
    os_version: str
    api_level: int
    abi: str
    machine: str
    versions: Mapping[str, str]


def _read_regular_no_follow(path: Path, *, maximum: int, code: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise RuntimeProfileError(code, f"cannot open {path}") from error
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size < 0
            or before.st_size > maximum
        ):
            _fail(code, f"invalid regular file {path}")
        chunks: list[bytes] = []
        remaining = before.st_size if before.st_size else maximum + 1
        while remaining:
            block = os.read(descriptor, min(1_048_576, remaining))
            if not block:
                break
            chunks.append(block)
            remaining -= len(block)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        if (
            not raw
            or len(raw) > maximum
            or (before.st_size and len(raw) != before.st_size)
            or (
                before.st_dev,
                before.st_ino,
                before.st_mode,
                before.st_size,
                before.st_mtime_ns,
            )
            != (
                after.st_dev,
                after.st_ino,
                after.st_mode,
                after.st_size,
                after.st_mtime_ns,
            )
        ):
            _fail(code, f"file changed while reading {path}")
        return raw
    finally:
        os.close(descriptor)


def _artifact(path_value: str, *, code: str = "RUNTIME_COMPONENT_MISSING") -> dict[str, Any]:
    path = Path(path_value)
    if not path.is_absolute():
        _fail(code, f"component path is not absolute: {path_value}")
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise RuntimeProfileError(code, f"component is missing: {path_value}") from error
    raw = _read_regular_no_follow(resolved, maximum=_PROFILE_SOURCE_MAX, code=code)
    try:
        metadata = resolved.stat(follow_symlinks=False)
    except OSError as error:
        raise RuntimeProfileError(code, f"cannot stat component: {path_value}") from error
    return {
        "byteLength": len(raw),
        "gid": int(getattr(metadata, "st_gid", os.getgid())),
        "mode": f"0{stat.S_IMODE(metadata.st_mode):03o}",
        "path": PurePosixPath(path_value).as_posix(),
        "resolvedPath": PurePosixPath(resolved.as_posix()).as_posix(),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "uid": int(getattr(metadata, "st_uid", os.getuid())),
    }


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ensure_private_directory(path: Path) -> None:
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = path.lstat()
    except OSError as error:
        raise RuntimeProfileError("RUNTIME_STATE_UNAVAILABLE", str(path)) from error
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        _fail("RUNTIME_STATE_UNAVAILABLE", f"state path is not a directory: {path}")
    try:
        os.chmod(path, 0o700)
    except OSError as error:
        raise RuntimeProfileError("RUNTIME_STATE_UNAVAILABLE", str(path)) from error


def _acquire_collection_lock(state_root: Path) -> int:
    if os.name != "posix":
        _fail("OH61_RUNTIME_UNSUPPORTED")
    import fcntl

    path = state_root / ".runtime-profile.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            _fail("RUNTIME_STATE_UNAVAILABLE", "Profile lock is not a file")
        os.fchmod(descriptor, 0o600)
    except BaseException:
        if "descriptor" in locals():
            os.close(descriptor)
        raise
    deadline = time.monotonic() + 10.0
    while True:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return descriptor
        except BlockingIOError:
            if time.monotonic() >= deadline:
                os.close(descriptor)
                _fail("RUNTIME_PROFILE_BUSY")
            time.sleep(0.05)
        except OSError as error:
            os.close(descriptor)
            raise RuntimeProfileError("RUNTIME_STATE_UNAVAILABLE") from error


def _release_collection_lock(descriptor: int) -> None:
    try:
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _worker_source_root() -> Path:
    root = Path(__file__).resolve().parents[1]
    if root.name != "mclaw" or not root.is_dir():
        _fail("WORKER_BUNDLE_SOURCE_INVALID")
    return root


def _worker_source_bytes(relative: str) -> bytes:
    parts = PurePosixPath(relative).parts
    if not parts or parts[0] != "mclaw":
        _fail("WORKER_BUNDLE_SOURCE_INVALID", relative)
    source = _worker_source_root().joinpath(*parts[1:]).resolve(strict=True)
    return _read_regular_no_follow(
        source,
        maximum=_WORKER_SOURCE_MAX,
        code="WORKER_BUNDLE_SOURCE_INVALID",
    )


def _worker_tree_sha256(records: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for record in records:
        digest.update(record["path"].encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(record["byteLength"]).encode("ascii"))
        digest.update(b"\0")
        digest.update(record["sha256"].encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _write_new_file(path: Path, raw: bytes, *, mode: int) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags, mode)
    try:
        offset = 0
        while offset < len(raw):
            written = os.write(descriptor, raw[offset:])
            if written <= 0:
                _fail("RUNTIME_STATE_UNAVAILABLE", f"short write: {path}")
            offset += written
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _verify_worker_release(
    release: Path,
    *,
    tree_sha256: str,
    records: list[dict[str, Any]],
    manifest_raw: bytes,
) -> None:
    if release.is_symlink() or not release.is_dir() or release.name != tree_sha256:
        _fail("WORKER_BUNDLE_INVALID", str(release))
    manifest_path = release / WORKER_CODE_MANIFEST_FILENAME
    if _read_regular_no_follow(
        manifest_path, maximum=65_536, code="WORKER_BUNDLE_INVALID"
    ) != manifest_raw:
        _fail("WORKER_BUNDLE_INVALID", "manifest mismatch")
    for record in records:
        path = release / "site" / Path(*PurePosixPath(record["path"]).parts)
        raw = _read_regular_no_follow(
            path,
            maximum=record["byteLength"],
            code="WORKER_BUNDLE_INVALID",
        )
        if (
            len(raw) != record["byteLength"]
            or hashlib.sha256(raw).hexdigest() != record["sha256"]
        ):
            _fail("WORKER_BUNDLE_INVALID", record["path"])


def _materialize_worker_bundle(state_root: Path) -> tuple[Path, list[dict[str, Any]], bytes]:
    source_records: list[dict[str, Any]] = []
    source_bytes: dict[str, bytes] = {}
    for relative in sorted(WORKER_MODULE_RELATIVE_PATHS):
        raw = _worker_source_bytes(relative)
        source_bytes[relative] = raw
        source_records.append(
            {
                "byteLength": len(raw),
                "path": relative,
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
    tree_sha256 = _worker_tree_sha256(source_records)
    manifest = {
        "bootstrap": WORKER_BOOTSTRAP_RELATIVE_PATH,
        "files": source_records,
        "packageRoot": "site",
        "schema": WORKER_CODE_MANIFEST_SCHEMA,
        "treeSha256": tree_sha256,
    }
    manifest_raw = canonical_json_bytes(manifest)

    releases = state_root / "worker" / "releases"
    _ensure_private_directory(releases)
    release = releases / tree_sha256
    if release.exists() or release.is_symlink():
        _verify_worker_release(
            release,
            tree_sha256=tree_sha256,
            records=source_records,
            manifest_raw=manifest_raw,
        )
        return release, source_records, manifest_raw

    staging = releases / f".{tree_sha256}.staging-{uuid.uuid4().hex}"
    published = False
    try:
        (staging / "site").mkdir(mode=0o700, parents=True)
        for relative, raw in source_bytes.items():
            destination = staging / "site" / Path(*PurePosixPath(relative).parts)
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            _write_new_file(destination, raw, mode=0o400)
        _write_new_file(staging / WORKER_CODE_MANIFEST_FILENAME, manifest_raw, mode=0o400)
        for directory in sorted(
            (path for path in staging.rglob("*") if path.is_dir()),
            key=lambda value: len(value.parts),
            reverse=True,
        ):
            os.chmod(directory, 0o700)
            _fsync_directory(directory)
        _fsync_directory(staging)
        try:
            os.rename(staging, release)
            published = True
        except FileExistsError:
            pass
        _fsync_directory(releases)
    except RuntimeProfileError:
        raise
    except OSError as error:
        raise RuntimeProfileError("WORKER_BUNDLE_WRITE_FAILED") from error
    finally:
        if not published and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)

    _verify_worker_release(
        release,
        tree_sha256=tree_sha256,
        records=source_records,
        manifest_raw=manifest_raw,
    )
    return release, source_records, manifest_raw


def _worker_deployment(state_root: Path) -> dict[str, Any]:
    release, source_records, _ = _materialize_worker_bundle(state_root)
    file_records: list[dict[str, Any]] = []
    for source in source_records:
        relative = source["path"]
        record = _artifact(
            (release / "site" / Path(*PurePosixPath(relative).parts)).as_posix(),
            code="WORKER_BUNDLE_INVALID",
        )
        record["relativePath"] = relative
        file_records.append(record)
    bootstrap = next(
        item
        for item in file_records
        if item["relativePath"] == WORKER_BOOTSTRAP_RELATIVE_PATH
    )
    return {
        "files": file_records,
        "manifest": _artifact(
            (release / WORKER_CODE_MANIFEST_FILENAME).as_posix(),
            code="WORKER_BUNDLE_INVALID",
        ),
        "moduleTreeSha256": release.name,
        "workerBootstrapFile": bootstrap["path"],
        "workerBootstrapSha256": bootstrap["sha256"],
    }


def _json_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail("MCLAW_TOKEN_INVALID", "duplicate JSON key")
        result[key] = value
    return result


def _read_mclaw_token_id() -> str:
    raw = _read_regular_no_follow(
        TOKEN_DATABASE,
        maximum=_TOKEN_DATABASE_MAX,
        code="MCLAW_TOKEN_MISSING",
    )
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_json_pairs)
    except RuntimeProfileError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise RuntimeProfileError("MCLAW_TOKEN_INVALID") from error
    if not isinstance(value, list):
        _fail("MCLAW_TOKEN_INVALID")
    matches = [
        item for item in value if isinstance(item, dict) and item.get("processName") == "mclaw"
    ]
    if len(matches) != 1:
        _fail("MCLAW_TOKEN_MISSING" if not matches else "MCLAW_TOKEN_INVALID")
    token = matches[0]
    token_id = token.get("tokenId")
    permissions = token.get("permissions")
    if (
        type(token_id) is not int
        or not 1 <= token_id <= 2**64 - 1
        or permissions != [DISTRIBUTED_DATASYNC_PERMISSION]
    ):
        _fail("MCLAW_TOKEN_INVALID")
    return str(token_id)


def _selinux_context() -> str:
    raw = _read_regular_no_follow(
        Path("/proc/self/attr/current"), maximum=256, code="RUNTIME_IDENTITY_UNAVAILABLE"
    )
    try:
        value = raw.decode("utf-8").rstrip("\x00\r\n")
    except UnicodeDecodeError as error:
        raise RuntimeProfileError("RUNTIME_IDENTITY_UNAVAILABLE") from error
    if not value:
        _fail("RUNTIME_IDENTITY_UNAVAILABLE")
    return value


def _capability_set() -> tuple[str, ...]:
    raw = _read_regular_no_follow(
        Path("/proc/self/status"), maximum=65_536, code="RUNTIME_IDENTITY_UNAVAILABLE"
    )
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise RuntimeProfileError("RUNTIME_IDENTITY_UNAVAILABLE") from error
    values = {
        key: value.strip()
        for line in lines
        if ":" in line
        for key, value in (line.split(":", 1),)
    }
    encoded = values.get("CapEff", "")
    try:
        mask = int(encoded, 16)
    except ValueError as error:
        raise RuntimeProfileError("RUNTIME_IDENTITY_UNAVAILABLE") from error
    if not encoded or mask >> len(LINUX_CAPABILITY_NAMES):
        _fail("RUNTIME_IDENTITY_UNAVAILABLE")
    return tuple(
        name
        for bit, name in enumerate(LINUX_CAPABILITY_NAMES)
        if mask & (1 << bit)
    )


def _read_system_parameters() -> dict[str, str]:
    """Read only the allowlisted public system facts needed by activation."""

    from mclaw.platform.detect import _read_live_parameter, _valid_parameter_value

    static = dict(BootstrapPathResolver.read_ohos_parameters())
    result = {
        key: _valid_parameter_value(str(static.get(key) or ""))
        for key in _SYSTEM_PARAMETER_KEYS
    }
    missing = [key for key, value in result.items() if not value]
    if missing:
        executable = shutil.which("param")
        if not executable and Path("/bin/param").is_file():
            executable = "/bin/param"
        if executable:
            for key in missing:
                value = _read_live_parameter(executable, key)
                if value:
                    result[key] = value
    return result


def _meaningful_parameter(value: str, *, rejected: frozenset[str] = frozenset()) -> str:
    text = str(value or "").strip()
    if not text or text.casefold() in _PARAMETER_PLACEHOLDERS | rejected:
        return ""
    return text


def _first_meaningful(
    parameters: Mapping[str, str],
    *keys: str,
    rejected: frozenset[str] = frozenset(),
) -> str:
    for key in keys:
        value = _meaningful_parameter(parameters.get(key, ""), rejected=rejected)
        if value:
            return value
    return ""


def _split_os_identity(
    ohos_version: str, ohos_fullname: str
) -> tuple[str, str]:
    primary = _VERSION_COMPONENT.search(ohos_version)
    if primary is None:
        _fail("DEVICE_IDENTITY_UNAVAILABLE")
    version = primary.group("version")
    name = ohos_version[: primary.start()].strip(" \t-_/.")
    if not name:
        fallback = _VERSION_COMPONENT.search(ohos_fullname)
        if fallback is not None:
            name = ohos_fullname[: fallback.start()].strip(" \t-_/.")
    if not _meaningful_parameter(name) or not _meaningful_parameter(version):
        _fail("DEVICE_IDENTITY_UNAVAILABLE")
    return name, version


def _device_identity_from_parameters(
    parameters: Mapping[str, str], *, machine: str
) -> _OhosDeviceIdentity:
    versions = {
        key: str(parameters.get(key) or "").strip() for key in _VERSION_KEYS
    }
    if any(
        not value or _OH61_VERSION.search(value) is None
        for value in versions.values()
    ):
        _fail("OH61_IDENTITY_UNAVAILABLE")

    normalized_machine = str(machine or "").strip().casefold()
    if normalized_machine == "arm64":
        normalized_machine = "aarch64"
    api_text = str(parameters.get("const.ohos.apiversion") or "").strip()
    abi_values = tuple(
        value.strip().casefold()
        for value in str(parameters.get("const.product.cpu.abilist") or "").split(",")
        if value.strip()
    )
    if re.fullmatch(r"[1-9][0-9]{0,3}", api_text) is None or not abi_values:
        _fail("DEVICE_IDENTITY_UNAVAILABLE")
    api_level = int(api_text)
    if (
        api_level != 23
        or normalized_machine != "aarch64"
        or "arm64-v8a" not in abi_values
    ):
        _fail("OH61_RUNTIME_UNSUPPORTED")

    manufacturer = _first_meaningful(
        parameters, "const.product.manufacturer", "const.product.brand"
    )
    product_name = _first_meaningful(parameters, "const.product.name")
    model = _first_meaningful(
        parameters,
        "const.product.model",
        rejected=_GENERIC_PRODUCT_MODELS,
    )
    if not model:
        model = product_name
    display_name = product_name or model
    if not manufacturer or not model or not display_name:
        _fail("DEVICE_IDENTITY_UNAVAILABLE")
    os_name, os_version = _split_os_identity(
        versions["const.ohos.version"], versions["const.ohos.fullname"]
    )
    return _OhosDeviceIdentity(
        manufacturer=manufacturer,
        model=model,
        display_name=display_name,
        os_name=os_name,
        os_version=os_version,
        api_level=api_level,
        abi="arm64-v8a",
        machine=normalized_machine,
        versions=MappingProxyType(versions),
    )


def _boot_id() -> str:
    raw = _read_regular_no_follow(
        Path("/proc/sys/kernel/random/boot_id"),
        maximum=64,
        code="BOOT_ID_UNAVAILABLE",
    )
    try:
        value = raw.decode("ascii").strip()
        parsed = uuid.UUID(value)
    except (UnicodeDecodeError, ValueError) as error:
        raise RuntimeProfileError("BOOT_ID_UNAVAILABLE") from error
    if str(parsed) != value:
        _fail("BOOT_ID_UNAVAILABLE")
    return value


def _plain_manifest_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain_manifest_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_manifest_value(item) for item in value]
    return value


def _manifest_core(identity: _OhosDeviceIdentity) -> dict[str, Any]:
    return {
        "schemaVersion": MANIFEST_SCHEMA,
        "device": {
            "manufacturer": identity.manufacturer,
            "model": identity.model,
            "displayName": identity.display_name,
            "os": {
                "name": identity.os_name,
                "version": identity.os_version,
                "apiLevel": identity.api_level,
                "arch": identity.machine,
            },
        },
        "resources": [
            {
                "resourceId": "host.system",
                "type": "system",
                "name": "Host system",
                "capabilities": ["status"],
                "operations": ["read"],
            }
        ],
        "bindings": {"host.system": {"reader": "system", "config": {}}},
    }


def _installed_manifest(path: Path) -> LocalManifestTemplate:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise RuntimeProfileError("MANIFEST_INSTALL_INVALID") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        _fail("MANIFEST_INSTALL_INVALID")
    if os.name == "posix" and (
        metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        _fail("MANIFEST_INSTALL_INVALID")
    raw = _read_regular_no_follow(
        path, maximum=65_536, code="MANIFEST_INSTALL_INVALID"
    )
    try:
        return parse_local_manifest_template(raw)
    except ManifestError as error:
        raise RuntimeProfileError("MANIFEST_INSTALL_INVALID") from error


def _ensure_manifest(state_root: Path, identity: _OhosDeviceIdentity) -> Path:
    """Create or refresh the product-owned Manifest from this device's facts."""

    destination = state_root / "device.yaml"
    desired_core = _manifest_core(identity)
    current: LocalManifestTemplate | None = None
    if destination.exists() or destination.is_symlink():
        current = _installed_manifest(destination)
        current_core = {
            key: _plain_manifest_value(current.document[key])
            for key in ("schemaVersion", "device", "resources", "bindings")
        }
        if current_core == desired_core:
            return destination

    revision = 1 if current is None else current.revision + 1
    if revision > _MAX_INT64:
        _fail("MANIFEST_INSTALL_INVALID")
    document = {
        "schemaVersion": desired_core["schemaVersion"],
        "revision": revision,
        "generatedAt": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "device": desired_core["device"],
        "resources": desired_core["resources"],
        "bindings": desired_core["bindings"],
    }
    try:
        raw = encode_local_manifest(document)
    except ManifestError as error:
        raise RuntimeProfileError("DEVICE_IDENTITY_UNAVAILABLE") from error

    temporary = state_root / f".device.yaml.staging-{uuid.uuid4().hex}"
    try:
        _write_new_file(temporary, raw, mode=0o600)
        os.replace(temporary, destination)
        _fsync_directory(state_root)
        published = _installed_manifest(destination)
        if _plain_manifest_value(published.document) != document:
            _fail("MANIFEST_INSTALL_INVALID")
    except RuntimeProfileError:
        raise
    except OSError as error:
        raise RuntimeProfileError("MANIFEST_INSTALL_INVALID") from error
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)
    return destination


def _transport_acl() -> dict[str, Any]:
    raw = _read_regular_no_follow(
        SOFTBUS_TRANSPORT_ACL,
        maximum=1_048_576,
        code="SOFTBUS_ACL_INVALID",
    )
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_json_pairs)
    except RuntimeProfileError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise RuntimeProfileError("SOFTBUS_ACL_INVALID") from error
    if not isinstance(value, list):
        _fail("SOFTBUS_ACL_INVALID")
    expected_names = ("mclaw.a2a.v1", "mclaw.a2a.v1.client")
    matches: dict[str, dict[str, Any]] = {}
    for item in value:
        if not isinstance(item, dict) or item.get("SESSION_NAME") not in expected_names:
            continue
        name = item["SESSION_NAME"]
        if name in matches or frozenset(item) != frozenset(
            {"SESSION_NAME", "REGEXP", "DEVID", "SEC_LEVEL", "APP_INFO"}
        ):
            _fail("SOFTBUS_ACL_INVALID")
        applications = item.get("APP_INFO")
        if not isinstance(applications, list) or len(applications) != 1:
            _fail("SOFTBUS_ACL_INVALID")
        application = applications[0]
        if (
            not isinstance(application, dict)
            or frozenset(application)
            != frozenset({"TYPE", "UID", "PKG_NAME", "ACTIONS"})
            or item.get("REGEXP") != "false"
            or item.get("DEVID") != "NETWORKID"
            or item.get("SEC_LEVEL") != "public"
            or application.get("TYPE") != "native_app"
            or application.get("UID") != "0"
            or application.get("PKG_NAME") != "mclaw"
            or application.get("ACTIONS") != "create,open"
        ):
            _fail("SOFTBUS_ACL_INVALID")
        matches[name] = item
    if tuple(name for name in expected_names if name in matches) != expected_names:
        _fail("SOFTBUS_ACL_INVALID")
    return {
        "actions": "create,open",
        "applicationType": "native_app",
        "artifact": _artifact(
            SOFTBUS_TRANSPORT_ACL.as_posix(), code="SOFTBUS_ACL_INVALID"
        ),
        "deviceIdType": "NETWORKID",
        "packageName": "mclaw",
        "regexp": False,
        "securityLevel": "public",
        "sessionNames": list(expected_names),
        "uid": 0,
    }


def _build_document(
    state_root: Path,
    raw_token_id: str,
    identity: _OhosDeviceIdentity,
) -> dict[str, Any]:
    if not platform.python_version().startswith("3.12."):
        _fail("OH61_RUNTIME_UNSUPPORTED")
    capability_set = _capability_set()
    selinux_context = _selinux_context()
    uid = os.getuid()
    gid = os.getgid()
    if uid != 0 or gid != 0 or selinux_context != "u:r:su:s0":
        _fail("RUNTIME_IDENTITY_UNAVAILABLE")
    supplementary_gids = PRODUCT_SUPPLEMENTARY_GIDS
    token_hash = "sha256:" + hashlib.sha256(
        _TOKEN_DOMAIN + raw_token_id.encode("ascii")
    ).hexdigest()

    softbus = _artifact(REMOTE_SOFTBUS_LIBRARY)
    libraries = {
        "bundledLibcxx": _artifact(BUNDLED_LIBCXX_LIBRARY),
        "cjBindFfi": _artifact(CJ_BIND_FFI_LIBRARY),
        "cjBindNative": _artifact(CJ_BIND_NATIVE_LIBRARY),
        "deviceManagerFfi": _artifact(DEVICE_MANAGER_FFI_LIBRARY),
        "permission": _artifact(PERMISSION_LIBRARY),
        "releaseLibcxx": _artifact(RELEASE_LIBCXX_LIBRARY),
        "shim": _artifact(SHIM_LIBRARY),
        "systemLibcxx": _artifact(SYSTEM_LIBCXX_LIBRARY),
    }
    target = {
        "abi": identity.abi,
        "apiLevel": identity.api_level,
        "capabilitySet": list(capability_set),
        "gid": gid,
        "machine": identity.machine,
        "pythonVersion": platform.python_version(),
        "selinuxContext": selinux_context,
        "supplementaryGids": list(supplementary_gids),
        "uid": uid,
        "versions": dict(identity.versions),
    }
    closure = {
        "identity": {
            "capabilitySet": list(capability_set),
            "gid": gid,
            "launcher": _artifact(TOKEN_LAUNCHER),
            "permission": DISTRIBUTED_DATASYNC_PERMISSION,
            "permissionGranted": True,
            "processName": "mclaw",
            "publicTokenIdAvailable": False,
            "sealedTokenIdHash": token_hash,
            "selinuxContext": selinux_context,
            "supplementaryGids": list(supplementary_gids),
            "uid": uid,
        },
        "libraries": libraries,
        "python": {
            "deployment": _worker_deployment(state_root),
            "dynamicLibpython": _artifact(PYTHON_DYNAMIC_LIBRARY),
            "executable": _artifact(PYTHON_EXECUTABLE),
            "releaseLibraryDirs": list(PYTHON_RELEASE_LIBRARY_DIRS),
        },
        "softbus": softbus,
        "softbusSocketCap": DEFAULT_SOCKET_CAP,
    }
    fingerprint_source = {
        "runtimeClosure": closure,
        "softbus": {
            "library": softbus,
            "requiredExports": list(REQUIRED_SOFTBUS_EXPORTS),
            "transportAcl": _transport_acl(),
        },
        "target": target,
    }
    fingerprint = "sha256:" + hashlib.sha256(
        canonical_json_bytes(fingerprint_source)
    ).hexdigest()
    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        ),
        **fingerprint_source,
        "schema": RUNTIME_PROFILE_SCHEMA,
        "systemFingerprint": fingerprint,
    }


def _observed_identity(document: Mapping[str, Any]) -> ObservedRuntimeIdentity:
    target = document["target"]
    return ObservedRuntimeIdentity(
        api_level=int(target["apiLevel"]),
        abi=str(target["abi"]),
        machine=str(target["machine"]),
        uid=int(target["uid"]),
        gid=int(target["gid"]),
        selinux_context=str(target["selinuxContext"]),
    )


def _publish_profile(path: Path, document: dict[str, Any]) -> RuntimeProfile:
    baseline_contract._validate_runtime_profile_document(document)
    raw = canonical_json_bytes(document)
    if len(raw) > RUNTIME_PROFILE_BYTES_MAX:
        _fail("RUNTIME_PROFILE_TOO_LARGE")
    if path.is_symlink():
        _fail("RUNTIME_PROFILE_INVALID", "profile path is a symlink")
    temporary = path.parent / f".{RUNTIME_PROFILE_FILENAME}.staging-{uuid.uuid4().hex}"
    try:
        _write_new_file(temporary, raw, mode=0o600)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except RuntimeProfileError:
        raise
    except OSError as error:
        raise RuntimeProfileError("RUNTIME_PROFILE_WRITE_FAILED") from error
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)
    return load_runtime_profile(path)


def prepare_runtime_profile(state_root: str | os.PathLike[str]) -> PreparedRuntimeProfile:
    """Validate or atomically refresh the device-local activation Profile."""

    root = Path(state_root).absolute()
    profile_path = root / RUNTIME_PROFILE_FILENAME
    lock_descriptor: int | None = None
    try:
        _ensure_private_directory(root)
        lock_descriptor = _acquire_collection_lock(root)
        parameters = _read_system_parameters()
        identity = _device_identity_from_parameters(
            parameters,
            machine=platform.machine(),
        )
        _ensure_manifest(root, identity)
        raw_token_id = _read_mclaw_token_id()
        current_boot_id = _boot_id()
        document = _build_document(root, raw_token_id, identity)

        current: RuntimeProfile | None = None
        if profile_path.exists() and not profile_path.is_symlink():
            try:
                candidate = load_runtime_profile(profile_path)
                preflight_runtime_profile(
                    profile_path,
                    expected_profile_sha256=candidate.sha256,
                    observed=_observed_identity(document),
                )
                if (
                    candidate.document["systemFingerprint"]
                    == document["systemFingerprint"]
                ):
                    current = candidate
            except BaselineError:
                current = None
        elif profile_path.is_symlink():
            _fail("RUNTIME_PROFILE_INVALID", "profile path is a symlink")

        profile = current if current is not None else _publish_profile(profile_path, document)
        preflight_runtime_profile(
            profile.path,
            expected_profile_sha256=profile.sha256,
            observed=_observed_identity(document),
        )
        return PreparedRuntimeProfile(
            profile=profile,
            raw_token_id=raw_token_id,
            current_boot_id=current_boot_id,
        )
    except RuntimeProfileError:
        raise
    except BaselineError as error:
        raise RuntimeProfileError(error.code, error.detail) from error
    except (OSError, TypeError, ValueError) as error:
        raise RuntimeProfileError("RUNTIME_PROFILE_COLLECTION_FAILED") from error
    finally:
        if lock_descriptor is not None:
            _release_collection_lock(lock_descriptor)


def activation_error_message(code: str) -> str:
    """Return one concise product-facing explanation for a stable failure code."""

    messages = {
        "BOOT_ID_UNAVAILABLE": "无法读取当前系统启动身份",
        "DEVICE_IDENTITY_UNAVAILABLE": "无法读取本机设备信息",
        "MANIFEST_INSTALL_INVALID": "无法初始化本机设备信息",
        "MCLAW_TOKEN_INVALID": "M-Claw 系统身份配置无效",
        "MCLAW_TOKEN_MISSING": "未找到 M-Claw 系统身份",
        "OH61_IDENTITY_UNAVAILABLE": "无法确认 OpenHarmony 6.1 系统信息",
        "OH61_RUNTIME_UNSUPPORTED": "当前系统运行环境不受支持",
        "RUNTIME_COMPONENT_MISSING": "缺少 DSoftBus 运行组件",
        "RUNTIME_IDENTITY_UNAVAILABLE": "无法确认 M-Claw 运行身份",
        "RUNTIME_PROFILE_WRITE_FAILED": "无法保存 DSoftBus 本机配置",
        "RUNTIME_PROFILE_BUSY": "另一 M-Claw 会话正在初始化可信设备协作",
        "RUNTIME_STATE_UNAVAILABLE": "DSoftBus 状态目录不可用",
        "SOFTBUS_ACL_INVALID": "SoftBus 未配置 M-Claw 传输权限",
        "WORKER_BUNDLE_INVALID": "DSoftBus Worker 安装不完整",
        "WORKER_BUNDLE_SOURCE_INVALID": "M-Claw Worker 源文件不完整",
        "WORKER_BUNDLE_WRITE_FAILED": "无法安装 DSoftBus Worker",
    }
    return messages.get(code, "DSoftBus 本机环境校验失败")


__all__ = [
    "PreparedRuntimeProfile",
    "RuntimeProfileError",
    "activation_error_message",
    "prepare_runtime_profile",
]
