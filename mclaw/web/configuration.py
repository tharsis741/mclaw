# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Authenticated adapters for shared configuration settings."""

import asyncio

from starlette.responses import JSONResponse
from starlette.routing import Route


def build_configuration_routes(service) -> list[Route]:
    from mclaw.web.app import body

    async def configuration(request):
        if request.method == "PATCH":
            return JSONResponse(await asyncio.to_thread(service.update, await body(request)))
        return JSONResponse(await asyncio.to_thread(service.read))

    return [Route("/api/v1/configuration", configuration, methods=["GET", "PATCH"])]
