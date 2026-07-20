# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from copy import deepcopy

import pytest

from mclaw.cli.auth import resolve_provider
from mclaw.providers.base import ModelTraits, RuntimeProviderProfile
from mclaw.providers.generic import (
    GenericAnthropicCompatibleProfile,
    GenericOpenAICompatibleProfile,
)
from mclaw.providers.openrouter import OpenRouterProfile
from mclaw.providers.resolver import (
    ProviderResolutionError,
    resolve_provider_runtime_context,
    restore_provider_runtime_context,
)
from mclaw.providers.registry import PROVIDER_REGISTRY


@pytest.fixture(autouse=True)
def _clean_provider_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    names = {
        "MCLAW_BASE_URL", "MCLAW_API_KEY", "MCLAW_ANTHROPIC_BASE_URL",
        "MCLAW_ANTHROPIC_API_KEY", "OPENAI_BASE_URL",
    }
    for profile in PROVIDER_REGISTRY.values():
        names.update(profile.env_vars)
        if profile.base_url_env_var:
            names.add(profile.base_url_env_var)
    for name in names:
        monkeypatch.delenv(name, raising=False)


def _error(code: str, function, *args, **kwargs) -> ProviderResolutionError:
    with pytest.raises(ProviderResolutionError) as caught:
        function(*args, **kwargs)
    assert caught.value.code == code
    return caught.value


def _snapshot(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": 1,
        "provider": "zhipu",
        "model": "glm-4.7",
        "safe_base_url": "https://open.bigmodel.cn/api/paas/v4",
        "base_url_source": "profile",
        "api_mode": "chat_completions",
        "auth_source": "GLM_API_KEY",
        "api_key_fingerprint": "sha256:" + "0" * 64,
        "reasoning_config": None,
    }
    value.update(overrides)
    return value


def test_explicit_builtin_is_terminal_when_credential_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "qwen-key")
    error = _error(
        "missing_credential",
        resolve_provider_runtime_context,
        provider="openai",
        model="gpt-5.4",
        config={"fallback_providers": [{"provider": "qwen", "model": "qwen3-plus"}]},
    )
    assert error.provider == "openai"
    assert error.key_env_var == "OPENAI_API_KEY"
    legacy = resolve_provider(
        provider="openai",
        model="gpt-5.4",
        config={"fallback_providers": [{"provider": "qwen", "model": "qwen3-plus"}]},
    )
    assert legacy["provider"] == "openai"
    assert legacy["api_key"] == ""


def test_explicit_user_provider_is_terminal_and_reserved_names_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "official-key")
    monkeypatch.delenv("TEAM_KEY", raising=False)
    config = {
        "providers": {
            "team": {
                "api_key_env": "TEAM_KEY",
                "base_url": "https://team.example/v1",
                "model": "team-model",
            }
        }
    }
    error = _error(
        "missing_credential",
        resolve_provider_runtime_context,
        provider="team",
        model="team-model",
        config=config,
    )
    assert error.provider == "team"
    for reserved in ("openai", "Z.AI", "custom", "custom_anthropic"):
        invalid_config = {"providers": {reserved: {"base_url": "https://example.com/v1"}}}
        _error(
            "invalid_provider_config",
            resolve_provider_runtime_context,
            provider="openai",
            model="gpt-5.4",
            api_key="key",
            config=invalid_config,
        )
        _error(
            "invalid_provider_config",
            resolve_provider,
            provider="openai",
            model="gpt-5.4",
            api_key="key",
            config=invalid_config,
        )


def test_unknown_provider_does_not_become_custom_when_base_url_is_present() -> None:
    _error(
        "unknown_provider",
        resolve_provider_runtime_context,
        provider="typo-provider",
        model="model",
        base_url="https://example.com/v1",
        api_key="key",
    )


