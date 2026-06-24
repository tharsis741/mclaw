"""Scheduler runner that executes one local MClaw turn."""

from __future__ import annotations

import concurrent.futures
from datetime import datetime, timezone
import logging
import time
import uuid
from typing import Any, Callable

from mclaw.scheduler.formatting import default_output_dir, write_run_output
from mclaw.scheduler.models import SchedulerJob, SchedulerRun
from mclaw.state import SessionDB

logger = logging.getLogger(__name__)


class SchedulerRunner:
    def __init__(
        self,
        *,
        model: str = "",
        api_key: str = "",
        base_url: str = "",
        api_mode: str = "chat_completions",
        provider: str = "",
        config: dict[str, Any] | None = None,
        session_db: SessionDB | None = None,
        store: Any = None,
        output_dir: str | None = None,
        agent_factory: Callable[..., Any] | None = None,
        print_fn: Callable[..., None] | None = None,
    ) -> None:
        self.model = model
        self.api_key = api_key
        self.base_url = base_url
        self.api_mode = api_mode
        self.provider = provider
        self.config = config or {}
        self.session_db = session_db or SessionDB()
        self.store = store
        scheduler_cfg = self.config.get("scheduler", {}) if isinstance(self.config, dict) else {}
        self.output_dir = output_dir or str(scheduler_cfg.get("output_dir") or default_output_dir())
        self.agent_factory = agent_factory
        self.print_fn = print_fn

    def run(self, job: SchedulerJob, run: SchedulerRun) -> SchedulerRun:
        started_at = run.started_at or time.time()
        run.started_at = started_at
        try:
            session_id = self._resolve_session(job, run)
            run.session_id = session_id
            history = self._history_for_session(session_id)
            extra_system = self._scheduler_context(job, run)
            agent_holder: dict[str, Any] = {}

            def _execute() -> dict[str, Any]:
                agent = self._make_agent(job=job, run=run, session_id=session_id)
                agent_holder["agent"] = agent
                return agent.run_conversation(
                    job.prompt,
                    history,
                    False,
                    False,
                    extra_system,
                    False,
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
                future.cancel()
                executor.shutdown(wait=False, cancel_futures=True)
                raise TimeoutError(f"scheduler run timed out after {job.timeout_seconds}s")
            finally:
                if not future.cancelled():
                    executor.shutdown(wait=False, cancel_futures=False)

            run.final_response = str(result.get("final_response") or "")
            run.error = ""
            run.finished_at = time.time()
            run.status = "succeeded" if run.final_response or result.get("completed") else "failed"
            if run.status == "failed":
                run.error = "agent returned no final response"
            run.tool_calls = _extract_tool_calls(result)
            run.token_usage = _extract_token_usage(result)
            run.output_path = write_run_output(output_dir=self.output_dir, job=job, run=run)
            return run
        except Exception as exc:
            run.status = "failed"
            run.error = str(exc)
            run.finished_at = time.time()
            run.output_path = write_run_output(output_dir=self.output_dir, job=job, run=run)
            return run

    def _resolve_session(self, job: SchedulerJob, run: SchedulerRun) -> str:
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
            model=self.model,
            workspace=job.workdir or None,
        )
        return session_id

    def _history_for_session(self, session_id: str) -> list[dict[str, Any]]:
        history = self.session_db.get_messages_as_conversation(session_id)
        if history and history[0].get("role") == "system":
            return history[1:]
        return history

    def _make_agent(self, *, job: SchedulerJob, run: SchedulerRun, session_id: str) -> Any:
        if self.agent_factory is not None:
            return self.agent_factory(
                model=self.model,
                api_key=self.api_key,
                base_url=self.base_url,
                api_mode=self.api_mode,
                provider=self.provider,
                session_db=self.session_db,
                session_id=session_id,
                enabled_toolsets=job.enabled_toolsets,
                max_iterations=job.max_iterations,
                platform="scheduler",
                workspace=job.workdir or None,
                config=self.config,
                print_fn=self.print_fn,
            )
        from mclaw.agent.core import MClaw

        return MClaw(
            model=self.model,
            api_key=self.api_key,
            base_url=self.base_url,
            api_mode=self.api_mode,
            provider=self.provider,
            session_db=self.session_db,
            session_id=session_id,
            enabled_toolsets=job.enabled_toolsets,
            max_iterations=job.max_iterations,
            platform="scheduler",
            workspace=job.workdir or None,
            config=self.config,
            print_fn=self.print_fn,
        )

    def _scheduler_context(self, job: SchedulerJob, run: SchedulerRun) -> str:
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
                f"enabled_toolsets: {', '.join(job.enabled_toolsets or [])}",
                f"max_iterations: {job.max_iterations}",
                f"timeout_seconds: {job.timeout_seconds}",
                "[End MClaw Scheduler Run]",
            ]
        )


def _extract_tool_calls(result: dict[str, Any]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for round_event in result.get("assistant_rounds") or []:
        for item in round_event.get("tool_calls") or []:
            if isinstance(item, dict):
                calls.append(item)
    if calls:
        return calls
    for msg in result.get("messages") or []:
        for item in msg.get("tool_calls") or []:
            if isinstance(item, dict):
                calls.append(item)
    return calls


def _extract_token_usage(result: dict[str, Any]) -> dict[str, Any]:
    usage = dict(result.get("token_usage") or {})
    usage.setdefault("api_calls", int(result.get("api_calls") or 0))
    return usage
