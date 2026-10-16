# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared scheduler management and a lifecycle owned by the local Web service."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timezone
import logging
from pathlib import Path
import threading
import time
from typing import Any

from mclaw.application.sessions import ApplicationError, SessionService
from mclaw.scheduler.delivery import DeliveryService
from mclaw.scheduler.engine import SchedulerEngine
from mclaw.scheduler.ids import new_job_id, new_pairing_code
from mclaw.scheduler.models import DeliverySpec, SchedulerJob, SchedulerRun, SchedulerTargetPairing
from mclaw.scheduler.ownership import SchedulerOwnershipError
from mclaw.scheduler.runner import SchedulerRunner
from mclaw.scheduler.store import SchedulerStore
from mclaw.scheduler.targets import TargetManager
from mclaw.scheduler.triggers import humanize_schedule, parse_schedule, preview_next_runs
from mclaw.tools.toolsets import validate_toolset

logger = logging.getLogger(__name__)
_EDITABLE = {"name", "prompt", "schedule", "enabled", "workdir", "session_policy", "model",
             "concurrency_policy", "enabled_toolsets", "max_iterations", "timeout_seconds", "delivery"}


class ScheduleService:
    """Keep the original scheduler tables and serialize turns through SessionService."""

    def __init__(self, home: Path, sessions: SessionService, *, config: dict | None = None,
                 delivery=None, tick_interval: float | None = None) -> None:
        self.home = Path(home).resolve()
        self.sessions = sessions
        self.store = SchedulerStore(sessions.history)
        self._configuration_error = ""
        self._provided_config = config
        self.config = config if config is not None else self._read_config()
        cfg = self.config.get("scheduler", {})
        self.tick_interval = tick_interval if tick_interval is not None else max(1, int(cfg.get("tick_interval_seconds", 30)))
        self.delivery = delivery or DeliveryService(store=self.store)
        self._own_delivery = delivery is None
        self._clients: list[Any] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ticker: asyncio.Task | None = None
        self._execution: dict[str, tuple[str, str]] = {}
        self._cancelled: set[str] = set()
        self._guard = threading.RLock()
        self._commands = threading.RLock()
        self._closing = False
        self._runtime_error = ""
        self.runner = SchedulerRunner(
            session_db=sessions.history, store=self.store, config=self.config,
            output_dir=str(cfg.get("output_dir") or self.home / "scheduler" / "output"),
            conversation_executor=self._execute_sync, cancel_executor=self._cancel_sync,
        )
        self.engine = SchedulerEngine(
            store=self.store, runner=self.runner, delivery=self.delivery, worker_id="web-scheduler",
            max_due_per_tick=int(cfg.get("max_due_per_tick", 5)), max_workers=int(cfg.get("max_workers", 2)),
            runtime_config=cfg,
        )

    def _read_config(self) -> dict:
        import yaml
        path = self.home / "config.yaml"
        if not path.exists():
            return {}
        try:
            value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if not isinstance(value, dict) or not isinstance(value.get("scheduler", {}), dict):
                raise ValueError("invalid scheduler config")
        except (OSError, ValueError, yaml.YAMLError):
            self._configuration_error = "配置文件无法读取，请修复配置后重启定时任务服务"
            return {"scheduler": {"enabled": False}}
        return value

    async def start(self) -> None:
        if self._ticker is not None:
            return
        self._loop = asyncio.get_running_loop()
        self.engine.start()
        if self._own_delivery:
            self._configure_delivery()
        self._ticker = asyncio.create_task(self._tick(), name="mclaw-scheduler")

    def runtime(self) -> dict:
        enabled = self.config.get("scheduler", {}).get("enabled", True)
        owned = self.engine.ownership.acquired
        return {"running": bool(owned and enabled and not self._closing),
                "owner": "web" if owned else "other",
                "reason": self._configuration_error or self._runtime_error or ("" if owned and enabled else
                    ("定时任务自动触发已在配置中关闭" if not enabled else "由另一个 M-Claw 入口运行"))}

    async def _tick(self) -> None:
        while not self._closing:
            try:
                if self.config.get("scheduler", {}).get("enabled", True):
                    await asyncio.to_thread(self._tick_once)
                self.store.expire_pairings(time.time())
                self._runtime_error = ""
            except Exception:
                logger.exception("scheduler tick failed")
                self._runtime_error = "定时任务调度失败，请检查配置或重启后台服务"
            await asyncio.sleep(self.tick_interval)

    def _tick_once(self) -> None:
        with self._commands:
            self.engine.tick_once()

    async def close(self) -> None:
        self._closing = True
        if self._ticker:
            self._ticker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._ticker
            self._ticker = None
        # Keep the application loop alive while scheduler workers cancel their queued turns.
        await asyncio.to_thread(self._shutdown)
        for client in self._clients:
            with contextlib.suppress(Exception):
                await client.close()
        self._clients.clear()

    def _shutdown(self) -> None:
        with self._commands:
            self.engine.shutdown(cancel_running=True)

    def _execute_sync(self, job, run, **execution) -> dict:
        if self._loop is None:
            raise RuntimeError("定时任务后台尚未启动")
        future = asyncio.run_coroutine_threadsafe(self._execute(job, run, execution), self._loop)
        return future.result()

    async def _execute(self, job, run, execution) -> dict:
        current_job = self.store.get_job(job.id) or job
        sid = current_job.session_id if job.session_policy == "task_thread" else ""
        if sid and self.sessions.store.setting(f"deleted_session:{sid}", False):
            sid = ""
        if not sid:
            created = self.sessions.create("scheduler-bootstrap", job.workdir, self._model(job.id))
            sid = created["id"]
            self.sessions.release(sid, "scheduler-bootstrap")
            if job.session_policy == "task_thread":
                job.session_id = sid
                self.store.update_job(job.id, {"session_id": sid})
        run.session_id = sid
        self.store.finish_run(run, job_patch={})
        execution.update(platform="scheduler", max_iterations=job.max_iterations)
        with self._guard:
            cancelled = run.id in self._cancelled
        if self._closing or cancelled:
            return {"cancelled": True, "completed": False}
        deadline = time.monotonic() + job.timeout_seconds
        while True:
            with self._guard:
                cancelled = run.id in self._cancelled
            if self._closing or cancelled:
                return {"cancelled": True, "completed": False}
            try:
                self.sessions.prepare_background_session(sid, source="scheduler")
                submission = self.sessions.submit_background(sid, job.prompt, "schedule:" + run.id, execution)
                break
            except ApplicationError as exc:
                if exc.code != "SESSION_OCCUPIED" or time.monotonic() >= deadline:
                    raise
                await asyncio.sleep(0.1)
        task_id = submission["task_id"]
        with self._guard:
            self._execution[run.id] = (sid, task_id)
        try:
            while True:
                with self._guard:
                    cancelled = run.id in self._cancelled
                if self._closing or cancelled or time.monotonic() >= deadline:
                    self.sessions.cancel_background(sid, task_id)
                    return {"cancelled": bool(self._closing or cancelled), "completed": False,
                            "error": "服务退出或运行已取消" if self._closing or cancelled else "定时任务执行超时"}
                result = self.sessions.task_result(sid, task_id)
                if result is not None:
                    return result
                await asyncio.sleep(0.05)
        finally:
            with self._guard:
                self._execution.pop(run.id, None)
                self._cancelled.discard(run.id)

    def _cancel_sync(self, run_id: str) -> None:
        with self._guard:
            self._cancelled.add(run_id)
        # The async executor observes cancellation before submission and while waiting.

    def _model(self, job_id: str) -> str:
        return str(self.sessions.store.setting("schedule_model:" + job_id, ""))

    def _public_job(self, job: SchedulerJob) -> dict:
        return job.to_dict() | {"model": self._model(job.id), "schedule_label": humanize_schedule(job.schedule),
                                "next_runs": self._preview(job.schedule)}

    @staticmethod
    def _preview(spec) -> list[float]:
        return [item.timestamp() for item in preview_next_runs(spec, after=datetime.now(timezone.utc), count=3)]

    def defaults(self) -> dict:
        """Read creation defaults fresh without changing a running scheduler."""
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        config = self._provided_config if self._provided_config is not None else self._read_config()
        cfg = config.get("scheduler", {})
        try:
            zone = str(cfg.get("default_timezone", "Asia/Shanghai"))
            ZoneInfo(zone)
            timeout = int(cfg.get("default_timeout_seconds", 3600))
            iterations = int(cfg.get("default_max_iterations", 200))
            if not 1 <= timeout <= 86400 or not 1 <= iterations <= 10000:
                raise ValueError("invalid defaults")
        except (ValueError, TypeError, ZoneInfoNotFoundError):
            raise ApplicationError("CONFIG_INVALID", "定时任务默认配置不正确，请在设置中检查。", 409) from None
        return {"timezone": zone, "timeout_seconds": timeout, "max_iterations": iterations}

    def _schedule(self, raw: Any):
        if not isinstance(raw, dict):
            raise ApplicationError("INVALID_SCHEDULE", "请选择运行时间")
        data = dict(raw)
        # Accept stored specs for editing while rebuilding their parsed representation.
        kind, expression = data.get("trigger_type"), str(data.get("expression") or "")
        if kind == "once":
            data.setdefault("at", expression)
        elif kind == "daily":
            data.setdefault("time", expression)
        elif kind in {"weekly", "monthly"} and expression:
            parts = expression.split()
            if len(parts) == 2:
                data.setdefault("weekday" if kind == "weekly" else "day", parts[0])
                data.setdefault("time", parts[1])
        elif kind == "interval":
            data.setdefault("every", expression)
        try:
            return parse_schedule(data, default_timezone=data.get("timezone") or self.defaults()["timezone"])
        except (ValueError, KeyError) as exc:
            raise ApplicationError("INVALID_SCHEDULE", str(exc)) from exc

    def preview(self, raw: dict) -> dict:
        spec = self._schedule(raw.get("schedule"))
        return {"next_runs": self._preview(spec), "schedule_label": humanize_schedule(spec)}

    def list(self, *, offset: int = 0, limit: int = 50) -> dict:
        jobs = self.store.list_jobs()
        return {"items": [self._public_job(job) for job in jobs[offset:offset + limit]],
                "total": len(jobs), "offset": offset, "limit": limit, "runtime": self.runtime(),
                "defaults": self.defaults()}

    def _job(self, job_id: str) -> SchedulerJob:
        job = self.store.get_job(job_id)
        if job is None:
            raise ApplicationError("SCHEDULE_NOT_FOUND", "计划不存在", 404)
        return job

    def get(self, job_id: str) -> dict:
        return self._public_job(self._job(job_id))

    def _validated(self, data: dict, previous: SchedulerJob | None = None) -> SchedulerJob:
        unknown = set(data) - _EDITABLE
        if unknown:
            raise ApplicationError("INVALID_SCHEDULE", "包含不可修改的计划字段")
        defaults = self.defaults() if previous is None else {}
        values = previous.to_dict() if previous else {"id": new_job_id(), "created_at": time.time(),
            "enabled_toolsets": ["mclaw-required"], "session_policy": "task_thread", "concurrency_policy": "skip",
            "max_iterations": defaults["max_iterations"], "timeout_seconds": defaults["timeout_seconds"]}
        values.update({key: value for key, value in data.items() if key != "model"})
        values["name"] = str(values.get("name") or "").strip()
        values["prompt"] = str(values.get("prompt") or "").strip()
        if not values["name"] or len(values["name"]) > 200 or not values["prompt"] or len(values["prompt"]) > 100000:
            raise ApplicationError("INVALID_SCHEDULE", "请填写计划名称（最多 200 字）和任务内容（最多 100000 字）")
        if "enabled" in data and not isinstance(data["enabled"], bool):
            raise ApplicationError("INVALID_SCHEDULE", "启用状态须为布尔值")
        spec = self._schedule(values.get("schedule"))
        values["schedule"] = spec.to_dict()
        raw_workdir = str(values.get("workdir") or "").strip()
        if raw_workdir:
            values["workdir"] = self.sessions.validate_workspace(raw_workdir)
        if previous and previous.session_id and raw_workdir != previous.workdir:
            raise ApplicationError("WORKSPACE_BOUND", "计划已有专属会话；更换工作区请新建计划", 409)
        try:
            job = SchedulerJob.from_dict(values)
        except (ValueError, TypeError) as exc:
            raise ApplicationError("INVALID_SCHEDULE", str(exc)) from exc
        if job.timeout_seconds > 86400 or job.max_iterations > 10000 or job.delivery.retry_count > 10:
            raise ApplicationError("INVALID_SCHEDULE", "运行时长最多 24 小时，迭代最多 10000 次，投递重试最多 10 次")
        if any(not validate_toolset(name, allow_platform=False, allow_scoped=False) for name in job.enabled_toolsets):
            raise ApplicationError("INVALID_TOOLSET", "计划包含不可用工具集")
        target = self.store.get_target(job.delivery.target_id)
        if target is None:
            raise ApplicationError("TARGET_NOT_FOUND", "请选择已有投递目标")
        if previous is None or "schedule" in data or data.get("enabled") is True:
            upcoming = self._preview(spec)
            if not upcoming:
                raise ApplicationError("SCHEDULE_EXPIRED", "计划没有未来执行时间，请调整时间")
            job.next_run_at = upcoming[0]
        job.updated_at = time.time()
        if not job.enabled:
            job.status = "paused"
        elif previous and not previous.enabled:
            job.status = "idle"
        return job

    def create(self, data: dict) -> dict:
        job = self._validated(data)
        self.store.create_job(job)
        self.sessions.store.set_setting("schedule_model:" + job.id, str(data.get("model") or ""))
        return self._public_job(job)

    def update(self, job_id: str, data: dict) -> dict:
        previous = self._job(job_id)
        job = self._validated(data, previous)
        if "model" in data and previous.session_id:
            self.sessions.update_background_model(previous.session_id, str(data["model"] or ""))
        self.store.update_job(job_id, job.to_dict())
        if "model" in data:
            self.sessions.store.set_setting("schedule_model:" + job_id, str(data["model"] or ""))
        return self.get(job_id)

    def delete(self, job_id: str) -> dict:
        self._job(job_id)
        active = [run for run in self.store.list_runs(job_id, limit=10000) if run.status in {"running", "queued"}]
        if active:
            raise ApplicationError("SCHEDULE_RUNNING", "请先取消此计划的运行记录再删除", 409)
        self.store.delete_job(job_id)
        self.sessions.store.set_setting("schedule_model:" + job_id, "")
        return {"deleted": True}

    async def run_now(self, job_id: str) -> dict:
        self._job(job_id)
        if self._closing or self._loop is None:
            raise ApplicationError("SERVICE_STOPPING", "定时任务服务尚未启动或正在退出", 409)
        def execute():
            with self._commands:
                return self.engine.run_job_now(job_id, manual=False).to_dict()
        try:
            return await asyncio.to_thread(execute)
        except SchedulerOwnershipError as exc:
            raise ApplicationError("SCHEDULER_OCCUPIED", str(exc), 409) from exc

    async def cancel_run(self, run_id: str) -> dict:
        if self.store.get_run(run_id) is None:
            raise ApplicationError("RUN_NOT_FOUND", "运行记录不存在", 404)
        def cancel():
            with self._commands:
                return self.engine.cancel_run(run_id).to_dict()
        try:
            return await asyncio.to_thread(cancel)
        except SchedulerOwnershipError as exc:
            raise ApplicationError("SCHEDULER_OCCUPIED", str(exc), 409) from exc

    def runs(self, job_id: str, *, offset: int = 0, limit: int = 50) -> dict:
        self._job(job_id)
        return {"items": [run.to_dict() for run in self.store.list_runs(job_id, offset=offset, limit=limit)],
                "offset": offset, "limit": limit}

    @staticmethod
    def _public_target(target) -> dict:
        return {key: value for key, value in TargetManager().validate_target(target).to_dict().items()
                if key != "route_metadata"}

    def targets(self) -> dict:
        return {"items": [self._public_target(target) for target in self.store.list_targets(enabled_only=False)]}

    def update_target(self, target_id: str, data: dict) -> dict:
        target = self.store.get_target(target_id)
        if target is None:
            raise ApplicationError("TARGET_NOT_FOUND", "投递目标不存在", 404)
        if set(data) - {"display_name", "enabled"}:
            raise ApplicationError("INVALID_TARGET", "只能修改目标名称和启用状态")
        if "enabled" in data and not isinstance(data["enabled"], bool):
            raise ApplicationError("INVALID_TARGET", "启用状态须为布尔值")
        if "display_name" in data and (not str(data["display_name"]).strip() or len(str(data["display_name"])) > 200):
            raise ApplicationError("INVALID_TARGET", "目标名称须为 1–200 字")
        if target_id == "local" and data.get("enabled") is False:
            raise ApplicationError("TARGET_BUILTIN", "本地投递目标保持启用")
        return self._public_target(self.store.update_target(target_id, data))

    def delete_target(self, target_id: str) -> dict:
        if target_id == "local":
            raise ApplicationError("TARGET_BUILTIN", "本地投递目标不可删除")
        if any(job.delivery.target_id == target_id for job in self.store.list_jobs()):
            raise ApplicationError("TARGET_IN_USE", "请先修改使用此目标的计划", 409)
        try:
            self.store.delete_target(target_id)
        except KeyError as exc:
            raise ApplicationError("TARGET_NOT_FOUND", "投递目标不存在", 404) from exc
        return {"deleted": True}

    async def test_target(self, target_id: str) -> dict:
        if not self.store.get_target(target_id):
            raise ApplicationError("TARGET_NOT_FOUND", "投递目标不存在", 404)
        job = SchedulerJob.from_dict({"id": "delivery-test", "name": "M-Claw 投递测试"})
        run = SchedulerRun.from_dict({"id": "delivery-test", "job_id": job.id})
        return await asyncio.to_thread(self.delivery.deliver_one, DeliverySpec(target_id, retry_count=1),
                                       job, run, "M-Claw 投递测试：目标连接正常。")

    def create_pairing(self, data: dict) -> dict:
        kind = data.get("type")
        if kind not in {"dingtalk_group", "dingtalk_private", "weixin_private"}:
            raise ApplicationError("INVALID_TARGET", "请选择钉钉群聊、钉钉单聊或微信单聊")
        name = str(data.get("display_name") or "").strip()
        if len(name) > 200:
            raise ApplicationError("INVALID_TARGET", "目标名称最多 200 字")
        pairing = SchedulerTargetPairing(new_pairing_code(), kind, "waiting", display_name_hint=name,
                                         expires_at=time.time() + 600)
        return self._public_pairing(self.store.create_pairing(pairing))

    @staticmethod
    def _public_pairing(pairing) -> dict:
        return pairing.to_dict() | {"command": "/schedule-bind " + pairing.code}

    def pairing(self, code: str, *, cancel: bool = False) -> dict:
        self.store.expire_pairings(time.time())
        pairing = self.store.get_pairing(code)
        if pairing is None:
            raise ApplicationError("PAIRING_NOT_FOUND", "绑定请求不存在", 404)
        if cancel and pairing.status == "waiting":
            pairing = self.store.cancel_pairing(code)
        return self._public_pairing(pairing)

    def _configure_delivery(self) -> None:
        """Use scoped channel configuration; transports stay on the service loop."""
        try:
            from mclaw.channels.dingtalk.config import DingTalkConfig
            from mclaw.channels.dingtalk.stream_client import DingTalkClient
            cfg = DingTalkConfig.from_config(self.config)
            if cfg.enabled:
                client = DingTalkClient(cfg)
                self._clients.append(client)
                self.delivery.dingtalk_client = _LoopClient(client, self._loop)
        except Exception:
            logger.warning("scheduler DingTalk delivery configuration unavailable")
        try:
            from mclaw.channels.weixin.config import WeixinConfig
            from mclaw.channels.weixin.context_token_store import ContextTokenStore
            from mclaw.channels.weixin.ilink_client import ILinkClient
            cfg = WeixinConfig.from_config(self.config)
            if cfg.enabled:
                client = ILinkClient(base_url=cfg.base_url, token=cfg.token, timeout_ms=cfg.api_timeout_ms)
                self._clients.append(client)
                self.delivery.weixin_client = _LoopClient(client, self._loop)
                self.delivery.weixin_token_store = ContextTokenStore()
                if cfg.account_id:
                    self.delivery.weixin_token_store.restore(cfg.account_id)
        except Exception:
            logger.warning("scheduler Weixin delivery configuration unavailable")


class _LoopClient:
    """Bridge synchronous scheduler delivery to one event loop per async transport."""

    def __init__(self, client, loop) -> None:
        self.client, self.loop = client, loop

    def __getattr__(self, name):
        value = getattr(self.client, name)
        if not callable(value):
            return value
        def call(*args, **kwargs):
            async def invoke():
                result = value(*args, **kwargs)
                return await result if hasattr(result, "__await__") else result
            return asyncio.run_coroutine_threadsafe(invoke(), self.loop).result()
        return call
