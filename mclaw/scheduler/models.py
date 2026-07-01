# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Dataclass models for scheduler jobs, runs, targets, and pairings.

These classes define the stable scheduler state shape shared by CLI commands,
channel adapters, and the persistence layer. Coercion is centralized here so
JSON imports and SQLite rows reject malformed state in the same way.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
from typing import Any, Literal
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

TriggerType = Literal["once", "daily", "weekly", "monthly", "interval", "cron"]
JobStatus = Literal["idle", "claimed", "running", "paused", "error"]
RunStatus = Literal["queued", "running", "succeeded", "failed", "skipped", "cancelled", "delivery_failed"]
SessionPolicy = Literal["task_thread", "new_session"]
ConcurrencyPolicy = Literal["skip", "queue", "parallel", "replace"]
TargetType = Literal["local", "dingtalk_group", "dingtalk_private", "weixin_private"]
RouteStatus = Literal["ready", "needs_route", "disabled"]
PairingStatus = Literal["waiting", "bound", "expired", "cancelled", "failed"]

_TRIGGER_TYPES = frozenset(("once", "daily", "weekly", "monthly", "interval", "cron"))
_JOB_STATUSES = frozenset(("idle", "claimed", "running", "paused", "error"))
_RUN_STATUSES = frozenset(("queued", "running", "succeeded", "failed", "skipped", "cancelled", "delivery_failed"))
_SESSION_POLICIES = frozenset(("task_thread", "new_session"))
_CONCURRENCY_POLICIES = frozenset(("skip", "queue", "parallel", "replace"))
_TARGET_TYPES = frozenset(("local", "dingtalk_group", "dingtalk_private", "weixin_private"))
_ROUTE_STATUSES = frozenset(("ready", "needs_route", "disabled"))
_PAIRING_STATUSES = frozenset(("waiting", "bound", "expired", "cancelled", "failed"))


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


def _mapping(value: Any, *, field: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return value


def _coerce_enum(value: Any, allowed: frozenset[str], default: str, *, field: str) -> str:
    text = str(default if value is None or value == "" else value).strip()
    if text not in allowed:
        allowed_text = ", ".join(sorted(allowed))
        raise ValueError(f"{field} must be one of: {allowed_text}")
    return text


def _coerce_bool(value: Any, default: bool, *, field: str) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if value in (0, 1):
            return bool(value)
        raise ValueError(f"{field} must be a boolean")
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "y", "on"}:
            return True
        if text in {"0", "false", "no", "n", "off"}:
            return False
    raise ValueError(f"{field} must be a boolean")


def _coerce_int(value: Any, default: int, *, field: str, min_value: int | None = None) -> int:
    if value is None or value == "":
        result = default
    elif isinstance(value, bool):
        raise ValueError(f"{field} must be an integer")
    else:
        try:
            result = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field} must be an integer") from exc
    if min_value is not None and result < min_value:
        raise ValueError(f"{field} must be >= {min_value}")
    return result


def _coerce_float(value: Any, default: float, *, field: str, min_value: float | None = None) -> float:
    if value is None or value == "":
        result = float(default)
    elif isinstance(value, bool):
        raise ValueError(f"{field} must be a number")
    else:
        try:
            result = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field} must be a number") from exc
    if min_value is not None and result < min_value:
        raise ValueError(f"{field} must be >= {min_value:g}")
    return result


def _coerce_optional_float(value: Any, *, field: str, min_value: float | None = None) -> float | None:
    if value is None or value == "":
        return None
    return _coerce_float(value, 0, field=field, min_value=min_value)


