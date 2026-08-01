# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import inspect
import subprocess
import sys
from dataclasses import FrozenInstanceError

import pytest

from mclaw.agent import context_metadata, models_dev
from mclaw.agent.models_dev import resolve_models_dev_provider
from mclaw.cli import auth, provider_profiles
from mclaw.cli.config import DEFAULT_CONFIG
from mclaw.providers.base import RuntimeProviderProfile
from mclaw.providers.anthropic import AnthropicProfile
from mclaw.providers.deepseek import DeepSeekProfile
from mclaw.providers.gemini import GoogleGeminiProfile
from mclaw.providers.generic import (
    GenericAnthropicCompatibleProfile,
    GenericOpenAICompatibleProfile,
)
from mclaw.providers.kimi import MoonshotKimiProfile
from mclaw.providers.minimax import MiniMaxProfile
from mclaw.providers.normalization import normalize_provider_key, reserved_provider_collision
from mclaw.providers.openai import OpenAIProfile
from mclaw.providers.openrouter import OpenRouterProfile
from mclaw.providers.qwen import QwenProfile
from mclaw.providers.registry import PROVIDER_REGISTRY
from mclaw.providers.runtime import ProviderRuntimeContext
from mclaw.providers.xai import XAIProfile
from mclaw.providers.xiaomi import XiaomiMiMoProfile
from mclaw.providers.zhipu import ZhipuGLMProfile


BEHAVIOR_PROVIDERS = {
    "openai", "anthropic", "openrouter", "deepseek", "moonshot",
    "moonshot-intl", "minimax", "minimax-cn", "zhipu", "google",
    "qwen", "qwen-intl", "xiaomi", "xai",
}
GENERIC_PROVIDERS = {
    "baidu", "tencent", "groq", "fireworks", "deepinfra", "mistral",
    "microsoft", "cohere", "amazon", "together", "perplexity", "meta",
    "yi", "stepfun", "baichuan", "doubao", "siliconflow",
}
GENERIC_BASE_URLS = {
    "baidu": "https://qianfan.baidubce.com/v2",
    "tencent": "https://api.hunyuan.cloud.tencent.com/v1",
    "groq": "https://api.groq.com/openai/v1",
    "fireworks": "https://api.fireworks.ai/inference/v1",
    "deepinfra": "https://api.deepinfra.com/v1/openai",
    "mistral": "https://api.mistral.ai/v1",
    "microsoft": "",
    "cohere": "https://api.cohere.ai/compatibility/v1",
    "amazon": "https://bedrock-mantle.us-east-1.api.aws/v1",
    "together": "https://api.together.xyz/v1",
    "perplexity": "https://api.perplexity.ai",
    "meta": "https://llama-api.meta.com/compat/v1",
    "yi": "https://api.lingyiwanwu.com/v1",
    "stepfun": "https://api.stepfun.com/v1",
    "baichuan": "https://api.baichuan-ai.com/v1",
    "doubao": "https://ark.cn-beijing.volces.com/api/v3",
    "siliconflow": "https://api.siliconflow.cn/v1",
}
GENERIC_OFFICIAL_METADATA = {
    "baidu": (("QIANFAN_API_KEY", "BAIDU_API_KEY"), "QIANFAN_BASE_URL", "direct", "qianfan"),
    "tencent": (("HUNYUAN_API_KEY", "TENCENT_HUNYUAN_API_KEY"), "HUNYUAN_BASE_URL", "direct", "hunyuan"),
    "groq": (("GROQ_API_KEY",), "GROQ_BASE_URL", "host", "groq"),
    "fireworks": (("FIREWORKS_API_KEY",), "FIREWORKS_BASE_URL", "host", "fireworks-ai"),
    "deepinfra": (("DEEPINFRA_API_KEY",), "DEEPINFRA_BASE_URL", "host", "deepinfra"),
    "mistral": (("MISTRAL_API_KEY",), "MISTRAL_BASE_URL", "direct", "mistral"),
    "microsoft": (("AZURE_OPENAI_API_KEY", "AZURE_AI_API_KEY", "MICROSOFT_AI_API_KEY"), "AZURE_OPENAI_BASE_URL", "host", "azure"),
    "cohere": (("COHERE_API_KEY",), "COHERE_BASE_URL", "direct", "cohere"),
    "amazon": (("AWS_BEARER_TOKEN_BEDROCK", "BEDROCK_API_KEY"), "BEDROCK_BASE_URL", "host", "amazon-bedrock"),
    "together": (("TOGETHER_API_KEY",), "TOGETHER_BASE_URL", "host", "togetherai"),
    "perplexity": (("PERPLEXITY_API_KEY",), "PERPLEXITY_BASE_URL", "direct", "perplexity"),
    "meta": (("LLAMA_API_KEY", "META_API_KEY"), "LLAMA_BASE_URL", "direct", "llama"),
    "yi": (("YI_API_KEY",), "YI_BASE_URL", "direct", "yi"),
    "stepfun": (("STEPFUN_API_KEY", "STEP_API_KEY"), "STEPFUN_BASE_URL", "direct", "stepfun"),
    "baichuan": (("BAICHUAN_API_KEY",), "BAICHUAN_BASE_URL", "direct", "baichuan"),
    "doubao": (("DOUBAO_API_KEY", "ARK_API_KEY"), "DOUBAO_BASE_URL", "direct", "bytedance"),
    "siliconflow": (("SILICONFLOW_API_KEY",), "SILICONFLOW_BASE_URL", "host", "siliconflow"),
}
BEHAVIOR_PROFILE_TYPES = {
    "openai": OpenAIProfile,
    "anthropic": AnthropicProfile,
    "openrouter": OpenRouterProfile,
    "deepseek": DeepSeekProfile,
    "moonshot": MoonshotKimiProfile,
    "moonshot-intl": MoonshotKimiProfile,
    "minimax": MiniMaxProfile,
    "minimax-cn": MiniMaxProfile,
    "zhipu": ZhipuGLMProfile,
    "google": GoogleGeminiProfile,
    "qwen": QwenProfile,
    "qwen-intl": QwenProfile,
    "xiaomi": XiaomiMiMoProfile,
    "xai": XAIProfile,
}


