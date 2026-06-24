# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Risk policy for file-safety mutation intents."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List


@dataclass
class RiskDecision:
    level: str
    action: str
    reason: str

    @property
    def allowed(self) -> bool:
        return self.action in {"allow", "warn", "checkpoint"}


class RiskPolicyEngine:
    """Classify file mutations and decide whether they can proceed."""

    def __init__(self, config: Dict = None):
        self.config = config or {}
        safety = self.config.get("file_safety", {}) if isinstance(self.config, dict) else {}
        self.high_risk_policy = safety.get("high_risk_policy", "require_confirm") if isinstance(safety, dict) else "require_confirm"
        self.external_path_policy = safety.get("external_path_policy", "warn") if isinstance(safety, dict) else "warn"

    def decide(
        self,
        *,
        action: str,
        target_paths: Iterable[str],
        workspace: str,
        raw_command: str = "",
    ) -> RiskDecision:
        targets = [str(p) for p in target_paths or [] if p]
        if self._is_blocked_root(targets):
            return RiskDecision("blocked", "block", "target includes home/root/system directory")
        if self._has_wildcard_or_recursive_delete(action, raw_command):
            return RiskDecision("high", self._high_risk_action(), "recursive or wildcard delete")
        if not targets:
            return RiskDecision("high", self._high_risk_action(), "destructive command with unresolved targets")
        if self._outside_workspace(targets, workspace):
            return RiskDecision("high", self.external_path_policy, "target outside workspace")
        if action in {"directory_delete", "move", "copy"} or len(targets) > 1:
            return RiskDecision("medium", "allow", "multi-file or directory mutation")
        return RiskDecision("normal", "allow", "single-target mutation")

    def _high_risk_action(self) -> str:
        return "block" if self.high_risk_policy == "block" else "require_confirm"

    @staticmethod
    def _outside_workspace(targets: List[str], workspace: str) -> bool:
        try:
            root = Path(workspace).expanduser().resolve()
        except OSError:
            return True
        for target in targets:
            try:
                path = Path(target).expanduser().resolve()
                try:
                    path.relative_to(root)
                except ValueError:
                    return True
            except OSError:
                return True
        return False

    @staticmethod
    def _is_blocked_root(targets: List[str]) -> bool:
        home = Path.home().resolve()
        roots = {str(home).lower()}
        if os.name == "nt":
            roots.update({
                "c:\\",
                "c:\\windows",
                "c:\\program files",
                "c:\\program files (x86)",
            })
        else:
            roots.update({"/", "/etc", "/usr", "/bin", "/sbin"})
        for target in targets:
            try:
                value = str(Path(target).expanduser().resolve()).rstrip("\\/").lower()
            except OSError:
                value = str(target).rstrip("\\/").lower()
            if value in {r.rstrip("\\/") for r in roots}:
                return True
        return False

    @staticmethod
    def _has_wildcard_or_recursive_delete(action: str, raw_command: str) -> bool:
        if action not in {"delete", "directory_delete", "unknown_destructive"}:
            return False
        command = raw_command or ""
        return bool(re.search(r'(\*|\?|-r\b|-rf\b|/s\b|-recurse\b)', command, re.IGNORECASE))
