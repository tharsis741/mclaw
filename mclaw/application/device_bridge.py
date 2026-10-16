# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Scoped worker calls into the application's sole native device runtime."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import math
import threading
import time


READ_OPERATIONS = frozenset({"health", "diagnostic_snapshot", "local_device", "list_peers", "list_trusted_devices", "get_cached_device_context"})
MANAGEMENT_OPERATIONS = frozenset({"discover_devices", "pair_device", "unbind_device"})
TASK_OPERATIONS = frozenset({"get_device_context", "run_agent_task", "continue_agent_task"})


def plain(value):
    if isinstance(value, Mapping):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [plain(item) for item in value]
    return value


class _CallScope:
    def __init__(self, session_id, cancel):
        self.session_id, self.cancel = session_id, cancel
        self.fences = set()
        self.lock = threading.Lock()

    def current_turn_cancel_event(self):
        return self.cancel

    def _register_turn_worker(self, fence):
        with self.lock:
            self.fences.add(fence)

    def _unregister_turn_worker(self, fence):
        with self.lock:
            self.fences.discard(fence)

    def drain(self):
        # Do not mistake cancellation of a proxy Future for actual native exit.
        while True:
            with self.lock:
                alive = any(fence.is_alive() for fence in self.fences)
            if not alive:
                return
            time.sleep(0.05)


def call_runtime(runtime, operation, args, kwargs, *, session_id, cancel, event_sink=None):
    from mclaw.dsoftbus.runtime import DsoftbusRuntimeError
    from mclaw.dsoftbus.tools import _run_runtime_outbound

    if cancel.is_set():
        raise DsoftbusRuntimeError("AGENT_INTERRUPTED", interrupted=True)
    if operation in READ_OPERATIONS | MANAGEMENT_OPERATIONS:
        return plain(getattr(runtime, operation)(*args, **kwargs))
    scope = _CallScope(session_id, cancel)
    if operation == "get_device_context":
        loop, coroutine = runtime.prepare_device_context_refresh(*args, **kwargs)
        timeout = 30.0
    elif operation in {"run_agent_task", "continue_agent_task"}:
        kwargs = {**kwargs, "event_sink": event_sink}
        prepare = runtime.prepare_run_agent_task_outbound if operation == "run_agent_task" else runtime.prepare_continue_agent_task_outbound
        loop, coroutine, *_identifiers = prepare(*args, **kwargs)
        timeout = math.inf
    else:
        raise DsoftbusRuntimeError("INVALID_PARAMS")
    try:
        succeeded, result = _run_runtime_outbound(coroutine, loop=loop, timeout=timeout,
                                                 label=operation, parent_agent=scope)
        if not succeeded:
            raise result
        return plain(result)
    finally:
        scope.drain()


async def invoke(service, request, *, session_id="terminal", cancel=None, event_sink=None, management=False,
                 allowed_devices=None):
    from mclaw.dsoftbus.runtime import DsoftbusRuntimeError

    operation = request.get("operation")
    allowed = READ_OPERATIONS | (MANAGEMENT_OPERATIONS if management else TASK_OPERATIONS)
    if not isinstance(operation, str) or operation not in allowed:
        raise DsoftbusRuntimeError("INVALID_PARAMS")
    if service is None or service.runtime is None or service._closing:
        raise DsoftbusRuntimeError("RUNTIME_UNAVAILABLE")
    args, kwargs = request.get("args", []), request.get("kwargs", {})
    if not isinstance(args, list) or not isinstance(kwargs, dict) or "event_sink" in kwargs:
        raise DsoftbusRuntimeError("INVALID_PARAMS")
    if allowed_devices is not None and operation in TASK_OPERATIONS | {"get_cached_device_context"}:
        device_id = args[0] if args else kwargs.get("device_id")
        if device_id not in allowed_devices:
            raise DsoftbusRuntimeError("AGENT_TOOLS_FORBIDDEN")
    cancel = cancel or threading.Event()
    if operation in MANAGEMENT_OPERATIONS:
        async with service._lock:
            if service._task and not service._task.done():
                raise DsoftbusRuntimeError("DEVICE_DISCOVERY_BUSY")
            # The shared task marker also prevents Web configuration/restart while pairing.
            task = asyncio.create_task(asyncio.to_thread(call_runtime, service.runtime, operation, args, kwargs,
                                                        session_id=session_id, cancel=cancel))
            service._task, service._task_kind = task, "operation"
        try:
            return await asyncio.shield(task)
        finally:
            service._read_at = 0
    result = await asyncio.to_thread(call_runtime, service.runtime, operation, args, kwargs,
                                     session_id=session_id, cancel=cancel, event_sink=event_sink)
    if allowed_devices is not None and operation == "list_peers":
        return [peer for peer in result if peer.get("device_id", peer.get("deviceId")) in allowed_devices]
    return result


class WorkerDeviceClient:
    """Installed in a session process; owns no native handles or endpoint locks."""

    def __init__(self, request):
        self.request = request

    def _call(self, operation, *args, **kwargs):
        from mclaw.dsoftbus.runtime import DsoftbusRuntimeError
        from mclaw.tools.dispatch import get_current_tool_call_id
        from mclaw.tools.interrupt import get_interrupt_event
        kwargs.pop("event_sink", None)
        result = self.request("device_rpc", _cancel_event=get_interrupt_event(), operation=operation,
                              args=args, kwargs=kwargs, tool_call_id=get_current_tool_call_id())
        if result.get("error"):
            error = result["error"]
            raise DsoftbusRuntimeError(error["code"], outcome_unknown=error.get("outcome_unknown", False),
                                      interrupted=error.get("interrupted", False))
        return result.get("value")

    def health(self):
        return self._call("health")

    def diagnostic_snapshot(self):
        return self._call("diagnostic_snapshot")

    def list_peers(self, *, ready_only=False):
        return self._call("list_peers", ready_only=ready_only)

    def get_cached_device_context(self, device_id):
        return self._call("get_cached_device_context", device_id)

    async def aget_device_context(self, device_id, *, refresh_state=False):
        operation = "get_device_context" if refresh_state else "get_cached_device_context"
        return await asyncio.to_thread(self._call, operation, device_id)

    async def arun_agent_task(self, *args, **kwargs):
        return await asyncio.to_thread(self._call, "run_agent_task", *args, **kwargs)

    async def acontinue_agent_task(self, *args, **kwargs):
        return await asyncio.to_thread(self._call, "continue_agent_task", *args, **kwargs)
