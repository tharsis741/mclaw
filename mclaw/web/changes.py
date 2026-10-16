# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""HTTP adapters for the Session worker's file recovery channel."""

import json

from starlette.responses import JSONResponse
from starlette.routing import Route

from mclaw.application.sessions import ApplicationError


def build_change_routes(service):
    async def changes(request):
        operation = request.path_params.get("operation", "list")
        if request.method == "GET" and operation not in {"list", "status"}:
            raise ApplicationError("METHOD_NOT_ALLOWED", "此操作需要提交请求", 405)
        value = {}
        if request.method == "POST":
            raw = await request.body()
            if len(raw) > 16384:
                raise ApplicationError("BODY_TOO_LARGE", "请求内容过大", 413)
            try:
                value = json.loads(raw or b"{}")
            except (ValueError, UnicodeDecodeError) as exc:
                raise ApplicationError("INVALID_JSON", "请求格式错误") from exc
            if not isinstance(value, dict):
                raise ApplicationError("INVALID_JSON", "请求内容须为对象")
        result = await service.change_operation(request.path_params["session_id"],
                                                request.headers.get("x-mclaw-client", ""), {**value, "operation": operation})
        return JSONResponse(result)

    return [Route("/api/v1/sessions/{session_id}/changes", changes, methods=["GET"]),
            Route("/api/v1/sessions/{session_id}/changes/{operation}", changes, methods=["GET", "POST"])]
