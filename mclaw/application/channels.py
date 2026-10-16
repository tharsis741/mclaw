# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Web-owned lifecycle and safe configuration for existing message gateways."""

from __future__ import annotations

import asyncio
import base64
import copy
from dataclasses import asdict
import io
import math
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import uuid

from mclaw.application.channel_runner import PersistentChannelRouter, SharedChannelRunner
from mclaw.application.sessions import ApplicationError
from mclaw.application.setup_interaction import SetupInteractionCancelled, current_setup_interaction
from mclaw.channels.dingtalk.config import DingTalkConfig
from mclaw.channels.weixin.config import WeixinConfig
from mclaw.system.lock import file_lock

_TYPES = {"weixin": WeixinConfig, "dingtalk": DingTalkConfig}
_LABELS = {"weixin": "微信", "dingtalk": "钉钉"}
_CREDENTIALS = {
    "weixin": {"account_id": "WEIXIN_ACCOUNT_ID", "token": "WEIXIN_TOKEN"},
    "dingtalk": {"client_id": "DINGTALK_CLIENT_ID", "client_secret": "DINGTALK_CLIENT_SECRET", "robot_code": "DINGTALK_ROBOT_CODE"},
}
_SELECTS = {"dm_policy": ["open", "allowlist", "disabled"], "group_policy": ["open", "mention_only", "disabled"],
            "session_scope": ["chat", "user", "chat_user"]}
_FIELD_LABELS = {"enabled": "随服务自动启动", "dm_policy": "私聊策略", "group_policy": "群聊策略",
    "require_mention": "群聊需提及机器人", "allowed_users": "允许的用户", "allowed_chats": "允许的群聊",
    "free_response_chats": "可直接响应的群聊", "mention_patterns": "提及匹配规则", "session_scope": "会话划分",
    "open_conversation_map": "钉钉群聊连接", "toolsets": "工具集", "base_url": "服务地址",
    "max_message_length": "消息长度上限", "media_cache_enabled": "缓存入站附件", "media_cache_dir": "附件缓存目录",
    "media_max_bytes": "附件大小上限（字节）", "send_chunk_delay_seconds": "分段发送间隔（秒）",
    "send_chunk_retries": "分段发送重试次数", "send_chunk_retry_delay_seconds": "发送重试间隔（秒）",
    "poll_timeout_ms": "接收超时（毫秒）", "api_timeout_ms": "接口超时（毫秒）",
    "shutdown_timeout_seconds": "停止等待（秒）", "close_timeout_seconds": "连接关闭超时（秒）",
    "dedup_ttl_seconds": "消息去重时长（秒）", "media_cdn_base_url": "附件 CDN 地址",
    "media_download_timeout_seconds": "附件下载超时（秒）", "media_upload_timeout_seconds": "附件上传超时（秒）",
    "reconnect_backoff_seconds": "重连等待间隔（秒）", "session_webhooks_max": "回信路由缓存上限"}
_COMMON = {"enabled", "dm_policy", "group_policy", "require_mention", "allowed_users", "allowed_chats"}


