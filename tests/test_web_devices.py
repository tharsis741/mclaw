# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from starlette.testclient import TestClient

from mclaw.application.sessions import ApplicationError
from mclaw.web.app import create_app


class Devices:
    def __init__(self):
        self.calls = []

    async def start(self):
        self.calls.append("start")

    async def begin_shutdown(self):
        self.calls.append("drain")

    async def close(self):
        self.calls.append("close")

    async def snapshot(self):
        return {"state": "ready", "trustedDevices": [], "pairableDevices": []}

    async def configure(self, data):
        self.calls.append(data)
        return {"state": "starting"}

    async def act(self, action, data):
        if data.get("confirmed") is not True and action != "discover":
            raise ApplicationError("DEVICE_CONFIRMATION_REQUIRED", "请先确认操作")
        self.calls.append((action, data))
        return {"operation": {"id": "test", "status": "running"}}


def test_device_routes_require_authentication_and_delegate_to_one_service(tmp_path, monkeypatch):
    monkeypatch.setenv("MCLAW_HOME", str(tmp_path))
    devices = Devices()
    with TestClient(create_app(tmp_path, "local-test", devices=devices), base_url="http://127.0.0.1") as client:
        assert client.get("/api/v1/devices").status_code == 403
        assert client.post("/api/v1/devices/discover", json={}).status_code == 403
        client.headers["Authorization"] = "Bearer local-test"
        assert client.get("/api/v1/devices").json()["state"] == "ready"
        assert client.post("/api/v1/devices/discover", json={}).status_code == 202
        assert client.post("/api/v1/devices/pair", json={}).json()["error"]["code"] == "DEVICE_CONFIRMATION_REQUIRED"
        assert client.patch("/api/v1/devices", json={"enabled": False}).status_code == 200
    assert devices.calls == ["start", ("discover", {}), {"enabled": False}, "drain", "close"]
