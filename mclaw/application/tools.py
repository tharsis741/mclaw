# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tool switches and scoped credentials shared by Web, channels and TUI."""

from __future__ import annotations

import copy
import hashlib
import os
from pathlib import Path

from mclaw.application.providers import _restore
from mclaw.application.sessions import ApplicationError
from mclaw.cli import config as config_io
from mclaw.providers.registry import get_runtime_profile
from mclaw.runtime import secrets
from mclaw.system.lock import file_lock
from mclaw.tools.toolsets import REQUIRED_TOOLSETS, TOOLSETS, resolve_multiple_toolsets

_LABELS = {
    "credentials": ("凭据授权", "按任务需要申请使用 API Key"),
    "terminal": ("终端", "执行命令，管理后台进程"),
    "file": ("文件", "读取、编辑和整理工作区文件"),
    "memory": ("记忆", "保存和更新长期记忆"),
    "skills": ("技能", "查找、读取和管理技能"),
    "session_search": ("会话检索", "查找历史对话中的信息"),
    "delegation": ("子代理", "创建子代理，协作处理复杂任务"),
    "web_search": ("网页搜索", "搜索最新资料和信息"),
    "web_extract": ("网页读取", "提取网页正文"),
    "vision": ("图片理解", "识别图片内容，分析截图"),
    "browser": ("浏览器", "访问网页、填写表单和下载文件"),
    "weixin": ("微信文件发送", "向当前微信对话发送任务文件"),
    "dingtalk": ("钉钉文件发送", "向当前钉钉对话发送任务文件"),
}


def _group(identifier: str) -> str:
    return "web" if identifier in {"web_search", "web_extract"} else identifier


def _names(identifier: str) -> list[str]:
    return [identifier] if identifier in {"web_search", "web_extract"} else TOOLSETS[identifier]["tools"]


def _backends(identifier: str) -> list[dict]:
    cn, intl = get_runtime_profile("qwen"), get_runtime_profile("qwen-intl")
    qwen = [{"id": "dashscope", "label": "通义千问 · 中国区", "credentials": list(cn.env_vars)},
            {"id": "dashscope-intl", "label": "通义千问 · 国际区", "credentials": list(intl.env_vars)}]
    if identifier == "web_search":
        return [{"id": "auto", "label": "自动选择", "credentials": list(dict.fromkeys(["TAVILY_API_KEY", *cn.env_vars, *intl.env_vars]))},
                {"id": "tavily", "label": "Tavily", "credentials": ["TAVILY_API_KEY"]}, *qwen]
    if identifier == "web_extract":
        return [{"id": "trafilatura", "label": "本地提取", "credentials": []},
                {"id": "tavily", "label": "Tavily", "credentials": ["TAVILY_API_KEY"]},
                {"id": "firecrawl", "label": "Firecrawl", "credentials": ["FIRECRAWL_API_KEY"]}]
    if identifier == "vision":
        return qwen
    return []


def _scope(identifier: str) -> str:
    return "tool:vision_analyze" if identifier == "vision" else f"tool:{identifier}"


def _selection(identifier: str, config: dict) -> tuple[dict, str]:
    auxiliary = (config.get("auxiliary") or {}).get(identifier) or {}
    endpoint = str(auxiliary.get("base_url") or "").lower()
    if identifier == "vision":
        selected = "dashscope-intl" if auxiliary.get("provider") == "qwen-intl" or "dashscope-intl" in endpoint else "dashscope"
    else:
        selected = str(auxiliary.get("backend") or ("trafilatura" if identifier == "web_extract" else "auto")).lower()
        if selected == "dashscope" and "dashscope-intl" in endpoint:
            selected = "dashscope-intl"
    return auxiliary, selected


