# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Async host for one long-lived, isolated Python worker per Session."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Callable


class ProcessRunner:
    """Keep cwd, environment, terminal state and tool lifetimes session-local."""

    def __init__(self, home: Path, session: dict, *, devices=None, background_emit=None) -> None:
        self.home = Path(home).resolve()
        self.session = {key: session[key] for key in (
            "id", "workspace", "source", "workspace_explicit", "managed_workspace_root"
        ) if key in session}
        self.process: subprocess.Popen | None = None
        self._output: asyncio.Queue | None = None
        self._input_lock = threading.Lock()
        self._pending: list[dict] = []
        self._running = False
        self._closed = False
        self._requests: set[asyncio.Task] = set()
        self.devices = devices
        self.background_emit = background_emit
        self._device_cancel = threading.Event()
        self._device_cancels: dict[str, threading.Event] = {}
        self._allowed_devices = None

    async def _device_request(self, request: dict) -> None:
        from mclaw.application.device_bridge import invoke, plain
        from mclaw.application.collaboration import import_receipts, peer_name

        cancel = self._device_cancels.setdefault(request["id"], threading.Event())
        if self._device_cancel.is_set():
            cancel.set()
        operation = request.get("operation")
        remote = operation in {"run_agent_task", "continue_agent_task"}
        args, kwargs = request.get("args") or [], request.get("kwargs") or {}
        device_id = str(args[0] if args else kwargs.get("device_id") or "")
        entry = {"type": "remote.task", "id": "remote_" + request["id"], "kind": "remote",
                 "tool_call_id": request.get("tool_call_id"),
                 "title": "继续远程任务" if operation == "continue_agent_task" else "远程任务",
                 "device_id": device_id, "device_name": "协作设备", "status": "running", "progress": [],
                 "arguments": {"args": args, **kwargs},
                 "task_id": (args[1] if len(args) > 1 else kwargs.get("task_id", "")) if operation == "continue_agent_task" else ""}

        def publish():
            self.send({"type": "device_event", "event": plain(entry)})

        def progress(event):
            if not remote:
                self.send({"type": "device_event", "event": plain(event)})
                return
            entry["task_id"] = event.get("remote_task_id") or entry["task_id"]
            if event.get("content"):
                # Each remote update keeps its identity; local reasoning never gets overwritten.
                count = entry.get("progress_count", 0) + 1
                entry["progress_count"] = count
                entry["progress"].append({"id": count, "content": str(event["content"])[:8000]})
                entry["progress"] = entry["progress"][-40:]
            publish()

        try:
            if remote:
                entry["device_name"] = peer_name(self.devices, device_id)
                publish()
            value = await invoke(self.devices, request, session_id=self.session["id"], cancel=cancel,
                                 event_sink=progress, allowed_devices=self._allowed_devices)
            if remote:
                entry.update(task_id=value.get("task_id") or entry["task_id"], output=value,
                             status="waiting" if value.get("input_request") else "completed",
                             input_request=value.get("input_request"))
                if value.get("success") is False:
                    entry["status"] = "unknown" if value.get("completion_unknown") or value.get("outcome_unknown") else (
                        "interrupted" if value.get("interrupted") else "failed")
                if value.get("artifacts") and value.get("task_id"):
                    try:
                        files, warnings = await asyncio.to_thread(import_receipts, self.home, self.session["id"],
                                                                 device_id, entry["device_name"], value)
                        entry.update(files=files, warnings=warnings)
                    except Exception:
                        entry["warnings"] = ["文件未能加入会话，请在调用详情中核对回传记录。"]
                publish()
            result = {"value": value}
        except Exception as exc:
            result = {"error": {"code": str(getattr(exc, "code", "INTERNAL_ERROR")),
                                "outcome_unknown": bool(getattr(exc, "outcome_unknown", False)),
                                "interrupted": bool(getattr(exc, "interrupted", False))}}
            if remote:
                entry.update(status="unknown" if result["error"]["outcome_unknown"] else (
                    "interrupted" if result["error"]["interrupted"] else "failed"), output=result)
                publish()
        self.send({"type": "reply", "request_id": request["id"], "value": result})
        self._device_cancels.pop(request["id"], None)

    async def _channel_request(self, request: dict) -> None:
        try:
            from mclaw.application.channel_outbound import handle_channel_outbound
            value = await handle_channel_outbound(self.home, self.session["id"], request)
        except Exception:
            value = {"success": False, "error": "文件发送失败，请检查渠道连接与文件状态"}
        self.send({"type": "reply", "request_id": request["id"], "value": value})

    async def _drain_requests(self) -> None:
        if self._requests:
            pending, self._requests = self._requests, set()
            await asyncio.gather(*pending, return_exceptions=True)

    def _cancel_device_calls(self) -> None:
        self._device_cancel.set()
        for cancel in list(self._device_cancels.values()):
            cancel.set()

    def _start(self) -> None:
        if self.process is not None:
            if self.process.poll() is None:
                return
            for stream in (self.process.stdin, self.process.stdout):
                if stream is not None:
                    stream.close()
            self.process = None
        loop = asyncio.get_running_loop()
        self._output = asyncio.Queue()
        output = self._output
        env = os.environ.copy()
        env.update(MCLAW_HOME=str(self.home), TERMINAL_CWD=self.session["workspace"],
                   PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1", PYTHONSAFEPATH="1")
        package_root = str(Path(__file__).resolve().parents[2])
        env["PYTHONPATH"] = package_root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        self.process = subprocess.Popen(
            [sys.executable, "-u", "-m", "mclaw.application.worker"],
            cwd=self.session["workspace"], env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        process = self.process
        worker_id = uuid.uuid4().hex
        from mclaw.cli.runtime.session_lock import InteractiveSessionLock

        owner = InteractiveSessionLock(self.session["id"], self.home / "sessions" / "locks")
        if owner.path.exists():
            try:
                # The child receives no input and cannot run tools until its
                # liveness is included in the cross-frontend ownership record.
                owner.attach_worker(process.pid)
            except BaseException:
                process.terminate()
                process.wait()
                raise

        def receive() -> None:
            try:
                while line := process.stdout.readline(2_000_001):
                    try:
                        if len(line) > 2_000_000:
                            raise ValueError("Worker message too large")
                        value = json.loads(line)
                        if not isinstance(value, dict):
                            raise ValueError("Invalid worker message")
                    except (ValueError, UnicodeDecodeError):
                        value = {"type": "protocol_error"}
                    if value.get("type") in {"background", "skill_review"} and self.background_emit:
                        loop.call_soon_threadsafe(self.background_emit, {**value, "worker_id": worker_id})
                    else:
                        loop.call_soon_threadsafe(output.put_nowait, value)
            except (OSError, RuntimeError, ValueError):
                pass
            finally:
                try:
                    if self.background_emit:
                        loop.call_soon_threadsafe(self.background_emit, {
                            "type": "skill_review_worker_stopped", "worker_id": worker_id,
                        })
                    loop.call_soon_threadsafe(output.put_nowait, {"type": "eof"})
                except RuntimeError:
                    pass

        threading.Thread(target=receive, name=f"mclaw-worker-{self.session['id']}", daemon=True).start()
        self.send({"type": "init", "home": str(self.home), "session": self.session})

    def send(self, command: dict) -> None:
        """Send controls synchronously; values never appear in logs or argv."""
        if self._closed:
            return
        if command.get("type") in {"stop", "shutdown"}:
            self._cancel_device_calls()
        with self._input_lock:
            if self.process is None:
                self._pending.append(dict(command))
                return
            if self.process.poll() is not None:
                return
            try:
                self.process.stdin.write((json.dumps(command, ensure_ascii=False) + "\n").encode("utf-8"))
                self.process.stdin.flush()
            except (BrokenPipeError, OSError):
                return

    async def run(self, task: dict, emit: Callable[[dict], None]) -> dict:
        """Return only after the attempt's real tool workers have drained."""
        if self._closed or self._running:
            raise RuntimeError("Session worker is closed or already running")
        self._running = True
        run_id = str((task.get("attempts") or [{}])[-1].get("id") or uuid.uuid4().hex)
        native_runtime = None
        local_turn = str(uuid.uuid4())
        from mclaw.application.streaming import EventBatcher
        batcher = EventBatcher(emit)
        try:
            self._device_cancel = threading.Event()
            from mclaw.application.collaboration import selected_scope
            self._allowed_devices = selected_scope(task)
            self._start()
            device_available = bool(self.devices and self.devices.runtime and self.devices.state in {"ready", "degraded"})
            if device_available and callable(getattr(self.devices.runtime, "local_turn_started", None)):
                native_runtime = self.devices.runtime
                native_runtime.local_turn_started(local_turn)
            self.send({"type": "run", "run_id": run_id, "task": {**task, "_device_service": device_available}})
            pending, self._pending = self._pending, []
            for command in pending:
                self.send(command)
            while True:
                event = await self._output.get()
                kind = event.get("type")
                if kind in {"eof", "protocol_error"}:
                    batcher.flush()
                    self._cancel_device_calls()
                    if kind == "protocol_error":
                        self.send({"type": "shutdown"})
                    await asyncio.to_thread(self.process.wait)
                    await self._drain_requests()
                    return {"completed": False, "error": "会话执行进程已退出，请核对已知结果后继续处理", "completion_unknown": True}
                if event.get("run_id") != run_id:
                    continue
                if kind == "result":
                    batcher.flush()
                    await self._drain_requests()
                    return event.get("result") or {"completed": False, "error": "执行器未返回结果"}
                if kind == "request" and (event.get("request") or {}).get("kind") == "channel_outbound":
                    batcher.flush()
                    self._requests.add(asyncio.create_task(self._channel_request(event["request"])))
                    continue
                if kind == "request" and (event.get("request") or {}).get("kind") == "device_rpc":
                    batcher.flush()
                    self._requests.add(asyncio.create_task(self._device_request(event["request"])))
                    continue
                if kind == "device_cancel":
                    self._device_cancels.setdefault(event["request_id"], threading.Event()).set()
                    continue
                batcher.push({key: value for key, value in event.items() if key != "run_id"})
        except asyncio.CancelledError:
            self.send({"type": "stop"})
            await self.close()
            raise
        finally:
            try:
                batcher.flush()
            finally:
                try:
                    if native_runtime is not None:
                        native_runtime.local_turn_finished(local_turn)
                finally:
                    self._running = False

    async def control(self, request: dict) -> dict:
        """Execute recovery on the same worker after its task workers drained."""
        from mclaw.application.sessions import ApplicationError

        if self._closed or self._running:
            raise ApplicationError("SESSION_BUSY", "请等待当前任务和工具收尾后再操作文件恢复", 409)
        self._running = True
        identifier = uuid.uuid4().hex
        try:
            self._start()
            self.send({"type": "maintenance", "run_id": identifier, "request": request})
            while True:
                event = await self._output.get()
                if event.get("type") in {"eof", "protocol_error"}:
                    if event.get("type") == "protocol_error":
                        self.send({"type": "shutdown"})
                    await asyncio.to_thread(self.process.wait)
                    raise ApplicationError("RECOVERY_UNKNOWN", "会话执行器已退出，请核对恢复记录和文件实际状态", 409)
                if event.get("run_id") == identifier and event.get("type") == "maintenance_result":
                    result = event.get("result") or {}
                    if result.get("error"):
                        raise ApplicationError(result.get("code", "RECOVERY_FAILED"), result["error"], result.get("status", 409))
                    return result.get("value") or {}
        except asyncio.CancelledError:
            await self.close()
            raise
        finally:
            self._running = False

    async def close(self) -> None:
        """Stop execution, drain managed resources and wait for actual exit."""
        if self._closed:
            return
        self.send({"type": "shutdown"})
        await self._drain_requests()
        if self.process is not None:
            await asyncio.to_thread(self.process.wait)
            for stream in (self.process.stdin, self.process.stdout):
                if stream is not None:
                    stream.close()
        self._closed = True
