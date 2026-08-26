# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace

import pytest

from mclaw.cli.runtime.interactive import InteractiveRuntime
from mclaw.dsoftbus.active import clear_active_runtime, get_active_runtime
from mclaw.dsoftbus.product import (
    ProductDiscoveryOwnerResources,
    ProductRuntimeInputs,
)


def test_missing_product_inputs_degrade_without_loading_profile() -> None:
    calls: list[str] = []
    resources = ProductDiscoveryOwnerResources(
        inputs=ProductRuntimeInputs(),
        profile_loader=lambda path: calls.append(path),
    )

    result = asyncio.run(resources.start("00000000-0000-4000-8000-000000000000"))
    stopped = asyncio.run(resources.stop(lambda: 1.0))

    assert result["state"] == "DEGRADED"
    assert result["degradedReasons"] == ("WORKER_START_FAILED",)
    assert stopped["workerAlive"] is False
    assert calls == []


def test_product_resource_routes_streamed_agent_task_to_delegate() -> None:
    resources = ProductDiscoveryOwnerResources(inputs=ProductRuntimeInputs())
    resources._owner_thread_id = threading.get_ident()
    delivered: list[dict] = []

    class Delegate:
        async def run_agent_task(self, device_id, text, **kwargs):
            sink = kwargs["event_sink"]
            sink({"type": "assistant.message", "content": "progress"})
            return {
                "success": True,
                "device_id": device_id,
                "text": text,
                "task_id": "0e4ba172-c081-48e9-a9d9-7b5714c98a42",
            }

    resources._delegate = Delegate()  # type: ignore[assignment]
    result = asyncio.run(
        resources.run_agent_task(
            "urn:mclaw:device:oh:" + "a" * 64,
            "work",
            context_id=None,
            message_id="b07eca54-7c35-4c27-833c-c578395cc13e",
            event_sink=lambda event: delivered.append(dict(event)),
        )
    )
    assert result["success"] is True
    assert result["text"] == "work"
    assert delivered == [
        {"type": "assistant.message", "content": "progress"}
    ]


def test_product_resource_routes_same_task_continuation_to_delegate() -> None:
    resources = ProductDiscoveryOwnerResources(inputs=ProductRuntimeInputs())
    resources._owner_thread_id = threading.get_ident()
    calls: list[dict] = []

    class Delegate:
        async def continue_agent_task(
            self,
            device_id,
            task_id,
            input_request_id,
            **kwargs,
        ):
            calls.append(
                {
                    "device_id": device_id,
                    "task_id": task_id,
                    "input_request_id": input_request_id,
                    **kwargs,
                }
            )
            return {
                "success": True,
                "device_id": device_id,
                "task_id": task_id,
                "task_state": "TASK_STATE_COMPLETED",
            }

    resources._delegate = Delegate()  # type: ignore[assignment]
    sink = lambda event: None
    result = asyncio.run(
        resources.continue_agent_task(
            "urn:mclaw:device:oh:" + "a" * 64,
            "0e4ba172-c081-48e9-a9d9-7b5714c98a42",
            "b07eca54-7c35-4c27-833c-c578395cc13e",
            text="",
            message_id="c3e36fe1-e09b-486b-8e03-e8254ce5cf3c",
            event_sink=sink,
            input_paths=("/data/local/tmp/config.txt",),
        )
    )

    assert result["task_state"] == "TASK_STATE_COMPLETED"
    assert calls == [
        {
            "device_id": "urn:mclaw:device:oh:" + "a" * 64,
            "task_id": "0e4ba172-c081-48e9-a9d9-7b5714c98a42",
            "input_request_id": "b07eca54-7c35-4c27-833c-c578395cc13e",
            "text": "",
            "message_id": "c3e36fe1-e09b-486b-8e03-e8254ce5cf3c",
            "event_sink": sink,
            "input_paths": ("/data/local/tmp/config.txt",),
        }
    ]


