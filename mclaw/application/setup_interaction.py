# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Context-local presentation boundary for the shared Setup workflow."""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Protocol


class SetupInteractionCancelled(BaseException):
    """Interrupt a setup operation without treating cancellation as a failure."""


class SetupInteraction(Protocol):
    def request(self, kind: str, title: str, **kwargs: Any) -> Any: ...

    def write(self, text: str) -> None: ...

    def check_cancelled(self) -> None: ...

    def show_qr(self, payload: str) -> None: ...


current_setup_interaction: ContextVar[SetupInteraction | None] = ContextVar(
    "mclaw_setup_interaction", default=None,
)


def check_setup_cancelled() -> None:
    """Check a Web cancellation before persisting a Setup mutation."""
    interaction = current_setup_interaction.get()
    if interaction is not None:
        interaction.check_cancelled()
