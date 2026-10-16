# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import fields
from types import SimpleNamespace
import threading

import pytest

from mclaw.application.sessions import SessionService
from mclaw.application.store import ApplicationStore
from mclaw.application.task_queue import (
    TaskQueue,
    adopt_terminal_queue,
    apply_pending_supplement,
    classify_task_result,
    terminal_queue,
)
from mclaw.cli.runtime.interactive import InteractiveRuntime
from mclaw.cli.runtime.turns import RuntimeTurnCoordinator, RuntimeTurnHooks


@pytest.mark.parametrize("result,expected", [
    ({"completed": True}, "completed"),
    ({"completed": False}, "failed"),
    ({"completed": True, "error": "failed"}, "failed"),
    ({"completed": True, "abort_reason": "tool_timeout"}, "failed"),
    ({"interrupted": True}, "interrupted"),
    ({"pending_skill_import_confirmation": True}, "waiting"),
])
def test_shared_task_result_requires_confirmed_completion(result, expected):
    assert classify_task_result(result) == expected


def test_queue_stop_holds_original_and_later_messages_until_explicit_resume():
    queue = TaskQueue()
    original = queue.submit("original")
    queue.submit("later-one")
    queue.submit("later-two")
    assert queue.take_next()["id"] == original
    queue.stop()
    assert queue.finish({"completed": True}) == "interrupted"
    assert queue.take_next() is None
    queue.resume("new constraint")
    resumed = queue.take_next()
    assert resumed["id"] == original
    assert resumed["supplements"][0]["content"] == "new constraint"
    queue.apply_supplements(lambda _item: None)
    queue.finish({"completed": True})
    assert queue.take_next()["content"] == "later-one"
    queue.finish({"completed": True})
    assert queue.take_next()["content"] == "later-two"


def test_queue_persists_submission_recovery_and_changes(tmp_path):
    path = tmp_path / "application.sqlite3"
    store = ApplicationStore(path)
    queue = terminal_queue(store, "session-a")
    original = queue.submit("original", "model-old")
    queue.take_next()
    queue.submit("later", "model-old")
    queue.update_model("model-new")
    assert queue.snapshot()["active"]["model"] == "model-old"
    queue.edit(0, "updated-later")
    store.close()
    reopened = ApplicationStore(path)
    recovered = terminal_queue(reopened, "session-a")
    assert recovered.snapshot()["paused"]
    assert recovered.snapshot()["active"]["id"] == original
    assert recovered.snapshot()["pending"][0]["model"] == "model-new"
    assert recovered.snapshot()["pending"][0]["content"] == "updated-later"
    assert recovered.take_next() is None
    reopened.close()


def test_queue_handoff_moves_one_authoritative_copy_and_preserves_web_messages(tmp_path):
    store = ApplicationStore(tmp_path / "application.sqlite3")
    session = SessionService._new_snapshot("session-a", str(tmp_path), True, "model")
    session["messages"] = [{"role": "assistant", "content": "existing"}]
    pending = TaskQueue()
    task_id = pending.submit("one")
    session["queue"] = pending.snapshot()["pending"]
    store.save(session)
    terminal = terminal_queue(store, "session-a")
    assert terminal.snapshot()["pending"][0]["id"] == task_id
    assert store.get("session-a")["queue"] == []
    assert store.get("session-a")["queue_owner"] == "terminal"
    adopted = adopt_terminal_queue(store, "session-a")
    assert adopted["queue"][0]["id"] == task_id
    assert adopted["messages"] == session["messages"]
    assert adopted["paused"]
    assert store.setting("tui_queue:session-a") is None
    assert adopt_terminal_queue(store, "session-a") is None
    store.close()


def test_supplement_applies_only_once_and_unapplied_additions_pause():
    queue = TaskQueue()
    queue.submit("original")
    queue.take_next()
    queue.supplement("constraint")
    assert queue.finish({"completed": True}) == "failed"
    queue.resume()
    queue.take_next()
    applied = []
    queue.apply_supplements(lambda item: applied.append(item["content"]))
    queue.apply_supplements(lambda item: applied.append(item["content"]))
    assert applied == ["constraint"]
    assert queue.finish({"completed": True}) == "completed"


def test_supplement_persistence_marker_deduplicates_replayed_addition():
    writes = []
    class DB:
        def append_message(self, *args, **kwargs):
            writes.append((args, kwargs))

    messages = []
    applied = set()
    item = {"id": "supplement-id", "content": "constraint"}
    assert apply_pending_supplement(DB(), "session", messages, "turn", item, applied)
    assert not apply_pending_supplement(DB(), "session", messages, "turn", item, applied)
    assert len(writes) == 1
    assert writes[0][1]["operation_id"] == "supplement:supplement-id"
    assert messages == [{"role": "user", "content": "constraint"}]