class ChannelService:
    def __init__(self, home: Path, sessions, *, runtime_factory=None, login_function=None) -> None:
        self.home, self.sessions = Path(home).resolve(), sessions
        self.runtime_factory = runtime_factory or self._build_runtime
        self.login_function = login_function
        self.runtimes: dict = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.errors: dict[str, str] = {}
        self.states: dict[str, str] = {}
        self._locks = {name: asyncio.Lock() for name in _TYPES}
        self._login: _Login | None = None
        self._closing = False

    @staticmethod
    def _kind(kind: str) -> None:
        if kind not in _TYPES:
            raise ApplicationError("CHANNEL_NOT_FOUND", "渠道不存在", 404)

    def _config(self) -> dict:
        from mclaw.cli.config import ConfigError, load_raw_user_config
        from mclaw.constants import get_mclaw_home
        if get_mclaw_home().resolve() != self.home:
            raise ApplicationError("CHANNEL_HOME_MISMATCH", "渠道配置目录与服务数据目录不一致")
        try:
            config = load_raw_user_config(strict=True)
            channels = config.get("channels", {})
            if not isinstance(channels, dict) or any(key in channels and not isinstance(channels[key], dict) for key in _TYPES):
                raise ConfigError("Invalid channels configuration")
            return config
        except ConfigError as exc:
            raise ApplicationError("CONFIG_INVALID", "配置文件无法读取，请先修复配置") from exc

    def get(self, kind: str) -> dict:
        self._kind(kind)
        from mclaw.cli.config import get_env_value
        try:
            root = self._config()
            cfg = _TYPES[kind].from_config(root)
            settings = asdict(cfg)
            if kind == "dingtalk":
                raw_map = root.get("channels", {}).get(kind, {}).get("open_conversation_map", {})
                settings["open_conversation_map"] = {
                    key: {"display_name": str(value.get("display_name") or ""),
                          "open_conversation_id": str(value.get("open_conversation_id") or "")}
                    for key, value in raw_map.items() if isinstance(value, dict)
                } if isinstance(raw_map, dict) else {}
            for name in _CREDENTIALS[kind]:
                settings.pop(name, None)
            credentials = {key: bool(get_env_value(env)) for key, env in _CREDENTIALS[kind].items()}
            configured = not cfg.validate()
            errors = cfg.validate()
        except (ApplicationError, ValueError, TypeError):
            settings, credentials, configured = {}, {}, False
            errors = ["配置文件无法读取，请先修复配置"]
        runtime = self.runtimes.get(kind)
        state = self.states.get(kind, "stopped")
        if state == "starting" and runtime is not None and runtime._running:
            state = "running"
        fields = []
        for key, value in settings.items():
            field_type = "select" if key in _SELECTS else (
                "boolean" if isinstance(value, bool) else "number" if isinstance(value, (int, float)) else
                "list" if isinstance(value, list) else "json" if isinstance(value, dict) else "text")
            fields.append({"name": key, "label": _FIELD_LABELS.get(key, key), "kind": field_type,
                           "options": _SELECTS.get(key, []), "advanced": key not in _COMMON})
        return {"id": kind, "label": _LABELS[kind], "enabled": bool(settings.get("enabled")),
                "configured": configured, "status": state, "error": self.errors.get(kind, ""),
                "settings": settings, "credentials": credentials, "fields": fields, "validation_errors": errors}

    def list(self) -> dict:
        return {"items": [self.get(kind) for kind in _TYPES]}

    async def start(self) -> None:
        for kind in _TYPES:
            if self.get(kind)["enabled"]:
                try:
                    await self.connect(kind)
                except ApplicationError:
                    pass

    async def close(self) -> None:
        self._closing = True
        if self._login:
            self._login.cancel()
            if self._login.thread:
                await asyncio.to_thread(self._login.thread.join, 0.5)
        await asyncio.gather(*(self.disconnect(kind) for kind in _TYPES))

    def _build_runtime(self, kind: str, config: dict):
        from mclaw.channels.audio_transcription import build_inbound_pipeline
        from mclaw.channels.dingtalk.runtime import DingTalkRuntime
        from mclaw.channels.weixin.runtime import WeixinRuntime
        cfg = _TYPES[kind].from_config(config)
        runner = SharedChannelRunner(self.sessions, platform=kind, config=config, toolsets=cfg.toolsets,
            account_id=cfg.account_id if kind == "weixin" else cfg.client_id,
            inbound_pipeline=build_inbound_pipeline(config))
        runtime_type = WeixinRuntime if kind == "weixin" else DingTalkRuntime
        runtime = runtime_type(config=config, provider_runtime=runner.startup_provider_runtime,
                               session_db=self.sessions.history, runner=runner)
        router = PersistentChannelRouter(runtime.session_router, self.sessions.store, kind)
        runtime.session_router = router
        runtime.adapter.session_router = router
        return runtime

    async def connect(self, kind: str) -> dict:
        self._kind(kind)
        async with self._locks[kind]:
            if self._closing:
                raise ApplicationError("SERVICE_STOPPING", "服务正在退出", 409)
            if kind in self.tasks and not self.tasks[kind].done():
                return self.get(kind)
            config = self._config()
            cfg = _TYPES[kind].from_config(config)
            if cfg.validate():
                self.errors[kind] = "；".join(cfg.validate())
                self.states[kind] = "error"
                raise ApplicationError("CHANNEL_NOT_CONFIGURED", self.errors[kind], 409)
            self.errors.pop(kind, None)
            self.states[kind] = "starting"
            try:
                runtime = self.runtime_factory(kind, config)
                self.runtimes[kind] = runtime
                self.tasks[kind] = asyncio.create_task(self._run(kind, runtime))
                # Allow synchronous validation/lock failures to become visible immediately.
                await asyncio.sleep(0)
            except Exception:
                self.states[kind] = "error"
                self.errors[kind] = "渠道启动失败，请运行诊断并检查配置"
            return self.get(kind)

    async def _run(self, kind, runtime) -> None:
        try:
            if kind == "weixin":
                starter = asyncio.create_task(runtime.start())
                while not runtime._running and not starter.done():
                    await asyncio.sleep(0.05)
                if runtime._running:
                    self.states[kind] = "running"
                    runtime.runner.restore_deliveries(runtime.adapter)
                await starter
            else:
                await runtime.start()
                self.states[kind] = "running"
                runtime.runner.restore_deliveries(runtime.adapter)
                while runtime._stream_task and not runtime._stream_task.done():
                    await asyncio.sleep(0.1)
                if runtime._stream_task:
                    await runtime._stream_task
        except asyncio.CancelledError:
            if kind == "weixin" and "starter" in locals():
                starter.cancel()
                await asyncio.gather(starter, return_exceptions=True)
        except Exception as exc:
            from mclaw.channels.weixin.runtime_lock import WeixinRuntimeLockError
            from mclaw.channels.dingtalk.runtime_lock import DingTalkRuntimeLockError
            self.states[kind] = "error"
            self.errors[kind] = ("此渠道已在其他入口运行，请先停止该入口" if isinstance(exc, (WeixinRuntimeLockError, DingTalkRuntimeLockError))
                                 else "渠道连接失败，请检查凭据、依赖与网络后重试")
        finally:
            from mclaw.application.channel_outbound import drain_channel_outbound
            if not await drain_channel_outbound(runtime.adapter):
                self.states[kind] = "error"
                self.errors[kind] = "渠道发送仍在收尾，结果尚未确认，请核对目标会话"
            await runtime.stop()
            if self.states.get(kind) != "error":
                self.states[kind] = "stopped"

    async def disconnect(self, kind: str) -> dict:
        self._kind(kind)
        async with self._locks[kind]:
            task = self.tasks.pop(kind, None)
            if task is not None and not task.done():
                self.states[kind] = "stopping"
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            self.runtimes.pop(kind, None)
            if self.states.get(kind) != "error":
                self.states[kind] = "stopped"
            return self.get(kind)

    async def update(self, kind: str, data: dict) -> dict:
        self._kind(kind)
        from mclaw.application.setup import _WORKFLOW_LOCK
        from mclaw.cli.config import get_env_value, save_config, save_env_value
        from mclaw.runtime.secrets import authorize
        if set(data) - {"settings", "credentials"}:
            raise ApplicationError("INVALID_CHANNEL", "渠道设置字段无效")
        settings, credentials = data.get("settings", {}), data.get("credentials", {})
        if not isinstance(settings, dict) or not isinstance(credentials, dict):
            raise ApplicationError("INVALID_CHANNEL", "渠道设置须为对象")
        allowed = set(asdict(_TYPES[kind]())) - set(_CREDENTIALS[kind])
        if set(settings) - allowed or set(credentials) - set(_CREDENTIALS[kind]):
            raise ApplicationError("INVALID_CHANNEL", "渠道设置包含不可修改的字段")
        for key, value in settings.items():
            default = getattr(_TYPES[kind](), key)
            if isinstance(default, bool) and type(value) is not bool:
                raise ApplicationError("INVALID_CHANNEL", "开关字段须为布尔值")
            if isinstance(default, list) and (not isinstance(value, list) or len(value) > 1000):
                raise ApplicationError("INVALID_CHANNEL", "列表字段格式无效")
            if isinstance(default, (int, float)) and not isinstance(default, bool) and (type(value) not in {int, float} or not math.isfinite(value) or value < 0):
                raise ApplicationError("INVALID_CHANNEL", "数值字段须为非负有限数")
            if isinstance(default, dict) and not isinstance(value, dict):
                raise ApplicationError("INVALID_CHANNEL", "映射字段格式无效")
            if key in _SELECTS and value not in _SELECTS[key]:
                raise ApplicationError("INVALID_CHANNEL", "策略选项无效")
        if any(not isinstance(value, str) or len(value) > 16000 or "\n" in value or "\r" in value for value in credentials.values()):
            raise ApplicationError("INVALID_CHANNEL", "凭据格式无效")
        account_id = credentials.get("account_id", "").strip()
        if account_id and (account_id in {".", ".."} or any(char in account_id for char in '/\\\0:')):
            raise ApplicationError("INVALID_CHANNEL", "微信账号标识格式无效")
        if not _WORKFLOW_LOCK.acquire(blocking=False):
            raise ApplicationError("SETUP_BUSY", "设置流程正在运行，请完成或取消后再保存", 409)
        try:
            with file_lock(self.home / "config.lock"):
                config = self._config()
                candidate = copy.deepcopy(config)
                candidate.setdefault("channels", {}).setdefault(kind, {}).update(settings)
                cfg = _TYPES[kind].from_config(candidate)
                for key, env in _CREDENTIALS[kind].items():
                    setattr(cfg, key, credentials.get(key, "").strip() or get_env_value(env) or getattr(cfg, key))
                errors = [value for value in cfg.validate() if cfg.enabled or " is required " not in value]
                if errors:
                    raise ApplicationError("INVALID_CHANNEL", "；".join(errors))
                for key, value in credentials.items():
                    if value.strip():
                        save_env_value(_CREDENTIALS[kind][key], value.strip())
                authorize("channel:" + kind, [env for env in _CREDENTIALS[kind].values() if get_env_value(env)])
                save_config(candidate)
        finally:
            _WORKFLOW_LOCK.release()
        await self.disconnect(kind)
        if cfg.enabled:
            return await self.connect(kind)
        return self.get(kind)

    def diagnose(self, kind: str) -> dict:
        self._kind(kind)
        from mclaw.providers.resolver import resolve_provider_runtime_context
        from mclaw.channels.dingtalk.stream_client import check_dingtalk_requirements
        status = self.get(kind)
        requirements = check_dingtalk_requirements() if kind == "dingtalk" else {"httpx": True}
        errors = list(status["validation_errors"])
        errors.extend("缺少依赖或接口：" + name for name, value in requirements.items() if not value)
        try:
            context = resolve_provider_runtime_context(config=self._config())
            provider, model = context.provider, context.model
        except Exception:
            provider, model = "", ""
            errors.append("模型接入配置不可用，请在设置中选择有效模型与凭据")
        return {"ready": not errors, "requirements": requirements, "errors": errors,
                "provider": provider, "model": model, "configuration": status}

    def login(self) -> dict:
        from mclaw.application.setup import _WORKFLOW_LOCK
        if self._closing:
            raise ApplicationError("SERVICE_STOPPING", "服务正在退出", 409)
        if self._login and self._login.status in {"waiting", "running"}:
            return self._login.snapshot()
        self._config()  # Refuse corrupt existing configuration before any credential write.
        if not _WORKFLOW_LOCK.acquire(blocking=False):
            raise ApplicationError("SETUP_BUSY", "设置流程正在运行，请完成或取消后再扫码", 409)
        self._login = _Login(self.login_function)
        self._login.start()
        return self._login.snapshot()

    def login_status(self, identifier: str, *, cancel=False) -> dict:
        if not self._login or self._login.id != identifier:
            raise ApplicationError("LOGIN_NOT_FOUND", "扫码登录流程不存在", 404)
        if cancel:
            self._login.cancel()
        return self._login.snapshot()


