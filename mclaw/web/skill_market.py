# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Authenticated HTTP transport for the two skill marketplaces."""

from __future__ import annotations

import asyncio

import httpx
from starlette.responses import JSONResponse
from starlette.routing import Route

from mclaw.application.sessions import ApplicationError


def build_skill_market_routes(market) -> list[Route]:
    async def invoke(function, *args):
        try:
            return JSONResponse(await asyncio.to_thread(function, *args))
        except ApplicationError:
            raise
        except ValueError as exc:
            raise ApplicationError("SKILL_MARKET_INVALID", str(exc)) from exc
        except (httpx.HTTPError, OSError) as exc:
            raise ApplicationError("SKILL_MARKET_IO_FAILED", "操作未完成，请检查网络或本机文件后重试。", 502) from exc

    async def trending(request):
        return await invoke(market.trending, request.path_params["market"], request.query_params.get("timezone"))

    async def refresh(request):
        from mclaw.web.app import body
        value = await body(request)
        return await invoke(market.trending, request.path_params["market"], value.get("timezone"), True)

    async def detail(request):
        return await invoke(market.detail, request.path_params["market"], request.query_params.get("id", ""))

    async def install(request):
        from mclaw.web.app import body
        value = await body(request)
        return await invoke(market.install, request.path_params["market"], value.get("id", ""))

    return [
        Route("/api/v1/skill-market/{market}/trending", trending),
        Route("/api/v1/skill-market/{market}/refresh", refresh, methods=["POST"]),
        Route("/api/v1/skill-market/{market}/detail", detail),
        Route("/api/v1/skill-market/{market}/install", install, methods=["POST"]),
    ]