@pytest.mark.parametrize(
    "value",
    [
        "ftp://example.com/v1",
        "https:///v1",
        "https://user:pass@example.com/v1",
        "https://example.com/v1?token=secret",
        "https://example.com/v1#fragment",
        "https://example.com?",
        "https://example.com#",
        "https://example.com:bad/v1",
        "https://bad host.example/v1",
        "https://bad%20host.example/v1",
        "https://example.com\\evil/v1",
        "https://example.com/a b",
        "https://example.com/v1\nnext",
    ],
)
def test_base_url_contract_rejects_unsafe_shapes(value: str) -> None:
    _error(
        "invalid_base_url",
        resolve_provider_runtime_context,
        provider="custom",
        model="local",
        base_url=value,
        api_key="key",
    )


def test_invalid_port_value_is_not_reflected_in_error_message() -> None:
    error = _error(
        "invalid_base_url",
        resolve_provider_runtime_context,
        provider="custom",
        model="model",
        base_url="https://example.com:port-secret/v1",
        api_key="key",
    )
    assert "port-secret" not in error.message


def test_custom_local_endpoint_can_be_keyless_but_remote_cannot() -> None:
    local = resolve_provider_runtime_context(
        provider="custom",
        model="local-model",
        base_url="http://127.0.0.1:11434/v1/",
    )
    assert local.api_key == ""
    assert local.safe_base_url == "http://127.0.0.1:11434/v1"
    _error(
        "missing_credential",
        resolve_provider_runtime_context,
        provider="custom",
        model="remote-model",
        base_url="https://remote.example/v1",
    )


def test_openai_base_url_has_different_semantics_for_explicit_and_implicit_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "https://gateway.example/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "key")
    explicit = resolve_provider_runtime_context(provider="openai", model="gpt-5.4")
    implicit = resolve_provider_runtime_context(model="unknown-hosted-model")
    assert explicit.provider == "openai"
    assert implicit.provider == "custom"
    assert explicit.base_url_source == implicit.base_url_source == "provider_env"


def test_explicit_custom_base_url_outranks_ambient_anthropic_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MCLAW_ANTHROPIC_BASE_URL", "https://anthropic-gateway.example.test")
    monkeypatch.setenv("MCLAW_ANTHROPIC_API_KEY", "ambient-anthropic-key")

    context = resolve_provider_runtime_context(
        model="gateway-model",
        base_url="https://openai-gateway.example.test/v1",
        api_key="explicit-openai-key",
    )

    assert context.provider == "custom"
    assert context.api_mode == "chat_completions"
    assert context.base_url == "https://openai-gateway.example.test/v1"
    assert context.auth_source == "explicit"


def test_active_config_selector_is_resolved_before_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    context = resolve_provider_runtime_context(
        config={"active_provider": "openai", "model": "configured-model"}
    )
    assert context.provider == "openai"
    assert context.model == "configured-model"
    explicit_model = resolve_provider_runtime_context(
        model="opaque-model",
        config={"active_provider": "openai", "model": "configured-model"},
    )
    assert explicit_model.provider == "openai"
    assert explicit_model.model == "opaque-model"


def test_dynamic_profiles_are_isolated_and_api_mode_is_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEAM_KEY", "key")
    config = {
        "providers": {
            "team": {
                "api_key_env": "TEAM_KEY",
                "base_url": "https://team.example/root",
                "api_mode": "anthropic_messages",
                "model": "team-model",
            }
        }
    }
    first = resolve_provider_runtime_context(provider="team", model="team-model", config=config)
    second = resolve_provider_runtime_context(provider="team", model="team-model", config=config)
    assert first.profile is not second.profile
    assert first.api_mode == "anthropic_messages"
    assert isinstance(first.profile, GenericAnthropicCompatibleProfile)
    assert first.profile.auth_scheme == "anthropic_x_api_key"
    assert first.profile.models_url == "https://team.example/root/v1/models"
    bad = deepcopy(config)
    bad["providers"]["team"]["api_mode"] = "responses"
    _error(
        "invalid_api_mode",
        resolve_provider_runtime_context,
        provider="team",
        model="team-model",
        config=bad,
    )
    _error(
        "invalid_provider_config",
        resolve_provider_runtime_context,
        provider="team",
        model="team-model",
        api_key="explicit-key",
        config={"providers": {"team": {"base_url": "https://team.example/v1"}}},
    )


