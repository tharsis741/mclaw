# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import base64
import json
import os
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

import pytest

from mclaw.agent.core import MClaw
from mclaw.agent.transports.base import ModelCallError, ModelCallResult
from mclaw.channels.base import ChannelMessage, ChannelSource
from mclaw.channels.runner import AgentRunner
from mclaw.dsoftbus.agent_message import (
    AgentRunnerTurnExecutor,
    ConversationKey,
    RemoteTurnRequest,
    _prepare_remote_agent_policy,
)
from mclaw.dsoftbus.task_artifact import (
    TaskArtifactCollector,
    bind_task_artifact_collector,
    reset_task_artifact_collector,
)
from mclaw.dsoftbus.tools import return_artifact_handler
from mclaw.providers.base import RuntimeProviderProfile
from mclaw.providers.runtime import ProviderRuntimeContext
from mclaw.tools import dispatch as tool_dispatch
from mclaw.tools.toolsets import DSOFTBUS_TOOLS


def _context(model: str = "model-a") -> ProviderRuntimeContext:
    return ProviderRuntimeContext(
        profile=RuntimeProviderProfile(
            name="test-provider",
            display_name="Test Provider",
        ),
        model=model,
        api_key="secret",
        base_url="https://provider.test/api",
    )


def _message(text: str = "hello") -> ChannelMessage:
    return ChannelMessage(
        text=text,
        source=ChannelSource(
            channel="dsoftbus",
            chat_id="peer",
            chat_type="direct",
            user_id="peer",
            message_id="00000000-0000-4000-8000-000000000001",
        ),
        raw_message={},
    )


def _remote_tools_config(*, allow: bool) -> dict[str, Any]:
    return {
        "toolsets": ["terminal", "file", "dsoftbus", "dsoftbus-remote"],
        "tools": {"disabled": ["process"]},
        "dsoftbus": {
            "enabled": "auto",
            "accept_remote_messages": True,
            "allow_remote_tools": allow,
        },
        "checkpoints": {"enabled": True},
        "compression": {"enabled": True},
        "prompt_cache": {"enabled": False},
    }


def _tool_call(name: str, call_id: str = "call-1") -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": "{}"},
    }


def _model_result(
    *,
    content: str = "",
    tool_calls: list[dict[str, Any]] | None = None,
) -> ModelCallResult:
    return ModelCallResult(
        content=content,
        tool_calls=tool_calls,
        finish_reason="tool_calls" if tool_calls else "stop",
        reasoning=None,
        usage=None,
        was_streamed=True,
        provider="test-provider",
        model="model-a",
    )


def test_remote_tool_policy_is_closed_unless_all_three_gates_are_true() -> None:
    config = _remote_tools_config(allow=False)

    policy = _prepare_remote_agent_policy(config)

    assert policy.disable_tools is True
    assert policy.enabled_toolsets == ("dsoftbus-remote",)
    assert policy.tool_definitions == ()
    assert config["checkpoints"]["enabled"] is True
    assert policy.config["checkpoints"]["enabled"] is False
    assert policy.config["compression"]["enabled"] is False

    for field, value in (
        ("enabled", False),
        ("accept_remote_messages", False),
        ("allow_remote_tools", False),
    ):
        candidate = _remote_tools_config(allow=True)
        candidate["dsoftbus"][field] = value
        assert _prepare_remote_agent_policy(candidate).disable_tools is True


def test_remote_tool_policy_copies_local_tools_and_removes_dsoftbus() -> None:
    config = _remote_tools_config(allow=True)

    policy = _prepare_remote_agent_policy(config)
    schema_names = {
        definition["function"]["name"] for definition in policy.tool_definitions
    }

    assert policy.disable_tools is False
    assert policy.enabled_toolsets == ("terminal", "file", "dsoftbus-artifact")
    assert "terminal" in schema_names
    assert "read_file" in schema_names
    assert "return_artifact" in schema_names
    assert "process" not in schema_names
    assert schema_names.isdisjoint(DSOFTBUS_TOOLS)
    assert set(DSOFTBUS_TOOLS).issubset(policy.config["tools"]["disabled"])


