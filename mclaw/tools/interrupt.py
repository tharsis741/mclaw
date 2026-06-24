"""Shared interrupt signaling for all tools.

Provides a global threading.Event that any tool can check to determine
if the user has requested an interrupt.
"""

import threading

_interrupt_event = threading.Event()


def set_interrupt(active: bool) -> None:
    if active:
        _interrupt_event.set()
    else:
        _interrupt_event.clear()


def is_interrupted() -> bool:
    return _interrupt_event.is_set()