def test_dynamic_provider_names_and_display_names_cannot_be_ambiguous() -> None:
    _error(
        "invalid_provider_config",
        resolve_provider_runtime_context,
        provider="TEAM",
        model="model",
        config={
            "providers": {
                "Team": {"api_key_env": "A_KEY", "base_url": "https://a.example/v1"},
                "team": {"api_key_env": "B_KEY", "base_url": "https://b.example/v1"},
            }
        },
    )
    _error(
        "invalid_provider_config",
        resolve_provider_runtime_context,
        provider="Google-AI-Studio",
        model="model",
        config={
            "providers": {
                "team": {
                    "display_name": "Google AI Studio",
                    "api_key_env": "TEAM_KEY",
                    "base_url": "https://team.example/v1",
                }
            }
        },
    )
    _error(
        "invalid_provider_config",
        resolve_provider_runtime_context,
        provider="alpha",
        model="model",
        config={
            "providers": {
                "alpha": {
                    "display_name": "Shared Name",
                    "api_key_env": "A_KEY",
                    "base_url": "https://a.example/v1",
                },
                "beta": {
                    "display_name": "shared-name",
                    "api_key_env": "B_KEY",
                    "base_url": "https://b.example/v1",
                },
            }
        },
    )


def test_required_endpoint_and_regional_setup_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "azure-key")
    _error(
        "invalid_base_url",
        resolve_provider_runtime_context,
        provider="microsoft",
        model="deployment",
    )
    monkeypatch.setenv("DASHSCOPE_INTL_API_KEY", "intl-key")
    context = resolve_provider_runtime_context(
        provider="qwen",
        setup_profile_id="api",
        model="qwen3.7-plus",
    )
    assert context.provider == "qwen-intl"
    assert context.base_url == "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
    assert context.auth_source == "DASHSCOPE_INTL_API_KEY"
    legacy = resolve_provider(
        provider="qwen",
        model="qwen3.7-plus",
        config={"active_provider": "qwen", "active_provider_profile": "api"},
    )
    assert legacy["provider"] == "qwen-intl"
    assert legacy["base_url"] == "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
    _error(
        "invalid_provider_config",
        resolve_provider_runtime_context,
        provider="qwen",
        setup_profile_id="coding-plan",
        model="qwen3.7-plus",
    )


@pytest.mark.parametrize(
    ("root", "setup_id", "target", "model", "key_env", "base_url"),
    [
        ("qwen", "api", "qwen-intl", "qwen3.7-plus", "DASHSCOPE_INTL_API_KEY", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"),
        ("qwen", "api-cn", "qwen", "qwen3.7-plus", "DASHSCOPE_API_KEY", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
        ("moonshot", "api", "moonshot-intl", "kimi-k2.6", "KIMI_INTL_API_KEY", "https://api.moonshot.ai/v1"),
        ("moonshot", "api-cn", "moonshot", "kimi-k2.6", "KIMI_API_KEY", "https://api.moonshot.cn/v1"),
        ("minimax", "api", "minimax", "MiniMax-M2.7", "MINIMAX_API_KEY", "https://api.minimax.io/v1"),
        ("minimax", "api-cn", "minimax-cn", "MiniMax-M2.7", "MINIMAX_CN_API_KEY", "https://api.minimaxi.com/v1"),
    ],
)
def test_all_regional_setup_targets_bind_target_credentials_and_endpoints(
    monkeypatch: pytest.MonkeyPatch,
    root: str,
    setup_id: str,
    target: str,
    model: str,
    key_env: str,
    base_url: str,
) -> None:
    monkeypatch.setenv(key_env, "region-key")
    context = resolve_provider_runtime_context(
        provider=root,
        setup_profile_id=setup_id,
        model=model,
    )
    assert context.provider == target
    assert context.auth_source == key_env
    assert context.base_url == base_url


def test_prefix_detection_accepts_one_direct_match_and_rejects_regional_ambiguity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "key")
    context = resolve_provider_runtime_context(model="deepseek-chat")
    assert context.provider == "deepseek"
    _error("provider_required", resolve_provider_runtime_context, model="qwen3.7-plus")
    _error("provider_required", resolve_provider_runtime_context, model="unknown-model")


def test_explicit_host_and_router_keep_their_own_behavior_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPINFRA_API_KEY", "host-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "router-key")
    hosted = resolve_provider_runtime_context(
        provider="deepinfra",
        model="deepseek-ai/DeepSeek-V3",
    )
    routed = resolve_provider_runtime_context(
        provider="openrouter",
        model="deepseek/deepseek-chat",
    )
    assert isinstance(hosted.profile, GenericOpenAICompatibleProfile)
    assert hosted.profile.provider_kind == "host"
    assert isinstance(routed.profile, OpenRouterProfile)
    assert routed.profile.provider_kind == "router"


