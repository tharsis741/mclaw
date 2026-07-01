# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Function-call tools for Weixin outbound channel sessions.

The tools only operate when the active agent session has a channel outbound
target bound by the Weixin runtime; direct CLI or non-Weixin calls fail closed.
"""

from __future__ import annotations

import json
from pathlib import Path

from mclaw.channels.weixin.outbound_registry import get_weixin_outbound_target
from mclaw.tools.registry import registry, tool_error


def weixin_send_file(file_path: str, caption: str = "", as_file: bool = False, parent_agent=None) -> str:
    """Send a local file through the Weixin target bound to the current session."""
    if parent_agent is None:
        return tool_error("weixin_send_file requires an active Weixin agent session", success=False)
    session_id = str(getattr(parent_agent, "session_id", "") or "")
    target = get_weixin_outbound_target(session_id)
    if target is None:
        return tool_error("No active Weixin outbound target is bound to this session", success=False)

    path = Path(str(file_path or "")).expanduser()
    if not path.is_file():
        return tool_error(f"File not found: {file_path}", success=False)

    result = target.send_file(file_path=str(path), caption=caption or "", as_file=bool(as_file))
    if not result.success:
        return tool_error(result.error or "Weixin file send failed", success=False)
    return json.dumps(
        {
            "success": True,
            "message_id": result.message_id,
            "path": str(path),
            "caption_sent": bool(caption),
        },
        ensure_ascii=False,
    )


WEIXIN_SEND_FILE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "weixin_send_file",
        "description": (
            "Send a local file to the current Weixin private chat as an attachment. "
            "Use this when the user asks you to send, deliver, or attach a file in Weixin."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Absolute or relative local file path to send.",
                },
                "caption": {
                    "type": "string",
                    "description": "Optional text caption to send before the attachment.",
                    "default": "",
                },
                "as_file": {
                    "type": "boolean",
                    "description": "When true, force images/audio/video to be sent as generic file attachments.",
                    "default": False,
                },
            },
            "required": ["file_path"],
        },
    },
}


def _handle_weixin_send_file(args: dict, **kw) -> str:
    """Registry adapter that keeps tool arguments isolated from runtime kwargs."""
    return weixin_send_file(
        file_path=args.get("file_path", ""),
        caption=args.get("caption", ""),
        as_file=bool(args.get("as_file", False)),
        parent_agent=kw.get("parent_agent"),
    )


registry.register(
    name="weixin_send_file",
    toolset="weixin",
    schema=WEIXIN_SEND_FILE_SCHEMA,
    handler=_handle_weixin_send_file,
    description="Send a file to the current Weixin private chat",
    emoji="📎",
    max_result_size_chars=2000,
)
