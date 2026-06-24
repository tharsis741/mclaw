"""Markdown formatting for scheduler run outputs."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mclaw.constants import get_mclaw_home
from mclaw.scheduler.models import SchedulerJob, SchedulerRun


def default_output_dir() -> Path:
    return get_mclaw_home() / "scheduler" / "output"


def output_path_for_run(output_dir: str | Path | None, job: SchedulerJob, run: SchedulerRun) -> Path:
    root = Path(output_dir).expanduser() if output_dir else default_output_dir()
    ts = datetime.fromtimestamp(run.started_at or run.created_at, timezone.utc).strftime("%Y%m%d_%H%M%S")
    safe_run_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", run.id)
    return root / job.id / f"{ts}_{safe_run_id}.md"


def final_response_path_for_run(
    path_spec: str | Path,
    job: SchedulerJob,
    run: SchedulerRun,
    *,
    filename_template: str = "",
) -> Path:
    raw = str(path_spec or "").strip()
    if not raw:
        raise ValueError("final_response_path is empty")
    expanded = os.path.expandvars(raw)
    path = Path(expanded).expanduser()
    if not path.is_absolute():
        base = Path(job.workdir or ".").expanduser()
        path = base / path
    if filename_template:
        return path / _render_final_response_filename(filename_template, job=job, run=run)
    explicit_dir = raw.endswith(("/", "\\"))
    if explicit_dir or (path.exists() and path.is_dir()) or not path.suffix:
        ts = datetime.fromtimestamp(run.started_at or run.created_at, timezone.utc).strftime("%Y%m%d_%H%M%S")
        safe_run_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", run.id)
        path = path / f"{ts}_{safe_run_id}_final.md"
    return path


def write_run_output(
    *,
    output_dir: str | Path | None,
    job: SchedulerJob,
    run: SchedulerRun,
) -> str:
    path = output_path_for_run(output_dir, job, run)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_run_output(job=job, run=run), encoding="utf-8")
    return str(path)


def write_final_response_output(
    *,
    path_spec: str | Path,
    job: SchedulerJob,
    run: SchedulerRun,
    filename_template: str = "",
) -> str:
    path = final_response_path_for_run(path_spec, job, run, filename_template=filename_template)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(run.final_response or "", encoding="utf-8")
    return str(path)


def render_run_output(*, job: SchedulerJob, run: SchedulerRun) -> str:
    lines = [
        "# Scheduler Run",
        "",
        f"- job_id: {job.id}",
        f"- job_name: {job.name}",
        f"- run_id: {run.id}",
        f"- status: {run.status}",
        f"- scheduled_for: {_fmt_ts(run.scheduled_for)}",
        f"- started_at: {_fmt_ts(run.started_at)}",
        f"- finished_at: {_fmt_ts(run.finished_at)}",
        f"- session_id: {run.session_id}",
        f"- max_iterations: {job.max_iterations}",
        f"- timeout_seconds: {job.timeout_seconds}",
        f"- delivery: {_delivery_summary(run.delivery_result)}",
        f"- final_response_output: {_final_response_output_summary(run.delivery_result)}",
        "",
        "## Prompt",
        "",
        job.prompt or "",
        "",
        "## Final Response",
        "",
        run.final_response or "",
        "",
        "## Tool Calls",
        "",
        _json_block(run.tool_calls or []),
        "",
        "## Delivery Result",
        "",
        _json_block(run.delivery_result or {}),
        "",
        "## Error",
        "",
        run.error or "-",
        "",
    ]
    return "\n".join(lines)


def _fmt_ts(value: float | None) -> str:
    if value is None:
        return "-"
    return datetime.fromtimestamp(float(value), timezone.utc).isoformat()


def _json_block(value: Any) -> str:
    return "```json\n" + json.dumps(value, ensure_ascii=False, indent=2) + "\n```"


def _delivery_summary(result: dict[str, Any]) -> str:
    if not result:
        return "-"
    if result.get("success"):
        return str(result.get("status") or "sent")
    return str(result.get("error") or result.get("status") or "failed")


def _final_response_output_summary(result: dict[str, Any]) -> str:
    if not result:
        return "-"
    saved = result.get("final_response_output")
    if not isinstance(saved, dict):
        return "-"
    if saved.get("success"):
        return str(saved.get("path") or "-")
    return str(saved.get("error") or "failed")


def _render_final_response_filename(template: str, *, job: SchedulerJob, run: SchedulerRun) -> str:
    raw = str(template or "").strip()
    if not raw:
        raise ValueError("final_response_filename_template is empty")
    dt = _template_datetime(job=job, run=run)

    def _date_match(match: re.Match[str]) -> str:
        fmt = match.group(1).strip() or "%Y-%m-%d"
        return dt.strftime(fmt)

    rendered = re.sub(r"\{date(?::([^}]+))?\}", _date_match, raw)
    replacements = {
        "yyyy": dt.strftime("%Y"),
        "YYYY": dt.strftime("%Y"),
        "yy": dt.strftime("%y"),
        "YY": dt.strftime("%y"),
        "mm": dt.strftime("%m"),
        "MM": dt.strftime("%m"),
        "dd": dt.strftime("%d"),
        "DD": dt.strftime("%d"),
        "{job_id}": job.id,
        "{run_id}": run.id,
        "{run_no}": str(run.run_no),
    }
    for token, value in replacements.items():
        rendered = rendered.replace(token, value)
    rendered = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", rendered).strip(" .")
    if not rendered:
        raise ValueError("final_response_filename_template rendered empty filename")
    if not Path(rendered).suffix:
        rendered += ".md"
    return rendered


def _template_datetime(*, job: SchedulerJob, run: SchedulerRun) -> datetime:
    timestamp = run.scheduled_for if run.scheduled_for is not None else (run.started_at or run.created_at)
    tz_name = getattr(getattr(job, "schedule", None), "timezone", "") or "UTC"
    try:
        tz = ZoneInfo(str(tz_name))
    except ZoneInfoNotFoundError:
        tz = timezone.utc
    return datetime.fromtimestamp(float(timestamp or 0), tz)
