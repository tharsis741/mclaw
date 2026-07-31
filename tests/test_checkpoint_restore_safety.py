# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from dataclasses import fields
import json
from pathlib import Path
from types import SimpleNamespace
import threading
import time

import pytest

from mclaw.cli.app import InteractiveChat
from mclaw.cli.runtime.file_safety_commands import (
    RuntimeFileSafetyCommandCoordinator,
    RuntimeFileSafetyCommandHooks,
)
from mclaw.runtime.base import Runtime
from mclaw.runtime.manager import RuntimeManager
from mclaw.runtime.paths import PathPolicy
from mclaw.safety import rollback_coordinator
from mclaw.safety.operation_journal import inspect_path
from mclaw.safety.rollback_coordinator import RollbackCoordinator
from mclaw.state import SessionDB
from mclaw.tools import checkpoint_manager, terminal_tool
from mclaw.tools.interrupt import reset_interrupt_event, set_interrupt_event


def _use_path_policy(monkeypatch, policy: PathPolicy) -> None:
    runtime = object.__new__(Runtime)
    runtime.paths = policy
    monkeypatch.setattr(RuntimeManager, "_current", runtime)


def _real_checkpoint_manager(monkeypatch, tmp_path: Path) -> checkpoint_manager.CheckpointManager:
    if checkpoint_manager.shutil.which("git") is None:
        pytest.skip("git is unavailable")
    monkeypatch.setattr(checkpoint_manager, "CHECKPOINT_BASE", tmp_path / "checkpoint-store")
    return checkpoint_manager.CheckpointManager(enabled=True)


def test_runtime_internal_directories_never_create_or_restore_checkpoints(
    monkeypatch,
    tmp_path: Path,
) -> None:
    mclaw_home = tmp_path / ".mclaw"
    internal = mclaw_home / "sessions"
    internal.mkdir(parents=True)
    (internal / "session.json").write_text("private", encoding="utf-8")
    monkeypatch.setenv("MCLAW_HOME", str(mclaw_home))
    _use_path_policy(monkeypatch, PathPolicy(mclaw_home=mclaw_home))

    manager = checkpoint_manager.CheckpointManager(enabled=True)
    manager._git_available = True
    monkeypatch.setattr(
        manager,
        "_take",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("internal state must not be snapshotted")),
    )

    assert manager.create_checkpoint(str(internal)) is False
    assert manager.last_attempt["status"] == "skipped"
    assert manager.list_checkpoints(str(internal)) == []
    result = manager.restore(str(internal), "abcd", create_pre_snapshot=False)
    assert result["success"] is False
    assert "runtime directory" in result["error"]


