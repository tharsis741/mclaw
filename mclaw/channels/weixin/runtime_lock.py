# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Single-instance lock for a Weixin iLink account/token."""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from mclaw.constants import get_mclaw_home


class WeixinRuntimeLockError(RuntimeError):
    pass


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


def _lock_name(account_id: str, token: str) -> str:
    digest = hashlib.sha256(f"{account_id}:{token}".encode("utf-8")).hexdigest()[:24]
    return f"{digest}.lock"


class WeixinRuntimeLock:
    def __init__(self, *, account_id: str, token: str, root: Path | None = None) -> None:
        self.account_id = account_id
        self.token = token
        self.root = root or (get_mclaw_home() / "weixin" / "locks")
        self.path = self.root / _lock_name(account_id, token)
        self._acquired = False

    def acquire(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {
            "pid": os.getpid(),
            "account_id": self.account_id,
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
            if int(data.get("pid") or 0) == os.getpid():
                self.path.unlink(missing_ok=True)
        finally:
            self._acquired = False

    def _handle_existing_lock(self) -> None:
        data = self._read_lock()
        pid = int(data.get("pid") or 0)
        if pid and _is_process_alive(pid):
            raise WeixinRuntimeLockError(
                f"Weixin gateway is already running for this account/token (pid={pid}). "
                "Stop the existing mclaw weixin process before starting another one."
            )
        self.path.unlink(missing_ok=True)

    def _read_lock(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def __enter__(self) -> "WeixinRuntimeLock":
        self.acquire()
        return self

    def __exit__(self, *_exc_info) -> None:
        self.release()
