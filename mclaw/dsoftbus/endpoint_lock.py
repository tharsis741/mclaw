# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""POSIX/OpenHarmony endpoint lock owned by one product Runtime.

The module is import-safe on non-POSIX hosts.  ``fcntl`` is imported only by
``DsoftbusEndpointLock.acquire()`` after the product Runtime has selected the
OpenHarmony path.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path
import re
import stat
import threading
from types import MappingProxyType
from typing import Any, Mapping, NoReturn

from .protocol import (
    RESOURCE_ENDPOINT_LOCK_BYTES_MAX,
    RESOURCE_RUNTIME_HEALTH_BYTES_MAX,
    ProtocolError,
    canonical_json_bytes,
    canonical_uuid4,
    strict_json_loads,
)


ENDPOINT_LOCK_FILENAME = "runtime.lock"
ENDPOINT_LOCK_SCHEMA = "mclaw.dsoftbus.endpoint-lock/v1"
RUNTIME_HEALTH_FILENAME = "runtime-health.json"
_HEALTH_TEMP_NAME = re.compile(r"^\.runtime-health\.[0-9a-f]{32}\.tmp$")
_HOLDER_KEYS = frozenset(
    {"pid", "runtimeInstanceId", "schemaVersion", "startTimeTicks"}
)


class EndpointLockError(RuntimeError):
    """Stable, non-sensitive endpoint-lock failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _fail(code: str, cause: BaseException | None = None) -> NoReturn:
    error = EndpointLockError(code)
    if cause is None:
        raise error
    raise error from cause


def _positive_integer(value: Any, name: str, maximum: int) -> int:
    if type(value) is not int or value < 1 or value > maximum:
        raise EndpointLockError(f"{name.upper()}_INVALID")
    return value


def build_endpoint_holder(
    runtime_instance_id: str,
    *,
    pid: int,
    start_time_ticks: int,
) -> Mapping[str, Any]:
    """Build the exact immutable holder record without filesystem access."""
    try:
        canonical_uuid4(runtime_instance_id, "runtimeInstanceId")
    except ProtocolError as error:
        raise EndpointLockError("RUNTIME_INSTANCE_ID_INVALID") from error
    normalized_pid = _positive_integer(pid, "pid", 2**31 - 1)
    normalized_ticks = _positive_integer(
        start_time_ticks, "start_time_ticks", 2**63 - 1
    )
    return MappingProxyType(
        {
            "pid": normalized_pid,
            "runtimeInstanceId": runtime_instance_id,
            "schemaVersion": ENDPOINT_LOCK_SCHEMA,
            "startTimeTicks": normalized_ticks,
        }
    )


def parse_endpoint_holder(raw: bytes) -> Mapping[str, Any]:
    """Parse exact canonical endpoint holder bytes."""
    try:
        value = strict_json_loads(
            raw,
            max_bytes=RESOURCE_ENDPOINT_LOCK_BYTES_MAX,
            require_canonical=True,
            require_object=True,
        )
        if frozenset(value) != _HOLDER_KEYS:
            raise EndpointLockError("ENDPOINT_LOCK_RECORD_INVALID")
        if value["schemaVersion"] != ENDPOINT_LOCK_SCHEMA:
            raise EndpointLockError("ENDPOINT_LOCK_RECORD_INVALID")
        canonical_uuid4(value["runtimeInstanceId"], "runtimeInstanceId")
        _positive_integer(value["pid"], "pid", 2**31 - 1)
        _positive_integer(
            value["startTimeTicks"], "start_time_ticks", 2**63 - 1
        )
    except (ProtocolError, EndpointLockError) as error:
        raise EndpointLockError("ENDPOINT_LOCK_RECORD_INVALID") from error
    return MappingProxyType(dict(value))


def read_process_start_time_ticks() -> int:
    """Read this process's Linux/OpenHarmony start-time token."""
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open("/proc/self/stat", flags)
    except OSError as error:
        _fail("PROCESS_IDENTITY_READ_FAILED", error)
    try:
        chunks: list[bytes] = []
        remaining = 4097
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if not raw or len(raw) > 4096:
            _fail("PROCESS_IDENTITY_READ_FAILED")
    except OSError as error:
        _fail("PROCESS_IDENTITY_READ_FAILED", error)
    finally:
        os.close(descriptor)
    try:
        text = raw.decode("ascii")
        command_end = text.rfind(")")
        fields = text[command_end + 2 :].split()
        ticks = int(fields[19], 10)
    except (UnicodeDecodeError, ValueError, IndexError) as error:
        _fail("PROCESS_IDENTITY_READ_FAILED", error)
    return _positive_integer(ticks, "start_time_ticks", 2**63 - 1)