def test_fallback_order_skips_missing_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-key")
    context = resolve_provider_runtime_context(
        config={
            "fallback_providers": [
                {"provider": "openai", "model": "gpt-5.4"},
                {"provider": "deepseek", "model": "deepseek-chat"},
            ]
        }
    )
    assert context.provider == "deepseek"
    assert context.model == "deepseek-chat"
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "azure-key")
    still_deepseek = resolve_provider_runtime_context(
        config={
            "fallback_providers": [
                {"provider": "microsoft", "model": "deployment"},
                {"provider": "deepseek", "model": "deepseek-chat"},
            ]
        }
    )
    assert still_deepseek.provider == "deepseek"


class _ReasoningProfile(RuntimeProviderProfile):
    def model_traits(self, model: str) -> ModelTraits:
        return ModelTraits(reasoning_modes=("low", "medium"))


def test_reasoning_config_is_strict_and_model_trait_scoped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _ReasoningProfile(
        name="reasoning-test",
        display_name="Reasoning Test",
        env_vars=("REASONING_KEY",),
        base_url="https://reasoning.example/v1",
    )
    monkeypatch.setitem(PROVIDER_REGISTRY, "reasoning-test", profile)
    monkeypatch.setenv("REASONING_KEY", "key")
    context = resolve_provider_runtime_context(
        provider="reasoning-test",
        model="reasoner",
        config={"reasoning": {"effort": "medium"}},
    )
    assert dict(context.reasoning_config or {}) == {"enabled": True, "effort": "medium"}
    explicit = resolve_provider_runtime_context(
        provider="reasoning-test",
        model="reasoner",
        reasoning_config={"enabled": True, "effort": " MEDIUM "},
    )
    assert dict(explicit.reasoning_config or {}) == {"enabled": True, "effort": "medium"}
    disabled = resolve_provider_runtime_context(
        provider="reasoning-test",
        model="reasoner",
        reasoning_config={"enabled": False, "effort": "   "},
    )
    assert dict(disabled.reasoning_config or {}) == {"enabled": False}
    _error(
        "invalid_reasoning_config",
        resolve_provider_runtime_context,
        provider="reasoning-test",
        model="reasoner",
        reasoning_config={"enabled": True, "effort": "high"},
    )
    _error(
        "invalid_reasoning_config",
        resolve_provider_runtime_context,
        provider="reasoning-test",
        model="reasoner",
        reasoning_config={"enabled": False, "effort": "low"},
    )
    _error(
        "invalid_reasoning_config",
        resolve_provider_runtime_context,
        provider="reasoning-test",
        model="reasoner",
        config={"reasoning": "high"},
    )


