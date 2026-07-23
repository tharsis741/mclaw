import importlib
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from mclaw.cli.app import InteractiveChat
from mclaw.cli.runtime.events import RuntimeStatus
from mclaw.runtime import secrets as secret_service
from mclaw.runtime.search import SearchProfile
from mclaw.skills_hub import install_service, source_resolver
from mclaw.skills_hub.search import ClawHubSearcher
from mclaw.tools import dispatch, file_tools, secret_tool, session_search_tool
from mclaw.tools.cancellation import (
    ASYNC_OR_FENCED_TOOLS,
    ATOMIC_LOCAL_TOOLS,
    CANCELLATION_STRATEGY_BY_TOOL,
    CANCELLATION_STRATEGY_DESCRIPTIONS,
    COOPERATIVE_TOOLS,
    cancellation_strategy,
)
from mclaw.tools.interrupt import reset_interrupt_event, set_interrupt_event
from mclaw.tools.registry import registry
from mclaw.tools.skill_tools import manage_tool, read_tools
from mclaw.tools.toolsets import resolve_toolset


def test_cancellation_contract_exactly_covers_all_34_registered_tools() -> None:
    from mclaw.tools.dispatch import _discover_tools

    _discover_tools()
    toolset_tools = set(resolve_toolset("all"))
    registered_tools = set(registry._tools)
    declared_tools = set(CANCELLATION_STRATEGY_BY_TOOL)

    assert len(toolset_tools) == 34
    assert registered_tools == toolset_tools
    assert declared_tools == toolset_tools
    assert len(ATOMIC_LOCAL_TOOLS) == 13
    assert len(COOPERATIVE_TOOLS) == 4
    assert len(ASYNC_OR_FENCED_TOOLS) == 17
    assert ATOMIC_LOCAL_TOOLS.isdisjoint(COOPERATIVE_TOOLS)
    assert ATOMIC_LOCAL_TOOLS.isdisjoint(ASYNC_OR_FENCED_TOOLS)
    assert COOPERATIVE_TOOLS.isdisjoint(ASYNC_OR_FENCED_TOOLS)
    assert set(CANCELLATION_STRATEGY_DESCRIPTIONS) == set(
        CANCELLATION_STRATEGY_BY_TOOL.values()
    )
    assert cancellation_strategy("read_file") == "atomic-local"
    assert cancellation_strategy("session_search") == "cooperative"
    assert cancellation_strategy("secret_request_many") == "async-or-fenced"


@pytest.mark.parametrize("tool_name", sorted(CANCELLATION_STRATEGY_BY_TOOL))
def test_ctrl_c_before_start_never_enters_any_registered_tool(
    monkeypatch,
    tool_name: str,
) -> None:
    """Every registered name must share the dispatcher's pre-start barrier."""

    cancel_event = threading.Event()
    cancel_event.set()
    entered = []

    def should_not_dispatch(*_args, **_kwargs):
        entered.append(tool_name)
        raise AssertionError("cancelled tool entered its handler")

    monkeypatch.setattr(dispatch, "_dispatch_single", should_not_dispatch)
    [result] = dispatch.handle_function_calls(
        [
            {
                "id": f"pre-cancel-{tool_name}",
                "function": {"name": tool_name, "arguments": "{}"},
            }
        ],
        {tool_name},
        cancel_event=cancel_event,
    )

    payload = json.loads(result)
    assert entered == []
    assert payload["status"] == "cancelled"
    assert payload["interrupted"] is True
    assert payload.get("completion_unknown") is not True


