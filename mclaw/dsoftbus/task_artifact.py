# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Task-local Artifact collection for inbound DSoftBus Agent turns."""

from __future__ import annotations

import copy
import hashlib
import os
import re
import stat
import threading
import uuid
from collections.abc import Mapping
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from . import protocol
from .a2a import A2AError, build_artifact_update
from .a2a_media import (
    TASK_ARTIFACT_REFERENCE_PREFIX,
    task_artifact_reference_metadata,
    task_artifact_reference_part,
)
from .task_files import TaskFileError, _copy_snapshot, safe_media_type
from .workspace import DsoftbusWorkspace, RemoteWorkspaceError


TASK_ARTIFACT_DESCRIPTOR_MEDIA_TYPE = (
    "application/vnd.mclaw.task-artifact+json"
)
_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


class TaskArtifactError(RuntimeError):
    """Stable failure raised before an Artifact enters a Task result."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return copy.deepcopy(value)


def artifact_display_filename(value: Any, fallback: str) -> str:
    """Return one portable display filename that can never become a path."""

    name = value if isinstance(value, str) else ""
    name = name.replace("\\", "/").rsplit("/", 1)[-1].strip()
    name = _SAFE_FILENAME.sub("_", name).strip(" ._")
    if not name:
        name = fallback
    raw = name.encode("utf-8")
    if len(raw) > 160:
        stem, dot, suffix = name.rpartition(".")
        suffix = suffix[:32] if dot else ""
        budget = 155 - len(suffix.encode("utf-8"))
        head = (stem if dot else name).encode("utf-8")[: max(1, budget)]
        name = head.decode("utf-8", errors="ignore") or fallback
        if suffix:
            name = f"{name}.{suffix}"
    return name


def _transfer_descriptor(part: Mapping[str, Any]) -> Mapping[str, Any] | None:
    try:
        reference = task_artifact_reference_metadata(part)
    except ValueError as error:
        raise TaskArtifactError("ARTIFACT_INVALID") from error
    if reference is not None:
        value: Mapping[str, Any] = {
            "transferId": reference["mclaw.transferId"],
            "contentMediaType": part.get("mediaType"),
            "byteLength": reference["mclaw.byteLength"],
            "sha256": reference["mclaw.sha256"],
        }
    else:
        if part.get("mediaType") != TASK_ARTIFACT_DESCRIPTOR_MEDIA_TYPE:
            return None
        value = part.get("data")
        if not isinstance(value, Mapping) or frozenset(value) != frozenset(
            {"transferId", "contentMediaType", "byteLength", "sha256"}
        ):
            raise TaskArtifactError("ARTIFACT_INVALID")
    try:
        transfer_id = protocol.canonical_uuid4(value["transferId"], "transferId")
    except protocol.ProtocolError as error:
        raise TaskArtifactError("ARTIFACT_INVALID") from error
    try:
        content_media_type = safe_media_type(value["contentMediaType"])
    except TaskFileError as error:
        raise TaskArtifactError("ARTIFACT_INVALID") from error
    byte_length = value["byteLength"]
    sha256 = value["sha256"]
    try:
        filename_bytes = (
            len(part["filename"].encode("utf-8"))
            if isinstance(part.get("filename"), str)
            else 0
        )
    except UnicodeEncodeError as error:
        raise TaskArtifactError("ARTIFACT_INVALID") from error
    if (
        not 1 <= filename_bytes <= 1_024
        or type(byte_length) is not int
        or not 0 <= byte_length <= protocol.TASK_ARTIFACT_BYTES_MAX
        or not isinstance(sha256, str)
        or len(sha256) != 64
        or any(character not in "0123456789abcdef" for character in sha256)
    ):
        raise TaskArtifactError("ARTIFACT_INVALID")
    if reference is not None and part.get("url") != (
        f"{TASK_ARTIFACT_REFERENCE_PREFIX}{transfer_id}"
    ):
        raise TaskArtifactError("ARTIFACT_INVALID")
    return MappingProxyType(
        {
            "transferId": transfer_id,
            "contentMediaType": content_media_type,
            "byteLength": byte_length,
            "sha256": sha256,
        }
    )


def artifact_transfer_parts(
    artifact: Mapping[str, Any],
) -> tuple[tuple[int, Mapping[str, Any], Mapping[str, Any]], ...]:
    """Validate and return every file-transfer Part in one Artifact."""

    parts = artifact.get("parts") if isinstance(artifact, Mapping) else None
    if not isinstance(parts, (tuple, list)) or not 1 <= len(parts) <= 32:
        raise TaskArtifactError("ARTIFACT_INVALID")
    extensions = artifact.get("extensions", ())
    if extensions is None:
        extensions = ()
    if not isinstance(extensions, (tuple, list)):
        raise TaskArtifactError("ARTIFACT_INVALID")
    result: list[tuple[int, Mapping[str, Any], Mapping[str, Any]]] = []
    total = 0
    identities: set[str] = set()
    for index, raw_part in enumerate(parts):
        if not isinstance(raw_part, Mapping):
            raise TaskArtifactError("ARTIFACT_INVALID")
        descriptor = _transfer_descriptor(raw_part)
        if descriptor is None:
            continue
        if protocol.TASK_FILES_EXTENSION_URI not in extensions:
            raise TaskArtifactError("ARTIFACT_INVALID")
        transfer_id = str(descriptor["transferId"])
        if transfer_id in identities:
            raise TaskArtifactError("ARTIFACT_INVALID")
        identities.add(transfer_id)
        total += int(descriptor["byteLength"])
        if total > protocol.TASK_ARTIFACT_BYTES_MAX:
            raise TaskArtifactError("ARTIFACT_TOO_LARGE")
        result.append((index, raw_part, descriptor))
    if protocol.TASK_FILES_EXTENSION_URI in extensions and not result:
        raise TaskArtifactError("ARTIFACT_INVALID")
    return tuple(result)


def artifact_part_local_filename(
    artifact: Mapping[str, Any],
    index: int,
) -> str:
    """Return the deterministic private filename for one Artifact Part."""

    parts = artifact.get("parts", ())
    try:
        part = parts[index]
    except (IndexError, KeyError, TypeError) as error:
        raise TaskArtifactError("ARTIFACT_INVALID") from error
    if not isinstance(part, Mapping):
        raise TaskArtifactError("ARTIFACT_INVALID")
    descriptor = _transfer_descriptor(part)
    if descriptor is not None:
        content_type = str(descriptor["contentMediaType"])
        fallback_suffix = (
            ".json"
            if content_type == "application/json"
            else ".txt"
            if content_type.startswith("text/")
            else ".bin"
        )
    elif "text" in part:
        fallback_suffix = ".txt"
    elif "raw" in part:
        fallback_suffix = ".bin"
    else:
        fallback_suffix = ".json"
    base = artifact_display_filename(
        part.get("filename") or artifact.get("name"),
        f"part-{index + 1}{fallback_suffix}",
    )
    if "." not in base and fallback_suffix:
        base += fallback_suffix
    return f"{index + 1:02d}-{base}"


def _hash_regular_file(path: Path) -> tuple[int, str]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise TaskArtifactError("ARTIFACT_IO_ERROR")
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != metadata.st_dev
            or opened.st_ino != metadata.st_ino
        ):
            raise TaskArtifactError("ARTIFACT_CHANGED")
        digest = hashlib.sha256()
        length = 0
        while True:
            chunk = os.read(descriptor, 65_536)
            if not chunk:
                break
            length += len(chunk)
            if length > protocol.TASK_ARTIFACT_BYTES_MAX:
                raise TaskArtifactError("ARTIFACT_TOO_LARGE")
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (
            after.st_dev != opened.st_dev
            or after.st_ino != opened.st_ino
            or after.st_size != opened.st_size
            or after.st_mtime_ns != opened.st_mtime_ns
            or length != opened.st_size
        ):
            raise TaskArtifactError("ARTIFACT_CHANGED")
        return length, digest.hexdigest()
    except TaskArtifactError:
        raise
    except OSError as error:
        raise TaskArtifactError("ARTIFACT_IO_ERROR") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


class InboundArtifactStore:
    """Resume and atomically publish transferred Artifact files on the caller."""

    def __init__(
        self,
        workspace: DsoftbusWorkspace,
        peer_device_id: str,
        task_id: str,
        artifact: Mapping[str, Any],
    ) -> None:
        try:
            self.artifact_id = protocol.canonical_uuid4(
                artifact.get("artifactId"), "artifactId"
            )
            self.directory = workspace.ensure_artifact_directory(
                "received", peer_device_id, task_id, self.artifact_id
            )
        except (protocol.ProtocolError, RemoteWorkspaceError) as error:
            raise TaskArtifactError("ARTIFACT_IO_ERROR") from error
        self.artifact = _plain(artifact)
        self._parts = {
            str(descriptor["transferId"]): (index, descriptor)
            for index, _part, descriptor in artifact_transfer_parts(self.artifact)
        }

    def _part(
        self, transfer_id: str
    ) -> tuple[int, Mapping[str, Any], Path, Path]:
        try:
            transfer_id = protocol.canonical_uuid4(transfer_id, "transferId")
            index, descriptor = self._parts[transfer_id]
        except (protocol.ProtocolError, KeyError) as error:
            raise TaskArtifactError("ARTIFACT_NOT_FOUND") from error
        target = self.directory / artifact_part_local_filename(self.artifact, index)
        partial = self.directory / f".{transfer_id}.part"
        return index, descriptor, target, partial

    def begin(self, transfer_id: str) -> int:
        _index, descriptor, target, partial = self._part(transfer_id)
        if target.is_file():
            length, sha256 = _hash_regular_file(target)
            if (
                length == descriptor["byteLength"]
                and sha256 == descriptor["sha256"]
            ):
                return length
            try:
                target.unlink()
            except OSError as error:
                raise TaskArtifactError("ARTIFACT_IO_ERROR") from error
        try:
            if partial.exists():
                metadata = partial.lstat()
                if (
                    stat.S_ISLNK(metadata.st_mode)
                    or not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_size > descriptor["byteLength"]
                ):
                    raise TaskArtifactError("TRANSFER_CONFLICT")
                return metadata.st_size
            file_descriptor = os.open(
                partial,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_BINARY", 0),
                0o600,
            )
            os.close(file_descriptor)
            return 0
        except TaskArtifactError:
            raise
        except OSError as error:
            raise TaskArtifactError("ARTIFACT_IO_ERROR") from error

    def append(self, transfer_id: str, offset: int, raw: bytes) -> int:
        _index, descriptor, _target, partial = self._part(transfer_id)
        if (
            type(offset) is not int
            or not isinstance(raw, bytes)
            or not 1 <= len(raw) <= protocol.TASK_TRANSFER_CHUNK_BYTES_MAX
            or offset < 0
            or offset > descriptor["byteLength"]
            or len(raw) > descriptor["byteLength"] - offset
        ):
            raise TaskArtifactError("ARTIFACT_INVALID")
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
            metadata = partial.lstat()
            if stat.S_ISLNK(metadata.st_mode) or metadata.st_size != offset:
                raise TaskArtifactError("TRANSFER_CONFLICT")
            file_descriptor = os.open(partial, flags)
            view = memoryview(raw)
            while view:
                written = os.write(file_descriptor, view)
                if written <= 0:
                    raise OSError("short Artifact write")
                view = view[written:]
            os.fsync(file_descriptor)
            return offset + len(raw)
        except TaskArtifactError:
            raise
        except OSError as error:
            raise TaskArtifactError("ARTIFACT_IO_ERROR") from error
        finally:
            if file_descriptor is not None:
                os.close(file_descriptor)

    def commit(self, transfer_id: str) -> Mapping[str, Any]:
        index, descriptor, target, partial = self._part(transfer_id)
        if not target.is_file():
            length, sha256 = _hash_regular_file(partial)
            if (
                length != descriptor["byteLength"]
                or sha256 != descriptor["sha256"]
            ):
                try:
                    partial.unlink(missing_ok=True)
                except OSError:
                    pass
                raise TaskArtifactError("ARTIFACT_HASH_MISMATCH")
            try:
                os.replace(partial, target)
                os.chmod(target, 0o600)
            except OSError as error:
                raise TaskArtifactError("ARTIFACT_IO_ERROR") from error
        length, sha256 = _hash_regular_file(target)
        if (
            length != descriptor["byteLength"]
            or sha256 != descriptor["sha256"]
        ):
            raise TaskArtifactError("ARTIFACT_HASH_MISMATCH")
        return MappingProxyType(
            {
                "transferId": transfer_id,
                "index": index,
                "filename": target.name,
                "localPath": str(target.resolve()),
                "mediaType": descriptor["contentMediaType"],
                "byteLength": length,
                "sha256": sha256,
            }
        )


@dataclass(slots=True)
class TaskArtifactCollector:
    """Thread-safe collector copied into AgentRunner and tool worker contexts."""

    task_id: str
    context_id: str
    peer_device_id: str = ""
    workspace: DsoftbusWorkspace | None = None
    _artifacts: list[dict[str, Any]] = field(default_factory=list, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def _append(self, candidate: Mapping[str, Any]) -> Mapping[str, Any]:
        try:
            event = build_artifact_update(
                task_id=self.task_id,
                context_id=self.context_id,
                artifact=candidate,
            )
        except A2AError as error:
            raise TaskArtifactError("INVALID_PARAMS") from error
        plain_event = _plain(event)
        if (
            len(protocol.canonical_json_bytes(plain_event))
            > protocol.TASK_STREAM_ITEM_BYTES_MAX
        ):
            raise TaskArtifactError("ARTIFACT_TOO_LARGE")
        normalized = plain_event["artifactUpdate"]["artifact"]
        with self._lock:
            # One slot is always reserved for the mandatory final-response
            # Artifact generated by DsoftbusTaskDispatcher.
            if len(self._artifacts) >= protocol.TASK_ARTIFACT_MAX - 1:
                raise TaskArtifactError("CAPACITY_BUSY")
            self._artifacts.append(normalized)
        return normalized

    def add_data(
        self,
        *,
        name: str,
        data: Any,
        description: str,
    ) -> Mapping[str, Any]:
        try:
            byte_length = len(protocol.canonical_json_bytes(data))
        except (protocol.ProtocolError, TypeError, ValueError) as error:
            raise TaskArtifactError("INVALID_PARAMS") from error
        if byte_length > protocol.TASK_SINGLE_FRAME_ARTIFACT_BYTES_MAX:
            raise TaskArtifactError("ARTIFACT_TOO_LARGE")
        return self._append(
            {
                "artifactId": str(uuid.uuid4()),
                "name": name,
                "description": description,
                "parts": [
                    {
                        "data": _plain(data),
                        "filename": name,
                        "mediaType": "application/json",
                    }
                ],
                "metadata": {"mclaw.artifactRole": "task-output"},
            }
        )

    def add_file(
        self,
        *,
        name: str,
        path: Path,
        media_type: str,
        description: str,
    ) -> Mapping[str, Any]:
        """Snapshot one local file and publish only its verified descriptor."""

        if self.workspace is None or not self.peer_device_id:
            raise TaskArtifactError("ARTIFACT_CONTEXT_UNAVAILABLE")
        artifact_id = str(uuid.uuid4())
        transfer_id = str(uuid.uuid4())
        candidate = {
            "artifactId": artifact_id,
            "name": name,
            "description": description,
            "parts": [
                task_artifact_reference_part(
                    transfer_id=transfer_id,
                    filename=name,
                    media_type=media_type,
                    byte_length=0,
                    sha256="0" * 64,
                )
            ],
            "metadata": {"mclaw.artifactRole": "task-output"},
            "extensions": [protocol.TASK_FILES_EXTENSION_URI],
        }
        published = False
        try:
            directory = self.workspace.ensure_artifact_directory(
                "produced",
                self.peer_device_id,
                self.task_id,
                artifact_id,
            )
            target = directory / artifact_part_local_filename(candidate, 0)
            byte_length, sha256 = _copy_snapshot(
                Path(path),
                target,
                minimum_bytes=0,
                maximum_bytes=protocol.TASK_ARTIFACT_BYTES_MAX,
            )
            candidate["parts"][0]["metadata"][
                "mclaw.byteLength"
            ] = byte_length
            candidate["parts"][0]["metadata"]["mclaw.sha256"] = sha256
            artifact = self._append(candidate)
            published = True
            return artifact
        except TaskArtifactError:
            raise
        except TaskFileError as error:
            code = (
                "ARTIFACT_TOO_LARGE"
                if error.code == "TASK_INPUT_TOO_LARGE"
                else "ARTIFACT_IO_ERROR"
            )
            raise TaskArtifactError(code) from error
        except RemoteWorkspaceError as error:
            raise TaskArtifactError("ARTIFACT_IO_ERROR") from error
        finally:
            if not published:
                try:
                    self.workspace.clear_artifact(
                        "produced",
                        self.peer_device_id,
                        self.task_id,
                        artifact_id,
                    )
                except RemoteWorkspaceError:
                    pass

    def snapshot(self) -> tuple[Mapping[str, Any], ...]:
        with self._lock:
            return tuple(_plain(value) for value in self._artifacts)


_CURRENT_COLLECTOR: ContextVar[TaskArtifactCollector | None] = ContextVar(
    "mclaw_dsoftbus_task_artifact_collector",
    default=None,
)


def bind_task_artifact_collector(
    collector: TaskArtifactCollector,
) -> Token[TaskArtifactCollector | None]:
    return _CURRENT_COLLECTOR.set(collector)


def reset_task_artifact_collector(
    token: Token[TaskArtifactCollector | None],
) -> None:
    _CURRENT_COLLECTOR.reset(token)


def get_task_artifact_collector() -> TaskArtifactCollector | None:
    return _CURRENT_COLLECTOR.get()


__all__ = [
    "InboundArtifactStore",
    "TASK_ARTIFACT_DESCRIPTOR_MEDIA_TYPE",
    "TaskArtifactCollector",
    "TaskArtifactError",
    "artifact_display_filename",
    "artifact_part_local_filename",
    "artifact_transfer_parts",
    "bind_task_artifact_collector",
    "get_task_artifact_collector",
    "reset_task_artifact_collector",
]
