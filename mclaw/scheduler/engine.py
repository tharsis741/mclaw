# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Scheduler engine: due detection, concurrency policy, execution, and completion.

This layer owns scheduler runtime state transitions. It claims due or queued
runs, executes them through a bounded worker pool, then commits output,
delivery, retry, and auto-pause results back to the store.
"""

from __future__ import annotations

import concurrent.futures
from datetime import datetime, timezone
import logging
import socket
import threading
import time
from typing import Any

from mclaw.scheduler.formatting import write_final_response_output, write_run_output
from mclaw.scheduler.models import SchedulerJob, SchedulerRun
from mclaw.scheduler.store import SchedulerStore
from mclaw.scheduler.triggers import next_run_after

logger = logging.getLogger(__name__)

_FINAL_RUN_STATUSES = {"succeeded", "failed", "skipped", "cancelled", "delivery_failed"}


class SchedulerEngine:
    """Coordinates due jobs, queued runs, worker futures, and completion state."""

    def __init__(
        self,
        *,
        store: SchedulerStore,
        runner: Any,
        delivery: Any,
        worker_id: str,
        max_due_per_tick: int = 5,
        max_workers: int = 2,
        runtime_config: dict | None = None,
    ) -> None:
        self.store = store
        self.runner = runner
        self.delivery = delivery
        self.worker_id = worker_id or f"{socket.gethostname()}-{id(self)}"
        self.max_due_per_tick = max(1, int(max_due_per_tick or 1))
        self.max_workers = max(1, int(max_workers or 1))
        self.runtime_config = runtime_config or {}
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers)
        self._futures: dict[str, concurrent.futures.Future[SchedulerRun]] = {}
        self._lock = threading.Lock()

    def tick_once(self, now: float | None = None) -> int:
        """Process one scheduler tick and return completed plus submitted work."""
        now = float(now if now is not None else time.time())
        completed = len(self.drain_completed())
        stale_after = _int_config(self.runtime_config, "stale_run_after_seconds", 3600)
        self.store.recover_stale_runs(now - stale_after, active_run_ids=self._active_future_run_ids())
        submitted = 0

        while self._free_slots() > 0:
            queued = self.store.claim_queued_run(self.worker_id, now)
            if not queued:
                break
            job = self.store.get_job(queued.job_id)
            if not job:
                continue
            self._submit(job, queued)
            submitted += 1

        for job in self.store.due_jobs(now, self.max_due_per_tick):
            if self._free_slots() <= 0 and job.concurrency_policy != "queue":
                break
            submitted += self._handle_due_job(job, now)

        completed += len(self.drain_completed())
        return submitted + completed

    def run_job_now(self, job_id: str, *, manual: bool = True) -> SchedulerRun:
        """Start a job immediately, optionally waiting for manual completion."""
        now = time.time()
        job = self.store.get_job(job_id)
        if not job:
            raise KeyError(job_id)
        active = self.store.list_active_runs(job.id)
        if active:
            if job.concurrency_policy == "skip":
                return self.store.record_skipped_run(job, scheduled_for=None, reason="manual run skipped: active run exists", now=now)
            if job.concurrency_policy == "queue":
                return self.store.enqueue_run(job, scheduled_for=None, now=now)
            if job.concurrency_policy == "replace":
                self.store.cancel_active_runs(job.id, reason="manual run replaced active run", now=now)
        if self._free_slots() <= 0 and job.concurrency_policy == "queue":
            return self.store.enqueue_run(job, scheduled_for=None, now=now)
        run = self.store.start_run(job, worker_id=self.worker_id, scheduled_for=None, now=now)
        if manual:
            completed = self._run_and_finish(job, run)
            return completed
        self._submit(job, run)
        return run

    def drain_completed(self) -> list[SchedulerRun]:
        """Collect worker futures that finished since the previous tick."""
        completed: list[SchedulerRun] = []
        with self._lock:
            done_ids = [run_id for run_id, future in self._futures.items() if future.done()]
        for run_id in done_ids:
            with self._lock:
                future = self._futures.pop(run_id, None)
            if future is None:
                continue
            try:
                run = future.result()
                completed.append(run)
            except Exception as exc:
                logger.exception("scheduler worker future failed: %s", exc)
                failed = self._record_future_failure(run_id, exc)
                if failed is not None:
                    completed.append(failed)
        return completed

    def shutdown(self) -> None:
        """Drain finished futures and wait for submitted scheduler work."""
        self.drain_completed()
        self._executor.shutdown(wait=True, cancel_futures=False)

    def _handle_due_job(self, job: SchedulerJob, now: float) -> int:
        """Apply the job concurrency policy to one due occurrence."""
        scheduled_for = job.next_run_at
        active = self.store.list_active_runs(job.id)
        if active:
            if job.concurrency_policy == "skip":
                self.store.record_skipped_run(job, scheduled_for=scheduled_for, reason="skipped: active run exists", now=now)
                self._advance_schedule(job, scheduled_for)
                return 1
            if job.concurrency_policy == "queue":
                self.store.enqueue_run(job, scheduled_for=scheduled_for, now=now)
                self._advance_schedule(job, scheduled_for)
                return 1
            if job.concurrency_policy == "replace":
                self.store.cancel_active_runs(job.id, reason="replaced by next due occurrence", now=now)
        elif self._free_slots() <= 0 and job.concurrency_policy == "queue":
            self.store.enqueue_run(job, scheduled_for=scheduled_for, now=now)
            self._advance_schedule(job, scheduled_for)
            return 1

        if self._free_slots() <= 0:
            return 0
        run = self.store.claim_job(job.id, self.worker_id, now)
        if run is None:
            return 0
        self._advance_schedule(job, scheduled_for)
        self._submit(job, run)
        return 1

    def _advance_schedule(self, job: SchedulerJob, occurrence_ts: float | None) -> None:
        """Move recurring jobs to their next occurrence after a claim decision."""
        if occurrence_ts is None:
            return
        after = datetime.fromtimestamp(float(occurrence_ts), timezone.utc)
        nxt = next_run_after(job.schedule, after)
        self.store.update_job(
            job.id,
            {
                "next_run_at": nxt.timestamp() if nxt is not None else None,
                "status": "running" if self.store.list_active_runs(job.id) else "idle",
            },
        )

    def _submit(self, job: SchedulerJob, run: SchedulerRun) -> None:
        """Submit a run to the bounded worker pool and track its future by run id."""
        future = self._executor.submit(self._run_and_finish, job, run)
        with self._lock:
            self._futures[run.id] = future

    def _run_and_finish(self, job: SchedulerJob, run: SchedulerRun) -> SchedulerRun:
        """Execute one run, deliver successful responses, and persist final state."""
        run = self.runner.run(job, run)
        current = self._finalized_run(run.id)
        if current is not None:
            return current
        if run.status == "succeeded":
            try:
                delivery_result = self.delivery.deliver(job, run, run.final_response)
            except Exception as exc:
                logger.exception("scheduler delivery failed: %s", exc)
                delivery_result = {"success": False, "status": "failed", "error": str(exc)}
            run.delivery_result = delivery_result
            if not delivery_result.get("success"):
                run.status = "delivery_failed"
                run.error = delivery_result.get("error") or "delivery failed"
        final_response_output = self._write_final_response_output(job, run)
        if final_response_output:
            run.delivery_result = {**(run.delivery_result or {}), "final_response_output": final_response_output}
            if run.status == "succeeded" and not final_response_output.get("success"):
                run.status = "delivery_failed"
                run.error = final_response_output.get("error") or "final response output failed"
        current = self._finalized_run(run.id)
        if current is not None:
            return current
        output_dir = getattr(self.runner, "output_dir", None)
        run.output_path = write_run_output(output_dir=output_dir, job=job, run=run)
        self._finish_run(job, run)
        return run

    def _write_final_response_output(self, job: SchedulerJob, run: SchedulerRun) -> dict[str, Any]:
        """Write optional final-response-only output without raising to callers."""
        path_spec = getattr(job.delivery, "final_response_path", "")
        if not path_spec or not run.final_response:
            return {}
        filename_template = getattr(job.delivery, "final_response_filename_template", "")
        try:
            path = write_final_response_output(
                path_spec=path_spec,
                job=job,
                run=run,
                filename_template=filename_template,
            )
            return {"success": True, "path": path}
        except Exception as exc:
            logger.exception("scheduler final response output failed: %s", exc)
            return {"success": False, "error": str(exc)}

    def _finish_run(self, job: SchedulerJob, run: SchedulerRun) -> None:
        """Commit final job counters and pause jobs after repeated failures."""
        if self._finalized_run(run.id) is not None:
            return
        now = run.finished_at or time.time()
        current_job = self.store.get_job(job.id) or job
        if run.status == "succeeded":
            patch: dict[str, Any] = {
                "last_run_at": now,
                "failure_count": 0,
                "status": "idle",
            }
        elif run.status in {"failed", "delivery_failed"}:
            failures = int(current_job.failure_count or 0) + 1
            max_failures = _int_config(self.runtime_config, "max_consecutive_failures", 5)
            patch = {
                "last_run_at": now,
                "failure_count": failures,
                "status": "error",
            }
            if failures >= max_failures:
                patch["enabled"] = False
                patch["status"] = "paused"
        else:
            patch = {"last_run_at": now, "status": "idle"}
        self.store.finish_run(run, job_patch=patch)

    def _free_slots(self) -> int:
        self.drain_completed()
        with self._lock:
            running = len(self._futures)
        return max(0, self.max_workers - running)

    def _active_future_run_ids(self) -> set[str]:
        with self._lock:
            return set(self._futures)

    def _finalized_run(self, run_id: str) -> SchedulerRun | None:
        current = self.store.get_run(run_id)
        if current is not None and current.status in _FINAL_RUN_STATUSES:
            return current
        return None

    def _record_future_failure(self, run_id: str, exc: BaseException) -> SchedulerRun | None:
        """Persist an unexpected worker exception as a failed run."""
        run = self.store.get_run(run_id)
        if run is None or run.status in _FINAL_RUN_STATUSES:
            return run
        job = self.store.get_job(run.job_id)
        if job is None:
            return None
        run.status = "failed"
        run.error = str(exc) or exc.__class__.__name__
        run.finished_at = time.time()
        try:
            output_dir = getattr(self.runner, "output_dir", None)
            run.output_path = write_run_output(output_dir=output_dir, job=job, run=run)
        except Exception as output_exc:
            logger.exception("scheduler failed-run output write failed: %s", output_exc)
        self._finish_run(job, run)
        return run


def _int_config(config: dict[str, Any], key: str, default: int) -> int:
    try:
        return int(config.get(key, default))
    except (TypeError, ValueError):
        return default