@pytest.mark.parametrize("tool_name", sorted(CANCELLATION_STRATEGY_BY_TOOL))
def test_uncooperative_started_tool_is_fail_closed_for_every_registered_name(
    monkeypatch,
    tool_name: str,
) -> None:
    """The universal worker fence is the worst-case fallback for all 34 tools."""

    cancel_event = threading.Event()
    entered = threading.Event()
    release = threading.Event()
    drained = threading.Event()
    abort_reasons = []

    class Parent:
        session_id = f"test-{tool_name}"

        def _register_turn_worker(self, _worker) -> None:
            drained.clear()

        def _unregister_turn_worker(self, _worker) -> None:
            drained.set()

        def _request_turn_abort(self, reason, event) -> None:
            abort_reasons.append(reason)
            event.set()

    def blocking_dispatch(*_args, **_kwargs):
        entered.set()
        release.wait(1)
        return json.dumps({"success": True})

    def cancel_after_entry() -> None:
        assert entered.wait(1)
        cancel_event.set()

    monkeypatch.setattr(dispatch, "_dispatch_single", blocking_dispatch)
    monkeypatch.setattr(dispatch, "_INTERRUPT_CLEANUP_GRACE", 0.01)
    canceller = threading.Thread(target=cancel_after_entry)
    canceller.start()
    try:
        [result] = dispatch.handle_function_calls(
            [
                {
                    "id": f"running-cancel-{tool_name}",
                    "function": {"name": tool_name, "arguments": "{}"},
                }
            ],
            {tool_name},
            parent_agent=Parent(),
            cancel_event=cancel_event,
        )
    finally:
        release.set()
        canceller.join(1)

    payload = json.loads(result)
    assert payload["status"] == "cancel_requested"
    assert payload["interrupted"] is True
    assert payload["completion_unknown"] is True
    assert "tool_completion_unknown" in abort_reasons
    assert drained.wait(1)


def test_skill_search_uses_same_event_and_returns_cancelled(monkeypatch) -> None:
    cancel_event = threading.Event()
    seen_events = []

    def fake_search(_query, limit=10, *, cancel_event=None):
        seen_events.append(cancel_event)
        cancel_event.set()
        raise RuntimeError("search transport closed")

    monkeypatch.setattr(read_tools, "search", fake_search)
    token = set_interrupt_event(cancel_event)
    try:
        result = json.loads(read_tools.skill_search("browser"))
    finally:
        reset_interrupt_event(token)

    assert seen_events == [cancel_event]
    assert result["success"] is False
    assert result["status"] == "cancelled"
    assert result["interrupted"] is True


def test_skill_manage_passes_same_event_and_prioritizes_cancel(monkeypatch) -> None:
    cancel_event = threading.Event()
    seen_events = []

    def fake_install(_source, *, user_intent=None, cancel_event=None):
        seen_events.append(cancel_event)
        cancel_event.set()
        raise RuntimeError("materializer closed")

    monkeypatch.setattr(manage_tool.install_service, "install_prepare", fake_install)
    token = set_interrupt_event(cancel_event)
    try:
        result = json.loads(
            manage_tool.skill_manage(
                "install_prepare",
                source="https://example.invalid/skill",
            )
        )
    finally:
        reset_interrupt_event(token)

    assert seen_events == [cancel_event]
    assert result["success"] is False
    assert result["status"] == "cancelled"
    assert result["interrupted"] is True


def test_search_files_passes_same_event_and_prioritizes_cancel(monkeypatch) -> None:
    cancel_event = threading.Event()
    seen_events = []

    def fake_search_files(*_args, cancel_event=None, **_kwargs):
        seen_events.append(cancel_event)
        cancel_event.set()
        raise OSError("search process closed")

    monkeypatch.setattr(file_tools.ops, "search_files", fake_search_files)
    token = set_interrupt_event(cancel_event)
    try:
        result = json.loads(file_tools.search_files_tool(".", "needle"))
    finally:
        reset_interrupt_event(token)

    assert seen_events == [cancel_event]
    assert result["success"] is False
    assert result["status"] == "cancelled"
    assert result["interrupted"] is True


