# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The single metadata source for built-in model providers."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TypeVar

from mclaw.providers.anthropic import AnthropicProfile
from mclaw.providers.base import (
    RuntimeProviderProfile,
    SetupProfileEntry,
    default_models_url,
)
from mclaw.providers.deepseek import DeepSeekProfile
from mclaw.providers.gemini import GoogleGeminiProfile
from mclaw.providers.generic import GenericOpenAICompatibleProfile
from mclaw.providers.kimi import MoonshotKimiProfile
from mclaw.providers.minimax import MiniMaxProfile
from mclaw.providers.openai import OpenAIProfile
from mclaw.providers.openrouter import OpenRouterProfile
from mclaw.providers.qwen import QwenProfile
from mclaw.providers.xai import XAIProfile
from mclaw.providers.xiaomi import XiaomiMiMoProfile
from mclaw.providers.zhipu import ZhipuGLMProfile

_Profile = TypeVar("_Profile", bound=RuntimeProviderProfile)

_SETUP_ORDER = {
    name: index
    for index, name in enumerate((
        "qwen", "deepseek", "moonshot", "minimax", "zhipu", "doubao",
        "baidu", "tencent", "baichuan", "xiaomi", "openai", "anthropic",
        "google", "xai", "meta", "mistral", "groq", "together",
        "fireworks", "deepinfra", "openrouter",
    ))
}

_FALLBACK_MODELS: dict[str, tuple[str, ...]] = {
    "openai": ("gpt-5.6", "gpt-5.4"),
    "anthropic": ("claude-sonnet-5", "claude-opus-4-8"),
    "openrouter": ("anthropic/claude-sonnet-5", "openai/gpt-5.6"),
    "deepseek": ("deepseek-v4-pro", "deepseek-v4-flash"),
    "baidu": ("ernie-4.5-turbo-128k", "ernie-5.0-thinking-preview"),
    "tencent": ("hunyuan-turbos-latest", "hunyuan-t1-latest"),
    "xiaomi": ("mimo-v2.5-pro", "mimo-v2.5"),
    "groq": ("llama-3.3-70b-versatile", "llama-3.1-8b-instant"),
    "fireworks": ("accounts/fireworks/models/llama-v3p1-70b-instruct",),
    "deepinfra": ("meta-llama/Meta-Llama-3.1-70B-Instruct", "deepseek-ai/DeepSeek-V3"),
    "mistral": ("mistral-large-latest", "mistral-small-latest"),
    "microsoft": ("gpt-4o-mini", "Phi-4"),
    "cohere": ("command-a-plus-05-2026", "command-a"),
    "amazon": ("openai.gpt-oss-120b", "amazon.nova-pro-v1:0"),
    "together": ("meta-llama/Llama-3.3-70B-Instruct-Turbo", "deepseek-ai/DeepSeek-V3"),
    "perplexity": ("sonar-pro", "sonar"),
    "meta": ("llama-4-maverick", "llama-4-scout"),
    "moonshot": ("kimi-k3", "kimi-k2.6", "kimi-k2.7-code", "kimi-k2.5"),
    "moonshot-intl": ("kimi-k3", "kimi-k2.6", "kimi-k2.7-code", "kimi-k2.7-code-highspeed"),
    "minimax": ("MiniMax-M3", "MiniMax-M2.7", "MiniMax-M2"),
    "minimax-cn": ("MiniMax-M3", "MiniMax-M2.7-highspeed", "MiniMax-M2", "MiniMax-M2.7-highspeed"),
    "google": ("gemini-3.5-flash", "gemini-2.5-pro"),
    "zhipu": ("glm-5.2", "glm-5.1", "glm-5"),
    "qwen": ("qwen3.7-plus", "qwen3-plus"),
    "qwen-intl": ("qwen3.7-plus", "qwen3-plus"),
    "yi": ("yi-lightning", "yi-large"),
    "stepfun": ("step-3.5-flash", "step-1-8k"),
    "xai": ("grok-4.5", "grok-4.3"),
}


