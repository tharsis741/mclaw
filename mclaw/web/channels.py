# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Channel lifecycle, credential forms, diagnostics, and QR login routes."""

from starlette.responses import JSONResponse
from starlette.routing import Route


def build_channel_routes(service) -> list[Route]:
    from mclaw.web.app import body

    async def channels(request):
        return JSONResponse(service.list())

    async def channel(request):
        kind = request.path_params["kind"]
        if request.method == "PATCH":
            return JSONResponse(await service.update(kind, await body(request)))
        return JSONResponse(service.get(kind))

    async def connect(request):
        return JSONResponse(await service.connect(request.path_params["kind"]))

    async def disconnect(request):
        return JSONResponse(await service.disconnect(request.path_params["kind"]))

    async def diagnose(request):
        return JSONResponse(service.diagnose(request.path_params["kind"]))

    async def login(request):
        return JSONResponse(service.login(), status_code=202)

    async def login_status(request):
        return JSONResponse(service.login_status(request.path_params["identifier"], cancel=request.method == "DELETE"))

    prefix = "/api/v1/channels"
    return [Route(prefix, channels), Route(prefix + "/weixin/login", login, methods=["POST"]),
            Route(prefix + "/weixin/login/{identifier}", login_status, methods=["GET", "DELETE"]),
            Route(prefix + "/{kind}", channel, methods=["GET", "PATCH"]),
            Route(prefix + "/{kind}/connect", connect, methods=["POST"]),
            Route(prefix + "/{kind}/disconnect", disconnect, methods=["POST"]),
            Route(prefix + "/{kind}/diagnose", diagnose, methods=["POST"])]
