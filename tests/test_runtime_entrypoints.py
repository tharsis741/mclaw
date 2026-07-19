# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from mclaw.agent.transports.base import ModelCallResult, ReasoningTrace
from mclaw.agent.usage import UsageRecord
from mclaw.channels.base import ChannelMessage, ChannelSource
from mclaw.channels.runner import AgentRunner
from mclaw.providers.registry import PROVIDER_REGISTRY
from mclaw.providers.runtime import ProviderRuntimeContext
from mclaw.scheduler.runner import SchedulerRunner
from mclaw.state import SessionDB


def _context(
    provider: str = "openai",
    model: str = "gpt-5.4",
    api_key: str = "parent-secret",
) -> ProviderRuntimeContext:
    profile = PROVIDER_REGISTRY[provider]
    return ProviderRuntimeContext(
        profile=profile,
        model=model,
        api_key=api_key,
        base_url=profile.base_url,
        auth_source="explicit",
    )


def test_scheduler_restores_persisted_context_and_reports_turn_usage(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db = SessionDB(tmp_path / "state.db")
    startup = _context(model="gpt-5.4")
    persisted = _context(model="gpt-5.6")
    db.create_session("scheduled-session", "scheduler", model=persisted.model)
    db.update_model_config(
        "scheduled-session",
        model=persisted.model,
        model_config=persisted.snapshot(),
    )
    captured: dict[str, object] = {}

    class FakeAgent:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

        def run_conversation(self, **kwargs):
            captured["call"] = kwargs
            return {
                "final_response": "done",
                "completed": True,
                "token_usage": {"input_tokens": 11, "output_tokens": 4},
                "api_calls": 2,
            }

    monkeypatch.setattr("mclaw.scheduler.runner.write_run_output", lambda **_kwargs: "run.md")
    runner = SchedulerRunner(
        provider_runtime=startup,
        session_db=db,
        output_dir=str(tmp_path),
        agent_factory=lambda **kwargs: FakeAgent(**kwargs),
    )
    job = SimpleNamespace(
        id="job-1",
        name="job",
        prompt="work",
        session_policy="task_thread",
        session_id="scheduled-session",
        workdir="",
        enabled_toolsets=["mclaw-required"],
        max_iterations=3,
        timeout_seconds=10,
    )
    run = SimpleNamespace(
        id="run-1",
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
    try:
        result = runner.run(job, run)
        runtime = captured["provider_runtime"]
        assert isinstance(runtime, ProviderRuntimeContext)
        assert runtime.model == "gpt-5.6"
        assert "call_source" not in captured["call"]
        assert result.token_usage == {
            "input_tokens": 11,
            "output_tokens": 4,
            "api_calls": 2,
        }
    finally:
        db.close()


def test_channel_bootstraps_row_model_and_replaces_changed_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db = SessionDB(tmp_path / "state.db")
    startup = _context(model="gpt-startup")
    db.create_session("channel-session", "channel", model="gpt-session")
    created: list[object] = []

    class FakeAgent:
        def __init__(self, *, provider_runtime, **_kwargs) -> None:
            self.provider_runtime = provider_runtime
            created.append(self)

        def interrupt(self) -> None:
            pass

    monkeypatch.setattr("mclaw.agent.core.MClaw", FakeAgent)
    runner = AgentRunner(provider_runtime=startup, session_db=db)
    try:
        first = runner._get_or_create_agent(session_id="channel-session")
        assert first.provider_runtime.model == "gpt-session"

        changed = _context(model="gpt-reconfigured")
        db.update_model_config(
            "channel-session",
            model=changed.model,
            model_config=changed.snapshot(),
        )
        second = runner._get_or_create_agent(session_id="channel-session")
        assert second is not first
        assert second.provider_runtime.model == "gpt-reconfigured"
        assert len(created) == 2
    finally:
        db.close()


def test_channel_rejects_corrupt_persisted_snapshot_before_cache_lookup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db = SessionDB(tmp_path / "state.db")
    db.create_session("broken-session", "channel", model="gpt-5.4")
    db._conn.execute(
        "UPDATE sessions SET model_config = ? WHERE id = ?",
        ("not-json", "broken-session"),
    )
    db._conn.commit()
    monkeypatch.setattr("mclaw.agent.core.MClaw", object)
    runner = AgentRunner(provider_runtime=_context(), session_db=db)
    try:
        with pytest.raises(ValueError, match="broken-session"):
            runner._get_or_create_agent(session_id="broken-session")
    finally:
        db.close()


def test_channel_turn_returns_session_scoped_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db = SessionDB(tmp_path / "state.db")
    db.create_session("broken-turn", "channel", model="gpt-5.4")
    db._conn.execute(
        "UPDATE sessions SET model_config = ? WHERE id = ?",
        ("not-json", "broken-turn"),
    )
    db._conn.commit()
    monkeypatch.setattr("mclaw.agent.core.MClaw", object)
    runner = AgentRunner(provider_runtime=_context(), session_db=db)
    message = ChannelMessage(
        text="hello",
        source=ChannelSource(channel="test", chat_id="chat"),
    )
    try:
        result = asyncio.run(
            runner._run_single_turn(
                message=message,
                session_id="broken-turn",
                conversation_history=None,
            )
        )
        assert result.session_id == "broken-turn"
        assert "broken-turn" in (result.error or "")
    finally:
        db.close()


def test_delegation_resolves_target_provider_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from mclaw.runtime.manager import RuntimeManager
    from mclaw.tools import delegate_tool

    monkeypatch.setenv("DEEPSEEK_API_KEY", "target-secret")
    monkeypatch.setattr(
        "mclaw.tools.dispatch.get_tool_definitions",
        lambda **_kwargs: (
            [{"type": "function", "function": {"name": "terminal", "parameters": {}}}],
            ["terminal"],
        ),
    )
    monkeypatch.setattr("mclaw.tools.dispatch.get_toolset_for_tool", lambda _name: "terminal")
    paths = SimpleNamespace(
        delegation_root=lambda: tmp_path / "delegations",
    )
    monkeypatch.setattr(
        RuntimeManager,
        "current",
        classmethod(lambda _cls, _config=None: SimpleNamespace(paths=paths)),
    )
    class FakeChild:
        def __init__(self, **kwargs) -> None:
            self.__dict__.update(kwargs)

    monkeypatch.setattr("mclaw.agent.core.MClaw", FakeChild)
    parent = SimpleNamespace(
        provider_runtime=_context(),
        config={
            "delegation": {
                "provider": "deepseek",
            }
        },
        enabled_toolsets=["terminal"],
        valid_tool_names={"terminal"},
        session_id="parent",
        workspace_path=str(tmp_path),
        _session_db=None,
        _delegate_depth=0,
    )

    child = delegate_tool._build_child_agent(
        task_index=0,
        goal="inspect",
        context=None,
        toolsets=["terminal"],
        max_iterations=2,
        parent_agent=parent,
    )
    assert child.provider_runtime.provider == "deepseek"
    assert child.provider_runtime.model == "deepseek-v4-pro"
    assert child.provider_runtime.api_key == "target-secret"
    assert child.provider_runtime.api_key != parent.provider_runtime.api_key

    parent.config.update(
        {
            "active_provider": "deepseek",
            "model": "deepseek-v4-pro",
            "delegation": {"provider": "auto", "model": "", "base_url": ""},
        }
    )
    inherited = delegate_tool._build_child_agent(
        task_index=1,
        goal="inherit",
        context=None,
        toolsets=["terminal"],
        max_iterations=2,
        parent_agent=parent,
    )
    assert inherited.provider_runtime is parent.provider_runtime

    parent.config["delegation"] = {
        "provider": "auto",
        "model": "gpt-5.6",
        "base_url": "",
    }
    model_override = delegate_tool._build_child_agent(
        task_index=2,
        goal="override model",
        context=None,
        toolsets=["terminal"],
        max_iterations=2,
        parent_agent=parent,
    )
    assert model_override.provider_runtime.provider == "openai"
    assert model_override.provider_runtime.model == "gpt-5.6"
    assert model_override.provider_runtime.api_key == "parent-secret"

    parent.config["delegation"] = {
        "provider": "auto",
        "model": "local-model",
        "base_url": "http://localhost:9000/v1",
    }
    custom_endpoint = delegate_tool._build_child_agent(
        task_index=3,
        goal="override endpoint",
        context=None,
        toolsets=["terminal"],
        max_iterations=2,
        parent_agent=parent,
    )
    assert custom_endpoint.provider_runtime.provider == "custom"
    assert custom_endpoint.provider_runtime.api_mode == parent.provider_runtime.api_mode
    assert custom_endpoint.provider_runtime.model == "local-model"
    assert custom_endpoint.provider_runtime.api_key == "parent-secret"


def test_subagent_relative_file_paths_use_inherited_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from mclaw.tools import file_tools

    child = SimpleNamespace(
        workspace_path=str(tmp_path),
        session_id="child-files",
    )
    captured: dict[str, str] = {}
    monkeypatch.setattr(
        file_tools,
        "read_file_tool",
        lambda **kwargs: captured.setdefault("path", kwargs["path"]),
    )

    file_tools._handle_read_file(
        {"path": "src/main.py"},
        parent_agent=child,
    )

    assert Path(captured["path"]) == tmp_path / "src" / "main.py"


def test_subagent_inherits_parent_terminal_cwd(tmp_path: Path) -> None:
    from mclaw.tools import delegate_tool, terminal_tool

    workspace = tmp_path / "project"
    terminal_cwd = workspace / "backend"
    terminal_cwd.mkdir(parents=True)
    parent = SimpleNamespace(
        session_id="parent-terminal-cwd",
        workspace_path=str(workspace),
    )
    terminal_tool._env_registry[parent.session_id] = terminal_tool.RuntimeTerminalSession(
        cwd=str(terminal_cwd)
    )
    try:
        assert delegate_tool._resolve_working_directory(parent) == str(terminal_cwd)
        terminal_tool._env_registry[parent.session_id].cwd = str(workspace / "missing")
        assert delegate_tool._resolve_working_directory(parent) == str(workspace)
    finally:
        terminal_tool._env_registry.pop(parent.session_id, None)


def test_subagent_terminal_cwd_is_inherited_and_session_local(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from mclaw.tools import terminal_tool
    from mclaw.tools.dispatch import set_tool_context

    first_workspace = tmp_path / "first"
    second_workspace = tmp_path / "second"
    (first_workspace / "sub").mkdir(parents=True)
    second_workspace.mkdir()
    calls: list[tuple[str, str]] = []

    class FakeRuntime:
        def exec(self, command: str, *, cwd: str, **_kwargs):
            calls.append((command, str(cwd)))
            next_cwd = Path(cwd) / "sub" if command == "cd sub" else Path(cwd)
            return SimpleNamespace(output="", returncode=0, cwd=str(next_cwd))

    monkeypatch.setattr(
        terminal_tool.RuntimeManager,
        "current",
        classmethod(lambda _cls: FakeRuntime()),
    )
    terminal_tool._env_registry.clear()
    try:
        first = SimpleNamespace(
            workspace_path=str(first_workspace),
            session_id="child-terminal-1",
            config={},
        )
        second = SimpleNamespace(
            workspace_path=str(second_workspace),
            session_id="child-terminal-2",
            config={},
        )

        set_tool_context(session_id=first.session_id)
        terminal_tool._handle_terminal({"command": "cd sub"}, parent_agent=first)
        terminal_tool._handle_terminal({"command": "pwd"}, parent_agent=first)

        set_tool_context(session_id=second.session_id)
        terminal_tool._handle_terminal({"command": "pwd"}, parent_agent=second)
    finally:
        terminal_tool._env_registry.clear()
        set_tool_context(session_id="")

    assert calls == [
        ("cd sub", str(first_workspace)),
        ("pwd", str(first_workspace / "sub")),
        ("pwd", str(second_workspace)),
    ]


def test_background_review_inherits_context_and_forwards_each_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mclaw.agent import background_review

    created: list[object] = []

    class FakeReviewAgent:
        def __init__(self, *, provider_runtime, usage_sink=None, **_kwargs) -> None:
            self.provider_runtime = provider_runtime
            self.usage_sink = usage_sink
            self.messages = []
            self.tools = []
            self.valid_tool_names = set()
            self.recorded: list[tuple[UsageRecord, bool]] = []
            self.enabled_toolsets = []
            self.config = {}
            self.session_id = "session"
            self.system_prompt = "system"
            created.append(self)

        def _record_usage(self, record, include_in_turn=True) -> None:
            self.recorded.append((record, include_in_turn))

        def run_conversation(self, **kwargs):
            self.call = kwargs
            if self.usage_sink:
                self.usage_sink(UsageRecord(input_tokens=7, source="background_review"))
            return {"completed": True}

    class ImmediateThread:
        def __init__(self, *, target, **_kwargs) -> None:
            self.target = target

        def start(self) -> None:
            self.target()

    monkeypatch.setattr(background_review.threading, "Thread", ImmediateThread)
    parent = FakeReviewAgent(provider_runtime=_context())
    background_review.spawn_background_review(
        parent,
        messages_snapshot=[{"role": "system", "content": "system"}],
        review_skills=True,
    )

    review = created[-1]
    assert review.provider_runtime is parent.provider_runtime
    assert review.call["call_source"] == "background_review"
    assert parent.recorded == [
        (UsageRecord(input_tokens=7, source="background_review"), False)
    ]


def test_background_review_captures_runtime_before_thread_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mclaw.agent import background_review

    created: list[object] = []
    pending: list[object] = []

    class FakeReviewAgent:
        def __init__(self, *, provider_runtime, **_kwargs) -> None:
            self.provider_runtime = provider_runtime
            self.messages = []
            self.tools = []
            self.valid_tool_names = set()
            self.enabled_toolsets = []
            self.config = {}
            self.session_id = "session"
            self.system_prompt = "system"
            created.append(self)

        def run_conversation(self, **_kwargs):
            return {"completed": True}

    class DeferredThread:
        def __init__(self, *, target, **_kwargs) -> None:
            pending.append(target)

        def start(self) -> None:
            pass

    monkeypatch.setattr(background_review.threading, "Thread", DeferredThread)
    scheduled_runtime = _context(model="gpt-scheduled")
    parent = FakeReviewAgent(provider_runtime=scheduled_runtime)
    background_review.spawn_background_review(
        parent,
        messages_snapshot=[{"role": "system", "content": "system"}],
        review_skills=True,
    )
    parent.provider_runtime = _context(model="gpt-switched")

    pending[0]()

    assert created[-1].provider_runtime is scheduled_runtime


def test_auxiliary_uses_inherited_or_independent_transport_and_parent_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mclaw.agent import auxiliary_client

    monkeypatch.setenv("DEEPSEEK_API_KEY", "auxiliary-secret")
    calls: list[tuple[ProviderRuntimeContext, dict]] = []
    usage = UsageRecord(input_tokens=5, output_tokens=2, source="auxiliary")

    class FakeTransport:
        def __init__(self, context: ProviderRuntimeContext) -> None:
            self.context = context

        def call(self, **kwargs):
            calls.append((self.context, kwargs))
            return ModelCallResult(
                content="",
                tool_calls=None,
                finish_reason="stop",
                reasoning=ReasoningTrace(text="auxiliary answer"),
                usage=usage,
                was_streamed=False,
                provider=self.context.provider,
                model=self.context.model,
            )

    monkeypatch.setattr(
        "mclaw.agent.transports.factory.create_transport",
        lambda context: FakeTransport(context),
    )
    parent = SimpleNamespace(
        provider_runtime=_context(),
        config={
            "auxiliary": {
                "session_search": {
                    "provider": "auto",
                    "model": "",
                    "base_url": "",
                    "timeout": 9,
                }
            }
        },
        recorded=[],
    )
    parent._record_usage = parent.recorded.append

    inherited = auxiliary_client.call_auxiliary_llm(
        "session_search",
        [{"role": "user", "content": "find"}],
        parent_agent=parent,
    )
    assert inherited == "auxiliary answer"
    assert calls[-1][0] is parent.provider_runtime
    assert calls[-1][1]["options"].source == "auxiliary"
    assert calls[-1][1]["options"].cache_plan is None
    assert auxiliary_client.extract_content_or_reasoning(
        SimpleNamespace(
            content="",
            reasoning=ReasoningTrace(
                format="gemini_thought_signature",
                payload={"signature": "internal-only"},
            ),
        )
    ) == ""

    parent.config["auxiliary"]["session_search"] = {
        "provider": "deepseek",
    }
    auxiliary_client.call_auxiliary_llm(
        "session_search",
        [{"role": "user", "content": "find"}],
        parent_agent=parent,
    )
    assert calls[-1][0].provider == "deepseek"
    assert calls[-1][0].model == "deepseek-v4-pro"
    assert calls[-1][0].api_key == "auxiliary-secret"
    assert parent.recorded == [usage, usage]
