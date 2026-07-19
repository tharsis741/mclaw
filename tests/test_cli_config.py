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
