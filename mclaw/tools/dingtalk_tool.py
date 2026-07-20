# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Function-call tool for DingTalk outbound channel sessions.

The tool sends files through the DingTalk target bound to the active agent
session. It intentionally fails closed when no channel target is bound.
"""

from __future__ import annotations

import json
from pathlib import Path

from mclaw.channels.dingtalk.outbound_registry import get_dingtalk_outbound_target
from mclaw.tools.registry import registry, tool_error


def dingtalk_send_file(file_path: str, caption: str = "", parent_agent=None) -> str:
    """Send a local file through the current DingTalk outbound target."""
    if parent_agent is None:
        return tool_error("dingtalk_send_file requires an active DingTalk agent session", success=False)
    session_id = str(getattr(parent_agent, "session_id", "") or "")
    target = get_dingtalk_outbound_target(session_id)
    if target is None:
        return tool_error("No active DingTalk outbound target is bound to this session", success=False)
    path = Path(str(file_path or "")).expanduser()
    if not path.is_file():
        return tool_error(f"File not found: {file_path}", success=False)
    try:
        result = target.send_file(file_path=str(path), caption=caption or "")
    except Exception as exc:
        return tool_error(f"DingTalk file send failed: {exc}", success=False)
    if not result.success:
        return tool_error(result.error or "DingTalk file send failed", success=False)
    return json.dumps(
        {
            "success": True,
            "message_id": result.message_id,
            "path": str(path),
            "caption_sent": bool(caption),
        },
        ensure_ascii=False,
    )


DINGTALK_SEND_FILE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "dingtalk_send_file",
        "description": (
            "Send a local file to the current DingTalk conversation. Text files are sent as segmented Markdown; "
            "audio/video/generic attachments use DingTalk OpenAPI media upload. Unsupported generic file types are "
            "zipped before sending."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Absolute or relative local file path to send."},
                "caption": {"type": "string", "description": "Optional caption.", "default": ""},
            },
            "required": ["file_path"],
        },
    },
}


def _handle_dingtalk_send_file(args: dict, **kw) -> str:
    """Registry adapter for DingTalk file sends."""
    return dingtalk_send_file(
        file_path=args.get("file_path", ""),
        caption=args.get("caption", ""),
        parent_agent=kw.get("parent_agent"),
    )


registry.register(
    name="dingtalk_send_file",
    toolset="dingtalk",
    schema=DINGTALK_SEND_FILE_SCHEMA,
    handler=_handle_dingtalk_send_file,
    description="Send a file to the current DingTalk conversation",
    emoji="📎",
    max_result_size_chars=2000,
)
