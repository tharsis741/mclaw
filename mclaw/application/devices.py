# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Application-owned Kaihong discovery, pairing and device lifecycle."""

from __future__ import annotations

import asyncio
import copy
from pathlib import Path
import re
import time
import uuid

from mclaw.application.sessions import ApplicationError
from mclaw.dsoftbus.device_management import DeviceManagementCoordinator


_MESSAGES = {
    "ENDPOINT_IN_USE": "设备协同正由另一个 M-Claw 进程使用，请退出该进程后重试。",
    "ENDPOINT_OCCUPIED": "设备协同正由另一个 M-Claw 进程使用。",
    "ENDPOINT_LOCKED": "设备协同正由另一个 M-Claw 进程使用。",
    "DEVICE_DISCOVERY_FAILED": "搜索未完成，请检查设备网络后重试。",
    "DEVICE_DISCOVERY_BUSY": "正在搜索设备，请稍候。",
    "DEVICE_NOT_FOUND": "未找到这台设备，请重新搜索。",
    "DEVICE_BIND_FAILED": "配对未完成，请检查两台设备上的系统提示。",
    "DEVICE_BIND_TIMEOUT": "配对超时，请确认另一台设备上的系统提示后重试。",
    "DEVICE_BIND_UNCONFIRMED": "尚未确认配对结果，请刷新已配对设备。",
    "DEVICE_UNBIND_FAILED": "解除配对未完成，请刷新后检查设备状态。",
    "DEVICE_UNBIND_UNCONFIRMED": "尚未确认解除配对的结果，请刷新设备列表。",
    "DEVICE_UNMANAGED": "这段信任关系由系统或其他应用建立，请在系统设置中管理。",
    "DEVICE_PERMISSION_DENIED": "设备协同缺少系统权限。",
    "WORKER_NOT_READY": "设备协同正在连接系统服务，请稍后重试。",
}


def _error(error: Exception) -> dict:
    code = str(getattr(error, "code", "DEVICE_SERVICE_ERROR"))
    return {"code": code, "message": _MESSAGES.get(code, "设备协同暂不可用，请检查系统服务后重试。"),
            "outcome_unknown": bool(getattr(error, "outcome_unknown", False))}


