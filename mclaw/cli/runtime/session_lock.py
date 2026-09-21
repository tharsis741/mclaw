# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Session-scoped single-frontend lock for interactive chat."""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from mclaw.constants import get_mclaw_home
from mclaw.system.lock import file_lock
from mclaw.utils import atomic_json_write


class InteractiveSessionLockError(RuntimeError):
    """Raised when an interactive session is already owned by another process."""


def _is_process_alive(pid: int) -> bool:
    """Best-effort liveness probe used before removing a stale session lock."""
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            process = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if not process:
                return ctypes.windll.kernel32.GetLastError() == 5
            try:
                exit_code = ctypes.c_ulong()
                if not ctypes.windll.kernel32.GetExitCodeProcess(process, ctypes.byref(exit_code)):
                    return True
                return exit_code.value == 259
            finally:
                ctypes.windll.kernel32.CloseHandle(process)
        except Exception:
            return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True


def _lock_name(session_id: str) -> str:
    """Hash session IDs so lock filenames never expose raw session identifiers."""
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:24]
    return f"{digest}.lock"


class InteractiveSessionLock:
    """Exclusive lock for one interactive frontend attached to one session_id."""

    def __init__(self, session_id: str, root: Path | None = None) -> None:
        self.session_id = str(session_id)
        self.root = root or (get_mclaw_home() / "sessions" / "locks")
        self.path = self.root / _lock_name(self.session_id)
        self._acquired = False

    def acquire(self) -> None:
        """Create the lock file atomically or fail when a live owner exists."""
        self.root.mkdir(parents=True, exist_ok=True)
        with file_lock(self.root / ".session-lock.guard"):
            self._acquire_guarded()

    def _acquire_guarded(self) -> None:
        """Keep stale inspection and metadata publication in one OS lock."""
        payload = {
            "pid": os.getpid(),
            "session_id": self.session_id,
            "cwd": os.getcwd(),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        try:
            fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            self._handle_existing_lock()
            fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        self._acquired = True

    def release(self) -> None:
        """Remove the lock only when it is still owned by this process."""
        if not self._acquired:
            return
        with file_lock(self.root / ".session-lock.guard"):
            data = self._read_lock()
            worker_pid = int(data.get("worker_pid") or 0)
            if worker_pid and _is_process_alive(worker_pid):
                return
            try:
                if (
                    int(data.get("pid") or 0) == os.getpid()
                    and str(data.get("session_id") or "") == self.session_id
                ):
                    self.path.unlink(missing_ok=True)
            finally:
                self._acquired = False

    def attach_worker(self, worker_pid: int) -> None:
        """Fence a child that can outlive its owning service during cleanup."""
        with file_lock(self.root / ".session-lock.guard"):
            data = self._read_lock()
            if int(data.get("pid") or 0) != os.getpid() or data.get("session_id") != self.session_id:
                raise InteractiveSessionLockError("Session ownership changed before worker startup")
            data["worker_pid"] = int(worker_pid)
            atomic_json_write(self.path, data)

    def _handle_existing_lock(self) -> None:
        """Reject live owners and clear stale or unreadable lock files."""
        data = self._read_lock()
        pid = int(data.get("pid") or 0)
        worker_pid = int(data.get("worker_pid") or 0)
        if (pid and _is_process_alive(pid)) or (worker_pid and _is_process_alive(worker_pid)):
            created_at = str(data.get("created_at") or "unknown")
            cwd = str(data.get("cwd") or "unknown")
            raise InteractiveSessionLockError(
                f"Session {self.session_id} is already open in another M-Claw frontend "
                f"(pid={pid}, started={created_at}, cwd={cwd})."
            )
        self.path.unlink(missing_ok=True)

    def _read_lock(self) -> dict[str, Any]:
        """Read lock metadata, treating corrupt files as stale candidates."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def __enter__(self) -> "InteractiveSessionLock":
        self.acquire()
        return self

    def __exit__(self, *_exc_info) -> None:
        self.release()
