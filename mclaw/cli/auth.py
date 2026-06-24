# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Authentication and provider resolution for M-Claw.

Resolves API credentials from environment variables, M-Claw home .env,
and config.yaml providers dict. Supports built-in and user-defined providers.
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from mclaw.cli.config import get_env_value
from mclaw.constants import OPENROUTER_BASE_URL

logger = logging.getLogger(__name__)


@dataclass
class ProviderConfig:
    name: str
    display_name: str
    api_key_env_vars: List[str]
    base_url: str
    base_url_env_var: str = ""
    api_mode: str = "chat_completions"
    key_url: str = ""
    model_prefixes: List[str] = field(default_factory=list)
    aliases: List[str] = field(default_factory=list)


# ── Built-in Provider Registry ──

PROVIDER_REGISTRY: Dict[str, ProviderConfig] = {
    "openrouter": ProviderConfig(
        name="openrouter",
        display_name="OpenRouter",
        api_key_env_vars=["OPENROUTER_API_KEY"],
        base_url=OPENROUTER_BASE_URL,
        key_url="https://openrouter.ai/keys",
        model_prefixes=[],
        aliases=["router", "open router"],
    ),
    "openai": ProviderConfig(
        name="openai",
        display_name="OpenAI",
        api_key_env_vars=["OPENAI_API_KEY"],
        base_url="https://api.openai.com/v1",
        base_url_env_var="OPENAI_BASE_URL",
        key_url="https://platform.openai.com/api-keys",
        model_prefixes=["gpt-", "o1", "o3", "o4", "o5"],
    ),
    "anthropic": ProviderConfig(
        name="anthropic",
        display_name="Anthropic",
        api_key_env_vars=["ANTHROPIC_API_KEY"],
        base_url="https://api.anthropic.com",
        api_mode="anthropic_messages",
        key_url="https://console.anthropic.com/settings/keys",
        model_prefixes=["claude"],
    ),
    "google": ProviderConfig(
        name="google",
        display_name="Google AI Studio",
        api_key_env_vars=["GOOGLE_API_KEY", "GEMINI_API_KEY"],
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        base_url_env_var="GEMINI_BASE_URL",
        key_url="https://aistudio.google.com/app/apikey",
        model_prefixes=["gemini", "gemma"],
        aliases=["gemini", "google ai"],
    ),
    "deepseek": ProviderConfig(
        name="deepseek",
        display_name="DeepSeek",
        api_key_env_vars=["DEEPSEEK_API_KEY"],
        base_url="https://api.deepseek.com/v1",
        base_url_env_var="DEEPSEEK_BASE_URL",
        key_url="https://platform.deepseek.com/api_keys",
        model_prefixes=["deepseek"],
    ),
    "baidu": ProviderConfig(
        name="baidu",
        display_name="Baidu / Qianfan",
        api_key_env_vars=["QIANFAN_API_KEY", "BAIDU_API_KEY"],
        base_url="https://qianfan.baidubce.com/v2",
        base_url_env_var="QIANFAN_BASE_URL",
        key_url="https://console.bce.baidu.com/qianfan/ais/console/apiKey",
        model_prefixes=["ernie", "qianfan"],
        aliases=["baidu", "qianfan", "wenxin", "ernie", "百度", "千帆", "文心"],
    ),
    "tencent": ProviderConfig(
        name="tencent",
        display_name="Tencent / Hunyuan",
        api_key_env_vars=["HUNYUAN_API_KEY", "TENCENT_HUNYUAN_API_KEY"],
        base_url="https://api.hunyuan.cloud.tencent.com/v1",
        base_url_env_var="HUNYUAN_BASE_URL",
        key_url="https://console.cloud.tencent.com/hunyuan/api-key",
        model_prefixes=["hunyuan"],
        aliases=["tencent", "hunyuan", "腾讯", "混元"],
    ),
    "xiaomi": ProviderConfig(
        name="xiaomi",
        display_name="Xiaomi / MiMo",
        api_key_env_vars=["XIAOMI_MIMO_API_KEY", "MIMO_API_KEY"],
        base_url="https://api.xiaomimimo.com/v1",
        base_url_env_var="XIAOMI_MIMO_BASE_URL",
        key_url="https://platform.xiaomimimo.com/#/console/api-keys",
        model_prefixes=["mimo"],
        aliases=["xiaomi", "mimo", "mi", "小米"],
    ),
    "groq": ProviderConfig(
        name="groq",
        display_name="Groq",
        api_key_env_vars=["GROQ_API_KEY"],
        base_url="https://api.groq.com/openai/v1",
        base_url_env_var="GROQ_BASE_URL",
        key_url="https://console.groq.com/keys",
        model_prefixes=["llama", "mixtral", "gemma"],
    ),
    "fireworks": ProviderConfig(
        name="fireworks",
        display_name="Fireworks AI",
        api_key_env_vars=["FIREWORKS_API_KEY"],
        base_url="https://api.fireworks.ai/inference/v1",
        base_url_env_var="FIREWORKS_BASE_URL",
        key_url="https://fireworks.ai/account/api-keys",
        model_prefixes=["accounts/fireworks", "firefunction"],
        aliases=["fireworks", "fireworks ai"],
    ),
    "deepinfra": ProviderConfig(
        name="deepinfra",
        display_name="DeepInfra",
        api_key_env_vars=["DEEPINFRA_API_KEY"],
        base_url="https://api.deepinfra.com/v1/openai",
        base_url_env_var="DEEPINFRA_BASE_URL",
        key_url="https://deepinfra.com/dash/api_keys",
        model_prefixes=["meta-llama", "deepseek-ai", "Qwen", "XiaomiMiMo", "MiniMaxAI"],
        aliases=["deepinfra", "deep infra"],
    ),
    "mistral": ProviderConfig(
        name="mistral",
        display_name="Mistral AI",
        api_key_env_vars=["MISTRAL_API_KEY"],
        base_url="https://api.mistral.ai/v1",
        base_url_env_var="MISTRAL_BASE_URL",
        key_url="https://console.mistral.ai/api-keys",
        model_prefixes=["mistral", "ministral", "codestral", "magistral"],
    ),
    "microsoft": ProviderConfig(
        name="microsoft",
        display_name="Microsoft / Azure AI",
        api_key_env_vars=["AZURE_OPENAI_API_KEY", "AZURE_AI_API_KEY", "MICROSOFT_AI_API_KEY"],
        base_url="https://models.inference.ai.azure.com",
        base_url_env_var="AZURE_OPENAI_BASE_URL",
        key_url="https://ai.azure.com/",
        model_prefixes=["gpt-", "o1", "o3", "o4", "o5", "phi", "microsoft"],
        aliases=["microsoft", "azure", "azure openai", "azure ai", "微软"],
    ),
    "cohere": ProviderConfig(
        name="cohere",
        display_name="Cohere",
        api_key_env_vars=["COHERE_API_KEY"],
        base_url="https://api.cohere.com/v2",
        base_url_env_var="COHERE_BASE_URL",
        key_url="https://dashboard.cohere.com/api-keys",
        model_prefixes=["command", "c4ai", "aya"],
    ),
    "amazon": ProviderConfig(
        name="amazon",
        display_name="Amazon Bedrock",
        api_key_env_vars=["AWS_BEARER_TOKEN_BEDROCK", "BEDROCK_API_KEY", "AWS_ACCESS_KEY_ID"],
        base_url="https://bedrock-mantle.us-east-1.api.aws/v1",
        base_url_env_var="BEDROCK_BASE_URL",
        key_url="https://console.aws.amazon.com/bedrock/",
        model_prefixes=["amazon", "anthropic", "meta", "mistral", "openai"],
        aliases=["amazon", "aws", "bedrock", "amazon bedrock"],
    ),
    "together": ProviderConfig(
        name="together",
        display_name="Together AI",
        api_key_env_vars=["TOGETHER_API_KEY"],
        base_url="https://api.together.xyz/v1",
        base_url_env_var="TOGETHER_BASE_URL",
        key_url="https://api.together.ai/settings/api-keys",
        model_prefixes=["meta-llama", "mistralai", "deepseek-ai", "qwen"],
    ),
    "perplexity": ProviderConfig(
        name="perplexity",
        display_name="Perplexity",
        api_key_env_vars=["PERPLEXITY_API_KEY"],
        base_url="https://api.perplexity.ai",
        base_url_env_var="PERPLEXITY_BASE_URL",
        key_url="https://www.perplexity.ai/settings/api",
        model_prefixes=["sonar"],
    ),
    "meta": ProviderConfig(
        name="meta",
        display_name="Meta / Llama",
        api_key_env_vars=["LLAMA_API_KEY", "META_API_KEY"],
        base_url="https://llama-api.meta.com/compat/v1",
        base_url_env_var="LLAMA_BASE_URL",
        key_url="https://llama.developer.meta.com/",
        model_prefixes=["llama", "meta-llama"],
        aliases=["meta", "llama", "meta llama"],
    ),
    "moonshot": ProviderConfig(
        name="moonshot",
        display_name="Moonshot / Kimi",
        api_key_env_vars=["KIMI_API_KEY", "MOONSHOT_API_KEY"],
        base_url="https://api.moonshot.cn/v1",
        base_url_env_var="KIMI_BASE_URL",
        key_url="https://platform.moonshot.cn/console/api-keys",
        model_prefixes=["moonshot", "kimi"],
        aliases=["kimi", "moonshot", "月之暗面"],
    ),
    "minimax": ProviderConfig(
        name="minimax",
        display_name="MiniMax",
        api_key_env_vars=["MINIMAX_API_KEY"],
        base_url="https://api.minimax.chat/v1",
        base_url_env_var="MINIMAX_BASE_URL",
        key_url="https://platform.minimaxi.com/user-center/basic-information/interface-key",
        model_prefixes=["minimax", "abab"],
        aliases=["minimax", "abab", "海螺"],
    ),
    "minimax-cn": ProviderConfig(
        name="minimax-cn",
        display_name="MiniMax (China)",
        api_key_env_vars=["MINIMAX_CN_API_KEY"],
        base_url="https://api.minimaxi.com/v1",
        base_url_env_var="MINIMAX_CN_BASE_URL",
        key_url="https://platform.minimaxi.com/user-center/basic-information/interface-key",
        model_prefixes=["minimax", "abab"],
    ),
    "zhipu": ProviderConfig(
        name="zhipu",
        display_name="Zhipu AI / GLM",
        api_key_env_vars=["GLM_API_KEY", "ZHIPU_API_KEY"],
        base_url="https://open.bigmodel.cn/api/paas/v4",
        base_url_env_var="GLM_BASE_URL",
        key_url="https://open.bigmodel.cn/usercenter/apikeys",
        model_prefixes=["glm"],
        aliases=["glm", "bigmodel", "智谱"],
    ),
    "qwen": ProviderConfig(
        name="qwen",
        display_name="Alibaba / Qwen (DashScope)",
        api_key_env_vars=["DASHSCOPE_API_KEY", "QWEN_API_KEY"],
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        base_url_env_var="DASHSCOPE_BASE_URL",
        key_url="https://dashscope.console.aliyun.com/apiKey",
        model_prefixes=["qwen"],
        aliases=["dashscope", "bailian", "aliyun", "alibaba", "qwen", "通义", "百炼", "阿里"],
    ),
    "yi": ProviderConfig(
        name="yi",
        display_name="01.AI / Yi",
        api_key_env_vars=["YI_API_KEY"],
        base_url="https://api.lingyiwanwu.com/v1",
        base_url_env_var="YI_BASE_URL",
        key_url="https://platform.01.ai/apikeys",
        model_prefixes=["yi-"],
    ),
    "stepfun": ProviderConfig(
        name="stepfun",
        display_name="StepFun",
        api_key_env_vars=["STEPFUN_API_KEY"],
        base_url="https://api.stepfun.com/v1",
        base_url_env_var="STEPFUN_BASE_URL",
        key_url="https://platform.stepfun.com/interface-key",
        model_prefixes=["step-"],
    ),
    "baichuan": ProviderConfig(
        name="baichuan",
        display_name="Baichuan",
        api_key_env_vars=["BAICHUAN_API_KEY"],
        base_url="https://api.baichuan-ai.com/v1",
        base_url_env_var="BAICHUAN_BASE_URL",
        key_url="https://platform.baichuan-ai.com/console/apikey",
        model_prefixes=["baichuan"],
    ),
    "doubao": ProviderConfig(
        name="doubao",
        display_name="Doubao / ByteDance",
        api_key_env_vars=["DOUBAO_API_KEY", "ARK_API_KEY"],
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        base_url_env_var="DOUBAO_BASE_URL",
        key_url="https://console.volcengine.com/ark",
        model_prefixes=["doubao", "ep-"],
        aliases=["doubao", "volcengine", "ark", "bytedance", "豆包", "火山", "方舟"],
    ),
    "xai": ProviderConfig(
        name="xai",
        display_name="xAI / Grok",
        api_key_env_vars=["XAI_API_KEY"],
        base_url="https://api.x.ai/v1",
        base_url_env_var="XAI_BASE_URL",
        key_url="https://console.x.ai/",
        model_prefixes=["grok"],
        aliases=["grok"],
    ),
    "siliconflow": ProviderConfig(
        name="siliconflow",
        display_name="SiliconFlow",
        api_key_env_vars=["SILICONFLOW_API_KEY"],
        base_url="https://api.siliconflow.cn/v1",
        base_url_env_var="SILICONFLOW_BASE_URL",
        key_url="https://cloud.siliconflow.cn/account/ak",
        model_prefixes=[],
        aliases=["硅基流动", "silicon"],
    ),
}


