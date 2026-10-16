# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared Web/TUI-facing vision configuration and capability status."""

from __future__ import annotations

import uuid

from mclaw.application.sessions import ApplicationError
from mclaw.providers.capabilities import capability_key, resolve_image_capability
from mclaw.providers.registry import PROVIDER_REGISTRY
from mclaw.providers.resolver import resolve_provider_runtime_context
from mclaw.tools.vision.config import resolve_mode
from mclaw.tools.vision.credentials import diagnose_vision_credentials


def vision_backends(config: dict) -> list[dict]:
    result = []
    for name, profile in PROVIDER_REGISTRY.items():
        if profile.api_mode not in {"chat_completions", "anthropic_messages"}:
            continue
        identifier = {"qwen": "dashscope", "qwen-intl": "dashscope-intl"}.get(name, name)
        result.append({"id": identifier, "label": profile.display_name, "credentials": list(profile.env_vars)})
    for name, provider in config.get("providers", {}).items():
        if name not in PROVIDER_REGISTRY and name not in {"custom", "custom_anthropic"} and isinstance(provider, dict) and isinstance(provider.get("api_key_env"), str):
            result.append({"id": name, "label": provider.get("display_name") or name, "credentials": [provider["api_key_env"]]})
    result.extend([
        {"id": "custom", "label": "自定义 OpenAI 兼容接口", "credentials": ["MCLAW_API_KEY", "OPENAI_API_KEY"]},
        {"id": "custom_anthropic", "label": "自定义 Anthropic 兼容接口", "credentials": ["MCLAW_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"]},
    ])
    return sorted(result, key=lambda entry: (entry["id"] not in {"dashscope", "dashscope-intl"}, entry["label"]))


def vision_status(config: dict) -> dict:
    section = config.get("auxiliary", {}).get("vision", {})
    result = {"mode": resolve_mode(config=config), "base_url": section.get("base_url", ""),
              "image_input": section.get("image_input", "auto"),
              "catalog_provider": section.get("catalog_provider", ""), "catalog_model": section.get("catalog_model", ""),
              "main_image_input": "auto", "main_model": "", "main_capability": {"state": "unknown", "source": "unknown"}}
    try:
        runtime = resolve_provider_runtime_context(config=config)
        result["main_model"] = f"{runtime.provider}/{runtime.model}"
        result["main_capability"] = resolve_image_capability(runtime, config).to_dict()
        result["main_image_input"] = config.get("model_capabilities", {}).get(capability_key(runtime), {}).get("image_input", "auto")
    except ValueError:
        pass
    diagnosis = diagnose_vision_credentials(config)
    result.update(ready=diagnosis["available"], reason=diagnosis["reason"])
    return result