def test_remote_executor_applies_the_prepared_tool_policy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, Any] = {}

    class FakeRunner:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    monkeypatch.setattr("mclaw.channels.runner.AgentRunner", FakeRunner)

    AgentRunnerTurnExecutor(
        provider_runtime=_context(),
        config=_remote_tools_config(allow=True),
        workspace_root=tmp_path,
    )

    assert captured["enabled_toolsets"] == [
        "terminal",
        "file",
        "dsoftbus-artifact",
    ]
    assert captured["disable_tools"] is False
    assert captured["call_source"] == "dsoftbus"
    assert captured["session_db"] is None
    assert captured["skip_memory"] is True
    assert set(DSOFTBUS_TOOLS).issubset(captured["config"]["tools"]["disabled"])


def test_return_artifact_collects_json_and_bounded_file_without_remote_path(
    tmp_path: Path,
) -> None:
    collector = TaskArtifactCollector(
        task_id="00000000-0000-4000-8000-000000000011",
        context_id="00000000-0000-4000-8000-000000000012",
    )
    token = bind_task_artifact_collector(collector)
    try:
        data_result = json.loads(
            return_artifact_handler(
                {"name": "summary.json", "data": {"count": 2, "ok": True}}
            )
        )
        local_file = tmp_path / "private-source.txt"
        local_file.write_bytes(b"artifact-bytes")
        parent = type(
            "Parent",
            (),
            {
                "workspace_path": str(tmp_path),
                "valid_tool_names": {"read_file", "return_artifact"},
            },
        )()
        file_result = json.loads(
            return_artifact_handler(
                {
                    "name": "result.bin",
                    "path": "private-source.txt",
                    "media_type": "application/octet-stream",
                },
                parent_agent=parent,
            )
        )
    finally:
        reset_task_artifact_collector(token)

    assert data_result["success"] is True
    assert file_result["success"] is True
    artifacts = collector.snapshot()
    assert artifacts[0]["parts"][0]["data"] == {"count": 2, "ok": True}
    assert base64.b64decode(artifacts[1]["parts"][0]["raw"]) == b"artifact-bytes"
    assert str(local_file) not in json.dumps(artifacts, ensure_ascii=False)
    assert artifacts[1]["parts"][0]["filename"] == "result.bin"


@pytest.mark.asyncio
async def test_remote_executor_returns_task_local_collected_artifact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from mclaw.channels.base import AgentTurnResult

    class FakeRunner:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def handle_message(self, **kwargs: Any) -> AgentTurnResult:
            parent = type(
                "Parent",
                (),
                {"workspace_path": str(tmp_path), "valid_tool_names": {"return_artifact"}},
            )()
            attached = await asyncio.to_thread(
                return_artifact_handler,
                {"name": "remote.json", "data": {"answer": 42}},
                parent_agent=parent,
            )
            assert json.loads(attached)["success"] is True
            return AgentTurnResult(
                session_id=kwargs["session_id"],
                final_response="done",
                raw_result={"completed": True, "messages": []},
            )

        def update_provider_runtime(self, _context: Any) -> None:
            pass

    monkeypatch.setattr("mclaw.channels.runner.AgentRunner", FakeRunner)
    executor = AgentRunnerTurnExecutor(
        provider_runtime=_context(),
        config=_remote_tools_config(allow=True),
        workspace_root=tmp_path,
    )
    result = await executor.execute(
        RemoteTurnRequest(
            conversation_key=ConversationKey(
                peer_device_id="urn:mclaw:device:oh:" + "a" * 64,
                peer_runtime_instance_id="00000000-0000-4000-8000-000000000013",
                context_id="00000000-0000-4000-8000-000000000014",
            ),
            deadline_monotonic=None,
            history=(),
            message_id="00000000-0000-4000-8000-000000000015",
            provider_runtime=_context(),
            text="return structured output",
            task_id="00000000-0000-4000-8000-000000000016",
        )
    )

    assert result["final_response"] == "done"
    assert result["artifacts"][0]["name"] == "remote.json"
    assert result["artifacts"][0]["parts"][0]["data"] == {"answer": 42}