def test_registry_has_exact_canonical_provider_sets_and_profile_bindings() -> None:
    assert set(PROVIDER_REGISTRY) == BEHAVIOR_PROVIDERS | GENERIC_PROVIDERS
    assert len(BEHAVIOR_PROVIDERS) == 14
    assert len(GENERIC_PROVIDERS) == 17
    assert all(
        isinstance(PROVIDER_REGISTRY[name], GenericOpenAICompatibleProfile)
        for name in GENERIC_PROVIDERS
    )
    assert all(
        not isinstance(PROVIDER_REGISTRY[name], GenericOpenAICompatibleProfile)
        for name in BEHAVIOR_PROVIDERS
    )
    assert all(profile.name == name for name, profile in PROVIDER_REGISTRY.items())
    assert {
        name: type(PROVIDER_REGISTRY[name]) for name in BEHAVIOR_PROVIDERS
    } == BEHAVIOR_PROFILE_TYPES


def test_registry_classification_and_protocol_metadata() -> None:
    assert {name for name, profile in PROVIDER_REGISTRY.items() if profile.provider_kind == "router"} == {
        "openrouter"
    }
    assert {name for name, profile in PROVIDER_REGISTRY.items() if profile.provider_kind == "host"} == {
        "amazon", "deepinfra", "fireworks", "groq", "microsoft", "siliconflow", "together"
    }
    assert {name for name, profile in PROVIDER_REGISTRY.items() if profile.provider_kind == "direct"} == (
        set(PROVIDER_REGISTRY) - {
            "openrouter", "amazon", "deepinfra", "fireworks", "groq",
            "microsoft", "siliconflow", "together",
        }
    )
    assert PROVIDER_REGISTRY["anthropic"].api_mode == "anthropic_messages"
    assert PROVIDER_REGISTRY["anthropic"].auth_scheme == "anthropic_x_api_key"
    assert all(
        profile.api_mode == "chat_completions" and profile.auth_scheme == "bearer"
        for name, profile in PROVIDER_REGISTRY.items()
        if name != "anthropic"
    )


def test_generic_official_metadata_fixture() -> None:
    assert {
        name: PROVIDER_REGISTRY[name].base_url for name in GENERIC_PROVIDERS
    } == GENERIC_BASE_URLS
    assert PROVIDER_REGISTRY["microsoft"].base_url_required is True
    assert PROVIDER_REGISTRY["microsoft"].base_url_env_var == "AZURE_OPENAI_BASE_URL"
    assert PROVIDER_REGISTRY["amazon"].env_vars == (
        "AWS_BEARER_TOKEN_BEDROCK", "BEDROCK_API_KEY"
    )
    assert PROVIDER_REGISTRY["amazon"].base_url_env_var == "BEDROCK_BASE_URL"
    assert all(PROVIDER_REGISTRY[name].credential_required for name in GENERIC_PROVIDERS)
    assert set(GENERIC_OFFICIAL_METADATA) == GENERIC_PROVIDERS
    for name, (env_vars, base_env, provider_kind, models_dev_provider) in GENERIC_OFFICIAL_METADATA.items():
        profile = PROVIDER_REGISTRY[name]
        base_url = GENERIC_BASE_URLS[name]
        assert profile.env_vars == env_vars
        assert profile.base_url_env_var == base_env
        assert profile.provider_kind == provider_kind
        assert profile.models_dev_provider == models_dev_provider
        assert profile.models_url == (f"{base_url}/models" if base_url else "")


