# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import asyncio
from types import SimpleNamespace

from starlette.testclient import TestClient

from mclaw.application.channels import ChannelService
from mclaw.application.sessions import SessionService
from mclaw.web.app import create_app


class Setup:
    def status(self):
        return {"ready": True, "required": False}

    def close(self):
        pass


def test_channels_http_management_credentials_and_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setenv("MCLAW_HOME", str(tmp_path))
    for name in ("WEIXIN_ACCOUNT_ID", "WEIXIN_TOKEN", "DINGTALK_CLIENT_ID", "DINGTALK_CLIENT_SECRET"):
        monkeypatch.setenv(name, "")
    instances = []
    class Runtime:
        def __init__(self, kind, config):
            self._running = False
            self.adapter = SimpleNamespace()
            self.runner = SimpleNamespace(restore_deliveries=lambda _adapter: None)
            self.closed = False
            instances.append(self)
        async def start(self):
            self._running = True
            await asyncio.Event().wait()
        async def stop(self):
            self._running = False
            self.closed = True
    sessions = SessionService(tmp_path, runner_factory=lambda _: None)
    channels = ChannelService(tmp_path, sessions, runtime_factory=Runtime)
    app = create_app(tmp_path, "test-token", service=sessions, setup=Setup(), channels=channels)
    headers = {"Authorization": "Bearer test-token", "X-MClaw-Client": "browser"}
    with TestClient(app, base_url="http://127.0.0.1", headers=headers) as client:
        assert len(client.get("/api/v1/channels").json()["items"]) == 2
        assert client.post("/api/v1/channels/weixin/connect").status_code == 409
        assert client.patch("/api/v1/channels/weixin", json={"settings": {"enabled": "true"}}).status_code == 400
        assert client.patch("/api/v1/channels/weixin", json={"credentials": {"account_id": "../private"}}).status_code == 400
        saved = client.patch("/api/v1/channels/weixin", json={"settings": {"enabled": True}, "credentials": {"account_id": "fake-account", "token": "fake-secret"}})
        assert saved.status_code == 200 and saved.json()["configured"]
        assert "fake-secret" not in saved.text
        assert client.get("/api/v1/channels/missing").status_code == 404
        assert client.get("/api/v1/channels/weixin/login/missing").status_code == 404
        assert not client.post("/api/v1/channels/dingtalk/diagnose").json()["ready"]
        assert client.post("/api/v1/channels/weixin/disconnect").json()["status"] == "stopped"
        assert instances[-1].closed
        assert client.post("/api/v1/channels/weixin/connect").status_code == 200
    assert all(runtime.closed for runtime in instances)