def test_remote_runner_constructor_is_zero_persistence_and_zero_pipeline(
    tmp_path: Path,
) -> None:
    runner = AgentRunner(
        provider_runtime=_context(),
        config={"toolsets": ["mclaw-required"]},
        enabled_toolsets=["dsoftbus-remote"],
        session_db=None,
        platform="dsoftbus",
        agent_system_prompt="remote prompt",
        agent_workspace_root=str(tmp_path),
        skip_memory=True,
        disable_tools=True,
        advance_background_review=False,
        call_source="dsoftbus",
        inbound_pipeline=None,
        apply_inbound_pipeline=False,
    )

    assert runner.session_db is None
    assert runner._owns_session_db is False
    assert runner.inbound_pipeline is None
    assert runner.enabled_toolsets == ["dsoftbus-remote"]
    assert runner.disable_tools is True
    assert runner.skip_memory is True
    assert runner.call_source == "dsoftbus"
    assert list(tmp_path.iterdir()) == []


def test_tool_enabled_remote_agent_creates_private_context_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, Any] = {}

    class FakeAgent:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    monkeypatch.setattr("mclaw.agent.core.MClaw", FakeAgent)
    root = tmp_path / "agent-contexts"
    runner = AgentRunner(
        provider_runtime=_context(),
        config={},
        enabled_toolsets=["terminal"],
        session_db=None,
        platform="dsoftbus",
        agent_workspace_root=str(root),
        skip_memory=True,
        disable_tools=False,
        advance_background_review=False,
        call_source="dsoftbus",
        inbound_pipeline=None,
        apply_inbound_pipeline=False,
    )
    session_id = "dsoftbus:" + "b" * 64

    runner._get_or_create_agent(session_id=session_id)

    workspace = root / ("b" * 32)
    assert captured["workspace"] == str(workspace)
    assert workspace.is_dir()
    if os.name == "posix":
        assert workspace.stat().st_mode & 0o777 == 0o700


@pytest.mark.asyncio
async def test_remote_task_id_crosses_agent_and_tool_thread_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from mclaw.channels.base import AgentTurnResult
    from mclaw.tools.dispatch import get_current_task_id

    observed: list[str] = []

    class FakeRunner:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def bind_session_events(self, **_kwargs: Any) -> None:
            pass

        async def flush_session_events(self, _session_id: str) -> None:
            pass

        def unbind_session_events(self, _session_id: str) -> None:
            pass

        async def handle_message(self, **kwargs: Any) -> AgentTurnResult:
            observed.append(get_current_task_id())
            observed.append(await asyncio.to_thread(get_current_task_id))
            return AgentTurnResult(
                session_id=kwargs["session_id"],
                final_response="done",
                raw_result={"completed": True, "messages": []},
            )

        def update_provider_runtime(self, _context: Any) -> None:
            pass

    monkeypatch.setattr("mclaw.channels.runner.AgentRunner", FakeRunner)
    executor = AgentRunnerTurnExecutor(
        provider_runtime=_context(),
        config=_remote_tools_config(allow=True),
        workspace_root=tmp_path,
    )
    task_id = "00000000-0000-4000-8000-000000000099"
    request = RemoteTurnRequest(
        conversation_key=ConversationKey(
            peer_device_id="urn:mclaw:device:oh:" + "c" * 64,
            peer_runtime_instance_id="00000000-0000-4000-8000-000000000097",
            context_id="00000000-0000-4000-8000-000000000098",
        ),
        deadline_monotonic=None,
        history=(),
        message_id="00000000-0000-4000-8000-000000000096",
        provider_runtime=_context(),
        text="run",
        task_id=task_id,
        event_sink=lambda _event: asyncio.sleep(0),
    )

    await executor.execute(request)

    assert observed == [task_id, task_id]
    assert get_current_task_id() == ""


