# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Provider profile metadata for setup and model catalog UX.

M-Claw's callable provider key is intentionally not the same thing as a
models.dev provider id.  One vendor can expose official API models, coding
plan models, token plan models, and cloud-hosted variants.  Their credentials
are not interchangeable, so the setup UI must show that distinction instead
of treating every models.dev provider as a directly callable API provider.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderProfile:
    """Callable and catalog identity for one provider-facing setup option."""

    id: str
    label: str
    models_dev_provider: str
    kind: str = "api"
    callable: bool = True
    credential_scope: str = "api"
    note: str = ""

    @property
    def status_label(self) -> str:
        return "可配置" if self.callable else "仅模型库"


CORE_PROVIDER_KEYS: list[str] = [
    # Major China-region providers.
    "qwen",
    "deepseek",
    "moonshot",
    "minimax",
    "zhipu",
    "doubao",
    "baidu",
    "tencent",
    "baichuan",
    "xiaomi",
    # Major global providers.
    "openai",
    "anthropic",
    "google",
    "xai",
    "meta",
    "mistral",
    "groq",
    "together",
    "fireworks",
    "deepinfra",
    # Router and aggregator providers.
    "openrouter",
]


PROVIDER_PROFILES: dict[str, list[ProviderProfile]] = {
    "qwen": [
        ProviderProfile("api", "DashScope 官方 API", "alibaba"),
        ProviderProfile("api-cn", "阿里云百炼中国区 API", "alibaba-cn"),
        ProviderProfile(
            "coding-plan",
            "Coding Plan",
            "alibaba-coding-plan",
            kind="coding_plan",
            callable=False,
            credential_scope="coding-plan",
            note="Coding Plan 凭据不能当作 DashScope API key 使用。",
        ),
        ProviderProfile(
            "token-plan",
            "Token Plan",
            "alibaba-token-plan",
            kind="token_plan",
            callable=False,
            credential_scope="token-plan",
            note="Token Plan 是计费/套餐视角，不是独立 API 接口。",
        ),
    ],
    "deepseek": [ProviderProfile("api", "DeepSeek 官方 API", "deepseek")],
    "moonshot": [
        ProviderProfile("api", "Moonshot 官方 API", "moonshotai"),
        ProviderProfile("api-cn", "Moonshot 中国区 API", "moonshotai-cn"),
        ProviderProfile(
            "coding-plan",
            "Kimi Coding Plan",
            "kimi-for-coding",
            kind="coding_plan",
            callable=False,
            credential_scope="coding-plan",
            note="Kimi Coding Plan key 不能作为 Moonshot API key 使用。",
        ),
    ],
    "minimax": [
        ProviderProfile("api", "MiniMax 官方 API", "minimax"),
        ProviderProfile("api-cn", "MiniMax 中国区 API", "minimax-cn"),
        ProviderProfile(
            "coding-plan",
            "MiniMax Coding Plan",
            "minimax-coding-plan",
            kind="coding_plan",
            callable=False,
            credential_scope="coding-plan",
            note="Coding Plan 与普通 MiniMax API key 不通用。",
        ),
    ],
    "minimax-cn": [ProviderProfile("api", "MiniMax 中国区 API", "minimax-cn")],
    "zhipu": [
        ProviderProfile("api", "智谱开放平台 API", "zhipuai"),
        ProviderProfile(
            "coding-plan",
            "GLM Coding Plan",
            "zhipuai-coding-plan",
            kind="coding_plan",
            callable=False,
            credential_scope="coding-plan",
            note="Coding Plan 与 open.bigmodel.cn API key 不通用。",
        ),
    ],
    "doubao": [ProviderProfile("api", "火山方舟 / 豆包 API", "bytedance")],
    "baidu": [ProviderProfile("api", "百度智能云千帆 API", "qianfan")],
    "tencent": [
        ProviderProfile("api", "腾讯混元 API", "hunyuan"),
        ProviderProfile(
            "coding-plan",
            "Tencent Coding Plan",
            "tencent-coding-plan",
            kind="coding_plan",
            callable=False,
            credential_scope="coding-plan",
            note="Coding Plan 不等同于腾讯混元 API key。",
        ),
        ProviderProfile(
            "tokenhub",
            "Tencent TokenHub",
            "tencent-tokenhub",
            kind="token_plan",
            callable=False,
            credential_scope="tokenhub",
            note="TokenHub 是模型库/计费入口，不是 M-Claw 的直接调用接口。",
        ),
    ],
    "baichuan": [ProviderProfile("api", "百川官方 API", "baichuan")],
    "xiaomi": [
        ProviderProfile("api", "小米 MiMo 官方 API", "xiaomi"),
        ProviderProfile(
            "token-plan-cn",
            "MiMo Token Plan 中国区",
            "xiaomi-token-plan-cn",
            kind="token_plan",
            callable=False,
            credential_scope="token-plan",
            note="Token Plan 与 MiMo 官方 API key 不通用。",
        ),
    ],
    "openai": [ProviderProfile("api", "OpenAI API", "openai")],
    "anthropic": [
        ProviderProfile("api", "Anthropic API", "anthropic"),
        ProviderProfile(
            "vertex",
            "Google Vertex Anthropic",
            "google-vertex-anthropic",
            kind="cloud_hosted",
            callable=False,
            credential_scope="google-vertex",
            note="Vertex 上的 Anthropic 模型需要 Google Cloud 凭据，不使用 Anthropic API key。",
        ),
    ],
    "google": [
        ProviderProfile("api", "Google AI Studio API", "google"),
        ProviderProfile(
            "vertex",
            "Google Vertex AI",
            "google-vertex",
            kind="cloud_hosted",
            callable=False,
            credential_scope="google-vertex",
            note="Vertex AI 需要 Google Cloud 项目凭据，不使用 AI Studio API key。",
        ),
    ],
    "xai": [ProviderProfile("api", "xAI API", "xai")],
    "microsoft": [ProviderProfile("api", "Azure AI / Azure OpenAI API", "azure")],
    "meta": [
        ProviderProfile("api", "Meta Llama API", "llama"),
    ],
    "mistral": [ProviderProfile("api", "Mistral API", "mistral")],
    "cohere": [ProviderProfile("api", "Cohere API", "cohere")],
    "amazon": [ProviderProfile("api", "Amazon Bedrock API", "amazon-bedrock")],
    "groq": [ProviderProfile("host", "Groq 托管模型 API", "groq", kind="host")],
    "together": [ProviderProfile("host", "Together AI 托管模型 API", "togetherai", kind="host")],
    "fireworks": [ProviderProfile("host", "Fireworks AI 托管模型 API", "fireworks-ai", kind="host")],
    "deepinfra": [ProviderProfile("host", "DeepInfra 托管模型 API", "deepinfra", kind="host")],
    "openrouter": [ProviderProfile("router", "OpenRouter 中转接口", "openrouter", kind="router")],
    "siliconflow": [ProviderProfile("host", "SiliconFlow 托管模型 API", "siliconflow", kind="host")],
    "yi": [ProviderProfile("api", "01.AI API", "yi")],
    "stepfun": [ProviderProfile("api", "阶跃星辰 API", "stepfun")],
    "perplexity": [ProviderProfile("api", "Perplexity API", "perplexity")],
}


