# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from types import SimpleNamespace

import pytest

from mclaw.cli import model_switch
from mclaw.cli.app import InteractiveChat
from mclaw.cli.model_switch import ModelSwitchResult, switch_model
from mclaw.cli.runtime.key_setup import RuntimeKeySetupCoordinator, RuntimeKeySetupHooks
from mclaw.cli.runtime.session_commands import (
    RuntimeSessionCommandCoordinator,
    RuntimeSessionCommandHooks,
)
from mclaw.providers.resolver import (
    ProviderResolutionError,
    resolve_provider_runtime_context,
    restore_session_runtime_context,
)
from mclaw.state import SessionDB


def _context(provider: str = "openai", model: str = "gpt-5.4", key: str = "test-key"):
    return resolve_provider_runtime_context(provider=provider, model=model, api_key=key)


def _args(**overrides):
    values = {
        "resume": "",
        "model": "",
        "provider": "",
        "base_url": "",
        "api_key": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_model_switch_result_carries_the_live_context(monkeypatch) -> None:
    from mclaw.cli import model_resolver

    monkeypatch.setattr(model_resolver.models_dev, "fetch_models_dev", lambda *args, **kwargs: {})
    current = _context()

    result = switch_model("gpt-5.6", current_runtime=current)

    assert result.success
    assert result.runtime_context is not None
    assert result.runtime_context.model == "gpt-5.6"
    assert result.runtime_context.provider == "openai"
    assert result.runtime_context.api_key == current.api_key
    assert result.runtime_context.base_url_source == current.base_url_source
    assert not hasattr(result, "api_key")


def test_startup_resolves_explicit_model_intent_before_active_provider(
    monkeypatch,
) -> None:
    from mclaw.cli import model_resolver
    from mclaw.cli.main import _resolve_configured_runtime

    monkeypatch.setattr(model_resolver.models_dev, "fetch_models_dev", lambda *args, **kwargs: {})
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")
    config = {
        "active_provider": "openai",
        "model": "gpt-5.4",
        "providers": {},
        "reasoning": {"effort": ""},
    }

    context = _resolve_configured_runtime(
        _args(model="claude-sonnet-4-6"),
        config,
    )

    assert context.provider == "anthropic"
    assert context.model == "claude-sonnet-4-6"


def test_mclaw_switch_model_commits_context_transport_prompt_and_snapshot(
    monkeypatch,
) -> None:
    from mclaw.agent.core import MClaw

    current = _context(model="gpt-5.4")
    target = _context(model="gpt-5.6")
    next_transport = object()
    db_calls = []
    compressor_calls = []
    agent = object.__new__(MClaw)
    agent.provider_runtime = current
    agent.transport = object()
    agent.session_id = "switch-session"
    agent.messages = [
        {"role": "system", "content": "old system"},
        {"role": "user", "content": "hello"},
    ]
    agent._session_db = SimpleNamespace(
        update_model_config=lambda *args, **kwargs: db_calls.append((args, kwargs))
    )
    agent.context_compressor = SimpleNamespace(
        reconfigure_model=lambda *args, **kwargs: compressor_calls.append((args, kwargs))
    )
    agent._build_system_prompt = lambda *, model=None: f"system:{model}"

    monkeypatch.setattr("mclaw.agent.core.create_transport", lambda context: next_transport)
    monkeypatch.setattr("mclaw.agent.core.resolve_context_length", lambda context: 123_456)

    agent.switch_model(target)

    assert agent.provider_runtime is target
    assert agent.transport is next_transport
    assert (agent.model, agent.provider, agent.api_key, agent.base_url, agent.api_mode) == (
        target.model,
        target.provider,
        target.api_key,
        target.base_url,
        target.api_mode,
    )
    assert agent.messages[0]["content"] == "system:gpt-5.6"
    assert db_calls == [
        (
            ("switch-session",),
            {"model": target.model, "model_config": target.snapshot()},
        )
    ]
    assert compressor_calls == [((target,), {"context_window": 123_456})]


def test_mclaw_switch_model_candidate_failure_preserves_active_runtime(
    monkeypatch,
) -> None:
    from mclaw.agent.core import MClaw

    current = _context(model="gpt-5.4")
    target = _context(model="gpt-5.6")
    original_transport = object()
    agent = object.__new__(MClaw)
    agent.provider_runtime = current
    agent.transport = original_transport
    agent._session_db = SimpleNamespace(
        update_model_config=lambda *_args, **_kwargs: pytest.fail(
            "snapshot must not change before candidate construction succeeds"
        )
    )

    monkeypatch.setattr(
        "mclaw.agent.core.create_transport",
        lambda _context: (_ for _ in ()).throw(RuntimeError("transport failed")),
    )

    with pytest.raises(RuntimeError, match="transport failed"):
        agent.switch_model(target)

    assert agent.provider_runtime is current
    assert agent.transport is original_transport


def test_mclaw_switch_model_db_failure_preserves_active_runtime(
    monkeypatch,
) -> None:
    from mclaw.agent.core import MClaw

    current = _context(model="gpt-5.4")
    target = _context(model="gpt-5.6")
    original_transport = object()
    messages = [{"role": "system", "content": "old system"}]
    agent = object.__new__(MClaw)
    agent.provider_runtime = current
    agent.transport = original_transport
    agent.session_id = "switch-session"
    agent.messages = messages
    agent._session_db = SimpleNamespace(
        update_model_config=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("db failed")
        )
    )
    agent.context_compressor = SimpleNamespace(
        reconfigure_model=lambda *_args, **_kwargs: pytest.fail(
            "compressor must not change before the snapshot commits"
        )
    )
    agent._build_system_prompt = lambda *, model=None: f"system:{model}"

    monkeypatch.setattr("mclaw.agent.core.create_transport", lambda _context: object())
    monkeypatch.setattr("mclaw.agent.core.resolve_context_length", lambda _context: 123_456)

    with pytest.raises(RuntimeError, match="db failed"):
        agent.switch_model(target)

    assert agent.provider_runtime is current
    assert agent.transport is original_transport
    assert agent.messages is messages
    assert agent.messages[0]["content"] == "old system"