@pytest.mark.asyncio
async def test_remote_cancel_reaps_exact_scope_before_reporting_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mclaw.tools.process_registry import process_registry

    runner = AgentRunner.__new__(AgentRunner)
    runner.call_source = "dsoftbus"
    interrupted: list[str] = []
    runner.interrupt = lambda session_id: (interrupted.append(session_id), True)[1]
    runner._session_is_idle = lambda _session_id: True
    scopes: list[tuple[str | None, str | None]] = []

    def terminate_scope(*, task_id=None, session_key=None):
        scopes.append((task_id, session_key))
        return {"termination_confirmed": True}

    monkeypatch.setattr(process_registry, "terminate_scope", terminate_scope)

    assert await runner.cancel_and_reap(
        "dsoftbus:session",
        task_id="task-id",
        deadline=time.monotonic() + 1,
    )
    assert interrupted == ["dsoftbus:session"]
    assert scopes == [
        ("task-id", "dsoftbus:session"),
        ("task-id", "dsoftbus:session"),
    ]


@pytest.mark.asyncio
async def test_direct_path_never_enters_channel_pending_fifo() -> None:
    runner = AgentRunner.__new__(AgentRunner)
    runner._locks = {"session": asyncio.Lock()}
    runner._pending = {}
    runner._ingress_queues = {}
    runner._ingress_generations = {}
    runner._active_agents = {}
    runner._closing_sessions = {}
    runner._closing_agents = {}
    runner._session_tasks = {}
    runner._cancelled_sessions = set()
    runner.max_pending_messages = 8
    await runner._locks["session"].acquire()
    callbacks: list[Any] = []

    result = await runner.handle_message(
        message=_message(),
        session_id="session",
        conversation_history=None,
        reply_callback=lambda *_args: callbacks.append(_args),
        enqueue_if_busy=False,
    )

    assert result.error == "RUNNER_BUSY"
    assert result.raw_result == {"runner_busy": True}
    assert runner._pending == {}
    assert callbacks == []
    runner._locks["session"].release()