@pytest.mark.parametrize(
    ("provider", "model", "env_name", "effort"),
    [
        ("openai", "gpt-5.6", "OPENAI_API_KEY", "max"),
        ("openai", "gpt-5", "OPENAI_API_KEY", "minimal"),
        ("anthropic", "claude-sonnet-5", "ANTHROPIC_API_KEY", "max"),
        ("anthropic", "claude-mythos-preview", "ANTHROPIC_API_KEY", "max"),
        ("minimax", "MiniMax-M3", "MINIMAX_API_KEY", "xhigh"),
        ("openrouter", "anthropic/claude-sonnet-5", "OPENROUTER_API_KEY", "max"),
        ("deepseek", "deepseek-v4-pro", "DEEPSEEK_API_KEY", "low"),
        ("moonshot", "kimi-k2.6", "KIMI_API_KEY", "high"),
        ("moonshot", "kimi-k3", "KIMI_API_KEY", "low"),
        ("zhipu", "glm-5.2", "GLM_API_KEY", "max"),
        ("google", "gemini-3.5-flash", "GEMINI_API_KEY", "minimal"),
        ("google", "gemini-3.1-pro", "GEMINI_API_KEY", "minimal"),
        ("google", "gemini-3.1-pro-preview-06-05", "GEMINI_API_KEY", "minimal"),
        ("qwen", "qwen3.7-plus", "DASHSCOPE_API_KEY", "xhigh"),
        ("xiaomi", "mimo-v2.5-pro", "MIMO_API_KEY", "high"),
        ("xai", "grok-4.5", "XAI_API_KEY", "high"),
    ],
)
def test_builtin_family_reasoning_modes_resolve_exactly(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    model: str,
    env_name: str,
    effort: str,
) -> None:
    monkeypatch.setenv(env_name, "key")

    context = resolve_provider_runtime_context(
        provider=provider,
        model=model,
        reasoning_config={"enabled": True, "effort": effort},
    )

    assert dict(context.reasoning_config or {}) == {"enabled": True, "effort": effort}


@pytest.mark.parametrize(
    ("provider", "model", "env_name", "effort"),
    [
        ("openai", "gpt-5.4", "OPENAI_API_KEY", "max"),
        ("openai", "gpt-5.1", "OPENAI_API_KEY", "xhigh"),
        ("openai", "gpt-5-pro", "OPENAI_API_KEY", "low"),
        ("openai", "gpt-5.1-chat-latest", "OPENAI_API_KEY", "high"),
        ("anthropic", "claude-sonnet-4-6", "ANTHROPIC_API_KEY", "xhigh"),
        ("anthropic", "claude-mythos-preview", "ANTHROPIC_API_KEY", "xhigh"),
        ("minimax", "MiniMax-M2.7", "MINIMAX_API_KEY", "high"),
        ("openrouter", "openrouter/auto", "OPENROUTER_API_KEY", "high"),
        ("moonshot", "kimi-k2.6", "KIMI_API_KEY", "max"),
        ("zhipu", "glm-5.1", "GLM_API_KEY", "low"),
        ("deepseek", "deepseek-v4-pro", "DEEPSEEK_API_KEY", "minimal"),
        ("qwen", "qwen2.5-72b", "DASHSCOPE_API_KEY", "high"),
        ("xiaomi", "mimo-v2.5", "MIMO_API_KEY", "xhigh"),
        ("xai", "grok-4.5", "XAI_API_KEY", "xhigh"),
    ],
)
def test_builtin_family_reasoning_modes_reject_unsupported_effort(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    model: str,
    env_name: str,
    effort: str,
) -> None:
    monkeypatch.setenv(env_name, "key")

    _error(
        "invalid_reasoning_config",
        resolve_provider_runtime_context,
        provider=provider,
        model=model,
        reasoning_config={"enabled": True, "effort": effort},
    )


def test_snapshot_schema_and_auth_source_are_strict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GLM_API_KEY", "key")
    with_secret = _snapshot(api_key="secret")
    _error("invalid_snapshot", restore_provider_runtime_context, with_secret)
    _error(
        "invalid_snapshot",
        restore_provider_runtime_context,
        _snapshot(api_key_fingerprint="sha256:short"),
    )
    _error(
        "invalid_snapshot",
        restore_provider_runtime_context,
        _snapshot(api_mode="responses"),
    )
    _error(
        "invalid_snapshot",
        restore_provider_runtime_context,
        _snapshot(safe_base_url="https://example.com?"),
    )
    _error(
        "invalid_snapshot",
        restore_provider_runtime_context,
        _snapshot(provider=" "),
    )
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-read")
    _error(
        "invalid_snapshot",
        restore_provider_runtime_context,
        _snapshot(auth_source="UNRELATED_SECRET"),
    )
    overlaid = restore_provider_runtime_context(
        _snapshot(auth_source="UNRELATED_SECRET"),
        api_key="explicit-key",
    )
    assert overlaid.api_key == "explicit-key"
    assert overlaid.auth_source == "explicit"