def _directory_flags() -> int:
    required = ("O_DIRECTORY", "O_NOFOLLOW")
    if any(not hasattr(os, name) for name in required):
        _fail("POSIX_LOCK_PRIMITIVE_UNAVAILABLE")
    return (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
    )


def _open_absolute_directory_no_follow(path: Path) -> int:
    if os.name != "posix" or not path.is_absolute():
        _fail("POSIX_LOCK_PRIMITIVE_UNAVAILABLE")
    parts = path.parts
    if not parts or parts[0] != "/":
        _fail("ENDPOINT_STATE_ROOT_INVALID")
    flags = _directory_flags()
    try:
        descriptor = os.open("/", flags)
    except OSError as error:
        _fail("ENDPOINT_STATE_ROOT_INVALID", error)
    try:
        for component in parts[1:]:
            if component in {"", ".", ".."} or "/" in component or "\x00" in component:
                _fail("ENDPOINT_STATE_ROOT_INVALID")
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except OSError as error:
                _fail("ENDPOINT_STATE_ROOT_INVALID", error)
            status = os.fstat(child)
            if not stat.S_ISDIR(status.st_mode):
                os.close(child)
                _fail("ENDPOINT_STATE_ROOT_INVALID")
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _read_descriptor(
    descriptor: int, *, maximum: int = RESOURCE_ENDPOINT_LOCK_BYTES_MAX
) -> bytes:
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        os.lseek(descriptor, 0, os.SEEK_SET)
    except OSError as error:
        _fail("ENDPOINT_LOCK_READ_FAILED", error)
    if not raw or len(raw) > maximum:
        _fail("ENDPOINT_LOCK_RECORD_INVALID")
    return raw