# Default provider model fallbacks.
# Used when the online /models API is unavailable due to network errors,
# timeouts, or authentication failures. Each provider lists a small set of
# stable, commonly used models.

DEFAULT_PROVIDER_MODELS: Dict[str, List[str]] = {
    "openai": [
        "gpt-4o", "gpt-4o-mini", "gpt-4-turbo",
        "gpt-4", "gpt-3.5-turbo",
    ],
    "anthropic": [
        "claude-sonnet-4-20250514", "claude-3-5-sonnet-20241022",
        "claude-3-opus-20240229", "claude-3-haiku-20240307",
    ],
    "openrouter": [
        "anthropic/claude-3.5-sonnet", "openai/gpt-4o",
        "google/gemini-2.5-flash", "meta-llama/llama-3-70b-instruct",
    ],
    "deepseek": [
        "deepseek-chat", "deepseek-reasoner",
    ],
    "baidu": [
        "ernie-4.5-turbo-128k", "ernie-5.0-thinking-preview",
        "deepseek-v3.2",
    ],
    "tencent": [
        "hunyuan-turbos-latest", "hunyuan-t1-latest",
        "hunyuan-large",
    ],
    "xiaomi": [
        "MiMo-V2-Pro", "MiMo-V2-Flash", "MiMo-V2-Omni",
    ],
    "groq": [
        "llama-3.3-70b-versatile", "llama-3.1-8b-instant",
        "gemma2-9b-it",
    ],
    "fireworks": [
        "accounts/fireworks/models/llama-v3p1-70b-instruct",
        "accounts/fireworks/models/firefunction-v2",
    ],
    "deepinfra": [
        "meta-llama/Meta-Llama-3.1-70B-Instruct",
        "deepseek-ai/DeepSeek-V3", "Qwen/Qwen2.5-72B-Instruct",
    ],
    "mistral": [
        "mistral-large-latest", "mistral-small-latest",
        "codestral-latest",
    ],
    "microsoft": [
        "gpt-4o-mini", "gpt-4o", "Phi-4",
    ],
    "cohere": [
        "command-a", "command-r-plus-08-2024",
        "command-a-reasoning-08-2025",
    ],
    "amazon": [
        "amazon.nova-pro-v1:0", "anthropic.claude-sonnet-4-5-20250929-v1:0",
        "openai.gpt-oss-120b-1:0",
    ],
    "together": [
        "meta-llama/Llama-3.3-70B-Instruct-Turbo",
        "deepseek-ai/DeepSeek-V3", "Qwen/Qwen2.5-72B-Instruct-Turbo",
    ],
    "perplexity": [
        "sonar-pro", "sonar", "sonar-reasoning-pro",
    ],
    "meta": [
        "llama-4-maverick", "llama-4-scout",
        "meta-llama/llama-3.3-70b-instruct",
    ],
    "moonshot": [
        "kimi-k2.5", "kimi-k2-thinking", "moonshot-v1-128k",
    ],
    "minimax": [
        "MiniMax-M1", "MiniMax-M1-40k", "MiniMax-M1-80k",
        "MiniMax-M1-128k", "MiniMax-M1-256k",
    ],
    "minimax-cn": [
        "MiniMax-M1", "MiniMax-M1-40k", "MiniMax-M1-80k",
        "MiniMax-M1-128k", "MiniMax-M1-256k",
    ],
    "google": [
        "gemini-2.5-flash", "gemini-2.5-pro", "gemma-4-31b-it",
    ],
    "zhipu": [
        "glm-4-flash", "glm-4-plus", "glm-4-long",
    ],
    "qwen": [
        "qwen-turbo", "qwen-plus", "qwen-max",
    ],
    "yi": [
        "yi-lightning", "yi-large",
    ],
    "azure": [
        "gpt-4o", "gpt-4o-mini", "gpt-4-turbo",
    ],
    "ollama": [
        "llama3.1:8b", "llama3.1:70b", "qwen2.5:14b", "mistral:7b",
    ],
}


