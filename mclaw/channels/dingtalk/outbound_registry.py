# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Runtime registry for DingTalk outbound tool calls."""

from __future__ import annotations

import asyncio
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mclaw.channels.base import SendResult

if TYPE_CHECKING:
    from mclaw.channels.dingtalk.adapter import DingTalkAdapter


@dataclass
class DingTalkOutboundTarget:
    """Session-bound bridge from function-call tools to the DingTalk event loop."""

    adapter: "DingTalkAdapter"
    chat_id: str
    loop: asyncio.AbstractEventLoop

    def send_file(self, *, file_path: str, caption: str = "") -> "SendResult":
        """Schedule a file send with a longer timeout for upload work."""
        return self._run(
            self.adapter.send_file(self.chat_id, file_path=file_path, caption=caption),
            timeout=600,
            label="file",
        )

    def _run(self, coro, *, timeout: float, label: str) -> "SendResult":
        """Run an async DingTalk send from synchronous tool execution."""
        if self.loop.is_closed():
            coro.close()
            return SendResult(success=False, error="DingTalk event loop is closed")
        try:
            future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        except RuntimeError as exc:
            coro.close()
            return SendResult(success=False, error=f"DingTalk event loop is unavailable: {exc}")
        try:
            return future.result(timeout=timeout)
        except FutureTimeoutError:
            future.cancel()
            return SendResult(success=False, error=f"DingTalk {label} send timed out")
        except Exception as exc:
            return SendResult(success=False, error=f"DingTalk {label} send failed: {exc}")


_targets: dict[str, DingTalkOutboundTarget] = {}


def register_dingtalk_outbound_target(
    *,
    session_id: str,
    adapter: "DingTalkAdapter",
    chat_id: str,
    loop: asyncio.AbstractEventLoop,
) -> None:
    """Bind a channel session to the outbound adapter target for tool calls."""
    if session_id:
        _targets[session_id] = DingTalkOutboundTarget(adapter=adapter, chat_id=chat_id, loop=loop)


def get_dingtalk_outbound_target(session_id: str) -> DingTalkOutboundTarget | None:
    """Return the outbound target for a currently active DingTalk session."""
    return _targets.get(session_id)


def unregister_dingtalk_outbound_targets_for_adapter(adapter: "DingTalkAdapter") -> None:
    """Remove stale target bindings when an adapter shuts down."""
    stale = [session_id for session_id, target in _targets.items() if target.adapter is adapter]
    for session_id in stale:
        _targets.pop(session_id, None)