def test_checkpoint_store_excludes_credentials_even_for_explicit_targets(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    normal = workspace / "data.txt"
    normal.write_text("first", encoding="utf-8")
    mclaw_home = tmp_path / ".mclaw"
    monkeypatch.setenv("MCLAW_HOME", str(mclaw_home))
    _use_path_policy(monkeypatch, PathPolicy(mclaw_home=mclaw_home))
    manager = _real_checkpoint_manager(monkeypatch, tmp_path)
    assert manager.create_checkpoint(str(workspace), "initialize store")

    store = checkpoint_manager._store_path(checkpoint_manager.CHECKPOINT_BASE)
    (store / "info" / "exclude").write_text("", encoding="utf-8")
    secrets = [
        workspace / ".npmrc",
        workspace / ".ssh" / "id_ed25519",
        workspace / ".aws" / "credentials",
    ]
    for secret in secrets:
        secret.parent.mkdir(parents=True, exist_ok=True)
        secret.write_text("secret", encoding="utf-8")
    normal.write_text("second", encoding="utf-8")

    assert manager.create_checkpoint(
        str(workspace),
        "credential exclusion",
        target_paths=[str(secrets[0])],
    )
    commit = manager.last_attempt["commit"]
    ok, tree, error = checkpoint_manager._run_git(
        ["ls-tree", "-r", "--name-only", commit],
        store,
        str(workspace),
    )
    assert ok, error
    assert tree.splitlines() == ["data.txt"]


def test_restore_refuses_a_protected_anchor_before_mutating_files(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "data.txt"
    target.write_text("before", encoding="utf-8")
    mclaw_home = tmp_path / ".mclaw"
    monkeypatch.setenv("MCLAW_HOME", str(mclaw_home))
    _use_path_policy(monkeypatch, PathPolicy(mclaw_home=mclaw_home))
    manager = _real_checkpoint_manager(monkeypatch, tmp_path)
    assert manager.create_checkpoint(str(workspace), "before restore")
    commit = manager.last_attempt["commit"]

    target.write_text("after", encoding="utf-8")
    restored = manager.restore(str(workspace), commit, create_pre_snapshot=False)
    assert restored["success"] is True
    assert target.read_text(encoding="utf-8") == "before"

    target.write_text("after", encoding="utf-8")
    _use_path_policy(
        monkeypatch,
        PathPolicy(mclaw_home=mclaw_home, protected_anchors=(workspace,)),
    )
    result = manager.restore(str(workspace), commit, create_pre_snapshot=False)

    assert result["success"] is False
    assert "protected_anchor" in result["error"]
    assert target.read_text(encoding="utf-8") == "after"


def test_operation_rollback_and_undo_only_touch_the_recorded_targets(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "target.txt"
    unrelated = workspace / "unrelated.txt"
    target.write_text("before", encoding="utf-8")
    unrelated.write_text("initial", encoding="utf-8")
    mclaw_home = tmp_path / ".mclaw"
    monkeypatch.setenv("MCLAW_HOME", str(mclaw_home))
    _use_path_policy(monkeypatch, PathPolicy(mclaw_home=mclaw_home))
    manager = _real_checkpoint_manager(monkeypatch, tmp_path)
    assert manager.create_checkpoint(
        str(workspace),
        "before operation",
        target_paths=[str(target)],
    )
    before_commit = manager.last_attempt["commit"]

    before = inspect_path(str(target))
    target.write_text("after", encoding="utf-8")
    unrelated.write_text("later", encoding="utf-8")
    operation = {
        "operation_id": "op-1",
        "session_id": "session-1",
        "turn_id": "turn-1",
        "workspace": str(workspace),
        "cwd": str(workspace),
        "before_commit": before_commit,
        "targets": [{"path": str(target), "before": before, "after": inspect_path(str(target))}],
    }

    class Journal:
        def get_operation(self, *_args, **_kwargs):
            return operation

        def record_rollback(self, **kwargs):
            return {
                "operation_id": kwargs["rollback_id"],
                "session_id": kwargs["session_id"],
                "workspace": str(workspace),
                "before_commit": kwargs["pre_rollback_commit"],
                "targets": operation["targets"],
                "rollback": {"context_rollback_id": None},
            }

    coordinator = RollbackCoordinator(checkpoint_manager=manager, journal=Journal())
    coordinator.context.apply = lambda **_kwargs: {"skipped": True}
    diff = coordinator.diff_operation("op-1", session_id="session-1", workspace=str(workspace))
    assert diff["success"] is True
    assert "target.txt" in diff["diff"]
    assert "unrelated.txt" not in diff["diff"]

    result = coordinator.rollback_operation("op-1", session_id="session-1", workspace=str(workspace))

    assert result["success"] is True
    assert target.read_text(encoding="utf-8") == "before"
    assert unrelated.read_text(encoding="utf-8") == "later"

    undo = coordinator._undo_rollback_record(result["rollback_record"], workspace=str(workspace))
    assert undo["success"] is True
    assert target.read_text(encoding="utf-8") == "after"
    assert unrelated.read_text(encoding="utf-8") == "later"


def test_operation_rollback_refuses_to_delete_runtime_state(
    monkeypatch,
    tmp_path: Path,
) -> None:
    mclaw_home = tmp_path / ".mclaw"
    target = mclaw_home / "sessions" / "created.json"
    target.parent.mkdir(parents=True)
    target.write_text("keep", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _use_path_policy(monkeypatch, PathPolicy(mclaw_home=mclaw_home))
    operation = {
        "operation_id": "op-internal",
        "workspace": str(workspace),
        "targets": [{
            "path": str(target),
            "before": {"exists": False},
            "after": {"exists": True},
        }],
    }

    class Journal:
        def get_operation(self, *_args, **_kwargs):
            return operation

    class Manager:
        def create_checkpoint(self, *_args, **_kwargs):
            raise AssertionError("blocked rollback must not create a checkpoint")

    result = RollbackCoordinator(checkpoint_manager=Manager(), journal=Journal()).rollback_operation(
        "op-internal",
        workspace=str(workspace),
    )

    assert result["success"] is False
    assert "runtime_internal" in result["error"]
    assert target.read_text(encoding="utf-8") == "keep"


def test_rollback_command_stops_before_building_a_coordinator_for_unsafe_workspaces(
    monkeypatch,
    tmp_path: Path,
) -> None:
    mclaw_home = tmp_path / ".mclaw"
    internal = mclaw_home / "sessions"
    internal.mkdir(parents=True)
    _use_path_policy(monkeypatch, PathPolicy(mclaw_home=mclaw_home))

    def noop(*_args, **_kwargs):
        return None

    for cwd in ("", str(internal)):
        notices = []
        hook_values = {field.name: noop for field in fields(RuntimeFileSafetyCommandHooks)}
        hook_values.update({
            "get_checkpoint_manager": lambda: SimpleNamespace(enabled=True),
            "create_rollback_coordinator": lambda _manager: (_ for _ in ()).throw(
                AssertionError("unsafe workspace must stop before coordinator creation")
            ),
            "resolve_rollback_workspace": lambda _options, value=cwd: (value, None),
            "render_notice": lambda *args: notices.append(args),
        })
        RuntimeFileSafetyCommandCoordinator(
            RuntimeFileSafetyCommandHooks(**hook_values)
        ).handle_rollback("")
        assert notices
        assert notices[-1][-1] == "danger"


def test_checkpoint_workspace_resolution_fails_closed_for_runtime_state(
    monkeypatch,
    tmp_path: Path,
) -> None:
    internal = tmp_path / ".mclaw" / "sessions"
    internal.mkdir(parents=True)
    _use_path_policy(monkeypatch, PathPolicy(mclaw_home=tmp_path / ".mclaw"))
    monkeypatch.setenv("TERMINAL_CWD", str(internal))
    monkeypatch.setattr(terminal_tool, "_current_session_id", None)
    monkeypatch.setattr(terminal_tool, "_env_registry", {})
    chat = object.__new__(InteractiveChat)
    chat.agent = SimpleNamespace(_last_checkpoint_work_dir=str(internal))
    chat.config = {
        "terminal": {"cwd": str(internal)},
        "_launch_cwd": str(internal),
    }

    assert chat._checkpoint_cwd() == ""


def test_conflict_backup_failure_stops_before_checkpoint_or_restore(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "settings.yaml"
    target.write_text("user edit", encoding="utf-8")
    _use_path_policy(monkeypatch, PathPolicy(mclaw_home=tmp_path / ".mclaw"))
    operation = {
        "operation_id": "op-conflict",
        "workspace": str(workspace),
        "before_commit": "abcd",
        "targets": [{
            "path": str(target),
            "before": {"exists": True},
            "after": {"exists": True, "kind": "file", "sha256": "different"},
        }],
    }

    class Journal:
        def get_operation(self, *_args, **_kwargs):
            return operation

    class Manager:
        def create_checkpoint(self, *_args, **_kwargs):
            raise AssertionError("backup failure must stop before checkpoint")

        def restore(self, *_args, **_kwargs):
            raise AssertionError("backup failure must stop before restore")

    monkeypatch.setattr(
        rollback_coordinator,
        "_backup_path",
        lambda _path: (_ for _ in ()).throw(OSError("disk full")),
    )
    result = RollbackCoordinator(checkpoint_manager=Manager(), journal=Journal()).rollback_operation(
        "op-conflict",
        workspace=str(workspace),
    )

    assert result["success"] is False
    assert "Conflict backup failed" in result["error"]
    assert target.read_text(encoding="utf-8") == "user edit"


def test_restore_stops_when_pre_restore_checkpoint_fails(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "data.txt"
    target.write_text("before", encoding="utf-8")
    _use_path_policy(monkeypatch, PathPolicy(mclaw_home=tmp_path / ".mclaw"))
    manager = _real_checkpoint_manager(monkeypatch, tmp_path)
    assert manager.create_checkpoint(str(workspace), "before")
    commit = manager.last_attempt["commit"]
    target.write_text("after", encoding="utf-8")

    def fail_checkpoint(*_args, **_kwargs):
        manager.last_attempt = {"status": "failed", "detail": "disk full"}
        return False

    monkeypatch.setattr(manager, "create_checkpoint", fail_checkpoint)
    result = manager.restore(str(workspace), commit)

    assert result["success"] is False
    assert "pre-restore checkpoint" in result["error"]
    assert target.read_text(encoding="utf-8") == "after"


def test_failed_checkpoint_does_not_reuse_an_old_last_attempt(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = checkpoint_manager.CheckpointManager(enabled=True)
    manager._git_available = True
    manager.last_attempt = {"status": "taken", "commit": "a" * 40}
    monkeypatch.setattr(manager, "_take", lambda *_args, **_kwargs: False)

    assert manager.create_checkpoint(str(workspace), "must fail") is False
    assert manager.last_attempt["status"] == "failed"
    assert "commit" not in manager.last_attempt


def test_full_restore_reuses_the_current_checkpoint_when_nothing_changed(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "data.txt"
    target.write_text("before", encoding="utf-8")
    _use_path_policy(monkeypatch, PathPolicy(mclaw_home=tmp_path / ".mclaw"))
    manager = _real_checkpoint_manager(monkeypatch, tmp_path)
    assert manager.create_checkpoint(str(workspace), "before")
    before_commit = manager.last_attempt["commit"]
    target.write_text("after", encoding="utf-8")
    assert manager.create_checkpoint(str(workspace), "after")
    after_commit = manager.last_attempt["commit"]

    result = manager.restore(str(workspace), before_commit)

    assert result["success"] is True
    assert result["pre_restore_commit"] == after_commit
    assert target.read_text(encoding="utf-8") == "before"


def test_pruning_keeps_checkpoint_hashes_stable_during_restore(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "data.txt"
    target.write_text("one", encoding="utf-8")
    _use_path_policy(monkeypatch, PathPolicy(mclaw_home=tmp_path / ".mclaw"))
    monkeypatch.setattr(checkpoint_manager, "CHECKPOINT_BASE", tmp_path / "checkpoint-store")
    manager = checkpoint_manager.CheckpointManager(enabled=True, max_snapshots=2)
    assert manager.create_checkpoint(str(workspace), "one")
    first_commit = manager.last_attempt["commit"]
    target.write_text("two", encoding="utf-8")
    assert manager.create_checkpoint(str(workspace), "two")
    target.write_text("three", encoding="utf-8")

    result = manager.restore(str(workspace), first_commit)

    assert result["success"] is True
    assert result["restored_hash"] == first_commit
    assert target.read_text(encoding="utf-8") == "one"


def test_multi_target_restore_compensates_after_later_checkout_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first = workspace / "first.txt"
    second = workspace / "second.txt"
    first.write_text("first-before", encoding="utf-8")
    second.write_text("second-before", encoding="utf-8")
    _use_path_policy(monkeypatch, PathPolicy(mclaw_home=tmp_path / ".mclaw"))
    manager = _real_checkpoint_manager(monkeypatch, tmp_path)
    assert manager.create_checkpoint(str(workspace), "before")
    before_commit = manager.last_attempt["commit"]
    first.write_text("first-after", encoding="utf-8")
    second.write_text("second-after", encoding="utf-8")

    real_run_git = checkpoint_manager._run_git
    failed = False

    def fail_second_checkout(args, *call_args, **call_kwargs):
        nonlocal failed
        if (
            not failed
            and len(args) >= 5
            and args[1] == "checkout"
            and args[2] == before_commit
            and args[-1] == "second.txt"
        ):
            failed = True
            return False, "", "file is locked"
        return real_run_git(args, *call_args, **call_kwargs)

    monkeypatch.setattr(checkpoint_manager, "_run_git", fail_second_checkout)
    result = manager.restore(
        str(workspace),
        before_commit,
        target_paths=[str(first), str(second)],
    )

    assert result["success"] is False
    assert result["failed_target"] == "second.txt"
    assert result["restored_targets"] == ["first.txt"]
    assert result["recovered"] is True
    assert first.read_text(encoding="utf-8") == "first-after"
    assert second.read_text(encoding="utf-8") == "second-after"


def test_created_target_rollback_stops_when_recovery_checkpoint_fails(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "created.txt"
    target.write_text("keep", encoding="utf-8")
    _use_path_policy(monkeypatch, PathPolicy(mclaw_home=tmp_path / ".mclaw"))
    operation = {
        "operation_id": "op-created",
        "workspace": str(workspace),
        "targets": [{
            "path": str(target),
            "before": {"exists": False},
            "after": inspect_path(str(target)),
        }],
    }

    class Journal:
        def get_operation(self, *_args, **_kwargs):
            return operation

    class Manager:
        last_attempt = {}

        def create_checkpoint(self, *_args, **_kwargs):
            self.last_attempt = {"status": "failed", "detail": "store is read-only"}
            return False

        def restore(self, *_args, **_kwargs):
            raise AssertionError("failed checkpoint must stop before deletion")

    result = RollbackCoordinator(checkpoint_manager=Manager(), journal=Journal()).rollback_operation(
        "op-created",
        workspace=str(workspace),
    )

    assert result["success"] is False
    assert "pre-rollback checkpoint" in result["error"]
    assert target.read_text(encoding="utf-8") == "keep"


def test_turn_rollback_compensates_completed_operations_after_later_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    targets = [workspace / "first.txt", workspace / "second.txt"]
    for target in targets:
        target.write_text("after", encoding="utf-8")
    _use_path_policy(monkeypatch, PathPolicy(mclaw_home=tmp_path / ".mclaw"))
    operations = [{
        "operation_id": f"op-{index}",
        "turn_id": "turn-1",
        "workspace": str(workspace),
        "before_commit": "b" * 40,
        "targets": [{
            "path": str(target),
            "before": {"exists": False},
            "after": inspect_path(str(target)),
        }],
    } for index, target in enumerate(targets, 1)]

    class Journal:
        def get_operation(self, *_args, **_kwargs):
            return operations[0]

        def list_operations(self, *_args, **_kwargs):
            return operations

    class Manager:
        last_attempt = {}

        def __init__(self):
            self.restore_calls = []

        def create_checkpoint(self, *_args, **_kwargs):
            self.last_attempt = {"status": "taken", "commit": "a" * 40}
            return True

        def begin_restore_intent(self, _work_dir, **kwargs):
            return {
                "success": True,
                "intent_id": "intent-1",
                "recovery_commit": "a" * 40,
                "target_paths": kwargs["target_paths"],
            }

        def finish_restore_intent(self, *_args, **_kwargs):
            return True

        def restore(self, *args, **kwargs):
            self.restore_calls.append((args, kwargs))
            return {"success": True}

    manager = Manager()
    coordinator = RollbackCoordinator(checkpoint_manager=manager, journal=Journal())
    rollback_calls = []

    def rollback_operation(ref, **kwargs):
        rollback_calls.append((ref, kwargs))
        if ref == "op-2":
            return {"success": False, "error": "second operation failed"}
        return {"success": True}

    monkeypatch.setattr(coordinator, "rollback_operation", rollback_operation)
    result = coordinator.rollback_turn("turn-1", workspace=str(workspace))

    assert result["success"] is False
    assert all(call[1]["recovery_commit"] == "a" * 40 for call in rollback_calls)
    assert result["compensation"]["success"] is True
    assert manager.restore_calls[-1][0][1] == "a" * 40
    assert manager.restore_calls[-1][1]["target_paths"] == [str(path) for path in targets]


def test_interrupted_restore_can_be_recovered_on_the_next_command(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first = workspace / "first.txt"
    second = workspace / "second.txt"
    first.write_text("before-first", encoding="utf-8")
    second.write_text("before-second", encoding="utf-8")
    _use_path_policy(monkeypatch, PathPolicy(mclaw_home=tmp_path / ".mclaw"))
    manager = _real_checkpoint_manager(monkeypatch, tmp_path)
    assert manager.create_checkpoint(str(workspace), "before")
    before_commit = manager.last_attempt["commit"]
    first.write_text("after-first", encoding="utf-8")
    second.write_text("after-second", encoding="utf-8")

    cancel_event = threading.Event()
    token = set_interrupt_event(cancel_event)
    real_run_git = checkpoint_manager._run_git
    interrupted = False

    def interrupt_after_first_checkout(args, *call_args, **call_kwargs):
        nonlocal interrupted
        result = real_run_git(args, *call_args, **call_kwargs)
        if (
            not interrupted
            and len(args) >= 5
            and args[1] == "checkout"
            and args[2] == before_commit
            and args[-1] == "first.txt"
        ):
            interrupted = True
            cancel_event.set()
        return result

    monkeypatch.setattr(checkpoint_manager, "_run_git", interrupt_after_first_checkout)
    try:
        with pytest.raises(InterruptedError):
            manager.restore(
                str(workspace),
                before_commit,
                target_paths=[str(first), str(second)],
            )
    finally:
        reset_interrupt_event(token)

    assert first.read_text(encoding="utf-8") == "before-first"
    assert second.read_text(encoding="utf-8") == "after-second"
    pending = manager.list_restore_intents(str(workspace))
    assert len(pending) == 1
    assert pending[0]["status"] == "interrupted"

    recovered = manager.recover_restore(working_dir=str(workspace))

    assert recovered["success"] is True
    assert first.read_text(encoding="utf-8") == "after-first"
    assert second.read_text(encoding="utf-8") == "after-second"
    assert manager.list_restore_intents(str(workspace)) == []


def test_context_rollbacks_are_restored_in_one_transaction(
    monkeypatch,
    tmp_path: Path,
) -> None:
    db = SessionDB(tmp_path / "state.db")
    db.create_session("session-1", "test")
    marker = db.append_message("session-1", "user", "start")
    db.append_message("session-1", "assistant", "one", operation_id="op-1")
    db.append_message("session-1", "assistant", "two", operation_id="op-2")
    db.invalidate_operation_context(
        "session-1",
        marker,
        operation_id="op-1",
        rollback_id="ctx-1",
    )
    db.invalidate_operation_context(
        "session-1",
        marker,
        operation_id="op-2",
        rollback_id="ctx-2",
    )

    monkeypatch.setattr(
        db,
        "_recompute_session_counts_in_tx",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("count update failed")),
    )
    with pytest.raises(RuntimeError, match="count update failed"):
        db.restore_context_rollbacks(["ctx-1", "ctx-2"])

    assert [message["content"] for message in db.get_messages("session-1")] == ["start"]
    rows = db._conn.execute(
        "SELECT restored_at FROM context_rollbacks ORDER BY id"
    ).fetchall()
    assert [row["restored_at"] for row in rows] == [None, None]


def test_group_context_failure_compensates_the_whole_file_batch(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first = workspace / "first.txt"
    second = workspace / "second.txt"
    first.write_text("undone-first", encoding="utf-8")
    second.write_text("undone-second", encoding="utf-8")
    operations = [
        {
            "operation_id": "op-1",
            "targets": [{
                "path": str(first),
                "before": inspect_path(str(first)),
                "after": {"exists": True, "kind": "file", "sha256": "after"},
            }],
        },
        {
            "operation_id": "op-2",
            "targets": [{
                "path": str(second),
                "before": inspect_path(str(second)),
                "after": {"exists": True, "kind": "file", "sha256": "after"},
            }],
        },
    ]
    records = [
        {
            "before_commit": str(index) * 40,
            "workspace": str(workspace),
            "session_id": "session-1",
            "targets": operation["targets"],
            "rollback": {"context_rollback_id": f"ctx-{index}"},
        }
        for index, operation in enumerate(operations, 1)
    ]

    class Manager:
        def restore_batch(self, _work_dir, _restores, **_kwargs):
            first.write_text("after-first", encoding="utf-8")
            second.write_text("after-second", encoding="utf-8")
            return {
                "success": True,
                "results": [{"success": True}, {"success": True}],
                "recovery_commit": "a" * 40,
                "target_paths": [str(first), str(second)],
            }

        def restore(self, *_args, **_kwargs):
            first.write_text("undone-first", encoding="utf-8")
            second.write_text("undone-second", encoding="utf-8")
            return {"success": True}

    coordinator = RollbackCoordinator(checkpoint_manager=Manager(), journal=SimpleNamespace())
    monkeypatch.setattr(
        coordinator,
        "get_group",
        lambda *_args, **_kwargs: {"operations": operations},
    )
    monkeypatch.setattr(
        coordinator,
        "_rollback_records_for_operations",
        lambda *_args, **_kwargs: records,
    )
    monkeypatch.setattr(
        coordinator.context,
        "restore_many",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("database unavailable")),
    )

    result = coordinator.restore_group("1", workspace=str(workspace))

    assert result["success"] is False
    assert result["compensation"]["success"] is True
    assert first.read_text(encoding="utf-8") == "undone-first"
    assert second.read_text(encoding="utf-8") == "undone-second"


def test_batch_restore_applies_multiple_commits_with_one_recovery_snapshot(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first = workspace / "first.txt"
    second = workspace / "second.txt"
    first.write_text("after-first", encoding="utf-8")
    second.write_text("base-second", encoding="utf-8")
    _use_path_policy(monkeypatch, PathPolicy(mclaw_home=tmp_path / ".mclaw"))
    manager = _real_checkpoint_manager(monkeypatch, tmp_path)
    assert manager.create_checkpoint(str(workspace), "first state")
    first_commit = manager.last_attempt["commit"]
    second.write_text("after-second", encoding="utf-8")
    assert manager.create_checkpoint(str(workspace), "second state")
    second_commit = manager.last_attempt["commit"]
    first.write_text("undone-first", encoding="utf-8")
    second.write_text("undone-second", encoding="utf-8")

    result = manager.restore_batch(
        str(workspace),
        [
            {"commit_hash": first_commit, "target_paths": [str(first)]},
            {"commit_hash": second_commit, "target_paths": [str(second)]},
        ],
    )

    assert result["success"] is True
    assert first.read_text(encoding="utf-8") == "after-first"
    assert second.read_text(encoding="utf-8") == "after-second"
    assert result["recovery_commit"]


def test_existing_tracked_credentials_are_removed_and_old_snapshot_is_purged(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    dormant_workspace = tmp_path / "dormant"
    dormant_workspace.mkdir()
    secret = workspace / ".npmrc"
    dormant_secret = dormant_workspace / ".npmrc"
    normal = workspace / "data.txt"
    secret.write_text("TOKEN=legacy", encoding="utf-8")
    dormant_secret.write_text("TOKEN=dormant", encoding="utf-8")
    normal.write_text("one", encoding="utf-8")
    _use_path_policy(monkeypatch, PathPolicy(mclaw_home=tmp_path / ".mclaw"))
    manager = _real_checkpoint_manager(monkeypatch, tmp_path)

    original_excludes = list(checkpoint_manager.DEFAULT_EXCLUDES)
    credential_check = PathPolicy.is_credential_path
    monkeypatch.setattr(
        checkpoint_manager,
        "DEFAULT_EXCLUDES",
        [item for item in original_excludes if item != ".npmrc"],
    )
    monkeypatch.setattr(
        PathPolicy,
        "is_credential_path",
        staticmethod(lambda _path: False),
    )
    assert manager.create_checkpoint(str(workspace), "legacy credential snapshot")
    old_commit = manager.last_attempt["commit"]
    assert manager.create_checkpoint(str(dormant_workspace), "dormant credential snapshot")
    dormant_commit = manager.last_attempt["commit"]

    monkeypatch.setattr(checkpoint_manager, "DEFAULT_EXCLUDES", original_excludes)
    monkeypatch.setattr(
        PathPolicy,
        "is_credential_path",
        staticmethod(credential_check),
    )
    store = checkpoint_manager._store_path(checkpoint_manager.CHECKPOINT_BASE)
    (store / checkpoint_manager._CREDENTIAL_SANITIZE_MARKER_NAME).unlink()
    normal.write_text("two", encoding="utf-8")
    assert manager.create_checkpoint(str(workspace), "sanitized snapshot")
    current_commit = manager.last_attempt["commit"]

    ok, tree, error = checkpoint_manager._run_git(
        ["ls-tree", "-r", "--name-only", current_commit],
        store,
        str(workspace),
    )
    assert ok, error
    assert tree.splitlines() == ["data.txt"]
    old_ref = checkpoint_manager._snapshot_ref_name(
        checkpoint_manager._project_hash(str(workspace)),
        old_commit,
    )
    retained, _, _ = checkpoint_manager._run_git(
        ["show-ref", "--verify", "--quiet", old_ref],
        store,
        str(workspace),
        allowed_returncodes={1, 128},
    )
    assert retained is False
    dormant_ref = checkpoint_manager._snapshot_ref_name(
        checkpoint_manager._project_hash(str(dormant_workspace)),
        dormant_commit,
    )
    retained, _, _ = checkpoint_manager._run_git(
        ["show-ref", "--verify", "--quiet", dormant_ref],
        store,
        str(dormant_workspace),
        allowed_returncodes={1, 128},
    )
    assert retained is False
    ok, tracked, error = checkpoint_manager._run_git(
        ["ls-files", "--cached"],
        store,
        str(dormant_workspace),
        index_file=checkpoint_manager._index_path(
            store,
            checkpoint_manager._project_hash(str(dormant_workspace)),
        ),
    )
    assert ok, error
    assert tracked == ""


def test_turn_interruption_leaves_one_group_recovery_intent(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    targets = [workspace / "first.txt", workspace / "second.txt"]
    for target in targets:
        target.write_text("after", encoding="utf-8")
    operations = [{
        "operation_id": f"op-{index}",
        "turn_id": "turn-1",
        "workspace": str(workspace),
        "before_commit": "b" * 40,
        "created_at": f"2026-01-01T00:00:0{index}+0000",
        "targets": [{
            "path": str(target),
            "before": {"exists": False},
            "after": inspect_path(str(target)),
        }],
    } for index, target in enumerate(targets, 1)]

    class Journal:
        def get_operation(self, *_args, **_kwargs):
            return operations[0]

        def list_operations(self, *_args, **_kwargs):
            return operations

    class Manager:
        last_attempt = {}

        def __init__(self):
            self.finished = []

        def create_checkpoint(self, *_args, **_kwargs):
            self.last_attempt = {"status": "taken", "commit": "a" * 40}
            return True

        def begin_restore_intent(self, _work_dir, **kwargs):
            return {
                "success": True,
                "intent_id": "turn-intent",
                "recovery_commit": "a" * 40,
                "target_paths": kwargs["target_paths"],
            }

        def finish_restore_intent(self, intent_id, status, **kwargs):
            self.finished.append((intent_id, status))
            return True

    manager = Manager()
    coordinator = RollbackCoordinator(checkpoint_manager=manager, journal=Journal())
    calls = 0

    def rollback_operation(_ref, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise InterruptedError("cancelled between operations")
        return {"success": True}

    monkeypatch.setattr(coordinator, "rollback_operation", rollback_operation)

    with pytest.raises(InterruptedError, match="between operations"):
        coordinator.rollback_turn("turn-1", workspace=str(workspace))

    assert manager.finished == [("turn-intent", "interrupted")]


def test_turn_context_id_is_persisted_to_each_rollback_record(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    targets = [workspace / "first.txt", workspace / "second.txt"]
    for target in targets:
        target.write_text("after", encoding="utf-8")
    operations = [{
        "operation_id": f"op-{index}",
        "turn_id": "turn-1",
        "session_id": "session-1",
        "message_id_before_turn": 1,
        "workspace": str(workspace),
        "before_commit": "b" * 40,
        "created_at": f"2026-01-01T00:00:0{index}+0000",
        "targets": [{
            "path": str(target),
            "before": {"exists": False},
            "after": inspect_path(str(target)),
        }],
    } for index, target in enumerate(targets, 1)]

    class Journal:
        def get_operation(self, *_args, **_kwargs):
            return operations[0]

        def list_operations(self, *_args, **_kwargs):
            return operations

        def update_rollback_context(self, record, context_id):
            record.setdefault("rollback", {})["context_rollback_id"] = context_id
            return record

    class Manager:
        last_attempt = {}

        def create_checkpoint(self, *_args, **_kwargs):
            self.last_attempt = {"status": "taken", "commit": "a" * 40}
            return True

        def begin_restore_intent(self, _work_dir, **kwargs):
            return {
                "success": True,
                "intent_id": "turn-intent",
                "recovery_commit": "a" * 40,
                "target_paths": kwargs["target_paths"],
            }

        def finish_restore_intent(self, *_args, **_kwargs):
            return True

    class Context:
        def apply(self, **kwargs):
            return {"rollback_id": kwargs["rollback_id"], "invalidated": 2}

    coordinator = RollbackCoordinator(checkpoint_manager=Manager(), journal=Journal())
    coordinator.context = Context()
    rollback_records = []

    def rollback_operation(ref, **_kwargs):
        record = {
            "operation_id": f"rollback-{ref}",
            "rollback": {"context_rollback_id": None},
        }
        rollback_records.append(record)
        return {"success": True, "rollback_record": record}

    monkeypatch.setattr(coordinator, "rollback_operation", rollback_operation)
    result = coordinator.rollback_turn(
        "turn-1",
        session_id="session-1",
        workspace=str(workspace),
    )

    context_id = result["context"]["rollback_id"]
    assert context_id
    assert {
        record["rollback"]["context_rollback_id"]
        for record in rollback_records
    } == {context_id}


def test_older_root_snapshot_accepts_the_displayed_short_hash(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "data.txt"
    _use_path_policy(monkeypatch, PathPolicy(mclaw_home=tmp_path / ".mclaw"))
    manager = _real_checkpoint_manager(monkeypatch, tmp_path)

    target.write_text("one", encoding="utf-8")
    assert manager.create_checkpoint(str(workspace), "one")
    first_commit = manager.last_attempt["commit"]
    target.write_text("two", encoding="utf-8")
    assert manager.create_checkpoint(str(workspace), "two")
    first = next(
        item
        for item in manager.list_checkpoints(str(workspace))
        if item["hash"] == first_commit
    )

    assert manager.diff(str(workspace), first["short_hash"])["success"] is True


def test_rollback_list_hides_groups_whose_checkpoint_was_pruned(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "data.txt"
    _use_path_policy(monkeypatch, PathPolicy(mclaw_home=tmp_path / ".mclaw"))
    manager = _real_checkpoint_manager(monkeypatch, tmp_path)
    manager.max_snapshots = 2
    operations = []
    for index in range(3):
        target.write_text(str(index), encoding="utf-8")
        assert manager.create_checkpoint(str(workspace), f"checkpoint-{index}")
        operations.insert(0, {
            "operation_id": f"op-{index}",
            "turn_id": f"turn-{index}",
            "workspace": str(workspace),
            "before_commit": manager.last_attempt["commit"],
            "targets": [{"path": str(target)}],
        })

    journal = SimpleNamespace(
        list_operations=lambda **_kwargs: operations,
    )
    visible = RollbackCoordinator(
        checkpoint_manager=manager,
        journal=journal,
    ).list_operations(workspace=str(workspace), limit=10)

    assert [record["operation_id"] for record in visible] == ["op-2", "op-1"]


def test_finish_restore_intent_reports_status_write_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "data.txt"
    target.write_text("one", encoding="utf-8")
    _use_path_policy(monkeypatch, PathPolicy(mclaw_home=tmp_path / ".mclaw"))
    manager = _real_checkpoint_manager(monkeypatch, tmp_path)
    assert manager.create_checkpoint(str(workspace), "one")
    commit = manager.last_attempt["commit"]
    store = checkpoint_manager._store_path(checkpoint_manager.CHECKPOINT_BASE)
    intent = {
        "id": "status-write-demo",
        "status": "applying",
        "created_at": time.time(),
        "workspace": str(workspace.resolve()),
        "target_commit": commit,
        "recovery_commit": commit,
        "target_paths": ["data.txt"],
    }
    checkpoint_manager._write_restore_intent(store, intent)
    monkeypatch.setattr(
        checkpoint_manager,
        "atomic_json_write",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    assert manager.finish_restore_intent("status-write-demo", "completed") is False
    persisted = json.loads(
        checkpoint_manager._restore_intent_path(
            store,
            "status-write-demo",
        ).read_text(encoding="utf-8")
    )
    assert persisted["status"] == "applying"


def test_expired_batch_intent_releases_every_target_pin(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "data.txt"
    _use_path_policy(monkeypatch, PathPolicy(mclaw_home=tmp_path / ".mclaw"))
    manager = _real_checkpoint_manager(monkeypatch, tmp_path)
    commits = []
    for index in range(3):
        target.write_text(str(index), encoding="utf-8")
        assert manager.create_checkpoint(str(workspace), f"checkpoint-{index}")
        commits.append(manager.last_attempt["commit"])
    store = checkpoint_manager._store_path(checkpoint_manager.CHECKPOINT_BASE)
    intent_id = "batch-pin-demo"
    for commit in commits:
        assert checkpoint_manager._pin_checkpoint(store, commit, intent_id)
    intent = {
        "id": intent_id,
        "status": "completed",
        "created_at": time.time() - 10,
        "workspace": str(workspace.resolve()),
        "target_commit": commits[0],
        "target_commits": commits[:2],
        "recovery_commit": commits[2],
        "target_paths": ["data.txt"],
    }
    checkpoint_manager._write_restore_intent(store, intent)

    checkpoint_manager._expire_restore_intents(store, time.time() + 1)

    assert not checkpoint_manager._restore_intent_path(store, intent_id).exists()
    assert all(
        not (checkpoint_manager._read_checkpoint_meta(store, commit) or {}).get("recovery_pins")
        for commit in commits
    )


def test_coordinated_recovery_restores_turn_files_and_context(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "data.txt"
    _use_path_policy(monkeypatch, PathPolicy(mclaw_home=tmp_path / ".mclaw"))
    manager = _real_checkpoint_manager(monkeypatch, tmp_path)

    target.write_text("before", encoding="utf-8")
    assert manager.create_checkpoint(str(workspace), "before")
    before_commit = manager.last_attempt["commit"]
    target.write_text("after", encoding="utf-8")
    assert manager.create_checkpoint(str(workspace), "after")
    recovery_commit = manager.last_attempt["commit"]

    rollback_id = "ctxrb-turn-recovery"
    intent = manager.begin_restore_intent(
        str(workspace),
        target_commits=[before_commit],
        recovery_commit=recovery_commit,
        target_paths=[str(target)],
        kind="rollback-turn",
        context_recovery={
            "action": "restore",
            "rollback_ids": [rollback_id],
            "session_id": "session-1",
        },
    )
    assert intent["success"]
    assert manager.restore(
        str(workspace),
        before_commit,
        target_paths=[str(target)],
        create_pre_snapshot=False,
        recovery_commit=recovery_commit,
        recovery_intent_id=intent["intent_id"],
    )["success"]

    db = SessionDB(tmp_path / "session.db")
    db.create_session("session-1", "test", workspace=str(workspace))
    marker = db.append_message("session-1", "user", "before turn", turn_id="turn-0")
    db.append_message("session-1", "assistant", "after turn", turn_id="turn-1")
    context = rollback_coordinator.ContextRollbackManager(session_db=db)
    assert context.apply(
        session_id="session-1",
        marker_message_id=marker,
        mode="soft",
        turn_id="turn-1",
        rollback_id=rollback_id,
    )["invalidated"] == 1

    result = RollbackCoordinator(
        checkpoint_manager=manager,
        session_db=db,
        journal=SimpleNamespace(),
    ).recover_restore(intent["intent_id"], workspace=str(workspace))

    assert result["success"] is True
    assert target.read_text(encoding="utf-8") == "after"
    assert len(db.get_messages("session-1")) == 2
    assert manager.list_restore_intents(str(workspace)) == []
    db.close()


def test_coordinated_recovery_reapplies_context_after_group_restore(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "data.txt"
    _use_path_policy(monkeypatch, PathPolicy(mclaw_home=tmp_path / ".mclaw"))
    manager = _real_checkpoint_manager(monkeypatch, tmp_path)

    target.write_text("rolled-back", encoding="utf-8")
    assert manager.create_checkpoint(str(workspace), "rolled back")
    recovery_commit = manager.last_attempt["commit"]
    target.write_text("restored", encoding="utf-8")
    assert manager.create_checkpoint(str(workspace), "restored")
    restored_commit = manager.last_attempt["commit"]
    target.write_text("rolled-back", encoding="utf-8")

    rollback_id = "ctxrb-group-recovery"
    db = SessionDB(tmp_path / "session.db")
    db.create_session("session-1", "test", workspace=str(workspace))
    marker = db.append_message("session-1", "user", "before turn", turn_id="turn-0")
    db.append_message("session-1", "assistant", "after turn", turn_id="turn-1")
    context = rollback_coordinator.ContextRollbackManager(session_db=db)
    assert context.apply(
        session_id="session-1",
        marker_message_id=marker,
        mode="soft",
        turn_id="turn-1",
        rollback_id=rollback_id,
    )["invalidated"] == 1

    intent = manager.begin_restore_intent(
        str(workspace),
        target_commits=[restored_commit],
        recovery_commit=recovery_commit,
        target_paths=[str(target)],
        kind="restore-batch",
        context_recovery={
            "action": "reapply",
            "rollback_ids": [rollback_id],
            "session_id": "session-1",
        },
    )
    assert intent["success"]
    assert manager.restore(
        str(workspace),
        restored_commit,
        target_paths=[str(target)],
        create_pre_snapshot=False,
        recovery_commit=recovery_commit,
        recovery_intent_id=intent["intent_id"],
    )["success"]
    assert context.restore_many([rollback_id], session_id="session-1")["restored"] == 1

    result = RollbackCoordinator(
        checkpoint_manager=manager,
        session_db=db,
        journal=SimpleNamespace(),
    ).recover_restore(intent["intent_id"], workspace=str(workspace))

    assert result["success"] is True
    assert target.read_text(encoding="utf-8") == "rolled-back"
    assert len(db.get_messages("session-1")) == 1
    assert len(db.get_messages("session-1", include_invalidated=True)) == 2
    db.close()


def test_outer_restore_intent_does_not_leave_anonymous_pin(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "data.txt"
    _use_path_policy(monkeypatch, PathPolicy(mclaw_home=tmp_path / ".mclaw"))
    manager = _real_checkpoint_manager(monkeypatch, tmp_path)

    target.write_text("before", encoding="utf-8")
    assert manager.create_checkpoint(str(workspace), "before")
    before_commit = manager.last_attempt["commit"]
    target.write_text("after", encoding="utf-8")
    assert manager.create_checkpoint(str(workspace), "after")
    recovery_commit = manager.last_attempt["commit"]
    intent = manager.begin_restore_intent(
        str(workspace),
        target_commits=[before_commit],
        recovery_commit=recovery_commit,
        target_paths=[str(target)],
    )
    assert intent["success"]
    assert manager.restore(
        str(workspace),
        before_commit,
        target_paths=[str(target)],
        create_pre_snapshot=False,
        recovery_commit=recovery_commit,
        recovery_intent_id=intent["intent_id"],
    )["success"]
    assert manager.finish_restore_intent(intent["intent_id"], "completed")

    store = checkpoint_manager._store_path(checkpoint_manager.CHECKPOINT_BASE)
    checkpoint_manager._expire_restore_intents(store, time.time() + 1)

    assert not (checkpoint_manager._read_checkpoint_meta(store, recovery_commit) or {}).get(
        "recovery_pins"
    )


def test_group_intent_pins_targets_before_creating_recovery_checkpoint(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "data.txt"
    _use_path_policy(monkeypatch, PathPolicy(mclaw_home=tmp_path / ".mclaw"))
    manager = _real_checkpoint_manager(monkeypatch, tmp_path)
    manager.max_snapshots = 2

    commits = []
    for value in ("zero", "one"):
        target.write_text(value, encoding="utf-8")
        assert manager.create_checkpoint(str(workspace), value)
        commits.append(manager.last_attempt["commit"])
    target.write_text("two", encoding="utf-8")

    intent = manager.begin_restore_intent(
        str(workspace),
        target_commits=commits,
        target_paths=[str(target)],
        kind="rollback-turn",
    )

    assert intent["success"] is True
    assert all(manager.has_checkpoint(str(workspace), commit) for commit in commits)
    assert manager.has_checkpoint(str(workspace), intent["recovery_commit"])


def test_single_operation_turn_compensation_does_not_slice_missing_group_commit(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "data.txt"
    target.write_text("after", encoding="utf-8")
    operation = {
        "operation_id": "op-1",
        "turn_id": "turn-1",
        "workspace": str(workspace),
        "before_commit": "b" * 40,
        "targets": [{
            "path": str(target),
            "before": {"exists": True},
            "after": inspect_path(str(target)),
        }],
    }
    journal = SimpleNamespace(
        get_operation=lambda *_args, **_kwargs: operation,
        list_operations=lambda **_kwargs: [operation],
    )
    manager = SimpleNamespace(
        restore=lambda *_args, **_kwargs: {"success": True},
    )
    coordinator = RollbackCoordinator(checkpoint_manager=manager, journal=journal)
    coordinator.rollback_operation = lambda *_args, **_kwargs: {
        "success": False,
        "error": "injected rollback failure",
        "pre_rollback_commit": "a" * 40,
    }

    result = coordinator.rollback_turn("turn-1", workspace=str(workspace))

    assert result["success"] is False
    assert "checkpoint aaaaaaaa" in result["error"]
