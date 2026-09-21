# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Authenticated, session-scoped file import and read-only preview endpoints."""

import asyncio
from pathlib import Path

from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

from mclaw.application.files import FileService, MAX_UPLOAD_BYTES
from mclaw.application.sessions import ApplicationError


def build_file_routes(sessions, files: FileService):
    from mclaw.web.app import body

    def claim(request):
        return sessions.snapshot(request.path_params["session_id"], request.headers.get("x-mclaw-client", ""))

    async def upload(request):
        snapshot = claim(request)
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content) > MAX_UPLOAD_BYTES:
                raise ApplicationError("FILE_TOO_LARGE", "单个附件不能超过 20 MB", 413)
        record = await asyncio.to_thread(files.upload, snapshot["id"], request.query_params.get("name", ""),
                                         bytes(content), snapshot["workspace"])
        return JSONResponse(record, status_code=201)

    async def link(request):
        snapshot = claim(request)
        value = await body(request)
        record = await asyncio.to_thread(files.link, snapshot["id"], value.get("path"), snapshot["workspace"])
        return JSONResponse(record, status_code=201)

    async def browse(request):
        snapshot = claim(request)
        return JSONResponse(await asyncio.to_thread(files.browse, request.query_params.get("path") or snapshot["workspace"] or str(Path.home())))

    async def preview(request):
        snapshot = claim(request)
        result = await asyncio.to_thread(files.preview, snapshot["id"], request.path_params["file_id"])
        return JSONResponse(result)

    async def metadata(request):
        snapshot = claim(request)
        return JSONResponse(await asyncio.to_thread(files.get, snapshot["id"], request.path_params["file_id"]))

    async def content(request):
        snapshot = claim(request)
        record, path = await asyncio.to_thread(files.resolve, snapshot["id"], request.path_params["file_id"])
        # All binary reads are downloads. The frontend may use an authenticated
        # fetch and a sandboxed object URL for image/PDF previews.
        return FileResponse(path, media_type="application/octet-stream", filename=record["name"])

    prefix = "/api/v1/sessions/{session_id}/files"
    return [Route(prefix, upload, methods=["POST"]), Route(prefix + "/link", link, methods=["POST"]),
            Route(prefix + "/browse", browse), Route(prefix + "/{file_id}/preview", preview),
            Route(prefix + "/{file_id}/content", content), Route(prefix + "/{file_id}", metadata)]
