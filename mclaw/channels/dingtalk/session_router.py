# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Map DingTalk conversations to M-Claw sessions."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass

from mclaw.channels.base import ChannelSource
from mclaw.state import SessionDB


def _hash(value: str, length: int = 24) -> str:
    """Create stable compact ids without exposing chat or account identifiers."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


@dataclass(frozen=True)
class RoutedSession:
    """Resolved M-Claw session for one DingTalk conversation key."""

    session_key: str
    session_id: str


class DingTalkSessionRouter:
    """Map chat/user scope to stable ids, with process-local new-session overrides."""

    def __init__(self, *, account_id: str, session_db: SessionDB, scope: str = "chat_user") -> None:
        self.account_id = account_id
        self.session_db = session_db
        self.scope = scope if scope in {"chat", "user", "chat_user"} else "chat_user"
        self._overrides: dict[str, str] = {}

    def route(self, source: ChannelSource, *, model: str = "") -> RoutedSession:
        """Return the active session for a source, creating DB metadata if needed."""
        session_key = self.session_key(source)
        session_id = self._overrides.get(session_key)
        if not session_id:
            session_id = f"dingtalk_{_hash(session_key)}"
        self.session_db.create_session(
            session_id,
            source="dingtalk",
            model=model,
            user_id=source.user_id or None,
        )
        return RoutedSession(session_key=session_key, session_id=session_id)

    def new_session(self, source: ChannelSource, *, model: str = "") -> RoutedSession:
        """Force a fresh session for the same DingTalk routing key."""
        session_key = self.session_key(source)
        session_id = f"dingtalk_{_hash(session_key + ':' + uuid.uuid4().hex)}"
        self._overrides[session_key] = session_id
        self.session_db.create_session(
            session_id,
            source="dingtalk",
            model=model,
            user_id=source.user_id or None,
        )
        return RoutedSession(session_key=session_key, session_id=session_id)

    def session_key(self, source: ChannelSource) -> str:
        """Build the configured chat/user scoping key for a DingTalk source."""
        account = source.account_id or self.account_id
        if self.scope == "chat":
            basis = f"chat:{source.chat_id}"
        elif self.scope == "user":
            basis = f"user:{source.user_id or source.chat_id}"
        else:
            basis = f"chat:{source.chat_id}:user:{source.user_id}"
        return f"dingtalk:{_hash(account, 12)}:{basis}"
