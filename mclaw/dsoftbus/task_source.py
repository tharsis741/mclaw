# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Read-only task source scopes and remote Agent source-tool context."""

from __future__ import annotations

import asyncio
import base64
import codecs
import hashlib
import json
import mimetypes
import os
import stat
import threading
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextvars import ContextVar, Token
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from . import protocol
from .task_files import (
    InboundTaskFileStore,
    PreparedTaskInputs,
    TaskFileError,
    TaskInputByteBudget,
    TaskInputDescriptor,
    TaskSourceScope,
    safe_relative_path,
)
from .workspace import TaskWorkspacePaths


def _canonical_uuid(value: Any, label: str) -> str:
    try:
        return protocol.canonical_uuid4(value, label)
    except protocol.ProtocolError as error:
        raise TaskFileError("TASK_INPUT_INVALID") from error


def _source_relative(value: Any, *, allow_root: bool = False) -> str:
    if allow_root and value == "":
        return ""
    return safe_relative_path(value)


def _source_transfer_id(task_id: str, scope_id: str, relative_path: str) -> str:
    """Derive one stable UUID4-shaped transfer identity for safe retries."""

    digest = bytearray(
        hashlib.sha256(
            b"mclaw.task-source.transfer\x00"
            + task_id.encode("ascii")
            + b"\x00"
            + scope_id.encode("ascii")
            + b"\x00"
            + relative_path.encode("utf-8")
        ).digest()[:16]
    )
    digest[6] = (digest[6] & 0x0F) | 0x40
    digest[8] = (digest[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(digest)))