def _entry(
    profile_type: type[_Profile],
    name: str,
    display_name: str,
    env_vars: tuple[str, ...],
    base_url: str,
    *,
    base_url_env_var: str = "",
    base_url_required: bool = False,
    key_url: str = "",
    models_dev_provider: str = "",
    model_prefixes: tuple[str, ...] = (),
    aliases: tuple[str, ...] = (),
    provider_kind: str = "direct",
    api_mode: str = "chat_completions",
    auth_scheme: str = "bearer",
    setup_profiles: tuple[SetupProfileEntry, ...] | None = None,
    models_url: str = "",
) -> _Profile:
    """Build one frozen profile while deriving mechanical metadata once."""
    base_url = base_url.rstrip("/")
    if setup_profiles is None:
        setup_profiles = (
            SetupProfileEntry(
                "api",
                f"{display_name} API",
                runtime_provider=name,
                models_dev_provider=models_dev_provider,
            ),
        )
    return profile_type(
        name=name,
        display_name=display_name,
        provider_kind=provider_kind,
        api_mode=api_mode,
        auth_scheme=auth_scheme,
        aliases=aliases,
        env_vars=env_vars,
        base_url=base_url,
        base_url_required=base_url_required,
        base_url_env_var=base_url_env_var,
        key_url=key_url,
        models_url=models_url or default_models_url(base_url, api_mode),
        models_dev_provider=models_dev_provider,
        setup_profiles=setup_profiles,
        setup_order=_SETUP_ORDER.get(name),
        model_prefixes=model_prefixes,
        fallback_models=_FALLBACK_MODELS.get(name, ()),
    )


