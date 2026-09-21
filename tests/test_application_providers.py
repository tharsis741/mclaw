# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
from unittest.mock import Mock

import pytest
from starlette.testclient import TestClient

from mclaw.application.providers import ProviderService
from mclaw.application.sessions import ApplicationError, SessionService
from mclaw.cli import config as config_io
from mclaw.providers.registry import PROVIDER_REGISTRY
from mclaw.web.app import create_app


@pytest.fixture
def providers(tmp_path, monkeypatch):
    monkeypatch.setenv("MCLAW_HOME", str(tmp_path))
    names = {"MCLAW_API_KEY", "MCLAW_BASE_URL", "MCLAW_ANTHROPIC_API_KEY", "MCLAW_ANTHROPIC_BASE_URL", "FIXTURE_KEY"}
    for provider in PROVIDER_REGISTRY.values():
        names.update(provider.env_vars)
        if provider.base_url_env_var:
            names.add(provider.base_url_env_var)
    for name in names:
        monkeypatch.setenv(name, "")
    config_io.save_env_value("DEEPSEEK_API_KEY", "fixture-existing-key")
    config_io.save_config({"active_provider": "deepseek", "model": "existing-model",
                           "channels": {"weixin": {"enabled": False}}, "toolsets": ["mclaw-required"],
                           "unknown_field": {"keep": True}})
    return ProviderService(tmp_path)


def form(service, **changes):
    return {"provider": "qwen", "profile": "api-cn", "model": "qwen-fixture",
            "api_key": "fixture-new-key", "make_default": False,
            "revision": service.catalog()["revision"], **changes}


def test_catalog_groups_brands_before_regions_and_never_returns_credentials(providers, monkeypatch):
    import requests

    network = Mock(side_effect=AssertionError("catalog must stay local"))
    monkeypatch.setattr(requests, "get", network)
    env_read = Mock(wraps=config_io.load_env)
    monkeypatch.setattr(config_io, "load_env", env_read)
    for _ in range(25):
        result = providers.catalog()
    assert env_read.call_count == 25
    catalog = {entry["id"]: entry for entry in result["catalog"]}
    assert "qwen" in catalog and "qwen-intl" not in catalog
    assert "minimax" in catalog and "minimax-cn" not in catalog
    assert {item["label"] for item in catalog["qwen"]["profiles"]} >= {"中国区 API", "国际区 API"}
    assert all(item["runtime_provider"] for entry in catalog.values() for item in entry["profiles"])
    assert "fixture-existing-key" not in json.dumps(result)
    assert result["configured"][0]["is_default"]
    network.assert_not_called()
    assert not hasattr(providers, "_runs")


@pytest.mark.parametrize(("brand", "region", "runtime", "env"), [
    ("qwen", "api-cn", "qwen", "DASHSCOPE_API_KEY"),
    ("qwen", "api", "qwen-intl", "DASHSCOPE_INTL_API_KEY"),
    ("minimax", "api-cn", "minimax-cn", "MINIMAX_CN_API_KEY"),
    ("moonshot", "api", "moonshot-intl", "KIMI_INTL_API_KEY"),
])
def test_add_provider_preserves_default_channels_unknown_fields_and_uses_exact_region(providers, brand, region, runtime, env):
    result = providers.save(form(providers, provider=brand, profile=region))
    saved = config_io.load_raw_user_config()
    assert saved["active_provider"] == "deepseek"
    assert saved["model"] == "existing-model"
    assert saved["channels"] == {"weixin": {"enabled": False}}
    assert saved["toolsets"] == ["mclaw-required"]
    assert saved["unknown_field"] == {"keep": True}
    assert saved["setup_provider_profiles"][brand] == region
    assert {"provider": runtime, "model": "qwen-fixture"} in saved["fallback_providers"]
    assert config_io.get_env_value(env) == "fixture-new-key"
    assert "fixture-new-key" not in json.dumps(result)


def test_setting_default_is_explicit_and_reused_credentials_are_not_overwritten(providers):
    providers.save(form(providers, provider="qwen", profile="api", make_default=True))
    assert config_io.load_config()["active_provider"] == "qwen-intl"
    providers.save(form(providers, provider="qwen", profile="api", api_key="", model="updated-model"))
    assert config_io.get_env_value("DASHSCOPE_INTL_API_KEY") == "fixture-new-key"
    assert config_io.load_config()["model"] == "updated-model"


def test_first_provider_becomes_default_and_is_available_to_shared_runtime(providers):
    from mclaw.application.setup import configuration_status

    config_io.save_config({"unknown_field": 1})
    providers.save(form(providers))
    assert configuration_status()["ready"]
    assert config_io.load_config()["active_provider"] == "qwen"


def test_legacy_active_profile_keeps_its_actual_default_when_adding_another_region(providers):
    config_io.save_env_value("DASHSCOPE_INTL_API_KEY", "fixture-existing-intl")
    config_io.save_config({"active_provider": "qwen", "active_provider_profile": "api", "model": "existing-intl-model"})
    before = next(item for item in providers.catalog()["configured"] if item["is_default"])
    assert before["runtime_provider"] == "qwen-intl"
    assert before["model"] == "existing-intl-model"
    providers.save(form(providers, profile="api-cn"))
    saved = config_io.load_config()
    assert saved["model"] == "existing-intl-model"
    assert saved["active_provider_profile"] == "api"
    assert next(item for item in providers.catalog()["configured"] if item["is_default"])["runtime_provider"] == "qwen-intl"