def get_provider_profiles(provider_key: str) -> list[ProviderProfile]:
    provider = str(provider_key or "").strip()
    profiles = PROVIDER_PROFILES.get(provider)
    if profiles:
        return profiles
    return [ProviderProfile("api", "官方 API", provider)]


def get_provider_profile(provider_key: str, profile_id: str = "") -> ProviderProfile:
    profile = find_provider_profile(provider_key, profile_id)
    if profile:
        return profile
    return get_default_provider_profile(provider_key)


def find_provider_profile(provider_key: str, profile_id: str = "") -> ProviderProfile | None:
    """Return an explicitly requested profile, or the default when omitted.

    Unlike ``get_provider_profile``, this returns ``None`` for an unknown
    explicit profile id.  Use it in user-facing flows where silently falling
    back to ``api`` would hide an input mistake.
    """
    profiles = get_provider_profiles(provider_key)
    requested = str(profile_id or "").strip().lower()
    if requested:
        for profile in profiles:
            if profile.id.lower() == requested:
                return profile
        return None
    return get_default_provider_profile(provider_key)


def get_default_provider_profile(provider_key: str) -> ProviderProfile:
    profiles = get_provider_profiles(provider_key)
    for profile in profiles:
        if profile.callable and profile.kind == "api":
            return profile
    for profile in profiles:
        if profile.callable:
            return profile
    return profiles[0]


def resolve_models_dev_provider(provider_key: str, profile_id: str = "") -> str:
    """Return the models.dev provider id that backs a M-Claw provider profile."""
    return get_provider_profile(provider_key, profile_id).models_dev_provider


def profile_help_lines(provider_key: str) -> list[str]:
    """Format profile choices for errors and setup prompts."""
    lines: list[str] = []
    for profile in get_provider_profiles(provider_key):
        suffix = f"；{profile.note}" if profile.note else ""
        lines.append(f"{profile.id}: {profile.label} [{profile.status_label}]{suffix}")
    return lines


def search_models_dev_provider_ids(query: str, provider_ids: list[str], *, limit: int = 10) -> list[str]:
    """Find likely models.dev provider ids without treating matches as routing."""
    raw = str(query or "").strip()
    if not raw:
        return []
    key = _compact(raw)
    scored: list[tuple[float, str]] = []
    for provider_id in provider_ids:
        candidate = _compact(provider_id)
        score = difflib.SequenceMatcher(None, key, candidate).ratio()
        if key and (key in candidate or candidate in key):
            score = max(score, 0.8)
        if score >= 0.55:
            scored.append((score, provider_id))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [provider_id for _score, provider_id in scored[:limit]]


def _compact(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())
