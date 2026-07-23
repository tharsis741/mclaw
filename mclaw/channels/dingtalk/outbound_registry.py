# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Runtime registry for DingTalk outbound tool calls."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mclaw.channels.base import SendResult
from mclaw.channels.outbound_bridge import run_outbound_coroutine

if TYPE_CHECKING:
    from mclaw.channels.dingtalk.adapter import DingTalkAdapter


@dataclass
class DingTalkOutboundTarget:
    """Session-bound bridge from function-call tools to the DingTalk event loop."""

    adapter: "DingTalkAdapter"
    chat_id: str
    loop: asyncio.AbstractEventLoop

    def send_file(
        self,
        *,
        file_path: str,
        caption: str = "",
        cancel_event: threading.Event | None = None,
        parent_agent: Any = None,
    ) -> SendResult:
        """Schedule a file send with a longer timeout for upload work."""
        return run_outbound_coroutine(
            self.adapter.send_file(self.chat_id, file_path=file_path, caption=caption),
            loop=self.loop,
            timeout=600,
            platform="dingtalk",
            display_name="DingTalk",
            label="file",
            cancel_event=cancel_event,
            parent_agent=parent_agent,
        )


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
