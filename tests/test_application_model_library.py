# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import threading
from unittest.mock import Mock

import pytest
from starlette.testclient import TestClient

from mclaw.agent import models_dev
from mclaw.application.model_library import ModelLibraryService
from mclaw.application.providers import ProviderService
from mclaw.application.sessions import ApplicationError
from mclaw.cli import config as config_io
from mclaw.web.app import create_app


def registry(name="new-model", count=1):
    return {"deepseek": {"name": "DeepSeek", "models": {
        f"{name}-{index}": {"id": f"{name}-{index}", "name": f"Example {index}",
                             "limit": {"context": 128000}, "release_date": "2026-09-01"}
        for index in range(count)
    }}}


@pytest.fixture
def library(tmp_path, monkeypatch):
    monkeypatch.setenv("MCLAW_HOME", str(tmp_path))
    monkeypatch.setattr(models_dev, "_models_dev_cache", {})
    monkeypatch.setattr(models_dev, "_models_dev_cache_time", 0)
    monkeypatch.setattr(models_dev, "_models_dev_cache_home", "")
    models_dev._read_cache_version.cache_clear()
    monkeypatch.setattr(models_dev.requests, "get", Mock(side_effect=AssertionError("no implicit network")))
    return ModelLibraryService(tmp_path)


def test_library_reads_are_offline_and_reuse_one_parsed_cache_version(library):
    assert library.status()["updated_at"] is None
    assert library.browse()["total"] == 0
    assert models_dev._save_disk_cache(registry(count=205))
    for _ in range(10):
        result = library.browse()
        assert len(result["models"]) == 100
        assert result["total"] == 205
    assert models_dev._read_cache_version.cache_info().misses == 1
    assert len(library.browse(offset=200)["models"]) == 5
    assert library.browse(offset=900)["offset"] == 200
    assert library.browse(query="Example 204")["models"][0]["id"] == "new-model-204"
    assert library.browse(query="Example 204", offset=200)["offset"] == 0
    assert library.browse(provider="missing")["total"] == 0
    assert library.browse(provider="deepseek")["providers"][0]["profiles"][0]["callable"]
    models_dev.requests.get.assert_not_called()


def test_refresh_updates_shared_cache_and_provider_choices_without_touching_config(library, monkeypatch):
    config_io.save_config({"active_provider": "deepseek", "model": "keep-current", "unknown": True})
    config_io.save_env_value("DEEPSEEK_API_KEY", "fixture-key")
    files = {name: (library.home / name).read_bytes() for name in ("config.yaml", ".env")}
    monkeypatch.setattr(models_dev, "_fetch_network_registry", lambda timeout: registry())
    result = library.refresh()
    assert result["last_refresh"]["ok"] and not result["refreshing"]
    assert result["updated_at"] and result["models"] == 1
    assert models_dev.list_provider_models("deepseek") == ["new-model-0"]
    catalog = ProviderService(library.home).catalog()
    profile = next(item for item in catalog["catalog"] if item["id"] == "deepseek")["profiles"][0]
    assert "new-model-0" in profile["models"]
    assert profile["model"] == "keep-current"
    assert {name: (library.home / name).read_bytes() for name in files} == files


@pytest.mark.parametrize("failure", ["network", "empty", "disk"])
def test_failed_refresh_preserves_the_previous_cache(library, monkeypatch, failure):
    assert models_dev._save_disk_cache(registry("old"))
    before = models_dev._get_cache_path().read_bytes()
    if failure == "network":
        monkeypatch.setattr(models_dev, "_fetch_network_registry", Mock(side_effect=ValueError("unavailable")))
    else:
        monkeypatch.setattr(models_dev, "_fetch_network_registry", lambda timeout: {} if failure == "empty" else registry())
    if failure == "disk":
        monkeypatch.setattr(models_dev.os, "replace", Mock(side_effect=OSError("read-only")))
    result = library.refresh()
    assert result["last_refresh"]["ok"] is False
    assert result["models"] == 1
    assert models_dev._get_cache_path().read_bytes() == before
    assert library.browse()["models"][0]["id"] == "old-0"
    assert not list(library.home.glob(".models-dev-*.tmp"))


def test_refresh_is_single_flight_and_browsing_remains_available(library, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def fetch(timeout):
        entered.set()
        assert release.wait(3)
        return registry()
    monkeypatch.setattr(models_dev, "_fetch_network_registry", fetch)
    worker = threading.Thread(target=library.refresh)
    worker.start()
    try:
        assert entered.wait(2)
        assert library.status()["refreshing"]
        assert library.browse()["total"] == 0
        with pytest.raises(ApplicationError) as error:
            library.refresh()
        assert error.value.code == "MODEL_LIBRARY_BUSY"
    finally:
        release.set()
        worker.join(3)
    assert not library.status()["refreshing"]
    assert not worker.is_alive()


def test_tui_reads_a_new_disk_version_even_with_an_existing_memory_cache(library):
    assert models_dev._save_disk_cache(registry("old"))
    assert models_dev.fetch_models_dev() == registry("old")
    assert models_dev._save_disk_cache(registry("changed-in-other-process"))
    assert models_dev.fetch_models_dev() == registry("changed-in-other-process")
    assert library.browse()["models"][0]["id"] == "changed-in-other-process-0"


def test_model_library_api_requires_auth_and_does_not_start_setup(library, monkeypatch):
    class Setup:
        def close(self):
            pass
        def start(self):
            pytest.fail("model library must not enter Setup")
    monkeypatch.setattr(models_dev, "_fetch_network_registry", lambda timeout: registry())
    with TestClient(create_app(library.home, "fixture-token", setup=Setup()), base_url="http://127.0.0.1") as client:
        path = "/api/v1/providers/library"
        assert client.get(path).status_code == 403
        assert client.post(path).status_code == 403
        headers = {"Authorization": "Bearer fixture-token"}
        assert client.get(path, headers=headers).json()["total"] == 0
        assert client.post(path, headers=headers).json()["last_refresh"]["ok"]
        assert client.get(path, headers=headers).json()["total"] == 1
        assert client.get(path + "?offset=invalid", headers=headers).status_code == 422
        assert client.get(path + "?offset=-1", headers=headers).status_code == 422


def test_cache_home_changes_do_not_reuse_another_devices_memory(library, monkeypatch, tmp_path):
    assert models_dev._save_disk_cache(registry("first-home"))
    assert models_dev.fetch_models_dev()
    other = tmp_path / "other-home"
    monkeypatch.setenv("MCLAW_HOME", str(other))
    assert models_dev.cached_models_dev() == {}
    monkeypatch.setattr(models_dev, "_fetch_network_registry", Mock(side_effect=ValueError("offline")))
    assert models_dev.fetch_models_dev() == {}
