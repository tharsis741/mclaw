"""Dataclass models for scheduler jobs, runs, targets, and pairings."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
from typing import Any, Literal

logger = logging.getLogger(__name__)

TriggerType = Literal["once", "daily", "weekly", "monthly", "interval", "cron"]
JobStatus = Literal["idle", "claimed", "running", "paused", "error"]
RunStatus = Literal["queued", "running", "succeeded", "failed", "skipped", "cancelled", "delivery_failed"]
SessionPolicy = Literal["task_thread", "new_session"]
ConcurrencyPolicy = Literal["skip", "queue", "parallel", "replace"]
TargetType = Literal["local", "dingtalk_group", "dingtalk_private", "weixin_private"]
RouteStatus = Literal["ready", "needs_route", "disabled"]
PairingStatus = Literal["waiting", "bound", "expired", "cancelled", "failed"]


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _json_loads(raw: Any, default: Any) -> Any:
    if raw is None or raw == "":
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        parsed = json.loads(str(raw))
    except Exception:
        logger.warning("scheduler: bad JSON field ignored: %r", str(raw)[:120])
        return default
    if isinstance(default, dict) and not isinstance(parsed, dict):
        return default
    if isinstance(default, list) and not isinstance(parsed, list):
        return default
    return parsed


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    try:
        return row[key]
    except Exception:
        return getattr(row, key, default)


def normalize_chat_type(value: str | None) -> str:
    raw = str(value or "").strip().lower()
    if raw == "dm":
        return "private"
    return raw


@dataclass
class ScheduleSpec:
    trigger_type: TriggerType
    expression: str
    timezone: str
    parsed: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "trigger_type": self.trigger_type,
            "expression": self.expression,
            "timezone": self.timezone,
            "parsed": dict(self.parsed or {}),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ScheduleSpec":
        raw = data or {}
        return cls(
            trigger_type=str(raw.get("trigger_type") or raw.get("type") or "once"),  # type: ignore[arg-type]
            expression=str(raw.get("expression") or raw.get("schedule_expr") or ""),
            timezone=str(raw.get("timezone") or "Asia/Shanghai"),
            parsed=dict(raw.get("parsed") or {}),
        )

    def to_row(self) -> dict[str, Any]:
        return {
            "trigger_type": self.trigger_type,
            "schedule_expr": self.expression,
            "schedule_parsed_json": _json_dumps(self.parsed or {}),
            "timezone": self.timezone,
        }

    @classmethod
    def from_row(cls, row: Any) -> "ScheduleSpec":
        return cls(
            trigger_type=str(_row_get(row, "trigger_type", "once")),  # type: ignore[arg-type]
            expression=str(_row_get(row, "schedule_expr", "") or ""),
            timezone=str(_row_get(row, "timezone", "Asia/Shanghai") or "Asia/Shanghai"),
            parsed=_json_loads(_row_get(row, "schedule_parsed_json", "{}"), {}),
        )


@dataclass
class SchedulerTarget:
    id: str
    type: TargetType
    display_name: str
    account_id: str = ""
    chat_id: str = ""
    chat_type: str = ""
    route_metadata: dict[str, Any] = field(default_factory=dict)
    capabilities: dict[str, Any] = field(default_factory=dict)
    route_status: RouteStatus = "ready"
    source: str = "binding"
    first_seen_at: float = 0
    last_seen_at: float = 0
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "display_name": self.display_name,
            "account_id": self.account_id,
            "chat_id": self.chat_id,
            "chat_type": normalize_chat_type(self.chat_type),
            "route_metadata": dict(self.route_metadata or {}),
            "capabilities": dict(self.capabilities or {}),
            "route_status": self.route_status,
            "source": self.source,
            "first_seen_at": self.first_seen_at,
            "last_seen_at": self.last_seen_at,
            "enabled": bool(self.enabled),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "SchedulerTarget":
        raw = data or {}
        return cls(
            id=str(raw.get("id") or ""),
            type=str(raw.get("type") or "local"),  # type: ignore[arg-type]
            display_name=str(raw.get("display_name") or ""),
            account_id=str(raw.get("account_id") or ""),
            chat_id=str(raw.get("chat_id") or ""),
            chat_type=normalize_chat_type(raw.get("chat_type")),
            route_metadata=dict(raw.get("route_metadata") or {}),
            capabilities=dict(raw.get("capabilities") or {}),
            route_status=str(raw.get("route_status") or "ready"),  # type: ignore[arg-type]
            source=str(raw.get("source") or "binding"),
            first_seen_at=float(raw.get("first_seen_at") or 0),
            last_seen_at=float(raw.get("last_seen_at") or 0),
            enabled=bool(raw.get("enabled", True)),
        )

    def to_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "display_name": self.display_name,
            "account_id": self.account_id,
            "chat_id": self.chat_id,
            "chat_type": normalize_chat_type(self.chat_type),
            "route_metadata_json": _json_dumps(self.route_metadata or {}),
            "capabilities_json": _json_dumps(self.capabilities or {}),
            "route_status": self.route_status,
            "source": self.source,
            "first_seen_at": float(self.first_seen_at or 0),
            "last_seen_at": float(self.last_seen_at or 0),
            "enabled": 1 if self.enabled else 0,
        }

    @classmethod
    def from_row(cls, row: Any) -> "SchedulerTarget":
        return cls(
            id=str(_row_get(row, "id", "") or ""),
            type=str(_row_get(row, "type", "local")),  # type: ignore[arg-type]
            display_name=str(_row_get(row, "display_name", "") or ""),
            account_id=str(_row_get(row, "account_id", "") or ""),
            chat_id=str(_row_get(row, "chat_id", "") or ""),
            chat_type=normalize_chat_type(_row_get(row, "chat_type", "")),
            route_metadata=_json_loads(_row_get(row, "route_metadata_json", "{}"), {}),
            capabilities=_json_loads(_row_get(row, "capabilities_json", "{}"), {}),
            route_status=str(_row_get(row, "route_status", "ready")),  # type: ignore[arg-type]
            source=str(_row_get(row, "source", "binding") or "binding"),
            first_seen_at=float(_row_get(row, "first_seen_at", 0) or 0),
            last_seen_at=float(_row_get(row, "last_seen_at", 0) or 0),
            enabled=bool(_row_get(row, "enabled", 1)),
        )


@dataclass
class SchedulerTargetPairing:
    code: str
    requested_type: TargetType
    status: PairingStatus
    display_name_hint: str = ""
    target_id: str = ""
    error: str = ""
    expires_at: float = 0
    created_at: float = 0
    updated_at: float = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "requested_type": self.requested_type,
            "status": self.status,
            "display_name_hint": self.display_name_hint,
            "target_id": self.target_id,
            "error": self.error,
            "expires_at": self.expires_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "SchedulerTargetPairing":
        raw = data or {}
        return cls(
            code=str(raw.get("code") or ""),
            requested_type=str(raw.get("requested_type") or "local"),  # type: ignore[arg-type]
            status=str(raw.get("status") or "waiting"),  # type: ignore[arg-type]
            display_name_hint=str(raw.get("display_name_hint") or ""),
            target_id=str(raw.get("target_id") or ""),
            error=str(raw.get("error") or ""),
            expires_at=float(raw.get("expires_at") or 0),
            created_at=float(raw.get("created_at") or 0),
            updated_at=float(raw.get("updated_at") or 0),
        )

    def to_row(self) -> dict[str, Any]:
        return self.to_dict()

    @classmethod
    def from_row(cls, row: Any) -> "SchedulerTargetPairing":
        return cls(
            code=str(_row_get(row, "code", "") or ""),
            requested_type=str(_row_get(row, "requested_type", "local")),  # type: ignore[arg-type]
            status=str(_row_get(row, "status", "waiting")),  # type: ignore[arg-type]
            display_name_hint=str(_row_get(row, "display_name_hint", "") or ""),
            target_id=str(_row_get(row, "target_id", "") or ""),
            error=str(_row_get(row, "error", "") or ""),
            expires_at=float(_row_get(row, "expires_at", 0) or 0),
            created_at=float(_row_get(row, "created_at", 0) or 0),
            updated_at=float(_row_get(row, "updated_at", 0) or 0),
        )


@dataclass
class DeliverySpec:
    target_id: str
    retry_count: int = 3
    final_response_path: str = ""
    final_response_filename_template: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_id": self.target_id,
            "retry_count": int(self.retry_count),
            "final_response_path": self.final_response_path,
            "final_response_filename_template": self.final_response_filename_template,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "DeliverySpec":
        raw = data or {}
        return cls(
            target_id=str(raw.get("target_id") or "local"),
            retry_count=int(raw.get("retry_count") or 3),
            final_response_path=str(raw.get("final_response_path") or ""),
            final_response_filename_template=str(raw.get("final_response_filename_template") or ""),
        )

    def to_row(self) -> dict[str, Any]:
        return self.to_dict()

    @classmethod
    def from_row(cls, row: Any) -> "DeliverySpec":
        if isinstance(row, dict):
            return cls.from_dict(row)
        return cls(
            target_id=str(_row_get(row, "target_id", "local") or "local"),
            retry_count=int(_row_get(row, "retry_count", 3) or 3),
            final_response_path=str(_row_get(row, "final_response_path", "") or ""),
            final_response_filename_template=str(_row_get(row, "final_response_filename_template", "") or ""),
        )


@dataclass
class SchedulerJob:
    id: str
    name: str
    enabled: bool
    schedule: ScheduleSpec
    prompt: str
    enabled_toolsets: list[str]
    workdir: str
    session_policy: SessionPolicy
    session_id: str
    delivery: DeliverySpec
    max_iterations: int
    timeout_seconds: int
    concurrency_policy: ConcurrencyPolicy
    next_run_at: float | None
    last_run_at: float | None
    failure_count: int
    created_at: float
    updated_at: float
    status: JobStatus = "idle"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "enabled": bool(self.enabled),
            "schedule": self.schedule.to_dict(),
            "prompt": self.prompt,
            "enabled_toolsets": list(self.enabled_toolsets or []),
            "workdir": self.workdir,
            "session_policy": self.session_policy,
            "session_id": self.session_id,
            "delivery": self.delivery.to_dict(),
            "max_iterations": int(self.max_iterations),
            "timeout_seconds": int(self.timeout_seconds),
            "concurrency_policy": self.concurrency_policy,
            "next_run_at": self.next_run_at,
            "last_run_at": self.last_run_at,
            "failure_count": int(self.failure_count),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "SchedulerJob":
        raw = data or {}
        return cls(
            id=str(raw.get("id") or ""),
            name=str(raw.get("name") or ""),
            enabled=bool(raw.get("enabled", True)),
            schedule=ScheduleSpec.from_dict(raw.get("schedule")),
            prompt=str(raw.get("prompt") or ""),
            enabled_toolsets=[str(item) for item in (raw.get("enabled_toolsets") or [])],
            workdir=str(raw.get("workdir") or ""),
            session_policy=str(raw.get("session_policy") or "task_thread"),  # type: ignore[arg-type]
            session_id=str(raw.get("session_id") or ""),
            delivery=DeliverySpec.from_dict(raw.get("delivery")),
            max_iterations=int(raw.get("max_iterations") or 200),
            timeout_seconds=int(raw.get("timeout_seconds") or 3600),
            concurrency_policy=str(raw.get("concurrency_policy") or "skip"),  # type: ignore[arg-type]
            next_run_at=raw.get("next_run_at"),
            last_run_at=raw.get("last_run_at"),
            failure_count=int(raw.get("failure_count") or 0),
            created_at=float(raw.get("created_at") or 0),
            updated_at=float(raw.get("updated_at") or 0),
            status=str(raw.get("status") or "idle"),  # type: ignore[arg-type]
        )

    def to_row(self) -> dict[str, Any]:
        row = {
            "id": self.id,
            "name": self.name,
            "enabled": 1 if self.enabled else 0,
            "next_run_at": self.next_run_at,
            "last_run_at": self.last_run_at,
            "status": self.status,
            "prompt": self.prompt,
            "enabled_toolsets_json": _json_dumps(self.enabled_toolsets or []),
            "workdir": self.workdir,
            "session_policy": self.session_policy,
            "session_id": self.session_id,
            "delivery_json": _json_dumps(self.delivery.to_dict()),
            "max_iterations": int(self.max_iterations),
            "timeout_seconds": int(self.timeout_seconds),
            "concurrency_policy": self.concurrency_policy,
            "failure_count": int(self.failure_count),
            "created_at": float(self.created_at),
            "updated_at": float(self.updated_at),
        }
        row.update(self.schedule.to_row())
        return row

    @classmethod
    def from_row(cls, row: Any) -> "SchedulerJob":
        return cls(
            id=str(_row_get(row, "id", "") or ""),
            name=str(_row_get(row, "name", "") or ""),
            enabled=bool(_row_get(row, "enabled", 1)),
            schedule=ScheduleSpec.from_row(row),
            prompt=str(_row_get(row, "prompt", "") or ""),
            enabled_toolsets=[str(item) for item in _json_loads(_row_get(row, "enabled_toolsets_json", "[]"), [])],
            workdir=str(_row_get(row, "workdir", "") or ""),
            session_policy=str(_row_get(row, "session_policy", "task_thread")),  # type: ignore[arg-type]
            session_id=str(_row_get(row, "session_id", "") or ""),
            delivery=DeliverySpec.from_dict(_json_loads(_row_get(row, "delivery_json", "{}"), {})),
            max_iterations=int(_row_get(row, "max_iterations", 200) or 200),
            timeout_seconds=int(_row_get(row, "timeout_seconds", 3600) or 3600),
            concurrency_policy=str(_row_get(row, "concurrency_policy", "skip")),  # type: ignore[arg-type]
            next_run_at=_row_get(row, "next_run_at"),
            last_run_at=_row_get(row, "last_run_at"),
            failure_count=int(_row_get(row, "failure_count", 0) or 0),
            created_at=float(_row_get(row, "created_at", 0) or 0),
            updated_at=float(_row_get(row, "updated_at", 0) or 0),
            status=str(_row_get(row, "status", "idle")),  # type: ignore[arg-type]
        )


@dataclass
class SchedulerRun:
    id: str
    job_id: str
    run_no: int
    status: RunStatus
    claimed_by: str
    scheduled_for: float | None
    started_at: float | None
    finished_at: float | None
    session_id: str
    output_path: str
    final_response: str
    error: str
    delivery_result: dict[str, Any]
    token_usage: dict[str, Any]
    tool_calls: list[dict[str, Any]]
    created_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "job_id": self.job_id,
            "run_no": int(self.run_no),
            "status": self.status,
            "claimed_by": self.claimed_by,
            "scheduled_for": self.scheduled_for,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "session_id": self.session_id,
            "output_path": self.output_path,
            "final_response": self.final_response,
            "error": self.error,
            "delivery_result": dict(self.delivery_result or {}),
            "token_usage": dict(self.token_usage or {}),
            "tool_calls": list(self.tool_calls or []),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "SchedulerRun":
        raw = data or {}
        return cls(
            id=str(raw.get("id") or ""),
            job_id=str(raw.get("job_id") or ""),
            run_no=int(raw.get("run_no") or 0),
            status=str(raw.get("status") or "queued"),  # type: ignore[arg-type]
            claimed_by=str(raw.get("claimed_by") or ""),
            scheduled_for=raw.get("scheduled_for"),
            started_at=raw.get("started_at"),
            finished_at=raw.get("finished_at"),
            session_id=str(raw.get("session_id") or ""),
            output_path=str(raw.get("output_path") or ""),
            final_response=str(raw.get("final_response") or ""),
            error=str(raw.get("error") or ""),
            delivery_result=dict(raw.get("delivery_result") or {}),
            token_usage=dict(raw.get("token_usage") or {}),
            tool_calls=list(raw.get("tool_calls") or []),
            created_at=float(raw.get("created_at") or 0),
        )

    def to_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "job_id": self.job_id,
            "run_no": int(self.run_no),
            "status": self.status,
            "claimed_by": self.claimed_by,
            "scheduled_for": self.scheduled_for,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "session_id": self.session_id,
            "output_path": self.output_path,
            "final_response": self.final_response,
            "error": self.error,
            "delivery_result_json": _json_dumps(self.delivery_result or {}),
            "token_usage_json": _json_dumps(self.token_usage or {}),
            "tool_calls_json": _json_dumps(self.tool_calls or []),
            "created_at": float(self.created_at),
        }

    @classmethod
    def from_row(cls, row: Any) -> "SchedulerRun":
        return cls(
            id=str(_row_get(row, "id", "") or ""),
            job_id=str(_row_get(row, "job_id", "") or ""),
            run_no=int(_row_get(row, "run_no", 0) or 0),
            status=str(_row_get(row, "status", "queued")),  # type: ignore[arg-type]
            claimed_by=str(_row_get(row, "claimed_by", "") or ""),
            scheduled_for=_row_get(row, "scheduled_for"),
            started_at=_row_get(row, "started_at"),
            finished_at=_row_get(row, "finished_at"),
            session_id=str(_row_get(row, "session_id", "") or ""),
            output_path=str(_row_get(row, "output_path", "") or ""),
            final_response=str(_row_get(row, "final_response", "") or ""),
            error=str(_row_get(row, "error", "") or ""),
            delivery_result=_json_loads(_row_get(row, "delivery_result_json", "{}"), {}),
            token_usage=_json_loads(_row_get(row, "token_usage_json", "{}"), {}),
            tool_calls=_json_loads(_row_get(row, "tool_calls_json", "[]"), []),
            created_at=float(_row_get(row, "created_at", 0) or 0),
        )
