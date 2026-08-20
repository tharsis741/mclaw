# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Persistent A2A Task and Artifact state for the DSoftBus Runtime.

The store is deliberately synchronous and owner-loop confined.  SQLite owns
the small protocol objects while artifact bytes live below the same
``MCLAW_HOME/dsoftbus`` state root.  Remote paths are never reused as local
paths: received bytes are written to a temporary sibling, verified, and then
atomically published.
"""

from __future__ import annotations

import base64
import binascii
import copy
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from types import MappingProxyType
from typing import Any, Literal, Mapping
import uuid

from . import protocol


TaskDirection = Literal["owned", "received"]

TERMINAL_TASK_STATES = frozenset(
    {
        "TASK_STATE_COMPLETED",
        "TASK_STATE_FAILED",
        "TASK_STATE_CANCELED",
        "TASK_STATE_REJECTED",
    }
)
KNOWN_TASK_STATES = TERMINAL_TASK_STATES | frozenset(
    {
        "TASK_STATE_SUBMITTED",
        "TASK_STATE_WORKING",
        "TASK_STATE_INPUT_REQUIRED",
        "TASK_STATE_AUTH_REQUIRED",
    }
)

_TASK_STATE_TRANSITIONS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "TASK_STATE_SUBMITTED": KNOWN_TASK_STATES,
        "TASK_STATE_WORKING": KNOWN_TASK_STATES - {"TASK_STATE_SUBMITTED"},
        "TASK_STATE_INPUT_REQUIRED": KNOWN_TASK_STATES
        - {"TASK_STATE_SUBMITTED"},
        "TASK_STATE_AUTH_REQUIRED": KNOWN_TASK_STATES
        - {"TASK_STATE_SUBMITTED"},
        **{
            state: frozenset({state})
            for state in TERMINAL_TASK_STATES
        },
    }
)

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_ID = re.compile(r"^[A-Za-z0-9._~-]{1,128}$")
_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")
class TaskStoreError(RuntimeError):
    """Stable local persistence fault."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return copy.deepcopy(value)


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze(item) for key, item in value.items()}
        )
    if isinstance(value, (tuple, list)):
        return tuple(_freeze(item) for item in value)
    return copy.deepcopy(value)