@pytest.mark.asyncio
async def test_remote_turn_pins_flags_history_deadline_and_cancel_event() -> None:
    captured: dict[str, Any] = {}
    cancel_event = threading.Event()

    class FakeAgent:
        provider_runtime = _context()

        def begin_turn(self) -> threading.Event:
            return cancel_event

        def end_turn(self, event: threading.Event) -> None:
            captured["ended"] = event

        def run_conversation(self, **kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {
                "completed": True,
                "final_response": "done",
                "messages": [{"role": "assistant", "content": "done"}],
            }

        def _has_outstanding_turn_workers(self) -> bool:
            return False

    runner = AgentRunner.__new__(AgentRunner)
    runner.disable_tools = True
    runner.advance_background_review = False
    runner.call_source = "dsoftbus"
    runner._active_agents = {}
    runner._cancelled_sessions = set()
    runner._agents = OrderedDict()
    runner._retiring_sessions = set()
    runner._retiring_agents = {}
    runner._get_or_create_agent = lambda **_kwargs: FakeAgent()
    history = [{"role": "user", "content": "old"}]
    deadline = time.monotonic() + 5.0

    result = await runner._run_single_turn(
        message=_message(),
        session_id="dsoftbus:abc",
        conversation_history=history,
        deadline_monotonic=deadline,
    )

    assert result.final_response == "done"
    assert captured["disable_tools"] is True
    assert captured["advance_background_review"] is False
    assert captured["call_source"] == "dsoftbus"
    assert captured["deadline_monotonic"] == deadline
    assert captured["cancel_event"] is cancel_event
    assert captured["conversation_history"] == history
    assert captured["conversation_history"] is not history
    assert captured["ended"] is cancel_event


def test_provider_update_retires_only_idle_mismatched_agent() -> None:
    first = _context("model-a")
    second = _context("model-b")

    class FakeAgent:
        def __init__(self) -> None:
            self.provider_runtime = first
            self.closed = 0

        def close(self, *, deadline: float) -> bool:
            self.closed += 1
            return True

        def _has_outstanding_turn_workers(self) -> bool:
            return False

    idle = FakeAgent()
    active = FakeAgent()
    runner = AgentRunner.__new__(AgentRunner)
    runner.startup_provider_runtime = first
    runner._agents = OrderedDict(
        (("idle", (idle, 0.0)), ("active", (active, 0.0)))
    )
    runner._active_agents = {"active": active}
    runner._closing_sessions = {}
    runner._closing_agents = {}
    runner._session_tasks = {}
    runner._pending = {}
    runner._ingress_queues = {}
    runner._ingress_generations = {}
    runner._locks = {}
    runner._cancelled_sessions = set()
    runner._event_callbacks = {}
    runner._retiring_sessions = set()
    runner._retiring_agents = {}

    retired = runner.update_provider_runtime(second)

    assert retired == 1
    assert idle.closed == 1
    assert "idle" not in runner._agents
    assert active.closed == 0
    assert "active" in runner._agents
    assert "active" in runner._retiring_sessions
    assert runner.startup_provider_runtime is second


@pytest.mark.asyncio
async def test_forget_and_dispose_return_all_session_containers_to_zero() -> None:
    class FakeAgent:
        def __init__(self) -> None:
            self.closed = 0

        def close(self, *, deadline: float) -> bool:
            self.closed += 1
            return True

        @staticmethod
        def _has_outstanding_turn_workers() -> bool:
            return False

    runner = AgentRunner.__new__(AgentRunner)
    runner.call_source = "dsoftbus"
    runner.session_db = None
    runner._owns_session_db = False
    runner._agents = OrderedDict()
    runner._active_agents = {}
    runner._closing_sessions = {}
    runner._closing_agents = {}
    runner._session_tasks = {}
    runner._pending = {}
    runner._ingress_queues = {}
    runner._ingress_generations = {}
    runner._locks = {}
    runner._cancelled_sessions = set()
    runner._event_callbacks = {}
    runner._retiring_sessions = set()
    runner._retiring_agents = {}
    agents: list[FakeAgent] = []
    for index in range(40):
        session_id = f"dsoftbus:{index:064x}"
        agent = FakeAgent()
        agents.append(agent)
        runner._agents[session_id] = (agent, 0.0)
        runner._locks[session_id] = asyncio.Lock()
        runner._event_callbacks[session_id] = (
            asyncio.get_running_loop(),
            lambda *_args: None,
        )

    assert await runner.dispose_all(
        deadline=time.monotonic() + 1.0,
        close_owned_session_db=False,
    )
    assert all(agent.closed == 1 for agent in agents)
    for name in (
        "_agents",
        "_active_agents",
        "_closing_sessions",
        "_closing_agents",
        "_session_tasks",
        "_pending",
        "_ingress_queues",
        "_ingress_generations",
        "_locks",
        "_event_callbacks",
        "_retiring_agents",
        "_retiring_sessions",
    ):
        assert not getattr(runner, name), name


@pytest.mark.asyncio
async def test_forget_session_retains_agent_until_lock_fence_releases() -> None:
    class FakeAgent:
        closed = 0

        def close(self, *, deadline: float) -> bool:
            self.closed += 1
            return True

        @staticmethod
        def _has_outstanding_turn_workers() -> bool:
            return False

    session_id = "dsoftbus:" + "a" * 64
    agent = FakeAgent()
    lock = asyncio.Lock()
    await lock.acquire()
    runner = AgentRunner.__new__(AgentRunner)
    runner.call_source = "dsoftbus"
    runner._agents = OrderedDict(((session_id, (agent, 0.0)),))
    runner._active_agents = {}
    runner._closing_sessions = {}
    runner._closing_agents = {}
    runner._session_tasks = {}
    runner._pending = {}
    runner._ingress_queues = {}
    runner._ingress_generations = {}
    runner._locks = {session_id: lock}
    runner._cancelled_sessions = set()
    runner._event_callbacks = {}
    runner._retiring_sessions = set()
    runner._retiring_agents = {}

    assert not await runner.forget_session(
        session_id,
        deadline=time.monotonic() + 0.01,
    )
    assert session_id in runner._agents
    assert session_id in runner._retiring_sessions
    assert agent.closed == 0

    lock.release()
    assert await runner.forget_session(
        session_id,
        deadline=time.monotonic() + 1.0,
    )
    assert agent.closed == 1
    assert session_id not in runner._agents
    assert session_id not in runner._locks
    assert session_id not in runner._retiring_sessions


def test_zero_tool_turn_rejects_provider_tool_calls_before_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[dict[str, Any]] = []

    class FakeTransport:
        def call(self, **_kwargs: Any) -> ModelCallResult:
            return ModelCallResult(
                content="",
                tool_calls=[
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "terminal", "arguments": "{}"},
                    }
                ],
                finish_reason="tool_calls",
                reasoning=None,
                usage=None,
                was_streamed=True,
                provider="test-provider",
                model="model-a",
            )

        def close(self) -> bool:
            return True

    monkeypatch.setattr("mclaw.agent.core.create_transport", lambda _context: FakeTransport())
    agent = MClaw(
        provider_runtime=_context(),
        session_db=None,
        enabled_toolsets=["dsoftbus-remote"],
        platform="dsoftbus",
        system_prompt="remote prompt",
        skip_memory=True,
        config={
            "checkpoints": {"enabled": False},
            "compression": {"enabled": False},
            "prompt_cache": {"enabled": False},
        },
        event_callback=events.append,
    )

    result = agent.run_conversation(
        "task",
        disable_tools=True,
        advance_background_review=False,
        call_source="dsoftbus",
        deadline_monotonic=time.monotonic() + 5.0,
    )

    assert result["completed"] is False
    assert result["error"] == "AGENT_TOOLS_FORBIDDEN"
    assert result["assistant_rounds"] == []
    assert events == []
    assert all("tool_calls" not in message for message in result["messages"])