def test_clawhub_search_stops_after_cancelled_detail_http(monkeypatch) -> None:
    cancel_event = threading.Event()
    calls = []

    async def fake_get(url, **_kwargs):
        calls.append(url)
        if url.endswith("/search"):
            return {
                "results": [
                    {"slug": "first", "displayName": "First"},
                    {"slug": "second", "displayName": "Second"},
                ]
            }
        cancel_event.set()
        return {"skill": {"slug": "first"}}

    search_module = importlib.import_module("mclaw.skills_hub.search")
    monkeypatch.setattr(search_module, "_get_json_async", fake_get)

    with pytest.raises(InterruptedError):
        ClawHubSearcher().search("demo", cancel_event=cancel_event)

    assert len(calls) == 2
    assert calls[0].endswith("/search")
    assert calls[1].endswith("/skills/first")


def test_install_prepare_cleans_draft_and_stops_after_materialize(
    monkeypatch,
    tmp_path: Path,
) -> None:
    cancel_event = threading.Event()
    later_stages = []
    resolved = source_resolver.ResolvedSource(
        type="local",
        original="source",
        fetch_url="source",
        local_path=tmp_path / "source",
    )

    monkeypatch.setattr(install_service, "ensure_runtime_roots", lambda: None)
    monkeypatch.setattr(install_service, "get_skill_drafting_dir", lambda: tmp_path / "drafts")
    monkeypatch.setattr(install_service, "_drafting_id", lambda: "draft-cancelled")

    def fake_resolve(_source, *, cancel_event=None):
        assert cancel_event is cancel_event_outer
        return resolved

    def fake_materialize(_resolved, target, *, cancel_event=None):
        assert cancel_event is cancel_event_outer
        target.mkdir(parents=True)
        (target / "partial").write_text("partial", encoding="utf-8")
        cancel_event.set()

    cancel_event_outer = cancel_event
    monkeypatch.setattr(install_service, "resolve_source", fake_resolve)
    monkeypatch.setattr(install_service, "materialize_source", fake_materialize)
    monkeypatch.setattr(
        install_service,
        "_read_root_skill_md",
        lambda _package: later_stages.append("read") or "unused",
    )

    with pytest.raises(InterruptedError):
        install_service.install_prepare("source", cancel_event=cancel_event)

    assert later_stages == []
    assert not (tmp_path / "drafts" / "draft-cancelled").exists()


def test_materialize_cancel_after_download_deletes_archive_before_extract(
    monkeypatch,
    tmp_path: Path,
) -> None:
    cancel_event = threading.Event()
    archive = tmp_path / "download.zip"
    archive.write_bytes(b"not inspected")
    extracted = []
    resolved = source_resolver.ResolvedSource(
        type="github",
        original="https://github.com/o/r",
        fetch_url="https://example.invalid/archive.zip",
    )

    def fake_download(_url, *, cancel_event=None):
        assert cancel_event is cancel_event_outer
        cancel_event.set()
        return archive

    cancel_event_outer = cancel_event
    monkeypatch.setattr(source_resolver, "_download_archive", fake_download)
    monkeypatch.setattr(
        source_resolver,
        "_safe_extract_archive",
        lambda *_args, **_kwargs: extracted.append(True),
    )

    with pytest.raises(InterruptedError):
        source_resolver.materialize_source(
            resolved,
            tmp_path / "target",
            cancel_event=cancel_event,
        )

    assert extracted == []
    assert not archive.exists()


def test_skill_archive_download_checks_between_stream_chunks(
    monkeypatch,
    tmp_path: Path,
) -> None:
    cancel_event = threading.Event()
    request_options = []
    real_mkstemp = source_resolver.tempfile.mkstemp

    async def fake_download(_url, path, *, cancel_event, timeout):
        request_options.append({"timeout": timeout})
        path.write_bytes(b"first")
        cancel_event.set()
        source_resolver.cancellation_checkpoint(cancel_event)

    monkeypatch.setattr(
        source_resolver.tempfile,
        "mkstemp",
        lambda **kwargs: real_mkstemp(dir=tmp_path, **kwargs),
    )
    monkeypatch.setattr(source_resolver, "_download_archive_async", fake_download)

    with pytest.raises(InterruptedError):
        source_resolver._download_archive(
            "https://example.invalid/archive.zip",
            cancel_event=cancel_event,
        )

    assert request_options == [{"timeout": 60}]
    assert list(tmp_path.glob("mclaw_skill_download_*.zip")) == []


