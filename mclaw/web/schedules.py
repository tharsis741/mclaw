# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Authenticated HTTP adapters for the shared scheduler application service."""

from starlette.responses import JSONResponse
from starlette.routing import Route

from mclaw.application.sessions import ApplicationError


def build_schedule_routes(service) -> list[Route]:
    # Import lazily so app.py can assemble routes without an import cycle.
    from mclaw.web.app import body

    def page(request):
        try:
            offset = int(request.query_params.get("offset", 0))
            limit = int(request.query_params.get("limit", 50))
        except ValueError as exc:
            raise ApplicationError("INVALID_PAGE", "分页参数格式错误") from exc
        if offset < 0 or not 1 <= limit <= 100:
            raise ApplicationError("INVALID_PAGE", "每页数量须为 1–100，偏移须为非负整数")
        return {"offset": offset, "limit": limit}

    async def schedules(request):
        if request.method == "POST":
            return JSONResponse(service.create(await body(request)), status_code=201)
        return JSONResponse(service.list(**page(request)))

    async def preview(request):
        return JSONResponse(service.preview(await body(request)))

    async def schedule(request):
        identifier = request.path_params["job_id"]
        if request.method == "DELETE":
            return JSONResponse(service.delete(identifier))
        if request.method == "PATCH":
            return JSONResponse(service.update(identifier, await body(request)))
        return JSONResponse(service.get(identifier))

    async def enabled(request):
        data = await body(request)
        if not isinstance(data.get("enabled"), bool):
            raise ApplicationError("INVALID_SCHEDULE", "请指定启用或暂停状态")
        return JSONResponse(service.update(request.path_params["job_id"], {"enabled": data["enabled"]}))

    async def run_now(request):
        return JSONResponse(await service.run_now(request.path_params["job_id"]), status_code=202)

    async def runs(request):
        return JSONResponse(service.runs(request.path_params["job_id"], **page(request)))

    async def cancel(request):
        return JSONResponse(await service.cancel_run(request.path_params["run_id"]))

    async def targets(request):
        return JSONResponse(service.targets())

    async def target(request):
        identifier = request.path_params["target_id"]
        if request.method == "DELETE":
            return JSONResponse(service.delete_target(identifier))
        return JSONResponse(service.update_target(identifier, await body(request)))

    async def test_target(request):
        return JSONResponse(await service.test_target(request.path_params["target_id"]))

    async def pairings(request):
        return JSONResponse(service.create_pairing(await body(request)), status_code=201)

    async def pairing(request):
        return JSONResponse(service.pairing(request.path_params["code"], cancel=request.method == "DELETE"))

    prefix = "/api/v1/schedules"
    target_prefix = "/api/v1/schedule-targets"
    return [
        Route(prefix, schedules, methods=["GET", "POST"]),
        Route(prefix + "/preview", preview, methods=["POST"]),
        Route(prefix + "/runs/{run_id}/cancel", cancel, methods=["POST"]),
        Route(prefix + "/{job_id}", schedule, methods=["GET", "PATCH", "DELETE"]),
        Route(prefix + "/{job_id}/enabled", enabled, methods=["POST"]),
        Route(prefix + "/{job_id}/run", run_now, methods=["POST"]),
        Route(prefix + "/{job_id}/runs", runs, methods=["GET"]),
        Route(target_prefix, targets, methods=["GET"]),
        Route(target_prefix + "/pairings", pairings, methods=["POST"]),
        Route(target_prefix + "/pairings/{code}", pairing, methods=["GET", "DELETE"]),
        Route(target_prefix + "/{target_id}/test", test_target, methods=["POST"]),
        Route(target_prefix + "/{target_id}", target, methods=["PATCH", "DELETE"]),
    ]
