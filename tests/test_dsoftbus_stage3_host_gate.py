# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import copy
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from mclaw.channels.dingtalk.config import DingTalkConfig
from mclaw.channels.weixin.config import WeixinConfig
from mclaw.cli import config as cli_config
from mclaw.dsoftbus import entrypoint
from mclaw.runtime import kaihong
from mclaw.runtime.features import FeatureState, runtime_features
from mclaw.scheduler.runner import SchedulerRunner
from mclaw.tools.toolsets import (
    DSOFTBUS_TOOLS,
    resolve_toolset,
    validate_toolset,
)


OH61_PARAMETERS = {
    "const.ohos.version": "KaihongOS 6.1.0.04",
    "const.ohos.fullname": "OpenHarmony-6.1.0.31",
    "const.product.software.version": "M-Robots OS 6.1.0.04Stan",
}

def _make_kaihong_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    parameters: dict[str, str],
    *,
    machine: str = "aarch64",
) -> kaihong.KaihongRuntime:
    monkeypatch.setattr(kaihong, "get_mclaw_home", lambda: tmp_path)
    monkeypatch.setattr(kaihong, "_pet_feature", lambda: FeatureState.DISABLED)
    monkeypatch.setattr(kaihong, "_browser_feature", lambda: FeatureState.DISABLED)
    monkeypatch.setattr(kaihong.host_platform, "machine", lambda: machine)
    monkeypatch.setattr(
        kaihong.BootstrapPathResolver,
        "read_ohos_parameters",
        staticmethod(lambda: dict(parameters)),
    )
    monkeypatch.setattr(kaihong.shutil, "which", lambda _name: None)
    return kaihong.KaihongRuntime()


def test_runtime_feature_registry_gates_all_three_dsoftbus_tools() -> None:
    disabled = runtime_features()
    enabled = runtime_features(dsoftbus=FeatureState.ENABLED)

    assert disabled.get("dsoftbus").state == FeatureState.DISABLED
    assert not disabled.toolset_enabled("dsoftbus")
    assert all(not disabled.tool_enabled(name) for name in DSOFTBUS_TOOLS)
    assert enabled.toolset_enabled("dsoftbus")
    assert all(enabled.tool_enabled(name) for name in DSOFTBUS_TOOLS)


def test_kaihong_oh61_identity_enables_dsoftbus_candidate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime = _make_kaihong_runtime(monkeypatch, tmp_path, OH61_PARAMETERS)

    assert runtime.features.is_enabled("dsoftbus")
    assert runtime.features.get("dsoftbus").reason == "OH61_CANDIDATE"


@pytest.mark.parametrize(
    ("parameters", "machine", "reason"),
    [
        (
            {
                **OH61_PARAMETERS,
                "const.ohos.fullname": "OpenHarmony-6.10.0.1",
            },
            "aarch64",
            "OH_IDENTITY_CONFLICT",
        ),
        (
            {
                **OH61_PARAMETERS,
                "const.product.software.version": "M-Robots OS 5.0",
            },
            "arm64",
            "OH_IDENTITY_CONFLICT",
        ),
        (
            {"const.ohos.version": "KaihongOS 6.1.0.04"},
            "aarch64",
            "OH_IDENTITY_INCOMPLETE",
        ),
        (OH61_PARAMETERS, "x86_64", "OH_ARCH_UNSUPPORTED"),
    ],
)
def test_kaihong_candidate_fails_closed_on_identity_or_architecture(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    parameters: dict[str, str],
    machine: str,
    reason: str,
) -> None:
    runtime = _make_kaihong_runtime(
        monkeypatch,
        tmp_path,
        parameters,
        machine=machine,
    )

    assert not runtime.features.is_enabled("dsoftbus")
    assert runtime.features.get("dsoftbus").reason == reason


