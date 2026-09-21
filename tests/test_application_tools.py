# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from starlette.testclient import TestClient

from mclaw.application.sessions import ApplicationError, SessionService
from mclaw.application.tools import ToolService
from mclaw.cli import config as config_io
from mclaw.cli.runtime.tool_configuration import refresh_interactive_tools
from mclaw.providers.registry import get_runtime_profile
from mclaw.runtime import secrets
from mclaw.tools.toolsets import resolve_multiple_toolsets
from mclaw.web.app import create_app


@pytest.fixture
def tools(tmp_path, monkeypatch):
    monkeypatch.setenv("MCLAW_HOME", str(tmp_path))
    for name in {"TAVILY_API_KEY", "FIRECRAWL_API_KEY", *get_runtime_profile("qwen").env_vars, *get_runtime_profile("qwen-intl").env_vars}:
        monkeypatch.setenv(name, "")
    config_io.save_config({"toolsets": ["mclaw-required"], "model": "preserved-model",
                           "channels": {"weixin": {"enabled": False, "toolsets": ["mclaw-required"]}},
                           "unknown_field": {"keep": True}})
    return ToolService(tmp_path)


def save(service, identifier, **changes):
    return service.save(identifier, {"enabled": True, "revision": service.catalog()["revision"], **changes})


def item(service, name):
    return next(row for row in service.catalog()["items"] if row["id"] == name)


def test_required_switches_are_enforced_and_catalog_is_read_only(tools):
    before = (tools.home / "config.yaml").read_bytes()
    for row in tools.catalog()["items"]:
        if row["required"]:
            assert row["enabled"]
            with pytest.raises(ApplicationError, match="保持开启"):
                save(tools, row["id"], enabled=False)
    assert (tools.home / "config.yaml").read_bytes() == before
    assert not (tools.home / secrets.ALLOWLIST_FILENAME).exists()


def test_independent_web_switches_preserve_unknown_config_and_channel_settings(tools):
    save(tools, "web_extract")
    assert item(tools, "web_extract")["enabled"]
    assert not item(tools, "web_search")["enabled"]
    config = config_io.load_config()
    assert "web" in config["channels"]["weixin"]["toolsets"]
    assert config["channels"]["weixin"]["enabled"] is False
    assert config["unknown_field"] == {"keep": True}
    save(tools, "web_extract", enabled=False)
    from mclaw.tools.dispatch import get_tool_definitions
    config = config_io.load_config()
    _definitions, names = get_tool_definitions(enabled_toolsets=config["toolsets"], config=config)
    assert "web_extract" not in names and "terminal" in names


def test_all_preset_can_disable_optional_without_removing_required(tools):
    config_io.save_config({"toolsets": ["all"]})
    save(tools, "browser", enabled=False)
    raw = config_io.load_raw_user_config()
    assert "all" in raw["toolsets"]
    assert "browser_download" in raw["tools"]["disabled"]
    assert "terminal" not in raw["tools"]["disabled"]


def test_missing_key_does_not_enable_or_authorize_anything(tools):
    before = (tools.home / "config.yaml").read_bytes()
    with pytest.raises(ApplicationError) as failure:
        save(tools, "web_search", backend="tavily")
    assert failure.value.code == "TOOL_CREDENTIAL_REQUIRED"
    assert (tools.home / "config.yaml").read_bytes() == before
    assert not secrets.is_authorized("tool:web_search", "TAVILY_API_KEY")


def test_reuse_credential_is_scoped_and_key_never_leaks(tools):
    config_io.save_env_value("TAVILY_API_KEY", "fixture-tavily-credential")
    assert not item(tools, "web_search")["ready"]
    result = save(tools, "web_search", backend="tavily", credential_source="TAVILY_API_KEY")
    assert item(tools, "web_search")["ready"]
    assert secrets.is_authorized("tool:web_search", "TAVILY_API_KEY")
    assert not secrets.is_authorized("tool:web_extract", "TAVILY_API_KEY")
    assert "fixture-tavily-credential" not in json.dumps(result)
    assert "fixture-tavily-credential" not in (tools.home / "config.yaml").read_text()