def test_same_runtime_profile_keeps_process_only_credential(monkeypatch) -> None:
    from mclaw.cli import model_resolver

    monkeypatch.setattr(model_resolver.models_dev, "fetch_models_dev", lambda *args, **kwargs: {})
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    current = _context(key="process-only-key")

    result = switch_model(
        "gpt-5.6",
        current_runtime=current,
        explicit_profile="api",
    )

    assert result.success
    assert result.runtime_context.api_key == "process-only-key"


def test_key_setup_retry_applies_a_live_context(monkeypatch) -> None:
    from mclaw.cli import model_resolver

    monkeypatch.setattr(model_resolver.models_dev, "fetch_models_dev", lambda *args, **kwargs: {})
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    current = _context()
    first = switch_model(
        "claude-sonnet-4-6",
        current_runtime=current,
        explicit_provider="anthropic",
    )
    assert first.needs_api_key

    applied = []

    def save_env(name: str, value: str) -> None:
        monkeypatch.setenv(name, value)

    hooks = RuntimeKeySetupHooks(
        save_env_value=save_env,
        render_missing_key=lambda: None,
        render_key_saved=lambda *_args: None,
        retry_search_backend=lambda: None,
        render_search_error=lambda _message: None,
        sync_search_backend=lambda _backend: None,
        render_search_success=lambda _message: None,
        retry_model_switch=lambda setup: switch_model(
            setup["model"],
            current_runtime=current,
            explicit_provider=setup["explicit_provider"],
        ),
        render_model_error=lambda _message: None,
        apply_model_switch=lambda result, is_global: applied.append((result, is_global)),
    )
    RuntimeKeySetupCoordinator(hooks).complete(
        {
            "model": "claude-sonnet-4-6",
            "explicit_provider": "anthropic",
            "env_var": first.key_env_var,
            "is_global": True,
        },
        "fresh-key",
    )

    assert applied[0][0].runtime_context.provider == "anthropic"
    assert applied[0][0].runtime_context.api_key == "fresh-key"
    assert applied[0][1] is True


