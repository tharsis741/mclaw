# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from dataclasses import FrozenInstanceError

import pytest

from mclaw.agent.prompt_cache import PromptCachePlan
from mclaw.agent.token_budget import TokenBudgetEstimate
from mclaw.agent.transports.base import ModelCallOptions, ReasoningTrace
from mclaw.agent.usage import UsageRecord
from mclaw.providers.base import RuntimeProviderProfile
from mclaw.providers.registry import PROVIDER_REGISTRY
from mclaw.providers.runtime import ProviderRuntimeContext
from mclaw.state import SessionDB


def _context(**overrides: object) -> ProviderRuntimeContext:
    values = {
        "profile": RuntimeProviderProfile(
            name="zhipu",
            display_name="Zhipu GLM",
            api_mode="chat_completions",
            auth_scheme="bearer",
        ),
        "model": "glm-4.7",
        "api_key": "super-secret",
        "base_url": "https://user:password@OPEN.BIGMODEL.CN/api/paas/v4/?token=secret#fragment",
        "base_url_source": "explicit",
        "auth_source": "GLM_API_KEY",
        "reasoning_config": {"enabled": True, "effort": "medium"},
    }
    values.update(overrides)
    return ProviderRuntimeContext(**values)


def test_runtime_context_is_frozen_and_copies_reasoning_config() -> None:
    source = {"enabled": True, "effort": "medium"}
    context = _context(reasoning_config=source)
    source["effort"] = "high"

    assert dict(context.reasoning_config or {}) == {"enabled": True, "effort": "medium"}
    with pytest.raises(TypeError):
        context.reasoning_config["effort"] = "low"  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        context.model = "other"  # type: ignore[misc]


def test_snapshot_is_an_exact_secret_free_allowlist() -> None:
    context = _context()
    snapshot = context.snapshot()

    assert context.safe_base_url == "https://open.bigmodel.cn/api/paas/v4"
    assert set(snapshot) == {
        "schema_version",
        "provider",
        "model",
        "safe_base_url",
        "base_url_source",
        "api_mode",
        "auth_source",
        "api_key_fingerprint",
        "reasoning_config",
    }
    assert snapshot["api_key_fingerprint"].startswith("sha256:")
    assert snapshot["reasoning_config"] == {"enabled": True, "effort": "medium"}
    assert "super-secret" not in repr(snapshot)
    assert "password" not in repr(snapshot)
    assert "token=secret" not in repr(snapshot)


def test_fingerprint_tracks_effective_behavior_not_restore_provenance() -> None:
    context = _context(base_url_source="profile", auth_source="FIRST_KEY")

    assert context.fingerprint() == _context(
        base_url_source="snapshot", auth_source="SECOND_KEY"
    ).fingerprint()
    assert context.fingerprint() != _context(model="glm-4.8").fingerprint()
    assert context.fingerprint() != _context(api_key="another-key").fingerprint()
    assert context.fingerprint() != _context(reasoning_config={"enabled": False}).fingerprint()
    assert context.fingerprint() != _context(base_url="https://example.com/v1").fingerprint()


def test_reasoning_trace_roundtrip_and_budget_representation() -> None:
    trace = ReasoningTrace(
        text="working",
        provider="zhipu",
        model="glm-4.7",
        api_mode="chat_completions",
        format="reasoning_details",
        payload=[{"type": "reasoning", "text": "working"}],
    )
    fields = trace.to_message_fields()

    assert ReasoningTrace.from_message(fields) == trace
    assert trace.to_budget_text() == '[{"text":"working","type":"reasoning"}]'
    assert ReasoningTrace(text="once", format="reasoning_content").to_budget_text() == "once"
    assert ReasoningTrace(
        text="summary",
        format="gemini_thought_signature",
        payload={"signature": "abc"},
    ).to_budget_text() == 'summary\n{"signature":"abc"}'


def test_reasoning_trace_accepts_legacy_details_but_rejects_unknown_envelope() -> None:
    legacy = ReasoningTrace.from_message(
        {"reasoning": "legacy", "reasoning_details": [{"text": "legacy"}]}
    )
    unsupported = ReasoningTrace.from_message(
        {
            "reasoning": "visible",
            "reasoning_details": {"schema_version": 2, "provider": "unknown"},
        }
    )

    assert legacy == ReasoningTrace(
        text="legacy", format="reasoning_details", payload=[{"text": "legacy"}]
    )
    assert unsupported == ReasoningTrace(text="visible", format="reasoning_content")


