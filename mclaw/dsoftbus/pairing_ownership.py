# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Durable ownership fence for trust relations created by M-Claw.

DeviceManager trust is package scoped, while a board can also contain trust
created by other product applications.  This store contains only redacted
device digests and prevents ``/unpair`` from acting on an ACL that M-Claw did
not create itself.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Any, NoReturn

from .protocol import canonical_json_bytes


PAIRING_OWNERSHIP_SCHEMA = "mclaw.dsoftbus.pairing-ownership/v1"
PAIRING_OWNERSHIP_MAX = 256
PAIRING_OWNERSHIP_BYTES_MAX = 32_768
_HEX = frozenset("0123456789abcdef")


class PairingOwnershipError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _fail(code: str) -> NoReturn:
    raise PairingOwnershipError(code)


def _validate_digest(value: Any) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _HEX for character in value)
    ):
        _fail("PAIRING_STATE_INVALID")
    return value


class PairingOwnershipStore:
    """Owner-thread store with canonical, atomic, root-private persistence."""

    def __init__(self, path: str | Path) -> None:
        selected = Path(path)
        if not selected.is_absolute():
            raise ValueError("pairing ownership path must be absolute")
        self._path = selected
        self._digests: set[str] = set()
        self._loaded = False

    @property
    def path(self) -> Path:
        return self._path

    @staticmethod
    def _document(digests: set[str]) -> dict[str, Any]:
        return {
            "deviceIdSha256": sorted(digests),
            "schemaVersion": PAIRING_OWNERSHIP_SCHEMA,
        }

    @staticmethod
    def _validate_document(value: Any) -> set[str]:
        if not isinstance(value, dict) or set(value) != {
            "deviceIdSha256",
            "schemaVersion",
        }:
            _fail("PAIRING_STATE_INVALID")
        if value["schemaVersion"] != PAIRING_OWNERSHIP_SCHEMA:
            _fail("PAIRING_STATE_INVALID")
        rows = value["deviceIdSha256"]
        if not isinstance(rows, list) or len(rows) > PAIRING_OWNERSHIP_MAX:
            _fail("PAIRING_STATE_INVALID")
        normalized = [_validate_digest(item) for item in rows]
        if normalized != sorted(set(normalized)):
            _fail("PAIRING_STATE_INVALID")
        return set(normalized)

    @staticmethod
    def _validate_stat(metadata: os.stat_result) -> None:
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size < 1
            or metadata.st_size > PAIRING_OWNERSHIP_BYTES_MAX
        ):
            _fail("PAIRING_STATE_INVALID")
        if os.name == "posix":
            if stat.S_IMODE(metadata.st_mode) != 0o600:
                _fail("PAIRING_STATE_INVALID")
            geteuid = getattr(os, "geteuid", None)
            if callable(geteuid) and metadata.st_uid != geteuid():
                _fail("PAIRING_STATE_INVALID")

    def load(self) -> tuple[str, ...]:
        if self._loaded:
            return tuple(sorted(self._digests))
        try:
            before = self._path.lstat()
        except FileNotFoundError:
            self._loaded = True
            return ()
        if stat.S_ISLNK(before.st_mode):
            _fail("PAIRING_STATE_INVALID")
        self._validate_stat(before)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self._path, flags)
        except OSError as error:
            raise PairingOwnershipError("PAIRING_STATE_INVALID") from error
        try:
            opened = os.fstat(descriptor)
            self._validate_stat(opened)
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                _fail("PAIRING_STATE_INVALID")
            chunks: list[bytes] = []
            remaining = PAIRING_OWNERSHIP_BYTES_MAX + 1
            while remaining:
                chunk = os.read(descriptor, min(remaining, 8_192))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
        finally:
            os.close(descriptor)
        if not raw or len(raw) > PAIRING_OWNERSHIP_BYTES_MAX:
            _fail("PAIRING_STATE_INVALID")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise PairingOwnershipError("PAIRING_STATE_INVALID") from error
        digests = self._validate_document(value)
        if canonical_json_bytes(self._document(digests)) != raw:
            _fail("PAIRING_STATE_INVALID")
        self._digests = digests
        self._loaded = True
        return tuple(sorted(self._digests))

    def _require_loaded(self) -> None:
        if not self._loaded:
            _fail("PAIRING_STATE_NOT_LOADED")

    def _save(self) -> None:
        self._require_loaded()
        encoded = canonical_json_bytes(self._document(self._digests))
        if len(encoded) > PAIRING_OWNERSHIP_BYTES_MAX:
            _fail("PAIRING_STATE_CAPACITY")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(
                prefix=f".{self._path.name}.",
                suffix=".tmp",
                dir=self._path.parent,
            )
            temporary_path = Path(temporary)
            try:
                os.chmod(temporary_path, 0o600)
                offset = 0
                while offset < len(encoded):
                    written = os.write(descriptor, encoded[offset:])
                    if written <= 0:
                        raise OSError("short write")
                    offset += written
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(temporary_path, self._path)
            temporary_path = None
            if os.name == "posix":
                directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                directory = os.open(self._path.parent, directory_flags)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        except PairingOwnershipError:
            raise
        except OSError as error:
            raise PairingOwnershipError("PAIRING_STATE_WRITE_FAILED") from error
        finally:
            candidate = locals().get("temporary_path")
            if isinstance(candidate, Path):
                try:
                    candidate.unlink()
                except FileNotFoundError:
                    pass

    def digests(self) -> tuple[str, ...]:
        self._require_loaded()
        return tuple(sorted(self._digests))

    def contains(self, device_id_sha256: str) -> bool:
        self._require_loaded()
        return _validate_digest(device_id_sha256) in self._digests

    def add(self, device_id_sha256: str) -> None:
        self._require_loaded()
        digest = _validate_digest(device_id_sha256)
        if digest in self._digests:
            return
        if len(self._digests) >= PAIRING_OWNERSHIP_MAX:
            _fail("PAIRING_STATE_CAPACITY")
        self._digests.add(digest)
        try:
            self._save()
        except BaseException:
            self._digests.remove(digest)
            raise

    def remove(self, device_id_sha256: str) -> None:
        self._require_loaded()
        digest = _validate_digest(device_id_sha256)
        if digest not in self._digests:
            _fail("DEVICE_NOT_MANAGED")
        self._digests.remove(digest)
        try:
            self._save()
        except BaseException:
            self._digests.add(digest)
            raise


__all__ = [
    "PAIRING_OWNERSHIP_BYTES_MAX",
    "PAIRING_OWNERSHIP_MAX",
    "PAIRING_OWNERSHIP_SCHEMA",
    "PairingOwnershipError",
    "PairingOwnershipStore",
]
