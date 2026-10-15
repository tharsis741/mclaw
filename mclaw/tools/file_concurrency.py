# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Serialize standard file tools and retain each session's observed versions.

Locks are advisory: terminal commands, restore operations and external programs
do not acquire them. Their changes are detected by the next version check.
"""

from __future__ import annotations

import errno
import hashlib
import os
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from mclaw.constants import get_mclaw_home
from mclaw.tools.cancellation import cancellation_checkpoint

_SESSION: ContextVar[str | None] = ContextVar("file_tool_session", default=None)
_STATE_LOCK = threading.Lock()
_LOCKS: dict[str, threading.RLock] = {}
_HELD = threading.local()
_VERSIONS: dict[tuple[str, str], dict[str, FileVersion | None]] = {}
_LOCK_POLL_SECONDS = 0.05
_LOCK_TIMEOUT_SECONDS = 30.0


class FileVersionConflict(OSError):
    """A file changed since the session last observed it."""

    def __init__(self, path: Path) -> None:
        self.path = str(path)
        super().__init__(
            f"File changed since this session last read it: {path}. "
            "Call read_file again to inspect the current content before retrying "
            "the write, patch, edit or delete."
        )


@dataclass(frozen=True)
class FileVersion:
    """Content plus filesystem identity, including same-size/mtime changes."""

    identity: tuple[int, int, int, int, int]
    digest: str


def target_key(path: Path) -> str:
    """Key an already policy-approved real target, folding Windows case."""
    return os.path.normcase(str(path.resolve()))


def current_session_id() -> str:
    session_id = _SESSION.get()
    if session_id is not None:
        return session_id
    from mclaw.tools.dispatch import get_current_session_id

    return get_current_session_id()


@contextmanager
def file_session(session_id: str | None):
    """Bind a handler's explicit session without changing dispatcher context."""
    token = _SESSION.set(str(session_id) if session_id else None)
    try:
        yield
    finally:
        _SESSION.reset(token)


def _session_key() -> tuple[str, str] | None:
    session_id = current_session_id()
    if not session_id:
        return None
    return target_key(get_mclaw_home()), session_id


def _identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def file_version(path: Path, cancel_event: threading.Event | None = None) -> FileVersion | None:
    """Fingerprint a file while checking for changes during the hash read."""
    cancellation_checkpoint(cancel_event)
    try:
        before = path.stat()
    except FileNotFoundError:
        return None
    if not path.is_file():
        return FileVersion(_identity(before), "")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            cancellation_checkpoint(cancel_event)
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    cancellation_checkpoint(cancel_event)
    try:
        after = path.stat()
    except FileNotFoundError:
        raise FileVersionConflict(path) from None
    if _identity(before) != _identity(after):
        raise FileVersionConflict(path)
    return FileVersion(_identity(after), digest.hexdigest())


def remember_version(path: Path, version: FileVersion | None) -> None:
    session = _session_key()
    if session is not None:
        with _STATE_LOCK:
            _VERSIONS.setdefault(session, {})[target_key(path)] = version


def version_is_current(path: Path, version: FileVersion | None) -> bool:
    """Require a known matching observation before reusing a read result."""
    session = _session_key()
    with _STATE_LOCK:
        versions = _VERSIONS.get(session, {})
        key = target_key(path)
        return key in versions and versions[key] == version


def check_write_version(path: Path, version: FileVersion | None) -> None:
    """Reject stale observations; preserve writes without a previous read."""
    session = _session_key()
    with _STATE_LOCK:
        versions = _VERSIONS.get(session, {})
        key = target_key(path)
        stale = key in versions and versions[key] != version
    if stale:
        raise FileVersionConflict(path)


def clear_file_versions(session_id: str | None = None) -> None:
    """Release observations when a caller discards a session's read history."""
    with _STATE_LOCK:
        if session_id is None:
            _VERSIONS.clear()
        else:
            for key in list(_VERSIONS):
                if key[1] == session_id:
                    del _VERSIONS[key]


@contextmanager
def _process_lock(key: str, cancel_event: threading.Event | None):
    lock_root = get_mclaw_home() / "file_locks"
    lock_root.mkdir(parents=True, exist_ok=True)
    lock_path = lock_root / (hashlib.sha256(key.encode("utf-8")).hexdigest() + ".lock")
    # Retain lock files: unlinking allows contenders to lock different inodes.
    with lock_path.open("a+b") as handle:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"\0")
            handle.flush()
        deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
        while True:
            cancellation_checkpoint(cancel_event)
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError("Timed out waiting for another file operation; retry.") from exc
                if cancel_event is not None:
                    cancel_event.wait(_LOCK_POLL_SECONDS)
                else:
                    time.sleep(_LOCK_POLL_SECONDS)
        try:
            cancellation_checkpoint(cancel_event)
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def locked_target(path: Path, cancel_event: threading.Event | None = None):
    """Hold a reentrant, cancellable lock across cooperating threads/processes."""
    key = target_key(path)
    with _STATE_LOCK:
        lock = _LOCKS.setdefault(key, threading.RLock())
    while not lock.acquire(timeout=_LOCK_POLL_SECONDS):
        cancellation_checkpoint(cancel_event)
    try:
        cancellation_checkpoint(cancel_event)
        held = getattr(_HELD, "targets", None)
        if held is None:
            held = _HELD.targets = set()
        if key in held:
            yield
        else:
            with _process_lock(key, cancel_event):
                held.add(key)
                try:
                    yield
                finally:
                    held.remove(key)
    finally:
        lock.release()