def test_missing_identity_keys_use_sanitized_live_parameter_probe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from mclaw.platform import detect as platform_detect

    monkeypatch.setattr(kaihong, "get_mclaw_home", lambda: tmp_path)
    monkeypatch.setattr(kaihong, "_pet_feature", lambda: FeatureState.DISABLED)
    monkeypatch.setattr(kaihong, "_browser_feature", lambda: FeatureState.DISABLED)
    monkeypatch.setattr(kaihong.host_platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(
        kaihong.BootstrapPathResolver,
        "read_ohos_parameters",
        staticmethod(lambda: {"const.ohos.version": OH61_PARAMETERS["const.ohos.version"]}),
    )
    monkeypatch.setattr(
        kaihong.shutil,
        "which",
        lambda name: "/bin/param" if name == "param" else None,
    )
    calls: list[tuple[str, str]] = []

    def read_live(executable: str, key: str) -> str:
        calls.append((executable, key))
        return OH61_PARAMETERS[key]

    monkeypatch.setattr(platform_detect, "_read_live_parameter", read_live)

    runtime = kaihong.KaihongRuntime()

    assert runtime.features.is_enabled("dsoftbus")
    assert calls == [
        ("/bin/param", "const.ohos.fullname"),
        ("/bin/param", "const.product.software.version"),
    ]


def test_dsoftbus_default_config_requires_all_three_user_opt_ins() -> None:
    config = copy.deepcopy(cli_config.DEFAULT_CONFIG)

    cli_config._validate_config(config)
    assert config["dsoftbus"] == {
        "enabled": False,
        "discovery_without_provider": False,
        "accept_remote_messages": False,
        "allow_remote_tools": False,
        "per_peer_requests_per_minute": 6,
        "global_requests_per_minute": 12,
        "remote_token_budget_per_hour": 100_000,
    }


def test_product_launcher_sets_the_release_python_environment() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    assert not (repository_root / "mclaw" / "dsoftbus" / "native").exists()
    source = (
        repository_root
        / "mclaw"
        / "dsoftbus"
        / "resources"
        / "mclaw_product_launcher.sh"
    ).read_text(encoding="utf-8")

    assert source.startswith("#!/system/bin/sh\n# Copyright © 2026 ")
    assert "MCLAW_DSOFTBUS_PRODUCT_ENTRY" not in source
    assert "MCLAW_DSOFTBUS_PROFILE" not in source
    assert 'export LD_PRELOAD="${MCLAW_PYTHON_PRELOAD}"' in source
    assert source.rstrip().endswith('exec python3 -m mclaw.cli.main "$@"')
    assert "eval " not in source


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("enabled", True),
        ("enabled", "on"),
        ("discovery_without_provider", 1),
        ("accept_remote_messages", "false"),
        ("allow_remote_tools", "false"),
        ("allow_remote_tools", 1),
        ("per_peer_requests_per_minute", True),
        ("per_peer_requests_per_minute", 0),
        ("per_peer_requests_per_minute", 61),
        ("global_requests_per_minute", 5),
        ("global_requests_per_minute", 241),
        ("remote_token_budget_per_hour", 999),
        ("remote_token_budget_per_hour", 10_000_001),
    ],
)
def test_dsoftbus_config_rejects_unknown_types_and_out_of_range_values(
    field: str,
    value: object,
) -> None:
    config = copy.deepcopy(cli_config.DEFAULT_CONFIG)
    config["dsoftbus"][field] = value

    with pytest.raises(cli_config.ConfigError) as caught:
        cli_config._validate_config(config)

    assert caught.value.code == "DSOFTBUS_CONFIG_INVALID"


def test_discovery_marker_conflicts_with_disabled_runtime() -> None:
    config = copy.deepcopy(cli_config.DEFAULT_CONFIG)
    config["dsoftbus"]["enabled"] = False
    config["dsoftbus"]["discovery_without_provider"] = True

    with pytest.raises(cli_config.ConfigError) as caught:
        cli_config._validate_config(config)

    assert caught.value.code == "DSOFTBUS_CONFIG_INVALID"