def test_snapshot_profile_and_provider_env_endpoint_hydration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GLM_API_KEY", "key")
    monkeypatch.setenv("GLM_BASE_URL", "https://old.example/v1")
    environment_context = resolve_provider_runtime_context(provider="zhipu", model="glm-4.7")
    monkeypatch.setenv("GLM_BASE_URL", "https://new.example/v2")
    restored_environment = restore_provider_runtime_context(environment_context.snapshot())
    assert restored_environment.base_url == "https://new.example/v2"
    assert restored_environment.base_url_source == "provider_env"

    monkeypatch.delenv("GLM_BASE_URL")
    continuity = restore_provider_runtime_context(environment_context.snapshot())
    assert continuity.base_url == "https://old.example/v1"
    assert continuity.base_url_source == "provider_env"

    profile_snapshot = _snapshot(base_url_source="profile")
    restored_profile = restore_provider_runtime_context(profile_snapshot)
    assert restored_profile.base_url == "https://open.bigmodel.cn/api/paas/v4"
    assert restored_profile.base_url_source == "profile"


def test_dynamic_provider_env_restore_ignores_unselected_config_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEAM_KEY", "key")
    monkeypatch.setenv("TEAM_URL", "https://old.team.example/v1")
    config = {
        "providers": {
            "team": {
                "api_key_env": "TEAM_KEY",
                "base_url_env": "TEAM_URL",
                "base_url": "https://fallback.team.example/v1",
                "model": "team-model",
            }
        }
    }
    snapshot = resolve_provider_runtime_context(
        provider="team",
        model="team-model",
        config=config,
    ).snapshot()
    assert snapshot["base_url_source"] == "provider_env"

    current = deepcopy(config)
    current["providers"]["team"]["base_url"] = "not-a-url"
    monkeypatch.setenv("TEAM_URL", "https://new.team.example/v2")
    restored = restore_provider_runtime_context(snapshot, config=current)

    assert restored.base_url == "https://new.team.example/v2"
    assert restored.base_url_source == "provider_env"


def test_implicit_openai_base_url_restores_from_its_current_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MCLAW_BASE_URL", raising=False)
    monkeypatch.delenv("MCLAW_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://old.gateway.example/v1")
    snapshot = resolve_provider_runtime_context(model="opaque-hosted-model").snapshot()
    assert snapshot["provider"] == "custom"
    assert snapshot["base_url_source"] == "provider_env"

    monkeypatch.setenv("OPENAI_BASE_URL", "https://new.gateway.example/v2")
    restored = restore_provider_runtime_context(snapshot)

    assert restored.base_url == "https://new.gateway.example/v2"
    assert restored.base_url_source == "provider_env"


def test_snapshot_prefers_recorded_allowlisted_credential_then_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GLM_API_KEY", "glm-key")
    monkeypatch.setenv("ZHIPU_API_KEY", "zhipu-key")
    snapshot = _snapshot(auth_source="ZHIPU_API_KEY")
    preferred = restore_provider_runtime_context(snapshot)
    assert preferred.api_key == "zhipu-key"
    assert preferred.auth_source == "ZHIPU_API_KEY"
    monkeypatch.delenv("ZHIPU_API_KEY")
    fallback = restore_provider_runtime_context(snapshot)
    assert fallback.api_key == "glm-key"
    assert fallback.auth_source == "GLM_API_KEY"


def test_snapshot_rejects_endpoint_source_not_supported_by_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key")
    _error(
        "invalid_snapshot",
        restore_provider_runtime_context,
        _snapshot(
            provider="anthropic",
            model="claude-sonnet-4-6",
            safe_base_url="https://api.anthropic.com",
            base_url_source="provider_env",
            api_mode="anthropic_messages",
            auth_source="ANTHROPIC_API_KEY",
        ),
    )