@pytest.mark.parametrize("identifier", ["vision", "web_search"])
def test_regional_backend_and_explicit_credential_source_reach_runtime(tools, identifier):
    from mclaw.tools.search.credentials import resolve_dashscope_creds
    from mclaw.tools.vision.credentials import resolve_vision_credentials
    profile = get_runtime_profile("qwen-intl")
    name = profile.env_vars[0]
    result = save(tools, identifier, backend="dashscope-intl", credential_source=name,
                  api_key="fixture-new-intl-key", model="qwen3-vl-flash" if identifier == "vision" else "qwen-plus")
    config = config_io.load_config()
    credentials = resolve_vision_credentials(config=config) if identifier == "vision" else resolve_dashscope_creds(config=config)
    key = credentials.api_key if identifier == "vision" else credentials["api_key"]
    endpoint = credentials.base_url if identifier == "vision" else credentials["base_url"]
    assert key == "fixture-new-intl-key" and endpoint == profile.base_url
    assert "fixture-new-intl-key" not in json.dumps(result)
    assert item(tools, identifier)["ready"]
    save(tools, identifier, enabled=False)
    assert not item(tools, identifier)["enabled"]


def test_save_failure_restores_config_env_and_allowlist(tools, monkeypatch):
    config_io.save_env_value("TAVILY_API_KEY", "fixture-original")
    secrets.authorize("tool:web_extract", ["TAVILY_API_KEY"])
    paths = [tools.home / name for name in ("config.yaml", ".env", secrets.ALLOWLIST_FILENAME)]
    before = [path.read_bytes() for path in paths]
    monkeypatch.setattr(config_io, "save_config", Mock(side_effect=OSError("write failure")))
    with pytest.raises(ApplicationError) as failure:
        save(tools, "web_search", backend="tavily", api_key="fixture-replacement")
    assert failure.value.code == "TOOL_SAVE_FAILED"
    assert [path.read_bytes() for path in paths] == before
    assert config_io.get_env_value("TAVILY_API_KEY") == "fixture-original"


@pytest.mark.parametrize("identifier", ["vision", "web_search"])
def test_selected_qwen_alias_wins_over_another_already_authorized_key(tools, identifier):
    from mclaw.tools.search.credentials import resolve_dashscope_creds
    from mclaw.tools.vision.credentials import resolve_vision_credentials
    config_io.save_env_value("DASHSCOPE_API_KEY", "fixture-primary")
    config_io.save_env_value("QWEN_API_KEY", "fixture-selected")
    scope = "tool:vision_analyze" if identifier == "vision" else "tool:web_search"
    secrets.authorize(scope, ["DASHSCOPE_API_KEY", "QWEN_API_KEY"])
    save(tools, identifier, backend="dashscope", credential_source="QWEN_API_KEY")
    config = config_io.load_config()
    value = resolve_vision_credentials(config=config).api_key if identifier == "vision" else resolve_dashscope_creds(config=config)["api_key"]
    assert value == "fixture-selected"


def test_stale_revision_and_unrelated_secret_source_cannot_overwrite(tools):
    revision = tools.catalog()["revision"]
    save(tools, "browser")
    with pytest.raises(ApplicationError) as failure:
        tools.save("browser", {"enabled": False, "revision": revision})
    assert failure.value.code == "TOOLS_CONFLICT"
    with pytest.raises(ApplicationError) as failure:
        save(tools, "web_search", backend="tavily", credential_source="WEIXIN_TOKEN")
    assert failure.value.code == "INVALID_CREDENTIAL_SOURCE"


def test_tui_refresh_preserves_model_and_scoped_toolset(tools):
    old = config_io.load_config()
    agent = SimpleNamespace(config=old, enabled_toolsets=old["toolsets"], _discover_tools=Mock(), mark_prompt_epoch_dirty=Mock())
    save(tools, "web_extract")
    refresh_interactive_tools(agent, tools.home.parent)
    assert "web_extract" in resolve_multiple_toolsets(agent.enabled_toolsets)
    assert agent.config["model"] == "preserved-model"
    agent._discover_tools.assert_called_once()
    agent.enabled_toolsets = ["delegation_skill_read"]
    save(tools, "web_extract", enabled=False)
    refresh_interactive_tools(agent, tools.home.parent)
    assert agent.enabled_toolsets == ["delegation_skill_read"]
    assert "web_extract" in agent.config["tools"]["disabled"]


def test_tool_routes_require_auth_and_persist_changes(tools):
    sessions = SessionService(tools.home, runner_factory=lambda _: None)
    app = create_app(tools.home, "fixture-token", service=sessions, tools=tools)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/api/v1/tools").status_code == 403
        headers = {"Authorization": "Bearer fixture-token"}
        catalog = client.get("/api/v1/tools", headers=headers).json()
        result = client.put("/api/v1/tools/web_extract", headers=headers, json={"enabled": True, "revision": catalog["revision"]})
        assert result.status_code == 200
        assert next(row for row in result.json()["items"] if row["id"] == "web_extract")["enabled"]
