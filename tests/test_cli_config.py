# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from pathlib import Path

import pytest

from mclaw.cli import config as cli_config


@pytest.mark.parametrize(
    ("yaml_text", "expected_timeout"),
    [
        ("delegation:\n  max_iterations: 50\n", 600),
        ("delegation:\n  timeout_seconds: 900\n", 900),
    ],
)
def test_delegation_timeout_default_and_user_override(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    yaml_text: str,
    expected_timeout: int,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml_text, encoding="utf-8")
    monkeypatch.setattr(cli_config, "ensure_mclaw_home", lambda: None)
    monkeypatch.setattr(cli_config, "get_config_path", lambda: config_path)

    config = cli_config.load_config(strict=True)

    assert config["delegation"]["timeout_seconds"] == expected_timeout


@pytest.mark.parametrize(
    ("yaml_text", "expected_limit"),
    [
        ("", None),
        ("agent:\n  max_turns: 200\n", 200),
    ],
)
def test_agent_iteration_limit_is_opt_in(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    yaml_text: str,
    expected_limit: int | None,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml_text, encoding="utf-8")
    monkeypatch.setattr(cli_config, "ensure_mclaw_home", lambda: None)
    monkeypatch.setattr(cli_config, "get_config_path", lambda: config_path)

    config = cli_config.load_config(strict=True)

    assert config.get("agent", {}).get("max_turns") == expected_limit


@pytest.mark.parametrize("yaml_text,first,idle", [
    ("model: old-model\n", 90, 60),
    ("agent:\n  max_turns: 200\n  first_response_timeout: 180\n", 180, 60),
    ("agent:\n  first_response_timeout: 120\n  stream_idle_timeout: 45\n", 120, 45),
])
def test_model_response_timeouts_merge_with_legacy_config(monkeypatch, tmp_path, yaml_text, first, idle):
    path = tmp_path / "config.yaml"
    path.write_text(yaml_text, encoding="utf-8")
    monkeypatch.setattr(cli_config, "ensure_mclaw_home", lambda: None)
    monkeypatch.setattr(cli_config, "get_config_path", lambda: path)
    config = cli_config.load_config(strict=True)
    assert config["agent"]["first_response_timeout"] == first
    assert config["agent"]["stream_idle_timeout"] == idle
    assert path.read_text(encoding="utf-8") == yaml_text


@pytest.mark.parametrize("key", ["first_response_timeout", "stream_idle_timeout"])
@pytest.mark.parametrize("value", ["0", "-1", "601", ".nan", ".inf", "true", "'90'", "1.5"])
def test_model_response_timeouts_reject_invalid_config(monkeypatch, tmp_path, key, value):
    path = tmp_path / "config.yaml"
    path.write_text(f"agent:\n  {key}: {value}\n", encoding="utf-8")
    monkeypatch.setattr(cli_config, "ensure_mclaw_home", lambda: None)
    monkeypatch.setattr(cli_config, "get_config_path", lambda: path)
    with pytest.raises(cli_config.ConfigError, match=key):
        cli_config.load_config(strict=True)