class _Login:
    def __init__(self, function=None):
        self.id = "login_" + uuid.uuid4().hex
        self.status, self.qr_code, self.message, self.error = "running", None, "正在生成登录二维码", ""
        self._cancelled = threading.Event()
        self._guard = threading.RLock()
        self.function = function
        self.thread = None

    def check_cancelled(self):
        if self._cancelled.is_set():
            raise SetupInteractionCancelled()

    def show_qr(self, payload):
        import qrcode
        from qrcode.image.svg import SvgPathImage
        self.check_cancelled()
        output = io.BytesIO()
        qrcode.make(payload, image_factory=SvgPathImage).save(output)
        with self._guard:
            self.qr_code = "data:image/svg+xml;base64," + base64.b64encode(output.getvalue()).decode()
            self.status, self.message = "waiting", "请用微信扫描二维码并确认登录"

    def write(self, text):
        self.check_cancelled()
        if "已扫码" in str(text):
            self.message = "已扫码，请在微信中确认登录"

    def cancel(self):
        self._cancelled.set()
        with self._guard:
            if self.status in {"waiting", "running"}:
                self.status, self.qr_code = "cancelled", None

    def snapshot(self):
        with self._guard:
            return {"id": self.id, "status": self.status, "qr_code": self.qr_code, "message": self.message, "error": self.error}

    def start(self):
        self.thread = threading.Thread(target=self._execute, name="mclaw-channel-login", daemon=True)
        self.thread.start()

    def _execute(self):
        from mclaw.application.setup import _WORKFLOW_LOCK
        from mclaw.cli.main import _run_weixin_login
        token = current_setup_interaction.set(self)
        try:
            function = self.function or (lambda: _run_weixin_login(SimpleNamespace(bot_type="3", timeout=480, no_qr=True), optional=True))
            ok = function()
            self.check_cancelled()
            self.status = "completed" if ok else "failed"
            self.message = "微信登录信息已保存，可启动网关" if ok else "扫码未完成，请重试"
        except SetupInteractionCancelled:
            self.status = "cancelled"
        except Exception:
            self.status, self.error = "failed", "微信扫码登录失败，请检查网络后重试"
        finally:
            self.qr_code = None
            current_setup_interaction.reset(token)
            _WORKFLOW_LOCK.release()
