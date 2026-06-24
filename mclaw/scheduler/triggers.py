"""Schedule parsing and next-run calculation."""

from __future__ import annotations

import calendar
from datetime import datetime, timedelta
import re
from typing import Any
from zoneinfo import ZoneInfo

from mclaw.scheduler.errors import ScheduleParseError
from mclaw.scheduler.models import ScheduleSpec

_WEEKDAYS = {
    "monday": 0,
    "mon": 0,
    "tuesday": 1,
    "tue": 1,
    "wednesday": 2,
    "wed": 2,
    "thursday": 3,
    "thu": 3,
    "friday": 4,
    "fri": 4,
    "saturday": 5,
    "sat": 5,
    "sunday": 6,
    "sun": 6,
}


def parse_schedule(raw: dict[str, Any], *, default_timezone: str) -> ScheduleSpec:
    data = raw or {}
    trigger_type = str(data.get("type") or data.get("trigger_type") or "").strip().lower()
    timezone = str(data.get("timezone") or default_timezone or "Asia/Shanghai").strip()
    _zone(timezone)
    if trigger_type not in {"once", "daily", "weekly", "monthly", "interval", "cron"}:
        raise ScheduleParseError(f"unsupported trigger type: {trigger_type or '<empty>'}")

    if trigger_type == "once":
        at = str(data.get("at") or data.get("time") or "").strip()
        if not at:
            raise ScheduleParseError("once schedule requires at")
        parsed = {"at": _parse_datetime(at, timezone).isoformat()}
        return ScheduleSpec("once", at, timezone, parsed)

    if trigger_type == "daily":
        time_text = str(data.get("time") or "").strip()
        hour, minute = _parse_hhmm(time_text)
        return ScheduleSpec("daily", time_text, timezone, {"hour": hour, "minute": minute})

    if trigger_type == "weekly":
        weekday = _parse_weekday(data.get("weekday"))
        time_text = str(data.get("time") or "").strip()
        hour, minute = _parse_hhmm(time_text)
        expression = f"{_weekday_name(weekday)} {time_text}"
        return ScheduleSpec("weekly", expression, timezone, {"weekday": weekday, "hour": hour, "minute": minute})

    if trigger_type == "monthly":
        try:
            day = int(data.get("day"))
        except (TypeError, ValueError) as exc:
            raise ScheduleParseError("monthly schedule requires integer day") from exc
        if day < 1:
            raise ScheduleParseError("monthly day must be >= 1")
        time_text = str(data.get("time") or "").strip()
        hour, minute = _parse_hhmm(time_text)
        return ScheduleSpec("monthly", f"{day} {time_text}", timezone, {"day": day, "hour": hour, "minute": minute})

    if trigger_type == "interval":
        every = str(data.get("every") or data.get("interval") or "").strip().lower()
        seconds = _parse_interval_seconds(every)
        return ScheduleSpec("interval", every, timezone, {"seconds": seconds})

    expr = str(data.get("expr") or data.get("expression") or "").strip()
    parsed = _parse_cron(expr)
    return ScheduleSpec("cron", expr, timezone, parsed)


def next_run_after(spec: ScheduleSpec, after: datetime) -> datetime | None:
    tz = _zone(spec.timezone)
    current = _as_tz(after, tz)
    parsed = spec.parsed or {}

    if spec.trigger_type == "once":
        candidate = datetime.fromisoformat(str(parsed.get("at") or spec.expression))
        candidate = _as_tz(candidate, tz)
        return candidate if candidate > current else None

    if spec.trigger_type == "daily":
        candidate = current.replace(
            hour=int(parsed["hour"]), minute=int(parsed["minute"]), second=0, microsecond=0
        )
        if candidate <= current:
            candidate += timedelta(days=1)
        return candidate

    if spec.trigger_type == "weekly":
        target = int(parsed["weekday"])
        days = (target - current.weekday()) % 7
        candidate = (current + timedelta(days=days)).replace(
            hour=int(parsed["hour"]), minute=int(parsed["minute"]), second=0, microsecond=0
        )
        if candidate <= current:
            candidate += timedelta(days=7)
        return candidate

    if spec.trigger_type == "monthly":
        year = current.year
        month = current.month
        while True:
            day = min(int(parsed["day"]), calendar.monthrange(year, month)[1])
            candidate = current.replace(
                year=year,
                month=month,
                day=day,
                hour=int(parsed["hour"]),
                minute=int(parsed["minute"]),
                second=0,
                microsecond=0,
            )
            if candidate > current:
                return candidate
            year, month = _add_month(year, month)

    if spec.trigger_type == "interval":
        seconds = int(parsed.get("seconds") or _parse_interval_seconds(spec.expression))
        return current + timedelta(seconds=seconds)

    if spec.trigger_type == "cron":
        cron = parsed if parsed else _parse_cron(spec.expression)
        candidate = (current + timedelta(minutes=1)).replace(second=0, microsecond=0)
        deadline = candidate + timedelta(days=366 * 5)
        while candidate <= deadline:
            if _cron_matches(candidate, cron):
                return candidate
            candidate += timedelta(minutes=1)
        return None

    raise ScheduleParseError(f"unsupported trigger type: {spec.trigger_type}")


