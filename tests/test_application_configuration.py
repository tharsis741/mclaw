# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import copy
import json

import pytest
import yaml
from starlette.testclient import TestClient

from mclaw.application.configuration import ConfigurationService
from mclaw.application.sessions import ApplicationError
from mclaw.cli import config as config_io
from mclaw.web.app import create_app


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setenv("MCLAW_HOME", str(tmp_path))
    return ConfigurationService(tmp_path)


def values(snapshot):
    return {field["path"]: field["value"] for group in snapshot["groups"] for field in group["fields"]}


def test_read_defaults_is_secret_free_and_does_not_materialize_config(service, tmp_path):
    (tmp_path / ".env").write_text("TEST_API_KEY=private-fixture-value\n", encoding="utf-8")
    result = service.read()
    assert values(result)["compression.threshold"] == 0.5
    assert values(result)["scheduler.output_dir"] == str(tmp_path / "scheduler" / "output")
    assert "private-fixture-value" not in json.dumps(result)
    assert not (tmp_path / "config.yaml").exists()


def test_round_trip_preserves_unknown_config_credentials_and_history(service, tmp_path):
    raw = {"memory": {"memory_char_limit": 5000, "future_option": True},
           "providers": {"custom": {"api_key_env": "CUSTOM_KEY"}},
           "security": {"trusted_workspaces": ["D:/work"]}, "future_section": {"value": 42}}
    config_io.save_config(raw)
    (tmp_path / ".env").write_bytes(b"CUSTOM_KEY=private-fixture-value")
    (tmp_path / "state.db").write_bytes(b"existing-history-fixture")
    snapshot = service.read()
    updated = service.update({"revision": snapshot["revision"], "changes": {
        "memory.memory_enabled": False, "terminal.timeout": 240, "compression.threshold": 0.6}})
    expected = copy.deepcopy(raw)
    expected["memory"]["memory_enabled"] = False
    expected["terminal"] = {"timeout": 240}
    expected["compression"] = {"threshold": 0.6}
    assert yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8")) == expected
    assert values(updated)["memory.memory_char_limit"] == 5000
    assert config_io.load_config(strict=True)["terminal"]["timeout"] == 240
    assert (tmp_path / ".env").read_bytes() == b"CUSTOM_KEY=private-fixture-value"
    assert (tmp_path / "state.db").read_bytes() == b"existing-history-fixture"


def test_external_edit_reloads_and_stale_save_cannot_overwrite_it(service, tmp_path):
    original = service.read()
    config_io.save_config({"memory": {"memory_char_limit": 7777}})
    latest = (tmp_path / "config.yaml").read_bytes()
    with pytest.raises(ApplicationError, match="配置已在其他地方修改") as error:
        service.update({"revision": original["revision"], "changes": {"prompt_cache.enabled": False}})
    assert error.value.code == "CONFIGURATION_CONFLICT"
    assert (tmp_path / "config.yaml").read_bytes() == latest
    assert values(service.read())["memory.memory_char_limit"] == 7777


@pytest.mark.parametrize("changes", [
    {"prompt_cache.enabled": "false"}, {"terminal.timeout": True}, {"terminal.timeout": 0},
    {"terminal.timeout": 3.5}, {"terminal.timeout": 10 ** 1000},
    {"compression.threshold": float("nan")}, {"compression.target_ratio": 0.9},
    {"terminal.timeout": 601}, {"scheduler.default_timezone": "Not/AZone"},
    {"scheduler.max_workers": 0}, {"reasoning.effort": "imaginary"},
    {"checkpoints.max_snapshots": 0}, {"delegation.base_url": "https://key:secret@example.com"},
    {"delegation.base_url": "https://example.com?api_key=secret"},
    {"security.trusted_workspaces": ["C:/"]}, {"providers": {}}, {},
])
def test_invalid_or_unscoped_writes_preserve_original(service, tmp_path, changes):
    config_io.save_config({"custom": "keep"})
    before = (tmp_path / "config.yaml").read_bytes()
    with pytest.raises(ApplicationError) as error:
        service.update({"revision": service.read()["revision"], "changes": changes})
    assert error.value.status == 422
    assert (tmp_path / "config.yaml").read_bytes() == before


