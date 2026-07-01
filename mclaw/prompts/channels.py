# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Channel-specific model context prompt builders.

These helpers add runtime channel constraints without exposing raw platform
identifiers or duplicating the main system prompt.
"""

from __future__ import annotations

import hashlib
from typing import Literal


def build_channel_context(
    channel: Literal["weixin", "dingtalk"],
    *,
    user_id: str = "",
    user_name: str = "",
    chat_type: str = "",
) -> str:
    """Build extra system context for a channel turn."""
    if channel == "weixin":
        return build_weixin_channel_context(user_id=user_id)
    if channel == "dingtalk":
        return build_dingtalk_channel_context(
            chat_type=chat_type,
            user_name=user_name,
            user_id=user_id,
        )
    raise ValueError(f"Unsupported channel: {channel}")


def build_weixin_channel_context(*, user_id: str = "") -> str:
    """Build Weixin-private-chat context with a hashed user label."""
    safe_user = hashlib.sha256((user_id or "").encode("utf-8")).hexdigest()[:12]
    return (
        "## 当前渠道\n"
        "- 渠道：微信私聊\n"
        f"- 用户：user_{safe_user}\n"
        "- 用户要求发送本地文件时，使用 weixin_send_file。\n"
    )


def build_dingtalk_channel_context(
    *,
    chat_type: str = "",
    user_name: str = "",
    user_id: str = "",
) -> str:
    """Build DingTalk context that nudges the model toward channel send tools."""
    display_chat_type = "private chat" if chat_type == "dm" else chat_type
    return (
        "## 当前渠道\n"
        f"- 渠道：钉钉 {display_chat_type}\n"
        f"- channel_key: dingtalk {display_chat_type}\n"
        f"- 用户：{user_name or user_id}\n"
        "- 回复前结合钉钉附件信息处理图片、视频、语音或文件。\n"
        "- 用户要求发送钉钉消息或文件时，使用 dingtalk_send_text 或 dingtalk_send_file。\n"
    )
