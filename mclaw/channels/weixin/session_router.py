# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Map Weixin peers to M-Claw sessions.

The router keeps stable peer-to-session bindings so private chat messages can
resume the correct agent conversation across channel events.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass

from mclaw.channels.base import ChannelSource
from mclaw.state import SessionDB


def _hash(value: str, length: int = 24) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


@dataclass(frozen=True)
class RoutedSession:
    """Resolved session binding for a Weixin peer event."""

    session_key: str
    session_id: str


class WeixinSessionRouter:
    """Map peers to stable ids, with process-local new-session overrides."""

    def __init__(self, *, account_id: str, session_db: SessionDB, scope: str = "user") -> None:
        self.account_id = account_id
        self.session_db = session_db
        self.scope = scope if scope in {"chat", "user", "chat_user"} else "user"
        self._overrides: dict[str, str] = {}

    def route(self, source: ChannelSource, *, model: str = "") -> RoutedSession:
        """Return the existing or deterministic session for an inbound source."""
        session_key = self.session_key(source)
        session_id = self._overrides.get(session_key)
        if not session_id:
            session_id = f"weixin_{_hash(session_key)}"
        self.session_db.create_session(
            session_id,
            source="weixin",
            model=model,
            user_id=source.user_id or None,
        )
        return RoutedSession(session_key=session_key, session_id=session_id)

    def new_session(self, source: ChannelSource, *, model: str = "") -> RoutedSession:
        """Create a fresh session override for the same Weixin peer binding."""
        session_key = self.session_key(source)
        session_id = f"weixin_{_hash(session_key + ':' + uuid.uuid4().hex)}"
        self._overrides[session_key] = session_id
        self.session_db.create_session(
            session_id,
            source="weixin",
            model=model,
            user_id=source.user_id or None,
        )
        return RoutedSession(session_key=session_key, session_id=session_id)

    def session_key(self, source: ChannelSource) -> str:
        """Build a privacy-preserving key from the configured Weixin routing scope."""
        account = source.account_id or self.account_id
        if self.scope == "chat":
            basis = f"chat:{source.chat_id}"
        elif self.scope == "chat_user":
            basis = f"chat:{source.chat_id}:user:{source.user_id}"
        else:
            basis = f"user:{source.user_id or source.chat_id}"
        return f"weixin:{_hash(account, 12)}:{basis}"
