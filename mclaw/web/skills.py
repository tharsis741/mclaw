# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""HTTP adapters for local Skill governance and explicit install confirmation."""

from __future__ import annotations

import asyncio
import json

from starlette.responses import JSONResponse
from starlette.routing import Route

from mclaw.application.sessions import ApplicationError
from mclaw.application.skills import SkillService


def build_skill_routes(service) -> list[Route]:
    skills = SkillService(service.home)

    async def data(request):
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

    async def call(function, *args, status=200):
        try:
            result = await asyncio.to_thread(function, *args)
        except ApplicationError:
            raise
        except ValueError as exc:
            raise ApplicationError("SKILL_VALIDATION_FAILED", str(exc)) from exc
        except OSError as exc:
            raise ApplicationError("SKILL_IO_FAILED", "技能文件无法读取或保存，请核对文件状态", 409) from exc
        return JSONResponse(result, status_code=status)

    async def collection(request):
        if request.method == "GET":
            return await call(skills.list, request.query_params.get("q", ""))
        value = await data(request)
        return await call(skills.create, str(value.get("name") or ""),
                          str(value.get("description") or ""), str(value.get("content") or ""), status=201)

    async def item(request):
        name = request.path_params["name"]
        if request.method == "GET":
            return await call(skills.detail, name)
        if request.method == "DELETE":
            return await call(skills.delete, name, request.query_params.get("version", ""))
        return await call(skills.update, name, await data(request))

    async def file(request):
        name = request.path_params["name"]
        if request.method == "GET":
            return await call(skills.file, name, request.query_params.get("path", "SKILL.md"))
        value = await data(request)
        return await call(skills.write_file, name, str(value.get("path") or ""),
                          str(value.get("content") or ""), str(value.get("version") or ""))

    async def prepare(request):
        value = await data(request)
        return await call(skills.prepare, str(value.get("source") or ""), str(value.get("user_intent") or ""), status=201)

    async def draft(request):
        identifier = request.path_params["drafting_id"]
        action = request.path_params.get("action")
        if action == "confirm":
            value = await data(request)
            return await call(skills.confirm, identifier, str(value.get("confirmation_token") or ""))
        if action == "cancel":
            return await call(skills.cancel, identifier)
        if action:
            raise ApplicationError("SKILL_ACTION_NOT_FOUND", "未知安装操作", 404)
        return await call(skills.draft_detail, identifier)

    async def draft_file(request):
        return await call(skills.draft_file, request.path_params["drafting_id"], request.query_params.get("path", "SKILL.md"))

    return [
        Route("/api/v1/skills", collection, methods=["GET", "POST"]),
        Route("/api/v1/skills/prepare", prepare, methods=["POST"]),
        Route("/api/v1/skills/drafts/{drafting_id}", draft, methods=["GET"]),
        Route("/api/v1/skills/drafts/{drafting_id}/file", draft_file, methods=["GET"]),
        Route("/api/v1/skills/drafts/{drafting_id}/{action}", draft, methods=["POST"]),
        Route("/api/v1/skills/{name}/file", file, methods=["GET", "PUT"]),
        Route("/api/v1/skills/{name}", item, methods=["GET", "PATCH", "DELETE"]),
    ]