# ── Model-to-Provider detection ──

def detect_provider_for_model(model_name: str) -> Optional[str]:
    """Infer which provider a model belongs to from its name.

    Pure prefix matching via PROVIDER_REGISTRY — no hardcoded catalog.
    Falls back to 'openrouter' for slash-format names (e.g. 'anthropic/claude-3.5-sonnet').
    Returns provider key or None.
    """
    if not model_name:
        return None
    lower = model_name.lower()

    for pname, pcfg in PROVIDER_REGISTRY.items():
        for prefix in pcfg.model_prefixes:
            if lower.startswith(prefix.lower()):
                return pname

    if "/" in model_name:
        prefix = lower.split("/")[0]
        # OpenRouter uses provider/model names, such as anthropic/claude-3.5-sonnet.
        # Infer openrouter only when the prefix looks like a known upstream provider.
        known_orgs = {
            "anthropic", "openai", "google", "meta-llama", "mistralai",
            "microsoft", "nousresearch", "qwen", "deepseek",
        }
        if prefix in known_orgs or prefix in PROVIDER_REGISTRY:
            return "openrouter"
    return None


def get_provider_models(provider: str) -> List[str]:
    """Get available model list for a provider.

    Flow: live API → DEFAULT_PROVIDER_MODELS fallback.
    Used by Setup and /model command.
    """
    config = PROVIDER_REGISTRY.get(provider)
    if not config:
        return []
    try:
        models = _fetch_provider_models_live(provider, config)
        if models:
            return models
    except Exception:
        pass
    return DEFAULT_PROVIDER_MODELS.get(provider, [])


