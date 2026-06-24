# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Runtime registry for Weixin outbound tool calls."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mclaw.channels.weixin.adapter import WeixinAdapter
    from mclaw.channels.base import SendResult


@dataclass
class WeixinOutboundTarget:
    adapter: "WeixinAdapter"
    chat_id: str
    loop: asyncio.AbstractEventLoop

    def send_file(self, *, file_path: str, caption: str = "", as_file: bool = False) -> "SendResult":
        future = asyncio.run_coroutine_threadsafe(
            self.adapter.send_file(
                self.chat_id,
                file_path=file_path,
                caption=caption,
                force_file_attachment=as_file,
            ),
            self.loop,
        )
        return future.result(timeout=600)


_targets: dict[str, WeixinOutboundTarget] = {}


def register_weixin_outbound_target(
    *,
    session_id: str,
    adapter: "WeixinAdapter",
    chat_id: str,
    loop: asyncio.AbstractEventLoop,
) -> None:
    if session_id:
        _targets[session_id] = WeixinOutboundTarget(adapter=adapter, chat_id=chat_id, loop=loop)


def get_weixin_outbound_target(session_id: str) -> WeixinOutboundTarget | None:
    return _targets.get(session_id)
