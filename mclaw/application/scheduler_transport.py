# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Versioned scheduler values shared by local presentation clients."""

from mclaw.scheduler import models

_MODELS = {name: getattr(models, name) for name in (
    "ScheduleSpec", "DeliverySpec", "SchedulerJob", "SchedulerRun", "SchedulerTarget", "SchedulerTargetPairing")}
OPERATIONS = {
    "store": frozenset({"list_jobs", "get_job", "create_job", "delete_job", "pause_job", "resume_job",
                        "list_runs", "get_run", "list_targets", "get_target", "delete_target", "update_target",
                        "get_pairing", "create_pairing", "cancel_pairing"}),
    "engine": frozenset({"run_job_now", "cancel_run"}),
    "delivery": frozenset({"deliver_one"}),
}


def encode(value, *, public=False):
    if type(value).__name__ in _MODELS and isinstance(value, _MODELS[type(value).__name__]):
        data = value.to_dict()
        if public and isinstance(value, models.SchedulerTarget):
            data.pop("route_metadata", None)
        return {"$scheduler": type(value).__name__, "value": data}
    if isinstance(value, (list, tuple)):
        return [encode(item, public=public) for item in value]
    if isinstance(value, dict):
        return {key: encode(item, public=public) for key, item in value.items()}
    return value


def decode(value):
    if isinstance(value, list):
        return [decode(item) for item in value]
    if isinstance(value, dict):
        if "$scheduler" in value:
            cls = _MODELS.get(value["$scheduler"])
            if cls is None or set(value) != {"$scheduler", "value"}:
                raise ValueError("未知定时任务数据类型")
            return cls.from_dict(value["value"])
        return {key: decode(item) for key, item in value.items()}
    return value


def invoke(service, request):
    from mclaw.application.sessions import ApplicationError

    area, operation = request.get("area"), request.get("operation")
    if not isinstance(area, str) or not isinstance(operation, str) or operation not in OPERATIONS.get(area, ()):
        raise ApplicationError("SCHEDULER_ACTION_INVALID", "定时任务操作无效")
    try:
        args, kwargs = decode(request.get("args", [])), decode(request.get("kwargs", {}))
    except (ValueError, TypeError, KeyError) as exc:
        raise ApplicationError("SCHEDULER_ACTION_INVALID", "定时任务参数无效") from exc
    if not isinstance(args, list) or not isinstance(kwargs, dict):
        raise ApplicationError("SCHEDULER_ACTION_INVALID", "定时任务参数无效")
    with service._commands:
        if service._closing:
            raise ApplicationError("SERVICE_STOPPING", "后台正在退出", 409)
        # Use the same engine and delivery clients as the Web scheduler.
        try:
            from mclaw.application.process_events import public_detail, secret_values
            return public_detail(encode(getattr(getattr(service, area), operation)(*args, **kwargs), public=True), secret_values(service.home))
        except (ValueError, TypeError) as exc:
            raise ApplicationError("SCHEDULER_ACTION_INVALID", str(exc)) from exc
