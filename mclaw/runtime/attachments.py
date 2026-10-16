# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Attachment instructions shared by new messages and history projection."""

from __future__ import annotations

import json

_HEADER = "\n\n[Attached files]\n"
_LEGACY_GUIDANCE = (
    "The user attached these local files. Read the relevant files with available tools before using their contents. "
    "Uploaded files are copies; local references retain their original paths.\n"
)
_GUIDANCE = (
    "Use image content, image analysis, and other file contents already supplied in this conversation directly. "
    "Read files with tools when the requested information is not already available.\n"
)


def attachment_context(attachments: list[dict]) -> str:
    """Keep supplied content usable directly while retaining paths for further reads."""
    if not attachments:
        return ""
    selected = [{key: item[key] for key in ("name", "path", "source", "mime_type") if key in item}
                for item in attachments]
    return _HEADER + _GUIDANCE + json.dumps(selected, ensure_ascii=False)


def _split_context(text: str) -> tuple[str, list[dict]] | None:
    prefix, separator, trailer = text.rpartition(_HEADER)
    if not separator or not trailer.startswith((_GUIDANCE, _LEGACY_GUIDANCE)):
        return None
    guidance = _GUIDANCE if trailer.startswith(_GUIDANCE) else _LEGACY_GUIDANCE
    try:
        attachments = json.loads(trailer[len(guidance):])
    except (ValueError, TypeError):
        return None
    if not isinstance(attachments, list) or not attachments or not all(
        isinstance(item, dict) and all(isinstance(item.get(key), str) for key in ("name", "path", "source"))
        for item in attachments
    ):
        return None
    return prefix, attachments


def refresh_attachment_context(text: str) -> str:
    """Update only a recognized generated trailer, without rewriting saved history."""
    parsed = _split_context(text)
    return parsed[0] + attachment_context(parsed[1]) if parsed else text


def image_question(text: str) -> str:
    """Avoid repeating the attachment manifest in every image's question."""
    parsed = _split_context(text)
    return parsed[0] if parsed else text