def _fetch_provider_models_live(provider: str, config: ProviderConfig) -> List[str] | None:
    """Call provider's /models endpoint. Returns None on any failure."""
    if not config.base_url:
        return None
    api_key = resolve_api_key(provider)
    try:
        import requests
        resp = requests.get(
            f"{config.base_url.rstrip('/')}/models",
            timeout=5,
            headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
        )
        if resp.status_code == 200:
            data = resp.json()
            if "data" in data:
                return [m["id"] for m in data["data"] if m.get("id")]
    except Exception:
        pass
    return None


def resolve_api_key(provider_name: str) -> Optional[str]:
    """Resolve API key for a provider from env vars / .env."""
    config = PROVIDER_REGISTRY.get(provider_name)
    if not config:
        return get_env_value("OPENAI_API_KEY")
    for env_var in config.api_key_env_vars:
        val = get_env_value(env_var)
        if val:
            return val
    return None


def resolve_api_key_with_env_var(provider_name: str, config: dict | None = None) -> tuple[str, str]:
    """Resolve a provider key and report the env var that supplied it."""
    user_provider = _resolve_user_provider(provider_name, config)
    if user_provider:
        env_var = str(
            (_configured_providers(config).get(provider_name) or {}).get("api_key_env")
            or (_configured_providers(config).get(provider_name) or {}).get("env_var")
            or ""
        ).strip()
        return str(user_provider.get("api_key") or ""), env_var

    provider_cfg = PROVIDER_REGISTRY.get(provider_name)
    if not provider_cfg:
        return "", ""

    for env_var in provider_cfg.api_key_env_vars:
        value = get_env_value(env_var)
        if value:
            return value, env_var
    return "", ""