def test_registry_profiles_are_frozen_and_fallbacks_cover_required_families() -> None:
    with pytest.raises(FrozenInstanceError):
        PROVIDER_REGISTRY["openai"].base_url = "https://example.com"  # type: ignore[misc]
    assert "mimo-v2.5-pro" in PROVIDER_REGISTRY["xiaomi"].fallback_models
    assert PROVIDER_REGISTRY["xiaomi"].fallback_models == (
        "mimo-v2.5-pro",
        "mimo-v2.5",
    )
    assert PROVIDER_REGISTRY["moonshot"].fallback_models[0] == "kimi-k3"
    assert "kimi-k2.6" in PROVIDER_REGISTRY["moonshot"].fallback_models
    assert "kimi-k2.7-code" in PROVIDER_REGISTRY["moonshot"].fallback_models
    assert PROVIDER_REGISTRY["deepseek"].fallback_models[:2] == (
        "deepseek-v4-pro",
        "deepseek-v4-flash",
    )
    assert PROVIDER_REGISTRY["anthropic"].fallback_models[:2] == (
        "claude-sonnet-5",
        "claude-opus-4-8",
    )
    assert PROVIDER_REGISTRY["minimax"].fallback_models[0] == "MiniMax-M3"
    assert "MiniMax-M2.7" in PROVIDER_REGISTRY["minimax"].fallback_models
    assert PROVIDER_REGISTRY["zhipu"].fallback_models[:2] == ("glm-5.2", "glm-5.1")
    assert "qwen3.7-plus" in PROVIDER_REGISTRY["qwen"].fallback_models
    assert PROVIDER_REGISTRY["xai"].fallback_models[0] == "grok-4.5"
    assert context_metadata.DEFAULT_CONTEXT_LENGTHS["claude-sonnet-5"] == 1_000_000
    assert context_metadata.DEFAULT_CONTEXT_LENGTHS["claude-mythos-5"] == 1_000_000
    assert context_metadata.DEFAULT_CONTEXT_LENGTHS["claude-mythos-preview"] == 1_000_000
    assert context_metadata.DEFAULT_CONTEXT_LENGTHS["gpt-5.6"] == 1_050_000
    assert context_metadata.DEFAULT_CONTEXT_LENGTHS["gpt-5.4"] == 1_050_000
    assert context_metadata.DEFAULT_CONTEXT_LENGTHS["o3"] == 200_000
    assert context_metadata.DEFAULT_CONTEXT_LENGTHS["minimax-m3"] == 1_000_000
    assert context_metadata.DEFAULT_CONTEXT_LENGTHS["deepseek-v4-pro"] == 1_000_000
    assert context_metadata.DEFAULT_CONTEXT_LENGTHS["qwen3.7"] == 1_000_000
    assert context_metadata.DEFAULT_CONTEXT_LENGTHS["mimo-v2.5"] == 1_048_576
    assert context_metadata.DEFAULT_CONTEXT_LENGTHS["glm-5.1"] == 200_000


def test_alias_normalization_and_reserved_dynamic_names() -> None:
    assert normalize_provider_key("GLM") == "zhipu"
    assert normalize_provider_key("z.ai") == "zhipu"
    assert normalize_provider_key("Google-AI-Studio") == "google"
    assert normalize_provider_key("CUSTOM", {"Custom": {}}) == "Custom"
    assert normalize_provider_key("unregistered") == "unregistered"
    assert reserved_provider_collision("openai") == "openai"
    assert reserved_provider_collision("Z.AI") == "zhipu"
    assert reserved_provider_collision("Google-AI-Studio") == "google"
    assert reserved_provider_collision("CUSTOM") == "custom"
    assert reserved_provider_collision("custom_anthropic") == "custom_anthropic"
    assert reserved_provider_collision("my-provider") == ""

    owners: dict[str, str] = {}
    for name, profile in PROVIDER_REGISTRY.items():
        for value in (name, *profile.aliases):
            key = value.casefold()
            assert key not in owners or owners[key] == name
            owners[key] = name


def test_cli_auth_is_a_registry_derived_view() -> None:
    assert auth.PROVIDER_REGISTRY is auth.PROVIDER_CONFIGS
    assert set(auth.PROVIDER_CONFIGS) == set(PROVIDER_REGISTRY)
    for name, profile in PROVIDER_REGISTRY.items():
        view = auth.PROVIDER_CONFIGS[name]
        assert view.name == profile.name
        assert view.display_name == profile.display_name
        assert view.api_key_env_vars == list(profile.env_vars)
        assert view.base_url == profile.base_url
        assert view.base_url_required == profile.base_url_required
        assert view.base_url_env_var == profile.base_url_env_var
        assert view.api_mode == profile.api_mode
        assert view.key_url == profile.key_url
        assert view.model_prefixes == list(profile.model_prefixes)
        assert view.aliases == list(profile.aliases)
        assert auth.DEFAULT_PROVIDER_MODELS.get(name, []) == list(profile.fallback_models)
    assert "https://" not in inspect.getsource(auth)


