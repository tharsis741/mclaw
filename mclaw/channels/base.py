# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Common channel data structures.

Channel runtimes normalize platform-specific messages into these small
objects before handing them to the shared agent runner.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class ChannelMessageType(str, Enum):
    """Normalized message kinds understood by the shared channel runner."""

    TEXT = "text"


class AttachmentKind(str, Enum):
    """Platform-neutral kinds for inbound channel attachments."""

    UNKNOWN = "unknown"
    IMAGE = "image"
    VOICE = "voice"
    AUDIO = "audio"
    VIDEO = "video"
    FILE = "file"


class AttachmentOrigin(str, Enum):
    """How an inbound attachment entered the channel message."""

    UNKNOWN = "unknown"
    VOICE_MESSAGE = "voice_message"
    FILE_UPLOAD = "file_upload"
    WEIXIN = "weixin"
    DINGTALK = "dingtalk"
    DSOFTBUS = "dsoftbus"


@dataclass(frozen=True)
class ChannelAttachment:
    """Normalized metadata for one cached inbound attachment.

    ``metadata`` is reserved for non-secret platform details that do not fit the
    shared model.  Download credentials, signed URLs, and other bearer material
    must not be copied into it.
    """

    kind: AttachmentKind = AttachmentKind.UNKNOWN
    origin: AttachmentOrigin = AttachmentOrigin.UNKNOWN
    path: str = ""
    filename: str = ""
    mime_type: str = "application/octet-stream"
    size_bytes: int = 0
    duration_ms: int = 0
    codec: str = ""
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize the attachment without exposing enum implementation details."""
        return {
            "kind": self.kind.value if isinstance(self.kind, AttachmentKind) else str(self.kind),
            "origin": self.origin.value if isinstance(self.origin, AttachmentOrigin) else str(self.origin),
            "path": self.path,
            "filename": self.filename,
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
            "duration_ms": self.duration_ms,
            "codec": self.codec,
            "error": self.error,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class ChannelSource:
    """Platform-neutral identity for an inbound channel message."""

    channel: str
    chat_id: str
    chat_type: str = "dm"
    user_id: str = ""
    user_name: str = ""
    message_id: str = ""
    account_id: str = ""


@dataclass(frozen=True)
class ChannelMessage:
    """Inbound message payload after platform adapters normalize metadata."""

    text: str
    source: ChannelSource
    message_type: ChannelMessageType = ChannelMessageType.TEXT
    raw_message: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)
    attachments: tuple[ChannelAttachment, ...] = ()
    capability_results: dict[str, Any] = field(default_factory=dict)


@dataclass
class SendResult:
    """Outbound send result returned by channel-specific targets."""

    success: bool
    message_id: str | None = None
    error: str | None = None


@dataclass
class AgentTurnResult:
    """Result of one channel-routed agent turn."""

    session_id: str
    final_response: str = ""
    queued: bool = False
    interrupted: bool = False
    error: str | None = None
    raw_result: dict[str, Any] = field(default_factory=dict)
