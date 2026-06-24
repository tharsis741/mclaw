"""Session-scoped single-frontend lock for interactive chat."""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from mclaw.constants import get_mclaw_home


class InteractiveSessionLockError(RuntimeError):
    """Raised when an interactive session is already owned by another process."""


def _is_process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            process = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if not process:
                return False
            ctypes.windll.kernel32.CloseHandle(process)
            return True
        except Exception:
            return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _lock_name(session_id: str) -> str:
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
        self.root.mkdir(parents=True, exist_ok=True)
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
        if not self._acquired:
            return
        try:
            data = self._read_lock()
            if (
                int(data.get("pid") or 0) == os.getpid()
                and str(data.get("session_id") or "") == self.session_id
            ):
                self.path.unlink(missing_ok=True)
        finally:
            self._acquired = False

    def _handle_existing_lock(self) -> None:
        data = self._read_lock()
        pid = int(data.get("pid") or 0)
        if pid and _is_process_alive(pid):
            created_at = str(data.get("created_at") or "unknown")
            cwd = str(data.get("cwd") or "unknown")
            raise InteractiveSessionLockError(
                f"Session {self.session_id} is already open in another M-Claw frontend "
                f"(pid={pid}, started={created_at}, cwd={cwd})."
            )
        self.path.unlink(missing_ok=True)

    def _read_lock(self) -> dict[str, Any]:
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
