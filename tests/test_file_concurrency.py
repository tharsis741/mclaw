# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from mclaw.runtime.manager import RuntimeManager
from mclaw.runtime.paths import PathPolicy
from mclaw.tools import file_concurrency, file_operations, file_tools
from mclaw.tools.dispatch import reset_tool_context, set_tool_context
from mclaw.tools.read_tracker import clear_read_tracker


@pytest.fixture(autouse=True)
def isolated_file_state(monkeypatch, tmp_path):
    home = tmp_path / "state"
    monkeypatch.setenv("MCLAW_HOME", str(home))
    monkeypatch.setattr(RuntimeManager, "_current", SimpleNamespace(paths=PathPolicy(mclaw_home=home)))
    file_concurrency.clear_file_versions()
    clear_read_tracker()
    yield
    file_concurrency.clear_file_versions()
    clear_read_tracker()


def _agent(tmp_path, session):
    return SimpleNamespace(workspace_path=str(tmp_path), session_id=session)


def _read(agent, path="shared.txt", **args):
    return json.loads(file_tools._handle_read_file({"path": path, **args}, parent_agent=agent))


def _write(agent, content, path="shared.txt"):
    return json.loads(file_tools._handle_write_file({"path": path, "content": content}, parent_agent=agent))


@pytest.mark.parametrize("mutation", ["write", "patch", "edit", "delete"])
def test_stale_session_mutations_require_reread(tmp_path, mutation):
    target = tmp_path / "shared.txt"
    target.write_text("old value", encoding="utf-8")
    first, second = _agent(tmp_path, "first"), _agent(tmp_path, "second")
    assert _read(first)["content"] == "old value"
    assert _read(second)["content"] == "old value"
    assert "error" not in _write(first, "old value with first session changes")

    if mutation == "write":
        result = _write(second, "stale replacement")
    elif mutation == "patch":
        result = json.loads(file_tools._handle_patch(
            {"path": "shared.txt", "old_str": "old", "new_str": "new"}, parent_agent=second,
        ))
    elif mutation == "edit":
        result = json.loads(file_tools._handle_edit_file(
            {"path": "shared.txt", "old_block": "old value", "new_block": "new value"}, parent_agent=second,
        ))
    else:
        result = json.loads(file_tools._handle_delete_file({"path": "shared.txt"}, parent_agent=second))

    assert result["code"] == "FILE_VERSION_CONFLICT"
    assert result["requires_reread"] is True
    assert "read_file" in result["error"]
    assert target.read_text(encoding="utf-8") == "old value with first session changes"
    assert _read(second)["content"] == "old value with first session changes"
    assert "error" not in _write(second, "reviewed replacement")


def test_own_successful_write_advances_session_version(tmp_path):
    agent = _agent(tmp_path, "session")
    assert "error" not in _write(agent, "one")
    assert "error" not in _write(agent, "two")
    assert _read(agent)["content"] == "two"


def test_missing_read_cannot_overwrite_another_session_creation(tmp_path):
    first, second = _agent(tmp_path, "first"), _agent(tmp_path, "second")
    assert "error" in _read(first)
    assert "error" not in _write(second, "created elsewhere")
    assert _write(first, "stale creation")["requires_reread"] is True
    assert (tmp_path / "shared.txt").read_text(encoding="utf-8") == "created elsewhere"


def test_external_same_size_and_mtime_change_invalidates_read_dedup(tmp_path):
    target = tmp_path / "shared.txt"
    target.write_text("first", encoding="utf-8")
    agent = _agent(tmp_path, "session")
    assert _read(agent)["content"] == "first"
    assert _read(agent)["dedup"] is True
    old_stat = target.stat()
    target.write_text("other", encoding="utf-8")
    os.utime(target, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))

    assert _write(agent, "stale")["requires_reread"] is True
    assert _read(agent)["content"] == "other"
    assert "error" not in _write(agent, "reviewed")


def test_rejected_oversized_read_does_not_refresh_stale_version(tmp_path):
    target = tmp_path / "shared.txt"
    target.write_text("first", encoding="utf-8")
    agent = _agent(tmp_path, "session")
    agent.config = {"file_read_max_chars": 10}
    _read(agent)
    target.write_text("changed and exceeds the configured limit", encoding="utf-8")
    assert "error" in _read(agent)
    assert _write(agent, "stale")["requires_reread"] is True
    assert _read(agent, limit=5)["content"] == "chang"
    assert "error" not in _write(agent, "reviewed")