def test_setup_and_catalog_views_bind_callable_runtime_profiles() -> None:
    assert provider_profiles.CORE_PROVIDER_KEYS == [
        "qwen", "deepseek", "moonshot", "minimax", "zhipu", "doubao",
        "baidu", "tencent", "baichuan", "xiaomi", "openai", "anthropic",
        "google", "xai", "meta", "mistral", "groq", "together",
        "fireworks", "deepinfra", "openrouter",
    ]
    for name, profile in PROVIDER_REGISTRY.items():
        views = provider_profiles.PROVIDER_PROFILES[name]
        assert len(views) == len(profile.setup_profiles)
        for source, view in zip(profile.setup_profiles, views, strict=True):
            assert view.id == source.id
            assert view.label == source.label
            assert view.runtime_provider == source.runtime_provider
            assert view.kind == source.kind
            assert view.callable == source.callable
            assert view.credential_scope == source.credential_scope
            assert view.note == source.note
            expected_catalog = (
                PROVIDER_REGISTRY[source.runtime_provider].models_dev_provider
                if source.callable
                else source.models_dev_provider
            )
            assert view.models_dev_provider == expected_catalog

    assert provider_profiles.get_provider_profile("qwen", "api").runtime_provider == "qwen-intl"
    assert provider_profiles.get_provider_profile("qwen", "api-cn").runtime_provider == "qwen"
    assert provider_profiles.get_provider_profile("moonshot", "api").runtime_provider == "moonshot-intl"
    assert provider_profiles.get_provider_profile("minimax", "api-cn").runtime_provider == "minimax-cn"


def test_models_dev_mapping_reads_runtime_registry_directly() -> None:
    assert resolve_models_dev_provider("qwen") == "alibaba-cn"
    assert resolve_models_dev_provider("qwen", "api") == "alibaba"
    assert resolve_models_dev_provider("qwen", "api-cn") == "alibaba-cn"
    assert resolve_models_dev_provider("moonshot") == "moonshotai-cn"
    assert resolve_models_dev_provider("unknown") == "unknown"


@pytest.mark.parametrize(
    ("config", "expected_profile", "starred_profile"),
    [
        ({}, "api", ""),
        (
            {
                "active_provider": "qwen-intl",
                "model": "qwen-current",
                "fallback_providers": [{"provider": "qwen", "model": "qwen-cn"}],
            },
            "api",
            "api",
        ),
        ({"active_provider": "qwen", "model": "qwen-cn"}, "api-cn", "api-cn"),
        (
            {
                "active_provider": "openai",
                "model": "gpt-current",
                "fallback_providers": [{"provider": "qwen", "model": "qwen-cn"}],
            },
            "api-cn",
            "api-cn",
        ),
    ],
)
def test_setup_profile_stars_only_previous_selection(
    monkeypatch: pytest.MonkeyPatch,
    config: dict,
    expected_profile: str,
    starred_profile: str,
) -> None:
    from mclaw.cli import colors
    from mclaw.cli import main as cli_main

    output: list[str] = []
    prompts: list[str] = []
    monkeypatch.setattr(colors, "color", lambda text, *_styles: str(text))
    monkeypatch.setattr(cli_main, "print_plain", lambda line="": output.append(str(line)))
    monkeypatch.setattr(
        cli_main,
        "_setup_input",
        lambda prompt, **_kwargs: prompts.append(prompt) or "",
    )

    selected = cli_main._select_setup_profile("qwen", config)

    assert selected.id == expected_profile
    starred_lines = [line for line in output if " *" in line]
    if starred_profile:
        assert len(starred_lines) == 1
        assert f"({starred_profile})" in starred_lines[0]
    else:
        assert starred_lines == []
    assert prompts == [f"  输入接口编号或名称 [{expected_profile}]: "]