PROVIDER_REGISTRY: dict[str, RuntimeProviderProfile] = {
    "openrouter": _entry(
        OpenRouterProfile, "openrouter", "OpenRouter", ("OPENROUTER_API_KEY",),
        "https://openrouter.ai/api/v1", key_url="https://openrouter.ai/keys",
        models_dev_provider="openrouter", aliases=("router", "open router"),
        provider_kind="router",
    ),
    "openai": _entry(
        OpenAIProfile, "openai", "OpenAI", ("OPENAI_API_KEY",),
        "https://api.openai.com/v1", base_url_env_var="OPENAI_BASE_URL",
        key_url="https://platform.openai.com/api-keys", models_dev_provider="openai",
        model_prefixes=("gpt-", "o1", "o3", "o4", "o5"),
    ),
    "anthropic": _entry(
        AnthropicProfile, "anthropic", "Anthropic", ("ANTHROPIC_API_KEY",),
        "https://api.anthropic.com", key_url="https://console.anthropic.com/settings/keys",
        models_dev_provider="anthropic", model_prefixes=("claude",),
        api_mode="anthropic_messages", auth_scheme="anthropic_x_api_key",
        models_url="https://api.anthropic.com/v1/models",
        setup_profiles=(
            SetupProfileEntry("api", "Anthropic API", "anthropic", "anthropic"),
            SetupProfileEntry(
                "vertex", "Google Vertex Anthropic", models_dev_provider="google-vertex-anthropic",
                kind="cloud_hosted", callable=False, credential_scope="google-vertex",
                note="Vertex 上的 Anthropic 模型需要 Google Cloud 凭据。",
            ),
        ),
    ),
    "google": _entry(
        GoogleGeminiProfile, "google", "Google AI Studio",
        ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
        "https://generativelanguage.googleapis.com/v1beta/openai",
        base_url_env_var="GEMINI_BASE_URL", key_url="https://aistudio.google.com/app/apikey",
        models_dev_provider="google", model_prefixes=("gemini", "gemma"),
        aliases=("gemini", "google ai"),
        setup_profiles=(
            SetupProfileEntry("api", "Google AI Studio API", "google", "google"),
            SetupProfileEntry(
                "vertex", "Google Vertex AI", models_dev_provider="google-vertex",
                kind="cloud_hosted", callable=False, credential_scope="google-vertex",
                note="Vertex AI 需要 Google Cloud 项目凭据。",
            ),
        ),
    ),
    "deepseek": _entry(
        DeepSeekProfile, "deepseek", "DeepSeek", ("DEEPSEEK_API_KEY",),
        "https://api.deepseek.com/v1", base_url_env_var="DEEPSEEK_BASE_URL",
        key_url="https://platform.deepseek.com/api_keys", models_dev_provider="deepseek",
        model_prefixes=("deepseek-chat", "deepseek-reasoner", "deepseek-v", "deepseek-r", "deepseek-coder"),
    ),
    "baidu": _entry(
        GenericOpenAICompatibleProfile, "baidu", "Baidu / Qianfan",
        ("QIANFAN_API_KEY", "BAIDU_API_KEY"), "https://qianfan.baidubce.com/v2",
        base_url_env_var="QIANFAN_BASE_URL",
        key_url="https://console.bce.baidu.com/qianfan/ais/console/apiKey",
        models_dev_provider="qianfan", model_prefixes=("ernie", "qianfan"),
        aliases=("qianfan", "wenxin", "ernie", "百度", "千帆", "文心"),
    ),
    "tencent": _entry(
        GenericOpenAICompatibleProfile, "tencent", "Tencent / Hunyuan",
        ("HUNYUAN_API_KEY", "TENCENT_HUNYUAN_API_KEY"),
        "https://api.hunyuan.cloud.tencent.com/v1", base_url_env_var="HUNYUAN_BASE_URL",
        key_url="https://console.cloud.tencent.com/hunyuan/api-key",
        models_dev_provider="hunyuan", model_prefixes=("hunyuan",),
        aliases=("hunyuan", "腾讯", "混元"),
        setup_profiles=(
            SetupProfileEntry("api", "腾讯混元 API", "tencent", "hunyuan"),
            SetupProfileEntry(
                "coding-plan", "Tencent Coding Plan", models_dev_provider="tencent-coding-plan",
                kind="coding_plan", callable=False, credential_scope="coding-plan",
                note="Coding Plan 不等同于腾讯混元 API key。",
            ),
            SetupProfileEntry(
                "tokenhub", "Tencent TokenHub", models_dev_provider="tencent-tokenhub",
                kind="token_plan", callable=False, credential_scope="tokenhub",
                note="TokenHub 是模型库/计费入口。",
            ),
        ),
    ),
    "xiaomi": _entry(
        XiaomiMiMoProfile, "xiaomi", "Xiaomi / MiMo",
        ("XIAOMI_MIMO_API_KEY", "MIMO_API_KEY"), "https://api.xiaomimimo.com/v1",
        base_url_env_var="XIAOMI_MIMO_BASE_URL",
        key_url="https://platform.xiaomimimo.com/#/console/api-keys",
        models_dev_provider="xiaomi", model_prefixes=("mimo",),
        aliases=("mimo", "mi", "小米"),
        setup_profiles=(
            SetupProfileEntry("api", "小米 MiMo 官方 API", "xiaomi", "xiaomi"),
            SetupProfileEntry(
                "token-plan-cn", "MiMo Token Plan 中国区", models_dev_provider="xiaomi-token-plan-cn",
                kind="token_plan", callable=False, credential_scope="token-plan",
                note="Token Plan 与 MiMo 官方 API key 不通用。",
            ),
        ),
    ),
    "groq": _entry(
        GenericOpenAICompatibleProfile, "groq", "Groq", ("GROQ_API_KEY",),
        "https://api.groq.com/openai/v1", base_url_env_var="GROQ_BASE_URL",
        key_url="https://console.groq.com/keys", models_dev_provider="groq",
        model_prefixes=("llama", "mixtral", "gemma"), provider_kind="host",
    ),
    "fireworks": _entry(
        GenericOpenAICompatibleProfile, "fireworks", "Fireworks AI", ("FIREWORKS_API_KEY",),
        "https://api.fireworks.ai/inference/v1", base_url_env_var="FIREWORKS_BASE_URL",
        key_url="https://fireworks.ai/account/api-keys", models_dev_provider="fireworks-ai",
        model_prefixes=("accounts/fireworks", "firefunction"),
        aliases=("fireworks ai",), provider_kind="host",
    ),
    "deepinfra": _entry(
        GenericOpenAICompatibleProfile, "deepinfra", "DeepInfra", ("DEEPINFRA_API_KEY",),
        "https://api.deepinfra.com/v1/openai", base_url_env_var="DEEPINFRA_BASE_URL",
        key_url="https://deepinfra.com/dash/api_keys", models_dev_provider="deepinfra",
        model_prefixes=("meta-llama", "deepseek-ai", "Qwen", "XiaomiMiMo", "MiniMaxAI"),
        aliases=("deep infra",), provider_kind="host",
    ),
    "mistral": _entry(
        GenericOpenAICompatibleProfile, "mistral", "Mistral AI", ("MISTRAL_API_KEY",),
        "https://api.mistral.ai/v1", base_url_env_var="MISTRAL_BASE_URL",
        key_url="https://console.mistral.ai/api-keys", models_dev_provider="mistral",
        model_prefixes=("mistral", "ministral", "codestral", "magistral"),
    ),
    "microsoft": _entry(
        GenericOpenAICompatibleProfile, "microsoft", "Microsoft / Azure AI",
        ("AZURE_OPENAI_API_KEY", "AZURE_AI_API_KEY", "MICROSOFT_AI_API_KEY"), "",
        base_url_env_var="AZURE_OPENAI_BASE_URL", base_url_required=True,
        key_url="https://ai.azure.com/", models_dev_provider="azure",
        model_prefixes=("gpt-", "o1", "o3", "o4", "o5", "phi", "microsoft"),
        aliases=("azure", "azure openai", "azure ai", "微软"), provider_kind="host",
    ),
    "cohere": _entry(
        GenericOpenAICompatibleProfile, "cohere", "Cohere", ("COHERE_API_KEY",),
        "https://api.cohere.ai/compatibility/v1", base_url_env_var="COHERE_BASE_URL",
        key_url="https://dashboard.cohere.com/api-keys", models_dev_provider="cohere",
        model_prefixes=("command", "c4ai", "aya"),
    ),
    "amazon": _entry(
        GenericOpenAICompatibleProfile, "amazon", "Amazon Bedrock",
        ("AWS_BEARER_TOKEN_BEDROCK", "BEDROCK_API_KEY"),
        "https://bedrock-mantle.us-east-1.api.aws/v1", base_url_env_var="BEDROCK_BASE_URL",
        key_url="https://console.aws.amazon.com/bedrock/", models_dev_provider="amazon-bedrock",
        model_prefixes=("amazon.", "anthropic.", "meta.", "mistral.", "openai."),
        aliases=("aws", "bedrock", "amazon bedrock"), provider_kind="host",
    ),
    "together": _entry(
        GenericOpenAICompatibleProfile, "together", "Together AI", ("TOGETHER_API_KEY",),
        "https://api.together.xyz/v1", base_url_env_var="TOGETHER_BASE_URL",
        key_url="https://api.together.ai/settings/api-keys", models_dev_provider="togetherai",
        model_prefixes=("meta-llama", "mistralai", "deepseek-ai", "qwen"), provider_kind="host",
    ),
    "perplexity": _entry(
        GenericOpenAICompatibleProfile, "perplexity", "Perplexity", ("PERPLEXITY_API_KEY",),
        "https://api.perplexity.ai", base_url_env_var="PERPLEXITY_BASE_URL",
        key_url="https://www.perplexity.ai/settings/api", models_dev_provider="perplexity",
        model_prefixes=("sonar",),
    ),
    "meta": _entry(
        GenericOpenAICompatibleProfile, "meta", "Meta / Llama", ("LLAMA_API_KEY", "META_API_KEY"),
        "https://llama-api.meta.com/compat/v1", base_url_env_var="LLAMA_BASE_URL",
        key_url="https://llama.developer.meta.com/", models_dev_provider="llama",
        model_prefixes=("llama", "meta-llama"), aliases=("llama", "meta llama"),
    ),
    "moonshot": _entry(
        MoonshotKimiProfile, "moonshot", "Moonshot / Kimi (China)",
        ("KIMI_API_KEY", "MOONSHOT_API_KEY"), "https://api.moonshot.cn/v1",
        base_url_env_var="KIMI_BASE_URL", key_url="https://platform.moonshot.cn/console/api-keys",
        models_dev_provider="moonshotai-cn", model_prefixes=("moonshot", "kimi"),
        aliases=("kimi", "月之暗面"),
        setup_profiles=(
            SetupProfileEntry("api", "Moonshot International API", "moonshot-intl", "moonshotai"),
            SetupProfileEntry("api-cn", "Moonshot 中国区 API", "moonshot", "moonshotai-cn"),
            SetupProfileEntry(
                "coding-plan", "Kimi Coding Plan", models_dev_provider="kimi-for-coding",
                kind="coding_plan", callable=False, credential_scope="coding-plan",
                note="Kimi Coding Plan key 不能作为 Moonshot API key 使用。",
            ),
        ),
    ),
    "moonshot-intl": _entry(
        MoonshotKimiProfile, "moonshot-intl", "Moonshot / Kimi (International)",
        ("KIMI_INTL_API_KEY", "MOONSHOT_INTL_API_KEY", "KIMI_API_KEY", "MOONSHOT_API_KEY"),
        "https://api.moonshot.ai/v1", base_url_env_var="KIMI_INTL_BASE_URL",
        key_url="https://platform.moonshot.ai/console/api-keys", models_dev_provider="moonshotai",
        model_prefixes=("moonshot", "kimi"), aliases=("kimi-intl", "moonshot international"),
    ),
    "minimax": _entry(
        MiniMaxProfile, "minimax", "MiniMax (International)", ("MINIMAX_API_KEY",),
        "https://api.minimax.io/v1", base_url_env_var="MINIMAX_BASE_URL",
        key_url="https://platform.minimax.io/user-center/basic-information/interface-key",
        models_dev_provider="minimax", model_prefixes=("minimax", "abab"), aliases=("abab", "海螺"),
        setup_profiles=(
            SetupProfileEntry("api", "MiniMax International API", "minimax", "minimax"),
            SetupProfileEntry("api-cn", "MiniMax 中国区 API", "minimax-cn", "minimax-cn"),
            SetupProfileEntry(
                "coding-plan", "MiniMax Coding Plan", models_dev_provider="minimax-coding-plan",
                kind="coding_plan", callable=False, credential_scope="coding-plan",
                note="Coding Plan 与普通 MiniMax API key 不通用。",
            ),
        ),
    ),
    "minimax-cn": _entry(
        MiniMaxProfile, "minimax-cn", "MiniMax (China)",
        ("MINIMAX_CN_API_KEY", "MINIMAX_API_KEY"), "https://api.minimaxi.com/v1",
        base_url_env_var="MINIMAX_CN_BASE_URL",
        key_url="https://platform.minimaxi.com/user-center/basic-information/interface-key",
        models_dev_provider="minimax-cn", model_prefixes=("minimax", "abab"),
    ),
    "zhipu": _entry(
        ZhipuGLMProfile, "zhipu", "Zhipu AI / GLM",
        ("GLM_API_KEY", "ZHIPU_API_KEY", "ZAI_API_KEY", "Z_AI_API_KEY"),
        "https://open.bigmodel.cn/api/paas/v4", base_url_env_var="GLM_BASE_URL",
        key_url="https://open.bigmodel.cn/usercenter/apikeys", models_dev_provider="zhipuai",
        model_prefixes=("glm", "chatglm"), aliases=("glm", "zai", "z.ai", "bigmodel", "chatglm", "智谱"),
        setup_profiles=(
            SetupProfileEntry("api", "智谱开放平台 API", "zhipu", "zhipuai"),
            SetupProfileEntry(
                "coding-plan", "GLM Coding Plan", models_dev_provider="zhipuai-coding-plan",
                kind="coding_plan", callable=False, credential_scope="coding-plan",
                note="Coding Plan 与 open.bigmodel.cn API key 不通用。",
            ),
        ),
    ),
    "qwen": _entry(
        QwenProfile, "qwen", "Alibaba / Qwen (China)", ("DASHSCOPE_API_KEY", "QWEN_API_KEY"),
        "https://dashscope.aliyuncs.com/compatible-mode/v1", base_url_env_var="DASHSCOPE_BASE_URL",
        key_url="https://dashscope.console.aliyun.com/apiKey", models_dev_provider="alibaba-cn",
        model_prefixes=("qwen",), aliases=("dashscope", "bailian", "aliyun", "alibaba", "通义", "百炼", "阿里"),
        setup_profiles=(
            SetupProfileEntry("api", "DashScope International API", "qwen-intl", "alibaba"),
            SetupProfileEntry("api-cn", "阿里云百炼中国区 API", "qwen", "alibaba-cn"),
            SetupProfileEntry(
                "coding-plan", "Coding Plan", models_dev_provider="alibaba-coding-plan",
                kind="coding_plan", callable=False, credential_scope="coding-plan",
                note="Coding Plan 凭据不能当作 DashScope API key 使用。",
            ),
            SetupProfileEntry(
                "token-plan", "Token Plan", models_dev_provider="alibaba-token-plan",
                kind="token_plan", callable=False, credential_scope="token-plan",
                note="Token Plan 是计费/套餐视角。",
            ),
        ),
    ),
    "qwen-intl": _entry(
        QwenProfile, "qwen-intl", "Alibaba / Qwen (International)",
        ("DASHSCOPE_INTL_API_KEY", "DASHSCOPE_API_KEY", "QWEN_API_KEY"),
        "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        base_url_env_var="DASHSCOPE_INTL_BASE_URL", key_url="https://dashscope.console.aliyun.com/apiKey",
        models_dev_provider="alibaba", model_prefixes=("qwen",),
        aliases=("dashscope-intl", "bailian-intl", "qwen international"),
    ),
    "yi": _entry(
        GenericOpenAICompatibleProfile, "yi", "01.AI / Yi", ("YI_API_KEY",),
        "https://api.lingyiwanwu.com/v1", base_url_env_var="YI_BASE_URL",
        key_url="https://platform.01.ai/apikeys", models_dev_provider="yi", model_prefixes=("yi-",),
    ),
    "stepfun": _entry(
        GenericOpenAICompatibleProfile, "stepfun", "StepFun", ("STEPFUN_API_KEY", "STEP_API_KEY"),
        "https://api.stepfun.com/v1", base_url_env_var="STEPFUN_BASE_URL",
        key_url="https://platform.stepfun.com/interface-key", models_dev_provider="stepfun",
        model_prefixes=("step-",),
    ),
    "baichuan": _entry(
        GenericOpenAICompatibleProfile, "baichuan", "Baichuan", ("BAICHUAN_API_KEY",),
        "https://api.baichuan-ai.com/v1", base_url_env_var="BAICHUAN_BASE_URL",
        key_url="https://platform.baichuan-ai.com/console/apikey", models_dev_provider="baichuan",
        model_prefixes=("baichuan",),
    ),
    "doubao": _entry(
        GenericOpenAICompatibleProfile, "doubao", "Doubao / ByteDance",
        ("DOUBAO_API_KEY", "ARK_API_KEY"), "https://ark.cn-beijing.volces.com/api/v3",
        base_url_env_var="DOUBAO_BASE_URL", key_url="https://console.volcengine.com/ark",
        models_dev_provider="bytedance", model_prefixes=("doubao", "ep-"),
        aliases=("volcengine", "ark", "bytedance", "豆包", "火山", "方舟"),
    ),
    "xai": _entry(
        XAIProfile, "xai", "xAI / Grok", ("XAI_API_KEY",), "https://api.x.ai/v1",
        base_url_env_var="XAI_BASE_URL", key_url="https://console.x.ai/",
        models_dev_provider="xai", model_prefixes=("grok",), aliases=("grok",),
    ),
    "siliconflow": _entry(
        GenericOpenAICompatibleProfile, "siliconflow", "SiliconFlow", ("SILICONFLOW_API_KEY",),
        "https://api.siliconflow.cn/v1", base_url_env_var="SILICONFLOW_BASE_URL",
        key_url="https://cloud.siliconflow.cn/account/ak", models_dev_provider="siliconflow",
        aliases=("硅基流动", "silicon"), provider_kind="host",
    ),
}


def get_runtime_profile(provider: str) -> RuntimeProviderProfile:
    """Return one canonical built-in profile."""
    return PROVIDER_REGISTRY[provider]


def iter_runtime_profiles() -> Iterable[RuntimeProviderProfile]:
    """Iterate profiles in stable registry order."""
    return PROVIDER_REGISTRY.values()
