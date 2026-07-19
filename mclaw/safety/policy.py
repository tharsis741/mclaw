# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Risk policy for file-safety mutation intents."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from mclaw.safety.mutation_detector import has_recursive_delete_flag


@dataclass
class RiskDecision:
    """Policy result consumed by checkpointing and tool-dispatch preflight."""

    level: str
    action: str
    reason: str

    @property
    def allowed(self) -> bool:
        return self.action in {"allow", "warn", "checkpoint"}


class RiskPolicyEngine:
    """Classify file mutations and decide whether they can proceed."""

    def decide(
        self,
        *,
        action: str,
        target_paths: Iterable[str],
        raw_command: str = "",
    ) -> RiskDecision:
        """Return the safest allowed action for a normalized mutation request."""
        targets = [str(p) for p in target_paths or [] if p]
        if action == "managed_mutation":
            return RiskDecision("managed", "allow", "mutation is handled by its dedicated tool")
        if self._has_wildcard_or_recursive_delete(action, raw_command):
            return RiskDecision("high", "block", "recursive or wildcard delete")
        if not targets:
            return RiskDecision("high", "block", "destructive command with unresolved targets")
        if action in {"directory_delete", "move", "copy"} or len(targets) > 1:
            return RiskDecision("medium", "allow", "multi-file or directory mutation")
        return RiskDecision("normal", "allow", "single-target mutation")

    @staticmethod
    def _has_wildcard_or_recursive_delete(action: str, raw_command: str) -> bool:
        """Identify delete forms that should not proceed under automatic policy."""
        if action not in {"delete", "directory_delete", "unknown_destructive"}:
            return False
        command = raw_command or ""
        return bool(
            re.search(r"\*|\?", command)
            or has_recursive_delete_flag(command)
        )
