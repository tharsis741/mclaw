# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Local terminal transport; terminal controls and rendering stay in cli/."""

import asyncio

from starlette.responses import JSONResponse
from starlette.routing import Route

from mclaw.application.sessions import ApplicationError


def build_terminal_routes(sessions, schedules=None, devices=None):
    from mclaw.web.app import body

    async def capabilities(_request):
        return JSONResponse({"version": 1, "execution": "application-service"})

    async def attach(request):
        data = await body(request)
        client = request.headers.get("x-mclaw-client", "")
        workspace = str(data.get("workspace") or "")
        reference = str(data.get("session_ref") or "")
        model = str(data.get("model") or "")
        identifier = ""
        if reference:
            if reference == "__latest__":
                rows = sessions.list(limit=10000)
                identifier = next((row["id"] for row in rows if row.get("source") == "cli" and row.get("workspace") == workspace), "")
            else:
                matches = [row["id"] for row in sessions.list(limit=10000) if row["id"].startswith(reference)]
                if len(matches) != 1:
                    raise ApplicationError("SESSION_NOT_FOUND", "未找到唯一匹配的会话，请使用完整会话 ID", 404)
                identifier = matches[0]
        if identifier:
            snapshot = sessions.snapshot(identifier, client)
        else:
            snapshot = sessions.create(client, workspace, model, source="cli")
        return JSONResponse(snapshot)

    async def history(request):
        identifier = request.path_params["session_id"]
        snapshot = sessions.snapshot(identifier)
        from mclaw.application.process_events import public_detail, secret_values
        secrets = secret_values(sessions.home)
        return JSONResponse({"session": public_detail(snapshot, secrets),
                             "conversation": public_detail(sessions.history.export_session(identifier), secrets),
                             "model_config": public_detail(sessions.history.get_model_config(identifier), secrets),
                             "messages": public_detail(sessions.history.get_messages_as_conversation(identifier), secrets)})

    async def scheduler(request):
        from mclaw.application.scheduler_transport import invoke
        return JSONResponse({"value": await asyncio.to_thread(invoke, schedules, await body(request))})

    async def runtime(request):
        return JSONResponse(sessions.terminal_runtime(request.path_params["session_id"],
                                                      request.headers.get("x-mclaw-client", ""), await body(request)))

    async def device(request):
        from mclaw.application.device_bridge import invoke
        try:
            value = await invoke(devices, await body(request), management=True)
            return JSONResponse({"value": value})
        except Exception as exc:
            return JSONResponse({"error": {"code": str(getattr(exc, "code", "INTERNAL_ERROR")),
                                           "outcome_unknown": bool(getattr(exc, "outcome_unknown", False))}})

    return [Route("/api/v1/terminal", capabilities),
            Route("/api/v1/terminal/attach", attach, methods=["POST"]),
            Route("/api/v1/terminal/sessions/{session_id}", history),
            Route("/api/v1/terminal/sessions/{session_id}/runtime", runtime, methods=["POST"]),
            Route("/api/v1/terminal/scheduler", scheduler, methods=["POST"]),
            Route("/api/v1/terminal/devices", device, methods=["POST"])]
