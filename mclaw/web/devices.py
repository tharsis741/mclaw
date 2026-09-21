# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from starlette.responses import JSONResponse
from starlette.routing import Route


def build_device_routes(service):
    from mclaw.web.app import body

    async def status(request):
        result = await service.configure(await body(request)) if request.method == "PATCH" else await service.snapshot()
        return JSONResponse(result)

    async def action(request):
        return JSONResponse(await service.act(request.path_params["action"], await body(request)), status_code=202)

    return [Route("/api/v1/devices", status, methods=["GET", "PATCH"]),
            Route("/api/v1/devices/{action}", action, methods=["POST"])]