def test_usage_record_only_exports_provider_reported_fields() -> None:
    usage = UsageRecord(
        provider="zhipu",
        model="glm-4.7",
        input_tokens=10,
        output_tokens=0,
        cache_read_tokens=3,
    )

    assert usage.available_fields == (
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
    )
    assert usage.to_counter_delta() == {
        "input_tokens": 10,
        "output_tokens": 0,
        "cache_read_tokens": 3,
    }
    assert usage.to_compressor_update() == {
        "prompt_tokens": 10,
        "completion_tokens": 0,
    }


def test_phase_one_value_contracts_are_frozen() -> None:
    values = (
        RuntimeProviderProfile(name="test", display_name="Test"),
        ModelCallOptions(),
        TokenBudgetEstimate(1, 2, 3, 4),
        PromptCachePlan(),
    )

    for value in values:
        with pytest.raises(FrozenInstanceError):
            value.source = "changed"  # type: ignore[attr-defined]


def test_session_db_updates_model_and_snapshot_atomically(tmp_path) -> None:
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("session-1", "cli", model="old-model")
        snapshot = {"schema_version": 1, "provider": "qwen", "model": "千问-max"}

        db.update_model_config("session-1", model="千问-max", model_config=snapshot)

        row = db.get_session("session-1")
        assert row is not None
        assert row["model"] == "千问-max"
        assert db.get_model_config("session-1") == snapshot
        assert "千问-max" in row["model_config"]
        assert "\\u5343" not in row["model_config"]
    finally:
        db.close()


@pytest.mark.parametrize("stored", [None, "", "   "])
def test_session_db_empty_model_config_is_an_empty_snapshot(tmp_path, stored) -> None:
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("session-1", "cli")
        db._conn.execute(
            "UPDATE sessions SET model_config = ? WHERE id = ?",
            (stored, "session-1"),
        )
        db._conn.commit()

        assert db.get_model_config("session-1") == {}
    finally:
        db.close()


@pytest.mark.parametrize("stored", ["not-json", "[]", '"string"', "42"])
def test_session_db_rejects_corrupt_or_non_object_model_config(tmp_path, stored) -> None:
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("session-1", "cli")
        db._conn.execute(
            "UPDATE sessions SET model_config = ? WHERE id = ?",
            (stored, "session-1"),
        )
        db._conn.commit()

        with pytest.raises(ValueError, match="session-1"):
            db.get_model_config("session-1")
    finally:
        db.close()


@pytest.mark.parametrize(
    ("selector", "profile_id", "runtime_provider", "credential_env"),
    [
        ("qwen", "api", "qwen-intl", "DASHSCOPE_INTL_API_KEY"),
        ("qwen", "api-cn", "qwen", "DASHSCOPE_API_KEY"),
        ("moonshot", "api", "moonshot-intl", "KIMI_INTL_API_KEY"),
        ("moonshot", "api-cn", "moonshot", "KIMI_API_KEY"),
        ("minimax", "api", "minimax", "MINIMAX_API_KEY"),
        ("minimax", "api-cn", "minimax-cn", "MINIMAX_CN_API_KEY"),
    ],
)
def test_setup_persists_canonical_runtime_provider_without_selector(
    monkeypatch,
    selector,
    profile_id,
    runtime_provider,
    credential_env,
) -> None:
    from mclaw.cli import main
    from mclaw.cli import config as cli_config

    saved_env: list[tuple[str, str]] = []
    prompts: list[str] = []
    output: list[str] = []
    monkeypatch.setattr(cli_config, "get_env_value", lambda _name: "")
    monkeypatch.setattr(cli_config, "save_env_value", lambda name, value: saved_env.append((name, value)))
    monkeypatch.setattr(cli_config, "save_config", lambda _config: None)
    monkeypatch.setattr(main, "_setup_secret", lambda prompt: prompts.append(prompt) or "test-secret")
    monkeypatch.setattr(main, "_select_setup_model", lambda *_args, **_kwargs: "test-model")
    monkeypatch.setattr(main, "print_plain", lambda text="", **_kwargs: output.append(str(text)))

    config: dict[str, object] = {"active_provider_profile": "stale-selector"}
    result = main._setup_api_key_provider(
        config,
        selector,
        profile_id=profile_id,
    )

    runtime_profile = PROVIDER_REGISTRY[runtime_provider]
    assert result is not None
    assert result["provider"] == runtime_provider
    assert result["profile"] == ""
    assert config["active_provider"] == runtime_provider
    assert config["active_provider_profile"] == ""
    assert saved_env == [(credential_env, "test-secret")]
    assert prompts == [f"  {runtime_profile.display_name} API 密钥: "]
    assert any(runtime_profile.display_name in line for line in output)
    assert any(runtime_profile.key_url in line for line in output)
