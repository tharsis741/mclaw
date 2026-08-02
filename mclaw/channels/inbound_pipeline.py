# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Composable preprocessing for normalized inbound channel messages."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable, Iterable
from typing import Any, Protocol, TypeAlias

from mclaw.channels.base import ChannelMessage

logger = logging.getLogger(__name__)

DEFAULT_INBOUND_CAPABILITY_ERROR_CODE = "inbound_capability_failed"
DEFAULT_INBOUND_CAPABILITY_SAFE_MESSAGE = "Unable to process the voice message safely."


class InboundCapability(Protocol):
    """Object contract for one ordered inbound-message capability."""

    async def process(self, message: ChannelMessage) -> ChannelMessage:
        """Return the message that the next capability should receive."""


InboundCapabilityCallable: TypeAlias = Callable[
    [ChannelMessage],
    ChannelMessage | Awaitable[ChannelMessage],
]
InboundCapabilityLike: TypeAlias = InboundCapability | InboundCapabilityCallable


class InboundCapabilityError(RuntimeError):
    """Internal failure raised when an inbound capability cannot complete safely."""

    def __init__(
        self,
        capability: str = "",
        *,
        code: str = DEFAULT_INBOUND_CAPABILITY_ERROR_CODE,
        detail: str = "",
        safe_message: str = DEFAULT_INBOUND_CAPABILITY_SAFE_MESSAGE,
    ) -> None:
        self.capability = str(capability or "")
        self.code = str(code or DEFAULT_INBOUND_CAPABILITY_ERROR_CODE)
        self.detail = str(detail or "")
        self.safe_message = str(safe_message or DEFAULT_INBOUND_CAPABILITY_SAFE_MESSAGE)
        super().__init__(self.detail or f"Inbound capability {self.capability!r} failed")


def _capability_name(capability: InboundCapabilityLike) -> str:
    explicit = str(getattr(capability, "name", "") or "").strip()
    if explicit:
        return explicit
    function_name = str(getattr(capability, "__name__", "") or "").strip()
    if function_name:
        return function_name
    return type(capability).__name__


def _capability_processor(capability: InboundCapabilityLike) -> Callable[[ChannelMessage], Any]:
    processor = getattr(capability, "process", None)
    if callable(processor):
        return processor
    if callable(capability):
        return capability
    raise TypeError(f"Inbound capability {_capability_name(capability)!r} is not callable")


class InboundCapabilityPipeline:
    """Run injected inbound capabilities sequentially.

    The empty pipeline is an identity operation and returns the exact message
    instance it received.  Capabilities may be async ``process`` objects or
    callables; each must return a :class:`ChannelMessage`.
    """

    def __init__(self, capabilities: Iterable[InboundCapabilityLike] | None = None) -> None:
        self.capabilities = tuple(capabilities or ())

    def __bool__(self) -> bool:
        return bool(self.capabilities)

    async def process(self, message: ChannelMessage) -> ChannelMessage:
        """Apply capabilities in registration order and return the final message."""
        current = message
        for capability in self.capabilities:
            name = _capability_name(capability)
            try:
                outcome = _capability_processor(capability)(current)
                if inspect.isawaitable(outcome):
                    outcome = await outcome
                if not isinstance(outcome, ChannelMessage):
                    raise TypeError(
                        f"Inbound capability {name!r} returned "
                        f"{type(outcome).__name__}, expected ChannelMessage"
                    )
                current = outcome
            except asyncio.CancelledError:
                raise
            except InboundCapabilityError as exc:
                if not exc.capability:
                    exc.capability = name
                raise
            except Exception as exc:
                logger.exception("inbound capability failed capability=%s", name)
                raise InboundCapabilityError(name, detail=str(exc)) from exc
        return current

    async def run(self, message: ChannelMessage) -> ChannelMessage:
        """Alias for callers that describe pipeline execution as a run."""
        return await self.process(message)

    async def __call__(self, message: ChannelMessage) -> ChannelMessage:
        return await self.process(message)