def test_models_dev_lists_recently_released_models_first(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = {
        "test-provider": {
            "models": {
                "older": {
                    "id": "older",
                    "release_date": "2025-01-01",
                    "last_updated": "2026-08-01",
                },
                "undated": {"id": "undated"},
                "newest": {"id": "newest", "release_date": "2026-07-30"},
            }
        }
    }
    monkeypatch.setattr(models_dev, "fetch_models_dev", lambda: registry)

    assert models_dev.list_models_dev_provider("test-provider", limit=2) == ["newest", "older"]
    assert models_dev.list_models_dev_provider("test-provider", limit=None) == [
        "newest",
        "older",
        "undated",
    ]


@pytest.mark.parametrize(
    ("config", "profile_id", "expected"),
    [
        ({}, "api-cn", "qwen3.7-plus"),
        ({"active_provider": "qwen", "model": "older"}, "api-cn", "older"),
        ({"active_provider": "qwen-intl", "model": "older"}, "api", "older"),
    ],
)
def test_setup_model_pins_current_or_recommended_first(
    monkeypatch: pytest.MonkeyPatch,
    config: dict,
    profile_id: str,
    expected: str,
) -> None:
    from mclaw.cli import main as cli_main
    from mclaw.cli.tui import selection_prompt

    candidates = ["newest", "qwen3.7-plus", "older", *(f"model-{index}" for index in range(22))]
    menu: dict[str, object] = {}

    def list_models(*_args, **kwargs):
        assert kwargs["limit"] is None
        return candidates

    def choose(title, items, **kwargs):
        menu.update(title=title, items=items, kwargs=kwargs)
        return items[0]["id"]

    monkeypatch.setattr(models_dev, "list_provider_models", list_models)
    monkeypatch.setattr(selection_prompt, "prompt_single_select", choose)

    assert cli_main._select_setup_model("qwen", config, profile_id) == expected
    assert menu["title"] == "M-Claw 模型选择"
    assert menu["items"][0]["id"] == expected
    assert {item["id"] for item in menu["items"][:-1]} == set(candidates)
    assert menu["items"][-1]["label"] == "其他模型名称"
    assert menu["kwargs"]["default_selected"] == expected
    assert menu["kwargs"]["max_visible_items"] == 20


def test_setup_multi_select_uses_checkmark_for_selected_item() -> None:
    from mclaw.cli.tui.selection_prompt import _multi_select_item_fragments

    fragments = _multi_select_item_fragments(
        {"label": "Item", "category": "", "description": ""},
        checked=True,
        current=False,
    )

    assert "[✓]" in "".join(text for _style, text in fragments)


def test_setup_single_select_scrolls_and_waits_for_enter() -> None:
    from prompt_toolkit.application.current import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from mclaw.cli.tui.selection_prompt import prompt_single_select

    items = [{"id": f"model-{index}", "label": f"model-{index}"} for index in range(30)]
    with create_pipe_input() as pipe:
        pipe.send_text("\x1b[B" * 21 + "\t\x1b[B\t\r")
        with create_app_session(input=pipe, output=DummyOutput()):
            assert prompt_single_select("Models", items, max_visible_items=20) == "model-22"


def test_provider_import_boundary_is_cli_and_sdk_independent() -> None:
    code = """
import sys
import mclaw.providers.registry
import mclaw.providers.normalization
import mclaw.providers.resolver
import mclaw.providers.runtime
forbidden_prefixes = ('mclaw.cli', 'mclaw.agent.core', 'openai', 'anthropic')
loaded = {
    name for name in sys.modules
    if any(name == prefix or name.startswith(prefix + '.') for prefix in forbidden_prefixes)
}
assert not loaded, sorted(loaded)
"""
    subprocess.run([sys.executable, "-c", code], check=True)


def test_registry_public_values_are_provider_layer_types_only() -> None:
    assert all(isinstance(value, RuntimeProviderProfile) for value in PROVIDER_REGISTRY.values())
    assert DEFAULT_CONFIG["reasoning"] == {"effort": ""}
    assert not hasattr(context_metadata, "get_model_context_length")


def test_shared_provider_probe_uses_profile_url_and_protocol_auth(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, str]]] = []

    class Response:
        status_code = 200

        @staticmethod
        def json() -> dict[str, object]:
            return {"data": [{"id": "model-a"}, {"missing": "id"}]}

    def fake_get(url: str, *, timeout: int, headers: dict[str, str]) -> Response:
        assert timeout == 10
        calls.append((url, headers))
        return Response()

    monkeypatch.setattr(context_metadata.requests, "get", fake_get)
    openai_profile = GenericOpenAICompatibleProfile(
        name="custom",
        display_name="Custom",
        models_url="https://catalog.example.test/v1/models",
    )
    anthropic_profile = PROVIDER_REGISTRY["anthropic"]

    assert context_metadata.probe_provider_models(
        openai_profile,
        base_url="https://ignored.example.test/v1",
        api_key="openai-key",
    ) == ["model-a"]
    assert context_metadata.probe_provider_models(
        anthropic_profile,
        base_url="https://anthropic.example.test/root/",
        api_key="anthropic-key",
    ) == ["model-a"]
    assert calls == [
        (
            "https://catalog.example.test/v1/models",
            {"Authorization": "Bearer openai-key"},
        ),
        (
            "https://anthropic.example.test/root/v1/models",
            {"anthropic-version": "2023-06-01", "x-api-key": "anthropic-key"},
        ),
    ]


def test_context_length_uses_runtime_safe_url_and_profile_metadata(monkeypatch) -> None:
    profile = GenericOpenAICompatibleProfile(
        name="runtime-host",
        display_name="Runtime Host",
        provider_kind="host",
        models_url="https://catalog.example.test/models",
        models_dev_provider="runtime-catalog",
    )
    context = ProviderRuntimeContext(
        profile=profile,
        model="namespace/model-a",
        api_key="secret",
        base_url="https://RUNTIME.example.test/v1/?ignored=secret",
    )
    cache_reads: list[tuple[str, str]] = []
    cache_writes: list[tuple[str, str, int]] = []

    monkeypatch.setattr(
        context_metadata,
        "get_cached_context_length",
        lambda model, url: cache_reads.append((model, url)),
    )
    monkeypatch.setattr(
        context_metadata,
        "save_context_length",
        lambda model, url, length: cache_writes.append((model, url, length)),
    )
    monkeypatch.setattr(
        context_metadata,
        "_probe_provider_model_entries",
        lambda *_args, **_kwargs: [{"id": "namespace/model-a", "context_window": 262_144}],
    )

    assert context_metadata.resolve_context_length(context) == 262_144
    assert cache_reads == [("namespace/model-a", "https://runtime.example.test/v1")]
    assert cache_writes == [
        ("namespace/model-a", "https://runtime.example.test/v1", 262_144)
    ]


