# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from starlette.testclient import TestClient

from mclaw.application.schedules import ScheduleService
from mclaw.application.sessions import SessionService
from mclaw.web.app import create_app


class Setup:
    def status(self):
        return {"ready": True, "required": False}

    def close(self):
        pass


def test_schedule_http_management_validation_and_target_pairing(tmp_path, monkeypatch):
    monkeypatch.setenv("MCLAW_HOME", str(tmp_path))
    sessions = SessionService(tmp_path, runner_factory=lambda _: None)
    schedules = ScheduleService(tmp_path, sessions, config={})
    app = create_app(tmp_path, "test-token", service=sessions, setup=Setup(), schedules=schedules)
    headers = {"Authorization": "Bearer test-token", "X-MClaw-Client": "browser"}
    with TestClient(app, base_url="http://127.0.0.1", headers=headers) as client:
        response = client.post("/api/v1/schedules", json={"name": "周报", "prompt": "汇总本周进展", "schedule": {
            "trigger_type": "weekly", "weekday": 4, "time": "17:00", "timezone": "Asia/Shanghai"}})
        assert response.status_code == 201
        job = response.json()
        identifier = job["id"]
        assert len(client.post("/api/v1/schedules/preview", json={"schedule": job["schedule"]}).json()["next_runs"]) == 3
        assert client.get("/api/v1/schedules").json()["items"][0]["id"] == identifier
        assert client.get("/api/v1/schedules?limit=0").status_code == 400
        assert client.post(f"/api/v1/schedules/{identifier}/enabled", json={"enabled": "false"}).status_code == 400
        assert not client.post(f"/api/v1/schedules/{identifier}/enabled", json={"enabled": False}).json()["enabled"]
        assert client.patch(f"/api/v1/schedules/{identifier}", json={"name": "更新周报"}).json()["name"] == "更新周报"
        assert client.get(f"/api/v1/schedules/{identifier}/runs").json()["items"] == []
        pairing = client.post("/api/v1/schedule-targets/pairings", json={"type": "dingtalk_group"}).json()
        assert client.get(f"/api/v1/schedule-targets/pairings/{pairing['code']}").json()["status"] == "waiting"
        assert client.delete(f"/api/v1/schedule-targets/pairings/{pairing['code']}").json()["status"] == "cancelled"
        assert client.post("/api/v1/schedule-targets/local/test").json()["success"]
        assert client.delete("/api/v1/schedule-targets/local").status_code == 400
        assert client.delete(f"/api/v1/schedules/{identifier}").json()["deleted"]
        assert client.get(f"/api/v1/schedules/{identifier}").status_code == 404