class DeviceService:
    """Own one Runtime per application, independent of pages and conversations."""

    def __init__(self, home: Path, *, runtime_factory=None, platform_factory=None) -> None:
        self.home = Path(home).resolve()
        self.runtime_factory = runtime_factory
        self.platform_factory = platform_factory
        self.runtime = None
        self.supported = False
        self.enabled = False
        self.state = "starting"
        self.error = None
        self.operation = None
        self.report = {"localDevice": None, "devices": [], "trustedDevices": [], "pairableDevices": [], "discoveryWarning": None}
        self._task: asyncio.Task | None = None
        self._task_kind = "startup"
        self._closing = False
        self._lock = asyncio.Lock()
        self._read_lock = asyncio.Lock()
        self._read_at = 0.0

    def _start_runtime(self) -> None:
        from mclaw.cli.config import load_config
        from mclaw.dsoftbus.product import create_product_runtime
        from mclaw.providers.resolver import ProviderResolutionError, resolve_provider_runtime_context
        from mclaw.runtime.manager import RuntimeManager

        platform = (self.platform_factory or RuntimeManager.create)()
        self.supported = platform.kind == "kaihong" and platform.features.is_enabled("dsoftbus")
        if not self.supported:
            self.state = "unsupported"
            return
        config = load_config(strict=True)
        self.enabled = config.get("dsoftbus", {}).get("enabled") == "auto"
        if not self.enabled:
            self.state = "disabled"
            return
        try:
            provider = resolve_provider_runtime_context(config=config)
        except ProviderResolutionError:
            provider = None
        self.runtime = (self.runtime_factory or create_product_runtime)(
            config=config, provider_runtime=provider, workspace=self.home / "_temp",
            state_root=self.home / "dsoftbus", require_activation=True,
        )
        health = self.runtime.start()
        self.state = str(health.get("state", "DEGRADED")).lower()
        if self.state not in {"ready", "degraded"}:
            raise ApplicationError(str(health.get("primaryErrorCode") or "WORKER_NOT_READY"), "设备协同尚未就绪")

    async def start(self) -> None:
        # Native startup must not block HTTP readiness or ordinary chat.
        if self._task is None and not self._closing:
            self._task = asyncio.create_task(self._activate())

    async def _activate(self) -> None:
        self.state, self.error = "starting", None
        try:
            await asyncio.to_thread(self._start_runtime)
        except Exception as error:
            self.state, self.error = "error", _error(error)
        finally:
            self._read_at = 0

    async def _refresh(self) -> None:
        async with self._read_lock:
            if self.runtime is None or self.state not in {"ready", "degraded"} or time.monotonic() - self._read_at < 3:
                return
            try:
                report = await asyncio.to_thread(DeviceManagementCoordinator(self.runtime).snapshot)
                data = report.public_dict()
                self.report.update({key: data[key] for key in ("localDevice", "devices", "trustedDevices")})
                health = self.runtime.health()
                self.state = str(health.get("state", "DEGRADED")).lower()
                self.error = None
            except Exception as error:
                self.error = _error(error)
            self._read_at = time.monotonic()

    async def snapshot(self) -> dict:
        await self._refresh()
        health = self.runtime.health() if self.runtime else {}
        return copy.deepcopy({"supported": self.supported, "enabled": self.enabled, "state": self.state,
                              "error": self.error, "operation": self.operation,
                              "provider_ready": bool(health.get("providerReady")), **self.report})

    async def configure(self, data: dict) -> dict:
        from mclaw.cli.config import load_raw_user_config, save_config
        from mclaw.constants import get_mclaw_home
        from mclaw.system.lock import file_lock

        enabled = data.get("enabled")
        if type(enabled) is not bool:
            raise ApplicationError("INVALID_DEVICE_SETTINGS", "请选择是否启用设备协同")
        async with self._lock:
            if self._closing or (self._task and not self._task.done()):
                raise ApplicationError("DEVICE_BUSY", "设备操作尚未结束，请稍候", 409)
            if not self.supported:
                raise ApplicationError("DEVICE_UNSUPPORTED", "当前系统不支持跨设备协同", 409)
            if get_mclaw_home().resolve() != self.home:
                raise ApplicationError("DEVICE_HOME_MISMATCH", "设备配置目录与服务不一致", 409)

            def save():
                with file_lock(self.home / "config.lock"):
                    config = load_raw_user_config(strict=True)
                    config.setdefault("dsoftbus", {})["enabled"] = "auto" if enabled else False
                    save_config(config)

            await asyncio.to_thread(save)
            self.enabled = enabled
            self.state = "starting"
            self._task_kind = "startup"
            self._task = asyncio.create_task(self._restart())
        return await self.snapshot()

    async def _stop_runtime(self) -> None:
        if self.runtime is not None:
            runtime, self.runtime = self.runtime, None
            from mclaw.dsoftbus.protocol import DSOFTBUS_SHUTDOWN_TIMEOUT_S
            try:
                await asyncio.to_thread(runtime.begin_shutdown)
            finally:
                await asyncio.to_thread(runtime.stop, time.monotonic() + DSOFTBUS_SHUTDOWN_TIMEOUT_S)

    async def begin_shutdown(self) -> None:
        """Stop accepting new device work while session workers are draining."""
        self._closing = True
        if self.runtime is not None:
            await asyncio.to_thread(self.runtime.begin_shutdown)

    async def _restart(self) -> None:
        try:
            await self._stop_runtime()
            self.report = {"localDevice": None, "devices": [], "trustedDevices": [], "pairableDevices": [], "discoveryWarning": None}
            await self._activate()
        except Exception as error:
            self.state, self.error = "error", _error(error)

    async def act(self, action: str, data: dict) -> dict:
        if action not in {"discover", "pair", "unpair"}:
            raise ApplicationError("DEVICE_ACTION_NOT_FOUND", "设备操作不存在", 404)
        selected = data.get("device_id", "")
        if action != "discover" and (not isinstance(selected, str) or not re.fullmatch("[0-9a-f]{64}", selected) or data.get("confirmed") is not True):
            raise ApplicationError("DEVICE_CONFIRMATION_REQUIRED", "请先选择设备并确认操作")
        async with self._lock:
            if self._closing or (self._task and not self._task.done()):
                raise ApplicationError("DEVICE_BUSY", "设备操作尚未结束，请稍候", 409)
            if self.runtime is None or self.state not in {"ready", "degraded"}:
                raise ApplicationError("DEVICE_RUNTIME_INACTIVE", "请先启用设备协同", 409)
            self.operation = {"id": uuid.uuid4().hex, "action": action, "status": "running", "device_id": selected, "error": None}
            self._task_kind = "operation"
            self._task = asyncio.create_task(self._execute(action, selected))
        return await self.snapshot()

    async def _execute(self, action: str, selected: str) -> None:
        coordinator = DeviceManagementCoordinator(
            self.runtime, device_selector=lambda _: selected, pair_device_selector=lambda _: selected,
            pair_confirmation=lambda _: True, unpair_confirmation=lambda _: True,
        )
        try:
            result = await asyncio.to_thread(coordinator.execute, "devices" if action == "discover" else action)
            report = result.public_dict()
            if action == "discover":
                self.report = report
                if report.get("discoveryWarning"):
                    warning = report["discoveryWarning"]
                    self.operation.update(status="failed", error={"code": warning["code"], "message": _MESSAGES.get(warning["code"], "部分设备未能完成搜索，请重试。")})
                else:
                    self.operation["status"] = "completed"
            else:
                self.operation["status"] = "completed"
                self.report["pairableDevices"] = [item for item in self.report["pairableDevices"] if item["deviceIdSha256"] != selected]
        except Exception as error:
            self.operation.update(status="failed", error=_error(error))
        finally:
            self._read_at = 0

    async def close(self) -> None:
        self._closing = True
        # Shutdown the native worker first to unblock pending system requests;
        # cancelling to_thread alone would leave a pairing operation running.
        if self._task and self._task_kind == "startup":
            await asyncio.gather(self._task, return_exceptions=True)
        try:
            await self._stop_runtime()
        finally:
            if self._task:
                await asyncio.gather(self._task, return_exceptions=True)
        self.state = "stopped"
