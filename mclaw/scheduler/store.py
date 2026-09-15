# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""SQLite persistence layer for scheduler state.

Scheduler data lives beside session data and reuses ``SessionDB`` locking and
write execution. This keeps job claiming, run updates, and target pairing
transitions transactional without giving the scheduler its own connection model.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mclaw.scheduler.ids import new_pairing_code, new_run_id, normalize_pairing_code
from mclaw.scheduler.models import (
    _CONCURRENCY_POLICIES,
    _JOB_STATUSES,
    _ROUTE_STATUSES,
    _SESSION_POLICIES,
    _TARGET_TYPES,
    _coerce_bool,
    _coerce_enum,
    _coerce_float,
    _coerce_int,
    _coerce_mapping,
    _coerce_optional_float,
    _coerce_string_list,
    DeliverySpec,
    ScheduleSpec,
    SchedulerJob,
    SchedulerRun,
    SchedulerTarget,
    SchedulerTargetPairing,
    _json_dumps,
)
from mclaw.scheduler.triggers import next_run_after
from mclaw.state import SessionDB


class SchedulerStore:
    """Repository for jobs, runs, delivery targets, and pairing requests."""

    def __init__(self, session_db: SessionDB | None = None, db_path: str | Path | None = None) -> None:
        self.session_db = session_db or SessionDB(Path(db_path) if db_path else None)
        self._conn = self.session_db._conn
        self._lock = self.session_db._lock
        self._execute_write = self.session_db._execute_write
        self.ensure_local_target()

    def ensure_local_target(self) -> SchedulerTarget:
        """Create the built-in local delivery target when the database is empty."""
        now = time.time()
        existing = self.get_target("local")
        if existing:
            return existing
        target = SchedulerTarget(
            id="local",
            type="local",
            display_name="本地",
            account_id="local",
            chat_id="local",
            chat_type="local",
            source="system",
            first_seen_at=now,
            last_seen_at=now,
            enabled=True,
        )
        return self.create_target(target)

    def create_job(self, job: SchedulerJob) -> SchedulerJob:
        row = job.to_row()

        def _do(conn: sqlite3.Connection) -> SchedulerJob:
            _insert_row(conn, "scheduler_jobs", row)
            return SchedulerJob.from_row(conn.execute("SELECT * FROM scheduler_jobs WHERE id = ?", (job.id,)).fetchone())

        return self._execute_write(_do)

    def update_job(self, job_id: str, patch: dict[str, Any]) -> SchedulerJob:
        updates = self._job_patch_to_row(patch)
        if not updates:
            job = self.get_job(job_id)
            if job is None:
                raise KeyError(job_id)
            return job
        updates["updated_at"] = _coerce_float(patch.get("updated_at", time.time()), time.time(), field="job.updated_at", min_value=0)
        assignments = ", ".join(f"{key} = ?" for key in updates)
        values = list(updates.values())

        def _do(conn: sqlite3.Connection) -> SchedulerJob:
            cursor = conn.execute(
                f"UPDATE scheduler_jobs SET {assignments} WHERE id = ?",
                (*values, job_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(job_id)
            return SchedulerJob.from_row(conn.execute("SELECT * FROM scheduler_jobs WHERE id = ?", (job_id,)).fetchone())

        return self._execute_write(_do)

    def list_jobs(self, *, include_paused: bool = True) -> list[SchedulerJob]:
        where = "" if include_paused else "WHERE enabled = 1"
        with self._lock:
            rows = self._conn.execute(f"SELECT * FROM scheduler_jobs {where} ORDER BY created_at DESC").fetchall()
        return [SchedulerJob.from_row(row) for row in rows]

    def get_job(self, job_id: str) -> SchedulerJob | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM scheduler_jobs WHERE id = ?", (job_id,)).fetchone()
        return SchedulerJob.from_row(row) if row else None

    def pause_job(self, job_id: str) -> SchedulerJob:
        return self.update_job(job_id, {"enabled": False, "status": "paused"})

    def resume_job(self, job_id: str) -> SchedulerJob:
        job = self.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        now = time.time()
        next_run = next_run_after(job.schedule, datetime.fromtimestamp(now, timezone.utc))
        return self.update_job(
            job_id,
            {
                "enabled": True,
                "status": "idle",
                "next_run_at": next_run.timestamp() if next_run is not None else None,
                "updated_at": now,
            },
        )

    def delete_job(self, job_id: str) -> None:
        def _do(conn: sqlite3.Connection) -> None:
            conn.execute("DELETE FROM scheduler_runs WHERE job_id = ?", (job_id,))
            cursor = conn.execute("DELETE FROM scheduler_jobs WHERE id = ?", (job_id,))
            if cursor.rowcount == 0:
                raise KeyError(job_id)

        self._execute_write(_do)

    def due_jobs(self, now: float, limit: int) -> list[SchedulerJob]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM scheduler_jobs
                WHERE enabled = 1
                  AND next_run_at IS NOT NULL
                  AND next_run_at <= ?
                ORDER BY next_run_at ASC, created_at ASC
                LIMIT ?
                """,
                (float(now), int(limit)),
            ).fetchall()
        return [SchedulerJob.from_row(row) for row in rows]

    def claim_job(self, job_id: str, worker_id: str, now: float) -> SchedulerRun | None:
        """Atomically claim a due job and create its running run record."""
        def _do(conn: sqlite3.Connection) -> SchedulerRun | None:
            job_row = conn.execute("SELECT * FROM scheduler_jobs WHERE id = ?", (job_id,)).fetchone()
            if not job_row:
                return None
            scheduled_for = job_row["next_run_at"]
            if scheduled_for is None:
                return None
            cursor = conn.execute(
                """
                UPDATE scheduler_jobs
                SET status = 'claimed', updated_at = ?
                WHERE id = ?
                  AND enabled = 1
                  AND next_run_at = ?
                  AND status != 'claimed'
                """,
                (now, job_id, scheduled_for),
            )
            if cursor.rowcount == 0:
                return None
            run = self._build_run(
                conn,
                job_id=job_id,
                status="running",
                claimed_by=worker_id,
                scheduled_for=scheduled_for,
                started_at=now,
                created_at=now,
                session_id=job_row["session_id"] or "",
            )
            return run

        return self._execute_write(_do)

    def start_run(
        self,
        job: SchedulerJob,
        *,
        worker_id: str,
        scheduled_for: float | None,
        now: float,
    ) -> SchedulerRun:
        """Start an immediate or manual run and mark the job as running."""
        def _do(conn: sqlite3.Connection) -> SchedulerRun:
            run = self._build_run(
                conn,
                job_id=job.id,
                status="running",
                claimed_by=worker_id,
                scheduled_for=scheduled_for,
                started_at=now,
                created_at=now,
                session_id=job.session_id,
            )
            conn.execute(
                "UPDATE scheduler_jobs SET status = 'running', updated_at = ? WHERE id = ?",
                (now, job.id),
            )
            return run

        return self._execute_write(_do)

    def list_active_runs(self, job_id: str) -> list[SchedulerRun]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM scheduler_runs WHERE job_id = ? AND status = 'running' ORDER BY started_at ASC",
                (job_id,),
            ).fetchall()
        return [SchedulerRun.from_row(row) for row in rows]

    def enqueue_run(self, job: SchedulerJob, *, scheduled_for: float | None, now: float) -> SchedulerRun:
        """Persist a queued run for policies that serialize overlapping work."""
        def _do(conn: sqlite3.Connection) -> SchedulerRun:
            return self._build_run(
                conn,
                job_id=job.id,
                status="queued",
                claimed_by="",
                scheduled_for=scheduled_for,
                started_at=None,
                created_at=now,
                session_id=job.session_id,
            )

        return self._execute_write(_do)

    def record_skipped_run(self, job: SchedulerJob, *, scheduled_for: float | None, reason: str, now: float) -> SchedulerRun:
        def _do(conn: sqlite3.Connection) -> SchedulerRun:
            run = self._build_run(
                conn,
                job_id=job.id,
                status="skipped",
                claimed_by="",
                scheduled_for=scheduled_for,
                started_at=None,
                finished_at=now,
                created_at=now,
                session_id=job.session_id,
                error=reason,
            )
            conn.execute(
                "UPDATE scheduler_jobs SET last_run_at = ?, status = 'idle', updated_at = ? WHERE id = ?",
                (now, now, job.id),
            )
            return run

        return self._execute_write(_do)

    def claim_queued_run(self, worker_id: str, now: float) -> SchedulerRun | None:
        """Claim the oldest queued run whose job has no active running attempt."""
        def _do(conn: sqlite3.Connection) -> SchedulerRun | None:
            row = conn.execute(
                """
                SELECT r.* FROM scheduler_runs r
                WHERE r.status = 'queued'
                  AND NOT EXISTS (
                    SELECT 1 FROM scheduler_runs active
                    WHERE active.job_id = r.job_id AND active.status = 'running'
                  )
                ORDER BY r.created_at ASC
                LIMIT 1
                """
            ).fetchone()
            if not row:
                return None
            cursor = conn.execute(
                """
                UPDATE scheduler_runs
                SET status = 'running', claimed_by = ?, started_at = ?
                WHERE id = ? AND status = 'queued'
                """,
                (worker_id, now, row["id"]),
            )
            if cursor.rowcount <= 0:
                return None
            conn.execute(
                "UPDATE scheduler_jobs SET status = 'running', updated_at = ? WHERE id = ?",
                (now, row["job_id"]),
            )
            return SchedulerRun.from_row(conn.execute("SELECT * FROM scheduler_runs WHERE id = ?", (row["id"],)).fetchone())

        return self._execute_write(_do)

    def cancel_active_runs(self, job_id: str, *, reason: str, now: float) -> int:
        """Mark running/queued rows cancelled; this does not stop worker execution."""
        def _do(conn: sqlite3.Connection) -> int:
            cursor = conn.execute(
                """
                UPDATE scheduler_runs
                SET status = 'cancelled', finished_at = ?, error = ?
                WHERE job_id = ? AND status IN ('running', 'queued')
                """,
                (now, reason, job_id),
            )
            return cursor.rowcount

        return self._execute_write(_do)

    def finish_run(self, run: SchedulerRun, *, job_patch: dict[str, Any]) -> None:
        """Persist final run output and related job state in one write step."""
        run_row = run.to_row()
        run_updates = {
            key: run_row[key]
            for key in (
                "status",
                "claimed_by",
                "scheduled_for",
                "started_at",
                "finished_at",
                "session_id",
                "output_path",
                "final_response",
                "error",
                "delivery_result_json",
                "token_usage_json",
                "tool_calls_json",
            )
        }
        job_updates = self._job_patch_to_row(job_patch)
        if job_patch:
            job_updates["updated_at"] = _coerce_float(job_patch.get("updated_at", time.time()), time.time(), field="job.updated_at", min_value=0)

        def _do(conn: sqlite3.Connection) -> None:
            assignments = ", ".join(f"{key} = ?" for key in run_updates)
            conn.execute(
                f"UPDATE scheduler_runs SET {assignments} WHERE id = ?",
                (*run_updates.values(), run.id),
            )
            if job_updates:
                job_assignments = ", ".join(f"{key} = ?" for key in job_updates)
                conn.execute(
                    f"UPDATE scheduler_jobs SET {job_assignments} WHERE id = ?",
                    (*job_updates.values(), run.job_id),
                )

        self._execute_write(_do)

    def recover_stale_runs(self, stale_before: float, *, active_run_ids: set[str] | None = None) -> int:
        """Fail abandoned running rows while preserving caller-known active runs."""
        now = time.time()
        active_ids = {str(run_id) for run_id in (active_run_ids or set()) if run_id}

        def _do(conn: sqlite3.Connection) -> int:
            active_filter = ""
            params: list[Any] = [now, float(stale_before)]
            if active_ids:
                placeholders = ", ".join("?" for _ in active_ids)
                active_filter = f" AND id NOT IN ({placeholders})"
                params.extend(sorted(active_ids))
            cursor = conn.execute(
                f"""
                UPDATE scheduler_runs
                SET status = 'failed', finished_at = ?, error = 'stale run recovered'
                WHERE status = 'running' AND COALESCE(started_at, created_at) < ?
                {active_filter}
                """,
                tuple(params),
            )
            recovered = cursor.rowcount
            conn.execute(
                """
                UPDATE scheduler_jobs
                SET status = 'idle', updated_at = ?
                WHERE status = 'running'
                  AND NOT EXISTS (
                    SELECT 1 FROM scheduler_runs r
                    WHERE r.job_id = scheduler_jobs.id AND r.status = 'running'
                  )
                """,
                (now,),
            )
            return recovered

        return self._execute_write(_do)

    def list_runs(self, job_id: str | None = None, limit: int = 20, offset: int = 0) -> list[SchedulerRun]:
        if job_id:
            sql = "SELECT * FROM scheduler_runs WHERE job_id = ? ORDER BY created_at DESC LIMIT ? OFFSET ?"
            params = (job_id, int(limit), int(offset))
        else:
            sql = "SELECT * FROM scheduler_runs ORDER BY created_at DESC LIMIT ? OFFSET ?"
            params = (int(limit), int(offset))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [SchedulerRun.from_row(row) for row in rows]

    def get_run(self, run_id: str) -> SchedulerRun | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM scheduler_runs WHERE id = ?", (run_id,)).fetchone()
        return SchedulerRun.from_row(row) if row else None

    def create_target(self, target: SchedulerTarget) -> SchedulerTarget:
        row = target.to_row()
        now = time.time()
        if not row.get("first_seen_at"):
            row["first_seen_at"] = now
        if not row.get("last_seen_at"):
            row["last_seen_at"] = now
        row["updated_at"] = now

        def _do(conn: sqlite3.Connection) -> SchedulerTarget:
            _insert_or_replace_row(conn, "scheduler_targets", row)
            return SchedulerTarget.from_row(conn.execute("SELECT * FROM scheduler_targets WHERE id = ?", (target.id,)).fetchone())

        return self._execute_write(_do)

    def list_targets(self, *, enabled_only: bool = True) -> list[SchedulerTarget]:
        where = "WHERE enabled = 1" if enabled_only else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM scheduler_targets {where} ORDER BY source = 'system' DESC, display_name ASC"
            ).fetchall()
        return [SchedulerTarget.from_row(row) for row in rows]

    def get_target(self, target_id: str) -> SchedulerTarget | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM scheduler_targets WHERE id = ?", (target_id,)).fetchone()
        return SchedulerTarget.from_row(row) if row else None

    def update_target(self, target_id: str, patch: dict[str, Any]) -> SchedulerTarget:
        updates = self._target_patch_to_row(patch)
        updates["updated_at"] = _coerce_float(patch.get("updated_at", time.time()), time.time(), field="target.updated_at", min_value=0)
        assignments = ", ".join(f"{key} = ?" for key in updates)

        def _do(conn: sqlite3.Connection) -> SchedulerTarget:
            cursor = conn.execute(
                f"UPDATE scheduler_targets SET {assignments} WHERE id = ?",
                (*updates.values(), target_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(target_id)
            return SchedulerTarget.from_row(conn.execute("SELECT * FROM scheduler_targets WHERE id = ?", (target_id,)).fetchone())

        return self._execute_write(_do)

    def delete_target(self, target_id: str) -> None:
        if target_id == "local":
            raise ValueError("built-in local target cannot be deleted")

        def _do(conn: sqlite3.Connection) -> None:
            cursor = conn.execute("DELETE FROM scheduler_targets WHERE id = ?", (target_id,))
            if cursor.rowcount == 0:
                raise KeyError(target_id)

        self._execute_write(_do)

    def create_pairing(self, pairing: SchedulerTargetPairing) -> SchedulerTargetPairing:
        row = pairing.to_row()
        if not row.get("code"):
            row["code"] = new_pairing_code()
        else:
            row["code"] = normalize_pairing_code(row["code"])
        now = time.time()
        if not row.get("created_at"):
            row["created_at"] = now
        if not row.get("updated_at"):
            row["updated_at"] = now

        def _do(conn: sqlite3.Connection) -> SchedulerTargetPairing:
            _insert_row(conn, "scheduler_target_pairings", row)
            return SchedulerTargetPairing.from_row(
                conn.execute("SELECT * FROM scheduler_target_pairings WHERE code = ?", (row["code"],)).fetchone()
            )

        return self._execute_write(_do)

    def get_pairing(self, code: str) -> SchedulerTargetPairing | None:
        code = normalize_pairing_code(code)
        with self._lock:
            row = self._conn.execute("SELECT * FROM scheduler_target_pairings WHERE code = ?", (code,)).fetchone()
        return SchedulerTargetPairing.from_row(row) if row else None

    def bind_pairing(self, code: str, *, target: SchedulerTarget) -> SchedulerTargetPairing:
        """Bind a waiting pairing code to a routable target transactionally."""
        code = normalize_pairing_code(code)
        now = time.time()

        def _do(conn: sqlite3.Connection) -> SchedulerTargetPairing:
            pairing = conn.execute("SELECT * FROM scheduler_target_pairings WHERE code = ?", (code,)).fetchone()
            if not pairing:
                raise KeyError(code)
            if pairing["status"] != "waiting":
                raise ValueError(f"pairing is not waiting: {pairing['status']}")
            if float(pairing["expires_at"] or 0) <= now:
                conn.execute(
                    "UPDATE scheduler_target_pairings SET status = 'expired', error = ?, updated_at = ? WHERE code = ?",
                    ("expired", now, code),
                )
                raise ValueError("pairing expired")
            target_row = target.to_row()
            target_row["updated_at"] = now
            if not target_row.get("first_seen_at"):
                target_row["first_seen_at"] = now
            target_row["last_seen_at"] = now
            existing_target = conn.execute(
                """
                SELECT * FROM scheduler_targets
                WHERE type = ? AND account_id = ? AND chat_id = ?
                """,
                (target_row["type"], target_row["account_id"], target_row["chat_id"]),
            ).fetchone()
            target_id = target.id
            if existing_target:
                target_id = existing_target["id"]
                target_row["id"] = target_id
                target_row["first_seen_at"] = existing_target["first_seen_at"] or target_row["first_seen_at"]
            _insert_or_replace_row(conn, "scheduler_targets", target_row)
            conn.execute(
                """
                UPDATE scheduler_target_pairings
                SET status = 'bound', target_id = ?, error = '', updated_at = ?
                WHERE code = ?
                """,
                (target_id, now, code),
            )
            return SchedulerTargetPairing.from_row(
                conn.execute("SELECT * FROM scheduler_target_pairings WHERE code = ?", (code,)).fetchone()
            )

        return self._execute_write(_do)

    def fail_pairing(self, code: str, *, error: str) -> SchedulerTargetPairing:
        return self._set_pairing_status(normalize_pairing_code(code), status="failed", error=error)

    def cancel_pairing(self, code: str, *, reason: str = "cancelled_by_user") -> SchedulerTargetPairing:
        return self._set_pairing_status(normalize_pairing_code(code), status="cancelled", error=reason)

    def expire_pairings(self, now: float) -> int:
        def _do(conn: sqlite3.Connection) -> int:
            cursor = conn.execute(
                """
                UPDATE scheduler_target_pairings
                SET status = 'expired', error = 'expired', updated_at = ?
                WHERE status = 'waiting' AND expires_at <= ?
                """,
                (now, now),
            )
            return cursor.rowcount

        return self._execute_write(_do)

    def _set_pairing_status(self, code: str, *, status: str, error: str) -> SchedulerTargetPairing:
        now = time.time()

        def _do(conn: sqlite3.Connection) -> SchedulerTargetPairing:
            cursor = conn.execute(
                "UPDATE scheduler_target_pairings SET status = ?, error = ?, updated_at = ? WHERE code = ?",
                (status, error, now, code),
            )
            if cursor.rowcount == 0:
                raise KeyError(code)
            return SchedulerTargetPairing.from_row(
                conn.execute("SELECT * FROM scheduler_target_pairings WHERE code = ?", (code,)).fetchone()
            )

        return self._execute_write(_do)

    def _build_run(
        self,
        conn: sqlite3.Connection,
        *,
        job_id: str,
        status: str,
        claimed_by: str,
        scheduled_for: float | None,
        started_at: float | None,
        created_at: float,
        session_id: str,
        finished_at: float | None = None,
        error: str = "",
    ) -> SchedulerRun:
        """Allocate a per-job run number and insert the run within a write transaction."""
        row = conn.execute("SELECT COALESCE(MAX(run_no), 0) + 1 AS next_no FROM scheduler_runs WHERE job_id = ?", (job_id,)).fetchone()
        run = SchedulerRun(
            id=new_run_id(),
            job_id=job_id,
            run_no=int(row["next_no"] if isinstance(row, sqlite3.Row) else row[0]),
            status=status,  # type: ignore[arg-type]
            claimed_by=claimed_by,
            scheduled_for=scheduled_for,
            started_at=started_at,
            finished_at=finished_at,
            session_id=session_id,
            output_path="",
            final_response="",
            error=error,
            delivery_result={},
            token_usage={},
            tool_calls=[],
            created_at=created_at,
        )
        _insert_row(conn, "scheduler_runs", run.to_row())
        return run

    def _job_patch_to_row(self, patch: dict[str, Any]) -> dict[str, Any]:
        """Validate job patch fields and translate API names to table columns."""
        result: dict[str, Any] = {}
        direct = {
            "name": "name",
            "enabled": "enabled",
            "next_run_at": "next_run_at",
            "last_run_at": "last_run_at",
            "status": "status",
            "prompt": "prompt",
            "workdir": "workdir",
            "session_policy": "session_policy",
            "session_id": "session_id",
            "max_iterations": "max_iterations",
            "timeout_seconds": "timeout_seconds",
            "concurrency_policy": "concurrency_policy",
            "failure_count": "failure_count",
            "created_at": "created_at",
            "updated_at": "updated_at",
        }
        for key, column in direct.items():
            if key in patch:
                value = patch[key]
                if key == "enabled":
                    value = 1 if _coerce_bool(value, True, field="job.enabled") else 0
                elif key in {"next_run_at", "last_run_at"}:
                    value = _coerce_optional_float(value, field=f"job.{key}", min_value=0)
                elif key == "status":
                    value = _coerce_enum(value, _JOB_STATUSES, "idle", field="job.status")
                elif key == "session_policy":
                    value = _coerce_enum(value, _SESSION_POLICIES, "task_thread", field="job.session_policy")
                elif key in {"max_iterations", "timeout_seconds"}:
                    value = _coerce_int(value, 200 if key == "max_iterations" else 3600, field=f"job.{key}", min_value=1)
                elif key == "concurrency_policy":
                    value = _coerce_enum(value, _CONCURRENCY_POLICIES, "skip", field="job.concurrency_policy")
                elif key == "failure_count":
                    value = _coerce_int(value, 0, field="job.failure_count", min_value=0)
                elif key in {"created_at", "updated_at"}:
                    value = _coerce_float(value, 0, field=f"job.{key}", min_value=0)
                result[column] = value
        if "schedule" in patch:
            schedule = patch["schedule"]
            if isinstance(schedule, ScheduleSpec):
                result.update(schedule.to_row())
            elif isinstance(schedule, dict):
                result.update(ScheduleSpec.from_dict(schedule).to_row())
        if "enabled_toolsets" in patch:
            result["enabled_toolsets_json"] = _json_dumps(
                _coerce_string_list(patch.get("enabled_toolsets"), field="job.enabled_toolsets")
            )
        if "delivery" in patch:
            delivery = patch["delivery"]
            if isinstance(delivery, DeliverySpec):
                result["delivery_json"] = _json_dumps(delivery.to_dict())
            elif isinstance(delivery, dict):
                result["delivery_json"] = _json_dumps(DeliverySpec.from_dict(delivery).to_dict())
        return result

    def _target_patch_to_row(self, patch: dict[str, Any]) -> dict[str, Any]:
        """Validate target patch fields and translate API names to table columns."""
        result: dict[str, Any] = {}
        direct = {
            "type": "type",
            "display_name": "display_name",
            "account_id": "account_id",
            "chat_id": "chat_id",
            "chat_type": "chat_type",
            "route_status": "route_status",
            "source": "source",
            "first_seen_at": "first_seen_at",
            "last_seen_at": "last_seen_at",
            "enabled": "enabled",
            "updated_at": "updated_at",
        }
        for key, column in direct.items():
            if key in patch:
                value = patch[key]
                if key == "type":
                    value = _coerce_enum(value, _TARGET_TYPES, "local", field="target.type")
                elif key == "route_status":
                    value = _coerce_enum(value, _ROUTE_STATUSES, "ready", field="target.route_status")
                elif key == "enabled":
                    value = 1 if _coerce_bool(value, True, field="target.enabled") else 0
                elif key in {"first_seen_at", "last_seen_at", "updated_at"}:
                    value = _coerce_float(value, 0, field=f"target.{key}", min_value=0)
                result[column] = value
        if "route_metadata" in patch:
            result["route_metadata_json"] = _json_dumps(_coerce_mapping(patch.get("route_metadata"), field="target.route_metadata"))
        if "capabilities" in patch:
            result["capabilities_json"] = _json_dumps(_coerce_mapping(patch.get("capabilities"), field="target.capabilities"))
        return result


def _insert_row(conn: sqlite3.Connection, table: str, row: dict[str, Any]) -> None:
    columns = list(row)
    placeholders = ", ".join("?" for _ in columns)
    col_sql = ", ".join(columns)
    conn.execute(
        f"INSERT INTO {table} ({col_sql}) VALUES ({placeholders})",
        tuple(row[column] for column in columns),
    )


def _insert_or_replace_row(conn: sqlite3.Connection, table: str, row: dict[str, Any]) -> None:
    columns = list(row)
    placeholders = ", ".join("?" for _ in columns)
    col_sql = ", ".join(columns)
    conn.execute(
        f"INSERT OR REPLACE INTO {table} ({col_sql}) VALUES ({placeholders})",
        tuple(row[column] for column in columns),
    )
