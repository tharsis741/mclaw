# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest

from mclaw.application.setup import SetupService, SetupServiceError
from mclaw.cli import config as cli_config
from mclaw.cli import main as cli_main
from mclaw.providers.registry import PROVIDER_REGISTRY


@pytest.fixture
def setup_service(tmp_path, monkeypatch):
    monkeypatch.setenv("MCLAW_HOME", str(tmp_path))
    for provider in PROVIDER_REGISTRY.values():
        for name in (*provider.env_vars, provider.base_url_env_var):
            if name:
                monkeypatch.delenv(name, raising=False)
    for name in ("MCLAW_API_KEY", "MCLAW_BASE_URL", "MCLAW_ANTHROPIC_API_KEY", "MCLAW_ANTHROPIC_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    from mclaw.agent import models_dev
    from mclaw.runtime.manager import RuntimeManager
    from mclaw.skills_hub import builtin_sync

    monkeypatch.setattr(models_dev, "list_provider_models", lambda *_a, **_k: [])
    monkeypatch.setattr(RuntimeManager, "current", lambda *_a, **_k: SimpleNamespace(features=SimpleNamespace(
        is_enabled=lambda _name: False,
        toolset_enabled=lambda _name: True,
    )))
    monkeypatch.setattr(builtin_sync, "list_builtin_skills", lambda: [
        {"name": "test-skill", "short_description": "Test", "category": "test", "source_key": "test"},
    ])
    service = SetupService()
    yield service
    service.close()
    for run in service._runs.values():
        run.thread.join(timeout=3)


def wait_for_form(service, run_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        snapshot = service.get(run_id)
        if snapshot["status"] != "running":
            return snapshot
        time.sleep(0.005)
    pytest.fail("Setup did not reach its next interaction")


def answer(service, snapshot, value):
    service.submit(snapshot["id"], snapshot["prompt"]["id"], value)
    return wait_for_form(service, snapshot["id"])


def select_deepseek(service):
    snapshot = service.start()
    assert snapshot["prompt"]["presentation"] == "welcome"
    snapshot = answer(service, snapshot, "y")
    assert snapshot["prompt"]["kind"] == "multi_select"
    snapshot = answer(service, snapshot, ["deepseek"])
    if snapshot["prompt"]["kind"] == "text":
        snapshot = answer(service, snapshot, "")
    assert snapshot["prompt"]["kind"] == "secret"
    return snapshot


@pytest.mark.parametrize(("brand", "label", "region", "runtime", "env"), [
    ("qwen", "Alibaba / Qwen", "api-cn", "qwen", "DASHSCOPE_API_KEY"),
    ("qwen", "Alibaba / Qwen", "api", "qwen-intl", "DASHSCOPE_INTL_API_KEY"),
    ("minimax", "MiniMax", "api-cn", "minimax-cn", "MINIMAX_CN_API_KEY"),
    ("minimax", "MiniMax", "api", "minimax", "MINIMAX_API_KEY"),
    ("moonshot", "Moonshot / Kimi", "api", "moonshot-intl", "KIMI_INTL_API_KEY"),
    ("moonshot", "Moonshot / Kimi", "api-cn", "moonshot", "KIMI_API_KEY"),
])
def test_setup_selects_brand_before_region_and_saves_the_exact_target(setup_service, brand, label, region, runtime, env):
    service = setup_service
    snapshot = answer(service, service.start(), "y")
    assert snapshot["prompt"]["title"] == "大模型接入配置"
    assert snapshot["prompt"]["presentation"] == "providers"
    options = {item["id"]: item for item in snapshot["prompt"]["options"]}
    assert options[brand]["label"] == label
    assert not any(name in options for name in ("qwen-intl", "minimax-cn", "moonshot-intl"))
    snapshot = answer(service, snapshot, [brand])
    assert snapshot["prompt"]["presentation"] == "profiles"
    assert snapshot["prompt"]["provider"] == brand
    assert {"中国区 API", "国际区 API"} <= {item["label"] for item in snapshot["prompt"]["options"]}
    snapshot = answer(service, snapshot, region)
    assert snapshot["prompt"]["provider"] == runtime
    assert snapshot["prompt"]["field_label"] == "API Key"
    assert snapshot["prompt"]["key_url"].startswith("https://")
    snapshot = answer(service, snapshot, "fixture-region-key-12345")
    assert cli_config.get_env_value(env) == "fixture-region-key-12345"
    model = snapshot["prompt"]["options"][0]["id"]
    snapshot = answer(service, snapshot, model)
    saved = cli_config.load_config(strict=True)
    assert saved["active_provider"] == runtime
    assert saved["setup_provider_profiles"][brand] == region
    assert "fixture-region-key" not in json.dumps(snapshot)


def test_submit_returns_the_next_form_without_a_poll(setup_service):
    snapshot = setup_service.start()
    next_step = setup_service.submit(snapshot["id"], snapshot["prompt"]["id"], "y")
    assert next_step["status"] == "waiting"
    assert next_step["prompt"]["presentation"] == "providers"


def test_slow_step_returns_running_and_remains_cancellable(setup_service, monkeypatch):
    from mclaw.application.setup_interaction import current_setup_interaction

    gate = threading.Event()
    def workflow(_args):
        interaction = current_setup_interaction.get()
        interaction.request("text", "First")
        gate.wait(3)
        interaction.check_cancelled()
        interaction.request("text", "Second")
    monkeypatch.setattr(cli_main, "_run_setup_impl", workflow)
    snapshot = setup_service.start()
    try:
        started = time.monotonic()
        next_step = setup_service.submit(snapshot["id"], snapshot["prompt"]["id"], "next")
        assert next_step["status"] == "running"
        assert time.monotonic() - started < 1
        setup_service.cancel(snapshot["id"])
    finally:
        gate.set()
    assert wait_for_form(setup_service, snapshot["id"])["status"] == "cancelled"


def test_custom_endpoint_model_choices_do_not_depend_on_terminal_output(setup_service, monkeypatch):
    monkeypatch.setattr(cli_main, "_probe_models", lambda *_a, **_k: ["model-one", "model-two"])
    service = setup_service
    snapshot = answer(service, service.start(), "y")
    snapshot = answer(service, snapshot, ["__custom_openai__"])
    assert snapshot["prompt"]["field_label"] == "API 接口地址"
    snapshot = answer(service, snapshot, "http://127.0.0.1:9999/v1")
    snapshot = answer(service, snapshot, "")
    assert snapshot["prompt"]["kind"] == "single_select"
    assert [item["label"] for item in snapshot["prompt"]["options"]] == ["model-one", "model-two", "其他模型名称"]
    snapshot = answer(service, snapshot, "2")
    assert snapshot["saved"]
    assert cli_config.load_config(strict=True)["model"] == "model-two"


def test_catalog_only_profile_explains_why_it_cannot_be_configured(setup_service):
    service = setup_service
    snapshot = answer(service, service.start(), "y")
    snapshot = answer(service, snapshot, ["qwen"])
    snapshot = answer(service, snapshot, "coding-plan")
    assert snapshot["prompt"]["presentation"] == "notice"
    assert "自定义接口" in snapshot["prompt"]["hint"]
    assert not snapshot["saved"]
    snapshot = answer(service, snapshot, "continue")
    assert snapshot["status"] == "failed"
    assert not cli_config.get_env_path().exists()


def finish_optional(service, snapshot):
    for _ in range(10):
        if snapshot["status"] != "waiting":
            return snapshot
        assert snapshot["prompt"]["kind"] == "multi_select"
        snapshot = answer(service, snapshot, [])
    pytest.fail("Unexpected number of setup steps")


def test_setup_fresh_full_workflow_uses_existing_save_semantics(setup_service):
    service = setup_service
    assert service.status()["required"]
    snapshot = select_deepseek(service)
    snapshot = answer(service, snapshot, "test-secret-deepseek-123")
    # Named provider keys are saved before model selection in existing Setup.
    assert cli_config.get_env_value("DEEPSEEK_API_KEY") == "test-secret-deepseek-123"
    assert snapshot["saved"]
    model = snapshot["prompt"]["options"][0]["id"]
    snapshot = answer(service, snapshot, model)
    snapshot = finish_optional(service, snapshot)
    assert snapshot["status"] == "completed"
    assert snapshot["configuration"]["ready"]
    saved = cli_config.load_config(strict=True)
    assert saved["active_provider"] == "deepseek"
    assert saved["model"] == model
    assert saved["toolsets"] == ["mclaw-required"]
    assert "test-secret-deepseek-123" not in json.dumps(snapshot)


def test_setup_reuses_existing_credential_without_echoing_prefix(setup_service):
    cli_config.save_env_value("DEEPSEEK_API_KEY", "existing-secret-credential")
    cli_config.save_config({"active_provider": "deepseek", "model": "kept-model", "custom_field": {"keep": True}})
    snapshot = select_deepseek(setup_service)
    assert snapshot["prompt"]["secret_configured"]
    assert "existing-secret-credential" not in json.dumps(snapshot)
    assert "existing..." not in json.dumps(snapshot)
    snapshot = answer(setup_service, snapshot, "")
    assert snapshot["prompt"]["default"] == ["kept-model"]
    snapshot = answer(setup_service, snapshot, "kept-model")
    snapshot = finish_optional(setup_service, snapshot)
    assert snapshot["status"] == "completed"
    assert cli_config.get_env_value("DEEPSEEK_API_KEY") == "existing-secret-credential"
    assert cli_config.load_config()["custom_field"] == {"keep": True}


def test_setup_missing_key_skips_provider_without_creating_config(setup_service):
    snapshot = select_deepseek(setup_service)
    snapshot = answer(setup_service, snapshot, "")
    assert snapshot["status"] == "failed"
    assert not snapshot["saved"]
    assert not cli_config.get_config_path().exists()


def test_setup_cancel_before_submission_preserves_configuration(setup_service):
    cli_config.save_config({"model": "old", "custom": {"value": 12}})
    before = cli_config.get_config_path().read_bytes()
    snapshot = select_deepseek(setup_service)
    setup_service.cancel(snapshot["id"])
    snapshot = wait_for_form(setup_service, snapshot["id"])
    assert snapshot["status"] == "cancelled"
    assert cli_config.get_config_path().read_bytes() == before
    assert not cli_config.get_env_path().exists()


def test_setup_cancel_retains_previously_saved_provider_step(setup_service):
    snapshot = select_deepseek(setup_service)
    snapshot = answer(setup_service, snapshot, "saved-secret")
    snapshot = answer(setup_service, snapshot, snapshot["prompt"]["options"][0]["id"])
    setup_service.cancel(snapshot["id"])
    assert wait_for_form(setup_service, snapshot["id"])["status"] == "cancelled"
    assert cli_config.load_config()["active_provider"] == "deepseek"
    assert cli_config.get_env_value("DEEPSEEK_API_KEY") == "saved-secret"


def test_setup_invalid_existing_file_is_not_overwritten(setup_service):
    path = cli_config.get_config_path()
    path.write_text("invalid: [\n", encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(SetupServiceError, match="原文件已保留"):
        setup_service.start()
    assert path.read_bytes() == before


def test_setup_failure_does_not_return_exception_secret(setup_service, monkeypatch):
    def fail(_args):
        raise RuntimeError("Authorization: Bearer private-provider-token")

    monkeypatch.setattr(cli_main, "_run_setup_impl", fail)
    snapshot = setup_service.start()
    assert snapshot["status"] == "failed"
    assert snapshot["error"]["code"] == "SETUP_FAILED"
    assert "private-provider-token" not in json.dumps(snapshot)
    assert not snapshot["saved"]


def test_setup_reload_reuses_run_and_rejects_stale_or_invalid_input(setup_service):
    snapshot = setup_service.start()
    assert setup_service.start()["id"] == snapshot["id"]
    assert setup_service.status()["active_run_id"] == snapshot["id"]
    with pytest.raises(SetupServiceError) as stale:
        setup_service.submit(snapshot["id"], "stale-prompt", "y")
    assert stale.value.code == "SETUP_STALE_PROMPT"
    snapshot = answer(setup_service, snapshot, "y")
    with pytest.raises(SetupServiceError) as invalid:
        setup_service.submit(snapshot["id"], snapshot["prompt"]["id"], ["unknown-provider"])
    assert invalid.value.code == "SETUP_INVALID_VALUE"
    assert setup_service.get(snapshot["id"])["prompt"]["id"] == snapshot["prompt"]["id"]


def test_setup_serializes_shared_setup_globals(setup_service):
    setup_service.start()
    with pytest.raises(SetupServiceError) as caught:
        SetupService().start()
    assert caught.value.code == "SETUP_BUSY"


def test_setup_context_does_not_capture_other_thread_output(setup_service, capsys):
    from mclaw.cli.tui.console import print_plain

    snapshot = setup_service.start()
    print_plain("unrelated-runtime-output")
    assert "unrelated-runtime-output" in capsys.readouterr().out
    assert "unrelated-runtime-output" not in json.dumps(setup_service.get(snapshot["id"]))


def test_setup_reuses_qwen_key_and_grants_original_asr_scope(setup_service):
    from mclaw.runtime.secrets import is_authorized

    cli_config.save_env_value("DASHSCOPE_API_KEY", "qwen-shared-test-secret")
    snapshot = select_deepseek(setup_service)
    snapshot = answer(setup_service, snapshot, "deepseek-test-secret")
    snapshot = answer(setup_service, snapshot, snapshot["prompt"]["options"][0]["id"])
    snapshot = answer(setup_service, snapshot, [])  # tools
    snapshot = answer(setup_service, snapshot, [])  # channels
    assert "语音" in snapshot["prompt"]["title"]
    snapshot = answer(setup_service, snapshot, ["asr", "inbound_audio"])
    assert snapshot["prompt"]["kind"] == "multi_select"  # built-in skills, no key prompt
    snapshot = answer(setup_service, snapshot, [])
    assert snapshot["status"] == "completed"
    assert is_authorized("runtime:asr", "DASHSCOPE_API_KEY")
    saved = cli_config.load_config()
    assert saved["auxiliary"]["asr"]["enabled"] is True
    assert saved["capabilities"]["inbound_audio"]["enabled"] is True
    assert "qwen-shared-test-secret" not in json.dumps(snapshot)


def test_setup_custom_probe_failure_keeps_manual_fallback_and_commit_boundary(setup_service, monkeypatch):
    calls = []

    def probe(base_url, api_key):
        calls.append((base_url, api_key))
        return None

    monkeypatch.setattr(cli_main, "_probe_models", probe)
    snapshot = answer(setup_service, setup_service.start(), "y")
    snapshot = answer(setup_service, snapshot, ["__custom_openai__"])
    snapshot = answer(setup_service, snapshot, "http://127.0.0.1:9089/v1")
    snapshot = answer(setup_service, snapshot, "custom-test-secret")
    assert calls == [("http://127.0.0.1:9089/v1", "custom-test-secret")]
    assert not cli_config.get_config_path().exists()
    assert not cli_config.get_env_path().exists()
    assert "模型名称" in snapshot["prompt"]["title"]
    snapshot = answer(setup_service, snapshot, "local-model")
    snapshot = finish_optional(setup_service, snapshot)
    assert snapshot["status"] == "completed"
    assert cli_config.load_config()["active_provider"] == "custom"
    assert cli_config.get_env_value("MCLAW_API_KEY") == "custom-test-secret"
    assert any("连接失败" in line for line in snapshot["messages"])


def test_setup_weixin_qr_is_returned_as_local_image(setup_service):
    from mclaw.channels.weixin.qr_login import _show_qr
    from mclaw.cli.tui.console import print_plain

    from mclaw.application.setup import _SetupRun
    from mclaw.application.setup_interaction import current_setup_interaction

    run = _SetupRun()
    token = current_setup_interaction.set(run)
    try:
        _show_qr(qrcode_value="test-login-id", qrcode_url="https://example.invalid/login", print_fn=print_plain, render_qr=True)
        assert run.snapshot()["qr_code"].startswith("data:image/svg+xml;base64,")
        assert "test-login-id" not in json.dumps(run.snapshot()["messages"])
    finally:
        current_setup_interaction.reset(token)