class ToolService:
    def __init__(self, home: Path) -> None:
        self.home = Path(home).resolve()

    def _config(self) -> dict:
        from mclaw.constants import get_mclaw_home

        if get_mclaw_home().resolve() != self.home:
            raise ApplicationError("CONFIG_ROOT_MISMATCH", "配置目录与本机服务不一致", 409)
        try:
            raw = config_io.load_raw_user_config(strict=True)
            for key in ("tools", "auxiliary", "channels"):
                if not isinstance(raw.get(key, {}), dict):
                    raise ValueError(key)
            for value in (raw.get("toolsets", []), raw.get("tools", {}).get("disabled", [])):
                if not isinstance(value, list) or any(not isinstance(name, str) for name in value):
                    raise ValueError("tools")
            for channel in ("weixin", "dingtalk"):
                value = raw.get("channels", {}).get(channel, {})
                if not isinstance(value, dict) or not isinstance(value.get("toolsets", []), list) or any(not isinstance(name, str) for name in value.get("toolsets", [])):
                    raise ValueError("channel tools")
            for value in raw.get("auxiliary", {}).values():
                if not isinstance(value, dict):
                    raise ValueError("auxiliary")
            return raw
        except (config_io.ConfigError, ValueError, TypeError, OSError):
            raise ApplicationError("CONFIG_INVALID", "工具配置无法读取，请检查配置文件。", 409) from None

    def _revision(self) -> str:
        digest = hashlib.sha256()
        for name in ("config.yaml", ".env", secrets.ALLOWLIST_FILENAME):
            path = self.home / name
            digest.update(name.encode())
            digest.update(path.read_bytes() if path.exists() else b"")
        return digest.hexdigest()

    @staticmethod
    def _environment() -> dict:
        return {**config_io.load_env(), **{key: value for key, value in os.environ.items() if value}}

    def catalog(self) -> dict:
        with file_lock(self.home / "config.lock"):
            config, env = self._config(), self._environment()
            active = resolve_multiple_toolsets(config.get("toolsets", ["mclaw-required"])) - set(config.get("tools", {}).get("disabled", []))
            items = []
            for identifier, (label, description) in _LABELS.items():
                required = identifier in REQUIRED_TOOLSETS
                auxiliary, selected = _selection(identifier, config)
                backends = _backends(identifier)
                for backend in backends:
                    backend["credentials"] = [{"id": name, "configured": bool(env.get(name))} for name in backend["credentials"]]
                backend = next((item for item in backends if item["id"] == selected), None)
                choices = backend["credentials"] if backend else []
                preferred = auxiliary.get("credential_env", "")
                available = [item["id"] for item in choices if item["configured"]]
                credential = preferred if preferred in available else next(iter(available), "")
                ready = not backends or bool(backend and (not choices or any(secrets.is_authorized(_scope(identifier), name) for name in available)))
                if identifier == "web_search":
                    from mclaw.tools.search.credentials import dashscope_creds_ok, tavily_creds_ok
                    ready = (selected in {"auto", "tavily"} and tavily_creds_ok()) or (selected in {"auto", "dashscope", "dashscope-intl"} and dashscope_creds_ok(config=config))
                elif identifier == "vision":
                    from mclaw.tools.vision.credentials import resolve_vision_credentials
                    ready = bool(resolve_vision_credentials(config=config).api_key)
                items.append({"id": identifier, "label": label, "description": description,
                              "required": required, "enabled": required or bool(set(_names(identifier)) & active),
                              "ready": ready, "tools": _names(identifier), "backends": backends,
                              "backend": selected if backends else "", "credential_source": credential,
                              "model": str(auxiliary.get("model") or ("qwen3-vl-flash" if identifier == "vision" else "")),
                              "channel": identifier if identifier in {"weixin", "dingtalk"} else ""})
            return {"items": items, "revision": self._revision()}

    def save(self, identifier: str, data: dict) -> dict:
        from mclaw.application.setup import _WORKFLOW_LOCK

        if identifier not in _LABELS:
            raise ApplicationError("TOOL_NOT_FOUND", "工具不存在", 404)
        if set(data) - {"revision", "enabled", "backend", "credential_source", "api_key", "model"}:
            raise ApplicationError("INVALID_TOOL", "工具配置包含不支持的选项", 422)
        if type(data.get("enabled")) is not bool:
            raise ApplicationError("INVALID_TOOL", "请选择是否启用工具", 422)
        if identifier in REQUIRED_TOOLSETS and not data["enabled"]:
            raise ApplicationError("TOOL_REQUIRED", "基础能力需要保持开启", 422)
        if not _WORKFLOW_LOCK.acquire(blocking=False):
            raise ApplicationError("SETUP_IN_PROGRESS", "请先完成或退出设置向导", 409)
        try:
            with file_lock(self.home / "config.lock"), file_lock(self.home / ".env.lock"), file_lock(secrets.allowlist_path().with_suffix(".lock")):
                candidate = copy.deepcopy(self._config())
                if data.get("revision") != self._revision():
                    raise ApplicationError("TOOLS_CONFLICT", "配置已更新，请重新打开工具后保存。", 409)
                writes, grants = self._prepare(identifier, data, candidate)
                paths = [self.home / name for name in ("config.yaml", ".env", secrets.ALLOWLIST_FILENAME)]
                previous = {path: path.read_bytes() if path.exists() else None for path in paths}
                environment = {name: os.environ.get(name) for name in writes}
                try:
                    for name, value in writes.items():
                        config_io._save_env_value_unlocked(name, value)
                    if grants:
                        secrets._authorize_unlocked(_scope(identifier), grants)
                    config_io.save_config(candidate)
                except Exception:
                    try:
                        for path, content in previous.items():
                            _restore(path, content)
                    finally:
                        for name, value in environment.items():
                            if value is None:
                                os.environ.pop(name, None)
                            else:
                                os.environ[name] = value
                    raise ApplicationError("TOOL_SAVE_FAILED", "保存失败，已恢复原有配置。", 409) from None
        finally:
            _WORKFLOW_LOCK.release()
        return self.catalog()

    def _prepare(self, identifier: str, data: dict, config: dict) -> tuple[dict, list]:
        writes, grants = {}, []
        auxiliary, current = _selection(identifier, config)
        backends = _backends(identifier)
        if backends and (data["enabled"] or any(key in data for key in ("backend", "api_key", "credential_source", "model"))):
            selected = data.get("backend", current)
            backend = next((item for item in backends if item["id"] == selected), None)
            if backend is None:
                raise ApplicationError("INVALID_TOOL_BACKEND", "请选择支持的服务来源", 422)
            auxiliary = config.setdefault("auxiliary", {}).setdefault(identifier, {})
            env = self._environment()
            allowed = backend["credentials"]
            preferred = auxiliary.get("credential_env")
            name = data.get("credential_source") or (preferred if preferred in allowed else "") or next((key for key in allowed if env.get(key)), next(iter(allowed), ""))
            key = data.get("api_key", "")
            if not isinstance(key, str) or len(key) > 8192 or any(char in key for char in "\r\n\0"):
                raise ApplicationError("INVALID_TOOL_KEY", "API Key 格式不正确", 422)
            key = key.strip()
            if allowed:
                if name not in allowed:
                    raise ApplicationError("INVALID_CREDENTIAL_SOURCE", "该凭据不适用于所选服务", 422)
                if key:
                    writes[name] = key
                if not (key or env.get(name)):
                    raise ApplicationError("TOOL_CREDENTIAL_REQUIRED", "请填写 API Key 或选择已保存的凭据", 422)
                grants.append(name)
            elif key or data.get("credential_source"):
                raise ApplicationError("INVALID_TOOL_KEY", "此服务不需要 API Key", 422)
            if identifier == "vision" or selected.startswith("dashscope") or (selected == "auto" and name != "TAVILY_API_KEY"):
                provider = "qwen-intl" if selected == "dashscope-intl" or (selected == "auto" and name in get_runtime_profile("qwen-intl").env_vars and name not in get_runtime_profile("qwen").env_vars) else "qwen"
                profile = get_runtime_profile(provider)
                # Preserve an existing endpoint when editing only the switch/model.
                if "backend" in data or not auxiliary.get("base_url"):
                    auxiliary["base_url"] = profile.base_url
                auxiliary["credential_env"] = name
                if identifier == "vision":
                    auxiliary["provider"] = provider
            if identifier != "vision":
                auxiliary["backend"] = "dashscope" if selected == "dashscope-intl" else selected
                if identifier == "web_extract" and "backend" in data:
                    auxiliary["backends"] = [selected]
            if "model" in data:
                model = data["model"]
                if not isinstance(model, str) or len(model) > 200 or any(char in model for char in "\r\n\0"):
                    raise ApplicationError("INVALID_TOOL_MODEL", "模型名称格式不正确", 422)
                if identifier not in {"vision", "web_search"}:
                    raise ApplicationError("INVALID_TOOL_MODEL", "此工具不使用模型", 422)
                auxiliary["model"] = model.strip() or ("qwen3-vl-flash" if identifier == "vision" else "")
        elif not backends and set(data) & {"backend", "api_key", "credential_source", "model"}:
            raise ApplicationError("INVALID_TOOL", "此工具无需配置模型或凭据", 422)

        toolsets = config.setdefault("toolsets", ["mclaw-required"])
        disabled = config.setdefault("tools", {}).setdefault("disabled", [])
        baseline = resolve_multiple_toolsets(REQUIRED_TOOLSETS)
        disabled[:] = [name for name in disabled if name not in baseline]
        if "mclaw-required" not in toolsets and "all" not in toolsets:
            toolsets.append("mclaw-required")
        names, group = _names(identifier), _group(identifier)
        if data["enabled"]:
            previous = resolve_multiple_toolsets(toolsets)
            if not set(names).issubset(previous):
                toolsets.append(group)
                disabled.extend(name for name in TOOLSETS[group]["tools"] if name not in previous and name not in names and name not in disabled)
            disabled[:] = [name for name in disabled if name not in names]
            # Existing IM runners reload these explicit selections at the next task.
            for channel in ("weixin", "dingtalk"):
                if identifier in {"weixin", "dingtalk"} and identifier != channel:
                    continue
                cfg = config.get("channels", {}).get(channel)
                if isinstance(cfg, dict):
                    selection = cfg.setdefault("toolsets", ["mclaw-required"])
                    if not set(names).issubset(resolve_multiple_toolsets(selection)):
                        selection.append(group)
        else:
            disabled.extend(name for name in names if name not in disabled)
        return writes, grants
