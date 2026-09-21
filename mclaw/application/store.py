# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Additive, transactional storage for application control state.

Conversation context remains in SessionDB. This sidecar never rewrites its
schema, so older releases can still open existing conversations on rollback.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import Any


def backup_history(home: Path) -> Path | None:
    """Take one consistent SQLite backup before the application first opens it."""
    source = home / "state.db"
    target = home / "backups" / "before-web-v1.state.db"
    if not source.is_file() or target.exists():
        return target if target.exists() else None
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_suffix(".pending")
    with closing(sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)) as original:
        with closing(sqlite3.connect(staging)) as backup:
            original.backup(backup)
            if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("历史数据库备份校验失败")
    staging.replace(target)
    return target


class ApplicationStore:
    """Persist session snapshots and submission identities in one transaction."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version > 1:
            self._db.close()
            raise RuntimeError("应用数据来自更新版本，请使用匹配版本打开")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS app_sessions (
                id TEXT PRIMARY KEY, document TEXT NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            );
            PRAGMA user_version=1;
        """)
        self._db.commit()

    def get(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT document FROM app_sessions WHERE id=?", (session_id,)
            ).fetchone()
            return json.loads(row[0]) if row else None

    def save(self, session: dict[str, Any]) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO app_sessions VALUES (?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET document=excluded.document, "
                "updated_at=excluded.updated_at",
                (session["id"], json.dumps(session, ensure_ascii=False), session["updated_at"]),
            )

    def all(self) -> list[dict[str, Any]]:
        with self._lock:
            return [json.loads(row[0]) for row in self._db.execute(
                "SELECT document FROM app_sessions ORDER BY updated_at DESC"
            )]

    def delete(self, session_id: str) -> None:
        """Remove the application snapshot after its history and worker close."""
        with self._lock, self._db:
            self._db.execute("DELETE FROM app_sessions WHERE id=?", (session_id,))
            self._db.execute("DELETE FROM app_settings WHERE key=?", (f"channel_title:{session_id}",))
            self._db.execute("INSERT OR REPLACE INTO app_settings VALUES (?, ?)",
                             (f"deleted_session:{session_id}", "true"))
            for key, value in self._db.execute("SELECT key, value FROM app_settings WHERE key LIKE 'channel_pending:%'").fetchall():
                receipts = {task_id: record for task_id, record in json.loads(value).items()
                            if record.get("session_id") != session_id}
                self._db.execute("UPDATE app_settings SET value=? WHERE key=?",
                                 (json.dumps(receipts, ensure_ascii=False), key))

    def setting(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._db.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def set_setting(self, key: str, value: Any) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO app_settings VALUES (?, ?)",
                (key, json.dumps(value, ensure_ascii=False)),
            )

    def channel_title(self, source: str, session_id: str, occupied: set[str]) -> str:
        """Reserve a durable, per-channel sequence without reusing deleted numbers."""
        prefix = {"weixin": "微信消息", "dingtalk": "钉钉消息"}[source]
        counter_key, title_key = f"channel_sequence:{source}", f"channel_title:{session_id}"
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            title = self.setting(title_key)
            if title and title not in occupied:
                return title
            number = int(self.setting(counter_key, 0)) + 1
            while f"{prefix}{number}" in occupied:
                number += 1
            title = f"{prefix}{number}"
            for key, value in ((counter_key, number), (title_key, title)):
                self._db.execute("INSERT OR REPLACE INTO app_settings VALUES (?, ?)",
                                 (key, json.dumps(value, ensure_ascii=False)))
            return title

    def recover(self) -> None:
        """Never replay work automatically after losing the service process."""
        for session in self.all():
            if session.get("active_task") or session.get("queue"):
                session["status"] = "interrupted"
                session["error"] = "服务已重启。请先核对已知执行结果，再继续处理原任务。"
                session["paused"] = True
                task = session.get("active_task")
                if task:
                    task["status"] = "interrupted"
                    task["recovery_required"] = True
                session["seq"] += 1
                self.save(session)

    def close(self) -> None:
        with self._lock:
            self._db.close()
