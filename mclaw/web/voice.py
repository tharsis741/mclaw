# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Bounded audio upload and scoped voice configuration routes."""

import asyncio

from starlette.responses import JSONResponse
from starlette.routing import Route

from mclaw.application.sessions import ApplicationError
from mclaw.application.voice import MAX_AUDIO_BYTES


def build_voice_routes(service):
    from mclaw.web.app import body

    async def voice(request):
        if request.method == "PUT":
            return JSONResponse(await asyncio.to_thread(service.configure, await body(request)))
        return JSONResponse(await asyncio.to_thread(service.status))

    async def transcribe(request):
        try:
            duration = int(request.headers.get("x-audio-duration-ms", "0"))
        except ValueError as exc:
            raise ApplicationError("AUDIO_DURATION_INVALID", "录音时长无效", 422) from exc
        chunks = bytearray()
        async for chunk in request.stream():
            if len(chunks) + len(chunk) > MAX_AUDIO_BYTES:
                raise ApplicationError("AUDIO_TOO_LARGE", "录音超过大小限制", 413)
            chunks.extend(chunk)
        result = await service.transcribe(bytes(chunks), request.headers.get("content-type", ""), duration)
        return JSONResponse(result)

    return [Route("/api/v1/voice", voice, methods=["GET", "PUT"]),
            Route("/api/v1/voice/transcribe", transcribe, methods=["POST"])]