def resolve_base_url(provider_name: str) -> str:
    """Resolve base URL for a provider."""
    config = PROVIDER_REGISTRY.get(provider_name)
    if not config:
        return "https://api.openai.com/v1"
    if config.base_url_env_var:
        override = get_env_value(config.base_url_env_var)
        if override:
            return override
    return config.base_url


def _configured_providers(config: dict | None = None) -> Dict[str, dict]:
    if not isinstance(config, dict):
        return {}
    providers = config.get("providers", {})
    return providers if isinstance(providers, dict) else {}


def _resolve_user_provider(provider_name: str, config: dict | None = None) -> Optional[Dict]:
    providers = _configured_providers(config)
    provider_cfg = providers.get(provider_name)
    if not isinstance(provider_cfg, dict):
        return None

    env_var = str(provider_cfg.get("api_key_env") or provider_cfg.get("env_var") or "")
    api_key = ""
    if env_var:
        api_key = get_env_value(env_var) or ""

    base_url = str(provider_cfg.get("base_url") or "").rstrip("/")
    api_mode = str(provider_cfg.get("api_mode") or "chat_completions")
    model = str(provider_cfg.get("model") or provider_cfg.get("default_model") or "")
    return {
        "provider": provider_name,
        "model": model,
        "api_key": api_key,
        "base_url": base_url,
        "api_mode": api_mode,
    }