def test_persist_model_choice_clears_setup_selector(monkeypatch) -> None:
    config = {"active_provider_profile": "api-cn"}
    saved = []
    monkeypatch.setattr(model_switch, "load_config", lambda strict=True: config)
    monkeypatch.setattr(model_switch, "save_config", lambda value: saved.append(dict(value)))
    monkeypatch.setattr("mclaw.cli.config.upsert_fallback_provider_model", lambda *_args: None)

    model_switch.persist_model_choice("qwen3.7-plus", "qwen-intl")

    assert saved[0]["model"] == "qwen3.7-plus"
    assert saved[0]["active_provider"] == "qwen-intl"
    assert saved[0]["active_provider_profile"] == ""


def test_startup_resume_overlay_matrix(monkeypatch, tmp_path) -> None:
    from mclaw.cli import main
    from mclaw import state

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(state, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setenv("OPENAI_API_KEY", "current-key")
    snapshot = _context(model="gpt-5.4", key="old-key").snapshot()
    db = SessionDB(db_path)
    try:
        db.create_session(
            "resume-target",
            "cli",
            model="gpt-5.4",
            model_config=snapshot,
            workspace=str(tmp_path),
        )
    finally:
        db.close()

    restored, session_id = main._resolve_startup_runtime(
        _args(resume="resume-target"),
        {"active_provider": "anthropic", "model": "claude-sonnet-4-6"},
        str(tmp_path),
    )
    model_overlay, _ = main._resolve_startup_runtime(
        _args(resume="resume-target", model="gpt-5.6"),
        {},
        str(tmp_path),
    )
    endpoint_overlay, _ = main._resolve_startup_runtime(
        _args(resume="resume-target", base_url="https://proxy.example/v1", api_key="overlay-key"),
        {},
        str(tmp_path),
    )

    assert session_id == "resume-target"
    assert restored.provider == "openai"
    assert restored.model == "gpt-5.4"
    assert restored.api_key == "current-key"
    assert model_overlay.provider == "openai"
    assert model_overlay.model == "gpt-5.6"
    assert endpoint_overlay.base_url == "https://proxy.example/v1"
    assert endpoint_overlay.base_url_source == "explicit"
    assert endpoint_overlay.api_key == "overlay-key"
    with pytest.raises(ProviderResolutionError, match="requires a model"):
        main._resolve_startup_runtime(
            _args(resume="resume-target", provider="anthropic"),
            {},
            str(tmp_path),
        )


def test_startup_empty_snapshot_bootstraps_from_row_model(monkeypatch, tmp_path) -> None:
    from mclaw.cli import main
    from mclaw import state

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(state, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setenv("OPENAI_API_KEY", "current-key")
    db = SessionDB(db_path)
    try:
        db.create_session(
            "legacy-target",
            "cli",
            model="gpt-5.4",
            workspace=str(tmp_path),
        )
    finally:
        db.close()

    context, _ = main._resolve_startup_runtime(
        _args(resume="legacy-target"),
        {"active_provider": "openai", "model": "ignored-config-model"},
        str(tmp_path),
    )

    assert context.provider == "openai"
    assert context.model == "gpt-5.4"


def test_slash_resume_restores_before_history_and_apply() -> None:
    calls = []
    context = _context()
    notices = []
    hooks = RuntimeSessionCommandHooks(
        current_session_id=lambda: "current",
        resolve_session_id=lambda value: calls.append(("resolve", value)) or "target",
        get_messages_as_conversation=lambda value: calls.append(("history", value)) or [{"role": "user"}],
        get_model_config=lambda value: calls.append(("snapshot", value)) or {"selector": True},
        restore_runtime_context=lambda snapshot, model: calls.append(("restore", snapshot, model)) or context,
        apply_resume=lambda target, runtime, history: calls.append(("apply", target, runtime, history)),
        get_session=lambda value: calls.append(("row", value)) or {"id": value, "model": "row-model", "title": "T"},
        set_session_title=lambda *_args: True,
        export_session=lambda _value: None,
        save_session_export=lambda *_args: "",
        render_notice=lambda *args: notices.append(args),
    )

    RuntimeSessionCommandCoordinator(hooks).handle_resume("target")

    assert [call[0] for call in calls] == ["resolve", "row", "snapshot", "restore", "history", "apply"]
    assert notices[-1][-1] == "success"


def test_interactive_chat_exposes_and_applies_only_runtime_context() -> None:
    old = _context(model="gpt-5.4")
    new = _context(model="gpt-5.6")

    class Agent:
        provider_runtime = old

        def switch_model(self, context):
            self.provider_runtime = context

    notices = []
    chat = object.__new__(InteractiveChat)
    chat.pending_provider_runtime = old
    chat.agent = Agent()
    scheduler_runner = SimpleNamespace(startup_provider_runtime=old)
    chat._scheduler_coordinator = SimpleNamespace(
        engine=SimpleNamespace(runner=scheduler_runner)
    )
    chat._commands_renderer = SimpleNamespace(render_notice=lambda *args, **kwargs: notices.append((args, kwargs)))

    chat._apply_model_switch(ModelSwitchResult(True, runtime_context=new, info_message="switched"))

    assert chat.provider_runtime is new
    assert chat.pending_provider_runtime is new
    assert chat.model == "gpt-5.6"
    assert chat.provider == "openai"
    assert scheduler_runner.startup_provider_runtime is new
    assert notices[-1][0][-1] == "switched"


def test_session_restore_reuses_live_key_and_bootstraps_from_live_context(
    monkeypatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    current = _context(model="gpt-5.4", key="process-only-key")
    populated = _context(model="gpt-5.6", key="old-key").snapshot()

    restored = restore_session_runtime_context(
        populated,
        fallback_context=current,
        config={"active_provider": "deepseek", "model": "deepseek-v4-pro"},
    )
    bootstrapped = restore_session_runtime_context(
        {},
        row_model="gpt-5.6",
        fallback_context=current,
        config={"active_provider": "deepseek", "model": "deepseek-v4-pro"},
    )

    assert restored.provider == "openai"
    assert restored.model == "gpt-5.6"
    assert restored.api_key == "process-only-key"
    assert restored.auth_source == current.auth_source
    assert bootstrapped.provider == "openai"
    assert bootstrapped.model == "gpt-5.6"
    assert bootstrapped.api_key == "process-only-key"


def test_session_restore_prefers_snapshot_credential_source(monkeypatch) -> None:
    monkeypatch.setenv("GLM_API_KEY", "startup-key")
    monkeypatch.setenv("ZHIPU_API_KEY", "snapshot-key")
    current = resolve_provider_runtime_context(provider="zhipu", model="glm-4.7")
    snapshot = dict(current.snapshot())
    snapshot["auth_source"] = "ZHIPU_API_KEY"

    restored = restore_session_runtime_context(snapshot, fallback_context=current)

    assert current.auth_source == "GLM_API_KEY"
    assert restored.auth_source == "ZHIPU_API_KEY"
    assert restored.api_key == "snapshot-key"


def test_session_restore_retains_live_source_when_environment_key_disappears(
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "startup-key")
    current = resolve_provider_runtime_context(provider="openai", model="gpt-5.4")
    snapshot = dict(current.snapshot())
    snapshot["model"] = "gpt-5.6"
    monkeypatch.delenv("OPENAI_API_KEY")

    restored = restore_session_runtime_context(snapshot, fallback_context=current)
    bootstrapped = restore_session_runtime_context(
        {},
        row_model="gpt-5.6",
        fallback_context=current,
    )

    assert restored.model == "gpt-5.6"
    assert restored.api_key == "startup-key"
    assert restored.auth_source == "OPENAI_API_KEY"
    assert bootstrapped.api_key == "startup-key"
    assert bootstrapped.auth_source == "OPENAI_API_KEY"


def test_interactive_resume_failure_preserves_current_state(monkeypatch) -> None:
    from mclaw.cli import app

    old = _context(model="gpt-5.4")
    new = _context(model="gpt-5.6")

    class Lock:
        instances = []

        def __init__(self, session_id):
            self.session_id = session_id
            self.released = False
            self.instances.append(self)

        def acquire(self):
            return None

        def release(self):
            self.released = True

    old_lock = Lock("current")
    old_agent = SimpleNamespace(provider_runtime=old)
    chat = object.__new__(InteractiveChat)
    chat.session_id = "current"
    chat._session_lock = old_lock
    chat.agent = old_agent
    chat.pending_provider_runtime = old
    chat._create_agent = lambda runtime, session_id: SimpleNamespace(
        provider_runtime=runtime,
        messages=[],
        session_user_messages=0,
    )
    chat._session_db = SimpleNamespace(
        switch_active_session=lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("db failed"))
    )
    monkeypatch.setattr(app, "InteractiveSessionLock", Lock)

    with pytest.raises(RuntimeError, match="db failed"):
        chat._apply_resume("target", new, [{"role": "user", "content": "hi"}])

    assert chat.session_id == "current"
    assert chat.agent is old_agent
    assert chat.pending_provider_runtime is old
    assert chat._session_lock is old_lock
    assert old_lock.released is False
    assert Lock.instances[-1].released is True


def test_startup_resume_lock_failure_does_not_reopen_target(monkeypatch, tmp_path) -> None:
    from mclaw import state
    from mclaw.cli import app
    from mclaw.cli.runtime.session_lock import InteractiveSessionLockError

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(state, "DEFAULT_DB_PATH", db_path)
    db = SessionDB(db_path)
    try:
        db.create_session(
            "locked-target",
            "cli",
            model="gpt-5.4",
            model_config=_context().snapshot(),
            workspace=str(tmp_path),
        )
        db.end_session("locked-target", "previous_exit")
    finally:
        db.close()

    class LockedSession:
        def __init__(self, session_id):
            self.session_id = session_id

        def acquire(self):
            raise InteractiveSessionLockError("already open")

        def release(self):
            return None

    monkeypatch.setattr(app, "InteractiveSessionLock", LockedSession)

    with pytest.raises(InteractiveSessionLockError, match="already open"):
        InteractiveChat(
            provider_runtime=_context(),
            resume_session_id="locked-target",
            config={"_launch_cwd": str(tmp_path)},
        )

    check = SessionDB(db_path)
    try:
        target = check.get_session("locked-target")
        assert target["ended_at"] is not None
        assert target["end_reason"] == "previous_exit"
    finally:
        check.close()


def test_startup_resume_agent_failure_does_not_reopen_target(monkeypatch, tmp_path) -> None:
    from mclaw import state
    from mclaw.cli import app

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(state, "DEFAULT_DB_PATH", db_path)
    db = SessionDB(db_path)
    try:
        db.create_session(
            "broken-target",
            "cli",
            model="gpt-5.4",
            model_config=_context().snapshot(),
            workspace=str(tmp_path),
        )
        db.end_session("broken-target", "previous_exit")
    finally:
        db.close()

    class Lock:
        def __init__(self, session_id):
            self.session_id = session_id

        def acquire(self):
            return None

        def release(self):
            return None

    monkeypatch.setattr(app, "InteractiveSessionLock", Lock)
    monkeypatch.setattr(app, "get_skill_registry", lambda: SimpleNamespace(refresh=lambda: None))
    monkeypatch.setattr(InteractiveChat, "_init_agent", lambda _self: (_ for _ in ()).throw(RuntimeError("init failed")))

    with pytest.raises(RuntimeError, match="init failed"):
        InteractiveChat(
            provider_runtime=_context(),
            resume_session_id="broken-target",
            config={"_launch_cwd": str(tmp_path)},
        )

    check = SessionDB(db_path)
    try:
        target = check.get_session("broken-target")
        assert target["ended_at"] is not None
        assert target["end_reason"] == "previous_exit"
    finally:
        check.close()


def test_session_db_switch_active_session_is_transactional(tmp_path) -> None:
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("current", "cli")
        db.create_session("target", "cli")
        db.end_session("target", "old")

        db.switch_active_session("current", "target", end_reason="user_resume")

        current = db.get_session("current")
        target = db.get_session("target")
        assert current["ended_at"] is not None
        assert current["end_reason"] == "user_resume"
        assert target["ended_at"] is None
        assert target["end_reason"] is None

        with pytest.raises(ValueError, match="missing"):
            db.switch_active_session("target", "missing", end_reason="should_rollback")
        assert db.get_session("target")["ended_at"] is None
    finally:
        db.close()