def _coordinator(runtime, run_turn):
    hooks = {field.name: (lambda *_args: None) for field in fields(RuntimeTurnHooks)}
    hooks.update(
        run_turn=run_turn,
        get_turn_result=lambda: runtime.session_state.last_result,
        current_model=lambda: "test-model",
        task_input=lambda task: task["content"],
    )
    return RuntimeTurnCoordinator(runtime, RuntimeTurnHooks(**hooks))


def test_tui_coordinator_failure_pauses_queue_and_resume_keeps_original_first():
    runtime = InteractiveRuntime(task_queue=TaskQueue())
    calls = []

    def run_turn(text):
        calls.append(text)
        if len(calls) == 1:
            runtime.task_queue.submit("second")
            runtime.session_state.last_result = {"completed": False, "error": "network"}
        else:
            runtime.session_state.last_result = {"completed": True}

    coordinator = _coordinator(runtime, run_turn)
    coordinator.handle_input("first")
    coordinator.on_idle()
    assert runtime.task_queue.snapshot()["paused"]
    coordinator.on_idle()
    assert calls == ["first"]
    coordinator.handle_input("/continue")
    assert calls[1] == "first"
    coordinator.on_idle()
    assert calls[2] == "second"


def test_tui_coordinator_preserves_control_prompts_before_normal_queue():
    runtime = InteractiveRuntime(task_queue=TaskQueue())
    calls = []
    hooks = {field.name: (lambda *_args: None) for field in fields(RuntimeTurnHooks)}
    hooks.update(has_pending_secret_request=lambda: True, handle_secret_request=calls.append)
    RuntimeTurnCoordinator(runtime, RuntimeTurnHooks(**hooks)).handle_input("secret-value")
    assert calls == ["secret-value"]
    assert not runtime.task_queue.snapshot()["pending"]


def test_queue_failed_save_does_not_accept_submission_in_memory():
    def fail(_state):
        raise OSError("disk unavailable")

    queue = TaskQueue(save=fail)
    with pytest.raises(OSError):
        queue.submit("draft must remain available")
    assert queue.snapshot()["pending"] == []


def test_queue_storage_failure_cannot_resume_queue_after_user_stop():
    queue = TaskQueue()
    queue.submit("original")
    queue.take_next()
    queue.submit("later")

    def fail(_state):
        raise OSError("disk unavailable")

    queue._save = fail
    with pytest.raises(OSError):
        queue.stop()
    assert queue.snapshot()["paused"]
    assert queue.snapshot()["status"] == "stopping"
    assert queue.take_next() is None


def test_tui_retry_uses_original_when_execution_never_started(tmp_path):
    from mclaw.cli.app import InteractiveChat
    from mclaw.state import SessionDB

    db = SessionDB(tmp_path / "history.db")
    db.create_session("session-a", "cli")
    ui = InteractiveChat.__new__(InteractiveChat)
    ui.session_id = "session-a"
    ui._session_db = db
    task = {"id": "task-a", "content": "original", "attempts": [{}, {}]}
    assert ui._prepare_task_input(task) == "original"
    assert ui._task_input_operation_id == "task:task-a"
    db.append_message("session-a", "user", content="original", operation_id="task:task-a")
    assert "继续处理" in ui._prepare_task_input(task)
    assert ui._task_input_operation_id == ""
    db.close()


def test_tui_submission_routes_running_input_and_explicit_supplements():
    from mclaw.cli.app import InteractiveChat

    ui = InteractiveChat.__new__(InteractiveChat)
    ui.runtime = InteractiveRuntime(task_queue=TaskQueue(), agent_running=True)
    ui._pending_key_setup = None
    ui._pending_skill_import_confirmation = None
    ui.pending_provider_runtime = SimpleNamespace(model="test-model")
    ui.agent = SimpleNamespace(provider_runtime=ui.pending_provider_runtime)
    ui.runtime.task_queue.submit("original")
    ui.runtime.task_queue.take_next()
    assert ui._submit_composer_text("later")
    assert ui._submit_composer_text("constraint", supplement=True)
    snapshot = ui.runtime.task_queue.snapshot()
    assert snapshot["pending"][0]["content"] == "later"
    assert snapshot["active"]["supplements"][0]["content"] == "constraint"
    assert ui._pending_input.empty()


@pytest.mark.parametrize("prompt", ["secret_request", "skill_import_confirmation", "key_setup", "scheduler_input"])
def test_tui_queue_waits_for_required_control_prompt(prompt):
    runtime = InteractiveRuntime(task_queue=TaskQueue())
    runtime.task_queue.submit("pending task")
    calls = []
    hooks = {field.name: (lambda *_args: None) for field in fields(RuntimeTurnHooks)}
    hooks.update(run_turn=calls.append)
    hooks["has_pending_" + prompt] = lambda: True
    RuntimeTurnCoordinator(runtime, RuntimeTurnHooks(**hooks)).on_idle()
    assert calls == []
    assert runtime.task_queue.snapshot()["pending"][0]["content"] == "pending task"


