# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Versioned, authenticated loopback API for the shared application service."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from mclaw.application.sessions import ApplicationError, SessionService
from mclaw.platform.detect import get_local_device_name

API_VERSION = 1


class LocalAccess:
    """Reject cross-origin requests and authenticate all API/event access."""

    def __init__(self, app, token: str) -> None:
        self.app, self.token = app, token

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            return await self.app(scope, receive, send)
        headers = {key.decode().lower(): value.decode() for key, value in scope["headers"]}
        host = headers.get("host", "")
        hostname = host.split(":", 1)[0]
        origin = headers.get("origin")
        allowed = hostname in {"127.0.0.1", "localhost"}
        if origin and origin != f"http://{host}":
            allowed = False
        if scope["path"].startswith("/api/"):
            authorization = headers.get("authorization", "")
            supplied = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else ""
            if scope["type"] == "websocket":
                from urllib.parse import parse_qs
                query = parse_qs(scope.get("query_string", b"").decode())
                supplied = query.get("token", [""])[0]
            allowed = allowed and bool(supplied) and hmac.compare_digest(supplied, self.token)
        if not allowed:
            if scope["type"] == "websocket":
                return await send({"type": "websocket.close", "code": 4403})
            return await JSONResponse({"error": {"code": "ACCESS_DENIED", "message": "本机连接认证失败，请重新运行 mclaw 打开页面"}}, status_code=403)(scope, receive, send)
        async def secure_send(message):
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).extend([
                    (b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"), (b"x-frame-options", b"DENY"),
                    (b"content-security-policy", b"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; frame-src blob:; object-src 'none'; connect-src 'self'; frame-ancestors 'none'"),
                ])
            await send(message)
        return await self.app(scope, receive, secure_send)


async def body(request: Request) -> dict:
    raw = await request.body()
    if len(raw) > 1024 * 1024:
        raise ApplicationError("BODY_TOO_LARGE", "请求内容过大", 413)
    try:
        value = json.loads(raw or b"{}")
    except (ValueError, UnicodeDecodeError) as exc:
        raise ApplicationError("INVALID_JSON", "请求内容格式错误") from exc
    if not isinstance(value, dict):
        raise ApplicationError("INVALID_JSON", "请求内容须为对象")
    return value


def create_app(home: Path, token: str, *, service: SessionService | None = None,
               setup=None, preferences=None, voice=None, schedules=None, channels=None, providers=None, tools=None, configuration=None, devices=None,
               shutdown: Callable | None = None, on_ready: Callable | None = None):
    """Create an ASGI adapter; tests inject services without touching user state."""
    service = service or SessionService(home)
    if devices is None:
        from mclaw.application.devices import DeviceService
        devices = DeviceService(home)
    service.devices = devices
    if configuration is None:
        from mclaw.application.configuration import ConfigurationService
        configuration = ConfigurationService(home)
    if tools is None:
        from mclaw.application.tools import ToolService
        tools = ToolService(home)
    if setup is None:
        from mclaw.application.setup import SetupService
        setup = SetupService()
    if providers is None:
        from mclaw.application.providers import ProviderService
        providers = ProviderService(home)
    if preferences is None:
        from mclaw.application.preferences import PreferencesService
        preferences = PreferencesService(home)
    if voice is None:
        from mclaw.application.voice import VoiceService
        voice = VoiceService(home)
    if schedules is None:
        from mclaw.application.schedules import ScheduleService
        schedules = ScheduleService(home, service)
    if channels is None:
        from mclaw.application.channels import ChannelService
        channels = ChannelService(home, service)

    @asynccontextmanager
    async def lifespan(_app):
        async def maintenance():
            while True:
                await asyncio.sleep(5)
                service.expire_leases()
        timer = asyncio.create_task(maintenance())
        try:
            await devices.start()
            await schedules.start()
            await channels.start()
            if on_ready:
                on_ready()
            yield
        finally:
            timer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await timer
            await devices.begin_shutdown()
            await voice.close()
            await channels.close()
            await schedules.close()
            await service.close()
            await devices.close()
            close = getattr(setup, "close", None)
            if close:
                await asyncio.to_thread(close)

    def client(request):
        return request.headers.get("x-mclaw-client", "")

    def sid(request):
        return request.path_params["session_id"]

    async def errors(_request, exc):
        error = {"code": exc.code, "message": str(exc)}
        if hasattr(exc, "current"):
            error["current"] = exc.current
        return JSONResponse({"error": error}, status_code=exc.status)

    async def health(_request):
        return JSONResponse({"status": "ready", "api_version": API_VERSION})

    async def bootstrap(_request):
        from importlib.metadata import version
        from mclaw.runtime.manager import RuntimeManager
        from mclaw.application.models import configured_models
        status = await asyncio.to_thread(setup.status)
        runtime = await asyncio.to_thread(RuntimeManager.create)
        device_name = await asyncio.to_thread(get_local_device_name)
        model = status.get("model") or ""
        choices = await asyncio.to_thread(configured_models, status) if status.get("ready") else []
        return JSONResponse({"api_version": API_VERSION, "version": version("m-claw"),
                             "setup_required": status.get("required", not status.get("ready")),
                             "setup": status, "device": {"name": device_name, "platform": runtime.kind},
                             "models": choices,
                             "default_model": f"{status['provider']}::{model}" if model and status.get("provider") else "",
                             "output_root": service.output_root(),
                             "capabilities": [{"id": name, "label": name, "available": value.enabled, "reason": value.reason}
                                              for name, value in runtime.features.features.items()]})

    async def sessions(request):
        if request.method == "POST":
            data = await body(request)
            return JSONResponse(service.create(client(request), str(data.get("workspace") or ""), str(data.get("model") or "")), status_code=201)
        try:
            offset = max(0, int(request.query_params.get("offset", 0)))
            limit = max(1, min(100, int(request.query_params.get("limit", 50))))
        except ValueError as exc:
            raise ApplicationError("INVALID_PAGINATION", "分页参数格式错误") from exc
        return JSONResponse({"sessions": service.list(request.query_params.get("q", ""), offset, limit)})

    async def session(request):
        if request.method == "DELETE":
            return JSONResponse(await service.delete(sid(request), client(request)))
        if request.method == "PATCH":
            return JSONResponse(service.patch(sid(request), client(request), await body(request)))
        return JSONResponse(service.snapshot(sid(request), client(request)))

    async def action(request):
        data = await body(request)
        name = request.path_params["action"]
        session_id, owner = sid(request), client(request)
        if name == "messages":
            result = service.submit(session_id, owner, str(data.get("content") or ""), str(data.get("request_id") or ""), str(data.get("mode") or "queue"), skills=data.get("skills"), attachments=data.get("attachments"), devices=data.get("devices"))
        elif name == "stop":
            result = service.stop(session_id, owner)
        elif name == "resume":
            result = service.resume(session_id, owner, str(data.get("content") or ""), str(data.get("request_id") or ""), skills=data.get("skills"), attachments=data.get("attachments"))
        elif name == "queue":
            items = data.get("items")
            if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                raise ApplicationError("INVALID_QUEUE", "队列内容格式错误")
            result = service.edit_queue(session_id, owner, items)
        elif name == "reply":
            result = service.reply(session_id, owner, str(data.get("request_id") or ""), data.get("value"))
        elif name == "release":
            service.release(session_id, owner)
            result = {"released": True}
        else:
            raise ApplicationError("ACTION_NOT_FOUND", "操作不存在", 404)
        return JSONResponse(result)

    async def export(request):
        snapshot = service.snapshot(sid(request), client(request))
        return JSONResponse({"application": snapshot, "conversation": service.history.export_session(sid(request))},
                            headers={"Content-Disposition": 'attachment; filename="mclaw-session.json"'})

    async def events(websocket: WebSocket):
        session_id = websocket.path_params["session_id"]
        owner = websocket.query_params.get("client", "")
        try:
            service.claim(session_id, owner)
        except ApplicationError:
            await websocket.close(code=4409)
            return
        await websocket.accept()
        last = -1
        previous = None
        incremental = websocket.query_params.get("incremental") == "1"
        try:
            while not service.closing:
                snapshot = service.snapshot(session_id, owner)
                if snapshot["seq"] != last:
                    if incremental and previous is not None:
                        from mclaw.application.streaming import snapshot_changes
                        await websocket.send_json({"type": "patch", "base_seq": last, "seq": snapshot["seq"],
                                                   "changes": snapshot_changes(previous, snapshot)})
                    else:
                        await websocket.send_json({"type": "snapshot", "seq": snapshot["seq"], "session": snapshot})
                    previous = snapshot
                    last = snapshot["seq"]
                # Receiving also detects disconnected sockets while no events change.
                try:
                    message = await asyncio.wait_for(websocket.receive(), timeout=0.05 if incremental else 0.15)
                    if message["type"] == "websocket.disconnect":
                        break
                except asyncio.TimeoutError:
                    pass
        except (WebSocketDisconnect, ApplicationError):
            pass

    async def setup_action(request):
        operation = request.path_params.get("operation")
        run_id = request.path_params.get("run_id")
        try:
            if request.method == "GET":
                result = await asyncio.to_thread(setup.get, run_id) if run_id else await asyncio.to_thread(setup.status)
            elif not run_id:
                result = await asyncio.to_thread(setup.start)
            elif operation == "cancel":
                result = await asyncio.to_thread(setup.cancel, run_id)
            else:
                data = await body(request)
                result = await asyncio.to_thread(setup.submit, run_id, str(data.get("prompt_id") or ""), data.get("value"))
        except (ValueError, KeyError, RuntimeError) as exc:
            raise ApplicationError("SETUP_ERROR", str(exc), 409) from exc
        return JSONResponse(result)

    async def provider_action(request):
        if request.method == "GET":
            result = await asyncio.to_thread(providers.catalog)
        else:
            data = await body(request)
            action = providers.models if request.url.path.endswith("/models") else providers.save
            result = await asyncio.to_thread(action, data)
        return JSONResponse(result)

    async def model_library(request):
        if request.method == "POST":
            result = await asyncio.to_thread(providers.library.refresh)
        else:
            try:
                offset = int(request.query_params.get("offset", 0))
            except ValueError:
                raise ApplicationError("INVALID_MODEL_QUERY", "分页参数格式错误", 422) from None
            result = await asyncio.to_thread(providers.library.browse, request.query_params.get("q", ""),
                                             request.query_params.get("provider", ""), offset)
        return JSONResponse(result)

    async def directories(request):
        from mclaw.runtime.manager import RuntimeManager
        path = Path(request.query_params.get("path") or Path.home()).expanduser().resolve()
        decision = RuntimeManager.current().paths.check("read", path)
        if not decision.allowed:
            raise ApplicationError("PATH_DENIED", decision.error_message(), 403)
        try:
            items = [{"name": child.name, "path": str(child.resolve())} for child in path.iterdir()
                     if child.is_dir() and not child.name.startswith(".")]
        except OSError as exc:
            raise ApplicationError("DIRECTORY_UNAVAILABLE", "无法浏览此目录") from exc
        return JSONResponse({"path": str(path), "parent": str(path.parent), "directories": sorted(items, key=lambda item: item["name"].casefold())[:500]})

    async def settings(request):
        data = await body(request)
        if "output_root" in data:
            service.set_output_root(str(data["output_root"]))
        return JSONResponse({"output_root": service.output_root()})

    async def stop_service(_request):
        if shutdown:
            service.closing = True
            shutdown()
        return JSONResponse({"status": "stopping"})

    async def static(request):
        root = Path(__file__).parent / "static"
        relative = request.path_params.get("path", "") or "index.html"
        path = (root / relative).resolve()
        if not path.is_relative_to(root.resolve()) or not path.is_file():
            return JSONResponse({"error": {"code": "STATIC_NOT_BUILT", "message": "请先在 webui 目录运行 npm ci 与 npm run build"}}, status_code=404)
        return FileResponse(path)

    routes = [Route("/api/v1/health", health), Route("/api/v1/bootstrap", bootstrap),
              Route("/api/v1/sessions", sessions, methods=["GET", "POST"]),
              Route("/api/v1/sessions/{session_id}", session, methods=["GET", "PATCH", "DELETE"]),
              Route("/api/v1/sessions/{session_id}/export", export),
              WebSocketRoute("/api/v1/sessions/{session_id}/events", events),
              Route("/api/v1/sessions/{session_id}/{action}", action, methods=["POST", "PATCH"]),
              Route("/api/v1/providers", provider_action, methods=["GET", "POST"]),
              Route("/api/v1/providers/models", provider_action, methods=["POST"]),
              Route("/api/v1/providers/library", model_library, methods=["GET", "POST"]),
              Route("/api/v1/setup", setup_action, methods=["GET", "POST"]),
              Route("/api/v1/setup/status", setup_action),
              Route("/api/v1/setup/{run_id}", setup_action),
              Route("/api/v1/setup/{run_id}/{operation}", setup_action, methods=["POST"]),
              Route("/api/v1/directories", directories), Route("/api/v1/settings", settings, methods=["PATCH"]),
              Route("/api/v1/service/stop", stop_service, methods=["POST"]),
              Route("/{path:path}", static)]
    from mclaw.web.preferences import build_preference_routes
    from mclaw.web.configuration import build_configuration_routes
    from mclaw.web.voice import build_voice_routes
    from mclaw.web.skills import build_skill_routes
    from mclaw.web.schedules import build_schedule_routes
    from mclaw.application.files import FileService
    from mclaw.web.files import build_file_routes
    from mclaw.web.changes import build_change_routes
    from mclaw.web.channels import build_channel_routes
    from mclaw.web.tools import build_tool_routes
    from mclaw.web.devices import build_device_routes
    from mclaw.web.terminal import build_terminal_routes
    routes[-1:-1] = build_terminal_routes(service, schedules, devices)
    routes[-1:-1] = build_device_routes(devices)
    routes[-1:-1] = build_tool_routes(tools)
    routes[-1:-1] = build_preference_routes(preferences)
    routes[-1:-1] = build_configuration_routes(configuration)
    routes[-1:-1] = build_voice_routes(voice)
    routes[-1:-1] = build_skill_routes(service)
    routes[-1:-1] = build_schedule_routes(schedules)
    routes[0:0] = build_file_routes(service, FileService(home))
    routes[0:0] = build_change_routes(service)
    routes[-1:-1] = build_channel_routes(channels)
    app = Starlette(routes=routes, lifespan=lifespan, exception_handlers={ApplicationError: errors})
    app.state.service = service
    return LocalAccess(app, token)
