# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Scoped, revision-checked editing of the shared user configuration."""

from __future__ import annotations

import copy
import hashlib
import math
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from mclaw.application.sessions import ApplicationError
from mclaw.cli import config as config_io
from mclaw.system.lock import file_lock


def field(path: str, label: str, kind: str = "boolean", *, description: str = "", advanced: bool = False,
          minimum: float | None = None, maximum: float | None = None, step: float | None = None,
          options: list[dict] | None = None) -> dict:
    return {"path": path, "label": label, "kind": kind, "description": description,
            "advanced": advanced, "minimum": minimum, "maximum": maximum,
            "step": step, "options": options or []}


def number(path: str, label: str, minimum: int, maximum: int, **options) -> dict:
    return field(path, label, "number", minimum=minimum, maximum=maximum, step=1, **options)


GROUPS = [
    {"id": "model", "page": "general", "title": "模型行为", "effect": "后续任务生效",
     "fields": [
         field("reasoning.effort", "思考强度", "select", description="由模型支持的档位决定；默认使用模型自身设置。",
               options=[{"value": value, "label": label} for value, label in
                        [("", "跟随模型"), ("none", "关闭"), ("minimal", "最低"), ("low", "低"),
                         ("medium", "中"), ("high", "高"), ("xhigh", "更高"), ("max", "最高")]]),
         field("prompt_cache.enabled", "提示词缓存", description="供应商支持时，复用重复上下文以减少开销。"),
         number("agent.first_response_timeout", "首段回复等待时间（秒）", 1, 600, advanced=True,
                description="开始请求到收到首段内容的最长等待时间；非流式请求等待完整回复。私有服务响应较慢时可适当增大。"),
         number("agent.stream_idle_timeout", "回复中断等待时间（秒）", 1, 600, advanced=True,
                description="已开始回复后，连续没有新内容时允许等待的时间。思考内容和工具调用也计为输出。"),
     ]},
    {"id": "compression", "page": "general", "title": "上下文压缩", "effect": "新建会话生效",
     "fields": [
         field("compression.enabled", "自动压缩上下文", description="对话较长时整理前文，为后续任务留出空间。"),
         field("compression.threshold", "触发比例", "number", minimum=0.1, maximum=0.95, step=0.01,
               description="占模型上下文容量的比例，例如 0.5 表示 50%。"),
         field("compression.target_ratio", "保留近期内容比例", "number", minimum=0.1, maximum=0.8, step=0.01,
               description="压缩后保留的近期原文，占触发容量的比例。", advanced=True),
         field("compression.summary_provider", "摘要供应商", "provider", advanced=True),
         field("compression.summary_model", "摘要模型", "text", description="留空使用默认选择。", advanced=True),
         field("compression.summary_base_url", "摘要服务地址", "url", description="留空使用供应商地址。", advanced=True),
         number("compression.summary_timeout", "摘要超时（秒）", 30, 600, advanced=True),
     ]},
    {"id": "memory", "page": "memory", "title": "记忆设置", "effect": "新建会话生效；编辑容量立即生效",
     "fields": [
         field("memory.memory_enabled", "使用长期记忆"),
         field("memory.user_profile_enabled", "使用个人偏好"),
         number("memory.memory_char_limit", "长期记忆容量（字）", 1, 1000000, advanced=True),
         number("memory.user_char_limit", "个人偏好容量（字）", 1, 1000000, advanced=True),
         number("memory.memory_review_round", "记忆整理间隔（轮）", 0, 10000, description="每隔多少轮对话整理记忆；0 表示关闭自动整理。", advanced=True),
         number("skills.evolution_review_round", "技能整理间隔（轮）", 0, 10000, description="每累计多少轮对话检查可沉淀的经验和 Skill；保存成功后重新计数。0 表示关闭定期检查，对话中的主动沉淀仍可执行。", advanced=True),
     ]},
    {"id": "terminal", "page": "execution", "title": "命令与文件", "effect": "下一次任务生效",
     "fields": [
         number("terminal.timeout", "命令默认超时（秒）", 1, 86400),
         number("terminal.max_timeout", "命令超时上限（秒）", 1, 86400, advanced=True),
         number("file_read_max_chars", "单次文件读取上限（字符）", 1, 10000000, advanced=True),
     ]},
    {"id": "delegation", "page": "execution", "title": "子代理", "effect": "下一次任务生效",
     "fields": [
         number("delegation.max_iterations", "每项任务的最大迭代次数", 1, 10000),
         number("delegation.timeout_seconds", "每项任务的超时（秒）", 1, 86400),
         field("delegation.provider", "子代理供应商", "provider", advanced=True),
         field("delegation.model", "子代理模型", "text", description="留空沿用当前会话。", advanced=True),
         field("delegation.base_url", "子代理服务地址", "url", description="留空使用供应商地址。", advanced=True),
     ]},
    {"id": "checkpoints", "page": "execution", "title": "文件恢复", "effect": "新建会话生效",
     "fields": [
         field("checkpoints.enabled", "保存文件恢复记录", description="为任务中的文件修改保留恢复点。"),
         number("checkpoints.max_snapshots", "最多保留的恢复点", 1, 100000, advanced=True),
         number("checkpoints.max_total_size_mb", "恢复记录容量（MB）", 0, 1000000, description="0 表示不按总容量限制。", advanced=True),
         number("checkpoints.max_file_size_mb", "单个文件备份上限（MB）", 0, 100000, description="0 表示不按文件大小限制。", advanced=True),
         number("checkpoints.prune_retention_days", "清理时保留的天数", 0, 36500, advanced=True),
     ]},
    {"id": "schedule_defaults", "page": "scheduler", "title": "新建计划的默认值", "effect": "新建计划生效",
     "fields": [
         field("scheduler.default_timezone", "默认时区", "timezone", description="例如 Asia/Shanghai、UTC。"),
         number("scheduler.default_timeout_seconds", "任务超时（秒）", 1, 86400),
         number("scheduler.default_max_iterations", "最大迭代次数", 1, 10000),
     ]},
    {"id": "scheduler", "page": "scheduler", "title": "调度服务", "effect": "重启 M-Claw 服务后生效",
     "fields": [
         field("scheduler.enabled", "自动执行定时任务"),
         number("scheduler.max_workers", "同时执行的任务数", 1, 64),
         number("scheduler.tick_interval_seconds", "检查间隔（秒）", 1, 3600, advanced=True),
         number("scheduler.max_due_per_tick", "每次检查最多处理的计划数", 1, 1000, advanced=True),
         number("scheduler.max_consecutive_failures", "连续失败后暂停（次）", 1, 1000, advanced=True),
         number("scheduler.stale_run_after_seconds", "异常运行回收等待（秒）", 1, 604800, advanced=True),
         field("scheduler.output_dir", "运行结果目录", "text", advanced=True),
         field("scheduler.delivery.weixin.enabled", "允许向微信投递结果", advanced=True),
         field("scheduler.delivery.dingtalk.enabled", "允许向钉钉投递结果", advanced=True),
     ]},
]
FIELDS = {item["path"]: item for group in GROUPS for item in group["fields"]}