def test_runtime_shutdown_uses_one_deadline_and_order() -> None:
    from mclaw.cli.runtime.lifecycle import RuntimeShutdownCoordinator, RuntimeShutdownHooks

    calls: list[tuple[str, float | None]] = []
    runtime = InteractiveRuntime()

    class FakeThread:
        def __init__(self, name: str) -> None:
            self.name = name

        def join(self, timeout: float) -> None:
            calls.append((f"join-{self.name}", timeout))

    def mark(name: str):
        return lambda: calls.append((name, None))

    def mark_deadline(name: str):
        return lambda deadline: calls.append((name, deadline)) or True

    coordinator = RuntimeShutdownCoordinator(
        runtime,
        RuntimeShutdownHooks(
            stop_asr_service=mark("asr"),
            interrupt_agent=mark("interrupt"),
            begin_dsoftbus_shutdown=mark("dsoftbus-begin"),
            stop_dsoftbus=mark_deadline("dsoftbus-stop"),
            restore_project_env=mark("env"),
            stop_pet=mark("pet"),
            end_session=lambda flush, deadline: calls.append(
                (f"session-{flush}", deadline)
            ),
            close_active_agent=mark_deadline("agent-close"),
            close_session_db=mark_deadline("db-close"),
            clear_terminal_title=mark("title"),
        ),
        monotonic=lambda: 100.0,
    )

    coordinator.shutdown(FakeThread("process"), FakeThread("animation"))

    names = [name for name, _ in calls]
    assert names == [
        "dsoftbus-begin",
        "asr",
        "interrupt",
        "dsoftbus-stop",
        "join-process",
        "join-animation",
        "env",
        "pet",
        "session-True",
        "agent-close",
        "db-close",
        "title",
    ]
    shared = [
        value
        for name, value in calls
        if name in {"dsoftbus-stop", "session-True", "agent-close", "db-close"}
    ]
    assert shared == [140.0, 140.0, 140.0, 140.0]
    assert runtime.should_exit is True


def test_worker_supervisor_reaps_partial_thread_start(monkeypatch) -> None:
    from mclaw.cli.runtime import workers

    events: list[str] = []

    class FakeThread:
        count = 0

        def __init__(self, *, target, daemon) -> None:
            self.index = FakeThread.count
            FakeThread.count += 1

        def start(self) -> None:
            events.append(f"start-{self.index}")
            if self.index == 1:
                raise RuntimeError("animation start failed")

        def join(self, timeout: float) -> None:
            events.append(f"join-{self.index}")

    monkeypatch.setattr(workers.threading, "Thread", FakeThread)
    runtime = InteractiveRuntime()
    hooks = workers.RuntimeWorkerHooks(
        handle_input=lambda _text: None,
        on_idle=lambda: None,
        on_animation_tick=lambda: None,
        on_error=lambda _error: None,
        invalidate=lambda: None,
        animation_enabled=lambda: False,
        animation_interval=lambda _running: 1.0,
    )

    with pytest.raises(RuntimeError, match="animation start failed"):
        workers.RuntimeWorkerSupervisor(runtime, hooks).start()

    assert runtime.should_exit is True
    assert events == ["start-0", "start-1", "join-0"]


def test_run_interactive_installs_runtime_before_chat_and_copies_toolsets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from mclaw.cli import app
    from mclaw.dsoftbus import entrypoint, product

    calls: list[object] = []
    runtime = SimpleNamespace(
        begin_shutdown=lambda: calls.append("begin"),
        stop=lambda deadline: calls.append(("stop", deadline)),
    )
    configured = ["custom"]

    monkeypatch.setattr(entrypoint, "is_discovery_only_candidate", lambda _config: True)
    monkeypatch.setattr(app, "ensure_workspace_trusted", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        product,
        "create_product_runtime",
        lambda **kwargs: calls.append(("create", kwargs)) or runtime,
    )

    class FakeChat:
        def __init__(self, **kwargs) -> None:
            assert get_active_runtime() is runtime
            assert kwargs["workspace_path"] == str(tmp_path.resolve())
            assert kwargs["enabled_toolsets"] == ["custom", "dsoftbus"]
            assert kwargs["enabled_toolsets"] is not configured
            assert kwargs["dsoftbus_runtime"] is runtime
            calls.append("chat-init")

        def run(self) -> None:
            calls.append("chat-run")

    monkeypatch.setattr(app, "InteractiveChat", FakeChat)
    try:
        app.run_interactive(
            provider_runtime=object(),
            enabled_toolsets=configured,
            config={"dsoftbus": {"enabled": "auto"}},
            workspace_path=str(tmp_path),
        )
    finally:
        clear_active_runtime(runtime)

    assert configured == ["custom"]
    assert calls[1:3] == ["chat-init", "chat-run"]
    assert calls[-2] == "begin"
    assert calls[-1][0] == "stop"


