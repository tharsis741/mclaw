# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Authenticated local application client, independent of frontend rendering."""

from __future__ import annotations

from pathlib import Path
import uuid

import httpx


class ServiceError(ValueError):
    def __init__(self, code: str, message: str, status: int = 0):
        super().__init__(message)
        self.code, self.status = code, status


class ApplicationClient:
    """One presentation controller; never owns an Agent or native Runtime."""

    def __init__(self, endpoint: dict, *, client_id: str | None = None, transport=None):
        self.client_id = client_id or "tui:" + uuid.uuid4().hex
        self.endpoint = endpoint
        self.home = None
        self.http = httpx.Client(
            base_url=f"http://127.0.0.1:{int(endpoint['port'])}/api/v1",
            headers={"Authorization": "Bearer " + endpoint["token"], "X-MClaw-Client": self.client_id},
            timeout=httpx.Timeout(10, connect=2), trust_env=False, transport=transport,
        )

    @classmethod
    def connect(cls, home: Path | None = None):
        from mclaw.web.launcher import ensure_service

        try:
            client = cls(ensure_service(home))
        except (RuntimeError, OSError) as exc:
            raise ServiceError("SERVICE_UNAVAILABLE", "M-Claw 后台未能启动，请运行 mclaw status 查看状态。") from exc
        from mclaw.constants import get_mclaw_home
        client.home = Path(home or get_mclaw_home()).resolve()
        try:
            capabilities = client.request("GET", "/terminal")
            if capabilities.get("version") != 1:
                raise ServiceError("SERVICE_VERSION", "后台版本需要更新，请运行 mclaw stop 后重新打开。")
        except ServiceError as exc:
            client.close()
            if exc.status == 404:
                raise ServiceError("SERVICE_VERSION", "后台版本需要更新，请运行 mclaw stop 后重新打开。") from exc
            raise
        except BaseException:
            client.close()
            raise
        return client

    def request(self, method: str, path: str, data=None, *, timeout=None, _rediscovered=False):
        try:
            options = {"timeout": timeout} if timeout is not None else {}
            endpoint = self.endpoint
            response = self.http.request(method, f"http://127.0.0.1:{endpoint['port']}/api/v1{path}", json=data,
                                         headers={"Authorization": "Bearer " + endpoint["token"]}, **options)
        except httpx.HTTPError as exc:
            if method == "GET" and self.home is not None and not _rediscovered:
                from mclaw.web.launcher import running_service
                current = running_service(self.home)
                if current and current != self.endpoint:
                    self.endpoint = current
                    return self.request(method, path, data, timeout=timeout, _rediscovered=True)
            raise ServiceError("SERVICE_UNAVAILABLE", "与 M-Claw 后台的连接已断开。重新连接后请核对任务状态。") from exc
        try:
            value = response.json()
        except ValueError:
            value = {}
        if not response.is_success:
            if response.status_code in {401, 403} and method == "GET" and self.home is not None and not _rediscovered:
                from mclaw.web.launcher import running_service
                current = running_service(self.home)
                if current and current != self.endpoint:
                    self.endpoint = current
                    return self.request(method, path, data, timeout=timeout, _rediscovered=True)
            error = value.get("error") or {}
            raise ServiceError(error.get("code", "SERVICE_ERROR"), error.get("message", "后台无法完成此操作"), response.status_code)
        return value

    def attach(self, workspace: str, model: str = "", session_ref: str = "") -> dict:
        return self.request("POST", "/terminal/attach", {"workspace": workspace, "model": model, "session_ref": session_ref})

    def session(self, identifier: str) -> dict:
        from urllib.parse import quote
        return self.request("GET", "/sessions/" + quote(identifier, safe=""))

    def patch(self, identifier: str, changes: dict) -> dict:
        from urllib.parse import quote
        return self.request("PATCH", "/sessions/" + quote(identifier, safe=""), changes)

    def runtime(self, identifier, context):
        data = {"model": f"{context.provider}::{context.model}"}
        if context.auth_source == "explicit" or context.base_url_source == "explicit":
            data["context"] = {"api_key": context.api_key, "base_url": context.base_url, "snapshot": context.snapshot()}
        return self.request("POST", f"/terminal/sessions/{identifier}/runtime", data)

    def action(self, identifier: str, action: str, data=None) -> dict:
        from urllib.parse import quote
        return self.request("POST", f"/sessions/{quote(identifier, safe='')}/{action}", data or {})

    def release(self, identifier: str) -> None:
        self.action(identifier, "release")

    def close(self):
        self.http.close()