def _get(config: dict, path: str) -> str | int | float | bool | None:
    value = config
    for key in path.split("."):
        if not isinstance(value, dict):
            raise ApplicationError("CONFIG_INVALID", "配置分组格式不正确，请检查 config.yaml。", 409)
        value = value.get(key)
    return value


def _set(config: dict, path: str, value: str | int | float | bool | None) -> None:
    parts = path.split(".")
    for key in parts[:-1]:
        config = config.setdefault(key, {})
    config[parts[-1]] = value


class ConfigurationService:
    def __init__(self, home: Path) -> None:
        self.home = Path(home).resolve()

    def _read(self) -> tuple[dict, dict, str]:
        from mclaw.constants import get_mclaw_home

        if get_mclaw_home().resolve() != self.home:
            raise ApplicationError("CONFIG_ROOT_MISMATCH", "配置目录与本机服务不一致", 409)
        try:
            path = self.home / "config.yaml"
            content = path.read_bytes() if path.exists() else b""
            raw = yaml.safe_load(content) if content.strip() else {}
            raw = {} if raw is None else raw
            if not isinstance(raw, dict):
                raise ValueError("mapping required")
            merged = config_io._deep_merge(copy.deepcopy(config_io.DEFAULT_CONFIG), raw)
            config_io._validate_config(merged)
            for field_path in FIELDS:
                _get(merged, field_path)
            if isinstance(raw.get("scheduler", {}), dict) and "output_dir" not in raw.get("scheduler", {}):
                merged["scheduler"]["output_dir"] = str(self.home / "scheduler" / "output")
        except (OSError, ValueError, TypeError, yaml.YAMLError, config_io.ConfigError):
            raise ApplicationError("CONFIG_INVALID", "配置文件无法读取，请检查 config.yaml。原文件已保留。", 409) from None
        return raw, merged, hashlib.sha256(content).hexdigest()

    def _snapshot(self, config: dict, revision: str) -> dict:
        from mclaw.providers.registry import PROVIDER_REGISTRY

        groups = copy.deepcopy(GROUPS)
        for group in groups:
            for item in group["fields"]:
                value = _get(config, item["path"])
                if (value is not None and type(value) not in (str, int, float, bool)) or (type(value) is float and not math.isfinite(value)):
                    raise ApplicationError("CONFIG_INVALID", f"{item['label']}的配置格式不正确。", 409)
                if item["path"] == "scheduler.output_dir" and not value:
                    value = str(self.home / "scheduler" / "output")
                # Legacy URLs may contain credentials. Keep those backend-only;
                # an unchanged blank field does not participate in the patch.
                hidden = False
                if item["kind"] == "url" and value:
                    try:
                        parsed = urlsplit(str(value))
                        hidden = bool(parsed.username or parsed.password or parsed.query or parsed.fragment)
                    except ValueError:
                        hidden = True
                item.update(value="" if hidden else value, credential_url=hidden)
                if item["kind"] == "provider":
                    providers = config.get("providers") if isinstance(config.get("providers"), dict) else {}
                    fallbacks = config.get("fallback_providers") if isinstance(config.get("fallback_providers"), list) else []
                    names = dict.fromkeys([str(config.get("active_provider") or ""), *providers,
                        *(str(entry.get("provider") or "") for entry in fallbacks if isinstance(entry, dict))])
                    options = [{"value": "auto" if item["path"].startswith("compression.") else "",
                                "label": "自动选择" if item["path"].startswith("compression.") else "沿用当前会话"}]
                    for name in names:
                        if name and name not in {option["value"] for option in options}:
                            profile = PROVIDER_REGISTRY.get(name)
                            options.append({"value": name, "label": profile.display_name if profile else name})
                    if value and value not in {option["value"] for option in options}:
                        options.append({"value": value, "label": str(value)})
                    item["options"] = options
        return {"revision": revision, "groups": groups}

    def read(self) -> dict:
        with file_lock(self.home / "config.lock"):
            _raw, config, revision = self._read()
            return self._snapshot(config, revision)

    @staticmethod
    def _validate(path: str, value: object) -> str | int | float | bool:
        item = FIELDS[path]
        kind = item["kind"]
        valid = True
        if kind == "boolean":
            valid = type(value) is bool
        elif kind == "number":
            valid = (type(value) in (int, float) and item["minimum"] <= value <= item["maximum"]
                     and math.isfinite(value)
                     and (item["step"] != 1 or value == int(value)))
        else:
            valid = isinstance(value, str) and len(value) <= 2048 and not any(char in value for char in "\r\n\0")
            if valid:
                value = value.strip()
                if kind == "select":
                    valid = value in {option["value"] for option in item["options"]}
                elif kind == "timezone":
                    try:
                        ZoneInfo(value)
                    except (ValueError, ZoneInfoNotFoundError):
                        valid = False
                elif kind == "url" and value:
                    try:
                        parsed = urlsplit(value)
                        valid = (parsed.scheme in {"http", "https"} and bool(parsed.hostname)
                                 and not (parsed.username or parsed.password or parsed.query or parsed.fragment))
                    except ValueError:
                        valid = False
        if not valid:
            raise ApplicationError("INVALID_CONFIGURATION", f"请检查“{item['label']}”的值。", 422)
        return int(value) if kind == "number" and item["step"] == 1 else value

    def update(self, data: dict) -> dict:
        from mclaw.application.setup import _WORKFLOW_LOCK

        changes = data.get("changes")
        if (set(data) != {"revision", "changes"} or not isinstance(data.get("revision"), str)
                or not isinstance(changes, dict) or not changes or set(changes) - FIELDS.keys()):
            raise ApplicationError("INVALID_CONFIGURATION", "请选择要修改的设置。", 422)
        cleaned = {path: self._validate(path, value) for path, value in changes.items()}
        if not _WORKFLOW_LOCK.acquire(blocking=False):
            raise ApplicationError("SETUP_IN_PROGRESS", "请先完成或退出设置向导。", 409)
        try:
            with file_lock(self.home / "config.lock"):
                raw, config, revision = self._read()
                if revision != data["revision"]:
                    raise ApplicationError("CONFIGURATION_CONFLICT", "配置已在其他地方修改。请读取最新配置，再保存本次修改。", 409)
                candidate = copy.deepcopy(raw)
                for path, value in cleaned.items():
                    inherited = {"", "auto"} if path.startswith("compression.") else {""}
                    if FIELDS[path]["kind"] == "provider" and value not in inherited:
                        from mclaw.providers.registry import PROVIDER_REGISTRY
                        if value not in PROVIDER_REGISTRY and value not in (config.get("providers") or {}):
                            raise ApplicationError("INVALID_CONFIGURATION", "请选择已配置的供应商。", 422)
                    _set(candidate, path, value)
                    _set(config, path, value)
                if {"terminal.timeout", "terminal.max_timeout"} & changes.keys():
                    if _get(config, "terminal.timeout") > _get(config, "terminal.max_timeout"):
                        raise ApplicationError("INVALID_CONFIGURATION", "命令默认超时不能超过超时上限。", 422)
                if "reasoning.effort" in changes and cleaned["reasoning.effort"] and config.get("model"):
                    from mclaw.providers.resolver import ProviderResolutionError, resolve_provider_runtime_context
                    try:
                        resolve_provider_runtime_context(config=config)
                    except ProviderResolutionError:
                        raise ApplicationError("INVALID_CONFIGURATION", "当前默认模型无法使用该思考强度。请选“跟随模型”，或先检查供应商配置。", 422) from None
                try:
                    config_io._validate_config(config)
                    config_io.save_config(candidate)
                except (config_io.ConfigError, OSError):
                    raise ApplicationError("CONFIGURATION_SAVE_FAILED", "配置保存失败，请检查文件权限后重试。", 409) from None
                _raw, saved, revision = self._read()
                return self._snapshot(saved, revision)
        finally:
            _WORKFLOW_LOCK.release()
