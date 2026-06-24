"""Weixin inbound message deduplication."""

from __future__ import annotations

import hashlib
import time


class MessageDeduplicator:
    def __init__(self, ttl_seconds: float = 300.0) -> None:
        self.ttl_seconds = ttl_seconds
        self._seen: dict[str, float] = {}

    def is_duplicate(self, key: str) -> bool:
        if not key:
            return False
        now = time.time()
        self._prune(now)
        if key in self._seen:
            return True
        self._seen[key] = now
        return False

    def content_key(self, sender_id: str, text: str) -> str:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return f"content:{sender_id}:{digest}"

    def _prune(self, now: float) -> None:
        cutoff = now - self.ttl_seconds
        stale = [key for key, ts in self._seen.items() if ts < cutoff]
        for key in stale:
            self._seen.pop(key, None)