def test_dsoftbus_config_rejects_unknown_fields_and_non_mapping() -> None:
    unknown = copy.deepcopy(cli_config.DEFAULT_CONFIG)
    unknown["dsoftbus"]["future"] = True
    with pytest.raises(cli_config.ConfigError, match="keys"):
        cli_config._validate_config(unknown)

    scalar = copy.deepcopy(cli_config.DEFAULT_CONFIG)
    scalar["dsoftbus"] = "auto"
    with pytest.raises(cli_config.ConfigError) as caught:
        cli_config._validate_config(scalar)
    assert caught.value.code == "DSOFTBUS_CONFIG_INVALID"


def test_dsoftbus_setup_uses_one_optional_capability_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mclaw.cli import main as cli_main
    from mclaw.cli.tui import console, selection_prompt

    runtime = SimpleNamespace(
        features=SimpleNamespace(is_enabled=lambda name: name == "dsoftbus")
    )
    config = copy.deepcopy(cli_config.DEFAULT_CONFIG)
    config["dsoftbus"]["discovery_without_provider"] = False
    prompts: list[tuple[str, list[dict], dict]] = []

    def choose(title, items, **kwargs):
        prompts.append((title, list(items), dict(kwargs)))
        return ["dsoftbus"]

    monkeypatch.setattr(selection_prompt, "prompt_multi_select", choose)
    monkeypatch.setattr(console, "print_plain", lambda *_args, **_kwargs: None)

    assert cli_main._run_setup_dsoftbus_selection(
        config,
        runtime=runtime,
    )
    assert [title for title, _items, _kwargs in prompts] == ["M-Claw 可选能力"]
    assert prompts[0][1] == [
        {
            "id": "dsoftbus",
            "label": "可信设备协作",
            "description": "发现并连接Open Harmony可信设备，允许双方 M-Claw 通信并使用对方设备上的工具。",
        }
    ]
    assert prompts[0][2]["default_selected"] == []
    assert config["dsoftbus"]["enabled"] == "auto"
    assert config["dsoftbus"]["discovery_without_provider"] is True
    assert config["dsoftbus"]["accept_remote_messages"] is True
    assert config["dsoftbus"]["allow_remote_tools"] is True
    cli_config._validate_config(config)