def humanize_schedule(spec: ScheduleSpec) -> str:
    tz = spec.timezone
    parsed = spec.parsed or {}
    if spec.trigger_type == "once":
        return f"单次 {spec.expression} {tz}"
    if spec.trigger_type == "daily":
        return f"每天 {int(parsed.get('hour', 0)):02d}:{int(parsed.get('minute', 0)):02d} {tz}"
    if spec.trigger_type == "weekly":
        return f"每周 {_weekday_name(int(parsed.get('weekday', 0)))} {int(parsed.get('hour', 0)):02d}:{int(parsed.get('minute', 0)):02d} {tz}"
    if spec.trigger_type == "monthly":
        return f"每月 {int(parsed.get('day', 1))} 日 {int(parsed.get('hour', 0)):02d}:{int(parsed.get('minute', 0)):02d} {tz}"
    if spec.trigger_type == "interval":
        return f"每隔 {spec.expression} {tz}"
    return f"cron {spec.expression} {tz}"


def preview_next_runs(spec: ScheduleSpec, *, after: datetime, count: int = 3) -> list[datetime]:
    result: list[datetime] = []
    cursor = after
    for _ in range(count):
        nxt = next_run_after(spec, cursor)
        if nxt is None:
            break
        result.append(nxt)
        cursor = nxt
    return result


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except Exception as exc:
        raise ScheduleParseError(f"invalid timezone: {name}") from exc


def _as_tz(value: datetime, tz: ZoneInfo) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=tz)
    return value.astimezone(tz)


def _parse_datetime(value: str, timezone: str) -> datetime:
    text = value.strip().replace("T", " ")
    dt: datetime
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(text, fmt)
            return dt.replace(tzinfo=_zone(timezone))
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ScheduleParseError(f"invalid datetime: {value}") from exc
    return _as_tz(dt, _zone(timezone))


def _parse_hhmm(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", value or "")
    if not match:
        raise ScheduleParseError("time must be HH:MM")
    hour = int(match.group(1))
    minute = int(match.group(2))
    if hour > 23 or minute > 59:
        raise ScheduleParseError("time must be HH:MM")
    return hour, minute


def _parse_weekday(value: Any) -> int:
    if isinstance(value, int):
        if 0 <= value <= 6:
            return value
        if 1 <= value <= 7:
            return value - 1
    text = str(value or "").strip().lower()
    if text.isdigit():
        num = int(text)
        if 0 <= num <= 6:
            return num
        if 1 <= num <= 7:
            return num - 1
    if text in _WEEKDAYS:
        return _WEEKDAYS[text]
    raise ScheduleParseError(f"invalid weekday: {value}")


def _weekday_name(weekday: int) -> str:
    names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    return names[int(weekday) % 7]


def _parse_interval_seconds(value: str) -> int:
    match = re.fullmatch(r"(\d+)\s*([smhd])", value or "")
    if not match:
        raise ScheduleParseError("interval must look like 30s, 30m, 2h, or 1d")
    amount = int(match.group(1))
    if amount <= 0:
        raise ScheduleParseError("interval amount must be > 0")
    unit = match.group(2)
    return amount * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


def _add_month(year: int, month: int) -> tuple[int, int]:
    if month == 12:
        return year + 1, 1
    return year, month + 1


def _parse_cron(expr: str) -> dict[str, Any]:
    parts = expr.split()
    if len(parts) != 5:
        raise ScheduleParseError("cron expression must have 5 fields")
    minute, hour, dom, month, dow = parts
    return {
        "minute": _parse_cron_field(minute, 0, 59),
        "hour": _parse_cron_field(hour, 0, 23),
        "day_of_month": _parse_cron_field(dom, 1, 31),
        "month": _parse_cron_field(month, 1, 12),
        "day_of_week": _parse_cron_field(dow, 0, 7, normalize_dow=True),
        "dom_any": dom == "*",
        "dow_any": dow == "*",
    }


def _parse_cron_field(field: str, low: int, high: int, *, normalize_dow: bool = False) -> list[int]:
    values: set[int] = set()
    for item in field.split(","):
        item = item.strip()
        if not item:
            raise ScheduleParseError("empty cron field item")
        step = 1
        if "/" in item:
            item, step_text = item.split("/", 1)
            try:
                step = int(step_text)
            except ValueError as exc:
                raise ScheduleParseError(f"invalid cron step: {step_text}") from exc
            if step <= 0:
                raise ScheduleParseError("cron step must be > 0")
        if item == "*":
            start, end = low, high
        elif "-" in item:
            start_text, end_text = item.split("-", 1)
            start, end = int(start_text), int(end_text)
        else:
            start = end = int(item)
        if start < low or end > high or start > end:
            raise ScheduleParseError(f"cron value out of range: {item}")
        for value in range(start, end + 1, step):
            if normalize_dow:
                value = 0 if value == 7 else value
                value = (value + 6) % 7
            values.add(value)
    return sorted(values)


def _cron_matches(value: datetime, cron: dict[str, Any]) -> bool:
    if value.minute not in cron["minute"]:
        return False
    if value.hour not in cron["hour"]:
        return False
    if value.month not in cron["month"]:
        return False
    dom_match = value.day in cron["day_of_month"]
    dow_match = value.weekday() in cron["day_of_week"]
    if cron.get("dom_any") and cron.get("dow_any"):
        return True
    if cron.get("dom_any"):
        return dow_match
    if cron.get("dow_any"):
        return dom_match
    return dom_match or dow_match