def test_remote_tool_schema_hides_dsoftbus_and_forged_call_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport_calls: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    dispatch_count = 0

    class FakeTransport:
        def call(self, **kwargs: Any) -> ModelCallResult:
            transport_calls.append(kwargs)
            return _model_result(tool_calls=[_tool_call("dsoftbus_run_agent_task")])

        @staticmethod
        def close() -> bool:
            return True

    def forbidden_dispatch(**_kwargs: Any) -> list[str]:
        nonlocal dispatch_count
        dispatch_count += 1
        return [json.dumps({"success": True})]

    monkeypatch.setattr("mclaw.agent.core.create_transport", lambda _context: FakeTransport())
    monkeypatch.setattr(
        "mclaw.tools.dispatch.handle_function_calls",
        forbidden_dispatch,
    )
    config = _remote_tools_config(allow=True)
    config["checkpoints"]["enabled"] = False
    config["compression"]["enabled"] = False
    agent = MClaw(
        provider_runtime=_context(),
        session_db=None,
        enabled_toolsets=["terminal", "dsoftbus"],
        platform="dsoftbus",
        system_prompt="remote prompt",
        skip_memory=True,
        config=config,
        event_callback=events.append,
    )

    result = agent.run_conversation(
        "task",
        disable_tools=False,
        advance_background_review=False,
        call_source="dsoftbus",
        deadline_monotonic=time.monotonic() + 5.0,
    )

    schema_names = {
        item["function"]["name"] for item in transport_calls[0]["tools"]
    }
    assert "terminal" in schema_names
    assert schema_names.isdisjoint(DSOFTBUS_TOOLS)
    assert result["completed"] is False
    assert result["error"] == "AGENT_TOOLS_FORBIDDEN"
    assert result["assistant_rounds"] == []
    assert dispatch_count == 0
    assert events == []


