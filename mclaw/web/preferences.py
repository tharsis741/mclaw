# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Settings routes backed by the existing memory and diagnostic services."""

import asyncio

from starlette.responses import JSONResponse
from starlette.routing import Route


def build_preference_routes(service):
    from mclaw.web.app import body

    async def memories(_request):
        documents = await asyncio.to_thread(lambda: [service.read(target) for target in ("memory", "user")])
        return JSONResponse({"documents": documents})

    async def memory(request):
        data = await body(request)
        result = await asyncio.to_thread(service.update, request.path_params["target"],
                                         data.get("content"), data.get("revision"))
        return JSONResponse(result)

    async def diagnostics(_request):
        return JSONResponse(await asyncio.to_thread(service.diagnostics))

    return [Route("/api/v1/memory", memories),
            Route("/api/v1/memory/{target}", memory, methods=["PUT"]),
            Route("/api/v1/diagnostics", diagnostics)]