def _write_descriptor(descriptor: int, raw: bytes) -> None:
    try:
        os.ftruncate(descriptor, 0)
        os.lseek(descriptor, 0, os.SEEK_SET)
        offset = 0
        while offset < len(raw):
            written = os.write(descriptor, raw[offset:])
            if written <= 0:
                _fail("ENDPOINT_LOCK_WRITE_FAILED")
            offset += written
        os.fsync(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
    except EndpointLockError:
        raise
    except OSError as error:
        _fail("ENDPOINT_LOCK_WRITE_FAILED", error)


class DsoftbusEndpointLock:
    """Held flock plus anchored state-directory and holder identity."""

    def __init__(
        self,
        *,
        state_root: Path,
        state_directory_fd: int,
        lock_fd: int,
        holder: Mapping[str, Any],
        device: int,
        inode: int,
    ) -> None:
        self.path = state_root / ENDPOINT_LOCK_FILENAME
        self._state_directory_fd = state_directory_fd
        self._lock_fd = lock_fd
        self._holder = MappingProxyType(dict(holder))
        self._device = device
        self._inode = inode
        self._mutex = threading.RLock()

    @classmethod
    def acquire(
        cls,
        state_root: str | Path,
        runtime_instance_id: str,
        *,
        pid: int | None = None,
        start_time_ticks: int | None = None,
    ) -> "DsoftbusEndpointLock":
        """Acquire, write, and fsync the exact current holder record."""
        if os.name != "posix":
            _fail("POSIX_LOCK_PRIMITIVE_UNAVAILABLE")
        try:
            import fcntl  # type: ignore[import-not-found]
        except ImportError as error:
            _fail("POSIX_LOCK_PRIMITIVE_UNAVAILABLE", error)

        normalized_root = Path(state_root)
        if (
            not normalized_root.is_absolute()
            or normalized_root.name in {"", ".", ".."}
            or normalized_root == normalized_root.parent
        ):
            _fail("ENDPOINT_STATE_ROOT_INVALID")
        holder = build_endpoint_holder(
            runtime_instance_id,
            pid=os.getpid() if pid is None else pid,
            start_time_ticks=(
                read_process_start_time_ticks()
                if start_time_ticks is None
                else start_time_ticks
            ),
        )
        raw = canonical_json_bytes(dict(holder))
        if len(raw) > RESOURCE_ENDPOINT_LOCK_BYTES_MAX:
            _fail("ENDPOINT_LOCK_RECORD_INVALID")

        parent_fd = _open_absolute_directory_no_follow(normalized_root.parent)
        state_fd: int | None = None
        lock_fd: int | None = None
        try:
            try:
                os.mkdir(normalized_root.name, 0o700, dir_fd=parent_fd)
                os.fsync(parent_fd)
            except FileExistsError:
                pass
            except OSError as error:
                _fail("ENDPOINT_STATE_ROOT_CREATE_FAILED", error)
            try:
                state_fd = os.open(
                    normalized_root.name,
                    _directory_flags(),
                    dir_fd=parent_fd,
                )
            except OSError as error:
                _fail("ENDPOINT_STATE_ROOT_INVALID", error)
            state_status = os.fstat(state_fd)
            if (
                not stat.S_ISDIR(state_status.st_mode)
                or state_status.st_uid != os.getuid()
                or stat.S_IMODE(state_status.st_mode) != 0o700
            ):
                _fail("ENDPOINT_STATE_ROOT_INVALID")

            flags = (
                os.O_CREAT
                | os.O_RDWR
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            try:
                lock_fd = os.open(
                    ENDPOINT_LOCK_FILENAME,
                    flags,
                    0o600,
                    dir_fd=state_fd,
                )
            except OSError as error:
                _fail("ENDPOINT_LOCK_OPEN_FAILED", error)
            lock_status = os.fstat(lock_fd)
            if (
                not stat.S_ISREG(lock_status.st_mode)
                or lock_status.st_uid != os.getuid()
                or stat.S_IMODE(lock_status.st_mode) != 0o600
                or lock_status.st_nlink != 1
            ):
                _fail("ENDPOINT_LOCK_FILE_INVALID")
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                if error.errno in {errno.EACCES, errno.EAGAIN}:
                    _fail("ENDPOINT_IN_USE", error)
                _fail("ENDPOINT_LOCK_ACQUIRE_FAILED", error)

            _write_descriptor(lock_fd, raw)
            os.fsync(state_fd)
            instance = cls(
                state_root=normalized_root,
                state_directory_fd=state_fd,
                lock_fd=lock_fd,
                holder=holder,
                device=lock_status.st_dev,
                inode=lock_status.st_ino,
            )
            instance.verify_current_path()
            state_fd = None
            lock_fd = None
            return instance
        finally:
            if lock_fd is not None:
                os.close(lock_fd)
            if state_fd is not None:
                os.close(state_fd)
            os.close(parent_fd)

    @property
    def holder(self) -> Mapping[str, Any]:
        return MappingProxyType(dict(self._holder))

    @property
    def is_held(self) -> bool:
        with self._mutex:
            return self._lock_fd is not None

    def verify_current_path(self) -> None:
        """Revalidate path identity and exact holder bytes on held descriptors."""
        with self._mutex:
            if self._lock_fd is None or self._state_directory_fd is None:
                _fail("ENDPOINT_LOCK_NOT_HELD")
            flags = (
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            try:
                current_fd = os.open(
                    ENDPOINT_LOCK_FILENAME,
                    flags,
                    dir_fd=self._state_directory_fd,
                )
            except OSError as error:
                _fail("ENDPOINT_LOCK_OWNERSHIP_LOST", error)
            try:
                current = os.fstat(current_fd)
                held = os.fstat(self._lock_fd)
                if (
                    (current.st_dev, current.st_ino) != (self._device, self._inode)
                    or (held.st_dev, held.st_ino) != (self._device, self._inode)
                    or not stat.S_ISREG(current.st_mode)
                    or current.st_uid != os.getuid()
                    or stat.S_IMODE(current.st_mode) != 0o600
                    or current.st_nlink != 1
                ):
                    _fail("ENDPOINT_LOCK_OWNERSHIP_LOST")
                current_holder = parse_endpoint_holder(_read_descriptor(current_fd))
                held_holder = parse_endpoint_holder(_read_descriptor(self._lock_fd))
                if dict(current_holder) != dict(self._holder) or dict(held_holder) != dict(
                    self._holder
                ):
                    _fail("ENDPOINT_LOCK_OWNERSHIP_LOST")
            finally:
                os.close(current_fd)

    def publish_owned_runtime_health(self, raw: bytes, *, temp_name: str) -> None:
        """Atomically publish owner-loop health under the held endpoint identity.

        This is an internal primitive.  The owner-loop health publisher is the
        only product caller; keeping the descriptor operation here prevents it
        from reopening the state directory by an unanchored path.
        """
        if (
            not isinstance(raw, bytes)
            or not raw
            or len(raw) > RESOURCE_RUNTIME_HEALTH_BYTES_MAX
        ):
            _fail("RUNTIME_HEALTH_BYTES_INVALID")
        if not isinstance(temp_name, str) or _HEALTH_TEMP_NAME.fullmatch(temp_name) is None:
            _fail("RUNTIME_HEALTH_TEMP_NAME_INVALID")

        with self._mutex:
            if self._lock_fd is None or self._state_directory_fd is None:
                _fail("ENDPOINT_LOCK_NOT_HELD")
            self.verify_current_path()
            state_fd = self._state_directory_fd
            target_fd: int | None = None
            temp_fd: int | None = None
            temp_created = False
            try:
                try:
                    target_fd = os.open(
                        RUNTIME_HEALTH_FILENAME,
                        os.O_RDONLY
                        | getattr(os, "O_CLOEXEC", 0)
                        | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=state_fd,
                    )
                except FileNotFoundError:
                    target_fd = None
                except OSError as error:
                    _fail("RUNTIME_HEALTH_FILE_INVALID", error)
                if target_fd is not None:
                    target_status = os.fstat(target_fd)
                    if (
                        not stat.S_ISREG(target_status.st_mode)
                        or target_status.st_uid != os.getuid()
                        or stat.S_IMODE(target_status.st_mode) != 0o600
                        or target_status.st_nlink != 1
                    ):
                        _fail("RUNTIME_HEALTH_FILE_INVALID")
                    os.close(target_fd)
                    target_fd = None

                flags = (
                    os.O_CREAT
                    | os.O_EXCL
                    | os.O_WRONLY
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                )
                try:
                    temp_fd = os.open(temp_name, flags, 0o600, dir_fd=state_fd)
                    temp_created = True
                except OSError as error:
                    _fail("RUNTIME_HEALTH_WRITE_FAILED", error)
                temp_status = os.fstat(temp_fd)
                if (
                    not stat.S_ISREG(temp_status.st_mode)
                    or temp_status.st_uid != os.getuid()
                    or stat.S_IMODE(temp_status.st_mode) != 0o600
                    or temp_status.st_nlink != 1
                ):
                    _fail("RUNTIME_HEALTH_WRITE_FAILED")

                offset = 0
                while offset < len(raw):
                    try:
                        written = os.write(temp_fd, raw[offset:])
                    except InterruptedError:
                        continue
                    except OSError as error:
                        _fail("RUNTIME_HEALTH_WRITE_FAILED", error)
                    if written <= 0:
                        _fail("RUNTIME_HEALTH_WRITE_FAILED")
                    offset += written
                try:
                    os.fsync(temp_fd)
                except OSError as error:
                    _fail("RUNTIME_HEALTH_WRITE_FAILED", error)
                os.close(temp_fd)
                temp_fd = None

                # A same-UID actor may have replaced runtime.lock while the
                # temporary file was written.  Revalidate immediately before
                # the only persistent publication operation.
                self.verify_current_path()
                try:
                    target_fd = os.open(
                        RUNTIME_HEALTH_FILENAME,
                        os.O_RDONLY
                        | getattr(os, "O_CLOEXEC", 0)
                        | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=state_fd,
                    )
                except FileNotFoundError:
                    target_fd = None
                except OSError as error:
                    _fail("RUNTIME_HEALTH_FILE_INVALID", error)
                if target_fd is not None:
                    target_status = os.fstat(target_fd)
                    if (
                        not stat.S_ISREG(target_status.st_mode)
                        or target_status.st_uid != os.getuid()
                        or stat.S_IMODE(target_status.st_mode) != 0o600
                        or target_status.st_nlink != 1
                    ):
                        _fail("RUNTIME_HEALTH_FILE_INVALID")
                    os.close(target_fd)
                    target_fd = None
                try:
                    os.replace(
                        temp_name,
                        RUNTIME_HEALTH_FILENAME,
                        src_dir_fd=state_fd,
                        dst_dir_fd=state_fd,
                    )
                    temp_created = False
                    os.fsync(state_fd)
                except OSError as error:
                    _fail("RUNTIME_HEALTH_WRITE_FAILED", error)

                try:
                    target_fd = os.open(
                        RUNTIME_HEALTH_FILENAME,
                        os.O_RDONLY
                        | getattr(os, "O_CLOEXEC", 0)
                        | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=state_fd,
                    )
                except OSError as error:
                    _fail("RUNTIME_HEALTH_WRITE_FAILED", error)
                target_status = os.fstat(target_fd)
                if (
                    not stat.S_ISREG(target_status.st_mode)
                    or target_status.st_uid != os.getuid()
                    or stat.S_IMODE(target_status.st_mode) != 0o600
                    or target_status.st_nlink != 1
                    or _read_descriptor(
                        target_fd, maximum=RESOURCE_RUNTIME_HEALTH_BYTES_MAX
                    )
                    != raw
                ):
                    _fail("RUNTIME_HEALTH_WRITE_FAILED")
            finally:
                if target_fd is not None:
                    os.close(target_fd)
                if temp_fd is not None:
                    os.close(temp_fd)
                if temp_created:
                    try:
                        os.unlink(temp_name, dir_fd=state_fd)
                    except FileNotFoundError:
                        pass
                    except OSError:
                        # Publication has already failed.  Do not mask the
                        # ownership/write error with cleanup diagnostics.
                        pass

    def release(self, *, require_current_path: bool = True) -> None:
        """Release flock by closing descriptors; the lock file is retained."""
        error: BaseException | None = None
        with self._mutex:
            if self._lock_fd is None:
                return
            if require_current_path:
                try:
                    self.verify_current_path()
                except BaseException as caught:
                    error = caught
            lock_fd = self._lock_fd
            state_fd = self._state_directory_fd
            self._lock_fd = None
            self._state_directory_fd = None
        os.close(lock_fd)
        if state_fd is not None:
            os.close(state_fd)
        if error is not None:
            raise error


__all__ = [
    "DsoftbusEndpointLock",
    "ENDPOINT_LOCK_FILENAME",
    "ENDPOINT_LOCK_SCHEMA",
    "RUNTIME_HEALTH_FILENAME",
    "EndpointLockError",
    "build_endpoint_holder",
    "parse_endpoint_holder",
    "read_process_start_time_ticks",
]