def test_remote_non_dsoftbus_tool_reaches_dispatch_and_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport_calls: list[dict[str, Any]] = []
    dispatched: list[dict[str, Any]] = []

    class FakeTransport:
        def call(self, **kwargs: Any) -> ModelCallResult:
            transport_calls.append(kwargs)
            if len(transport_calls) == 1:
                return _model_result(tool_calls=[_tool_call("terminal")])
            return _model_result(content="done")

        @staticmethod
        def close() -> bool:
            return True

    def local_dispatch(**kwargs: Any) -> list[str]:
        dispatched.append(kwargs)
        return [json.dumps({"success": True, "stdout": "ok"})]

    monkeypatch.setattr("mclaw.agent.core.create_transport", lambda _context: FakeTransport())
    monkeypatch.setattr(
        "mclaw.tools.dispatch.handle_function_calls",
        local_dispatch,
    )
    config = _remote_tools_config(allow=True)
    config["checkpoints"]["enabled"] = False
    config["compression"]["enabled"] = False
    agent = MClaw(
        provider_runtime=_context(),
        session_db=None,
        enabled_toolsets=["terminal", "dsoftbus"],
        platform="dsoftbus",
        system_prompt="remote prompt",
        skip_memory=True,
        config=config,
    )

    result = agent.run_conversation(
        "task",
        disable_tools=False,
        advance_background_review=False,
        call_source="dsoftbus",
        deadline_monotonic=time.monotonic() + 5.0,
    )

    assert result["completed"] is True
    assert result["final_response"] == "done"
    assert len(transport_calls) == 2
    assert len(dispatched) == 1
    assert dispatched[0]["call_source"] == "dsoftbus"
    assert dispatched[0]["calls"][0]["function"]["name"] == "terminal"
    assert set(dispatched[0]["tool_names"]).isdisjoint(DSOFTBUS_TOOLS)


def test_dispatch_boundary_blocks_inbound_dsoftbus_but_not_local_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry_calls: list[str] = []

    def registry_dispatch(tool_name: str, _arguments: dict, **_kwargs: Any) -> str:
        registry_calls.append(tool_name)
        return json.dumps({"success": True})

    monkeypatch.setattr(tool_dispatch.registry, "dispatch", registry_dispatch)
    call = _tool_call("dsoftbus_list_peers")

    [blocked] = tool_dispatch.handle_function_calls(
        calls=[call],
        tool_names=set(DSOFTBUS_TOOLS),
        call_source="dsoftbus",
    )
    blocked_value = json.loads(blocked)
    assert blocked_value["code"] == "AGENT_TOOLS_FORBIDDEN"
    assert blocked_value["success"] is False
    assert registry_calls == []

    [allowed] = tool_dispatch.handle_function_calls(
        calls=[call],
        tool_names=set(DSOFTBUS_TOOLS),
        call_source="turn",
    )
    assert json.loads(allowed)["success"] is True
    assert registry_calls == ["dsoftbus_list_peers"]


