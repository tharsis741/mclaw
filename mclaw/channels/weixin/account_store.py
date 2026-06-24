"""Disk-backed Weixin account runtime state."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from mclaw.constants import get_mclaw_home


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
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
        except OSError:
            pass


class WeixinAccountStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or (get_mclaw_home() / "weixin")
        self.accounts_dir = self.root / "accounts"
        self.accounts_dir.mkdir(parents=True, exist_ok=True)

    def account_path(self, account_id: str) -> Path:
        return self.accounts_dir / f"{account_id}.json"

    def sync_path(self, account_id: str) -> Path:
        return self.accounts_dir / f"{account_id}.sync.json"

    def load_account(self, account_id: str) -> dict[str, Any] | None:
        path = self.account_path(account_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def save_account(self, account_id: str, payload: dict[str, Any]) -> None:
        _write_json_atomic(self.account_path(account_id), payload)

    def load_sync_buf(self, account_id: str) -> str:
        path = self.sync_path(account_id)
        if not path.exists():
            return ""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return str(data.get("sync_buf") or "")
        except Exception:
            return ""

    def save_sync_buf(self, account_id: str, sync_buf: str) -> None:
        _write_json_atomic(self.sync_path(account_id), {"sync_buf": sync_buf})

