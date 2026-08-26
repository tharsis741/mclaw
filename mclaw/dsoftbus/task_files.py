# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Task-scoped file snapshots and verified chunk assembly."""

from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import stat
import threading
import uuid
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from . import protocol
from .workspace import (
    DsoftbusWorkspace,
    RemoteWorkspaceError,
    TaskWorkspacePaths,
    ensure_private_subdirectory,
    open_private_directory_no_follow,
)


TASK_INPUT_MANIFEST_MEDIA_TYPE = "application/vnd.mclaw.task-input+json"
_MEDIA_TYPE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,63}/"
    r"[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,63}"
)


class TaskFileError(RuntimeError):
    """Stable file-transfer failure safe to return across the binding."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class TaskInputByteBudget:
    """Thread-safe Task-wide byte ledger shared by every input path."""

    def __init__(self, used_bytes: int = 0) -> None:
        if (
            type(used_bytes) is not int
            or not 0 <= used_bytes <= protocol.TASK_INPUT_TASK_BYTES_MAX
        ):
            raise TaskFileError("TASK_INPUT_TOO_LARGE")
        self._used_bytes = used_bytes
        self._lock = threading.Lock()

    @property
    def used_bytes(self) -> int:
        with self._lock:
            return self._used_bytes

    def reserve(self, byte_length: int, *, error_code: str) -> None:
        if type(byte_length) is not int or byte_length < 0:
            raise TaskFileError("TASK_INPUT_INVALID")
        with self._lock:
            if (
                self._used_bytes
                > protocol.TASK_INPUT_TASK_BYTES_MAX - byte_length
            ):
                raise TaskFileError(error_code)
            self._used_bytes += byte_length

    def release(self, byte_length: int) -> None:
        if type(byte_length) is not int or byte_length < 0:
            raise TaskFileError("TASK_INPUT_INVALID")
        with self._lock:
            if byte_length > self._used_bytes:
                raise TaskFileError("TASK_INPUT_INVALID")
            self._used_bytes -= byte_length


def _canonical_uuid(value: Any, label: str) -> str:
    try:
        return protocol.canonical_uuid4(value, label)
    except protocol.ProtocolError as error:
        raise TaskFileError("TASK_INPUT_INVALID") from error


def safe_relative_path(value: Any) -> str:
    """Validate one platform-independent relative path without traversal."""

    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise TaskFileError("SOURCE_PATH_FORBIDDEN")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise TaskFileError("SOURCE_PATH_FORBIDDEN") from error
    path = PurePosixPath(value)
    if (
        len(encoded) > 1_024
        or path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise TaskFileError("SOURCE_PATH_FORBIDDEN")
    return path.as_posix()


def safe_media_type(value: Any) -> str:
    """Validate one parameter-free Internet media type for prompts and Parts."""

    try:
        encoded_size = len(value.encode("utf-8")) if isinstance(value, str) else 0
    except UnicodeEncodeError as error:
        raise TaskFileError("TASK_INPUT_INVALID") from error
    if (
        not isinstance(value, str)
        or not 1 <= encoded_size <= 128
        or _MEDIA_TYPE.fullmatch(value) is None
    ):
        raise TaskFileError("TASK_INPUT_INVALID")
    return value


@dataclass(frozen=True, slots=True)
class TaskInputDescriptor:
    input_id: str
    relative_path: str
    filename: str
    media_type: str
    byte_length: int
    sha256: str

    def wire_value(self) -> Mapping[str, Any]:
        return MappingProxyType(
            {
                "inputId": self.input_id,
                "relativePath": self.relative_path,
                "filename": self.filename,
                "mediaType": self.media_type,
                "byteLength": self.byte_length,
                "sha256": self.sha256,
            }
        )

    @classmethod
    def from_wire(cls, value: Mapping[str, Any]) -> "TaskInputDescriptor":
        if not isinstance(value, Mapping) or frozenset(value) != frozenset(
            {
                "inputId",
                "relativePath",
                "filename",
                "mediaType",
                "byteLength",
                "sha256",
            }
        ):
            raise TaskFileError("TASK_INPUT_INVALID")
        input_id = _canonical_uuid(value["inputId"], "inputId")
        relative_path = safe_relative_path(value["relativePath"])
        filename = safe_relative_path(value["filename"])
        if "/" in filename:
            raise TaskFileError("TASK_INPUT_INVALID")
        media_type = value["mediaType"]
        byte_length = value["byteLength"]
        sha256 = value["sha256"]
        media_type = safe_media_type(media_type)
        if (
            type(byte_length) is not int
            or not 0 <= byte_length <= protocol.TASK_INPUT_FILE_BYTES_MAX
            or not isinstance(sha256, str)
            or len(sha256) != 64
            or any(character not in "0123456789abcdef" for character in sha256)
        ):
            raise TaskFileError("TASK_INPUT_INVALID")
        return cls(
            input_id=input_id,
            relative_path=relative_path,
            filename=filename,
            media_type=media_type,
            byte_length=byte_length,
            sha256=sha256,
        )


@dataclass(frozen=True, slots=True)
class TaskSourceScope:
    scope_id: str
    name: str
    root: Path
    device: int
    inode: int

    def wire_value(self) -> Mapping[str, str]:
        return MappingProxyType(
            {
                "scopeId": self.scope_id,
                "name": self.name,
                "kind": "directory",
            }
        )


def normalize_task_input_manifest(value: Any) -> Mapping[str, Any]:
    """Validate the bounded manifest carried by one A2A data Part."""

    if not isinstance(value, Mapping) or frozenset(value) != frozenset(
        {"files", "sourceScopes"}
    ):
        raise TaskFileError("TASK_INPUT_INVALID")
    raw_files = value["files"]
    raw_scopes = value["sourceScopes"]
    if not isinstance(raw_files, (tuple, list)) or not isinstance(
        raw_scopes, (tuple, list)
    ):
        raise TaskFileError("TASK_INPUT_INVALID")
    if (
        not 1 <= len(raw_files) + len(raw_scopes) <= protocol.TASK_INPUT_PATH_MAX
        or len(raw_files) > protocol.TASK_SOURCE_FILE_MAX
    ):
        raise TaskFileError("TASK_INPUT_INVALID")
    files = tuple(TaskInputDescriptor.from_wire(item) for item in raw_files)
    total_bytes = sum(item.byte_length for item in files)
    if total_bytes > protocol.TASK_INPUT_TASK_BYTES_MAX:
        raise TaskFileError("TASK_INPUT_TOO_LARGE")
    scopes: list[Mapping[str, str]] = []
    for raw in raw_scopes:
        if not isinstance(raw, Mapping) or frozenset(raw) != frozenset(
            {"scopeId", "name", "kind"}
        ):
            raise TaskFileError("TASK_INPUT_INVALID")
        scope_id = _canonical_uuid(raw["scopeId"], "scopeId")
        name = safe_relative_path(raw["name"])
        if "/" in name or raw["kind"] != "directory":
            raise TaskFileError("TASK_INPUT_INVALID")
        scopes.append(
            MappingProxyType(
                {"scopeId": scope_id, "name": name, "kind": "directory"}
            )
        )
    identities = [item.input_id for item in files] + [
        item["scopeId"] for item in scopes
    ]
    if len(identities) != len(set(identities)):
        raise TaskFileError("TASK_INPUT_INVALID")
    return MappingProxyType(
        {
            "files": tuple(item.wire_value() for item in files),
            "sourceScopes": tuple(scopes),
        }
    )


@dataclass(frozen=True, slots=True)
class PreparedTaskInputs:
    peer_device_id: str
    request_message_id: str
    task_id: str
    workspace: TaskWorkspacePaths
    files: tuple[tuple[TaskInputDescriptor, Path], ...]
    scopes: tuple[TaskSourceScope, ...]

    def manifest(self) -> Mapping[str, Any] | None:
        if not self.files and not self.scopes:
            return None
        return MappingProxyType(
            {
                "files": tuple(descriptor.wire_value() for descriptor, _ in self.files),
                "sourceScopes": tuple(scope.wire_value() for scope in self.scopes),
            }
        )

    def bind_task(
        self,
        manager: DsoftbusWorkspace,
        task_id: str,
    ) -> "PreparedTaskInputs":
        paths = manager.bind_requested_task(
            self.peer_device_id,
            self.request_message_id,
            task_id,
        )
        if paths.outgoing is None:
            raise TaskFileError("TASK_INPUT_IO_ERROR")
        rebound = tuple(
            (descriptor, paths.outgoing / snapshot.name)
            for descriptor, snapshot in self.files
        )
        return replace(
            self,
            task_id=task_id,
            workspace=paths,
            files=rebound,
        )


def _copy_snapshot(
    source: Path,
    target: Path,
    *,
    minimum_bytes: int = 0,
    maximum_bytes: int = protocol.TASK_INPUT_FILE_BYTES_MAX,
) -> tuple[int, str]:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_BINARY", 0)
    )
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    part = target.with_name(f".{target.name}.part")
    source_fd: int | None = None
    target_fd: int | None = None
    try:
        source_before = source.lstat()
        if stat.S_ISLNK(source_before.st_mode) or not stat.S_ISREG(
            source_before.st_mode
        ):
            raise TaskFileError("SOURCE_PATH_FORBIDDEN")
        if (
            type(minimum_bytes) is not int
            or type(maximum_bytes) is not int
            or not 0 <= minimum_bytes <= maximum_bytes
            or not minimum_bytes <= source_before.st_size <= maximum_bytes
        ):
            raise TaskFileError("TASK_INPUT_TOO_LARGE")
        source_fd = os.open(source, flags)
        opened = os.fstat(source_fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != source_before.st_dev
            or opened.st_ino != source_before.st_ino
        ):
            raise TaskFileError("SOURCE_CHANGED")
        part.unlink(missing_ok=True)
        target_fd = os.open(
            part,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_BINARY", 0),
            0o600,
        )
        digest = hashlib.sha256()
        length = 0
        while True:
            chunk = os.read(source_fd, 65_536)
            if not chunk:
                break
            length += len(chunk)
            if length > maximum_bytes:
                raise TaskFileError("TASK_INPUT_TOO_LARGE")
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(target_fd, view)
                if written <= 0:
                    raise OSError("short task snapshot write")
                view = view[written:]
        os.fsync(target_fd)
        os.close(target_fd)
        target_fd = None
        source_after = source.lstat()
        if (
            source_after.st_dev != opened.st_dev
            or source_after.st_ino != opened.st_ino
            or source_after.st_size != opened.st_size
            or source_after.st_mtime_ns != opened.st_mtime_ns
            or length != opened.st_size
        ):
            raise TaskFileError("SOURCE_CHANGED")
        os.replace(part, target)
        os.chmod(target, 0o600)
        return length, digest.hexdigest()
    except TaskFileError:
        part.unlink(missing_ok=True)
        raise
    except OSError as error:
        part.unlink(missing_ok=True)
        raise TaskFileError("TASK_INPUT_IO_ERROR") from error
    finally:
        if target_fd is not None:
            os.close(target_fd)
        if source_fd is not None:
            os.close(source_fd)


class OutboundTaskFileStore:
    """Prepare immutable files and read-only directory scopes on the caller."""

    def __init__(
        self,
        workspace: DsoftbusWorkspace,
        *,
        uuid_factory: Any = uuid.uuid4,
    ) -> None:
        self._workspace = workspace
        self._uuid_factory = uuid_factory

    def prepare(
        self,
        peer_device_id: str,
        request_message_id: str,
        paths: Sequence[str],
    ) -> PreparedTaskInputs:
        return self._prepare(
            peer_device_id=peer_device_id,
            request_message_id=request_message_id,
            workspace_task_id=request_message_id,
            authoritative_task_id="",
            relative_root="attachments",
            paths=paths,
        )

    def prepare_supplement(
        self,
        peer_device_id: str,
        task_id: str,
        request_message_id: str,
        paths: Sequence[str],
    ) -> PreparedTaskInputs:
        """Snapshot one continuation batch into an existing requested Task."""

        task_id = _canonical_uuid(task_id, "taskId")
        request_message_id = _canonical_uuid(request_message_id, "messageId")
        return self._prepare(
            peer_device_id=peer_device_id,
            request_message_id=request_message_id,
            workspace_task_id=task_id,
            authoritative_task_id=task_id,
            relative_root=f"supplements/{request_message_id}/attachments",
            paths=paths,
        )

    def _prepare(
        self,
        *,
        peer_device_id: str,
        request_message_id: str,
        workspace_task_id: str,
        authoritative_task_id: str,
        relative_root: str,
        paths: Sequence[str],
    ) -> PreparedTaskInputs:
        if not isinstance(paths, (tuple, list)) or len(paths) > protocol.TASK_INPUT_PATH_MAX:
            raise TaskFileError("TASK_INPUT_INVALID")
        request_message_id = _canonical_uuid(request_message_id, "messageId")
        workspace_task_id = _canonical_uuid(workspace_task_id, "taskId")
        relative_root = safe_relative_path(relative_root)
        try:
            workspace = self._workspace.ensure_task(
                "requested", peer_device_id, workspace_task_id
            )
        except RemoteWorkspaceError as error:
            raise TaskFileError("TASK_INPUT_IO_ERROR") from error
        if workspace.outgoing is None:
            raise TaskFileError("TASK_INPUT_IO_ERROR")
        files: list[tuple[TaskInputDescriptor, Path]] = []
        scopes: list[TaskSourceScope] = []
        created_snapshots: list[Path] = []
        total_file_bytes = 0
        try:
            for raw_path in paths:
                if not isinstance(raw_path, str) or not raw_path:
                    raise TaskFileError("TASK_INPUT_INVALID")
                source = Path(raw_path)
                if not source.is_absolute():
                    raise TaskFileError("TASK_INPUT_INVALID")
                metadata = source.lstat()
                if stat.S_ISLNK(metadata.st_mode):
                    raise TaskFileError("SOURCE_PATH_FORBIDDEN")
                item_id = _canonical_uuid(str(self._uuid_factory()), "inputId")
                name = source.name or "input"
                safe_name = safe_relative_path(name)
                if stat.S_ISDIR(metadata.st_mode):
                    resolved = source.resolve(strict=True)
                    scopes.append(
                        TaskSourceScope(
                            scope_id=item_id,
                            name=safe_name,
                            root=resolved,
                            device=metadata.st_dev,
                            inode=metadata.st_ino,
                        )
                    )
                    continue
                if not stat.S_ISREG(metadata.st_mode):
                    raise TaskFileError("SOURCE_PATH_FORBIDDEN")
                relative = safe_relative_path(
                    f"{relative_root}/{item_id}/{safe_name}"
                )
                target = workspace.outgoing / f"{item_id}.snapshot"
                remaining_task_bytes = (
                    protocol.TASK_INPUT_TASK_BYTES_MAX - total_file_bytes
                )
                if metadata.st_size > remaining_task_bytes:
                    raise TaskFileError("TASK_INPUT_TOO_LARGE")
                byte_length, sha256 = _copy_snapshot(
                    source,
                    target,
                    maximum_bytes=min(
                        protocol.TASK_INPUT_FILE_BYTES_MAX,
                        remaining_task_bytes,
                    ),
                )
                created_snapshots.append(target)
                total_file_bytes += byte_length
                media_type = mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
                files.append(
                    (
                        TaskInputDescriptor(
                            input_id=item_id,
                            relative_path=relative,
                            filename=safe_name,
                            media_type=media_type,
                            byte_length=byte_length,
                            sha256=sha256,
                        ),
                        target,
                    )
                )
        except (OSError, TaskFileError) as error:
            if authoritative_task_id:
                for snapshot in created_snapshots:
                    try:
                        snapshot.unlink(missing_ok=True)
                    except OSError:
                        pass
            else:
                try:
                    self._workspace.clear_task(
                        "requested", peer_device_id, workspace_task_id
                    )
                except RemoteWorkspaceError:
                    pass
            if isinstance(error, TaskFileError):
                raise
            raise TaskFileError("TASK_INPUT_IO_ERROR") from error
        return PreparedTaskInputs(
            peer_device_id=peer_device_id,
            request_message_id=request_message_id,
            task_id=authoritative_task_id,
            workspace=workspace,
            files=tuple(files),
            scopes=tuple(scopes),
        )

    @staticmethod
    def discard_prepared(prepared: PreparedTaskInputs) -> None:
        """Delete only the immutable snapshots created for one rejected batch."""

        outgoing = prepared.workspace.outgoing
        if outgoing is None:
            raise TaskFileError("TASK_INPUT_IO_ERROR")
        for descriptor, snapshot in prepared.files:
            expected = outgoing / f"{descriptor.input_id}.snapshot"
            if snapshot != expected:
                raise TaskFileError("TASK_INPUT_IO_ERROR")
            try:
                metadata = snapshot.lstat()
            except FileNotFoundError:
                continue
            except OSError as error:
                raise TaskFileError("TASK_INPUT_IO_ERROR") from error
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise TaskFileError("TASK_INPUT_IO_ERROR")
            try:
                snapshot.unlink()
            except OSError as error:
                raise TaskFileError("TASK_INPUT_IO_ERROR") from error


def _hash_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    length = 0
    descriptor: int | None = None
    try:
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise TaskFileError("SOURCE_PATH_FORBIDDEN")
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_BINARY", 0)
        )
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
        ):
            raise TaskFileError("SOURCE_CHANGED")
        while True:
            chunk = os.read(descriptor, 65_536)
            if not chunk:
                break
            length += len(chunk)
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (
            after.st_dev != opened.st_dev
            or after.st_ino != opened.st_ino
            or after.st_size != opened.st_size
            or after.st_mtime_ns != opened.st_mtime_ns
            or length != opened.st_size
        ):
            raise TaskFileError("SOURCE_CHANGED")
    except TaskFileError:
        raise
    except OSError as error:
        raise TaskFileError("TASK_INPUT_IO_ERROR") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return length, digest.hexdigest()


def _atomic_copy_into_private_tree(
    source: Path,
    root: Path,
    relative_path: str,
) -> Path:
    relative = Path(safe_relative_path(relative_path))
    try:
        parent = ensure_private_subdirectory(root, tuple(relative.parts[:-1]))
    except RemoteWorkspaceError as error:
        raise TaskFileError("SOURCE_PATH_FORBIDDEN") from error
    target = parent / relative.name
    if target.exists() or target.is_symlink():
        metadata = target.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise TaskFileError("SOURCE_PATH_FORBIDDEN")
        return target

    if os.name != "posix":
        part = target.with_name(f".{target.name}.{uuid.uuid4().hex}.part")
        try:
            _copy_snapshot(source, part, minimum_bytes=0)
            os.replace(part, target)
            return target
        except TaskFileError:
            part.unlink(missing_ok=True)
            raise
        except OSError as error:
            part.unlink(missing_ok=True)
            raise TaskFileError("TASK_INPUT_IO_ERROR") from error

    parent_descriptor: int | None = None
    source_descriptor: int | None = None
    target_descriptor: int | None = None
    part_name = f".{relative.name}.{uuid.uuid4().hex}.part"
    try:
        parent_descriptor = open_private_directory_no_follow(parent)
        before = source.lstat()
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise TaskFileError("SOURCE_PATH_FORBIDDEN")
        source_flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        source_descriptor = os.open(source, source_flags)
        opened = os.fstat(source_descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
        ):
            raise TaskFileError("SOURCE_CHANGED")
        target_descriptor = os.open(
            part_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=parent_descriptor,
        )
        length = 0
        while True:
            chunk = os.read(source_descriptor, 65_536)
            if not chunk:
                break
            length += len(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(target_descriptor, view)
                if written <= 0:
                    raise OSError("short private task copy write")
                view = view[written:]
        os.fsync(target_descriptor)
        os.close(target_descriptor)
        target_descriptor = None
        after = os.fstat(source_descriptor)
        if (
            after.st_dev != opened.st_dev
            or after.st_ino != opened.st_ino
            or after.st_size != opened.st_size
            or after.st_mtime_ns != opened.st_mtime_ns
            or length != opened.st_size
        ):
            raise TaskFileError("SOURCE_CHANGED")
        os.replace(
            part_name,
            relative.name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
        os.fsync(parent_descriptor)
        return target
    except TaskFileError:
        if parent_descriptor is not None:
            try:
                os.unlink(part_name, dir_fd=parent_descriptor)
            except FileNotFoundError:
                pass
            except OSError:
                pass
        raise
    except RemoteWorkspaceError as error:
        if parent_descriptor is not None:
            try:
                os.unlink(part_name, dir_fd=parent_descriptor)
            except (FileNotFoundError, OSError):
                pass
        raise TaskFileError("SOURCE_PATH_FORBIDDEN") from error
    except OSError as error:
        if parent_descriptor is not None:
            try:
                os.unlink(part_name, dir_fd=parent_descriptor)
            except FileNotFoundError:
                pass
            except OSError:
                pass
        raise TaskFileError("TASK_INPUT_IO_ERROR") from error
    finally:
        if target_descriptor is not None:
            os.close(target_descriptor)
        if source_descriptor is not None:
            os.close(source_descriptor)
        if parent_descriptor is not None:
            os.close(parent_descriptor)


class InboundTaskFileStore:
    """Assemble declared input chunks into one task's input and work trees."""

    def __init__(
        self,
        workspace: TaskWorkspacePaths,
        descriptors: Sequence[TaskInputDescriptor],
    ) -> None:
        if workspace.incoming is None or workspace.input is None or workspace.work is None:
            raise TaskFileError("TASK_INPUT_IO_ERROR")
        self.workspace = workspace
        self._descriptors = {value.input_id: value for value in descriptors}
        if len(self._descriptors) != len(descriptors):
            raise TaskFileError("TASK_INPUT_INVALID")
        self._committed: set[str] = set()

    @property
    def ready(self) -> bool:
        return len(self._committed) == len(self._descriptors)

    def descriptor(self, input_id: str) -> TaskInputDescriptor:
        input_id = _canonical_uuid(input_id, "inputId")
        try:
            return self._descriptors[input_id]
        except KeyError as error:
            raise TaskFileError("TASK_INPUT_INVALID") from error

    def begin(self, input_id: str) -> int:
        descriptor = self.descriptor(input_id)
        target = self.workspace.input / Path(descriptor.relative_path)
        if target.exists() or target.is_symlink():
            length, sha256 = _hash_file(target)
            if length == descriptor.byte_length and sha256 == descriptor.sha256:
                self._committed.add(input_id)
                return length
            raise TaskFileError("TRANSFER_CONFLICT")
        part = self.workspace.incoming / f"{input_id}.part"
        try:
            if part.exists():
                metadata = part.lstat()
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(
                    metadata.st_mode
                ):
                    raise TaskFileError("TRANSFER_CONFLICT")
                if metadata.st_size > descriptor.byte_length:
                    raise TaskFileError("TRANSFER_CONFLICT")
                return metadata.st_size
            descriptor_fd = os.open(
                part,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_BINARY", 0),
                0o600,
            )
            os.close(descriptor_fd)
            return 0
        except TaskFileError:
            raise
        except OSError as error:
            raise TaskFileError("TASK_INPUT_IO_ERROR") from error

    def append(self, input_id: str, offset: int, raw: bytes) -> int:
        descriptor = self.descriptor(input_id)
        if (
            type(offset) is not int
            or not isinstance(raw, bytes)
            or not 1 <= len(raw) <= protocol.TASK_TRANSFER_CHUNK_BYTES_MAX
            or offset < 0
            or offset > descriptor.byte_length
            or len(raw) > descriptor.byte_length - offset
        ):
            raise TaskFileError("TASK_INPUT_INVALID")
        part = self.workspace.incoming / f"{input_id}.part"
        flags = (
            os.O_WRONLY
            | os.O_APPEND
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_BINARY", 0)
        )
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        file_descriptor: int | None = None
        try:
            metadata = part.lstat()
            if stat.S_ISLNK(metadata.st_mode) or metadata.st_size != offset:
                raise TaskFileError("TRANSFER_CONFLICT")
            file_descriptor = os.open(part, flags)
            view = memoryview(raw)
            while view:
                written = os.write(file_descriptor, view)
                if written <= 0:
                    raise OSError("short task input write")
                view = view[written:]
            os.fsync(file_descriptor)
            return offset + len(raw)
        except TaskFileError:
            raise
        except OSError as error:
            raise TaskFileError("TASK_INPUT_IO_ERROR") from error
        finally:
            if file_descriptor is not None:
                os.close(file_descriptor)

    def commit(self, input_id: str) -> Mapping[str, Any]:
        descriptor = self.descriptor(input_id)
        part = self.workspace.incoming / f"{input_id}.part"
        target = self.workspace.input / Path(descriptor.relative_path)
        work = self.workspace.work / Path(descriptor.relative_path)
        if target.exists() or target.is_symlink():
            length, sha256 = _hash_file(target)
        else:
            length, sha256 = _hash_file(part)
            if length != descriptor.byte_length or sha256 != descriptor.sha256:
                raise TaskFileError("TASK_INPUT_HASH_MISMATCH")
            try:
                target = _atomic_copy_into_private_tree(
                    part,
                    self.workspace.input,
                    descriptor.relative_path,
                )
                part.unlink(missing_ok=True)
                length, sha256 = _hash_file(target)
            except TaskFileError:
                raise
            except OSError as error:
                raise TaskFileError("TASK_INPUT_IO_ERROR") from error
        if length != descriptor.byte_length or sha256 != descriptor.sha256:
            raise TaskFileError("TASK_INPUT_HASH_MISMATCH")
        if not work.exists() and not work.is_symlink():
            work = _atomic_copy_into_private_tree(
                target,
                self.workspace.work,
                descriptor.relative_path,
            )
        work_length, work_sha256 = _hash_file(work)
        if work_length != length or work_sha256 != sha256:
            raise TaskFileError("TASK_INPUT_HASH_MISMATCH")
        self._committed.add(input_id)
        return MappingProxyType(
            {
                "inputId": input_id,
                "relativePath": descriptor.relative_path,
                "byteLength": length,
                "sha256": sha256,
            }
        )

    def abort(self, input_id: str) -> bool:
        self.descriptor(input_id)
        part = self.workspace.incoming / f"{input_id}.part"
        try:
            existed = part.exists() or part.is_symlink()
            part.unlink(missing_ok=True)
            return existed
        except OSError as error:
            raise TaskFileError("TASK_INPUT_IO_ERROR") from error


__all__ = [
    "InboundTaskFileStore",
    "OutboundTaskFileStore",
    "PreparedTaskInputs",
    "TASK_INPUT_MANIFEST_MEDIA_TYPE",
    "TaskFileError",
    "TaskInputByteBudget",
    "TaskInputDescriptor",
    "TaskSourceScope",
    "normalize_task_input_manifest",
    "safe_media_type",
    "safe_relative_path",
]
