# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Disk-backed Weixin context-token cache.

Context tokens are scoped per peer and reused on outbound calls so replies stay
attached to the correct iLink conversation context across process restarts.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from mclaw.channels.weixin.account_store import _write_json_atomic
from mclaw.constants import get_mclaw_home

logger = logging.getLogger(__name__)


class ContextTokenStore:
    """Cache and persist per-peer Weixin context tokens for one or more accounts."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or (get_mclaw_home() / "weixin" / "accounts")
        self.root.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, str] = {}

    def _path(self, account_id: str) -> Path:
        return self.root / f"{account_id}.context-tokens.json"

    def _key(self, account_id: str, peer_id: str) -> str:
        return f"{account_id}:{peer_id}"

    def restore(self, account_id: str) -> None:
        """Load persisted peer tokens into the in-memory cache for an account."""
        path = self._path(account_id)
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.debug("Weixin context_token cache load failed for %s: %s", account_id, exc)
            return
        if not isinstance(data, dict):
            return
        for peer_id, token in data.items():
            if isinstance(token, str) and token:
                self._cache[self._key(account_id, str(peer_id))] = token

    def get(self, account_id: str, peer_id: str) -> str | None:
        """Return the cached context token for a peer, if one is known."""
        return self._cache.get(self._key(account_id, peer_id))

    def set(self, account_id: str, peer_id: str, token: str) -> None:
        """Update a peer token and immediately persist the account token map."""
        if not token:
            return
        self._cache[self._key(account_id, peer_id)] = token
        self.persist(account_id)

    def clear(self, account_id: str, peer_id: str) -> None:
        """Remove a peer token after iLink rejects or invalidates it."""
        self._cache.pop(self._key(account_id, peer_id), None)
        self.persist(account_id)

    def persist(self, account_id: str) -> None:
        """Write only the selected account's peer-token map to disk."""
        prefix = f"{account_id}:"
        payload = {
            key[len(prefix):]: value
            for key, value in self._cache.items()
            if key.startswith(prefix)
        }
        _write_json_atomic(self._path(account_id), payload)
