# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Standalone provider management using the shared registry and configuration."""

from __future__ import annotations

import copy
import hashlib
import os
import tempfile
import threading
from pathlib import Path

from mclaw.application.sessions import ApplicationError
from mclaw.cli import config as config_io
from mclaw.cli.provider_profiles import (
    CORE_PROVIDER_KEYS, find_provider_profile, get_provider_profiles,
    get_setup_profile_label, get_setup_provider_label,
)
from mclaw.providers.registry import PROVIDER_REGISTRY
from mclaw.providers.normalization import normalize_provider_key
from mclaw.providers.resolver import (
    ProviderResolutionError, _CUSTOM_ENV, _validate_base_url,
    resolve_provider_runtime_context,
)
from mclaw.system.lock import file_lock

_ALIASES = {
    "qwen": "阿里 通义 千问 阿里云", "deepseek": "深度求索", "moonshot": "月之暗面",
    "zhipu": "智谱 清言 GLM", "doubao": "豆包 火山 字节", "baidu": "百度 文心",
    "tencent": "腾讯 混元", "xiaomi": "小米 MiMo", "minimax": "海螺 稀宇",
    "google": "谷歌 Gemini", "anthropic": "Claude", "xai": "Grok",
    "siliconflow": "硅基流动", "stepfun": "阶跃星辰", "yi": "零一万物",
}


def _active_provider(config: dict) -> str:
    active = normalize_provider_key(str(config.get("active_provider") or ""), config.get("providers") or {})
    if active in PROVIDER_REGISTRY and config.get("active_provider_profile"):
        profile = find_provider_profile(active, str(config["active_provider_profile"]))
        if profile and profile.callable:
            return profile.runtime_provider
    return active


def _model(config: dict, provider: str) -> str:
    if _active_provider(config) == provider:
        return str(config.get("model") or "")
    for entry in config.get("fallback_providers", []):
        if isinstance(entry, dict) and entry.get("provider") == provider:
            return str(entry.get("model") or "")
    return str(((config.get("providers") or {}).get(provider) or {}).get("model") or "")


def _endpoint(value: str) -> str:
    try:
        return _validate_base_url(value, provider="") if value else ""
    except ProviderResolutionError:
        return ""