def _bounded_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    key: str,
) -> tuple[list[Mapping[str, Any]], bool]:
    selected: list[Mapping[str, Any]] = []
    for row in rows:
        candidate = [*selected, row]
        size = len(
            json.dumps(
                {key: [dict(item) for item in candidate]},
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        )
        if size > protocol.TASK_SOURCE_TOOL_RESULT_BYTES_MAX:
            return selected, True
        selected.append(row)
    return selected, False


@dataclass(slots=True)
class _SourceScanState:
    remaining: int
    limited: bool = False


class LocalTaskSourceService:
    """Serve one caller-owned directory scope without exposing host paths."""

    def __init__(self, prepared: PreparedTaskInputs) -> None:
        if not prepared.task_id:
            raise TaskFileError("TASK_INPUT_INVALID")
        self._prepared = prepared
        self._scopes = {scope.scope_id: scope for scope in prepared.scopes}
        self._transfers: dict[str, tuple[str, str, TaskInputDescriptor, Path]] = {}
        self._fetched_files = 0
        self._fetched_bytes = 0
        self._transfer_lock = threading.Lock()

    def _scope(self, scope_id: Any) -> TaskSourceScope:
        normalized = _canonical_uuid(scope_id, "scopeId")
        try:
            return self._scopes[normalized]
        except KeyError as error:
            raise TaskFileError("SOURCE_SCOPE_NOT_FOUND") from error

    def add_scopes(self, scopes: Sequence[TaskSourceScope]) -> None:
        """Add one verified continuation batch without resetting Task quotas."""

        with self._transfer_lock:
            for scope in scopes:
                if not isinstance(scope, TaskSourceScope):
                    raise TaskFileError("TASK_INPUT_INVALID")
                existing = self._scopes.get(scope.scope_id)
                if existing is not None and existing != scope:
                    raise TaskFileError("TRANSFER_CONFLICT")
                self._scopes[scope.scope_id] = scope

    @staticmethod
    def _scope_path(
        scope: TaskSourceScope,
        relative_path: Any,
        *,
        allow_root: bool = False,
    ) -> tuple[Path, str]:
        relative = _source_relative(relative_path, allow_root=allow_root)
        try:
            root_metadata = scope.root.lstat()
        except OSError as error:
            raise TaskFileError("SOURCE_CHANGED") from error
        if (
            stat.S_ISLNK(root_metadata.st_mode)
            or not stat.S_ISDIR(root_metadata.st_mode)
            or root_metadata.st_dev != scope.device
            or root_metadata.st_ino != scope.inode
        ):
            raise TaskFileError("SOURCE_CHANGED")
        candidate = scope.root if not relative else scope.root / Path(relative)
        current = scope.root
        try:
            for part in Path(relative).parts if relative else ():
                current = current / part
                metadata = current.lstat()
                if stat.S_ISLNK(metadata.st_mode):
                    raise TaskFileError("SOURCE_PATH_FORBIDDEN")
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(scope.root)
        except TaskFileError:
            raise
        except (OSError, ValueError) as error:
            raise TaskFileError("SOURCE_PATH_FORBIDDEN") from error
        return resolved, relative

    @staticmethod
    def _open_scope_node(
        scope: TaskSourceScope,
        relative_path: Any,
        *,
        directory: bool,
        allow_root: bool = False,
    ) -> tuple[int, os.stat_result, str]:
        """Open one source node from the frozen root without following links."""

        relative = _source_relative(relative_path, allow_root=allow_root)
        if os.name == "posix" and all(
            hasattr(os, name) for name in ("O_DIRECTORY", "O_NOFOLLOW")
        ):
            directory_flags = (
                os.O_RDONLY
                | os.O_DIRECTORY
                | os.O_NOFOLLOW
                | getattr(os, "O_CLOEXEC", 0)
            )
            file_flags = (
                os.O_RDONLY
                | os.O_NOFOLLOW
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_BINARY", 0)
            )
            descriptor: int | None = None
            try:
                descriptor = os.open(scope.root, directory_flags)
                root_metadata = os.fstat(descriptor)
                if (
                    not stat.S_ISDIR(root_metadata.st_mode)
                    or root_metadata.st_dev != scope.device
                    or root_metadata.st_ino != scope.inode
                ):
                    raise TaskFileError("SOURCE_CHANGED")
                parts = Path(relative).parts if relative else ()
                for index, component in enumerate(parts):
                    final = index == len(parts) - 1
                    flags = directory_flags if not final or directory else file_flags
                    child = os.open(component, flags, dir_fd=descriptor)
                    previous = descriptor
                    descriptor = child
                    os.close(previous)
                metadata = os.fstat(descriptor)
                expected = stat.S_ISDIR if directory else stat.S_ISREG
                if not expected(metadata.st_mode):
                    raise TaskFileError("SOURCE_PATH_FORBIDDEN")
                result = descriptor
                descriptor = None
                return result, metadata, relative
            except TaskFileError:
                raise
            except OSError as error:
                raise TaskFileError("SOURCE_PATH_FORBIDDEN") from error
            finally:
                if descriptor is not None:
                    os.close(descriptor)

        path, relative = LocalTaskSourceService._scope_path(
            scope, relative, allow_root=allow_root
        )
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_BINARY", 0)
        )
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if directory and hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        descriptor = None
        try:
            before = path.lstat()
            descriptor = os.open(path, flags)
            metadata = os.fstat(descriptor)
            expected = stat.S_ISDIR if directory else stat.S_ISREG
            if (
                stat.S_ISLNK(before.st_mode)
                or not expected(metadata.st_mode)
                or metadata.st_dev != before.st_dev
                or metadata.st_ino != before.st_ino
            ):
                raise TaskFileError("SOURCE_CHANGED")
            result = descriptor
            descriptor = None
            return result, metadata, relative
        except TaskFileError:
            raise
        except OSError as error:
            raise TaskFileError("SOURCE_PATH_FORBIDDEN") from error
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @staticmethod
    def _search_scope_text(
        scope: TaskSourceScope,
        relative_path: str,
        needle: str,
        *,
        byte_budget: int,
        result_limit: int,
    ) -> tuple[list[Mapping[str, Any]], int, bool]:
        """Search one anchored UTF-8 file without a hidden per-file prefix cap."""

        descriptor: int | None = None
        matches: list[Mapping[str, Any]] = []
        consumed = 0
        pending = ""
        line_number = 1
        stopped_for_results = False
        binary_file = False
        line_breaks = "\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029"
        decoder = codecs.getincrementaldecoder("utf-8")(errors="ignore")

        def consume_text(value: str, *, final: bool) -> None:
            nonlocal line_number, pending, stopped_for_results
            pending += value
            rows = pending.splitlines(keepends=True)
            pending = ""
            if not final and rows:
                last = rows[-1]
                if not last or last[-1] not in line_breaks or last.endswith("\r"):
                    pending = rows.pop()
            for row in rows:
                line = row.rstrip(line_breaks)
                if needle in line.casefold():
                    preview = line.strip()
                    if len(preview) > 240:
                        preview = preview[:237] + "..."
                    matches.append(
                        MappingProxyType(
                            {
                                "path": relative_path,
                                "kind": "content",
                                "line": line_number,
                                "preview": preview,
                            }
                        )
                    )
                    if len(matches) >= result_limit:
                        stopped_for_results = True
                        return
                line_number += 1

        try:
            descriptor, before, _relative = LocalTaskSourceService._open_scope_node(
                scope,
                relative_path,
                directory=False,
            )
            maximum = min(max(0, int(byte_budget)), before.st_size)
            first_chunk = True
            while consumed < maximum and not stopped_for_results:
                amount = min(65_536, maximum - consumed)
                if first_chunk:
                    amount = min(amount, 4_096)
                raw = os.read(descriptor, amount)
                if not raw:
                    break
                consumed += len(raw)
                if first_chunk:
                    first_chunk = False
                    if b"\x00" in raw:
                        binary_file = True
                        pending = ""
                        break
                consume_text(decoder.decode(raw, final=False), final=False)
            if not stopped_for_results and not binary_file:
                consume_text(decoder.decode(b"", final=True), final=True)
            after = os.fstat(descriptor)
            if (
                after.st_dev != before.st_dev
                or after.st_ino != before.st_ino
                or after.st_size != before.st_size
                or after.st_mtime_ns != before.st_mtime_ns
            ):
                raise TaskFileError("SOURCE_CHANGED")
            fully_scanned = binary_file or (
                consumed >= before.st_size and not stopped_for_results
            )
            if before.st_size == 0:
                fully_scanned = True
            return matches, consumed, fully_scanned
        except TaskFileError:
            raise
        except OSError as error:
            raise TaskFileError("TASK_INPUT_IO_ERROR") from error
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @staticmethod
    def _copy_open_scope_file(
        source_descriptor: int,
        before: os.stat_result,
        target: Path,
    ) -> tuple[int, str]:
        """Copy one anchored source FD to an immutable private snapshot."""

        if (
            not stat.S_ISREG(before.st_mode)
            or not 0 <= before.st_size <= protocol.TASK_INPUT_FILE_BYTES_MAX
        ):
            raise TaskFileError("TASK_INPUT_TOO_LARGE")
        partial = target.with_name(f".{target.name}.part")
        target_descriptor: int | None = None
        try:
            partial.unlink(missing_ok=True)
            target_descriptor = os.open(
                partial,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_BINARY", 0),
                0o600,
            )
            os.lseek(source_descriptor, 0, os.SEEK_SET)
            digest = hashlib.sha256()
            length = 0
            while True:
                chunk = os.read(source_descriptor, 65_536)
                if not chunk:
                    break
                length += len(chunk)
                if length > protocol.TASK_INPUT_FILE_BYTES_MAX:
                    raise TaskFileError("TASK_INPUT_TOO_LARGE")
                digest.update(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(target_descriptor, view)
                    if written <= 0:
                        raise OSError("short source snapshot write")
                    view = view[written:]
            os.fsync(target_descriptor)
            os.close(target_descriptor)
            target_descriptor = None
            after = os.fstat(source_descriptor)
            if (
                after.st_dev != before.st_dev
                or after.st_ino != before.st_ino
                or after.st_size != before.st_size
                or after.st_mtime_ns != before.st_mtime_ns
                or length != before.st_size
            ):
                raise TaskFileError("SOURCE_CHANGED")
            os.replace(partial, target)
            os.chmod(target, 0o600)
            return length, digest.hexdigest()
        except TaskFileError:
            partial.unlink(missing_ok=True)
            raise
        except OSError as error:
            partial.unlink(missing_ok=True)
            raise TaskFileError("TASK_INPUT_IO_ERROR") from error
        finally:
            if target_descriptor is not None:
                os.close(target_descriptor)

    @staticmethod
    def _walk(
        scope: TaskSourceScope,
        base_relative: str,
        depth: int,
        scan_state: _SourceScanState,
    ):
        if scan_state.remaining <= 0:
            scan_state.limited = True
            return
        descriptor: int | None = None
        try:
            if os.name == "posix":
                descriptor, _metadata, _relative = (
                    LocalTaskSourceService._open_scope_node(
                        scope,
                        base_relative,
                        directory=True,
                        allow_root=True,
                    )
                )
                directory: int | Path = descriptor
            else:
                directory, _relative = LocalTaskSourceService._scope_path(
                    scope,
                    base_relative,
                    allow_root=True,
                )
                metadata = directory.lstat()
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(
                    metadata.st_mode
                ):
                    raise TaskFileError("SOURCE_PATH_FORBIDDEN")
            with os.scandir(directory) as iterator:
                entries: list[tuple[str, str, int]] = []
                while scan_state.remaining > 0:
                    try:
                        entry = next(iterator)
                    except StopIteration:
                        break
                    scan_state.remaining -= 1
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            entries.append((entry.name, "directory", 0))
                        elif entry.is_file(follow_symlinks=False):
                            metadata = entry.stat(follow_symlinks=False)
                            entries.append((entry.name, "file", metadata.st_size))
                    except OSError:
                        continue
                if scan_state.remaining == 0:
                    scan_state.limited = True
                entries.sort(key=lambda item: item[0])
        except TaskFileError:
            raise
        except OSError as error:
            raise TaskFileError("TASK_INPUT_IO_ERROR") from error
        finally:
            if descriptor is not None:
                os.close(descriptor)
        for name, kind, byte_length in entries:
            try:
                relative = (
                    name
                    if not base_relative
                    else f"{base_relative}/{name}"
                )
                safe_relative_path(relative)
                if kind == "directory":
                    yield MappingProxyType(
                        {"path": relative, "name": name, "kind": "directory"}
                    )
                    if depth > 1:
                        yield from LocalTaskSourceService._walk(
                            scope,
                            relative,
                            depth - 1,
                            scan_state,
                        )
                else:
                    yield MappingProxyType(
                        {
                            "path": relative,
                            "name": name,
                            "kind": "file",
                            "byteLength": byte_length,
                        }
                    )
            except (OSError, TaskFileError):
                continue

    def list_entries(
        self,
        *,
        scope_id: Any,
        relative_path: Any,
        depth: Any,
        page_size: Any,
        page_token: Any,
    ) -> Mapping[str, Any]:
        scope = self._scope(scope_id)
        base_relative = _source_relative(relative_path, allow_root=True)
        if (
            type(depth) is not int
            or not 1 <= depth <= protocol.TASK_SOURCE_DEPTH_MAX
            or type(page_size) is not int
            or not 1 <= page_size <= protocol.TASK_SOURCE_PAGE_MAX
        ):
            raise TaskFileError("TASK_INPUT_INVALID")
        token = _source_relative(page_token, allow_root=True)
        scan_state = _SourceScanState(protocol.TASK_SOURCE_SCAN_ENTRY_MAX)
        rows = sorted(
            self._walk(scope, base_relative, depth, scan_state),
            key=lambda row: str(row["path"]),
        )
        remaining = [
            row for row in rows if not token or str(row["path"]) > token
        ]
        candidates = remaining[:page_size]
        more = len(remaining) > len(candidates)
        selected, context_limited = _bounded_rows(candidates, key="entries")
        if len(selected) < len(candidates):
            more = True
        next_token = str(selected[-1]["path"]) if more and selected else ""
        return MappingProxyType(
            {
                "scopeId": scope.scope_id,
                "path": base_relative,
                "entries": tuple(selected),
                "nextPageToken": next_token,
                "contextLimited": context_limited or scan_state.limited,
            }
        )

    def search(
        self,
        *,
        scope_id: Any,
        relative_path: Any,
        query: Any,
        mode: Any,
        max_results: Any,
    ) -> Mapping[str, Any]:
        scope = self._scope(scope_id)
        base_relative = _source_relative(relative_path, allow_root=True)
        if (
            not isinstance(query, str)
            or not 1 <= len(query.encode("utf-8")) <= 256
            or mode not in {"filename", "content"}
            or type(max_results) is not int
            or not 1 <= max_results <= protocol.TASK_SOURCE_SEARCH_RESULT_MAX
        ):
            raise TaskFileError("TASK_INPUT_INVALID")
        needle = query.casefold()
        matches: list[Mapping[str, Any]] = []
        scanned_bytes = 0
        scan_limited = False
        scan_state = _SourceScanState(protocol.TASK_SOURCE_SCAN_ENTRY_MAX)
        stack: list[str] = [base_relative]
        while stack and len(matches) < max_results:
            current_relative = stack.pop()
            rows = list(self._walk(scope, current_relative, 1, scan_state))
            for row in reversed(rows):
                if row["kind"] == "directory":
                    stack.append(str(row["path"]))
            for row in rows:
                path = str(row["path"])
                if mode == "filename":
                    if needle in path.casefold():
                        matches.append(
                            MappingProxyType(
                                {"path": path, "kind": str(row["kind"])}
                            )
                        )
                    if len(matches) >= max_results:
                        break
                    continue
                if row["kind"] != "file":
                    continue
                remaining = (
                    protocol.TASK_SOURCE_SEARCH_BYTES_MAX - scanned_bytes
                )
                if remaining <= 0:
                    scan_limited = True
                    stack.clear()
                    break
                try:
                    file_matches, consumed, fully_scanned = self._search_scope_text(
                        scope,
                        path,
                        needle,
                        byte_budget=remaining,
                        result_limit=max_results - len(matches),
                    )
                except TaskFileError:
                    scan_limited = True
                    continue
                scanned_bytes += consumed
                matches.extend(file_matches)
                if not fully_scanned:
                    scan_limited = True
                    if scanned_bytes >= protocol.TASK_SOURCE_SEARCH_BYTES_MAX:
                        stack.clear()
                if len(matches) >= max_results:
                    break
            if scan_state.limited:
                scan_limited = True
                stack.clear()
        selected, context_limited = _bounded_rows(matches, key="matches")
        return MappingProxyType(
            {
                "scopeId": scope.scope_id,
                "path": base_relative,
                "mode": mode,
                "matches": tuple(selected),
                "scanLimited": scan_limited,
                "contextLimited": context_limited,
            }
        )

    def open_snapshot(
        self,
        *,
        scope_id: Any,
        relative_path: Any,
        transfer_id: Any,
    ) -> Mapping[str, Any]:
        with self._transfer_lock:
            return self._open_snapshot_locked(
                scope_id=scope_id,
                relative_path=relative_path,
                transfer_id=transfer_id,
            )

    def _open_snapshot_locked(
        self,
        *,
        scope_id: Any,
        relative_path: Any,
        transfer_id: Any,
    ) -> Mapping[str, Any]:
        scope = self._scope(scope_id)
        relative = _source_relative(relative_path)
        transfer_id = _canonical_uuid(transfer_id, "transferId")
        prior = self._transfers.get(transfer_id)
        if prior is not None:
            prior_scope, prior_path, descriptor, _snapshot = prior
            if prior_scope != scope.scope_id or prior_path != relative:
                raise TaskFileError("TRANSFER_CONFLICT")
            return descriptor.wire_value()
        source_descriptor: int | None = None
        try:
            source_descriptor, metadata, _ = self._open_scope_node(
                scope, relative, directory=False
            )
            if (
                self._fetched_files >= protocol.TASK_SOURCE_FILE_MAX
                or metadata.st_size > protocol.TASK_INPUT_FILE_BYTES_MAX
                or self._fetched_bytes
                > protocol.TASK_INPUT_TASK_BYTES_MAX - metadata.st_size
            ):
                raise TaskFileError("SOURCE_QUOTA_EXCEEDED")
            if self._prepared.workspace.outgoing is None:
                raise TaskFileError("TASK_INPUT_IO_ERROR")
            snapshot = (
                self._prepared.workspace.outgoing
                / f"source-{transfer_id}.snapshot"
            )
            byte_length, sha256 = self._copy_open_scope_file(
                source_descriptor, metadata, snapshot
            )
        finally:
            if source_descriptor is not None:
                os.close(source_descriptor)
        logical = safe_relative_path(
            f"sources/{scope.scope_id}/{relative}"
        )
        descriptor = TaskInputDescriptor(
            input_id=transfer_id,
            relative_path=logical,
            filename=safe_relative_path(Path(relative).name),
            media_type=mimetypes.guess_type(Path(relative).name)[0]
            or "application/octet-stream",
            byte_length=byte_length,
            sha256=sha256,
        )
        self._transfers[transfer_id] = (
            scope.scope_id,
            relative,
            descriptor,
            snapshot,
        )
        self._fetched_files += 1
        self._fetched_bytes += byte_length
        return descriptor.wire_value()

    def read_snapshot(
        self,
        *,
        transfer_id: Any,
        offset: Any,
    ) -> Mapping[str, Any]:
        transfer_id = _canonical_uuid(transfer_id, "transferId")
        with self._transfer_lock:
            try:
                _scope_id, _path, descriptor, snapshot = self._transfers[transfer_id]
            except KeyError as error:
                raise TaskFileError("SOURCE_SCOPE_NOT_FOUND") from error
        if type(offset) is not int or not 0 <= offset < descriptor.byte_length:
            raise TaskFileError("TASK_INPUT_INVALID")
        amount = min(
            protocol.TASK_TRANSFER_CHUNK_BYTES_MAX,
            descriptor.byte_length - offset,
        )
        try:
            with snapshot.open("rb") as stream:
                stream.seek(offset)
                raw = stream.read(amount)
        except OSError as error:
            raise TaskFileError("TASK_INPUT_IO_ERROR") from error
        if len(raw) != amount:
            raise TaskFileError("SOURCE_CHANGED")
        next_offset = offset + len(raw)
        return MappingProxyType(
            {
                "transferId": transfer_id,
                "offset": offset,
                "nextOffset": next_offset,
                "data": base64.b64encode(raw).decode("ascii"),
                "eof": next_offset == descriptor.byte_length,
            }
        )


class RemoteTaskSourceClient:
    """Task-bound B-side client used only by the three private source tools."""

    def __init__(
        self,
        *,
        owner_loop: asyncio.AbstractEventLoop,
        requester: Callable[[str, Mapping[str, Any]], Awaitable[Mapping[str, Any]]],
        task_id: str,
        workspace: TaskWorkspacePaths,
        source_scopes: Sequence[Mapping[str, Any]],
        input_byte_budget: TaskInputByteBudget | None = None,
    ) -> None:
        if workspace.work is None:
            raise TaskFileError("TASK_INPUT_IO_ERROR")
        self._owner_loop = owner_loop
        self._requester = requester
        self.task_id = _canonical_uuid(task_id, "taskId")
        self.workspace = workspace
        self.source_scopes = tuple(source_scopes)
        self._active = threading.Event()
        self._active.set()
        self._fetched_files = 0
        self._input_byte_budget = input_byte_budget or TaskInputByteBudget()
        self._quota_lock = threading.Lock()
        self._fetches_in_progress: set[tuple[str, str]] = set()
        self._fetch_receipts: dict[tuple[str, str], Mapping[str, Any]] = {}

    def close(self) -> None:
        self._active.clear()

    def add_scopes(self, scopes: Sequence[Mapping[str, Any]]) -> None:
        """Expose newly shared continuation scopes while retaining fetch quotas."""

        normalized = tuple(scopes)
        known = {
            str(value.get("scopeId"))
            for value in self.source_scopes
            if isinstance(value, Mapping)
        }
        for value in normalized:
            if not isinstance(value, Mapping):
                raise TaskFileError("TASK_INPUT_INVALID")
            scope_id = _canonical_uuid(value.get("scopeId"), "scopeId")
            if scope_id in known:
                raise TaskFileError("TRANSFER_CONFLICT")
            known.add(scope_id)
        self.source_scopes = (*self.source_scopes, *normalized)

    async def _request(
        self, method: str, params: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        if not self._active.is_set():
            raise TaskFileError("SOURCE_SCOPE_NOT_FOUND")
        operation = self._requester(method, params)
        try:
            if asyncio.get_running_loop() is self._owner_loop:
                result = await operation
            else:
                try:
                    future = asyncio.run_coroutine_threadsafe(
                        operation, self._owner_loop
                    )
                except BaseException:
                    close = getattr(operation, "close", None)
                    if callable(close):
                        close()
                    raise
                result = await asyncio.wrap_future(future)
        except asyncio.CancelledError:
            raise
        if not isinstance(result, Mapping):
            raise TaskFileError("TASK_INPUT_INVALID")
        return result

    async def list_entries(self, values: Mapping[str, Any]) -> Mapping[str, Any]:
        return await self._request(
            "mclaw.taskSource.list",
            {"taskId": self.task_id, **dict(values)},
        )

    async def search(self, values: Mapping[str, Any]) -> Mapping[str, Any]:
        return await self._request(
            "mclaw.taskSource.search",
            {"taskId": self.task_id, **dict(values)},
        )

    async def fetch(
        self,
        *,
        scope_id: str,
        paths: Sequence[str],
    ) -> Mapping[str, Any]:
        if (
            not isinstance(paths, (tuple, list))
            or not 1 <= len(paths) <= protocol.TASK_SOURCE_FETCH_MAX
        ):
            raise TaskFileError("TASK_INPUT_INVALID")
        normalized_scope_id = _canonical_uuid(scope_id, "scopeId")
        normalized_paths = tuple(_source_relative(path) for path in paths)
        if len(normalized_paths) != len(set(normalized_paths)):
            raise TaskFileError("TASK_INPUT_INVALID")
        receipts: list[Mapping[str, Any]] = []
        for relative in normalized_paths:
            fetch_key = (normalized_scope_id, relative)
            with self._quota_lock:
                cached = self._fetch_receipts.get(fetch_key)
                if cached is not None:
                    receipts.append(cached)
                    continue
                if fetch_key in self._fetches_in_progress:
                    raise TaskFileError("CAPACITY_BUSY")
                self._fetches_in_progress.add(fetch_key)
            transfer_id = _source_transfer_id(
                self.task_id,
                normalized_scope_id,
                relative,
            )
            descriptor: TaskInputDescriptor | None = None
            store: InboundTaskFileStore | None = None
            quota_reserved = False
            try:
                raw_descriptor = await self._request(
                    "mclaw.taskSource.open",
                    {
                        "taskId": self.task_id,
                        "scopeId": normalized_scope_id,
                        "path": relative,
                        "transferId": transfer_id,
                    },
                )
                descriptor = TaskInputDescriptor.from_wire(raw_descriptor)
                if descriptor.input_id != transfer_id:
                    raise TaskFileError("TASK_INPUT_INVALID")
                with self._quota_lock:
                    if self._fetched_files >= protocol.TASK_SOURCE_FILE_MAX:
                        raise TaskFileError("SOURCE_QUOTA_EXCEEDED")
                    self._input_byte_budget.reserve(
                        descriptor.byte_length,
                        error_code="SOURCE_QUOTA_EXCEEDED",
                    )
                    self._fetched_files += 1
                    quota_reserved = True
                store = InboundTaskFileStore(self.workspace, (descriptor,))
                offset = await asyncio.to_thread(store.begin, transfer_id)
                while offset < descriptor.byte_length:
                    response = await self._request(
                        "mclaw.taskSource.read",
                        {
                            "taskId": self.task_id,
                            "transferId": transfer_id,
                            "offset": offset,
                        },
                    )
                    try:
                        raw = protocol.decode_strict_base64(
                            response["data"],
                            maximum=protocol.TASK_TRANSFER_CHUNK_BYTES_MAX,
                        )
                    except (KeyError, protocol.ProtocolError) as error:
                        raise TaskFileError("TASK_INPUT_INVALID") from error
                    expected = offset + len(raw)
                    if (
                        response.get("transferId") != transfer_id
                        or response.get("offset") != offset
                        or response.get("nextOffset") != expected
                        or response.get("eof") is not (
                            expected == descriptor.byte_length
                        )
                    ):
                        raise TaskFileError("TASK_INPUT_INVALID")
                    offset = await asyncio.to_thread(
                        store.append, transfer_id, offset, raw
                    )
                receipt = await asyncio.to_thread(store.commit, transfer_id)
                assert self.workspace.work is not None
                completed = MappingProxyType(
                    {
                        **dict(receipt),
                        "localPath": str(
                            (self.workspace.work / descriptor.relative_path).resolve()
                        ),
                    }
                )
                with self._quota_lock:
                    self._fetch_receipts[fetch_key] = completed
                receipts.append(completed)
            except BaseException as error:
                if (
                    store is not None
                    and isinstance(error, TaskFileError)
                    and error.code
                    in {
                        "TASK_INPUT_HASH_MISMATCH",
                        "TASK_INPUT_INVALID",
                        "TRANSFER_CONFLICT",
                    }
                ):
                    try:
                        await asyncio.to_thread(store.abort, transfer_id)
                    except TaskFileError:
                        pass
                if quota_reserved:
                    assert descriptor is not None
                    with self._quota_lock:
                        self._fetched_files -= 1
                        self._input_byte_budget.release(descriptor.byte_length)
                raise
            finally:
                with self._quota_lock:
                    self._fetches_in_progress.discard(fetch_key)
        return MappingProxyType({"files": tuple(receipts)})


_CURRENT_SOURCE_CLIENT: ContextVar[RemoteTaskSourceClient | None] = ContextVar(
    "mclaw_dsoftbus_task_source_client", default=None
)


def bind_task_source_client(
    client: RemoteTaskSourceClient | None,
) -> Token[RemoteTaskSourceClient | None]:
    return _CURRENT_SOURCE_CLIENT.set(client)


def reset_task_source_client(token: Token[RemoteTaskSourceClient | None]) -> None:
    _CURRENT_SOURCE_CLIENT.reset(token)


def get_task_source_client() -> RemoteTaskSourceClient | None:
    return _CURRENT_SOURCE_CLIENT.get()


__all__ = [
    "LocalTaskSourceService",
    "RemoteTaskSourceClient",
    "bind_task_source_client",
    "get_task_source_client",
    "reset_task_source_client",
]
