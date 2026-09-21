# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""One scheduler controller per application data root, across entrypoints."""

from pathlib import Path
import threading

from mclaw.cli.runtime.session_lock import InteractiveSessionLock, InteractiveSessionLockError


class SchedulerOwnershipError(RuntimeError):
    """Another live frontend already runs this root's scheduler."""


class SchedulerOwnership:
    def __init__(self, home: Path) -> None:
        self._lock = InteractiveSessionLock("scheduler-controller", home / "scheduler" / "locks")
        self.acquired = False
        self._guard = threading.Lock()

    def acquire(self) -> bool:
        with self._guard:
            if self.acquired:
                return True
            try:
                self._lock.acquire()
            except InteractiveSessionLockError:
                return False
            self.acquired = True
            return True

    def release(self) -> None:
        if self.acquired:
            self._lock.release()
            self.acquired = False