def test_anthropic_context_length_prefers_input_window_over_output_limit(monkeypatch) -> None:
    context = ProviderRuntimeContext(
        profile=PROVIDER_REGISTRY["anthropic"],
        model="claude-sonnet-test",
        api_key="secret",
        base_url="https://api.anthropic.com",
    )
    monkeypatch.setattr(context_metadata, "get_cached_context_length", lambda *_args: None)
    monkeypatch.setattr(context_metadata, "save_context_length", lambda *_args: None)
    monkeypatch.setattr(
        context_metadata,
        "_probe_provider_model_entries",
        lambda *_args, **_kwargs: [{
            "id": "claude-sonnet-test",
            "max_input_tokens": 200_000,
            "max_tokens": 16_384,
        }],
    )

    assert context_metadata.resolve_context_length(context) == 200_000


def test_anthropic_context_length_never_treats_output_limit_as_input_window(monkeypatch) -> None:
    profile = GenericAnthropicCompatibleProfile(
        name="custom-anthropic",
        display_name="Custom Anthropic",
        api_mode="anthropic_messages",
        auth_scheme="anthropic_x_api_key",
    )
    context = ProviderRuntimeContext(
        profile=profile,
        model="opaque-model",
        api_key="secret",
        base_url="https://anthropic.example.test",
    )
    monkeypatch.setattr(context_metadata, "get_cached_context_length", lambda *_args: None)
    monkeypatch.setattr(context_metadata, "save_context_length", lambda *_args: None)
    monkeypatch.setattr(
        context_metadata,
        "_probe_provider_model_entries",
        lambda *_args, **_kwargs: [{"id": "opaque-model", "max_tokens": 16_384}],
    )

    assert (
        context_metadata.resolve_context_length(context)
        == context_metadata.DEFAULT_FALLBACK_CONTEXT
    )


def test_context_length_cache_version_invalidates_pre_fix_values(monkeypatch) -> None:
    old_key = "https://api.anthropic.com:claude-sonnet-test"
    cache = {old_key: {"context_length": 16_384}}
    saved: dict[str, object] = {}
    monkeypatch.setattr(context_metadata, "_load_length_cache", lambda: cache)
    monkeypatch.setattr(
        context_metadata,
        "_save_length_cache",
        lambda value: saved.update(value),
    )

    assert context_metadata.get_cached_context_length(
        "claude-sonnet-test",
        "https://api.anthropic.com",
    ) is None
    context_metadata.save_context_length(
        "claude-sonnet-test",
        "https://api.anthropic.com",
        200_000,
    )
    assert saved[f"v2:{old_key}"]["context_length"] == 200_000


def test_context_length_models_dev_lookup_uses_profile_mapping(monkeypatch) -> None:
    from mclaw.agent import models_dev

    profile = RuntimeProviderProfile(
        name="dynamic",
        display_name="Dynamic",
        models_dev_provider="catalog-provider",
    )
    context = ProviderRuntimeContext(profile, "model-a", "", "")
    lookups: list[tuple[str, str]] = []

    monkeypatch.setattr(context_metadata, "get_cached_context_length", lambda *_args: None)
    monkeypatch.setattr(context_metadata, "save_context_length", lambda *_args: None)
    monkeypatch.setattr(
        context_metadata,
        "_probe_provider_model_entries",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        models_dev,
        "lookup_models_dev_context",
        lambda provider, model: lookups.append((provider, model)) or 65_536,
    )

    assert context_metadata.resolve_context_length(context) == 65_536
    assert lookups == [("catalog-provider", "model-a")]


def test_cli_custom_setup_probes_delegate_to_shared_profile_helper(monkeypatch) -> None:
    from mclaw.cli import main

    profiles: list[RuntimeProviderProfile] = []

    def fake_probe(
        profile: RuntimeProviderProfile,
        *,
        base_url: str,
        api_key: str,
    ) -> list[str]:
        assert base_url == "https://custom.example.test/root"
        assert api_key == "key"
        profiles.append(profile)
        return ["model-a"]

    monkeypatch.setattr(context_metadata, "probe_provider_models", fake_probe)

    assert main._probe_models("https://custom.example.test/root/", "key") == ["model-a"]
    assert main._probe_anthropic("https://custom.example.test/root/", "key") is True
    assert isinstance(profiles[0], GenericOpenAICompatibleProfile)
    assert profiles[0].auth_scheme == "bearer"
    assert profiles[0].models_url == "https://custom.example.test/root/models"
    assert isinstance(profiles[1], GenericAnthropicCompatibleProfile)
    assert profiles[1].auth_scheme == "anthropic_x_api_key"
    assert profiles[1].models_url == "https://custom.example.test/root/v1/models"