def _restore(path: Path, before: bytes | None) -> None:
    if before is None:
        path.unlink(missing_ok=True)
        return
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".provider-", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(before)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        config_io._secure_file(path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class ProviderService:
    def __init__(self, home: Path) -> None:
        from mclaw.application.model_library import ModelLibraryService

        self.home = Path(home).resolve()
        self._probes = threading.BoundedSemaphore(2)
        self.library = ModelLibraryService(self.home)

    def _config(self) -> dict:
        from mclaw.constants import get_mclaw_home

        if get_mclaw_home().resolve() != self.home:
            raise ApplicationError("CONFIG_ROOT_MISMATCH", "配置目录与本机服务不一致", 409)
        try:
            config = config_io.load_raw_user_config(strict=True)
            providers = config.get("providers") or {}
            if not isinstance(providers, dict) or any(not isinstance(value, dict) for value in providers.values()):
                raise config_io.ConfigError("Invalid providers")
            if not isinstance(config.get("fallback_providers", []), list):
                raise config_io.ConfigError("Invalid fallback providers")
            return config
        except (config_io.ConfigError, OSError):
            raise ApplicationError("CONFIG_INVALID", "现有配置无法读取，请检查配置文件。原文件已保留。", 409) from None

    def _revision(self) -> str:
        digest = hashlib.sha256()
        for name in ("config.yaml", ".env"):
            path = self.home / name
            digest.update(name.encode())
            digest.update(path.read_bytes() if path.exists() else b"")
        return digest.hexdigest()

    @staticmethod
    def _environment() -> dict[str, str]:
        # Match get_env_value precedence while reading the local file only once.
        values = config_io.load_env()
        values.update({key: value for key, value in os.environ.items() if value})
        return values

    def catalog(self) -> dict:
        with file_lock(self.home / "config.lock"):
            return self._catalog()

    def _catalog(self) -> dict:
        from mclaw.agent import models_dev

        config = self._config()
        environment = self._environment()
        model_registry = models_dev.cached_models_dev()
        common = [name for name in CORE_PROVIDER_KEYS if name in PROVIDER_REGISTRY]
        represented = {p.runtime_provider for name in common for p in get_provider_profiles(name)}
        brands = common + [name for name in PROVIDER_REGISTRY
                           if name not in common and name not in represented and name not in _CUSTOM_ENV]
        catalog, configured = [], []
        active = _active_provider(config)
        for brand in brands:
            profiles = []
            for entry in get_provider_profiles(brand):
                if not entry.callable or entry.runtime_provider not in PROVIDER_REGISTRY:
                    continue
                target = PROVIDER_REGISTRY[entry.runtime_provider]
                saved_model = _model(config, target.name)
                has_key = any(environment.get(key) for key in target.env_vars)
                cached = models_dev.list_provider_models(brand, profile_id=entry.id, limit=None, registry=model_registry)
                models = list(dict.fromkeys([saved_model, *cached, *target.fallback_models]))
                profiles.append({
                    "id": entry.id, "label": get_setup_profile_label(entry), "runtime_provider": target.name,
                    "api_mode": target.api_mode, "base_url": _endpoint(environment.get(target.base_url_env_var, "") or target.base_url),
                    "base_url_editable": bool(target.base_url_env_var or target.base_url_required),
                    "credential_configured": has_key, "key_url": target.key_url,
                    "model": saved_model or next(iter(target.fallback_models), ""),
                    "models": [model for model in models if model],
                })
                if has_key or saved_model:
                    configured.append({"provider": brand, "profile": entry.id, "label": get_setup_provider_label(brand),
                                       "region": get_setup_profile_label(entry), "runtime_provider": target.name,
                                       "model": saved_model or next(iter(target.fallback_models), ""),
                                       "credential_configured": has_key, "is_default": target.name == active})
            if profiles:
                catalog.append({"id": brand, "label": get_setup_provider_label(brand),
                                "keywords": " ".join([brand, _ALIASES.get(brand, ""), *PROVIDER_REGISTRY[brand].aliases]),
                                "profiles": profiles})
        custom = dict(config.get("providers") or {})
        for name in _CUSTOM_ENV:
            target = PROVIDER_REGISTRY[name]
            custom[name] = {"display_name": "自定义 Anthropic 接口" if name.endswith("anthropic") else "自定义 OpenAI 接口",
                            "api_mode": target.api_mode,
                            "base_url": next((environment[key] for key in target.base_url_env_vars if environment.get(key)), ""),
                            "api_key_env": next((key for key in target.env_vars if environment.get(key)), target.env_vars[0])}
        for name, raw in custom.items():
            has_key = bool(environment.get(raw.get("api_key_env", "")))
            saved_model = _model(config, name)
            base_url = _endpoint(environment.get(raw.get("base_url_env", ""), "") or raw.get("base_url", ""))
            label = str(raw.get("display_name") or name)
            profile = {"id": "api", "label": "兼容接口", "runtime_provider": name,
                       "api_mode": raw.get("api_mode", "chat_completions"), "base_url": base_url,
                       "base_url_editable": True, "credential_configured": has_key, "key_url": "",
                       "model": saved_model, "models": [saved_model] if saved_model else []}
            catalog.append({"id": name, "label": label, "keywords": f"{name} 自定义 兼容 本地 私有 网关", "profiles": [profile]})
            if saved_model or (has_key and base_url) or name in (config.get("providers") or {}):
                configured.append({"provider": name, "profile": "api", "label": label, "region": "兼容接口",
                                   "runtime_provider": name, "model": saved_model,
                                   "credential_configured": has_key, "is_default": name == active})
        return {"catalog": catalog, "configured": configured, "revision": self._revision(),
                "library": self.library.status()}

    def _prepare(self, data: dict, config: dict, *, probe: bool = False):
        for key in ("provider", "profile", "model", "api_key", "base_url"):
            if not isinstance(data.get(key, ""), str) or len(data.get(key, "")) > (8192 if key == "api_key" else 2048):
                raise ApplicationError("INVALID_PROVIDER", "供应商配置格式不正确", 422)
        provider, profile_id = data.get("provider", "").strip(), data.get("profile", "").strip()
        model, key, base = (data.get(name, "").strip() for name in ("model", "api_key", "base_url"))
        if any(char in key for char in "\r\n"):
            raise ApplicationError("INVALID_PROVIDER_KEY", "API Key 不能包含换行", 422)
        key = config_io._sanitize_api_key(key)
        if not model and not probe:
            raise ApplicationError("MODEL_REQUIRED", "请选择或填写模型名称", 422)
        environment = self._environment()
        writes = {}
        if provider in _CUSTOM_ENV:
            target = PROVIDER_REGISTRY[provider]
            key_env, key_aliases = target.env_vars[0], target.env_vars
            base = base or next((environment[name] for name in target.base_url_env_vars if environment.get(name)), "")
            writes[target.base_url_env_vars[0]] = base
            runtime = provider
        elif provider in PROVIDER_REGISTRY:
            entry = find_provider_profile(provider, profile_id)
            if not entry or not entry.callable or entry.runtime_provider not in PROVIDER_REGISTRY:
                raise ApplicationError("INVALID_PROVIDER_PROFILE", "请选择支持调用的 API 区域或接口", 422)
            target = PROVIDER_REGISTRY[entry.runtime_provider]
            if base and base != (environment.get(target.base_url_env_var) or target.base_url) and not target.base_url_env_var:
                raise ApplicationError("PROVIDER_ENDPOINT_FIXED", "修改服务地址时，请使用自定义兼容接口", 422)
            base = base or environment.get(target.base_url_env_var, "") or target.base_url
            key_env, key_aliases = target.env_vars[0], target.env_vars
            if target.base_url_env_var:
                writes[target.base_url_env_var] = base
            runtime = target.name
            selected_profiles = config.setdefault("setup_provider_profiles", {})
            if not isinstance(selected_profiles, dict):
                raise ApplicationError("CONFIG_INVALID", "已保存的 API 区域配置格式不正确", 409)
            selected_profiles[provider] = entry.id
        elif provider in (config.get("providers") or {}):
            raw = config["providers"][provider]
            key_env, key_aliases = raw.get("api_key_env", ""), (raw.get("api_key_env", ""),)
            base = base or environment.get(raw.get("base_url_env", ""), "") or raw.get("base_url", "")
            raw["base_url"] = base
            if raw.get("base_url_env"):
                writes[raw["base_url_env"]] = base
            if model:
                raw["model"] = model
            runtime = provider
        else:
            raise ApplicationError("PROVIDER_NOT_FOUND", "找不到这个供应商", 404)
        resolved_key = key or next((environment[name] for name in key_aliases if environment.get(name)), "")
        try:
            context = resolve_provider_runtime_context(provider=runtime, model=model or "connection-check",
                                                       base_url=base, api_key=resolved_key, config=config)
        except ProviderResolutionError as exc:
            messages = {"missing_credential": "请填写 API Key，或使用已保存的凭据", "invalid_base_url": "请填写有效的服务地址，不要包含密钥、查询参数或片段"}
            raise ApplicationError("INVALID_PROVIDER", messages.get(exc.code, "请检查供应商、接口地址和模型配置"), 422) from None
        if key:
            writes[key_env] = key
        return context, writes

    def models(self, data: dict) -> dict:
        from mclaw.agent.context_metadata import probe_provider_models

        if not self._probes.acquire(blocking=False):
            raise ApplicationError("PROVIDER_PROBE_BUSY", "正在获取模型，请稍后再试", 429)
        try:
            context, _writes = self._prepare(data, self._config(), probe=True)
            try:
                models = probe_provider_models(context.profile, base_url=context.base_url, api_key=context.api_key)
            except Exception:
                raise ApplicationError("PROVIDER_PROBE_FAILED", "无法获取模型，请稍后重试，或直接填写模型名称。", 422) from None
            if models is None:
                raise ApplicationError("PROVIDER_PROBE_FAILED", "无法获取模型，请检查 API Key 和服务地址，也可以直接填写模型名称。", 422)
            return {"models": list(dict.fromkeys(models))[:2000]}
        finally:
            self._probes.release()

    def save(self, data: dict) -> dict:
        from mclaw.application.setup import _WORKFLOW_LOCK

        if not isinstance(data.get("make_default", False), bool):
            raise ApplicationError("INVALID_PROVIDER", "默认模型选项格式不正确", 422)
        if not _WORKFLOW_LOCK.acquire(blocking=False):
            raise ApplicationError("SETUP_IN_PROGRESS", "请先完成或退出正在进行的设置向导", 409)
        try:
            with file_lock(self.home / "config.lock"):
                config = self._config()
                if data.get("revision") != self._revision():
                    raise ApplicationError("PROVIDER_CONFLICT", "配置已在其他窗口更新，请重新打开此供应商后保存。", 409)
                candidate = copy.deepcopy(config)
                context, writes = self._prepare(data, candidate)
                config_io.upsert_fallback_provider_model(candidate, context.provider, context.model)
                if data.get("make_default") or not config.get("active_provider") or _active_provider(config) == context.provider:
                    candidate.update(active_provider=context.provider, active_provider_profile="", model=context.model)
                paths = [self.home / "config.yaml", self.home / ".env"]
                with file_lock(self.home / ".env.lock"):
                    previous = {path: path.read_bytes() if path.exists() else None for path in paths}
                    old_environment = {name: os.environ.get(name) for name in writes}
                    try:
                        for name, value in writes.items():
                            config_io._save_env_value_unlocked(name, value)
                        config_io.save_config(candidate)
                    except Exception:
                        try:
                            for path, content in previous.items():
                                _restore(path, content)
                        finally:
                            for name, value in old_environment.items():
                                if value is None:
                                    os.environ.pop(name, None)
                                else:
                                    os.environ[name] = value
                        raise ApplicationError("PROVIDER_SAVE_FAILED", "保存失败，已恢复原有配置，请检查文件权限后重试。", 409) from None
        finally:
            _WORKFLOW_LOCK.release()
        return self.catalog()
