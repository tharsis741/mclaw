# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Deduplicate inbound Weixin messages before agent dispatch.

The cache avoids duplicate turns when the platform repeats the same message and
keeps channel-specific identity handling outside the shared runner.
"""

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
