# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""
SQLite State Store for M-Claw.

Provides persistent session storage with FTS5 full-text search.
Stores session metadata, full message history, and model configuration.

Key design decisions:
- WAL mode for concurrent readers + one writer
- FTS5 virtual table for fast text search across all session messages
- Batch writes with jitter retry to avoid convoy effects
"""

import json
import logging
import os
import random
import re
import sqlite3
import threading
import time
from pathlib import Path
from mclaw.constants import get_mclaw_home
from typing import Any, Callable, Dict, List, Optional, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

DEFAULT_DB_PATH = get_mclaw_home() / "state.db"


def _normalize_workspace_path(path: str | os.PathLike[str] | None) -> str | None:
    """Return a stable display path for workspace-scoped session lookups."""
    if path is None:
        return None
    raw = str(path).strip()
    if not raw:
        return None
    workspace = Path(raw).expanduser()
    try:
        workspace = workspace.resolve()
    except OSError:
        workspace = workspace.absolute()
    return os.path.normpath(str(workspace))


def _workspace_key(path: str | os.PathLike[str] | None) -> str | None:
    """Normalize workspace identity using host-specific path comparison rules."""
    normalized = _normalize_workspace_path(path)
    if not normalized:
        return None
    if os.name == "nt":
        return os.path.normcase(normalized)
    return normalized


SCHEMA_VERSION = 9

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    user_id TEXT,
    model TEXT,
    model_config TEXT,
    system_prompt TEXT,
    parent_session_id TEXT,
    workspace TEXT,
    workspace_key TEXT,
    started_at REAL NOT NULL,
    ended_at REAL,
    end_reason TEXT,
    message_count INTEGER DEFAULT 0,
    tool_call_count INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0,
    reasoning_tokens INTEGER DEFAULT 0,
    billing_provider TEXT,
    billing_base_url TEXT,
    billing_mode TEXT,
    estimated_cost_usd REAL,
    actual_cost_usd REAL,
    cost_status TEXT,
    cost_source TEXT,
    pricing_version TEXT,
    title TEXT,
    FOREIGN KEY (parent_session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL,
    content TEXT,
    tool_call_id TEXT,
    tool_calls TEXT,
    tool_name TEXT,
    timestamp REAL NOT NULL,
    token_count INTEGER,
    finish_reason TEXT,
    reasoning TEXT,
    reasoning_details TEXT,
    codex_reasoning_items TEXT,
    turn_id TEXT,
    operation_id TEXT,
    invalidated_at REAL,
    invalidated_by TEXT,
    invalidation_reason TEXT
);

CREATE TABLE IF NOT EXISTS context_rollbacks (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    operation_id TEXT,
    checkpoint_hash TEXT,
    marker_message_id INTEGER,
    invalidated_message_ids TEXT,
    mode TEXT NOT NULL,
    created_at REAL NOT NULL,
    restored_at REAL,
    metadata TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_source ON sessions(source);
CREATE INDEX IF NOT EXISTS idx_sessions_parent ON sessions(parent_session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, timestamp);

CREATE TABLE IF NOT EXISTS scheduler_jobs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    trigger_type TEXT NOT NULL,
    schedule_expr TEXT NOT NULL,
    schedule_parsed_json TEXT NOT NULL DEFAULT '{}',
    timezone TEXT NOT NULL,
    next_run_at REAL,
    last_run_at REAL,
    status TEXT NOT NULL DEFAULT 'idle',
    prompt TEXT NOT NULL,
    enabled_toolsets_json TEXT NOT NULL DEFAULT '[]',
    workdir TEXT,
    session_policy TEXT NOT NULL DEFAULT 'task_thread',
    session_id TEXT,
    delivery_json TEXT NOT NULL DEFAULT '{}',
    max_iterations INTEGER NOT NULL DEFAULT 200,
    timeout_seconds INTEGER NOT NULL DEFAULT 3600,
    concurrency_policy TEXT NOT NULL DEFAULT 'skip',
    failure_count INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_scheduler_jobs_due
ON scheduler_jobs(enabled, next_run_at);

CREATE TABLE IF NOT EXISTS scheduler_runs (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES scheduler_jobs(id),
    run_no INTEGER NOT NULL,
    status TEXT NOT NULL,
    claimed_by TEXT,
    scheduled_for REAL,
    started_at REAL,
    finished_at REAL,
    session_id TEXT,
    output_path TEXT,
    final_response TEXT,
    error TEXT,
    delivery_result_json TEXT NOT NULL DEFAULT '{}',
    token_usage_json TEXT NOT NULL DEFAULT '{}',
    tool_calls_json TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_scheduler_runs_job
ON scheduler_runs(job_id, created_at DESC);

CREATE TABLE IF NOT EXISTS scheduler_targets (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    display_name TEXT NOT NULL,
    account_id TEXT,
    chat_id TEXT,
    chat_type TEXT,
    route_metadata_json TEXT NOT NULL DEFAULT '{}',
    capabilities_json TEXT NOT NULL DEFAULT '{}',
    route_status TEXT NOT NULL DEFAULT 'ready',
    source TEXT NOT NULL DEFAULT 'binding',
    first_seen_at REAL NOT NULL,
    last_seen_at REAL NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    updated_at REAL NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_scheduler_targets_unique_route
ON scheduler_targets(type, account_id, chat_id);

CREATE TABLE IF NOT EXISTS scheduler_target_pairings (
    code TEXT PRIMARY KEY,
    requested_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'waiting',
    display_name_hint TEXT,
    target_id TEXT,
    error TEXT,
    expires_at REAL NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_scheduler_target_pairings_status
ON scheduler_target_pairings(status, expires_at);
"""

