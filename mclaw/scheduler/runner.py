# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Scheduler runner that executes one local MClaw turn.

The runner converts a scheduled job into an ordinary M-Claw conversation while
preserving scheduler metadata, session policy, timeout handling, and captured
tool/token telemetry for the run report.
"""

from __future__ import annotations

import concurrent.futures
from datetime import datetime, timezone
import logging
from pathlib import Path
import threading
import time
import uuid
from typing import Any, Callable

from mclaw.providers.resolver import restore_session_runtime_context
from mclaw.providers.runtime import ProviderRuntimeContext
from mclaw.cli.runtime.session_lock import InteractiveSessionLock
from mclaw.scheduler.formatting import default_output_dir, write_run_output
from mclaw.scheduler.models import SchedulerJob, SchedulerRun
from mclaw.state import SessionDB
from mclaw.tools.toolsets import validate_toolset

logger = logging.getLogger(__name__)


class SchedulerRunner:
    """Runs a single scheduler job through the agent runtime."""

    def __init__(
        self,
        *,
        provider_runtime: ProviderRuntimeContext | None = None,
        config: dict[str, Any] | None = None,
        session_db: SessionDB | None = None,
        store: Any = None,
        output_dir: str | None = None,
        agent_factory: Callable[..., Any] | None = None,
        print_fn: Callable[..., None] | None = None,
        conversation_executor: Callable[..., dict[str, Any]] | None = None,
        cancel_executor: Callable[[str], None] | None = None,
    ) -> None:
        self.startup_provider_runtime = provider_runtime
        self.config = config or {}
        self.session_db = session_db or SessionDB()
        self.store = store
        scheduler_cfg = self.config.get("scheduler", {}) if isinstance(self.config, dict) else {}
        self.output_dir = output_dir or str(scheduler_cfg.get("output_dir") or default_output_dir())
        self.agent_factory = agent_factory
        self.print_fn = print_fn
        self.conversation_executor = conversation_executor
        self.cancel_executor = cancel_executor
        self._agents: dict[str, Any] = {}
        self._cancelled: set[str] = set()
        self._guard = threading.Lock()

    def cancel(self, run_id: str) -> None:
        """Interrupt actual execution in addition to cancelling the persisted row."""
        with self._guard:
            self._cancelled.add(run_id)
            agent = self._agents.get(run_id)
        if agent is not None:
            agent.interrupt()
        if self.cancel_executor:
            self.cancel_executor(run_id)

    def _is_cancelled(self, run_id: str) -> bool:
        with self._guard:
            return run_id in self._cancelled

    def run(self, job: SchedulerJob, run: SchedulerRun) -> SchedulerRun:
        """Execute the job prompt and return the updated run record."""
        started_at = run.started_at or time.time()
        run.started_at = started_at
        agent_holder: dict[str, Any] = {}
        session_lock = None
        release_on_done = False
        agent_closed = True
        try:
            if self._is_cancelled(run.id):
                run.status, run.finished_at = "cancelled", time.time()
                return run
            if self.conversation_executor is not None:
                result = self.conversation_executor(
                    job, run, extra_system=self._scheduler_context(job, run),
                    enabled_toolsets=self._effective_toolsets(job),
                )
                run.final_response = str(result.get("final_response") or "")
                cancelled = result.get("cancelled") or result.get("interrupted") or result.get("status") == "interrupted"
                run.status = "cancelled" if cancelled else ("succeeded" if result.get("completed") else "failed")
                run.error = str(result.get("error") or "")
                run.tool_calls = _extract_tool_calls(result)
                run.token_usage = _extract_token_usage(result)
                run.finished_at = time.time()
                return run
            session_id = self._resolve_session(job, run)
            run.session_id = session_id
            session_lock = InteractiveSessionLock(session_id, Path(self.session_db.db_path).parent / "sessions" / "locks")
            session_lock.acquire()
            history = self._history_for_session(session_id)
            enabled_toolsets = self._effective_toolsets(job)
            extra_system = self._scheduler_context(
                job,
                run,
                enabled_toolsets=enabled_toolsets,
            )
            def _execute() -> dict[str, Any]:
                agent = self._make_agent(
                    job=job,
                    run=run,
                    session_id=session_id,
                    enabled_toolsets=enabled_toolsets,
                )
                agent_holder["agent"] = agent
                with self._guard:
                    self._agents[run.id] = agent
                    cancelled = run.id in self._cancelled
                if cancelled:
                    agent.interrupt()
                return agent.run_conversation(
                    user_message=job.prompt,
                    conversation_history=history,
                    disable_tools=False,
                    extra_system=extra_system,
                    advance_background_review=False,
                )

            executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            future = executor.submit(_execute)
            try:
                result = future.result(timeout=max(1, int(job.timeout_seconds or 1)))
            except concurrent.futures.TimeoutError:
                agent = agent_holder.get("agent")
                if agent is not None and hasattr(agent, "interrupt"):
                    try:
                        agent.interrupt()
                    except Exception as exc:
                        logger.debug("scheduler agent interrupt failed after timeout: %s", exc)
                # The worker thread may still unwind cooperatively after interrupt.
                release_on_done = True
                def close_timed_out(_future):
                    if self._close_agent(agent_holder.get("agent")):
                        session_lock.release()
                future.add_done_callback(close_timed_out)
                future.cancel()
                executor.shutdown(wait=False, cancel_futures=True)
                raise TimeoutError(f"scheduler run timed out after {job.timeout_seconds}s")
            except BaseException:
                agent_closed = self._close_agent(agent_holder.get("agent"))
                raise
            finally:
                if not future.cancelled():
                    executor.shutdown(wait=False, cancel_futures=False)

            agent_closed = self._close_agent(agent_holder.get("agent"))

            run.final_response = str(result.get("final_response") or "")
            run.error = ""
            run.finished_at = time.time()
            run.status = "succeeded" if run.final_response or result.get("completed") else "failed"
            if result.get("error") or result.get("abort") or result.get("completed") is False:
                run.status = "failed"
                run.error = str(result.get("error") or result.get("abort_message") or "任务未完成")
            if self._is_cancelled(run.id):
                run.status = "cancelled"
            if run.status == "failed" and not run.error:
                run.error = "agent returned no final response"
            run.tool_calls = _extract_tool_calls(result)
            run.token_usage = _extract_token_usage(result)
            run.output_path = write_run_output(output_dir=self.output_dir, job=job, run=run)
            return run
        except Exception as exc:
            run.status = "cancelled" if self._is_cancelled(run.id) else "failed"
            run.error = str(exc)
            run.finished_at = time.time()
            run.output_path = write_run_output(output_dir=self.output_dir, job=job, run=run)
            return run
        finally:
            with self._guard:
                self._agents.pop(run.id, None)
                self._cancelled.discard(run.id)
            if session_lock is not None and not release_on_done and agent_closed:
                session_lock.release()

    @staticmethod
    def _close_agent(agent: Any) -> bool:
        """Close a completed scheduler Agent while accepting lightweight test factories."""
        close = getattr(agent, "close", None)
        if not callable(close):
            return True
        try:
            result = close()
        except BaseException:
            logger.warning("scheduler Agent close failed", exc_info=True)
            return False
        return result is not False

    def _resolve_session(self, job: SchedulerJob, run: SchedulerRun) -> str:
        """Resolve or create the session governed by the job's session policy."""
        if job.session_policy == "new_session":
            session_id = f"session_{uuid.uuid4().hex[:12]}"
        else:
            session_id = job.session_id or f"session_{uuid.uuid4().hex[:12]}"
            if not job.session_id:
                job.session_id = session_id
                if self.store is not None:
                    self.store.update_job(job.id, {"session_id": session_id})
        self.session_db.create_session(
            session_id=session_id,
            source="scheduler",
            model=self.startup_provider_runtime.model,
            workspace=job.workdir or None,
        )
        return session_id

    def _history_for_session(self, session_id: str) -> list[dict[str, Any]]:
        """Load prior messages without duplicating the persisted system prompt."""
        history = self.session_db.get_messages_as_conversation(session_id)
        if history and history[0].get("role") == "system":
            return history[1:]
        return history

    def _effective_toolsets(self, job: SchedulerJob) -> list[str]:
        """Filter persisted jobs again at the final scheduler execution boundary."""
        effective: list[str] = []
        for raw_name in job.enabled_toolsets or []:
            name = str(raw_name or "").strip()
            if not name or name in effective:
                continue
            if validate_toolset(name, allow_platform=False, allow_scoped=False):
                effective.append(name)
        return effective or ["mclaw-required"]

    def _make_agent(
        self,
        *,
        job: SchedulerJob,
        run: SchedulerRun,
        session_id: str,
        enabled_toolsets: list[str] | None = None,
    ) -> Any:
        """Construct the agent with scheduler-scoped platform and tool settings."""
        provider_runtime = self._runtime_for_session(session_id)
        effective_toolsets = enabled_toolsets or self._effective_toolsets(job)
        if self.agent_factory is not None:
            return self.agent_factory(
                provider_runtime=provider_runtime,
                session_db=self.session_db,
                session_id=session_id,
                enabled_toolsets=effective_toolsets,
                max_iterations=job.max_iterations,
                platform="scheduler",
                workspace=job.workdir or None,
                config=self.config,
                print_fn=self.print_fn,
            )
        from mclaw.agent.core import MClaw

        return MClaw(
            provider_runtime=provider_runtime,
            session_db=self.session_db,
            session_id=session_id,
            enabled_toolsets=effective_toolsets,
            max_iterations=job.max_iterations,
            platform="scheduler",
            workspace=job.workdir or None,
            config=self.config,
            print_fn=self.print_fn,
        )

    def _runtime_for_session(self, session_id: str) -> ProviderRuntimeContext:
        """Restore a persisted session or bootstrap it from the startup context."""
        snapshot = self.session_db.get_model_config(session_id)
        row = self.session_db.get_session(session_id) or {}
        return restore_session_runtime_context(
            snapshot,
            config=self.config,
            row_model=str(row.get("model") or ""),
            fallback_context=self.startup_provider_runtime,
        )

    def _scheduler_context(
        self,
        job: SchedulerJob,
        run: SchedulerRun,
        *,
        enabled_toolsets: list[str] | None = None,
    ) -> str:
        """Build extra system context that identifies the scheduler run."""
        scheduled_for = "-"
        if run.scheduled_for is not None:
            scheduled_for = datetime.fromtimestamp(float(run.scheduled_for), timezone.utc).isoformat()
        return "\n".join(
            [
                "[MClaw Scheduler Run]",
                f"job_id: {job.id}",
                f"job_name: {job.name}",
                f"run_id: {run.id}",
                f"scheduled_for: {scheduled_for}",
                f"workdir: {job.workdir}",
                f"enabled_toolsets: {', '.join(enabled_toolsets or self._effective_toolsets(job))}",
                f"max_iterations: {job.max_iterations}",
                f"timeout_seconds: {job.timeout_seconds}",
                "[End MClaw Scheduler Run]",
            ]
        )


def _extract_tool_calls(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Collect tool call telemetry from both modern and message-shaped results."""
    calls = [item for item in result.get("tool_calls", []) if isinstance(item, dict)]
    if calls:
        return calls
    for round_event in result.get("assistant_rounds") or []:
        if not isinstance(round_event, dict):
            continue
        for item in round_event.get("tool_calls") or []:
            if isinstance(item, dict):
                calls.append(item)
    if calls:
        return calls
    for msg in result.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        for item in msg.get("tool_calls") or []:
            if isinstance(item, dict):
                calls.append(item)
    return calls


def _extract_token_usage(result: dict[str, Any]) -> dict[str, Any]:
    """Normalize token usage metadata while preserving API call count."""
    raw_usage = result.get("token_usage") or {}
    usage = dict(raw_usage) if isinstance(raw_usage, dict) else {}
    try:
        api_calls = int(result.get("api_calls") or 0)
    except (TypeError, ValueError):
        api_calls = 0
    usage.setdefault("api_calls", api_calls)
    return usage
