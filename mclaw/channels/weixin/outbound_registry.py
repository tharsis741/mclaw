# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Runtime registry for Weixin outbound tool calls.

Function-call tools run outside the channel event loop. The registry stores
session-bound targets so those tools can hand work back to the active adapter.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mclaw.channels.base import SendResult

if TYPE_CHECKING:
    from mclaw.channels.weixin.adapter import WeixinAdapter


@dataclass
class WeixinOutboundTarget:
    """Thread-safe handle for sending outbound files to one Weixin chat."""

    adapter: "WeixinAdapter"
    chat_id: str
    loop: asyncio.AbstractEventLoop

    def send_file(self, *, file_path: str, caption: str = "", as_file: bool = False) -> "SendResult":
        """Send a local file through the adapter associated with this session."""
        return self._run(
            self.adapter.send_file(
                self.chat_id,
                file_path=file_path,
                caption=caption,
                force_file_attachment=as_file,
            ),
            timeout=600,
            label="file",
        )

    def _run(self, coro, *, timeout: float, label: str) -> "SendResult":
        """Bridge synchronous tool calls onto the adapter's asyncio event loop."""
        if self.loop.is_closed():
            coro.close()
            return SendResult(success=False, error="Weixin event loop is closed")
        try:
            future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        except RuntimeError as exc:
            coro.close()
            return SendResult(success=False, error=f"Weixin event loop is unavailable: {exc}")
        try:
            return future.result(timeout=timeout)
        except FutureTimeoutError:
            future.cancel()
            return SendResult(success=False, error=f"Weixin {label} send timed out")
        except Exception as exc:
            return SendResult(success=False, error=f"Weixin {label} send failed: {exc}")


_targets: dict[str, WeixinOutboundTarget] = {}


def register_weixin_outbound_target(
    *,
    session_id: str,
    adapter: "WeixinAdapter",
    chat_id: str,
    loop: asyncio.AbstractEventLoop,
) -> None:
    """Associate an agent session with the Weixin chat currently handling it."""
    if session_id:
        _targets[session_id] = WeixinOutboundTarget(adapter=adapter, chat_id=chat_id, loop=loop)


def get_weixin_outbound_target(session_id: str) -> WeixinOutboundTarget | None:
    """Return the outbound target for a session, if the channel registered one."""
    return _targets.get(session_id)


def unregister_weixin_outbound_targets_for_adapter(adapter: "WeixinAdapter") -> None:
    """Remove all session targets owned by an adapter during channel shutdown."""
    stale = [session_id for session_id, target in _targets.items() if target.adapter is adapter]
    for session_id in stale:
        _targets.pop(session_id, None)