def _resolve_config_profile(provider_name: str, config: dict | None = None) -> str:
    """Return a valid callable active_provider_profile for a built-in provider."""
    if not provider_name or provider_name not in PROVIDER_REGISTRY or not isinstance(config, dict):
        return ""
    active_provider = str(config.get("active_provider") or "").strip()
    if active_provider and active_provider != provider_name:
        return ""
    profile_id = str(config.get("active_provider_profile") or "").strip()
    if not profile_id:
        return ""
    try:
        from mclaw.cli.provider_profiles import find_provider_profile

        profile = find_provider_profile(provider_name, profile_id)
        if profile and profile.callable:
            return profile.id
    except Exception:
        return ""
    return ""


def _fallback_provider_entries(config: dict | None = None) -> List[Dict[str, str]]:
    """Return strict fallback provider entries.

    Expected config shape:
      fallback_providers:
        - provider: qwen
          model: qwen-max

    Non-dictionary entries and entries without an explicit model are ignored so
    fallback never reuses a model from a different provider by accident.
    """
    if not isinstance(config, dict):
        return []
    raw_entries = config.get("fallback_providers")
    if not isinstance(raw_entries, list):
        return []

    entries: List[Dict[str, str]] = []
    for raw in raw_entries:
        if not isinstance(raw, dict):
            continue
        provider_name = str(raw.get("provider") or "").strip()
        model_name = str(raw.get("model") or "").strip()
        if not provider_name or not model_name:
            continue
        entries.append({
            "provider": provider_name,
            "model": model_name,
        })
    return entries


def _configured_model_for_provider(config: dict | None, provider_name: str) -> str:
    """Return the user-recorded model for a provider, if any."""
    if not isinstance(config, dict):
        return ""
    provider_name = str(provider_name or "").strip()
    if not provider_name:
        return ""

    active_provider = str(config.get("active_provider") or "").strip()
    active_model = str(config.get("model") or "").strip()
    if active_provider == provider_name and active_model:
        return active_model

    for entry in _fallback_provider_entries(config):
        if entry.get("provider") == provider_name:
            return str(entry.get("model") or "").strip()
    return ""


