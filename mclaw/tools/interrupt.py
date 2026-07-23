# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Turn-scoped interrupt signaling for tools."""

import threading
import uuid
from collections.abc import Callable
from contextvars import ContextVar, Token

_legacy_interrupt_event = threading.Event()
_cancel_id_lock = threading.Lock()
_current_interrupt_event: ContextVar[threading.Event | None] = ContextVar(
    "current_tool_interrupt_event",
    default=None,
)


def safe_cancel_trace(emit: Callable[[], object]) -> None:
    """Run cancellation diagnostics without letting them affect control flow.

    The callback is intentionally lazy so both record construction and handler
    execution stay behind the fault boundary.  This helper must never log its
    own failure: doing so would recurse through the same broken handler.
    """
    try:
        emit()
    except BaseException:
        pass


def set_interrupt_event(event: threading.Event | None) -> Token[threading.Event | None]:
    """Bind a turn-owned interrupt event to the current context."""
    return _current_interrupt_event.set(event)


def reset_interrupt_event(token: Token[threading.Event | None]) -> None:
    """Restore the interrupt event previously bound to this context."""
    _current_interrupt_event.reset(token)


def get_interrupt_event() -> threading.Event | None:
    """Return the turn-owned interrupt event bound to this context, if any."""
    return _current_interrupt_event.get()


def get_cancel_id(event: threading.Event | None = None) -> str:
    """Return the stable trace ID carried by one shared turn Event."""
    event = event or get_interrupt_event()
    if event is None:
        return "none"
    cancel_id = getattr(event, "_mclaw_cancel_id", None)
    if cancel_id:
        return str(cancel_id)
    with _cancel_id_lock:
        cancel_id = getattr(event, "_mclaw_cancel_id", None)
        if not cancel_id:
            cancel_id = uuid.uuid4().hex[:12]
            event._mclaw_cancel_id = cancel_id
    return str(cancel_id)


def set_interrupt(active: bool) -> None:
    """Request cancellation, or reset only the legacy process-wide flag.

    A turn-owned token is monotonic: once set, late cleanup must never clear it.
    """
    event = get_interrupt_event()
    if event is not None:
        if active:
            event.set()
        return
    if active:
        _legacy_interrupt_event.set()
    else:
        _legacy_interrupt_event.clear()


def is_interrupted() -> bool:
    """Return whether the current turn (or legacy process) is interrupted."""
    event = get_interrupt_event()
    if event is None:
        event = _legacy_interrupt_event
    return event.is_set()