def test_paired_timeout_edit_and_zero_semantics(service):
    snapshot = service.update({"revision": service.read()["revision"], "changes": {
        "terminal.timeout": 900, "terminal.max_timeout": 1200,
        "checkpoints.max_total_size_mb": 0, "checkpoints.max_file_size_mb": 0,
        "memory.memory_review_round": 0, "skills.evolution_review_round": 0}})
    assert values(snapshot)["terminal.timeout"] == 900
    assert values(snapshot)["memory.memory_review_round"] == 0


def test_model_response_timeouts_can_be_saved_without_changing_other_agent_settings(service, tmp_path):
    config_io.save_config({"agent": {"max_turns": 200, "future_option": "keep"}})
    before = service.read()
    assert values(before)["agent.first_response_timeout"] == 90
    assert values(before)["agent.stream_idle_timeout"] == 60
    after = service.update({"revision": before["revision"], "changes": {
        "agent.first_response_timeout": 180, "agent.stream_idle_timeout": 45}})
    assert values(after)["agent.first_response_timeout"] == 180
    assert values(after)["agent.stream_idle_timeout"] == 45
    assert yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))["agent"] == {
        "max_turns": 200, "future_option": "keep", "first_response_timeout": 180, "stream_idle_timeout": 45}


def test_sensitive_legacy_url_is_preserved_but_not_sent_to_browser(service, tmp_path):
    endpoint = "https://name:private-fixture-value@example.com/v1?token=hidden"
    config_io.save_config({"delegation": {"base_url": endpoint}})
    snapshot = service.read()
    assert "private-fixture-value" not in json.dumps(snapshot)
    assert values(snapshot)["delegation.base_url"] == ""
    service.update({"revision": snapshot["revision"], "changes": {"delegation.max_iterations": 20}})
    assert config_io.load_config(strict=True)["delegation"]["base_url"] == endpoint


@pytest.mark.parametrize("content", ["memory: [invalid]", "- invalid", "memory: {", "memory: null"])
def test_malformed_config_never_gets_replaced_with_defaults(service, tmp_path, content):
    path = tmp_path / "config.yaml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ApplicationError):
        service.read()
    assert path.read_text(encoding="utf-8") == content


def test_write_failure_preserves_file_and_releases_workflow_lock(service, tmp_path, monkeypatch):
    from mclaw.application.setup import _WORKFLOW_LOCK

    config_io.save_config({"memory": {"memory_char_limit": 4000}})
    before = (tmp_path / "config.yaml").read_bytes()
    snapshot = service.read()
    monkeypatch.setattr(config_io, "save_config", lambda _: (_ for _ in ()).throw(OSError("fixture failure")))
    with pytest.raises(ApplicationError) as error:
        service.update({"revision": snapshot["revision"], "changes": {"memory.memory_enabled": False}})
    assert error.value.code == "CONFIGURATION_SAVE_FAILED"
    assert (tmp_path / "config.yaml").read_bytes() == before
    assert _WORKFLOW_LOCK.acquire(blocking=False)
    _WORKFLOW_LOCK.release()


def test_configuration_api_auth_patch_and_conflict(service, tmp_path):
    with TestClient(create_app(tmp_path, "fixture-token"), base_url="http://127.0.0.1") as client:
        headers = {"Authorization": "Bearer fixture-token"}
        assert client.get("/api/v1/configuration").status_code == 403
        snapshot = client.get("/api/v1/configuration", headers=headers).json()
        data = {"revision": snapshot["revision"], "changes": {"prompt_cache.enabled": False}}
        assert client.patch("/api/v1/configuration", headers=headers, json=data).status_code == 200
        assert client.patch("/api/v1/configuration", headers=headers, json=data).status_code == 409
        assert client.patch("/api/v1/configuration", headers=headers, json={"config": {}}).status_code == 422
