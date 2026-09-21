# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from mclaw.application.sessions import SessionService
from mclaw.web.app import create_app


class Setup:
    def status(self):
        return {"required": False, "ready": True, "model": "configured", "provider": "example"}

    def close(self):
        pass


@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.setenv("MCLAW_HOME", str(tmp_path))
    service = SessionService(tmp_path, runner_factory=lambda _: None)
    app = create_app(tmp_path, "test-connection-token", service=service, setup=Setup())
    with TestClient(app, base_url="http://127.0.0.1", headers={"Authorization": "Bearer test-connection-token", "X-MClaw-Client": "window-a"}) as client:
        yield client


@pytest.mark.parametrize("headers", [{"Authorization": ""}, {"Authorization": "Bearer wrong"}, {"Origin": "https://evil.example"}, {"Host": "evil.example"}, {"Host": "testserver"}])
def test_api_rejects_missing_auth_and_cross_origin(web, headers):
    assert web.get("/api/v1/health", headers=headers).status_code == 403


def test_api_session_creation_rename_and_ownership(web):
    response = web.post("/api/v1/sessions", json={"model": "configured"})
    assert response.status_code == 201
    sid = response.json()["id"]
    assert web.patch(f"/api/v1/sessions/{sid}", json={"title": "项目A"}).json()["title"] == "项目A"
    assert web.get("/api/v1/sessions?q=项目").json()["sessions"][0]["id"] == sid
    occupied = web.get(f"/api/v1/sessions/{sid}", headers={"X-MClaw-Client": "window-b"})
    assert occupied.status_code == 409
    assert occupied.json()["error"]["code"] == "SESSION_OCCUPIED"
    assert web.get(f"/api/v1/sessions/{sid}/export").json()["application"]["title"] == "项目A"


def test_api_rejects_invalid_json_and_queue(web):
    assert web.post("/api/v1/sessions", content="{").status_code == 400
    sid = web.post("/api/v1/sessions", json={}).json()["id"]
    assert web.patch(f"/api/v1/sessions/{sid}/queue", json={"items": "bad"}).status_code == 400
    assert web.patch(f"/api/v1/sessions/{sid}/queue", json={"items": [{"id": {}, "content": "bad"}]}).status_code == 400
    assert web.get("/api/v1/sessions?offset=bad").status_code == 400


def test_api_deletion_requires_auth_and_ownership_then_removes_record(web):
    sid = web.post("/api/v1/sessions", json={}).json()["id"]
    url = f"/api/v1/sessions/{sid}"
    assert web.delete(url, headers={"Authorization": ""}).status_code == 403
    assert web.delete(url, headers={"Origin": "https://evil.example"}).status_code == 403
    assert web.delete(url, headers={"X-MClaw-Client": "window-b"}).status_code == 409
    assert web.delete(url).json() == {"deleted": True, "id": sid}
    assert web.get(url).status_code == 404
    assert web.get(url + "/export").status_code == 404
    assert not web.get("/api/v1/sessions").json()["sessions"]


def test_task_directory_defaults_to_managed_home_and_can_be_saved(web, tmp_path):
    root = tmp_path / "_temp"
    assert Path(web.get("/api/v1/bootstrap").json()["output_root"]) == root
    assert not root.exists()
    response = web.patch("/api/v1/settings", json={"output_root": str(root)})
    assert response.status_code == 200
    assert root.is_dir()
    assert Path(response.json()["output_root"]) == root
    for blocked in (tmp_path, tmp_path / "sessions", root / ".." / "skills"):
        result = web.patch("/api/v1/settings", json={"output_root": str(blocked)})
        assert result.status_code == 400
        assert result.json()["error"]["code"] == "WORKSPACE_PROTECTED"
    assert Path(web.get("/api/v1/bootstrap").json()["output_root"]) == root


def test_bootstrap_stays_available_when_optional_model_config_read_fails(web, monkeypatch):
    from mclaw.cli import config

    def invalid_config(**_kwargs):
        raise config.ConfigError("invalid config")
    monkeypatch.setattr(config, "load_config", invalid_config)
    response = web.get("/api/v1/bootstrap")
    assert response.status_code == 200
    assert response.json()["models"] == []


def test_bootstrap_shows_system_device_name(web, monkeypatch):
    monkeypatch.setattr("mclaw.web.app.get_local_device_name", lambda: "Kaihong BotBook")
    assert web.get("/api/v1/bootstrap").json()["device"]["name"] == "Kaihong BotBook"


def test_websocket_snapshot_reconnect_uses_persisted_state(web):
    sid = web.post("/api/v1/sessions", json={}).json()["id"]
    url = f"ws://127.0.0.1/api/v1/sessions/{sid}/events?token=test-connection-token&client=window-a"
    with web.websocket_connect(url) as ws:
        first = ws.receive_json()
        assert first["type"] == "snapshot"
        assert first["session"]["id"] == sid
    web.patch(f"/api/v1/sessions/{sid}", json={"draft": "保留草稿"})
    with web.websocket_connect(url) as ws:
        snapshot = ws.receive_json()
        assert snapshot["seq"] > first["seq"]
        assert snapshot["session"]["draft"] == "保留草稿"


def test_websocket_requires_authentication(web):
    sid = web.post("/api/v1/sessions", json={}).json()["id"]
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with web.websocket_connect(f"ws://127.0.0.1/api/v1/sessions/{sid}/events?client=window-a"):
            pass