def get_provider_status(provider_name: str) -> Dict:
    """Check if a provider has credentials configured.

    Returns: {"configured": bool, "api_key": str, "base_url": str, "display_name": str}
    """
    pcfg = PROVIDER_REGISTRY.get(provider_name)
    if not pcfg:
        return {"configured": False, "api_key": "", "base_url": "", "display_name": provider_name}

    key = resolve_api_key(provider_name)
    url = resolve_base_url(provider_name)
    return {
        "configured": bool(key),
        "api_key": key or "",
        "base_url": url,
        "display_name": pcfg.display_name,
        "key_url": pcfg.key_url,
        "api_mode": pcfg.api_mode,
        "primary_key_env": pcfg.api_key_env_vars[0] if pcfg.api_key_env_vars else "",
    }


def list_configured_providers() -> List[Dict]:
    """Return all providers that have credentials set."""
    result = []
    try:
        from mclaw.cli.config import load_config
        config = load_config()
        user_providers = _configured_providers(config)
    except Exception:
        config = {}
        user_providers = {}

    for pname, pcfg in user_providers.items():
        resolved = _resolve_user_provider(pname, {"providers": user_providers}) or {}
        if resolved.get("api_key"):
            configured_model = _configured_model_for_provider(config, pname) or resolved.get("model", "")
            result.append({
                "name": pname,
                "display_name": str(pcfg.get("display_name") or pname),
                "has_key": True,
                "model": str(configured_model or ""),
                "api_mode": str(pcfg.get("api_mode") or "chat_completions"),
            })

    for pname, pcfg in PROVIDER_REGISTRY.items():
        if pname in user_providers:
            continue
        key = resolve_api_key(pname)
        if key:
            configured_model = _configured_model_for_provider(config, pname)
            result.append({
                "name": pname,
                "display_name": pcfg.display_name,
                "has_key": True,
                "model": configured_model,
                "api_mode": pcfg.api_mode,
            })
    return result


