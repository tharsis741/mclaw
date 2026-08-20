# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import json
import threading
from types import MappingProxyType, SimpleNamespace
from typing import Any

from mclaw.dsoftbus.active import clear_active_runtime, install_active_runtime
from mclaw.dsoftbus.tools import (
    get_device_context_handler,
    run_agent_task_handler,
)
from mclaw.tools.registry import registry
from mclaw.tools.toolsets import DSOFTBUS_TOOLS, TOOLSETS
from mclaw.cli.tui.renderers.banner import BannerRenderer

_DEVICE = "urn:mclaw:device:oh:" + "a" * 64
_RUNTIME = "00000000-0000-4000-8000-000000000101"
_MESSAGE = "00000000-0000-4000-8000-000000000102"
_CONTEXT = "00000000-0000-4000-8000-000000000103"


def test_dsoftbus_tools_use_the_standard_tui_display_metadata() -> None:
    assert TOOLSETS["dsoftbus"]["display"] == {
        "emoji": "✉",
        "summary_zh": "可信设备协作",
    }
    assert all(registry._tools[name].emoji == "✉" for name in DSOFTBUS_TOOLS)
    rows = BannerRenderer(box_factory=lambda: None, logo="")._enabled_toolset_rows(
        SimpleNamespace(valid_tool_names=set(DSOFTBUS_TOOLS))
    )
    assert rows == [
        ("dsoftbus", "可信设备协作", "✉", DSOFTBUS_TOOLS)
    ]
    assert BannerRenderer._toolset_label("✉", "dsoftbus") == "✉  dsoftbus"
    assert BannerRenderer._toolset_label("💻", "terminal") == "💻 terminal"


def _start_loop() -> tuple[asyncio.AbstractEventLoop, threading.Thread]:
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    return loop, thread


def test_agent_tools_use_the_runtime_owner_loop_and_turn_fence() -> None:
    loop, thread = _start_loop()
    owner_threads: list[int] = []

    class Runtime:
        @staticmethod
        def prepare_run_agent_task_outbound(
            device_id: str,
            text: str,
            *,
            context_id: str | None,
            message_id: str,
            event_sink=None,
        ) -> tuple[Any, Any, str | None, str]:
            async def operation() -> dict[str, Any]:
                owner_threads.append(threading.get_ident())
                if event_sink is not None:
                    event_sink(
                        {
                            "type": "assistant.message",
                            "content": "remote progress",
                            "content_source": "content",
                            "is_final": False,
                            "origin": "dsoftbus",
                        }
                    )
                return MappingProxyType({
                    "success": True,
                    "device_id": device_id,
                    "context_id": context_id or _CONTEXT,
                    "message_id": message_id,
                    "task_id": _MESSAGE,
                    "task_state": "TASK_STATE_COMPLETED",
                    "text": f"reply:{text}",
                    "artifacts": (),
                    "_mclawProvenance": MappingProxyType({
                        "kind": "peer",
                        "source": "mclaw.dsoftbus.runtime",
                        "peerDeviceId": device_id,
                        "peerRuntimeInstanceId": _RUNTIME,
                        "connectionGeneration": 1,
                        "receivedVia": "softbus",
                        "verifiedBinding": True,
                    }),
                    "_untrustedRemoteData": True,
                })

            return loop, operation(), context_id, message_id

        @staticmethod
        def prepare_device_context_refresh(device_id: str) -> tuple[Any, Any]:
            async def operation() -> dict[str, Any]:
                owner_threads.append(threading.get_ident())
                return MappingProxyType({
                    "success": True,
                    "device_id": device_id,
                    "agentAvailability": "READY",
                    "deviceContextAvailability": "STATE_FRESH",
                    "_mclawProvenance": MappingProxyType({
                        "kind": "peer",
                        "source": "mclaw.dsoftbus.runtime",
                        "peerDeviceId": device_id,
                        "peerRuntimeInstanceId": _RUNTIME,
                        "connectionGeneration": 1,
                        "receivedVia": "softbus",
                        "verifiedBinding": True,
                    }),
                    "_untrustedRemoteData": True,
                })

            return loop, operation()

    class Agent:
        def __init__(self) -> None:
            self.registered: list[Any] = []
            self.unregistered: list[Any] = []
            self.events: list[dict[str, Any]] = []

        def _register_turn_worker(self, worker: Any) -> None:
            self.registered.append(worker)

        def _unregister_turn_worker(self, worker: Any) -> None:
            self.unregistered.append(worker)

        def _emit_event(self, event: dict[str, Any]) -> None:
            self.events.append(event)

        @staticmethod
        def current_turn_cancel_event() -> threading.Event:
            return threading.Event()

    runtime = Runtime()
    agent = Agent()
    install_active_runtime(runtime)
    try:
        sent = json.loads(
            asyncio.run(
                run_agent_task_handler(
                    {
                        "device_id": _DEVICE,
                        "text": "hello",
                        "message_id": _MESSAGE,
                    },
                    parent_agent=agent,
                )
            )
        )
        context = json.loads(
            asyncio.run(
                get_device_context_handler(
                    {"device_id": _DEVICE, "refresh_state": True},
                    parent_agent=agent,
                )
            )
        )
    finally:
        clear_active_runtime(runtime)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(2)
        loop.close()

    assert sent["success"] is True
    assert sent["message_id"] == _MESSAGE
    assert sent["task_state"] == "TASK_STATE_COMPLETED"
    assert sent["_untrustedRemoteData"] is True
    assert context["success"] is True
    assert owner_threads == [thread.ident, thread.ident]
    assert len(agent.registered) == 2
    assert agent.unregistered == agent.registered
    assert agent.events[0]["content"] == "remote progress"


def test_owner_bridge_preflight_failure_never_uses_unfenced_fallback() -> None:
    class Runtime:
        fallback_calls = 0

        @staticmethod
        def prepare_run_agent_task_outbound(*args: Any, **kwargs: Any) -> Any:
            from mclaw.dsoftbus.runtime import DsoftbusRuntimeError

            raise DsoftbusRuntimeError("PEER_NOT_READY")

        @staticmethod
        def prepare_device_context_refresh(*args: Any, **kwargs: Any) -> Any:
            from mclaw.dsoftbus.runtime import DsoftbusRuntimeError

            raise DsoftbusRuntimeError("PEER_NOT_READY")

        async def arun_agent_task(self, *args: Any, **kwargs: Any) -> Any:
            self.fallback_calls += 1
            raise AssertionError("unfenced send fallback used")

        async def aget_device_context(self, *args: Any, **kwargs: Any) -> Any:
            self.fallback_calls += 1
            raise AssertionError("unfenced context fallback used")

    runtime = Runtime()
    install_active_runtime(runtime)
    try:
        sent = json.loads(
            asyncio.run(
                run_agent_task_handler(
                    {
                        "device_id": _DEVICE,
                        "text": "hello",
                        "message_id": _MESSAGE,
                    }
                )
            )
        )
        context = json.loads(
            asyncio.run(
                get_device_context_handler(
                    {"device_id": _DEVICE, "refresh_state": True}
                )
            )
        )
    finally:
        clear_active_runtime(runtime)

    assert sent["success"] is False
    assert sent["code"] == "PEER_NOT_READY"
    assert sent["message_id"] == _MESSAGE
    assert context["success"] is False
    assert context["code"] == "PEER_NOT_READY"
    assert runtime.fallback_calls == 0