def _canonical_json(value: Any) -> bytes:
    try:
        encoded = json.dumps(
            _plain(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, UnicodeEncodeError, ValueError) as error:
        raise TaskStoreError("TASK_STATE_INVALID") from error
    if len(encoded) > protocol.TASK_JSON_BYTES_MAX:
        raise TaskStoreError("TASK_STATE_TOO_LARGE")
    return encoded


def _direction(value: Any) -> TaskDirection:
    if value not in {"owned", "received"}:
        raise TaskStoreError("TASK_DIRECTION_INVALID")
    return value


def _identifier(value: Any, code: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise TaskStoreError(code)
    return value


def _peer(value: Any) -> str:
    if not isinstance(value, str):
        raise TaskStoreError("TASK_PEER_INVALID")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise TaskStoreError("TASK_PEER_INVALID") from error
    if not 1 <= size <= 256 or "\x00" in value:
        raise TaskStoreError("TASK_PEER_INVALID")
    return value


def _safe_filename(value: Any, fallback: str) -> str:
    name = value if isinstance(value, str) else ""
    # A remote filename is display metadata, never a path.  Keeping only the
    # final component also prevents Windows and POSIX separator ambiguity.
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


def _set_private_mode(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError as error:
        raise TaskStoreError("TASK_STATE_IO_ERROR") from error


class DsoftbusTaskStore:
    """Owner-loop-confined persistent Task repository.

    ``state_root=None`` selects an in-memory SQLite database and no artifact
    filesystem.  Product construction always supplies the Runtime state root.
    """

    def __init__(self, state_root: str | Path | None = None) -> None:
        self._root: Path | None = None
        database = ":memory:"
        if state_root is not None:
            root = Path(state_root)
            try:
                root.mkdir(parents=True, exist_ok=True)
                if not root.is_dir() or root.is_symlink():
                    raise OSError("state root is not a real directory")
                _set_private_mode(root, 0o700)
            except OSError as error:
                raise TaskStoreError("TASK_STATE_IO_ERROR") from error
            self._root = root
            database = str(root / "tasks.db")
        try:
            self._connection = sqlite3.connect(database)
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA foreign_keys=ON")
            if self._root is not None:
                # The Runtime has one endpoint owner, so rollback journaling is
                # sufficient and avoids persistent ``-wal``/``-shm`` files
                # with platform-dependent creation modes.
                self._connection.execute("PRAGMA journal_mode=DELETE")
                self._connection.execute("PRAGMA synchronous=FULL")
            self._create_schema()
            if self._root is not None:
                _set_private_mode(self._root / "tasks.db", 0o600)
        except (OSError, sqlite3.Error) as error:
            raise TaskStoreError("TASK_STATE_IO_ERROR") from error

    @property
    def database_path(self) -> Path | None:
        return None if self._root is None else self._root / "tasks.db"

    @property
    def state_root(self) -> Path | None:
        return self._root

    def _create_schema(self) -> None:
        with self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    direction TEXT NOT NULL,
                    peer_device_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    context_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    request_message_id TEXT NOT NULL DEFAULT '',
                    peer_runtime_instance_id TEXT NOT NULL DEFAULT '',
                    request_digest TEXT NOT NULL DEFAULT '',
                    task_json BLOB NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (direction, peer_device_id, task_id)
                );
                CREATE INDEX IF NOT EXISTS tasks_peer_updated
                    ON tasks(direction, peer_device_id, updated_at DESC);
                CREATE INDEX IF NOT EXISTS tasks_context_updated
                    ON tasks(direction, peer_device_id, context_id, updated_at DESC);
                CREATE TABLE IF NOT EXISTS artifact_receipts (
                    direction TEXT NOT NULL,
                    peer_device_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    artifact_id TEXT NOT NULL,
                    artifact_json BLOB NOT NULL,
                    local_json BLOB NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (
                        direction, peer_device_id, task_id, artifact_id
                    ),
                    FOREIGN KEY (direction, peer_device_id, task_id)
                        REFERENCES tasks(direction, peer_device_id, task_id)
                        ON DELETE CASCADE
                );
                """
            )
            columns = {
                str(row[1])
                for row in self._connection.execute(
                    "PRAGMA table_info(tasks)"
                ).fetchall()
            }
            for name in (
                "request_message_id",
                "peer_runtime_instance_id",
                "request_digest",
            ):
                if name not in columns:
                    self._connection.execute(
                        f"ALTER TABLE tasks ADD COLUMN {name} "
                        "TEXT NOT NULL DEFAULT ''"
                    )
            self._connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS tasks_request_identity
                    ON tasks(
                        direction, peer_device_id,
                        peer_runtime_instance_id, request_message_id
                    )
                    WHERE request_message_id <> ''
                      AND peer_runtime_instance_id <> ''
                """
            )

    @staticmethod
    def _validate_task(task: Any) -> dict[str, Any]:
        value = _plain(task)
        if not isinstance(value, dict):
            raise TaskStoreError("TASK_STATE_INVALID")
        if not {"id", "contextId", "status"}.issubset(value):
            raise TaskStoreError("TASK_STATE_INVALID")
        _identifier(value["id"], "TASK_ID_INVALID")
        _identifier(value["contextId"], "TASK_CONTEXT_INVALID")
        status = value["status"]
        if not isinstance(status, dict) or status.get("state") not in KNOWN_TASK_STATES:
            raise TaskStoreError("TASK_STATE_INVALID")
        _canonical_json(value)
        return value

    def put_task(
        self,
        direction: TaskDirection,
        peer_device_id: str,
        task: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        direction = _direction(direction)
        peer_device_id = _peer(peer_device_id)
        value = self._validate_task(task)
        encoded = _canonical_json(value)
        metadata = value.get("metadata", {})
        request_message_id = ""
        peer_runtime_instance_id = ""
        request_digest = ""
        if isinstance(metadata, dict):
            candidate = metadata.get("mclaw.requestMessageId")
            if isinstance(candidate, str) and _ID.fullmatch(candidate):
                request_message_id = candidate
            candidate = metadata.get("mclaw.peerRuntimeInstanceId")
            if isinstance(candidate, str) and _ID.fullmatch(candidate):
                peer_runtime_instance_id = candidate
            candidate = metadata.get("mclaw.requestDigest")
            if isinstance(candidate, str) and _HEX64.fullmatch(candidate):
                request_digest = candidate
        now = _timestamp()
        try:
            with self._connection:
                existing = self._connection.execute(
                    """
                    SELECT context_id, state FROM tasks
                    WHERE direction=? AND peer_device_id=? AND task_id=?
                    """,
                    (direction, peer_device_id, value["id"]),
                ).fetchone()
                if existing is not None:
                    if str(existing["context_id"]) != value["contextId"]:
                        raise TaskStoreError("TASK_CONTEXT_CONFLICT")
                    prior_state = str(existing["state"])
                    if value["status"]["state"] not in _TASK_STATE_TRANSITIONS.get(
                        prior_state, frozenset()
                    ):
                        raise TaskStoreError("TASK_STATE_REGRESSION")
                self._connection.execute(
                    """
                    INSERT INTO tasks(
                        direction, peer_device_id, task_id, context_id, state,
                        request_message_id, peer_runtime_instance_id,
                        request_digest, task_json, created_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(direction, peer_device_id, task_id) DO UPDATE SET
                        context_id=excluded.context_id,
                        state=excluded.state,
                        request_message_id=excluded.request_message_id,
                        peer_runtime_instance_id=excluded.peer_runtime_instance_id,
                        request_digest=excluded.request_digest,
                        task_json=excluded.task_json,
                        updated_at=excluded.updated_at
                    """,
                    (
                        direction,
                        peer_device_id,
                        value["id"],
                        value["contextId"],
                        value["status"]["state"],
                        request_message_id,
                        peer_runtime_instance_id,
                        request_digest,
                        encoded,
                        now,
                        now,
                    ),
                )
        except TaskStoreError:
            raise
        except sqlite3.Error as error:
            raise TaskStoreError("TASK_STATE_IO_ERROR") from error
        return _freeze(value)

    def find_task_by_request(
        self,
        direction: TaskDirection,
        peer_device_id: str,
        peer_runtime_instance_id: str,
        request_message_id: str,
    ) -> Mapping[str, Any] | None:
        """Find an idempotent Task without a bounded recent-task scan."""

        direction = _direction(direction)
        peer_device_id = _peer(peer_device_id)
        peer_runtime_instance_id = _identifier(
            peer_runtime_instance_id, "TASK_RUNTIME_INVALID"
        )
        request_message_id = _identifier(
            request_message_id, "TASK_REQUEST_INVALID"
        )
        try:
            row = self._connection.execute(
                """
                SELECT task_json FROM tasks
                WHERE direction=? AND peer_device_id=?
                  AND peer_runtime_instance_id=? AND request_message_id=?
                """,
                (
                    direction,
                    peer_device_id,
                    peer_runtime_instance_id,
                    request_message_id,
                ),
            ).fetchone()
        except sqlite3.Error as error:
            raise TaskStoreError("TASK_STATE_IO_ERROR") from error
        if row is None:
            return None
        try:
            return _freeze(json.loads(bytes(row["task_json"])))
        except (json.JSONDecodeError, TypeError, ValueError) as error:
            raise TaskStoreError("TASK_STATE_CORRUPT") from error

    def get_task(
        self,
        direction: TaskDirection,
        peer_device_id: str,
        task_id: str,
    ) -> Mapping[str, Any] | None:
        direction = _direction(direction)
        peer_device_id = _peer(peer_device_id)
        task_id = _identifier(task_id, "TASK_ID_INVALID")
        try:
            row = self._connection.execute(
                """
                SELECT task_json FROM tasks
                WHERE direction=? AND peer_device_id=? AND task_id=?
                """,
                (direction, peer_device_id, task_id),
            ).fetchone()
        except sqlite3.Error as error:
            raise TaskStoreError("TASK_STATE_IO_ERROR") from error
        if row is None:
            return None
        try:
            return _freeze(json.loads(bytes(row["task_json"])))
        except (json.JSONDecodeError, TypeError, ValueError) as error:
            raise TaskStoreError("TASK_STATE_CORRUPT") from error

    def list_tasks(
        self,
        direction: TaskDirection,
        peer_device_id: str,
        *,
        context_id: str | None = None,
        state: str | None = None,
        limit: int = 100,
    ) -> tuple[Mapping[str, Any], ...]:
        direction = _direction(direction)
        peer_device_id = _peer(peer_device_id)
        if context_id is not None:
            context_id = _identifier(context_id, "TASK_CONTEXT_INVALID")
        if state is not None and state not in KNOWN_TASK_STATES:
            raise TaskStoreError("TASK_STATE_INVALID")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise TaskStoreError("TASK_LIST_LIMIT_INVALID")
        clauses = ["direction=?", "peer_device_id=?"]
        values: list[Any] = [direction, peer_device_id]
        if context_id is not None:
            clauses.append("context_id=?")
            values.append(context_id)
        if state is not None:
            clauses.append("state=?")
            values.append(state)
        values.append(limit)
        try:
            rows = self._connection.execute(
                "SELECT task_json FROM tasks WHERE "
                + " AND ".join(clauses)
                + " ORDER BY updated_at DESC, task_id ASC LIMIT ?",
                tuple(values),
            ).fetchall()
        except sqlite3.Error as error:
            raise TaskStoreError("TASK_STATE_IO_ERROR") from error
        result: list[Mapping[str, Any]] = []
        try:
            for row in rows:
                result.append(_freeze(json.loads(bytes(row["task_json"]))))
        except (json.JSONDecodeError, TypeError, ValueError) as error:
            raise TaskStoreError("TASK_STATE_CORRUPT") from error
        return tuple(result)

    def _artifact_directory(
        self,
        direction: TaskDirection,
        peer_device_id: str,
        task_id: str,
        artifact_id: str,
    ) -> Path | None:
        root = self._root
        if root is None:
            return None
        if direction == "owned":
            directory = root / "artifacts" / "owned" / task_id / artifact_id
        else:
            digest = hashlib.sha256(peer_device_id.encode("utf-8")).hexdigest()
            if _HEX64.fullmatch(digest) is None:  # pragma: no cover - hashlib invariant
                raise AssertionError("invalid peer digest")
            directory = (
                root
                / "artifacts"
                / "received"
                / digest
                / task_id
                / artifact_id
            )
        try:
            directory.mkdir(parents=True, exist_ok=True)
            if not directory.is_dir() or directory.is_symlink():
                raise OSError("artifact directory is not a real directory")
            current = directory
            stop = root.parent
            while current != stop and current.is_relative_to(root):
                _set_private_mode(current, 0o700)
                if current == root:
                    break
                current = current.parent
        except OSError as error:
            raise TaskStoreError("ARTIFACT_IO_ERROR") from error
        return directory

    @staticmethod
    def _part_bytes(part: Mapping[str, Any]) -> tuple[bytes, str]:
        choices = [name for name in ("text", "raw", "data") if name in part]
        if len(choices) != 1 or "url" in part:
            raise TaskStoreError("ARTIFACT_PART_UNSUPPORTED")
        choice = choices[0]
        if choice == "text":
            text = part["text"]
            if not isinstance(text, str):
                raise TaskStoreError("ARTIFACT_INVALID")
            try:
                return text.encode("utf-8"), ".txt"
            except UnicodeEncodeError as error:
                raise TaskStoreError("ARTIFACT_INVALID") from error
        if choice == "raw":
            raw = part["raw"]
            if not isinstance(raw, str):
                raise TaskStoreError("ARTIFACT_INVALID")
            try:
                encoded = raw.encode("ascii")
                decoded = base64.b64decode(encoded, validate=True)
            except (UnicodeEncodeError, binascii.Error, ValueError) as error:
                raise TaskStoreError("ARTIFACT_INVALID") from error
            if base64.b64encode(decoded) != encoded:
                raise TaskStoreError("ARTIFACT_INVALID")
            return decoded, ".bin"
        return _canonical_json(part["data"]), ".json"

    @staticmethod
    def _atomic_write(path: Path, raw: bytes) -> None:
        if len(raw) > protocol.TASK_ARTIFACT_BYTES_MAX:
            raise TaskStoreError("ARTIFACT_TOO_LARGE")
        part = path.with_name(f".{path.name}.part")
        try:
            if part.exists():
                part.unlink()
            descriptor = os.open(
                part,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            try:
                with os.fdopen(descriptor, "wb", closefd=True) as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
            except BaseException:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                raise
            if part.stat().st_size != len(raw):
                raise OSError("artifact byte length mismatch")
            os.replace(part, path)
            _set_private_mode(path, 0o600)
            # POSIX directory fsync makes the rename durable.  Windows does
            # not permit opening directories through ``os.open``; the file
            # itself has already been flushed before ``os.replace`` there.
            if os.name != "nt":
                directory_descriptor = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_descriptor)
                finally:
                    os.close(directory_descriptor)
        except (OSError, ValueError) as error:
            try:
                part.unlink(missing_ok=True)
            except OSError:
                pass
            raise TaskStoreError("ARTIFACT_IO_ERROR") from error

    def persist_artifact(
        self,
        direction: TaskDirection,
        peer_device_id: str,
        task_id: str,
        artifact: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        direction = _direction(direction)
        peer_device_id = _peer(peer_device_id)
        task_id = _identifier(task_id, "TASK_ID_INVALID")
        value = _plain(artifact)
        if not isinstance(value, dict):
            raise TaskStoreError("ARTIFACT_INVALID")
        artifact_id = _identifier(value.get("artifactId"), "ARTIFACT_ID_INVALID")
        parts = value.get("parts")
        if not isinstance(parts, list) or not 1 <= len(parts) <= 32:
            raise TaskStoreError("ARTIFACT_INVALID")
        if self.get_task(direction, peer_device_id, task_id) is None:
            raise TaskStoreError("TASK_NOT_FOUND")
        artifact_json = _canonical_json(value)
        try:
            existing = self._connection.execute(
                """
                SELECT artifact_json FROM artifact_receipts
                WHERE direction=? AND peer_device_id=?
                  AND task_id=? AND artifact_id=?
                """,
                (direction, peer_device_id, task_id, artifact_id),
            ).fetchone()
        except sqlite3.Error as error:
            raise TaskStoreError("TASK_STATE_IO_ERROR") from error
        if existing is not None and bytes(existing["artifact_json"]) != artifact_json:
            # Artifact IDs are immutable within one Task.  This also prevents
            # a reconnect replay from replacing already verified local bytes
            # with different peer-controlled content.
            raise TaskStoreError("ARTIFACT_CONFLICT")
        directory = self._artifact_directory(
            direction, peer_device_id, task_id, artifact_id
        )
        local_parts: list[dict[str, Any]] = []
        total = 0
        for index, part_value in enumerate(parts):
            if not isinstance(part_value, Mapping):
                raise TaskStoreError("ARTIFACT_INVALID")
            part = _plain(part_value)
            raw, fallback_suffix = self._part_bytes(part)
            total += len(raw)
            if total > protocol.TASK_ARTIFACT_BYTES_MAX:
                raise TaskStoreError("ARTIFACT_TOO_LARGE")
            base = _safe_filename(
                part.get("filename") or value.get("name"),
                f"part-{index + 1}{fallback_suffix}",
            )
            if "." not in base and fallback_suffix:
                base += fallback_suffix
            filename = f"{index + 1:02d}-{base}"
            sha256 = hashlib.sha256(raw).hexdigest()
            local_path = ""
            if directory is not None:
                path = directory / filename
                self._atomic_write(path, raw)
                if path.stat().st_size != len(raw):
                    raise TaskStoreError("ARTIFACT_IO_ERROR")
                with path.open("rb") as stream:
                    actual = hashlib.sha256(stream.read()).hexdigest()
                if actual != sha256:
                    raise TaskStoreError("ARTIFACT_HASH_MISMATCH")
                local_path = str(path.resolve())
            local_parts.append(
                {
                    "index": index,
                    "filename": filename,
                    "localPath": local_path,
                    "mediaType": str(part.get("mediaType") or ""),
                    "byteLength": len(raw),
                    "sha256": sha256,
                }
            )
        local = {
            "artifactId": artifact_id,
            "parts": local_parts,
            "totalByteLength": total,
        }
        local_json = _canonical_json(local)
        try:
            with self._connection:
                self._connection.execute(
                    """
                    INSERT INTO artifact_receipts(
                        direction, peer_device_id, task_id, artifact_id,
                        artifact_json, local_json, created_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(
                        direction, peer_device_id, task_id, artifact_id
                    ) DO UPDATE SET
                        artifact_json=excluded.artifact_json,
                        local_json=excluded.local_json
                    """,
                    (
                        direction,
                        peer_device_id,
                        task_id,
                        artifact_id,
                        artifact_json,
                        local_json,
                        _timestamp(),
                    ),
                )
        except sqlite3.Error as error:
            raise TaskStoreError("TASK_STATE_IO_ERROR") from error
        return _freeze(local)

    def get_artifact_receipt(
        self,
        direction: TaskDirection,
        peer_device_id: str,
        task_id: str,
        artifact_id: str,
    ) -> Mapping[str, Any] | None:
        direction = _direction(direction)
        peer_device_id = _peer(peer_device_id)
        task_id = _identifier(task_id, "TASK_ID_INVALID")
        artifact_id = _identifier(artifact_id, "ARTIFACT_ID_INVALID")
        try:
            row = self._connection.execute(
                """
                SELECT local_json FROM artifact_receipts
                WHERE direction=? AND peer_device_id=?
                  AND task_id=? AND artifact_id=?
                """,
                (direction, peer_device_id, task_id, artifact_id),
            ).fetchone()
        except sqlite3.Error as error:
            raise TaskStoreError("TASK_STATE_IO_ERROR") from error
        if row is None:
            return None
        try:
            return _freeze(json.loads(bytes(row["local_json"])))
        except (json.JSONDecodeError, TypeError, ValueError) as error:
            raise TaskStoreError("TASK_STATE_CORRUPT") from error

    def recover_interrupted_owned_tasks(self) -> int:
        """Mark work that cannot survive a Runtime process restart as failed."""

        try:
            rows = self._connection.execute(
                """
                SELECT peer_device_id, task_id, task_json FROM tasks
                WHERE direction='owned' AND state IN (
                    'TASK_STATE_SUBMITTED', 'TASK_STATE_WORKING'
                )
                """
            ).fetchall()
            count = 0
            for row in rows:
                task = json.loads(bytes(row["task_json"]))
                task["status"] = {
                    "state": "TASK_STATE_FAILED",
                    "timestamp": _timestamp(),
                    "message": {
                        "messageId": str(
                            uuid.uuid5(
                                uuid.NAMESPACE_URL,
                                f"mclaw:dsoftbus:restart:{row['task_id']}",
                            )
                        ),
                        "contextId": task["contextId"],
                        "taskId": task["id"],
                        "role": "ROLE_AGENT",
                        "parts": [
                            {
                                "text": "Runtime restarted before the task completed."
                            }
                        ],
                        "metadata": {
                            "mclaw.failureReason": "AGENT_INTERRUPTED"
                        },
                    },
                }
                self.put_task("owned", str(row["peer_device_id"]), task)
                count += 1
            return count
        except (json.JSONDecodeError, sqlite3.Error, TypeError, ValueError) as error:
            raise TaskStoreError("TASK_STATE_CORRUPT") from error

    def close(self) -> None:
        try:
            self._connection.close()
        except sqlite3.Error as error:
            raise TaskStoreError("TASK_STATE_IO_ERROR") from error


__all__ = [
    "DsoftbusTaskStore",
    "KNOWN_TASK_STATES",
    "TERMINAL_TASK_STATES",
    "TaskDirection",
    "TaskStoreError",
]
