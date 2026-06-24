# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Transcript filtering and command gating for voice input."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass


@dataclass
class FilterResult:
    action: str
    text: str = ""
    reason: str = ""


class TranscriptFilter:
    def __init__(self, config: dict):
        self.config = config or {}
        self._last_text = ""
        self._last_at = 0.0

    def process(self, text: str, mode: str | None = None) -> FilterResult:
        raw = (text or "").strip()
        if not raw:
            return FilterResult("discard", reason="empty")

        mode = mode or str(self.config.get("listen_mode") or "wake_word")
        cleaned = self._strip_wake_word(raw, mode)
        if cleaned is None:
            return FilterResult("discard", reason="missing_wake_word")

        if self._is_duplicate(cleaned):
            return FilterResult("discard", reason="duplicate")

        if self._is_interrupt(cleaned):
            return FilterResult("interrupt", text=cleaned, reason="interrupt_phrase")

        if cleaned.startswith("/") and not self.config.get("allow_voice_slash_commands", False):
            return FilterResult("discard", text=cleaned, reason="slash_command_blocked")

        return FilterResult("submit", text=cleaned)

    def _strip_wake_word(self, text: str, mode: str) -> str | None:
        require = bool(self.config.get("require_wake_word"))
        if mode in ("push_to_talk", "once"):
            require = False
        if not require:
            return text

        lower = text.lower()
        for wake in self.config.get("wake_words") or []:
            wake = str(wake).strip()
            if not wake:
                continue
            idx = lower.find(wake.lower())
            if idx >= 0:
                rest = text[idx + len(wake):]
                rest = re.sub(r"^[\s,，。:：;；!！]+", "", rest).strip()
                return rest or text.strip()
        return None

    def _is_duplicate(self, text: str) -> bool:
        now = time.time()
        window = float(self.config.get("dedupe_seconds") or 0)
        if window > 0 and text == self._last_text and (now - self._last_at) <= window:
            return True
        self._last_text = text
        self._last_at = now
        return False

    @staticmethod
    def _is_interrupt(text: str) -> bool:
        normalized = re.sub(r"[\s,，。.!！?？:：;；]+", "", text.lower())
        return normalized in {
            "停止",
            "取消",
            "别动",
            "停",
            "stop",
            "cancel",
            "abort",
            "interrupt",
        }