def _coerce_mapping(value: Any, *, field: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return dict(value)


def _coerce_list(value: Any, *, field: str) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise ValueError(f"{field} must be a list")
    return list(value)


def _coerce_string_list(value: Any, *, field: str) -> list[str]:
    return [str(item) for item in _coerce_list(value, field=field)]


def _coerce_dict_list(value: Any, *, field: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for index, item in enumerate(_coerce_list(value, field=field)):
        if not isinstance(item, dict):
            raise ValueError(f"{field}[{index}] must be an object")
        result.append(dict(item))
    return result


def _coerce_timezone(value: Any, default: str, *, field: str) -> str:
    text = str(default if value is None or value == "" else value).strip()
    try:
        ZoneInfo(text)
    except Exception as exc:
        raise ValueError(f"{field} must be a valid IANA timezone") from exc
    return text


def normalize_chat_type(value: str | None) -> str:
    """Return the canonical chat scope name used by scheduler target routing."""
    raw = str(value or "").strip().lower()
    if raw == "dm":
        return "private"
    return raw


@dataclass
class ScheduleSpec:
    """Serializable trigger configuration after user input has been parsed."""

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
        raw = _mapping(data, field="schedule")
        if "type" in raw or "schedule_expr" in raw:
            raise ValueError("schedule dict must use trigger_type and expression")
        return cls(
            trigger_type=_coerce_enum(raw.get("trigger_type"), _TRIGGER_TYPES, "once", field="schedule.trigger_type"),  # type: ignore[arg-type]
            expression=str(raw.get("expression") or ""),
            timezone=_coerce_timezone(raw.get("timezone"), "Asia/Shanghai", field="schedule.timezone"),
            parsed=_coerce_mapping(raw.get("parsed"), field="schedule.parsed"),
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
            trigger_type=_coerce_enum(_row_get(row, "trigger_type", "once"), _TRIGGER_TYPES, "once", field="schedule.trigger_type"),  # type: ignore[arg-type]
            expression=str(_row_get(row, "schedule_expr", "") or ""),
            timezone=_coerce_timezone(_row_get(row, "timezone", "Asia/Shanghai"), "Asia/Shanghai", field="schedule.timezone"),
            parsed=_coerce_mapping(_json_loads(_row_get(row, "schedule_parsed_json", "{}"), {}), field="schedule.parsed"),
        )


@dataclass
class SchedulerTarget:
    """Delivery destination known to the scheduler and outbound adapters."""

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
        raw = _mapping(data, field="target")
        return cls(
            id=str(raw.get("id") or ""),
            type=_coerce_enum(raw.get("type"), _TARGET_TYPES, "local", field="target.type"),  # type: ignore[arg-type]
            display_name=str(raw.get("display_name") or ""),
            account_id=str(raw.get("account_id") or ""),
            chat_id=str(raw.get("chat_id") or ""),
            chat_type=normalize_chat_type(raw.get("chat_type")),
            route_metadata=_coerce_mapping(raw.get("route_metadata"), field="target.route_metadata"),
            capabilities=_coerce_mapping(raw.get("capabilities"), field="target.capabilities"),
            route_status=_coerce_enum(raw.get("route_status"), _ROUTE_STATUSES, "ready", field="target.route_status"),  # type: ignore[arg-type]
            source=str(raw.get("source") or "binding"),
            first_seen_at=_coerce_float(raw.get("first_seen_at"), 0, field="target.first_seen_at", min_value=0),
            last_seen_at=_coerce_float(raw.get("last_seen_at"), 0, field="target.last_seen_at", min_value=0),
            enabled=_coerce_bool(raw.get("enabled"), True, field="target.enabled"),
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
            type=_coerce_enum(_row_get(row, "type", "local"), _TARGET_TYPES, "local", field="target.type"),  # type: ignore[arg-type]
            display_name=str(_row_get(row, "display_name", "") or ""),
            account_id=str(_row_get(row, "account_id", "") or ""),
            chat_id=str(_row_get(row, "chat_id", "") or ""),
            chat_type=normalize_chat_type(_row_get(row, "chat_type", "")),
            route_metadata=_coerce_mapping(_json_loads(_row_get(row, "route_metadata_json", "{}"), {}), field="target.route_metadata"),
            capabilities=_coerce_mapping(_json_loads(_row_get(row, "capabilities_json", "{}"), {}), field="target.capabilities"),
            route_status=_coerce_enum(_row_get(row, "route_status", "ready"), _ROUTE_STATUSES, "ready", field="target.route_status"),  # type: ignore[arg-type]
            source=str(_row_get(row, "source", "binding") or "binding"),
            first_seen_at=_coerce_float(_row_get(row, "first_seen_at", 0), 0, field="target.first_seen_at", min_value=0),
            last_seen_at=_coerce_float(_row_get(row, "last_seen_at", 0), 0, field="target.last_seen_at", min_value=0),
            enabled=_coerce_bool(_row_get(row, "enabled", 1), True, field="target.enabled"),
        )


@dataclass
class SchedulerTargetPairing:
    """Short-lived channel binding request used before a target is routable."""

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
        raw = _mapping(data, field="pairing")
        return cls(
            code=str(raw.get("code") or ""),
            requested_type=_coerce_enum(raw.get("requested_type"), _TARGET_TYPES, "local", field="pairing.requested_type"),  # type: ignore[arg-type]
            status=_coerce_enum(raw.get("status"), _PAIRING_STATUSES, "waiting", field="pairing.status"),  # type: ignore[arg-type]
            display_name_hint=str(raw.get("display_name_hint") or ""),
            target_id=str(raw.get("target_id") or ""),
            error=str(raw.get("error") or ""),
            expires_at=_coerce_float(raw.get("expires_at"), 0, field="pairing.expires_at", min_value=0),
            created_at=_coerce_float(raw.get("created_at"), 0, field="pairing.created_at", min_value=0),
            updated_at=_coerce_float(raw.get("updated_at"), 0, field="pairing.updated_at", min_value=0),
        )

    def to_row(self) -> dict[str, Any]:
        return self.to_dict()

    @classmethod
    def from_row(cls, row: Any) -> "SchedulerTargetPairing":
        return cls(
            code=str(_row_get(row, "code", "") or ""),
            requested_type=_coerce_enum(_row_get(row, "requested_type", "local"), _TARGET_TYPES, "local", field="pairing.requested_type"),  # type: ignore[arg-type]
            status=_coerce_enum(_row_get(row, "status", "waiting"), _PAIRING_STATUSES, "waiting", field="pairing.status"),  # type: ignore[arg-type]
            display_name_hint=str(_row_get(row, "display_name_hint", "") or ""),
            target_id=str(_row_get(row, "target_id", "") or ""),
            error=str(_row_get(row, "error", "") or ""),
            expires_at=_coerce_float(_row_get(row, "expires_at", 0), 0, field="pairing.expires_at", min_value=0),
            created_at=_coerce_float(_row_get(row, "created_at", 0), 0, field="pairing.created_at", min_value=0),
            updated_at=_coerce_float(_row_get(row, "updated_at", 0), 0, field="pairing.updated_at", min_value=0),
        )


@dataclass
class DeliverySpec:
    """Delivery and optional final-response export settings for a job."""

    target_id: str
    retry_count: int = 3
    final_response_path: str = ""
    final_response_filename_template: str = ""

    def __post_init__(self) -> None:
        self.target_id = str(self.target_id or "local")
        self.retry_count = _coerce_int(self.retry_count, 3, field="delivery.retry_count", min_value=1)
        self.final_response_path = str(self.final_response_path or "")
        self.final_response_filename_template = str(self.final_response_filename_template or "")

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_id": self.target_id,
            "retry_count": int(self.retry_count),
            "final_response_path": self.final_response_path,
            "final_response_filename_template": self.final_response_filename_template,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "DeliverySpec":
        raw = _mapping(data, field="delivery")
        return cls(
            target_id=str(raw.get("target_id") or "local"),
            retry_count=_coerce_int(raw.get("retry_count"), 3, field="delivery.retry_count", min_value=1),
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
            retry_count=_coerce_int(_row_get(row, "retry_count", 3), 3, field="delivery.retry_count", min_value=1),
            final_response_path=str(_row_get(row, "final_response_path", "") or ""),
            final_response_filename_template=str(_row_get(row, "final_response_filename_template", "") or ""),
        )


@dataclass
class SchedulerJob:
    """Persisted recurring task definition plus scheduler-owned runtime state."""

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
        raw = _mapping(data, field="job")
        return cls(
            id=str(raw.get("id") or ""),
            name=str(raw.get("name") or ""),
            enabled=_coerce_bool(raw.get("enabled"), True, field="job.enabled"),
            schedule=ScheduleSpec.from_dict(raw.get("schedule")),
            prompt=str(raw.get("prompt") or ""),
            enabled_toolsets=_coerce_string_list(raw.get("enabled_toolsets"), field="job.enabled_toolsets"),
            workdir=str(raw.get("workdir") or ""),
            session_policy=_coerce_enum(raw.get("session_policy"), _SESSION_POLICIES, "task_thread", field="job.session_policy"),  # type: ignore[arg-type]
            session_id=str(raw.get("session_id") or ""),
            delivery=DeliverySpec.from_dict(raw.get("delivery")),
            max_iterations=_coerce_int(raw.get("max_iterations"), 200, field="job.max_iterations", min_value=1),
            timeout_seconds=_coerce_int(raw.get("timeout_seconds"), 3600, field="job.timeout_seconds", min_value=1),
            concurrency_policy=_coerce_enum(raw.get("concurrency_policy"), _CONCURRENCY_POLICIES, "skip", field="job.concurrency_policy"),  # type: ignore[arg-type]
            next_run_at=_coerce_optional_float(raw.get("next_run_at"), field="job.next_run_at", min_value=0),
            last_run_at=_coerce_optional_float(raw.get("last_run_at"), field="job.last_run_at", min_value=0),
            failure_count=_coerce_int(raw.get("failure_count"), 0, field="job.failure_count", min_value=0),
            created_at=_coerce_float(raw.get("created_at"), 0, field="job.created_at", min_value=0),
            updated_at=_coerce_float(raw.get("updated_at"), 0, field="job.updated_at", min_value=0),
            status=_coerce_enum(raw.get("status"), _JOB_STATUSES, "idle", field="job.status"),  # type: ignore[arg-type]
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
            enabled=_coerce_bool(_row_get(row, "enabled", 1), True, field="job.enabled"),
            schedule=ScheduleSpec.from_row(row),
            prompt=str(_row_get(row, "prompt", "") or ""),
            enabled_toolsets=_coerce_string_list(_json_loads(_row_get(row, "enabled_toolsets_json", "[]"), []), field="job.enabled_toolsets"),
            workdir=str(_row_get(row, "workdir", "") or ""),
            session_policy=_coerce_enum(_row_get(row, "session_policy", "task_thread"), _SESSION_POLICIES, "task_thread", field="job.session_policy"),  # type: ignore[arg-type]
            session_id=str(_row_get(row, "session_id", "") or ""),
            delivery=DeliverySpec.from_dict(_json_loads(_row_get(row, "delivery_json", "{}"), {})),
            max_iterations=_coerce_int(_row_get(row, "max_iterations", 200), 200, field="job.max_iterations", min_value=1),
            timeout_seconds=_coerce_int(_row_get(row, "timeout_seconds", 3600), 3600, field="job.timeout_seconds", min_value=1),
            concurrency_policy=_coerce_enum(_row_get(row, "concurrency_policy", "skip"), _CONCURRENCY_POLICIES, "skip", field="job.concurrency_policy"),  # type: ignore[arg-type]
            next_run_at=_coerce_optional_float(_row_get(row, "next_run_at"), field="job.next_run_at", min_value=0),
            last_run_at=_coerce_optional_float(_row_get(row, "last_run_at"), field="job.last_run_at", min_value=0),
            failure_count=_coerce_int(_row_get(row, "failure_count", 0), 0, field="job.failure_count", min_value=0),
            created_at=_coerce_float(_row_get(row, "created_at", 0), 0, field="job.created_at", min_value=0),
            updated_at=_coerce_float(_row_get(row, "updated_at", 0), 0, field="job.updated_at", min_value=0),
            status=_coerce_enum(_row_get(row, "status", "idle"), _JOB_STATUSES, "idle", field="job.status"),  # type: ignore[arg-type]
        )


@dataclass
class SchedulerRun:
    """Single execution attempt and its captured result metadata."""

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
        raw = _mapping(data, field="run")
        return cls(
            id=str(raw.get("id") or ""),
            job_id=str(raw.get("job_id") or ""),
            run_no=_coerce_int(raw.get("run_no"), 0, field="run.run_no", min_value=0),
            status=_coerce_enum(raw.get("status"), _RUN_STATUSES, "queued", field="run.status"),  # type: ignore[arg-type]
            claimed_by=str(raw.get("claimed_by") or ""),
            scheduled_for=_coerce_optional_float(raw.get("scheduled_for"), field="run.scheduled_for", min_value=0),
            started_at=_coerce_optional_float(raw.get("started_at"), field="run.started_at", min_value=0),
            finished_at=_coerce_optional_float(raw.get("finished_at"), field="run.finished_at", min_value=0),
            session_id=str(raw.get("session_id") or ""),
            output_path=str(raw.get("output_path") or ""),
            final_response=str(raw.get("final_response") or ""),
            error=str(raw.get("error") or ""),
            delivery_result=_coerce_mapping(raw.get("delivery_result"), field="run.delivery_result"),
            token_usage=_coerce_mapping(raw.get("token_usage"), field="run.token_usage"),
            tool_calls=_coerce_dict_list(raw.get("tool_calls"), field="run.tool_calls"),
            created_at=_coerce_float(raw.get("created_at"), 0, field="run.created_at", min_value=0),
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
            run_no=_coerce_int(_row_get(row, "run_no", 0), 0, field="run.run_no", min_value=0),
            status=_coerce_enum(_row_get(row, "status", "queued"), _RUN_STATUSES, "queued", field="run.status"),  # type: ignore[arg-type]
            claimed_by=str(_row_get(row, "claimed_by", "") or ""),
            scheduled_for=_coerce_optional_float(_row_get(row, "scheduled_for"), field="run.scheduled_for", min_value=0),
            started_at=_coerce_optional_float(_row_get(row, "started_at"), field="run.started_at", min_value=0),
            finished_at=_coerce_optional_float(_row_get(row, "finished_at"), field="run.finished_at", min_value=0),
            session_id=str(_row_get(row, "session_id", "") or ""),
            output_path=str(_row_get(row, "output_path", "") or ""),
            final_response=str(_row_get(row, "final_response", "") or ""),
            error=str(_row_get(row, "error", "") or ""),
            delivery_result=_coerce_mapping(_json_loads(_row_get(row, "delivery_result_json", "{}"), {}), field="run.delivery_result"),
            token_usage=_coerce_mapping(_json_loads(_row_get(row, "token_usage_json", "{}"), {}), field="run.token_usage"),
            tool_calls=_coerce_dict_list(_json_loads(_row_get(row, "tool_calls_json", "[]"), []), field="run.tool_calls"),
            created_at=_coerce_float(_row_get(row, "created_at", 0), 0, field="run.created_at", min_value=0),
        )
