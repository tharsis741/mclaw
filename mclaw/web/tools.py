# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""HTTP adapter for shared tool configuration."""

import asyncio

from starlette.responses import JSONResponse
from starlette.routing import Route


def build_tool_routes(service):
    from mclaw.web.app import body

    async def catalog(request):
        return JSONResponse(await asyncio.to_thread(service.catalog))

    async def save(request):
        return JSONResponse(await asyncio.to_thread(service.save, request.path_params["identifier"], await body(request)))

    return [Route("/api/v1/tools", catalog), Route("/api/v1/tools/{identifier}", save, methods=["PUT"])]
