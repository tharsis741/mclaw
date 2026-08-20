# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Process-local accessor for the one active DSoftBus Runtime instance."""

from __future__ import annotations

import threading
from typing import Any


class ActiveRuntimeError(RuntimeError):
    """Stable active-Runtime ownership failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


_LOCK = threading.RLock()
_ACTIVE_RUNTIME: Any | None = None


def install_active_runtime(runtime: Any) -> None:
    """Install *runtime*, allowing only identity-idempotent reinstallation."""
    if runtime is None:
        raise ValueError("runtime must not be None")
    global _ACTIVE_RUNTIME
    with _LOCK:
        if _ACTIVE_RUNTIME is None:
            _ACTIVE_RUNTIME = runtime
            return
        if _ACTIVE_RUNTIME is runtime:
            return
        raise ActiveRuntimeError("ACTIVE_RUNTIME_EXISTS")


def get_active_runtime() -> Any | None:
    """Return the current process-local Runtime without starting it."""
    with _LOCK:
        return _ACTIVE_RUNTIME


def clear_active_runtime(expected: Any) -> bool:
    """Clear only when *expected* is the currently installed object."""
    global _ACTIVE_RUNTIME
    with _LOCK:
        if _ACTIVE_RUNTIME is not expected:
            return False
        _ACTIVE_RUNTIME = None
        return True


__all__ = [
    "ActiveRuntimeError",
    "clear_active_runtime",
    "get_active_runtime",
    "install_active_runtime",
]