def test_non_kaihong_runtime_constructs_no_endpoint_and_strips_platform_toolsets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from mclaw.cli import app
    from mclaw.dsoftbus import product

    monkeypatch.setattr(app, "ensure_workspace_trusted", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        product,
        "create_product_runtime",
        lambda **_kwargs: pytest.fail("direct mclaw created a DSoftBus endpoint"),
    )
    observed: list[dict[str, object]] = []

    class FakeChat:
        def __init__(self, **kwargs) -> None:
            observed.append(dict(kwargs))

        def run(self) -> None:
            return None

    monkeypatch.setattr(app, "InteractiveChat", FakeChat)
    app.run_interactive(
        provider_runtime=object(),
        enabled_toolsets=["terminal", "dsoftbus", "dsoftbus-remote"],
        config={"dsoftbus": {"enabled": "auto"}},
        workspace_path=str(tmp_path),
    )

    assert len(observed) == 1
    assert observed[0]["enabled_toolsets"] == ["terminal"]
    assert observed[0]["dsoftbus_runtime"] is None
    assert get_active_runtime() is None


def test_providerless_product_runtime_uses_standard_interactive_chat(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from mclaw.cli import app
    from mclaw.dsoftbus import entrypoint, product

    calls: list[object] = []
    runtime = SimpleNamespace(
        begin_shutdown=lambda: calls.append("begin"),
        stop=lambda _deadline: calls.append("stop"),
    )
    unavailable = RuntimeError("provider unavailable")
    monkeypatch.setattr(entrypoint, "is_discovery_only_candidate", lambda _config: True)
    monkeypatch.setattr(app, "ensure_workspace_trusted", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(product, "create_product_runtime", lambda **_kwargs: runtime)
    class FakeChat:
        def __init__(self, **kwargs) -> None:
            assert kwargs["provider_runtime"] is None
            assert kwargs["provider_unavailable_error"] is unavailable
            assert kwargs["dsoftbus_runtime"] is runtime
            calls.append("chat-init")

        def run(self) -> None:
            calls.append("chat-run")

    monkeypatch.setattr(app, "InteractiveChat", FakeChat)
    try:
        app.run_interactive(
            provider_runtime=None,
            config={"dsoftbus": {"enabled": "auto"}},
            workspace_path=str(tmp_path),
            provider_resolution_error=unavailable,
            discovery_only_approved=True,
        )
    finally:
        clear_active_runtime(runtime)

    assert calls == ["chat-init", "chat-run", "begin", "stop"]


def test_interactive_run_cleans_up_when_application_construction_fails() -> None:
    from mclaw.cli.app import InteractiveChat

    chat = InteractiveChat.__new__(InteractiveChat)
    calls: list[tuple[object, ...]] = []
    chat._run_application = lambda: (_ for _ in ()).throw(RuntimeError("banner failed"))
    chat._shutdown_runtime_threads = lambda process, animation: calls.append(
        ("shutdown", process, animation)
    )
    chat._release_session_lock = lambda: calls.append(("release",))

    with pytest.raises(RuntimeError, match="banner failed"):
        chat.run()

    assert calls == [("shutdown", None, None), ("release",)]


def test_session_db_deadline_end_and_close(tmp_path: Path) -> None:
    import time

    from mclaw.state import SessionDB

    db = SessionDB(tmp_path / "state.db")
    db.create_session("deadline-session", "cli")
    deadline = time.monotonic() + 2.0
    assert db.end_session_with_deadline(
        "deadline-session",
        "session_end",
        deadline=deadline,
    )
    assert db.get_session("deadline-session")["end_reason"] == "session_end"
    assert db.close_with_deadline(deadline)


def test_local_tool_shapes_are_closed_before_remote_stage() -> None:
    from mclaw.dsoftbus.active import install_active_runtime
    from mclaw.dsoftbus.runtime import DsoftbusRuntimeError
    from mclaw.dsoftbus.tools import (
        get_device_context_handler,
        list_peers_handler,
        run_agent_task_handler,
    )

    class FakeRuntime:
        def list_peers(self, *, ready_only=False):
            return []

        def diagnostic_snapshot(self):
            return {"lifecycle": {"state": "DEGRADED"}, "resource": {}}

        def get_cached_device_context(self, _device_id):
            raise DsoftbusRuntimeError("PEER_NOT_READY")

        async def arun_agent_task(self, *_args, **_kwargs):
            raise DsoftbusRuntimeError("PEER_NOT_READY")

    runtime = FakeRuntime()
    device_id = "urn:mclaw:device:oh:" + "a" * 64
    install_active_runtime(runtime)
    try:
        peers = json.loads(list_peers_handler({}))
        context = json.loads(
            asyncio.run(get_device_context_handler({"device_id": device_id}))
        )
        message = json.loads(
            asyncio.run(run_agent_task_handler({"device_id": device_id, "text": "hi"}))
        )
    finally:
        clear_active_runtime(runtime)

    assert peers == {
        "_mclawProvenance": {
            "kind": "aggregate",
            "receivedVia": "softbus",
            "source": "mclaw.dsoftbus.runtime",
        },
        "_untrustedRemoteData": False,
        "peers": [],
        "success": True,
    }
    assert context["code"] == "PEER_NOT_READY"
    assert context["device_id"] == device_id
    assert message["code"] == "PEER_NOT_READY"
    assert message["message_id"]


def test_model_transport_and_compressor_close_private_clients_once() -> None:
    from mclaw.agent.context_compressor import ContextCompressor
    from mclaw.agent.transports.base import ModelCallResult, ModelTransport

    class Client:
        def __init__(self) -> None:
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

    class Transport(ModelTransport):
        def call(self, **_kwargs) -> ModelCallResult:
            raise AssertionError("not called")

    client = Client()
    transport = Transport(SimpleNamespace(), client)
    assert transport.close() is True
    assert transport.close() is True
    assert client.close_calls == 1

    summary = Transport(SimpleNamespace(), Client())
    compressor = ContextCompressor.__new__(ContextCompressor)
    compressor._summary_transport = summary
    assert compressor.close() is True
    assert compressor.close() is True
    assert summary.client.close_calls == 1


def test_agent_constructor_failure_closes_partial_transport_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mclaw.agent.core import MClaw

    class Transport:
        def __init__(self) -> None:
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

    transport = Transport()

    def fail_initialize(self, **_kwargs) -> None:
        self.transport = transport
        raise RuntimeError("constructor failpoint")

    monkeypatch.setattr(MClaw, "_initialize", fail_initialize)
    with pytest.raises(RuntimeError, match="constructor failpoint"):
        MClaw(provider_runtime=object())
    assert transport.close_calls == 1


def test_channel_runner_dispose_closes_idle_agents_and_explicit_none_owns_no_db() -> None:
    from mclaw.channels.runner import AgentRunner

    runner_without_db = AgentRunner(
        provider_runtime=object(),
        session_db=None,
        inbound_pipeline=object(),
    )
    assert runner_without_db.session_db is None
    assert runner_without_db._owns_session_db is False

    class Agent:
        def __init__(self) -> None:
            self.close_calls = 0
            self.deadlines: list[float] = []

        def close(self, *, deadline: float) -> bool:
            self.close_calls += 1
            self.deadlines.append(deadline)
            return True

    agent = Agent()
    runner = AgentRunner.__new__(AgentRunner)
    runner._agents = OrderedDict({"session": (agent, 0.0)})
    runner._active_agents = {}
    runner._closing_sessions = {}
    runner._closing_agents = {}
    runner._retiring_sessions = set()
    runner._retiring_agents = {}
    runner._session_tasks = {}
    runner._pending = {}
    runner._ingress_queues = {}
    runner._ingress_generations = {}
    runner._locks = {}
    runner._cancelled_sessions = set()
    runner._event_callbacks = {}
    runner._owns_session_db = False
    runner.session_db = None

    deadline = time.monotonic() + 1.0
    assert asyncio.run(runner.dispose_all(deadline=deadline)) is True
    assert agent.close_calls == 1
    assert agent.deadlines == [deadline]
    assert runner._agents == {}


def test_scheduler_closes_success_and_timeout_agents_after_worker_fence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mclaw.scheduler.runner import SchedulerRunner

    monkeypatch.setattr(
        "mclaw.scheduler.runner.write_run_output",
        lambda **_kwargs: "run.json",
    )

    class Agent:
        def __init__(self, release: threading.Event | None = None) -> None:
            self.release = release
            self.started = threading.Event()
            self.closed = threading.Event()
            self.close_calls = 0

        def run_conversation(self, **_kwargs):
            self.started.set()
            if self.release is not None:
                self.release.wait(3)
            return {"final_response": "done", "completed": True}

        def interrupt(self) -> None:
            pass

        def close(self) -> bool:
            self.close_calls += 1
            self.closed.set()
            return True

    def make_runner(agent: Agent) -> SchedulerRunner:
        runner = SchedulerRunner.__new__(SchedulerRunner)
        runner.output_dir = "."
        runner.config = {}
        runner.print_fn = None
        runner.store = None
        runner.session_db = SimpleNamespace()
        runner.startup_provider_runtime = SimpleNamespace(model="model")
        runner.agent_factory = lambda **_kwargs: agent
        runner._resolve_session = lambda _job, _run: "session"
        runner._history_for_session = lambda _session: []
        runner._effective_toolsets = lambda _job: ["mclaw-required"]
        runner._runtime_for_session = lambda _session: object()
        return runner

    def job(timeout: int) -> SimpleNamespace:
        return SimpleNamespace(
            id="job",
            name="job",
            prompt="work",
            workdir="",
            enabled_toolsets=["mclaw-required"],
            max_iterations=1,
            timeout_seconds=timeout,
        )

    def run_record() -> SimpleNamespace:
        return SimpleNamespace(
            id="run",
            started_at=None,
            scheduled_for=None,
            session_id="",
            final_response="",
            error="",
            finished_at=None,
            status="running",
            tool_calls=[],
            token_usage={},
            output_path="",
        )

    success_agent = Agent()
    success = make_runner(success_agent).run(job(2), run_record())
    assert success.status == "succeeded"
    assert success_agent.close_calls == 1

    release = threading.Event()
    timeout_agent = Agent(release)
    timed_out = make_runner(timeout_agent).run(job(1), run_record())
    assert timed_out.status == "failed"
    assert "timed out" in timed_out.error
    assert timeout_agent.close_calls == 0
    release.set()
    assert timeout_agent.closed.wait(2)
    assert timeout_agent.close_calls == 1


def test_delegate_child_dispose_is_close_then_cleanup_and_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mclaw.tools import delegate_tool
    from mclaw.tools import terminal_tool

    calls: list[str] = []

    class Child:
        session_id = "child-session"

        def close(self) -> bool:
            calls.append("close")
            return True

    monkeypatch.setattr(
        terminal_tool,
        "cleanup_session",
        lambda _session_id: calls.append("terminal-cleanup"),
    )
    child = Child()
    delegate_tool._dispose_child_agent(child)
    delegate_tool._dispose_child_agent(child)
    assert calls == ["close", "terminal-cleanup"]


def test_background_review_closes_clone_private_transport() -> None:
    from mclaw.agent.background_review import spawn_background_review

    clone_closed = threading.Event()
    clones: list[object] = []

    class ReviewAgent:
        def __init__(self, **kwargs) -> None:
            self.provider_runtime = kwargs.get("provider_runtime", object())
            self.enabled_toolsets = []
            self.config = {}
            self.session_id = "session"
            self.system_prompt = "system"
            self.messages = []
            self.tools = []
            self.valid_tool_names = set()
            self._memory_store = None
            self._memory_manager = None
            self._print_fn = None
            if kwargs:
                clones.append(self)

        def run_conversation(self, **_kwargs) -> dict:
            return {"final_response": "done"}

        def close(self) -> bool:
            clone_closed.set()
            return True

        def _record_usage(self, *_args, **_kwargs) -> None:
            pass

        def _emit_event(self, *_args, **_kwargs) -> None:
            pass

    parent = ReviewAgent()
    spawn_background_review(
        parent,
        messages_snapshot=[],
        review_skills=True,
    )
    assert clone_closed.wait(2)
    assert len(clones) == 1


def test_doctor_static_dsoftbus_check_constructs_no_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mclaw import doctor
    from mclaw.dsoftbus import product

    monkeypatch.setattr(
        product,
        "create_product_runtime",
        lambda **_kwargs: pytest.fail("doctor must not construct the Runtime"),
    )
    results = []
    doctor._append_dsoftbus_checks(results, {})
    assert [(item.name, item.ok, item.detail) for item in results] == [
        ("dsoftbus runtime", True, "not enabled; optional")
    ]
