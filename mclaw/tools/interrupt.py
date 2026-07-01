# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared interrupt signaling for all tools.

Provides a global threading.Event that any tool can check to determine
if the user has requested an interrupt.
"""

import threading

_interrupt_event = threading.Event()


def set_interrupt(active: bool) -> None:
    """Set or clear the process-wide interrupt flag checked by tools."""
    if active:
        _interrupt_event.set()
    else:
        _interrupt_event.clear()


def is_interrupted() -> bool:
    """Return whether a user interrupt is currently active."""
    return _interrupt_event.is_set()