FTS_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content,
    content=messages,
    content_rowid=id
);

CREATE TRIGGER IF NOT EXISTS messages_fts_insert AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_delete AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content) VALUES('delete', old.id, old.content);
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_update AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content) VALUES('delete', old.id, old.content);
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;
"""


class SessionDB:
    """SQLite-backed session storage with FTS5 search.

    Thread-safe: multiple reader threads, single writer via WAL mode.
    """

    _WRITE_MAX_RETRIES = 15
    _WRITE_RETRY_MIN_S = 0.020
    _WRITE_RETRY_MAX_S = 0.150
    _CHECKPOINT_EVERY_N_WRITES = 50

    def __init__(self, db_path: Path = None):
        """Open the session database and initialize schema in WAL mode."""
        self.db_path = db_path or DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._lock = threading.Lock()
        self._write_count = 0
        self._conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            timeout=1.0,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def _execute_write(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """Execute a write transaction with BEGIN IMMEDIATE and jitter retry."""
        last_err: Optional[Exception] = None
        do_checkpoint = False
        for attempt in range(self._WRITE_MAX_RETRIES):
            try:
                with self._lock:
                    self._conn.execute("BEGIN IMMEDIATE")
                    try:
                        result = fn(self._conn)
                        self._conn.commit()
                        self._write_count += 1
                        if self._write_count % self._CHECKPOINT_EVERY_N_WRITES == 0:
                            do_checkpoint = True
                        return result
                    except BaseException:
                        try:
                            self._conn.rollback()
                        except Exception:
                            pass
                        raise
            except sqlite3.OperationalError as exc:
                err_msg = str(exc).lower()
                if "locked" in err_msg or "busy" in err_msg:
                    last_err = exc
                    if attempt < self._WRITE_MAX_RETRIES - 1:
                        time.sleep(random.uniform(self._WRITE_RETRY_MIN_S, self._WRITE_RETRY_MAX_S))
                        continue
                raise
            finally:
                if do_checkpoint:
                    self._try_wal_checkpoint()
        raise last_err or sqlite3.OperationalError("database is locked after max retries")

    def _try_wal_checkpoint(self) -> None:
        try:
            with self._lock:
                self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except Exception:
            pass

    def close(self):
        """Close the shared connection without blocking another active operation."""
        acquired = self._lock.acquire(blocking=False)
        if not acquired:
            logger.warning("SessionDB.close() skipped: lock held by another thread")
            return
        try:
            if self._conn:
                try:
                    self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                except Exception:
                    pass
                self._conn.close()
                self._conn = None
        finally:
            self._lock.release()

    def _init_schema(self):
        """Create or upgrade the durable schema used by sessions and schedulers."""
        cursor = self._conn.cursor()
        cursor.executescript(SCHEMA_SQL)

        cursor.execute("SELECT version FROM schema_version LIMIT 1")
        row = cursor.fetchone()
        if row is None:
            cursor.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
        else:
            current_version = row["version"] if isinstance(row, sqlite3.Row) else row[0]
            if current_version < 2:
                try:
                    cursor.execute("ALTER TABLE messages ADD COLUMN finish_reason TEXT")
                except sqlite3.OperationalError:
                    pass
                cursor.execute("UPDATE schema_version SET version = 2")
            if current_version < 3:
                try:
                    cursor.execute("ALTER TABLE sessions ADD COLUMN title TEXT")
                except sqlite3.OperationalError:
                    pass
                cursor.execute("UPDATE schema_version SET version = 3")
            if current_version < 4:
                try:
                    cursor.execute(
                        "CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_title_unique "
                        "ON sessions(title) WHERE title IS NOT NULL"
                    )
                except sqlite3.OperationalError:
                    pass
                cursor.execute("UPDATE schema_version SET version = 4")
            if current_version < 5:
                for name, column_type in [
                    ("cache_read_tokens", "INTEGER DEFAULT 0"),
                    ("cache_write_tokens", "INTEGER DEFAULT 0"),
                    ("reasoning_tokens", "INTEGER DEFAULT 0"),
                    ("billing_provider", "TEXT"),
                    ("billing_base_url", "TEXT"),
                    ("billing_mode", "TEXT"),
                    ("estimated_cost_usd", "REAL"),
                    ("actual_cost_usd", "REAL"),
                    ("cost_status", "TEXT"),
                    ("cost_source", "TEXT"),
                    ("pricing_version", "TEXT"),
                ]:
                    try:
                        safe_name = name.replace('"', '""')
                        cursor.execute(f'ALTER TABLE sessions ADD COLUMN "{safe_name}" {column_type}')
                    except sqlite3.OperationalError:
                        pass
                cursor.execute("UPDATE schema_version SET version = 5")
            if current_version < 6:
                for col_name, col_type in [
                    ("reasoning", "TEXT"),
                    ("reasoning_details", "TEXT"),
                    ("codex_reasoning_items", "TEXT"),
                ]:
                    try:
                        safe = col_name.replace('"', '""')
                        cursor.execute(f'ALTER TABLE messages ADD COLUMN "{safe}" {col_type}')
                    except sqlite3.OperationalError:
                        pass
                cursor.execute("UPDATE schema_version SET version = 6")
            if current_version < 7:
                for col_name, col_type in [
                    ("turn_id", "TEXT"),
                    ("operation_id", "TEXT"),
                    ("invalidated_at", "REAL"),
                    ("invalidated_by", "TEXT"),
                    ("invalidation_reason", "TEXT"),
                ]:
                    try:
                        safe = col_name.replace('"', '""')
                        cursor.execute(f'ALTER TABLE messages ADD COLUMN "{safe}" {col_type}')
                    except sqlite3.OperationalError:
                        pass
                cursor.execute(
                    """CREATE TABLE IF NOT EXISTS context_rollbacks (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id),
                        operation_id TEXT,
                        checkpoint_hash TEXT,
                        marker_message_id INTEGER,
                        invalidated_message_ids TEXT,
                        mode TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        restored_at REAL,
                        metadata TEXT
                    )"""
                )
                cursor.execute("UPDATE schema_version SET version = 7")
            if current_version < 8:
                for col_name, col_type in [
                    ("workspace", "TEXT"),
                    ("workspace_key", "TEXT"),
                ]:
                    try:
                        safe = col_name.replace('"', '""')
                        cursor.execute(f'ALTER TABLE sessions ADD COLUMN "{safe}" {col_type}')
                    except sqlite3.OperationalError:
                        pass
                cursor.execute("UPDATE schema_version SET version = 8")
            if current_version < 9:
                cursor.executescript(SCHEMA_SQL)
                cursor.execute("UPDATE schema_version SET version = 9")

        for col_name, col_type in [
            ("workspace", "TEXT"),
            ("workspace_key", "TEXT"),
        ]:
            try:
                safe = col_name.replace('"', '""')
                cursor.execute(f'ALTER TABLE sessions ADD COLUMN "{safe}" {col_type}')
            except sqlite3.OperationalError:
                pass
        cursor.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))

        try:
            cursor.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_title_unique "
                "ON sessions(title) WHERE title IS NOT NULL"
            )
        except sqlite3.OperationalError:
            pass
        try:
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_messages_invalidated ON messages(session_id, invalidated_at)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_messages_turn ON messages(session_id, turn_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_workspace ON sessions(workspace_key)")
        except sqlite3.OperationalError:
            pass

        try:
            cursor.execute("SELECT * FROM messages_fts LIMIT 0")
        except sqlite3.OperationalError:
            cursor.executescript(FTS_SQL)

        self._conn.commit()

    # ── Session lifecycle ──

    def create_session(
        self, session_id: str, source: str, model: str = None,
        model_config: Dict[str, Any] = None, system_prompt: str = None,
        user_id: str = None, parent_session_id: str = None,
        workspace: str = None,
    ) -> str:
        """Create a session row while preserving existing metadata on re-entry."""
        normalized_workspace = _normalize_workspace_path(workspace)
        workspace_key = _workspace_key(normalized_workspace)

        def _do(conn):
            conn.execute(
                """INSERT OR IGNORE INTO sessions (id, source, user_id, model, model_config,
                   system_prompt, parent_session_id, workspace, workspace_key, started_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (session_id, source, user_id, model,
                 json.dumps(model_config) if model_config else None,
                 system_prompt, parent_session_id, normalized_workspace, workspace_key, time.time()),
            )
            if normalized_workspace and workspace_key:
                conn.execute(
                    "UPDATE sessions SET "
                    "workspace = COALESCE(workspace, ?), "
                    "workspace_key = COALESCE(workspace_key, ?) "
                    "WHERE id = ?",
                    (normalized_workspace, workspace_key, session_id),
                )
        self._execute_write(_do)
        return session_id

    def end_session(self, session_id: str, end_reason: str) -> None:
        def _do(conn):
            conn.execute(
                "UPDATE sessions SET ended_at = ?, end_reason = ? WHERE id = ?",
                (time.time(), end_reason, session_id),
            )
        self._execute_write(_do)

    def reopen_session(self, session_id: str) -> None:
        def _do(conn):
            conn.execute(
                "UPDATE sessions SET ended_at = NULL, end_reason = NULL WHERE id = ?",
                (session_id,),
            )
        self._execute_write(_do)

    def update_system_prompt(self, session_id: str, system_prompt: str) -> None:
        def _do(conn):
            conn.execute(
                "UPDATE sessions SET system_prompt = ? WHERE id = ?",
                (system_prompt, session_id),
            )
        self._execute_write(_do)

    def update_token_counts(
        self, session_id: str, input_tokens: int = 0, output_tokens: int = 0,
        model: str = None, cache_read_tokens: int = 0, cache_write_tokens: int = 0,
        reasoning_tokens: int = 0, estimated_cost_usd: Optional[float] = None,
        actual_cost_usd: Optional[float] = None, cost_status: Optional[str] = None,
        cost_source: Optional[str] = None, pricing_version: Optional[str] = None,
        billing_provider: Optional[str] = None, billing_base_url: Optional[str] = None,
        billing_mode: Optional[str] = None, absolute: bool = False,
    ) -> None:
        """Record token and billing totals as either deltas or absolute snapshots."""
        if absolute:
            sql = """UPDATE sessions SET
                   input_tokens = ?, output_tokens = ?,
                   cache_read_tokens = ?, cache_write_tokens = ?, reasoning_tokens = ?,
                   estimated_cost_usd = COALESCE(?, 0),
                   actual_cost_usd = CASE WHEN ? IS NULL THEN actual_cost_usd ELSE ? END,
                   cost_status = COALESCE(?, cost_status),
                   cost_source = COALESCE(?, cost_source),
                   pricing_version = COALESCE(?, pricing_version),
                   billing_provider = COALESCE(billing_provider, ?),
                   billing_base_url = COALESCE(billing_base_url, ?),
                   billing_mode = COALESCE(billing_mode, ?),
                   model = COALESCE(model, ?)
                   WHERE id = ?"""
        else:
            sql = """UPDATE sessions SET
                   input_tokens = input_tokens + ?, output_tokens = output_tokens + ?,
                   cache_read_tokens = cache_read_tokens + ?,
                   cache_write_tokens = cache_write_tokens + ?,
                   reasoning_tokens = reasoning_tokens + ?,
                   estimated_cost_usd = COALESCE(estimated_cost_usd, 0) + COALESCE(?, 0),
                   actual_cost_usd = CASE WHEN ? IS NULL THEN actual_cost_usd
                       ELSE COALESCE(actual_cost_usd, 0) + ? END,
                   cost_status = COALESCE(?, cost_status),
                   cost_source = COALESCE(?, cost_source),
                   pricing_version = COALESCE(?, pricing_version),
                   billing_provider = COALESCE(billing_provider, ?),
                   billing_base_url = COALESCE(billing_base_url, ?),
                   billing_mode = COALESCE(billing_mode, ?),
                   model = COALESCE(model, ?)
                   WHERE id = ?"""
        params = (
            input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
            reasoning_tokens, estimated_cost_usd, actual_cost_usd, actual_cost_usd,
            cost_status, cost_source, pricing_version, billing_provider,
            billing_base_url, billing_mode, model, session_id,
        )
        self._execute_write(lambda conn: conn.execute(sql, params))

    def ensure_session(
        self,
        session_id: str,
        source: str = "unknown",
        model: str = None,
        workspace: str = None,
    ) -> None:
        """Create a minimal session placeholder used by late-bound callers."""
        normalized_workspace = _normalize_workspace_path(workspace)
        workspace_key = _workspace_key(normalized_workspace)

        def _do(conn):
            conn.execute(
                "INSERT OR IGNORE INTO sessions "
                "(id, source, model, workspace, workspace_key, started_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, source, model, normalized_workspace, workspace_key, time.time()),
            )
            if normalized_workspace and workspace_key:
                conn.execute(
                    "UPDATE sessions SET "
                    "workspace = COALESCE(workspace, ?), "
                    "workspace_key = COALESCE(workspace_key, ?) "
                    "WHERE id = ?",
                    (normalized_workspace, workspace_key, session_id),
                )
        self._execute_write(_do)

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            cursor = self._conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,))
            row = cursor.fetchone()
        return dict(row) if row else None

    def get_last_message_id(self, session_id: str) -> Optional[int]:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(id) AS max_id FROM messages WHERE session_id = ? AND invalidated_at IS NULL",
                (session_id,),
            ).fetchone()
        if not row:
            return None
        value = row["max_id"] if isinstance(row, sqlite3.Row) else row[0]
        return int(value) if value is not None else None

    def count_user_messages(self, session_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS count FROM messages "
                "WHERE session_id = ? AND role = 'user' AND invalidated_at IS NULL",
                (session_id,),
            ).fetchone()
        if not row:
            return 0
        return int(row["count"] if isinstance(row, sqlite3.Row) else row[0])

    def recompute_session_counts(self, session_id: str) -> None:
        """Rebuild derived message/tool counters after soft invalidation changes."""
        def _do(conn):
            row = conn.execute(
                "SELECT COUNT(*) AS message_count, "
                "COALESCE(SUM(CASE WHEN tool_calls IS NOT NULL AND tool_calls != '' THEN 1 ELSE 0 END), 0) AS tool_messages "
                "FROM messages WHERE session_id = ? AND invalidated_at IS NULL",
                (session_id,),
            ).fetchone()
            message_count = int(row["message_count"] if isinstance(row, sqlite3.Row) else row[0])
            tool_call_count = 0
            rows = conn.execute(
                "SELECT tool_calls FROM messages WHERE session_id = ? AND invalidated_at IS NULL "
                "AND tool_calls IS NOT NULL AND tool_calls != ''",
                (session_id,),
            ).fetchall()
            for item in rows:
                raw = item["tool_calls"] if isinstance(item, sqlite3.Row) else item[0]
                try:
                    parsed = json.loads(raw)
                    tool_call_count += len(parsed) if isinstance(parsed, list) else 1
                except (json.JSONDecodeError, TypeError):
                    tool_call_count += 1
            conn.execute(
                "UPDATE sessions SET message_count = ?, tool_call_count = ? WHERE id = ?",
                (message_count, tool_call_count, session_id),
            )

        self._execute_write(_do)

    def invalidate_messages_after(
        self,
        session_id: str,
        message_id: Optional[int],
        rollback_id: str = None,
        reason: str = "rollback",
        mode: str = "soft",
        checkpoint_hash: str = None,
        operation_id: str = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Soft-invalidate messages after a marker and keep an audit record.

        Invalidated messages remain available for audit and restore operations,
        but are excluded from normal conversation and search APIs. A missing
        marker is intentionally a no-op; clearing an entire session should be an
        explicit, separate operation.
        """
        if message_id is None:
            return {"rollback_id": None, "invalidated": 0, "message_ids": []}
        mode = mode or "soft"
        if rollback_id is None:
            rollback_id = f"ctxrb_{int(time.time() * 1000)}"
        now = time.time()

        def _do(conn):
            if message_id is None:
                invalidated_ids: List[int] = []
            else:
                rows = conn.execute(
                    "SELECT id FROM messages WHERE session_id = ? AND id > ? AND invalidated_at IS NULL ORDER BY id",
                    (session_id, int(message_id)),
                ).fetchall()
                invalidated_ids = [int(r["id"] if isinstance(r, sqlite3.Row) else r[0]) for r in rows]
                if invalidated_ids:
                    placeholders = ",".join("?" for _ in invalidated_ids)
                    conn.execute(
                        f"UPDATE messages SET invalidated_at = ?, invalidated_by = ?, "
                        f"invalidation_reason = ? WHERE id IN ({placeholders})",
                        (now, rollback_id, reason, *invalidated_ids),
                    )

            conn.execute(
                """INSERT OR REPLACE INTO context_rollbacks
                   (id, session_id, operation_id, checkpoint_hash, marker_message_id,
                    invalidated_message_ids, mode, created_at, restored_at, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)""",
                (
                    rollback_id,
                    session_id,
                    operation_id,
                    checkpoint_hash,
                    int(message_id) if message_id is not None else None,
                    json.dumps(invalidated_ids),
                    mode,
                    now,
                    json.dumps(metadata or {}, ensure_ascii=False),
                ),
            )
            self._recompute_session_counts_in_tx(conn, session_id)
            return {
                "rollback_id": rollback_id,
                "invalidated": len(invalidated_ids),
                "message_ids": invalidated_ids,
            }

        return self._execute_write(_do)

    def invalidate_operation_context(
        self,
        session_id: str,
        marker_message_id: Optional[int],
        turn_id: str = None,
        operation_id: str = None,
        rollback_id: str = None,
        reason: str = "rollback",
        mode: str = "soft",
        checkpoint_hash: str = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Soft-invalidate only the context produced by one operation/turn.

        This is the default operation rollback behavior. It preserves later
        conversation turns unless the caller explicitly chooses strict rollback.
        """
        if marker_message_id is None:
            return {"rollback_id": None, "invalidated": 0, "message_ids": []}
        if not turn_id and not operation_id:
            return self.invalidate_messages_after(
                session_id,
                marker_message_id,
                rollback_id=rollback_id,
                reason=reason,
                mode=mode,
                checkpoint_hash=checkpoint_hash,
                operation_id=operation_id,
                metadata=metadata,
            )
        mode = mode or "soft"
        if rollback_id is None:
            rollback_id = f"ctxrb_{int(time.time() * 1000)}"
        now = time.time()

        def _do(conn):
            clauses = ["session_id = ?", "id > ?", "invalidated_at IS NULL"]
            params: List[Any] = [session_id, int(marker_message_id)]
            op_clauses = []
            if turn_id:
                op_clauses.append("turn_id = ?")
                params.append(turn_id)
            if operation_id:
                op_clauses.append("operation_id = ?")
                params.append(operation_id)
            clauses.append("(" + " OR ".join(op_clauses) + ")")
            rows = conn.execute(
                f"SELECT id FROM messages WHERE {' AND '.join(clauses)} ORDER BY id",
                params,
            ).fetchall()
            invalidated_ids = [int(r["id"] if isinstance(r, sqlite3.Row) else r[0]) for r in rows]
            if invalidated_ids:
                placeholders = ",".join("?" for _ in invalidated_ids)
                conn.execute(
                    f"UPDATE messages SET invalidated_at = ?, invalidated_by = ?, "
                    f"invalidation_reason = ? WHERE id IN ({placeholders})",
                    (now, rollback_id, reason, *invalidated_ids),
                )
            context_metadata = dict(metadata or {})
            context_metadata.update({"turn_id": turn_id, "soft_scope": "operation"})
            conn.execute(
                """INSERT OR REPLACE INTO context_rollbacks
                   (id, session_id, operation_id, checkpoint_hash, marker_message_id,
                    invalidated_message_ids, mode, created_at, restored_at, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)""",
                (
                    rollback_id,
                    session_id,
                    operation_id,
                    checkpoint_hash,
                    int(marker_message_id),
                    json.dumps(invalidated_ids),
                    mode,
                    now,
                    json.dumps(context_metadata, ensure_ascii=False),
                ),
            )
            self._recompute_session_counts_in_tx(conn, session_id)
            return {
                "rollback_id": rollback_id,
                "invalidated": len(invalidated_ids),
                "message_ids": invalidated_ids,
            }

        return self._execute_write(_do)

    def restore_context_rollback(self, rollback_id: str) -> int:
        """Undo a soft context rollback by reactivating its invalidated messages."""
        def _do(conn):
            row = conn.execute(
                "SELECT session_id, invalidated_message_ids FROM context_rollbacks WHERE id = ? AND restored_at IS NULL",
                (rollback_id,),
            ).fetchone()
            if not row:
                return 0
            session_id = row["session_id"] if isinstance(row, sqlite3.Row) else row[0]
            raw_ids = row["invalidated_message_ids"] if isinstance(row, sqlite3.Row) else row[1]
            try:
                message_ids = [int(v) for v in json.loads(raw_ids or "[]")]
            except (json.JSONDecodeError, TypeError, ValueError):
                message_ids = []
            if not message_ids:
                conn.execute("UPDATE context_rollbacks SET restored_at = ? WHERE id = ?", (time.time(), rollback_id))
                return 0
            placeholders = ",".join("?" for _ in message_ids)
            cursor = conn.execute(
                f"UPDATE messages SET invalidated_at = NULL, invalidated_by = NULL, "
                f"invalidation_reason = NULL WHERE id IN ({placeholders}) AND invalidated_by = ?",
                (*message_ids, rollback_id),
            )
            conn.execute("UPDATE context_rollbacks SET restored_at = ? WHERE id = ?", (time.time(), rollback_id))
            self._recompute_session_counts_in_tx(conn, session_id)
            return cursor.rowcount

        return self._execute_write(_do)

    def _recompute_session_counts_in_tx(self, conn: sqlite3.Connection, session_id: str) -> None:
        row = conn.execute(
            "SELECT COUNT(*) AS message_count FROM messages WHERE session_id = ? AND invalidated_at IS NULL",
            (session_id,),
        ).fetchone()
        message_count = int(row["message_count"] if isinstance(row, sqlite3.Row) else row[0])
        rows = conn.execute(
            "SELECT tool_calls FROM messages WHERE session_id = ? AND invalidated_at IS NULL "
            "AND tool_calls IS NOT NULL AND tool_calls != ''",
            (session_id,),
        ).fetchall()
        tool_call_count = 0
        for item in rows:
            raw = item["tool_calls"] if isinstance(item, sqlite3.Row) else item[0]
            try:
                parsed = json.loads(raw)
                tool_call_count += len(parsed) if isinstance(parsed, list) else 1
            except (json.JSONDecodeError, TypeError):
                tool_call_count += 1
        conn.execute(
            "UPDATE sessions SET message_count = ?, tool_call_count = ? WHERE id = ?",
            (message_count, tool_call_count, session_id),
        )

    def resolve_session_id(self, session_id_or_prefix: str, workspace: str = None) -> Optional[str]:
        """Resolve an exact ID or unambiguous prefix within an optional workspace."""
        workspace_key = _workspace_key(workspace)
        if workspace_key:
            with self._lock:
                exact_row = self._conn.execute(
                    "SELECT id FROM sessions WHERE id = ? AND workspace_key = ?",
                    (session_id_or_prefix, workspace_key),
                ).fetchone()
            if exact_row:
                return exact_row["id"]
        else:
            exact = self.get_session(session_id_or_prefix)
            if exact:
                return exact["id"]
        escaped = session_id_or_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where_clauses = ["id LIKE ? ESCAPE '\\'"]
        params = [f"{escaped}%"]
        if workspace_key:
            where_clauses.append("workspace_key = ?")
            params.append(workspace_key)
        where_sql = " AND ".join(where_clauses)
        with self._lock:
            cursor = self._conn.execute(
                f"SELECT id FROM sessions WHERE {where_sql} ORDER BY started_at DESC LIMIT 2",
                params,
            )
            matches = [row["id"] for row in cursor.fetchall()]
        return matches[0] if len(matches) == 1 else None

    def latest_session_id(
        self,
        source: str = None,
        include_children: bool = False,
        workspace: str = None,
    ) -> Optional[str]:
        """Return the most recently active root session for filters."""
        where_clauses = []
        params = []
        if source:
            where_clauses.append("s.source = ?")
            params.append(source)
        if not include_children:
            where_clauses.append("s.parent_session_id IS NULL")
        workspace_key = _workspace_key(workspace)
        if workspace_key:
            where_clauses.append("s.workspace_key = ?")
            params.append(workspace_key)
        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        query = f"""
            SELECT s.id
            FROM sessions s {where_sql}
            ORDER BY
                COALESCE(
                    (SELECT MAX(m.timestamp)
                     FROM messages m
                     WHERE m.session_id = s.id AND m.invalidated_at IS NULL),
                    s.started_at
                ) DESC,
                s.started_at DESC
            LIMIT 1
        """
        with self._lock:
            row = self._conn.execute(query, params).fetchone()
        return row["id"] if row else None

    MAX_TITLE_LENGTH = 100

    @staticmethod
    def sanitize_title(title: Optional[str]) -> Optional[str]:
        """Normalize user-visible titles before enforcing uniqueness."""
        if not title:
            return None
        cleaned = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', title)
        cleaned = re.sub(r'[\u200b-\u200f\u2028-\u202e\u2060-\u2069\ufeff\ufffc\ufff9-\ufffb]', '', cleaned)
        cleaned = re.sub(r'\s+', ' ', cleaned).strip()
        if not cleaned:
            return None
        if len(cleaned) > SessionDB.MAX_TITLE_LENGTH:
            raise ValueError(f"Title too long ({len(cleaned)} chars, max {SessionDB.MAX_TITLE_LENGTH})")
        return cleaned

    def set_session_title(self, session_id: str, title: str) -> bool:
        title = self.sanitize_title(title)
        def _do(conn):
            if title:
                cursor = conn.execute("SELECT id FROM sessions WHERE title = ? AND id != ?", (title, session_id))
                if cursor.fetchone():
                    raise ValueError(f"Title '{title}' is already in use")
            cursor = conn.execute("UPDATE sessions SET title = ? WHERE id = ?", (title, session_id))
            return cursor.rowcount
        return self._execute_write(_do) > 0

    def list_sessions_rich(
        self, source: str = None, exclude_sources: List[str] = None,
        limit: int = 20, offset: int = 0, include_children: bool = False,
        workspace: str = None,
    ) -> List[Dict[str, Any]]:
        """List sessions with preview and activity fields for CLI displays."""
        where_clauses = []
        params = []
        if not include_children:
            where_clauses.append("s.parent_session_id IS NULL")
        if source:
            where_clauses.append("s.source = ?")
            params.append(source)
        if exclude_sources:
            placeholders = ",".join("?" for _ in exclude_sources)
            where_clauses.append(f"s.source NOT IN ({placeholders})")
            params.extend(exclude_sources)
        workspace_key = _workspace_key(workspace)
        if workspace_key:
            where_clauses.append("s.workspace_key = ?")
            params.append(workspace_key)
        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        query = f"""
            SELECT s.*,
                COALESCE(
                    (SELECT SUBSTR(REPLACE(REPLACE(m.content, X'0A', ' '), X'0D', ' '), 1, 63)
                     FROM messages m WHERE m.session_id = s.id AND m.role = 'user' AND m.content IS NOT NULL
                     ORDER BY m.timestamp, m.id LIMIT 1), ''
                ) AS _preview_raw,
                COALESCE(
                    (SELECT COUNT(*) FROM messages um
                     WHERE um.session_id = s.id
                       AND um.role = 'user'
                       AND um.invalidated_at IS NULL), 0
                ) AS user_message_count,
                COALESCE(
                    (SELECT MAX(m2.timestamp) FROM messages m2 WHERE m2.session_id = s.id), s.started_at
                ) AS last_active
            FROM sessions s {where_sql} ORDER BY s.started_at DESC LIMIT ? OFFSET ?
        """
        params.extend([limit, offset])
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        sessions = []
        for row in rows:
            s = dict(row)
            raw = s.pop("_preview_raw", "").strip()
            s["preview"] = (raw[:60] + "...") if len(raw) > 60 else raw
            sessions.append(s)
        return sessions

    # ── Message storage ──

    def append_message(
        self, session_id: str, role: str, content: str = None,
        tool_name: str = None, tool_calls: Any = None, tool_call_id: str = None,
        token_count: int = None, finish_reason: str = None,
        reasoning: str = None, reasoning_details: Any = None,
        codex_reasoning_items: Any = None, turn_id: str = None,
        operation_id: str = None,
    ) -> int:
        """Persist one conversation message and update session counters atomically."""
        reasoning_details_json = json.dumps(reasoning_details) if reasoning_details else None
        codex_items_json = json.dumps(codex_reasoning_items) if codex_reasoning_items else None
        tool_calls_json = json.dumps(tool_calls) if tool_calls else None
        num_tool_calls = len(tool_calls) if isinstance(tool_calls, list) else (1 if tool_calls else 0)

        def _do(conn):
            cursor = conn.execute(
                """INSERT INTO messages (session_id, role, content, tool_call_id,
                   tool_calls, tool_name, timestamp, token_count, finish_reason,
                   reasoning, reasoning_details, codex_reasoning_items, turn_id,
                   operation_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (session_id, role, content, tool_call_id, tool_calls_json, tool_name,
                 time.time(), token_count, finish_reason, reasoning,
                 reasoning_details_json, codex_items_json, turn_id, operation_id),
            )
            msg_id = cursor.lastrowid
            if num_tool_calls > 0:
                conn.execute(
                    "UPDATE sessions SET message_count = message_count + 1, tool_call_count = tool_call_count + ? WHERE id = ?",
                    (num_tool_calls, session_id),
                )
            else:
                conn.execute("UPDATE sessions SET message_count = message_count + 1 WHERE id = ?", (session_id,))
            return msg_id
        return self._execute_write(_do)

    def get_messages(self, session_id: str, include_invalidated: bool = False) -> List[Dict[str, Any]]:
        where = "session_id = ?" if include_invalidated else "session_id = ? AND invalidated_at IS NULL"
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM messages WHERE {where} ORDER BY timestamp, id", (session_id,)
            ).fetchall()
        result = []
        for row in rows:
            msg = dict(row)
            if msg.get("tool_calls"):
                try:
                    msg["tool_calls"] = json.loads(msg["tool_calls"])
                except (json.JSONDecodeError, TypeError):
                    msg["tool_calls"] = []
            result.append(msg)
        return result

    def get_messages_as_conversation(self, session_id: str, include_invalidated: bool = False) -> List[Dict[str, Any]]:
        """Return messages in provider-facing conversation shape."""
        where = "session_id = ?" if include_invalidated else "session_id = ? AND invalidated_at IS NULL"
        with self._lock:
            rows = self._conn.execute(
                "SELECT role, content, tool_call_id, tool_calls, tool_name, "
                "reasoning, reasoning_details, codex_reasoning_items "
                f"FROM messages WHERE {where} ORDER BY timestamp, id",
                (session_id,),
            ).fetchall()
        messages = []
        for row in rows:
            msg = {"role": row["role"], "content": row["content"]}
            if row["tool_call_id"]:
                msg["tool_call_id"] = row["tool_call_id"]
            if row["tool_name"]:
                msg["tool_name"] = row["tool_name"]
            if row["tool_calls"]:
                try:
                    msg["tool_calls"] = json.loads(row["tool_calls"])
                except (json.JSONDecodeError, TypeError):
                    msg["tool_calls"] = []
            if row["role"] == "assistant":
                if row["reasoning"]:
                    msg["reasoning"] = row["reasoning"]
                if row["reasoning_details"]:
                    try:
                        msg["reasoning_details"] = json.loads(row["reasoning_details"])
                    except (json.JSONDecodeError, TypeError):
                        pass
                if row["codex_reasoning_items"]:
                    try:
                        msg["codex_reasoning_items"] = json.loads(row["codex_reasoning_items"])
                    except (json.JSONDecodeError, TypeError):
                        pass
            messages.append(msg)
        return messages

    # ── Search ──

    @staticmethod
    def _sanitize_fts5_query(query: str) -> str:
        """Relax user search text into a query shape accepted by SQLite FTS5."""
        _quoted_parts: list = []
        def _preserve_quoted(m: re.Match) -> str:
            _quoted_parts.append(m.group(0))
            return f"\x00Q{len(_quoted_parts) - 1}\x00"
        sanitized = re.sub(r'"[^"]*"', _preserve_quoted, query)
        sanitized = re.sub(r'[+{}()\"^]', " ", sanitized)
        sanitized = re.sub(r"\*+", "*", sanitized)
        sanitized = re.sub(r"(^|\s)\*", r"\1", sanitized)
        sanitized = re.sub(r"(?i)^(AND|OR|NOT)\b\s*", "", sanitized.strip())
        sanitized = re.sub(r"(?i)\s+(AND|OR|NOT)\s*$", "", sanitized.strip())
        sanitized = re.sub(r"\b(\w+(?:[.-]\w+)+)\b", r'"\1"', sanitized)
        for i, quoted in enumerate(_quoted_parts):
            sanitized = sanitized.replace(f"\x00Q{i}\x00", quoted)
        return sanitized.strip()

    def search_messages(
        self, query: str, source_filter: List[str] = None,
        exclude_sources: List[str] = None, role_filter: List[str] = None,
        limit: int = 20, offset: int = 0, workspace: str = None,
    ) -> List[Dict[str, Any]]:
        """Search active messages and attach a small neighboring-message context."""
        if not query or not query.strip():
            return []
        query = self._sanitize_fts5_query(query)
        if not query:
            return []
        where_clauses = ["messages_fts MATCH ?"]
        params: list = [query]
        if source_filter is not None:
            where_clauses.append(f"s.source IN ({','.join('?' for _ in source_filter)})")
            params.extend(source_filter)
        if exclude_sources is not None:
            where_clauses.append(f"s.source NOT IN ({','.join('?' for _ in exclude_sources)})")
            params.extend(exclude_sources)
        if role_filter:
            where_clauses.append(f"m.role IN ({','.join('?' for _ in role_filter)})")
            params.extend(role_filter)
        workspace_key = _workspace_key(workspace)
        if workspace_key:
            where_clauses.append("s.workspace_key = ?")
            params.append(workspace_key)
        where_clauses.append("m.invalidated_at IS NULL")
        where_sql = " AND ".join(where_clauses)
        params.extend([limit, offset])
        sql = f"""
            SELECT m.id, m.session_id, m.role,
                snippet(messages_fts, 0, '>>>', '<<<', '...', 40) AS snippet,
                m.content, m.timestamp, m.tool_name, s.source, s.model,
                s.started_at AS session_started
            FROM messages_fts
            JOIN messages m ON m.id = messages_fts.rowid
            JOIN sessions s ON s.id = m.session_id
            WHERE {where_sql} ORDER BY rank LIMIT ? OFFSET ?
        """
        with self._lock:
            try:
                matches = [dict(row) for row in self._conn.execute(sql, params).fetchall()]
            except sqlite3.OperationalError as exc:
                raise RuntimeError(f"Session FTS search failed: {exc}") from exc
        for match in matches:
            try:
                with self._lock:
                    context_msgs = [
                        {"role": r["role"], "content": (r["content"] or "")[:200]}
                        for r in self._conn.execute(
                            "SELECT role, content FROM messages WHERE session_id = ? "
                            "AND invalidated_at IS NULL AND id >= ? - 1 AND id <= ? + 1 ORDER BY id",
                            (match["session_id"], match["id"], match["id"]),
                        ).fetchall()
                    ]
                match["context"] = context_msgs
            except Exception:
                match["context"] = []
            match.pop("content", None)
        return matches

    # ── Utility ──

    def session_count(self, source: str = None) -> int:
        with self._lock:
            if source:
                return self._conn.execute("SELECT COUNT(*) FROM sessions WHERE source = ?", (source,)).fetchone()[0]
            return self._conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]

    def delete_session(self, session_id: str) -> bool:
        """Delete a session and detach children that referenced it as parent."""
        def _do(conn):
            if conn.execute("SELECT COUNT(*) FROM sessions WHERE id = ?", (session_id,)).fetchone()[0] == 0:
                return False
            conn.execute("DELETE FROM context_rollbacks WHERE session_id = ?", (session_id,))
            conn.execute("UPDATE sessions SET parent_session_id = NULL WHERE parent_session_id = ?", (session_id,))
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            return True
        return self._execute_write(_do)

    def export_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Return a full session export, including invalidated audit messages."""
        session = self.get_session(session_id)
        if not session:
            return None
        return {**session, "messages": self.get_messages(session_id, include_invalidated=True)}