def test_remote_turn_logs_only_stable_error_type_and_content_lengths(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    user_secret = "REMOTE-USER-SENTINEL-4a70"
    provider_secret = "PROVIDER-SECRET-SENTINEL-9d31"

    class FailingTransport:
        def call(self, **_kwargs: Any) -> ModelCallResult:
            raise ModelCallError(
                message=provider_secret,
                provider="test-provider",
                model="model-a",
            )

        @staticmethod
        def close() -> bool:
            return True

    monkeypatch.setattr(
        "mclaw.agent.core.create_transport",
        lambda _context: FailingTransport(),
    )
    agent = MClaw(
        provider_runtime=_context(),
        session_db=None,
        enabled_toolsets=["dsoftbus-remote"],
        platform="dsoftbus",
        system_prompt="remote prompt",
        skip_memory=True,
        config={
            "checkpoints": {"enabled": False},
            "compression": {"enabled": False},
            "prompt_cache": {"enabled": False},
        },
    )

    with caplog.at_level("INFO"):
        result = agent.run_conversation(
            user_secret,
            disable_tools=True,
            advance_background_review=False,
            call_source="dsoftbus",
            deadline_monotonic=time.monotonic() + 5.0,
        )

    assert result["completed"] is False
    assert result["error"] == "PROVIDER_ERROR"
    assert user_secret not in caplog.text
    assert provider_secret not in caplog.text
    assert "source=dsoftbus" in caplog.text
    assert "type=ModelCallError" in caplog.text


def test_remote_core_retry_backoff_ends_as_deadline_exceeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    class RetryTransport:
        def call(self, **_kwargs: Any) -> ModelCallResult:
            nonlocal calls
            calls += 1
            raise ModelCallError(
                message="retryable",
                provider="test-provider",
                model="model-a",
                retryable=True,
            )

        @staticmethod
        def close() -> bool:
            return True

    monkeypatch.setattr(
        "mclaw.agent.core.create_transport",
        lambda _context: RetryTransport(),
    )
    monkeypatch.setattr("mclaw.agent.core.jittered_backoff", lambda _attempt: 1.0)
    agent = MClaw(
        provider_runtime=_context(),
        session_db=None,
        enabled_toolsets=["dsoftbus-remote"],
        platform="dsoftbus",
        system_prompt="remote prompt",
        skip_memory=True,
        config={
            "checkpoints": {"enabled": False},
            "compression": {"enabled": False},
            "prompt_cache": {"enabled": False},
        },
    )

    result = agent.run_conversation(
        "task",
        disable_tools=True,
        advance_background_review=False,
        call_source="dsoftbus",
        deadline_monotonic=time.monotonic() + 0.02,
    )

    assert calls == 1
    assert result["completed"] is False
    assert result["deadline_exceeded"] is True
    assert result["error"] == "DEADLINE_EXCEEDED"


def test_model_options_expose_remote_fence_fields() -> None:
    from mclaw.agent.transports.base import ModelCallOptions, ModelTransport

    options = ModelCallOptions(
        deadline_monotonic=1.0,
        response_utf8_max_bytes=2,
        stream_queue_max_items=3,
        stream_queue_max_bytes=4,
        stream_accumulator_max_bytes=5,
        register_worker=lambda _worker: None,
        unregister_worker=lambda _worker: None,
    )

    assert options.deadline_monotonic == 1.0
    assert options.response_utf8_max_bytes == 2
    assert options.stream_queue_max_items == 3
    assert options.stream_queue_max_bytes == 4
    assert options.stream_accumulator_max_bytes == 5
    assert ModelTransport.supports_dsoftbus_remote_fence is False


def test_remote_tool_result_allows_followup_tools_in_the_same_user_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote_result = json.dumps(
        {
            "_mclawProvenance": {
                "kind": "aggregate",
                "receivedVia": "softbus",
                "source": "mclaw.dsoftbus.runtime",
            },
            "_untrustedRemoteData": True,
            "peers": [{"displayName": "untrusted"}],
            "success": True,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    dispatch_count = 0

    def dispatch(**_kwargs: Any) -> list[str]:
        nonlocal dispatch_count
        dispatch_count += 1
        return [remote_result]

    monkeypatch.setattr("mclaw.tools.dispatch.handle_function_calls", dispatch)

    class Checkpoints:
        count = 0

        @classmethod
        def new_turn(cls) -> None:
            cls.count += 1

    agent = MClaw.__new__(MClaw)
    agent.session_id = "local-session"
    agent._delegate_depth = 0
    agent._turn_cancel_event = threading.Event()
    agent._get_checkpoint_manager = lambda: Checkpoints()
    agent.config = {"tools": {"disabled": []}}
    agent.valid_tool_names = {
        "dsoftbus_get_device_context",
        "dsoftbus_list_peers",
        "dsoftbus_run_agent_task",
    }
    agent._tool_callback = None
    agent._tool_end_callback = None
    agent._status_callback = None
    agent._memory_manager = None
    agent._session_db = None
    agent._checkpoint_turn_id = None
    agent._tool_operation_ids = {}

    messages: list[dict[str, Any]] = []
    agent._execute_tool_calls(
        [
            {
                "id": "remote-call",
                "type": "function",
                "function": {
                    "name": "dsoftbus_list_peers",
                    "arguments": "{}",
                },
            }
        ],
        messages,
    )
    assert dispatch_count == 1

    agent._execute_tool_calls(
        [
            {
                "id": "context-call",
                "type": "function",
                "function": {
                    "name": "dsoftbus_get_device_context",
                    "arguments": "{}",
                },
            }
        ],
        messages,
    )
    assert dispatch_count == 2

    agent._execute_tool_calls(
        [
            {
                "id": "task-call",
                "type": "function",
                "function": {
                    "name": "dsoftbus_run_agent_task",
                    "arguments": "{}",
                },
            }
        ],
        messages,
    )
    assert dispatch_count == 3
    assert Checkpoints.count == 3
    assert all(
        "REMOTE_DATA_REQUIRES_CONFIRMATION" not in str(message.get("content", ""))
        for message in messages
    )