def test_dsoftbus_setup_cancellation_does_not_apply_partial_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mclaw.cli import main as cli_main
    from mclaw.cli.tui import console, selection_prompt

    runtime = SimpleNamespace(
        features=SimpleNamespace(is_enabled=lambda name: name == "dsoftbus")
    )
    config = copy.deepcopy(cli_config.DEFAULT_CONFIG)
    before = copy.deepcopy(config)

    def cancel(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(selection_prompt, "prompt_multi_select", cancel)
    monkeypatch.setattr(console, "print_plain", lambda *_args, **_kwargs: None)

    with pytest.raises(cli_main._SetupCancelled):
        cli_main._run_setup_dsoftbus_selection(
            config,
            runtime=runtime,
        )

    assert config == before


def test_setup_capability_flow_persists_dsoftbus_answers_for_oh61_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mclaw.cli import main as cli_main
    from mclaw.cli.tui import console, selection_prompt
    from mclaw.runtime.manager import RuntimeManager

    runtime = SimpleNamespace(
        features=SimpleNamespace(
            is_enabled=lambda name: name == "dsoftbus",
            toolset_enabled=lambda _name: False,
        )
    )
    config = copy.deepcopy(cli_config.DEFAULT_CONFIG)
    prompt_titles: list[str] = []

    def choose_multiple(title, items, **_kwargs):
        prompt_titles.append(title)
        if title == "M-Claw 可选能力":
            assert any(item.get("id") == "dsoftbus" for item in items)
            return ["dsoftbus"]
        return []

    monkeypatch.setattr(RuntimeManager, "current", staticmethod(lambda _config: runtime))
    monkeypatch.setattr(selection_prompt, "prompt_multi_select", choose_multiple)
    monkeypatch.setattr(console, "print_plain", lambda *_args, **_kwargs: None)

    cli_main._run_setup_capability_selection(config)

    assert prompt_titles == [
        "M-Claw 可选能力",
        "M-Claw IM 交互配置",
        "M-Claw 语音输入",
    ]
    assert config["dsoftbus"]["enabled"] == "auto"
    assert config["dsoftbus"]["discovery_without_provider"] is True
    assert config["dsoftbus"]["accept_remote_messages"] is True
    assert config["dsoftbus"]["allow_remote_tools"] is True
    cli_config._validate_config(config)


def test_product_discovery_only_config_does_not_repeat_first_run_setup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from mclaw import constants
    from mclaw.cli import main as cli_main

    (tmp_path / "config.yaml").write_text("dsoftbus: {}\n", encoding="utf-8")
    config = copy.deepcopy(cli_config.DEFAULT_CONFIG)
    config["dsoftbus"].update(
        {
            "enabled": "auto",
            "discovery_without_provider": True,
        }
    )
    monkeypatch.setattr(constants, "get_mclaw_home", lambda: tmp_path)
    monkeypatch.setattr(cli_config, "load_config", lambda *, strict: config)
    monkeypatch.setattr(entrypoint, "is_discovery_only_candidate", lambda _cfg: True)

    assert cli_main._first_run_check() is False

    monkeypatch.setattr(entrypoint, "is_discovery_only_candidate", lambda _cfg: False)
    assert cli_main._first_run_check() is True


def test_fresh_product_setup_can_enable_discovery_without_a_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mclaw.cli import main as cli_main
    from mclaw.cli.tui import console, selection_prompt
    from mclaw.runtime.manager import RuntimeManager

    config = copy.deepcopy(cli_config.DEFAULT_CONFIG)
    saved: list[dict] = []
    runtime = SimpleNamespace(
        features=SimpleNamespace(is_enabled=lambda name: name == "dsoftbus")
    )
    monkeypatch.setattr(cli_config, "ensure_mclaw_home", lambda: None)
    monkeypatch.setattr(cli_config, "load_config", lambda: config)
    monkeypatch.setattr(cli_config, "save_config", lambda value: saved.append(copy.deepcopy(value)))
    monkeypatch.setattr(console, "print_plain", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        selection_prompt,
        "prompt_multi_select",
        lambda title, *_args, **_kwargs: (
            ["dsoftbus"] if title == "M-Claw 可选能力" else []
        ),
    )
    monkeypatch.setattr(cli_main, "_print_setup_intro", lambda: False)
    monkeypatch.setattr(cli_main, "_print_setup_step", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(RuntimeManager, "current", staticmethod(lambda _config: runtime))

    cli_main._run_setup_impl(object())

    assert len(saved) == 1
    assert saved[0]["dsoftbus"]["enabled"] == "auto"
    assert saved[0]["dsoftbus"]["discovery_without_provider"] is True
    assert saved[0]["dsoftbus"]["accept_remote_messages"] is True
    assert saved[0]["dsoftbus"]["allow_remote_tools"] is True
    cli_config._validate_config(saved[0])


def test_fresh_setup_without_provider_or_discovery_keeps_existing_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mclaw.cli import main as cli_main
    from mclaw.cli.tui import console, selection_prompt
    from mclaw.runtime.manager import RuntimeManager

    config = copy.deepcopy(cli_config.DEFAULT_CONFIG)
    saved: list[dict] = []
    runtime = SimpleNamespace(
        features=SimpleNamespace(is_enabled=lambda name: name == "dsoftbus")
    )
    monkeypatch.setattr(cli_config, "ensure_mclaw_home", lambda: None)
    monkeypatch.setattr(cli_config, "load_config", lambda: config)
    monkeypatch.setattr(cli_config, "save_config", lambda value: saved.append(copy.deepcopy(value)))
    monkeypatch.setattr(console, "print_plain", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(selection_prompt, "prompt_multi_select", lambda *_a, **_k: [])
    monkeypatch.setattr(cli_main, "_print_setup_intro", lambda: False)
    monkeypatch.setattr(cli_main, "_print_setup_step", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(RuntimeManager, "current", staticmethod(lambda _config: runtime))

    with pytest.raises(SystemExit) as caught:
        cli_main._run_setup_impl(object())

    assert caught.value.code == 1
    assert saved == []


def test_dsoftbus_setup_values_round_trip_through_user_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    config = copy.deepcopy(cli_config.DEFAULT_CONFIG)
    config["dsoftbus"].update(
        {
            "enabled": "auto",
            "accept_remote_messages": True,
            "allow_remote_tools": True,
        }
    )
    monkeypatch.setattr(cli_config, "ensure_mclaw_home", lambda: None)
    monkeypatch.setattr(cli_config, "get_config_path", lambda: config_path)

    cli_config.save_config(config)
    loaded = cli_config.load_config(strict=True)

    assert loaded["dsoftbus"] == config["dsoftbus"]


def test_non_oh_setup_does_not_read_write_or_prompt_for_dsoftbus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mclaw.cli import main as cli_main
    from mclaw.cli.tui import selection_prompt

    class NoDsoftbusRead(dict):
        def get(self, key, default=None):
            if key == "dsoftbus":
                raise AssertionError("non-OH setup read dsoftbus config")
            return super().get(key, default)

    runtime = SimpleNamespace(
        features=SimpleNamespace(is_enabled=lambda _name: False)
    )
    config = NoDsoftbusRead({"model": "local"})
    before = dict(config)
    monkeypatch.setattr(
        selection_prompt,
        "prompt_multi_select",
        lambda *_a, **_k: pytest.fail("non-OH setup exposed DSoftBus prompt"),
    )

    assert not cli_main._run_setup_dsoftbus_selection(
        config,
        runtime=runtime,
    )
    assert dict(config) == before


def test_oh_setup_eligibility_depends_on_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mclaw.cli import main as cli_main
    from mclaw.cli.tui import console, selection_prompt

    runtime = SimpleNamespace(
        features=SimpleNamespace(is_enabled=lambda name: name == "dsoftbus")
    )
    config = copy.deepcopy(cli_config.DEFAULT_CONFIG)
    prompts: list[str] = []

    def choose(title, _items, **_kwargs):
        prompts.append(title)
        return ["dsoftbus"]

    monkeypatch.setattr(
        selection_prompt,
        "prompt_multi_select",
        choose,
    )
    monkeypatch.setattr(console, "print_plain", lambda *_args, **_kwargs: None)

    assert cli_main._run_setup_dsoftbus_selection(
        config,
        runtime=runtime,
    )
    assert prompts == ["M-Claw 可选能力"]
    assert config["dsoftbus"]["enabled"] == "auto"
    assert config["dsoftbus"]["accept_remote_messages"] is True
    assert config["dsoftbus"]["allow_remote_tools"] is True


def test_project_config_cannot_supply_dsoftbus_or_platform_toolsets() -> None:
    with pytest.raises(cli_config.ConfigError, match="dsoftbus"):
        cli_config._normalize_project_config({"dsoftbus": {"enabled": "auto"}})

    config = copy.deepcopy(cli_config.DEFAULT_CONFIG)
    config["toolsets"] = ["mclaw-required", "dsoftbus"]
    with pytest.raises(cli_config.ConfigError, match="disallowed toolset"):
        cli_config._validate_config(config)

    config = copy.deepcopy(cli_config.DEFAULT_CONFIG)
    config["channels"]["weixin"]["toolsets"] = ["dsoftbus-remote"]
    with pytest.raises(cli_config.ConfigError, match="disallowed toolset"):
        cli_config._validate_config(config)


def test_non_strict_loader_returns_a_clean_default_after_validation_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("dsoftbus:\n  enabled: true\nmodel: must-not-survive\n", encoding="utf-8")
    warnings: list[str] = []
    monkeypatch.setattr(cli_config, "ensure_mclaw_home", lambda: None)
    monkeypatch.setattr(cli_config, "get_config_path", lambda: config_path)
    monkeypatch.setattr(cli_config, "print_plain", warnings.append)

    loaded = cli_config.load_config(strict=False)

    assert loaded == cli_config.DEFAULT_CONFIG
    assert loaded is not cli_config.DEFAULT_CONFIG
    assert warnings == ["Warning: Failed to load config: DSOFTBUS_CONFIG_INVALID"]


def test_raw_user_loader_returns_only_independent_user_fields(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "dsoftbus:\n  accept_remote_messages: true\nunknown_user_field:\n  nested: [one]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cli_config, "ensure_mclaw_home", lambda: None)
    monkeypatch.setattr(cli_config, "get_config_path", lambda: config_path)

    raw = cli_config.load_raw_user_config(strict=True)
    raw["unknown_user_field"]["nested"].append("two")
    reread = cli_config.load_raw_user_config(strict=True)

    assert "model" not in raw
    assert reread["dsoftbus"] == {"accept_remote_messages": True}
    assert reread["unknown_user_field"]["nested"] == ["one"]


def test_config_source_snapshot_is_deeply_immutable_and_returns_fresh_copies() -> None:
    user = {"providers": {"local": {"model": "one"}}}
    project = {"display": {"pet": {"enabled": True, "scale": 2.0}}}
    snapshot = cli_config.ConfigSourceSnapshot(user, project, Path("C:/project/.mclaw.yaml"))
    user["providers"]["local"]["model"] = "mutated"

    assert snapshot.user_config_copy()["providers"]["local"]["model"] == "one"
    with pytest.raises(TypeError):
        snapshot.raw_user_config["new"] = True  # type: ignore[index]
    with pytest.raises(TypeError):
        snapshot.raw_user_config["providers"]["local"]["model"] = "bad"  # type: ignore[index]

    first = snapshot.user_config_copy()
    second = snapshot.user_config_copy()
    first["providers"]["local"]["model"] = "changed"
    assert second["providers"]["local"]["model"] == "one"


def test_merged_config_from_snapshot_does_not_read_disk_and_renormalizes_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = cli_config.ConfigSourceSnapshot(
        {"dsoftbus": {"accept_remote_messages": True}},
        {"display": {"pet": {"enabled": True, "scale": 1.5}}},
        Path("C:/workspace/.mclaw.yaml"),
    )
    monkeypatch.setattr(cli_config, "ensure_mclaw_home", lambda: None)
    monkeypatch.setattr(
        cli_config,
        "load_config_source_snapshot",
        lambda _cwd=None: pytest.fail("disk snapshot must not be reloaded"),
    )

    merged = cli_config.load_merged_config(source_snapshot=snapshot)

    assert merged["dsoftbus"]["accept_remote_messages"] is True
    assert merged["display"]["pet"]["enabled"] == cli_config.DEFAULT_CONFIG["display"]["pet"]["enabled"]
    assert merged["display"]["pet"]["scale"] == 1.5
    assert Path(merged["_project_config_dir"]) == Path("C:/workspace")


def test_platform_and_scoped_toolsets_require_explicit_internal_authority() -> None:
    assert resolve_toolset("dsoftbus") == DSOFTBUS_TOOLS
    assert not validate_toolset("dsoftbus")
    assert validate_toolset("dsoftbus", allow_platform=True)
    assert not validate_toolset("dsoftbus-remote", allow_platform=True)
    assert validate_toolset("dsoftbus-remote", allow_scoped=True)

    default_all = set(resolve_toolset("all"))
    platform_all = set(resolve_toolset("all", include_platform=True))
    assert default_all.isdisjoint(DSOFTBUS_TOOLS)
    assert set(DSOFTBUS_TOOLS) <= platform_all
    assert "dsoftbus-remote" not in default_all


def test_channel_and_scheduler_final_boundaries_remove_platform_and_scoped_toolsets() -> None:
    config = {
        "toolsets": ["mclaw-required", "dsoftbus", "dsoftbus-remote"],
        "channels": {
            "weixin": {"toolsets": ["terminal", "dsoftbus"]},
            "dingtalk": {"toolsets": ["file", "dsoftbus-remote"]},
        },
    }

    assert WeixinConfig.from_config(config).toolsets == ["terminal"]
    assert DingTalkConfig.from_config(config).toolsets == ["file"]

    runner = SchedulerRunner.__new__(SchedulerRunner)
    job = SimpleNamespace(
        enabled_toolsets=["terminal", "dsoftbus", "dsoftbus-remote", "terminal"]
    )
    assert runner._effective_toolsets(job) == ["terminal"]
    job.enabled_toolsets = ["dsoftbus", "dsoftbus-remote"]
    assert runner._effective_toolsets(job) == ["mclaw-required"]


def test_persisted_provider_selection_uses_only_exact_allowed_shapes() -> None:
    assert entrypoint.has_persisted_provider_selection({"model": "qwen"}, None)
    assert entrypoint.has_persisted_provider_selection(
        {"active_provider": "custom", "active_provider_profile": "local"},
        None,
    )
    assert entrypoint.has_persisted_provider_selection(
        {"fallback_providers": [{"provider": "qwen", "model": "qwen-plus"}]},
        None,
    )
    assert entrypoint.has_persisted_provider_selection(
        {"providers": {"local": {"model": "test"}}},
        None,
    )
    assert entrypoint.has_persisted_provider_selection(
        {},
        {"schema_version": 1, "provider": "qwen", "model": "qwen-plus"},
    )
    assert not entrypoint.has_persisted_provider_selection(
        {},
        {"schema_version": True, "provider": "qwen", "model": "qwen-plus"},
    )
    assert not entrypoint.has_persisted_provider_selection(
        {"providers": {"local": {"api_key": "not-evidence"}}},
        None,
    )


def test_discovery_only_downgrade_requires_candidate_and_persisted_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(entrypoint, "is_discovery_only_candidate", lambda _config: True)
    base = {
        "resume_snapshot": None,
        "cli_values": {key: "" for key in ("model", "provider", "api_key", "base_url")},
        "raw_user_config": {"dsoftbus": {"discovery_without_provider": True}},
        "raw_project_config": {},
        "merged_config": copy.deepcopy(cli_config.DEFAULT_CONFIG),
    }

    assert entrypoint.can_enter_discovery_only(error_code="provider_required", **base)
    assert not entrypoint.can_enter_discovery_only(error_code="missing_model", **base)

    persisted = dict(base)
    persisted["raw_user_config"] = {"model": "configured-before"}
    assert entrypoint.can_enter_discovery_only(error_code="missing_model", **persisted)
    assert entrypoint.can_enter_discovery_only(error_code="missing_credential", **persisted)
    assert not entrypoint.can_enter_discovery_only(error_code="invalid_provider", **persisted)

    cli_selected = copy.deepcopy(persisted)
    cli_selected["cli_values"] = {**base["cli_values"], "model": "cli-model"}
    assert not entrypoint.can_enter_discovery_only(
        error_code="missing_credential",
        **cli_selected,
    )

    project_selected = copy.deepcopy(persisted)
    project_selected["raw_project_config"] = {"providers": {"local": {"model": "x"}}}
    assert not entrypoint.can_enter_discovery_only(
        error_code="missing_credential",
        **project_selected,
    )


def test_candidate_check_uses_runtime_feature_without_native_loading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mclaw.runtime.manager import RuntimeManager

    runtime = SimpleNamespace(
        features=SimpleNamespace(is_enabled=lambda name: name == "dsoftbus")
    )
    monkeypatch.setattr(RuntimeManager, "current", classmethod(lambda _cls, _config=None: runtime))

    assert entrypoint.is_discovery_only_candidate(
        {"dsoftbus": {"enabled": "auto"}},
    )
    assert not entrypoint.is_discovery_only_candidate(
        {"dsoftbus": {"enabled": False}},
    )


def test_host_gate_modules_import_without_ctypes_or_native_side_effects() -> None:
    script = (
        "import sys; "
        "import mclaw.dsoftbus.entrypoint; "
        "import mclaw.cli.config; "
        "import mclaw.runtime.features; "
        "import mclaw.tools.toolsets; "
        "assert 'ctypes' not in sys.modules; "
        "assert not any(name.startswith('mclaw.dsoftbus.native') for name in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