def resolve_provider(
    model: str = "",
    provider: str = "",
    base_url: str = "",
    api_key: str = "",
    config: dict | None = None,
) -> Dict:
    """Resolve the active provider based on config and available keys.

    Returns dict with: provider, model, api_key, base_url, api_mode
    """
    user_providers = _configured_providers(config)
    configured_fallbacks = _fallback_provider_entries(config)

    # Explicit provider has the highest priority.
    # This covers the --provider CLI argument and config.yaml active_provider,
    # which is set by /model --global and only used when the provider has a key.
    if provider and provider in user_providers:
        resolved = _resolve_user_provider(provider, config) or {}
        resolved_key = api_key or resolved.get("api_key", "")
        if resolved_key:
            configured_model = _configured_model_for_provider(config, provider)
            return {
                "provider": provider,
                "model": model or configured_model or resolved.get("model", ""),
                "api_key": resolved_key,
                "base_url": (base_url or resolved.get("base_url", "")).rstrip("/"),
                "api_mode": resolved.get("api_mode", "chat_completions"),
                "provider_profile": "",
            }

    if provider and provider in PROVIDER_REGISTRY:
        resolved_key = api_key or resolve_api_key(provider)
        if resolved_key:
            resolved_url = base_url or resolve_base_url(provider)
            api_mode = PROVIDER_REGISTRY[provider].api_mode
            profile_id = _resolve_config_profile(provider, config)
            configured_model = _configured_model_for_provider(config, provider)
            return {
                "provider": provider,
                "model": model or configured_model,
                "api_key": resolved_key,
                "base_url": resolved_url,
                "api_mode": api_mode,
                "provider_profile": profile_id,
            }

    # --- Custom Anthropic endpoint: MCLAW_ANTHROPIC_BASE_URL ---
    anthropic_custom_url = get_env_value("MCLAW_ANTHROPIC_BASE_URL") or ""
    anthropic_custom_key = api_key or get_env_value("MCLAW_ANTHROPIC_API_KEY") or get_env_value("ANTHROPIC_API_KEY") or ""
    if anthropic_custom_url and not base_url:
        return {
            "provider": "custom_anthropic",
            "model": model,
            "api_key": anthropic_custom_key,
            "base_url": anthropic_custom_url.rstrip("/"),
            "api_mode": "anthropic_messages",
            "provider_profile": "",
        }

    # --- Custom OpenAI endpoint: explicit args or MCLAW_* env vars ---
    custom_url = base_url or get_env_value("MCLAW_BASE_URL") or ""
    custom_key = api_key or get_env_value("MCLAW_API_KEY") or ""

    if custom_url:
        if not custom_key:
            custom_key = get_env_value("OPENAI_API_KEY") or ""
        return {
            "provider": "custom",
            "model": model,
            "api_key": custom_key,
            "base_url": custom_url.rstrip("/"),
            "api_mode": "chat_completions",
            "provider_profile": "",
        }

    # --- OPENAI_BASE_URL override ---
    openai_base_override = get_env_value("OPENAI_BASE_URL")
    if openai_base_override and not provider:
        key = get_env_value("OPENAI_API_KEY") or ""
        return {
            "provider": "custom",
            "model": model,
            "api_key": key,
            "base_url": openai_base_override.rstrip("/"),
            "api_mode": "chat_completions",
            "provider_profile": "",
        }

    # Auto-detect from model name.
    if model:
        detected = detect_provider_for_model(model)
        if detected and detected in user_providers:
            resolved = _resolve_user_provider(detected, config) or {}
            if resolved.get("api_key"):
                return {
                    "provider": detected,
                    "model": model,
                    "api_key": resolved.get("api_key", ""),
                    "base_url": resolved.get("base_url", ""),
                    "api_mode": resolved.get("api_mode", "chat_completions"),
                    "provider_profile": "",
                }
        if detected:
            key = resolve_api_key(detected)
            if key:
                pcfg = PROVIDER_REGISTRY[detected]
                return {
                    "provider": detected,
                    "model": model,
                    "api_key": key,
                    "base_url": resolve_base_url(detected),
                    "api_mode": pcfg.api_mode,
                    "provider_profile": _resolve_config_profile(detected, config),
                }

    # Fallback order: explicit config first, then user providers, then built-ins.
    fallback_order: list[dict[str, str]] = []
    seen_fallbacks: set[str] = set()
    for entry in configured_fallbacks:
        pname = entry["provider"]
        if pname in seen_fallbacks:
            continue
        fallback_order.append(entry)
        seen_fallbacks.add(pname)
    for pname in user_providers:
        if pname not in seen_fallbacks:
            fallback_order.append({"provider": pname, "model": ""})
            seen_fallbacks.add(pname)
    for pname in PROVIDER_REGISTRY:
        if pname not in seen_fallbacks:
            fallback_order.append({"provider": pname, "model": ""})
            seen_fallbacks.add(pname)

    for fallback_entry in fallback_order:
        pname = fallback_entry["provider"]
        configured_model = fallback_entry.get("model", "")
        if pname in user_providers:
            resolved = _resolve_user_provider(pname, config) or {}
            fallback_key = resolved.get("api_key", "")
            if fallback_key:
                return {
                    "provider": pname,
                    "model": configured_model or resolved.get("model", ""),
                    "api_key": fallback_key,
                    "base_url": resolved.get("base_url", ""),
                    "api_mode": resolved.get("api_mode", "chat_completions"),
                    "provider_profile": "",
                }
            continue

        pcfg = PROVIDER_REGISTRY.get(pname)
        if not pcfg:
            continue
        fallback_key = resolve_api_key(pname)
        if fallback_key and configured_model:
            return {
                "provider": pname,
                "model": configured_model,
                "api_key": fallback_key,
                "base_url": resolve_base_url(pname),
                "api_mode": pcfg.api_mode,
                "provider_profile": _resolve_config_profile(pname, config),
            }

    return {
        "provider": "",
        "model": model,
        "api_key": "",
        "base_url": "",
        "api_mode": "chat_completions",
        "provider_profile": "",
    }