def test_search_provider_does_not_fall_through_after_blocking_call_cancel(
    monkeypatch,
    tmp_path: Path,
) -> None:
    cancel_event = threading.Event()
    subprocess_calls = []
    fallback_calls = []
    decision = SimpleNamespace(
        allowed=True,
        resolved=tmp_path,
        error_message=lambda: "denied",
    )
    runtime = SimpleNamespace(
        kind="linux",
        paths=SimpleNamespace(check=lambda *_args: decision),
    )
    profile = SearchProfile.__new__(SearchProfile)
    profile.runtime = runtime
    profile.provider = "rg"

    def fake_run(*args, **kwargs):
        subprocess_calls.append((args, kwargs))
        cancel_event.set()
        raise OSError("provider stopped")

    monkeypatch.setattr("mclaw.runtime.search.run_captured_process", fake_run)
    monkeypatch.setattr(
        profile,
        "_python_regex",
        lambda *_args, **_kwargs: fallback_calls.append(True) or "unexpected",
    )

    with pytest.raises(InterruptedError):
        profile.search(str(tmp_path), "needle", cancel_event=cancel_event)

    assert len(subprocess_calls) == 1
    assert fallback_calls == []
    assert subprocess_calls[0][1] == {
        "timeout": 30,
        "cancel_event": cancel_event,
    }


def test_session_search_cancelled_summary_does_not_start_next_session(
    monkeypatch,
) -> None:
    cancel_event = threading.Event()
    summarized_sessions = []

    class FakeDB:
        def search_messages(self, **_kwargs):
            return [
                {"session_id": "first", "source": "cli"},
                {"session_id": "second", "source": "cli"},
            ]

        def get_session(self, session_id):
            return {
                "id": session_id,
                "source": "cli",
                "parent_session_id": None,
            }

        def get_messages_as_conversation(self, session_id):
            summarized_sessions.append(session_id)
            return [{"role": "user", "content": f"message for {session_id}"}]

    def fake_auxiliary_llm(**_kwargs):
        cancel_event.set()
        raise RuntimeError("transport closed")

    monkeypatch.setattr(session_search_tool, "_resolve_db", lambda _parent=None: FakeDB())
    monkeypatch.setattr(session_search_tool, "_resolve_current_session_id", lambda _parent=None: "")
    monkeypatch.setattr(
        "mclaw.agent.auxiliary_client.call_auxiliary_llm",
        fake_auxiliary_llm,
    )

    token = set_interrupt_event(cancel_event)
    try:
        result = json.loads(session_search_tool.session_search(query="message", limit=2))
    finally:
        reset_interrupt_event(token)

    assert result["success"] is False
    assert result["status"] == "cancelled"
    assert summarized_sessions == ["first"]


def test_secret_cancelled_by_prompt_never_saves_or_authorizes(monkeypatch) -> None:
    cancel_event = threading.Event()
    saved = []
    authorized = []

    monkeypatch.setattr(secret_service, "get_env_value", lambda _name: None)
    monkeypatch.setattr(
        secret_service,
        "save_env_value",
        lambda name, value: saved.append((name, value)),
    )
    monkeypatch.setattr(
        secret_service,
        "authorize",
        lambda scope, names: authorized.append((scope, names)) or list(names),
    )

    def prompt(_scope, _needs):
        cancel_event.set()
        return {
            "values": {"DEMO_API_KEY": "must-not-be-saved"},
            "authorized": ["DEMO_API_KEY"],
        }

    token = set_interrupt_event(cancel_event)
    try:
        result = json.loads(
            secret_tool._handle_secret_request_many(
                {
                    "required_for": "tool:demo",
                    "secrets": ["DEMO_API_KEY"],
                },
                parent_agent=SimpleNamespace(secret_request_callback=prompt),
            )
        )
    finally:
        reset_interrupt_event(token)

    assert result["success"] is False
    assert result["status"] == "cancelled"
    assert result["interrupted"] is True
    assert saved == []
    assert authorized == []


