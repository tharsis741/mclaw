# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Short-lived inbound message deduplication for channel runtimes."""

from __future__ import annotations

import hashlib
import threading
import time


class MessageDeduplicator:
    """Thread-safe TTL cache for suppressing repeated inbound channel events."""

    def __init__(self, ttl_seconds: float = 300.0) -> None:
        self.ttl_seconds = ttl_seconds
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def is_duplicate(self, key: str) -> bool:
        """Return true if a key has already been observed inside the TTL window."""
        if not key:
            return False
        now = time.time()
        with self._lock:
            self._prune(now)
            if key in self._seen:
                return True
            self._seen[key] = now
            return False

    def content_key(self, sender_id: str, text: str) -> str:
        """Build a stable key for platforms that lack reliable message ids."""
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return f"content:{sender_id}:{digest}"

    def clear(self) -> None:
        """Clear all remembered inbound message keys."""
        with self._lock:
            self._seen.clear()

    def _prune(self, now: float) -> None:
        """Remove expired keys while the caller holds the lock."""
        cutoff = now - self.ttl_seconds
        stale = [key for key, ts in self._seen.items() if ts < cutoff]
        for key in stale:
            self._seen.pop(key, None)