def test_tui_idle_rechecks_controls_arriving_after_worker_timeout():
    runtime = InteractiveRuntime(task_queue=TaskQueue())
    runtime.task_queue.submit("A", "old-model")
    runtime.pending_input.put("/model new-model")
    calls = []
    _coordinator(runtime, calls.append).on_idle()
    assert calls == []
    assert runtime.task_queue.snapshot()["pending"][0]["content"] == "A"


@pytest.mark.parametrize("command", ["/model new-model", "/clear", "/resume previous-session"])
def test_running_tui_defers_agent_changing_commands_until_turn_finishes(command):
    from mclaw.cli.app import InteractiveChat
    from mclaw.cli.runtime.workers import RuntimeWorkerHooks, RuntimeWorkerSupervisor

    ui = InteractiveChat.__new__(InteractiveChat)
    ui.runtime = InteractiveRuntime(task_queue=TaskQueue())
    ui._pending_key_setup = None
    ui._pending_skill_import_confirmation = None
    started, release, dispatched = threading.Event(), threading.Event(), threading.Event()
    actions = []
    hooks = {field.name: (lambda *_args: None) for field in fields(RuntimeTurnHooks)}

    def run(_text):
        started.set()
        assert release.wait(3)
        actions.append("task-finished")

    def dispatch(text):
        actions.append(text)
        dispatched.set()
        ui.runtime.request_exit()
        return True

    hooks.update(run_turn=run, dispatch_slash=dispatch, get_turn_result=lambda: {"completed": True},
                 task_input=lambda task: task["content"],
                 begin_turn=lambda _text: setattr(ui.runtime, "agent_running", True),
                 finish_turn=lambda: setattr(ui.runtime, "agent_running", False))
    coordinator = RuntimeTurnCoordinator(ui.runtime, RuntimeTurnHooks(**hooks))
    supervisor = RuntimeWorkerSupervisor(ui.runtime, RuntimeWorkerHooks(
        handle_input=coordinator.handle_input, on_idle=coordinator.on_idle,
        on_animation_tick=lambda: None, on_error=lambda exc: actions.append(type(exc).__name__),
        invalidate=lambda: None, animation_enabled=lambda: False, animation_interval=lambda _: .01,
    ))
    ui.runtime.task_queue.submit("original")
    workers = supervisor.start()
    try:
        assert started.wait(2)
        assert ui._submit_composer_text(command)
        assert not dispatched.is_set()
        release.set()
        assert dispatched.wait(2)
        assert actions == ["task-finished", command]
    finally:
        release.set()
        ui.runtime.request_exit()
        workers.process_thread.join(2)
        workers.animation_thread.join(2)


@pytest.mark.parametrize("queued_messages", [["A"], ["A", "B", "C"]])
@pytest.mark.parametrize("input_source", ["composer", "runtime"])
def test_tui_model_control_precedes_idle_queue_consumption(queued_messages, input_source):
    from mclaw.cli.app import InteractiveChat
    from mclaw.cli.runtime.workers import RuntimeWorkerHooks, RuntimeWorkerSupervisor

    ui = InteractiveChat.__new__(InteractiveChat)
    ui.runtime = InteractiveRuntime(task_queue=TaskQueue())
    ui._pending_key_setup = None
    ui._pending_skill_import_confirmation = None
    ui.pending_provider_runtime = SimpleNamespace(model="old-model")
    ui.agent = SimpleNamespace(provider_runtime=ui.pending_provider_runtime)
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    actions, errors = [], []
    hooks = {field.name: (lambda *_args: None) for field in fields(RuntimeTurnHooks)}

    def run(text):
        active = ui.runtime.task_queue.snapshot()["active"]
        actions.append((text, active["model"], ui.model))
        if text == "original":
            started.set()
            assert release.wait(3)
        if text == queued_messages[-1]:
            finished.set()
            ui.runtime.request_exit()

    def dispatch(text):
        assert text == "/model new-model"
        actions.append(("model-change",))
        ui.pending_provider_runtime.model = "new-model"
        return True

    hooks.update(run_turn=run, dispatch_slash=dispatch, get_turn_result=lambda: {"completed": True},
                 current_model=lambda: ui.model, task_input=lambda task: task["content"],
                 begin_turn=lambda _text: setattr(ui.runtime, "agent_running", True),
                 finish_turn=lambda: setattr(ui.runtime, "agent_running", False))
    coordinator = RuntimeTurnCoordinator(ui.runtime, RuntimeTurnHooks(**hooks))
    supervisor = RuntimeWorkerSupervisor(ui.runtime, RuntimeWorkerHooks(
        handle_input=coordinator.handle_input, on_idle=coordinator.on_idle,
        on_animation_tick=lambda: None, on_error=errors.append, invalidate=lambda: None,
        animation_enabled=lambda: False, animation_interval=lambda _: .01,
    ))
    ui.runtime.task_queue.submit("original", "old-model")
    workers = supervisor.start()
    try:
        assert started.wait(2)
        for text in queued_messages:
            if input_source == "composer":
                assert ui._submit_composer_text(text)
            else:
                ui.runtime.submit_text(text)
        assert ui._submit_composer_text("/model new-model")
        assert ui.runtime.task_queue.snapshot()["active"]["model"] == "old-model"
        release.set()
        assert finished.wait(3)
        assert errors == []
        assert actions == [("original", "old-model", "old-model"), ("model-change",)] + [
            (text, "new-model", "new-model") for text in queued_messages
        ]
    finally:
        release.set()
        ui.runtime.request_exit()
        workers.process_thread.join(2)
        workers.animation_thread.join(2)