def test_qwen_tool_and_feature_metadata_are_registry_derived() -> None:
    from mclaw.cli import config as cli_config
    from mclaw.cli import search_backend_switch
    from mclaw.runtime import features
    from mclaw.tools.search import credentials as search_credentials
    from mclaw.tools.search import dashscope_backend
    from mclaw.tools.vision import config as vision_config

    qwen = PROVIDER_REGISTRY["qwen"]
    qwen_intl = PROVIDER_REGISTRY["qwen-intl"]
    assert cli_config.DEFAULT_CONFIG["auxiliary"]["asr"]["provider"] == qwen.name
    assert "region" not in cli_config.DEFAULT_CONFIG["auxiliary"]["asr"]
    assert vision_config.DEFAULT_PROVIDER == qwen.name
    assert vision_config.DASHSCOPE_BASE_URL == qwen.base_url
    assert vision_config.QWEN_BASE_URL_ENV_VAR == qwen.base_url_env_var
    assert vision_config.QWEN_CREDENTIAL_ENV_VARS == qwen.env_vars
    assert search_credentials.DASHSCOPE_BASE_URL == qwen.base_url
    assert search_credentials.QWEN_CREDENTIAL_ENV_VARS == qwen.env_vars
    assert search_credentials.QWEN_CREDENTIAL_HINT == " or ".join(
        dict.fromkeys((*qwen.env_vars, *qwen_intl.env_vars))
    )
    assert search_backend_switch._QWEN_PROFILE is qwen
    assert search_backend_switch._QWEN_CREDENTIAL_HINT == " or ".join(
        dict.fromkeys((*qwen.env_vars, *qwen_intl.env_vars))
    )
    assert tuple(search_credentials.QWEN_CREDENTIAL_HINT.split(" or ")) == tuple(
        dict.fromkeys((*qwen.env_vars, *qwen_intl.env_vars))
    )
    assert dashscope_backend.DASHSCOPE_BASE_URL == qwen.base_url
    qwen_groups = (qwen.env_vars, qwen_intl.env_vars)
    assert features.FEATURES["web_search"].requires_any == (("TAVILY_API_KEY",), *qwen_groups)
    assert features.FEATURES["vision_analyze"].requires_any == qwen_groups
    assert features.FEATURES["asr"].requires_any == qwen_groups


def test_qwen_tool_consumers_select_regional_registry_metadata(monkeypatch) -> None:
    from mclaw.tools.search import credentials as search_credentials
    from mclaw.tools.vision import credentials as vision_credentials
    from mclaw.voice import config as voice_config

    qwen_intl = PROVIDER_REGISTRY["qwen-intl"]
    key_values = {"DASHSCOPE_INTL_API_KEY": "intl-secret"}
    monkeypatch.setattr(
        vision_credentials,
        "authorized_env_value",
        lambda name, default="": key_values.get(name, default),
    )
    monkeypatch.setattr(
        vision_credentials,
        "env_value",
        lambda _name, default="": default,
    )
    vision = vision_credentials.resolve_vision_credentials(
        config={"auxiliary": {"vision": {"provider": "qwen-intl"}}}
    )
    assert (vision.provider, vision.env_var, vision.api_key, vision.base_url) == (
        "qwen-intl",
        "DASHSCOPE_INTL_API_KEY",
        "intl-secret",
        qwen_intl.base_url,
    )

    monkeypatch.setattr(
        search_credentials,
        "authorized_env_value",
        lambda name, default="": key_values.get(name, default),
    )
    search = search_credentials.resolve_dashscope_creds(
        config={"auxiliary": {"web_search": {"base_url": qwen_intl.base_url}}}
    )
    assert search["api_key"] == "intl-secret"
    assert search["base_url"] == qwen_intl.base_url

    monkeypatch.setattr(
        search_credentials,
        "env_value",
        lambda name, default="": key_values.get(name, default),
    )
    search_from_intl_key = search_credentials.resolve_dashscope_creds(
        config={"auxiliary": {"web_search": {}}}
    )
    assert search_from_intl_key["api_key"] == "intl-secret"
    assert search_from_intl_key["base_url"] == qwen_intl.base_url

    monkeypatch.setattr(
        search_credentials,
        "env_value",
        lambda name, default="": qwen_intl.base_url
        if name == qwen_intl.base_url_env_var
        else default,
    )
    search_from_env = search_credentials.resolve_dashscope_creds(
        config={"auxiliary": {"web_search": {}}}
    )
    assert search_from_env["api_key"] == "intl-secret"
    assert search_from_env["base_url"] == qwen_intl.base_url

    monkeypatch.setattr(
        voice_config,
        "_authorized_env_value",
        lambda name: key_values.get(name, ""),
    )
    asr = voice_config.resolve_asr_config(
        config={"auxiliary": {"asr": {"enabled": False, "region": "intl"}}}
    )
    assert asr["provider"] == "qwen-intl"
    assert asr["credential_provider"] == "qwen-intl"
    assert asr["key_source"] == "DASHSCOPE_INTL_API_KEY"
    assert asr["api_key"] == "intl-secret"
    assert asr["websocket_url"] == voice_config.INTL_REALTIME_URL

    asr_by_provider = voice_config.resolve_asr_config(
        config={"auxiliary": {"asr": {"enabled": False, "provider": "qwen-intl"}}}
    )
    assert asr_by_provider["provider"] == "qwen-intl"
    assert asr_by_provider["websocket_url"] == voice_config.INTL_REALTIME_URL

    asr_by_explicit_region = voice_config.resolve_asr_config(
        config={
            "auxiliary": {
                "asr": {
                    "enabled": False,
                    "provider": "qwen-intl",
                    "region": "cn",
                }
            }
        }
    )
    assert asr_by_explicit_region["provider"] == "qwen"
    assert asr_by_explicit_region["websocket_url"] == voice_config.CN_REALTIME_URL


