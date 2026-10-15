# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Drive the existing Setup workflow through versionable, secret-safe forms.

Each run executes the original provider, capability, channel and skill setup
functions. Only their interaction boundary is adapted. In particular, successful
provider steps retain the existing immediate-save and cancellation semantics.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import io
import re
import threading
import uuid
from dataclasses import asdict
from types import SimpleNamespace
from typing import Any

from mclaw.application.setup_interaction import (
    SetupInteractionCancelled,
    current_setup_interaction,
)

_WORKFLOW_LOCK = threading.Lock()
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_SECRET_HINT = re.compile(r"\[([^\]]*\.\.\.)\]")
_TERMINAL = {"completed", "cancelled", "failed"}
SETUP_STEPS = [
    {"id": "welcome", "label": "欢迎与使用说明"},
    {"id": "providers", "label": "选择并配置接入方"},
    {"id": "default_model", "label": "确定默认模型"},
    {"id": "capabilities", "label": "工具能力、消息渠道与语音"},
    {"id": "skills", "label": "导入内置技能"},
    {"id": "complete", "label": "配置完成"},
]


class SetupServiceError(ValueError):
    """A safe, stable error suitable for HTTP responses."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def configuration_status() -> dict[str, Any]:
    """Report readiness without returning any resolved credentials."""
    from mclaw.cli.config import ConfigError, load_config
    from mclaw.providers.resolver import (
        ProviderResolutionError,
        resolve_provider_runtime_context,
    )

    try:
        config = load_config(strict=True)
    except ConfigError:
        return {
            "ready": False, "required": True, "provider": "", "model": "",
            "error": {"code": "CONFIG_INVALID", "message": "现有配置无法读取，请修复配置后继续。"},
        }
    result: dict[str, Any] = {
        "ready": False,
        "required": True,
        "provider": str(config.get("active_provider") or ""),
        "model": str(config.get("model") or ""),
        "error": None,
    }
    try:
        context = resolve_provider_runtime_context(config=config)
    except ProviderResolutionError as exc:
        result["error"] = {
            "code": exc.code,
            "message": "请检查模型、接入方和凭据设置。",
        }
    else:
        result.update(ready=True, required=False, provider=context.provider, model=context.model)
    return result


def _saved_fingerprint() -> str:
    from mclaw.cli.config import get_config_path, get_env_path

    digest = hashlib.sha256()
    for path in (get_config_path(), get_env_path()):
        if path.exists():
            digest.update(path.read_bytes())
    return digest.hexdigest()


class _SetupRun:
    def __init__(self) -> None:
        self.id = uuid.uuid4().hex
        self.condition = threading.Condition(threading.RLock())
        self.cancelled = threading.Event()
        self.status = "running"
        self.prompt: dict[str, Any] | None = None
        self.messages: list[str] = []
        self.error: dict[str, str] | None = None
        self.qr_code: str | None = None
        self.answer: Any = None
        self.answered = False
        self.secrets: set[str] = set()
        self.before = _saved_fingerprint()
        self.saved = False
        self.configuration = configuration_status()
        self.thread: threading.Thread | None = None
        self._load_secrets()

    def _load_secrets(self) -> None:
        from mclaw.cli.config import get_env_value, load_env
        from mclaw.providers.registry import PROVIDER_REGISTRY

        names = set(load_env())
        names.update(name for profile in PROVIDER_REGISTRY.values() for name in profile.env_vars)
        for name in names:
            if any(marker in name.upper() for marker in ("KEY", "TOKEN", "SECRET", "PASSWORD")):
                value = get_env_value(name)
                if value:
                    self.secrets.add(value)

    def clean(self, value: str) -> str:
        text = _ANSI.sub("", value)
        for secret in sorted(self.secrets, key=len, reverse=True):
            text = text.replace(secret, "[已配置]")
            if len(secret) > 8:
                text = text.replace(secret[:8] + "...", "已配置")
        return text.strip()

    def check_cancelled(self) -> None:
        if self.cancelled.is_set():
            raise SetupInteractionCancelled()

    def write(self, text: str) -> None:
        self.check_cancelled()
        with self.condition:
            cleaned = self.clean(text)
            if cleaned:
                self.messages.append(cleaned)
                self.messages = self.messages[-300:]
            self.condition.notify_all()

    def show_qr(self, payload: str) -> None:
        """Render the existing iLink login payload for the browser."""
        import qrcode
        from qrcode.image.svg import SvgPathImage

        self.check_cancelled()
        output = io.BytesIO()
        qrcode.make(payload, image_factory=SvgPathImage).save(output)
        with self.condition:
            self.qr_code = "data:image/svg+xml;base64," + base64.b64encode(output.getvalue()).decode("ascii")
            self.condition.notify_all()

    def request(self, kind: str, title: str, **kwargs: Any) -> Any:
        self.check_cancelled()
        with self.condition:
            self._load_secrets()
            self._refresh_saved_state()
            self.qr_code = None
            raw_title = _ANSI.sub("", title)
            configured = bool(_SECRET_HINT.search(raw_title)) if kind == "secret" else False
            if kind == "secret":
                raw_title = _SECRET_HINT.sub("[已配置，留空复用]", raw_title)
            self.prompt = {
                "id": uuid.uuid4().hex,
                "kind": kind,
                "title": self.clean(raw_title),
                "hint": self.clean(str(kwargs.get("hint", ""))),
                "options": copy.deepcopy(kwargs.get("options", [])),
                "default": copy.deepcopy(kwargs.get("default", "")),
            }
            for key in ("presentation", "provider", "field_label", "placeholder", "credential_path", "key_url"):
                if kwargs.get(key):
                    self.prompt[key] = self.clean(str(kwargs[key]))
            if kind == "secret":
                self.prompt["secret_configured"] = kwargs.get("secret_configured", configured)
                if not self.prompt["hint"] and not self.prompt["secret_configured"]:
                    notes = [line for line in self.messages[-8:] if any(word in line for word in ("需要", "跳过", "获取密钥", "填写", "格式"))]
                    self.prompt["hint"] = "\n".join(notes)
            self.status = "waiting"
            self.answered = False
            self.condition.notify_all()
            self.condition.wait_for(lambda: self.answered or self.cancelled.is_set())
            self.check_cancelled()
            answer = self.answer
            self.answer = None
            self.prompt = None
            self.status = "running"
            return answer

    def submit(self, prompt_id: str, value: Any) -> None:
        with self.condition:
            if self.status != "waiting" or not self.prompt or self.prompt["id"] != prompt_id:
                raise SetupServiceError("SETUP_STALE_PROMPT", "该设置步骤已改变，请刷新后继续。")
            kind = self.prompt["kind"]
            if kind in {"single_select", "multi_select"}:
                if kind == "single_select" and isinstance(value, str):
                    value = [value] if value else []
                allowed = {item["id"] for item in self.prompt["options"]}
                if (
                    not isinstance(value, list)
                    or any(not isinstance(item, str) or item not in allowed for item in value)
                    or len(value) != len(set(value))
                    or (kind == "single_select" and len(value) > 1)
                ):
                    raise SetupServiceError("SETUP_INVALID_VALUE", "请选择当前步骤提供的选项。")
            elif not isinstance(value, str) or len(value) > 32768:
                raise SetupServiceError("SETUP_INVALID_VALUE", "请输入有效的设置值。")
            if kind == "secret" and value:
                self.secrets.add(value)
                for item in re.split(r"[;\n]", value):
                    if "=" in item and item.partition("=")[2].strip():
                        self.secrets.add(item.partition("=")[2].strip())
            self.answer = copy.deepcopy(value)
            self.answered = True
            self.status = "running"
            self.condition.notify_all()

    def snapshot(self) -> dict[str, Any]:
        with self.condition:
            return {
                "id": self.id,
                "status": self.status,
                "prompt": copy.deepcopy(self.prompt) if self.status == "waiting" else None,
                "messages": [self.clean(item) for item in self.messages],
                "error": copy.deepcopy(self.error),
                "qr_code": self.qr_code,
                "saved": self.saved,
                "configuration": copy.deepcopy(self.configuration),
            }

    def _refresh_saved_state(self) -> None:
        # Windows may refuse atomic replacement while another thread is reading
        # the old file. Read only at worker-owned interaction/terminal boundaries.
        self.saved = self.before != _saved_fingerprint()
        self.configuration = configuration_status()

    def execute(self) -> None:
        from mclaw.cli.main import _SetupCancelled, _run_setup_impl

        token = current_setup_interaction.set(self)
        terminal_status = "failed"
        try:
            _run_setup_impl(SimpleNamespace())
        except (SetupInteractionCancelled, _SetupCancelled):
            terminal_status = "cancelled"
        except SystemExit as exc:
            terminal_status = "cancelled" if not exc.code else "failed"
            if exc.code:
                self.error = {"code": "SETUP_INCOMPLETE", "message": "尚未完成接入方配置。"}
        except Exception:
            terminal_status = "failed"
            # Provider failures may include headers or submitted credentials.
            # Never return their raw exception or traceback to the browser.
            self.error = {"code": "SETUP_FAILED", "message": "设置未完成。已保存的步骤已保留，请重新打开设置重试。"}
        else:
            terminal_status = "cancelled" if self.cancelled.is_set() else "completed"
        finally:
            current_setup_interaction.reset(token)
            with self.condition:
                try:
                    self._refresh_saved_state()
                except OSError:
                    self.error = {"code": "SETUP_READ_FAILED", "message": "暂时无法读取保存结果，请重新检查设置。"}
                    terminal_status = "failed"
                self.status = terminal_status
                self.prompt = None
                self.qr_code = None
                self.answer = None
                self.condition.notify_all()
            _WORKFLOW_LOCK.release()


class SetupService:
    """Own one Setup run at a time and retain its forms across browser reloads."""

    def __init__(self) -> None:
        self._runs: dict[str, _SetupRun] = {}
        self._lock = threading.RLock()
        self._providers: list[dict[str, Any]] | None = None

    def status(self) -> dict[str, Any]:
        from mclaw.cli.auth import PROVIDER_REGISTRY, resolve_api_key
        from mclaw.cli.provider_profiles import get_provider_profiles

        with self._lock:
            active = next((run for run in self._runs.values() if run.status not in _TERMINAL), None)
            result = active.snapshot()["configuration"] if active else configuration_status()
            if self._providers is None or active is None:
                self._providers = [
                    {
                        "id": name,
                        "label": provider.display_name,
                        "configured": bool(resolve_api_key(name)),
                        "profiles": [asdict(profile) for profile in get_provider_profiles(name)],
                    }
                    for name, provider in PROVIDER_REGISTRY.items()
                ]
            result["steps"] = copy.deepcopy(SETUP_STEPS)
            result["providers"] = copy.deepcopy(self._providers)
            result["active_run_id"] = active.id if active else None
        return result

    def start(self) -> dict[str, Any]:
        from mclaw.cli.config import ConfigError, load_config

        with self._lock:
            active = next((run for run in self._runs.values() if run.status not in _TERMINAL), None)
            if active is not None:
                return active.snapshot()
            try:
                load_config(strict=True)
            except ConfigError as exc:
                raise SetupServiceError("CONFIG_INVALID", "现有配置无法读取；请修复后继续，原文件已保留。") from exc
            if not _WORKFLOW_LOCK.acquire(blocking=False):
                raise SetupServiceError("SETUP_BUSY", "另一个设置流程正在运行。")
            try:
                self.status()
                run = _SetupRun()
                self._runs = dict(list(self._runs.items())[-15:])
                self._runs[run.id] = run
                run.thread = threading.Thread(target=run.execute, name="mclaw-setup", daemon=True)
                run.thread.start()
            except BaseException:
                _WORKFLOW_LOCK.release()
                raise
        with run.condition:
            run.condition.wait_for(lambda: run.status != "running", timeout=1)
        return run.snapshot()

    def _get(self, run_id: str) -> _SetupRun:
        with self._lock:
            if run_id not in self._runs:
                raise SetupServiceError("SETUP_NOT_FOUND", "设置流程不存在，请重新打开设置。")
            return self._runs[run_id]

    def get(self, run_id: str) -> dict[str, Any]:
        return self._get(run_id).snapshot()

    def submit(self, run_id: str, prompt_id: str, value: Any) -> dict[str, Any]:
        run = self._get(run_id)
        run.submit(prompt_id, value)
        # Most steps only prepare the next form. Return it in this response;
        # network probes and QR login remain asynchronous and cancellable.
        with run.condition:
            run.condition.wait_for(lambda: run.status != "running" or run.qr_code is not None, timeout=0.2)
            return run.snapshot()

    def cancel(self, run_id: str) -> dict[str, Any]:
        run = self._get(run_id)
        with run.condition:
            if run.status not in _TERMINAL:
                run.cancelled.set()
                run.condition.notify_all()
                run.condition.wait_for(lambda: run.status in _TERMINAL, timeout=0.1)
        return run.snapshot()

    def close(self) -> None:
        """Cancel outstanding prompts when the owning application stops."""
        for run in list(self._runs.values()):
            self.cancel(run.id)