def prepare_vision_settings(data: dict, config: dict, environment: dict) -> tuple[dict, list]:
    section = config.setdefault("auxiliary", {}).setdefault("vision", {})
    mode = data.get("mode", resolve_mode(config=config))
    if not isinstance(mode, str) or mode not in {"main", "auto", "auxiliary"}:
        raise ApplicationError("INVALID_TOOL", "请选择自动、当前模型或专用视觉模型", 422)
    section["mode"] = mode
    for key in ("image_input", "main_image_input"):
        if key in data and (not isinstance(data[key], str) or data[key] not in {"auto", "enabled", "disabled"}):
            raise ApplicationError("INVALID_TOOL", "图片能力设置不正确", 422)
    if "main_image_input" in data:
        try:
            runtime = resolve_provider_runtime_context(config=config)
        except ValueError:
            if data["main_image_input"] != "auto":
                raise ApplicationError("INVALID_TOOL", "请先在模型设置中配置默认模型", 422) from None
        else:
            config.setdefault("model_capabilities", {}).setdefault(capability_key(runtime), {}).update(
                image_input=data["main_image_input"], revision=uuid.uuid4().hex,
            )
    writes, grants = {}, []
    if mode == "main" and not any(key in data for key in ("backend", "model", "base_url", "api_key", "credential_source")):
        return writes, grants
    previous_provider = section.get("provider", "qwen")
    if previous_provider == "auto":
        previous_provider = "qwen-intl" if "dashscope-intl" in section.get("base_url", "") else "qwen"
    previous_backend = {"qwen": "dashscope", "qwen-intl": "dashscope-intl"}.get(previous_provider, previous_provider)
    backend_id = data.get("backend", previous_backend)
    backend = next((item for item in vision_backends(config) if item["id"] == backend_id), None)
    if backend is None:
        raise ApplicationError("INVALID_TOOL_BACKEND", "请选择支持的视觉供应商", 422)
    provider = {"dashscope": "qwen", "dashscope-intl": "qwen-intl"}.get(backend_id, backend_id)
    changed = provider != previous_provider
    section["provider"] = provider
    if changed:
        for key in ("credential_env", "base_url", "model", "image_input", "catalog_provider", "catalog_model"):
            section.pop(key, None)
    elif any(isinstance(data.get(key), str) and data[key].strip() != section.get(key, "") for key in ("model", "base_url")):
        # A manual declaration or alias for one target must not leak to another.
        for key in ("image_input", "catalog_provider", "catalog_model"):
            section.pop(key, None)
    for key in ("model", "base_url", "image_input", "catalog_provider", "catalog_model"):
        if key in data:
            value = data[key]
            if not isinstance(value, str) or len(value) > (2000 if key == "base_url" else 200) or any(char in value for char in "\r\n\0"):
                raise ApplicationError("INVALID_TOOL", "视觉配置格式不正确", 422)
            section[key] = value.strip()
    if not section.get("model") and provider in {"qwen", "qwen-intl"}:
        section["model"] = "qwen3-vl-flash"
    if section.get("base_url"):
        from mclaw.providers.resolver import _validate_base_url

        try:
            _validate_base_url(section["base_url"], provider=provider)
        except ValueError:
            raise ApplicationError("INVALID_TOOL_ENDPOINT", "接口地址需要有效的 HTTP/HTTPS URL，且不能包含凭据、查询参数或片段", 422) from None
    choices = backend["credentials"]
    if "credential_source" in data and not isinstance(data["credential_source"], str):
        raise ApplicationError("INVALID_CREDENTIAL_SOURCE", "请选择有效的凭据来源", 422)
    selected = data.get("credential_source") or section.get("credential_env") or next((name for name in choices if environment.get(name)), next(iter(choices), ""))
    key = data.get("api_key", "")
    if not isinstance(key, str) or len(key) > 8192 or any(char in key for char in "\r\n\0"):
        raise ApplicationError("INVALID_TOOL_KEY", "API Key 格式不正确", 422)
    key = key.strip()
    if selected and selected not in choices:
        raise ApplicationError("INVALID_CREDENTIAL_SOURCE", "该凭据不适用于所选服务", 422)
    if key or environment.get(selected):
        if key:
            writes[selected] = key
        grants.append(selected)
        section["credential_env"] = selected
    elif mode == "auxiliary" and data["enabled"] and choices:
        # Local custom endpoints are allowed to run without an API credential.
        from mclaw.providers.resolver import _is_local_url

        if provider not in {"custom", "custom_anthropic"} or not _is_local_url(section.get("base_url", "")):
            raise ApplicationError("TOOL_CREDENTIAL_REQUIRED", "请填写 API Key 或选择已保存的凭据", 422)
    if mode == "auxiliary" and data["enabled"] and not section.get("model"):
        raise ApplicationError("INVALID_TOOL_MODEL", "请填写视觉模型名称", 422)
    if section.get("model"):
        try:
            runtime = resolve_provider_runtime_context(
                provider=provider, model=section["model"], base_url=section.get("base_url", ""),
                api_key="vision-metadata-only", config=config, reasoning_config={"enabled": False},
            )
        except ValueError:
            if mode == "auxiliary" and data["enabled"]:
                raise ApplicationError("INVALID_TOOL_ENDPOINT", "请检查专用视觉模型的供应商和接口地址", 422) from None
        else:
            if set(data) & {"image_input", "catalog_provider", "catalog_model"}:
                config.setdefault("model_capabilities", {}).setdefault(capability_key(runtime), {})["revision"] = uuid.uuid4().hex
    return writes, grants