def test_doctor_reports_registry_metadata_and_secret_free_active_provider(monkeypatch) -> None:
    from mclaw import doctor

    monkeypatch.setenv("OPENAI_API_KEY", "doctor-secret")
    results: list[doctor.CheckResult] = []
    doctor._append_provider_checks(
        results,
        {"active_provider": "openai", "model": "gpt-test"},
    )

    assert results[0].name == "provider registry"
    assert results[0].ok is True
    assert "canonical=31" in results[0].detail
    assert results[1].name == "agent provider"
    assert results[1].ok is True
    assert "provider=openai" in results[1].detail
    assert "credential_candidates=OPENAI_API_KEY" in results[1].detail
    assert "doctor-secret" not in f"{results[1].detail} {results[1].fix}"


def test_doctor_maps_typed_provider_failure_without_exposing_secrets(monkeypatch) -> None:
    from mclaw import doctor

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    results: list[doctor.CheckResult] = []
    doctor._append_provider_checks(
        results,
        {"active_provider": "openai", "model": "gpt-test"},
    )

    failure = results[-1]
    assert failure.name == "agent provider"
    assert failure.ok is False
    assert "code=missing_credential" in failure.detail
    assert "credential_candidates=OPENAI_API_KEY" in failure.detail
    assert "sk-" not in f"{failure.detail} {failure.fix}"


def test_setup_selects_multiple_providers_before_configuring_each_model(monkeypatch) -> None:
    from mclaw.cli import config as cli_config
    from mclaw.cli import main as cli_main
    from mclaw.cli.tui import console, selection_prompt

    config = {
        "active_provider": "qwen-intl",
        "model": "qwen-existing",
        "fallback_providers": [
            {"provider": "openai", "model": "openai-existing"},
        ],
    }
    provider_menu: dict[str, object] = {}
    provider_calls: list[str] = []
    input_prompts: list[str] = []

    def choose(title, items, **kwargs):
        provider_menu.update(title=title, items=items, kwargs=kwargs)
        return ["qwen", "openai"]

    def configure(_config, provider_key, *_args, **_kwargs):
        provider_calls.append(provider_key)
        return {
            "provider": provider_key,
            "profile": "",
            "model": f"{provider_key}-model",
        }

    def setup_input(prompt, **_kwargs):
        input_prompts.append(prompt)
        return "2"

    monkeypatch.setattr(cli_config, "ensure_mclaw_home", lambda: None)
    monkeypatch.setattr(cli_config, "load_config", lambda: config)
    monkeypatch.setattr(cli_config, "save_config", lambda _config: None)
    monkeypatch.setattr(console, "print_plain", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(selection_prompt, "prompt_multi_select", choose)
    monkeypatch.setattr(cli_main, "_print_setup_intro", lambda: False)
    monkeypatch.setattr(cli_main, "_print_setup_step", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cli_main,
        "_select_setup_profile",
        lambda _provider, _config: type("Profile", (), {"id": "api"})(),
    )
    monkeypatch.setattr(cli_main, "_setup_api_key_provider", configure)
    monkeypatch.setattr(cli_main, "_setup_input", setup_input)
    monkeypatch.setattr(cli_main, "_run_setup_capability_selection", lambda _config: None)
    monkeypatch.setattr(cli_main, "_run_setup_builtin_skill_selection", lambda: None)

    cli_main._run_setup_impl(object())

    item_ids = [item["id"] for item in provider_menu["items"]]
    assert provider_menu["title"] == "M-Claw 大模型接入配置"
    assert provider_menu["kwargs"]["default_selected"] == ["qwen", "openai"]
    assert "microsoft" in item_ids
    assert "qwen-intl" not in item_ids
    assert item_ids[-3:] == [
        "__other_provider__",
        "__custom_openai__",
        "__custom_anthropic__",
    ]
    assert provider_calls == ["qwen", "openai"]
    assert input_prompts == ["  输入编号 [1]: "]
    assert config["active_provider"] == "openai"
    assert config["model"] == "openai-model"