def test_local_custom_endpoint_preserves_existing_optional_key_semantics(providers):
    providers.save(form(providers, provider="custom", profile="api", api_key="",
                        model="local-model", base_url="http://127.0.0.1:11434/v1"))
    assert config_io.get_env_value("MCLAW_BASE_URL") == "http://127.0.0.1:11434/v1"
    assert not config_io.get_env_value("MCLAW_API_KEY")
    assert any(item["runtime_provider"] == "custom" for item in providers.catalog()["configured"])


def test_existing_named_custom_provider_keeps_its_identity_and_credential_reference(providers):
    config = config_io.load_raw_user_config()
    config["providers"] = {"office-gateway": {"display_name": "Office Gateway", "api_key_env": "FIXTURE_KEY",
                                             "base_url": "https://gateway.example/v1", "model": "old-custom",
                                             "api_mode": "chat_completions"}}
    config_io.save_config(config)
    providers.save(form(providers, provider="office-gateway", profile="api", model="new-custom"))
    assert config_io.load_config()["providers"]["office-gateway"]["model"] == "new-custom"
    assert config_io.get_env_value("FIXTURE_KEY") == "fixture-new-key"


def test_probe_uses_submitted_credentials_but_saves_nothing(providers, monkeypatch):
    from mclaw.agent import context_metadata

    before = {name: (providers.home / name).read_bytes() for name in ("config.yaml", ".env")}
    probe = Mock(return_value=["model-one", "model-one", "model-two"])
    monkeypatch.setattr(context_metadata, "probe_provider_models", probe)
    assert providers.models(form(providers, model="")) == {"models": ["model-one", "model-two"]}
    assert probe.call_args.kwargs["api_key"] == "fixture-new-key"
    assert before == {name: (providers.home / name).read_bytes() for name in before}
    probe.return_value = None
    with pytest.raises(ApplicationError) as error:
        providers.models(form(providers))
    assert error.value.code == "PROVIDER_PROBE_FAILED"


def test_probes_are_bounded_and_release_capacity_after_failures(providers, monkeypatch):
    from mclaw.agent import context_metadata

    assert providers._probes.acquire(blocking=False)
    assert providers._probes.acquire(blocking=False)
    try:
        with pytest.raises(ApplicationError) as error:
            providers.models(form(providers))
        assert error.value.code == "PROVIDER_PROBE_BUSY"
    finally:
        providers._probes.release()
        providers._probes.release()
    monkeypatch.setattr(context_metadata, "probe_provider_models", Mock(side_effect=RuntimeError("fixture-private-token")))
    for _ in range(3):
        with pytest.raises(ApplicationError) as error:
            providers.models(form(providers))
        assert error.value.code == "PROVIDER_PROBE_FAILED"
        assert "fixture-private-token" not in str(error.value)


@pytest.mark.parametrize("changes", [
    {"api_key": ""}, {"api_key": "line\nbreak"}, {"provider": "unknown"},
    {"profile": "coding-plan"}, {"profile": "invalid"}, {"model": ""},
    {"provider": "custom", "base_url": "https://user:password@example.com/v1"},
    {"provider": "custom", "base_url": "https://example.com/v1?key=private"},
    {"make_default": "false"}, {"api_key": ["bad"]},
])
def test_invalid_input_never_changes_files(providers, changes):
    before = {name: (providers.home / name).read_bytes() for name in ("config.yaml", ".env")}
    with pytest.raises(ApplicationError):
        providers.save(form(providers, **changes))
    assert before == {name: (providers.home / name).read_bytes() for name in before}


def test_stale_save_and_active_setup_do_not_overwrite_configuration(providers):
    from mclaw.application.setup import _WORKFLOW_LOCK

    data = form(providers)
    config_io.save_env_value("FIXTURE_KEY", "changed-elsewhere")
    with pytest.raises(ApplicationError) as error:
        providers.save(data)
    assert error.value.code == "PROVIDER_CONFLICT"
    _WORKFLOW_LOCK.acquire()
    try:
        with pytest.raises(ApplicationError) as error:
            providers.save(form(providers))
        assert error.value.code == "SETUP_IN_PROGRESS"
    finally:
        _WORKFLOW_LOCK.release()


def test_failed_save_restores_config_credentials_and_process_environment(providers, monkeypatch):
    before = {name: (providers.home / name).read_bytes() for name in ("config.yaml", ".env")}
    data = form(providers, provider="deepseek", profile="api")
    monkeypatch.setattr(config_io, "save_config", Mock(side_effect=OSError("fixture-secret-in-exception")))
    with pytest.raises(ApplicationError) as error:
        providers.save(data)
    assert error.value.code == "PROVIDER_SAVE_FAILED"
    assert "fixture-secret-in-exception" not in str(error.value)
    assert config_io.get_env_value("DEEPSEEK_API_KEY") == "fixture-existing-key"
    assert before == {name: (providers.home / name).read_bytes() for name in before}


def test_provider_http_api_is_authenticated_and_independent_of_setup(providers):
    setup = Mock()
    service = SessionService(providers.home, runner_factory=lambda _: None)
    app = create_app(providers.home, "test-token", service=service, setup=setup, providers=providers)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/api/v1/providers").status_code == 403
        headers = {"Authorization": "Bearer test-token"}
        result = client.get("/api/v1/providers", headers=headers)
        assert result.status_code == 200
        assert client.post("/api/v1/providers", headers=headers, json=form(providers)).status_code == 200
        assert client.post("/api/v1/providers", headers=headers, json=form(providers, revision="old")).status_code == 409
        setup.start.assert_not_called()
        setup.status.assert_not_called()