def test_secret_cancel_after_commit_point_finishes_persistence_and_authorization(monkeypatch) -> None:
    cancel_event = threading.Event()
    saved = []
    authorized = []

    monkeypatch.setattr(secret_service, "get_env_value", lambda _name: None)

    def save(name, value):
        saved.append((name, value))
        if len(saved) == 1:
            cancel_event.set()

    monkeypatch.setattr(secret_service, "save_env_value", save)
    monkeypatch.setattr(
        secret_service,
        "authorize",
        lambda scope, names: authorized.append((scope, list(names))) or list(names),
    )

    result = secret_service.secret_request_many(
        "tool:demo",
        ["FIRST_API_KEY", "SECOND_API_KEY"],
        prompt_callback=lambda *_args: {
            "values": {
                "FIRST_API_KEY": "first",
                "SECOND_API_KEY": "second",
            }
        },
        cancel_event=cancel_event,
    )

    assert result["success"] is True
    assert result["configured"] == ["FIRST_API_KEY", "SECOND_API_KEY"]
    assert saved == [("FIRST_API_KEY", "first"), ("SECOND_API_KEY", "second")]
    assert authorized == [
        ("tool:demo", ["FIRST_API_KEY"]),
        ("tool:demo", ["SECOND_API_KEY"]),
    ]


def test_cli_secret_prompt_cancel_wakes_and_clears_pending_ui() -> None:
    cancel_event = threading.Event()
    invalidations = []
    prompt_rendered = threading.Event()
    outcomes = []
    errors = []

    class State:
        status = RuntimeStatus.IDLE
        detail = "ready"

        def set_status(self, status, detail):
            self.status = status
            self.detail = detail

    state = State()
    chat = SimpleNamespace(
        # The active turn may already have ended by the time this worker polls;
        # the ContextVar-bound event must remain sufficient to wake the prompt.
        agent=SimpleNamespace(current_turn_cancel_event=lambda: None),
        _app=SimpleNamespace(invalidate=lambda: invalidations.append(True)),
        _pending_secret_request=None,
        _last_rendered_secret_request_signature=("old",),
        _runtime_state=lambda: state,
        _emit_runtime_event=lambda *_args, **_kwargs: None,
        _render_secret_request=lambda _pending: prompt_rendered.set(),
        _pet_emit_for_runtime_status=lambda *_args, **_kwargs: None,
    )

    def run_prompt():
        token = set_interrupt_event(cancel_event)
        try:
            outcomes.append(
                InteractiveChat._prompt_secret_request(
                    chat,
                    "tool:demo",
                    [{"env_var": "DEMO_API_KEY", "state": "missing"}],
                )
            )
        except BaseException as exc:
            errors.append(exc)
        finally:
            reset_interrupt_event(token)

    worker = threading.Thread(target=run_prompt)
    worker.start()
    try:
        assert prompt_rendered.wait(1)
        assert chat._pending_secret_request is not None
    finally:
        cancel_event.set()
    worker.join(1)

    assert not worker.is_alive()
    assert errors == []
    assert outcomes == [{
        "values": {},
        "authorized": [],
        "skipped": ["DEMO_API_KEY"],
        "cancelled": True,
        "status": "cancelled",
    }]
    assert chat._pending_secret_request is None
    assert chat._last_rendered_secret_request_signature is None
    assert state.status == RuntimeStatus.IDLE
    assert state.detail == "ready"
    assert invalidations