@pytest.mark.parametrize("alias", ["relative", "symlink", "case"])
def test_path_aliases_share_observed_version(tmp_path, alias):
    target = tmp_path / "shared.txt"
    target.write_text("first", encoding="utf-8")
    if alias == "symlink":
        alternate = tmp_path / "linked.txt"
        try:
            alternate.symlink_to(target)
        except OSError:
            pytest.skip("Host does not permit symlink creation")
    elif alias == "case":
        if os.name != "nt":
            pytest.skip("Case-folded paths are a Windows behavior")
        alternate = tmp_path / "SHARED.TXT"
    else:
        alternate = tmp_path / "." / "shared.txt"
    agent = _agent(tmp_path, "session")
    assert _read(agent, str(alternate))["content"] == "first"
    target.write_text("external", encoding="utf-8")
    assert _write(agent, "stale", str(target))["requires_reread"] is True


def test_two_readers_racing_writes_commit_exactly_once(tmp_path):
    target = tmp_path / "shared.txt"
    target.write_text("initial", encoding="utf-8")
    barrier = threading.Barrier(2)

    def contender(session):
        agent = _agent(tmp_path, session)
        assert _read(agent)["content"] == "initial"
        barrier.wait(timeout=5)
        return _write(agent, session)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(contender, ["first", "second"]))
    assert sum("error" not in result for result in results) == 1
    assert sum(result.get("requires_reread", False) for result in results) == 1
    assert target.read_text(encoding="utf-8") in {"first", "second"}


def test_dispatch_context_reaches_low_level_operations(tmp_path):
    target = tmp_path / "shared.txt"
    target.write_text("initial", encoding="utf-8")
    tokens = set_tool_context(session_id="from-dispatch")
    try:
        assert file_operations.read_file(str(target)) == "initial"
        target.write_text("outside", encoding="utf-8")
        with pytest.raises(file_concurrency.FileVersionConflict):
            file_operations.write_file(str(target), "stale")
    finally:
        reset_tool_context(tokens)


def test_external_change_before_atomic_replace_preserves_both_old_and_new_data(monkeypatch, tmp_path):
    target = tmp_path / "shared.txt"
    target.write_text("initial", encoding="utf-8")
    real_copymode = file_operations.shutil.copymode

    def external_change(source, destination):
        real_copymode(source, destination)
        target.write_text("external", encoding="utf-8")

    monkeypatch.setattr(file_operations.shutil, "copymode", external_change)
    result = _write(_agent(tmp_path, "session"), "would overwrite")
    assert result["requires_reread"] is True
    assert target.read_text(encoding="utf-8") == "external"
    assert sorted(item.name for item in tmp_path.iterdir()) == ["shared.txt", "state"]


def test_lock_wait_cancellation_does_not_mutate_and_lock_is_reusable(tmp_path):
    target = tmp_path / "shared.txt"
    target.write_text("initial", encoding="utf-8")
    cancelled = threading.Event()
    started = threading.Event()

    def waiting_write():
        started.set()
        with pytest.raises(InterruptedError):
            file_operations.write_file(str(target), "cancelled", cancel_event=cancelled)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with file_concurrency.locked_target(target):
            future = pool.submit(waiting_write)
            assert started.wait(3)
            cancelled.set()
            future.result(timeout=3)
        file_operations.write_file(str(target), "after release")
    assert target.read_text(encoding="utf-8") == "after release"


def test_other_process_lock_blocks_and_cancels_local_writer(tmp_path):
    target = tmp_path / "shared.txt"
    target.write_text("initial", encoding="utf-8")
    script = (
        "import sys; from pathlib import Path; "
        "from mclaw.tools.file_concurrency import locked_target; "
        "lock = locked_target(Path(sys.argv[1])); lock.__enter__(); "
        "print('locked', flush=True); sys.stdin.readline(); lock.__exit__(None, None, None)"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(target)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(process.stdout.readline).result(timeout=10).strip() == "locked"
        cancelled = threading.Event()
        timer = threading.Timer(0.15, cancelled.set)
        timer.start()
        try:
            with pytest.raises(InterruptedError):
                file_operations.write_file(str(target), "blocked", cancel_event=cancelled)
        finally:
            timer.cancel()
        assert target.read_text(encoding="utf-8") == "initial"
        process.communicate("release\n", timeout=10)
        assert process.returncode == 0
        file_operations.write_file(str(target), "after process release")
        assert target.read_text(encoding="utf-8") == "after process release"
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)


def test_existing_path_policy_is_checked_before_version_reads(tmp_path):
    target = tmp_path / ".env"
    target.write_text("sensitive", encoding="utf-8")
    agent = _agent(tmp_path, "session")
    assert "PathPolicy denied" in _read(agent, str(target))["error"]
    assert "PathPolicy denied" in _write(agent, "replacement", str(target))["error"]
    assert target.read_text(encoding="utf-8") == "sensitive"
