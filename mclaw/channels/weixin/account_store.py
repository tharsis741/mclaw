# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Persist Weixin account runtime state outside the project workspace.

The channel stores login credentials and the iLink sync buffer separately so a
gateway restart can resume polling without putting secrets in project files.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from mclaw.constants import get_mclaw_home

logger = logging.getLogger(__name__)


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON through a same-directory temp file to avoid torn state files."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(tmp_name, path)
    finally:
        try:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        except OSError as exc:
            logger.debug("Weixin account store temp cleanup failed for %s: %s", tmp_name, exc)


class WeixinAccountStore:
    """Disk-backed account credential and sync-buffer store for Weixin."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or (get_mclaw_home() / "weixin")
        self.accounts_dir = self.root / "accounts"
        self.accounts_dir.mkdir(parents=True, exist_ok=True)

    def account_path(self, account_id: str) -> Path:
        """Return the credential state path for one iLink account."""
        return self.accounts_dir / f"{account_id}.json"

    def sync_path(self, account_id: str) -> Path:
        """Return the long-poll sync buffer path for one iLink account."""
        return self.accounts_dir / f"{account_id}.sync.json"

    def load_account(self, account_id: str) -> dict[str, Any] | None:
        """Load saved credentials, treating missing or invalid state as absent."""
        path = self.account_path(account_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except (OSError, json.JSONDecodeError) as exc:
            logger.debug("Weixin account state load failed for %s: %s", account_id, exc)
            return None

    def save_account(self, account_id: str, payload: dict[str, Any]) -> None:
        """Persist credentials or login metadata for an account atomically."""
        _write_json_atomic(self.account_path(account_id), payload)

    def load_sync_buf(self, account_id: str) -> str:
        """Load the iLink polling cursor for an account."""
        path = self.sync_path(account_id)
        if not path.exists():
            return ""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return str(data.get("sync_buf") or "")
        except (OSError, json.JSONDecodeError) as exc:
            logger.debug("Weixin sync buffer load failed for %s: %s", account_id, exc)
            return ""

    def save_sync_buf(self, account_id: str, sync_buf: str) -> None:
        """Persist the latest iLink polling cursor atomically."""
        _write_json_atomic(self.sync_path(account_id), {"sync_buf": sync_buf})