def test_snapshot_provider_config_uses_current_config_with_continuity_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEAM_KEY", "key")
    old_config = {
        "providers": {
            "team": {
                "api_key_env": "TEAM_KEY",
                "base_url": "https://old.team/v1",
                "model": "team-model",
            }
        }
    }
    context = resolve_provider_runtime_context(provider="team", model="team-model", config=old_config)
    new_config = deepcopy(old_config)
    new_config["providers"]["team"]["base_url"] = "https://new.team/v2"
    restored = restore_provider_runtime_context(context.snapshot(), config=new_config)
    assert restored.base_url == "https://new.team/v2"
    assert restored.base_url_source == "provider_config"

    broken_config = deepcopy(old_config)
    broken_config["providers"]["team"]["base_url"] = "not-a-url"
    overlaid = restore_provider_runtime_context(
        context.snapshot(),
        config=broken_config,
        base_url="https://fixed.team/v3",
        api_key="explicit-key",
    )
    assert overlaid.base_url == "https://fixed.team/v3"
    assert overlaid.base_url_source == "explicit"


def test_restore_overlay_matrix_drops_old_provider_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GLM_API_KEY", "glm-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-key")
    snapshot = resolve_provider_runtime_context(provider="zhipu", model="glm-4.7").snapshot()
    _error(
        "missing_model",
        restore_provider_runtime_context,
        snapshot,
        provider="deepseek",
    )
    switched = restore_provider_runtime_context(
        snapshot,
        provider="deepseek",
        model="deepseek-chat",
    )
    assert switched.provider == "deepseek"
    assert switched.api_key == "deepseek-key"
    assert switched.base_url == "https://api.deepseek.com/v1"
    assert switched.auth_source == "DEEPSEEK_API_KEY"
    retained = restore_provider_runtime_context(snapshot, provider="glm")
    assert retained.provider == "zhipu"
    assert retained.model == "glm-4.7"


def test_regional_restore_switch_requires_model_and_uses_new_region(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "cn-key")
    monkeypatch.setenv("DASHSCOPE_INTL_API_KEY", "intl-key")
    snapshot = resolve_provider_runtime_context(
        provider="qwen",
        model="qwen3.7-plus",
    ).snapshot()
    _error(
        "missing_model",
        restore_provider_runtime_context,
        snapshot,
        provider="qwen-intl",
    )
    restored = restore_provider_runtime_context(
        snapshot,
        provider="qwen-intl",
        model="qwen3.7-plus",
    )
    assert restored.provider == "qwen-intl"
    assert restored.api_key == "intl-key"
    assert restored.base_url == "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"


def test_empty_snapshot_bootstrap_and_snapshot_only_dynamic_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "key")
    bootstrapped = restore_provider_runtime_context(
        {},
        provider="deepseek",
        model="deepseek-chat",
    )
    assert bootstrapped.provider == "deepseek"

    dynamic = _snapshot(
        provider="retired-provider",
        model="retired-model",
        safe_base_url="https://retired.example/v1",
        base_url_source="snapshot",
        auth_source="explicit",
    )
    _error("missing_credential", restore_provider_runtime_context, dynamic)
    restored = restore_provider_runtime_context(dynamic, api_key="current-key")
    assert restored.provider == "retired-provider"
    assert restored.api_key == "current-key"
    assert restored.base_url == "https://retired.example/v1"


def test_restore_uses_current_named_dynamic_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEAM_KEY", "key")
    old_config = {
        "providers": {
            "team": {
                "api_key_env": "TEAM_KEY",
                "base_url": "https://old.team/v1",
                "api_mode": "chat_completions",
                "model": "team-model",
            }
        }
    }
    snapshot = resolve_provider_runtime_context(
        provider="team",
        model="team-model",
        config=old_config,
    ).snapshot()
    new_config = deepcopy(old_config)
    new_config["providers"]["team"].update({
        "base_url": "https://new.team/v2",
        "api_mode": "anthropic_messages",
    })
    restored = restore_provider_runtime_context(snapshot, config=new_config)
    assert isinstance(restored.profile, GenericAnthropicCompatibleProfile)
    assert restored.api_mode == "anthropic_messages"
    assert restored.base_url == "https://new.team/v2"


def test_local_custom_snapshot_cannot_be_overlaid_to_remote_without_a_key() -> None:
    local = resolve_provider_runtime_context(
        provider="custom",
        model="local-model",
        base_url="http://localhost:11434/v1",
    )
    _error(
        "missing_credential",
        restore_provider_runtime_context,
        local.snapshot(),
        base_url="https://remote.example/v1",
    )