def test_terminal_handoff_appends_new_history_and_keeps_web_process_details(tmp_path):
    from mclaw.state import SessionDB

    history = SessionDB(tmp_path / "state.db")
    history.create_session("session-a", "cli", model="model-new")
    old_id = history.append_message("session-a", "user", content="web original")
    store = ApplicationStore(tmp_path / "application.sqlite3")
    web = SessionService._new_snapshot("session-a", str(tmp_path), True, "model-old")
    web["messages"] = [{"id": "web-id", "role": "user", "content": "web original", "process": [{"title": "kept"}]}]
    store.save(web)
    queue = terminal_queue(store, "session-a", history_after=old_id)
    history.append_message("session-a", "user", content="terminal question")
    history.append_message("session-a", "assistant", content="terminal response")
    queue.submit("next task")
    adopted = adopt_terminal_queue(store, "session-a", history=history)
    assert [m["content"] for m in adopted["messages"]] == ["web original", "terminal question", "terminal response"]
    assert adopted["messages"][0]["process"] == [{"title": "kept"}]
    assert adopted["model"] == "model-new"
    assert adopted["queue"][0]["content"] == "next task"
    assert adopt_terminal_queue(store, "session-a", history=history) is None
    history.close()
    store.close()


def test_first_terminal_history_import_does_not_duplicate_existing_imported_rows(tmp_path):
    from mclaw.state import SessionDB

    history = SessionDB(tmp_path / "state.db")
    history.create_session("session-a", "cli")
    store = ApplicationStore(tmp_path / "application.sqlite3")
    queue = terminal_queue(store, "session-a")
    queue.submit("task")
    message_id = history.append_message("session-a", "user", content="task")
    # SessionService imports the legacy history before acquiring the entry lock.
    imported = SessionService._new_snapshot("session-a", str(tmp_path), True, "")
    imported["messages"] = [{"id": f"history_{message_id}", "role": "user", "content": "task", "process": []}]
    store.save(imported)
    adopted = adopt_terminal_queue(store, "session-a", history=history)
    assert len(adopted["messages"]) == 1
    assert adopted["queue"][0]["content"] == "task"
    history.close()
    store.close()


def test_terminal_handoff_refreshes_old_rollback_flags_and_title(tmp_path):
    from mclaw.state import SessionDB

    history = SessionDB(tmp_path / "state.db")
    history.create_session("session-a", "cli")
    marker = history.append_message("session-a", "system", content="system")
    user_id = history.append_message("session-a", "user", content="original", operation_id="task:task-a")
    assistant_id = history.append_message("session-a", "assistant", content="result")
    store = ApplicationStore(tmp_path / "application.sqlite3")
    web = SessionService._new_snapshot("session-a", str(tmp_path), True, "")
    web["messages"] = [
        {"id": "task-a", "role": "user", "content": "original", "process": []},
        {"id": "attempt-a", "role": "assistant", "content": "result", "process": [{"title": "kept"}]},
    ]
    store.save(web)
    terminal_queue(store, "session-a", history_after=assistant_id)
    history.invalidate_messages_after("session-a", marker, rollback_id="rollback-test")
    history.set_session_title("session-a", "renamed in terminal")
    adopted = adopt_terminal_queue(store, "session-a", history=history)
    assert all(message["invalidated"] for message in adopted["messages"])
    assert adopted["messages"][1]["process"] == [{"title": "kept"}]
    assert adopted["title"] == "renamed in terminal"
    assert len(adopted["messages"]) == 2
    history.close()
    store.close()
