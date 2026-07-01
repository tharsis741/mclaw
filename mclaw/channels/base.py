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
