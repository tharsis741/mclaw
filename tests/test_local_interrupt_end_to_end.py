import asyncio
import json
import os
import shlex
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from mclaw.agent import auxiliary_client
from mclaw.runtime import process as runtime_process
from mclaw.runtime.process import run_captured_process
from mclaw.runtime.search import SearchProfile
from mclaw.safety import operation_journal
from mclaw.tools import checkpoint_manager, dispatch, file_operations, file_tools
from mclaw.tools.interrupt import reset_interrupt_event, set_interrupt_event


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())


def _pid_exists(pid: int) -> bool:
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))) and exit_code.value == 259
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, OSError):
        return False
    return True


def test_auxiliary_transport_receives_turn_cancel_and_drains(monkeypatch) -> None:
    cancel_event = threading.Event()
    entered = threading.Event()
    drained = threading.Event()
    result: dict[str, object] = {}

    async def blocking_call(*_args, **_kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            drained.set()

    monkeypatch.setattr(
        auxiliary_client,
        "_resolve_auxiliary_runtime",
        lambda *_args, **_kwargs: (object(), 60),
    )
    monkeypatch.setattr(
        auxiliary_client,
        "_call_auxiliary_model",
        blocking_call,
    )

    def invoke() -> None:
        token = set_interrupt_event(cancel_event)
        try:
            auxiliary_client.call_auxiliary_llm("session_search", [])
        except BaseException as exc:
            result["exception"] = exc
        finally:
            reset_interrupt_event(token)

    worker = threading.Thread(target=invoke)
    worker.start()
    assert entered.wait(1)
    cancel_event.set()
    worker.join(1)

    assert not worker.is_alive()
    assert isinstance(result.get("exception"), InterruptedError)
    assert drained.wait(1)


def test_cancel_kills_and_drains_real_process_tree(tmp_path: Path) -> None:
    cancel_event = threading.Event()
    pid_file = tmp_path / "captured-process.pids"
    script = (
        "import os,subprocess,sys,time;"
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
        f"open({str(pid_file)!r},'w').write(f'{{os.getpid()}},{{child.pid}}');"
        "time.sleep(60)"
    )
    result: dict[str, object] = {}

    def invoke() -> None:
        try:
            run_captured_process(
                [sys.executable, "-c", script],
                timeout=60,
                env=dict(os.environ),
                cwd=str(tmp_path),
                cancel_event=cancel_event,
            )
        except BaseException as exc:
            result["exception"] = exc

    worker = threading.Thread(target=invoke)
    worker.start()
    assert _wait_until(pid_file.exists)
    pids = [int(value) for value in pid_file.read_text(encoding="utf-8").split(",")]
    cancel_event.set()
    worker.join(8)

    assert not worker.is_alive()
    assert isinstance(result.get("exception"), InterruptedError)
    assert _wait_until(lambda: all(not _pid_exists(pid) for pid in pids))


def test_captured_process_uses_native_session_creation(monkeypatch) -> None:
    seen: dict[str, object] = {}

    class FinishedProcess:
        pid = 12345
        returncode = 0

        def poll(self):
            return 0

        def communicate(self, timeout=None):
            return "ok", ""

    def fake_popen(argv, **kwargs):
        seen.update(kwargs)
        return FinishedProcess()

    monkeypatch.setattr(runtime_process.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(runtime_process, "get_process_group_id", lambda _pid: None)

    result = run_captured_process(["test-command"], timeout=1)

    assert result.stdout == "ok"
    assert "preexec_fn" not in seen
    assert seen["start_new_session"] is (os.name != "nt")


def test_unconfirmed_captured_process_keeps_fence_through_checkpoint_and_dispatch(
    monkeypatch,
    tmp_path: Path,
) -> None:
    cancel_event = threading.Event()

    class Pipe:
        def close(self):
            raise AssertionError("cancellation must not synchronously close a busy pipe")

    class StuckProcess:
        pid = 12346
        returncode = None
        stdin = Pipe()
        stdout = Pipe()
        stderr = Pipe()

        def poll(self):
            cancel_event.set()
            return None

        def communicate(self, *_args, **_kwargs):
            raise subprocess.TimeoutExpired("stuck", 0.01)

        def kill(self):
            return None

        def wait(self, *_args, **_kwargs):
            raise subprocess.TimeoutExpired("stuck", 0.01)

    monkeypatch.setattr(runtime_process.subprocess, "Popen", lambda *_a, **_k: StuckProcess())
    monkeypatch.setattr(runtime_process, "get_process_group_id", lambda _pid: None)
    monkeypatch.setattr(runtime_process, "kill_process_tree", lambda _pid: False)

    with pytest.raises(RuntimeError, match="could not be confirmed stopped") as exc_info:
        run_captured_process(["stuck"], timeout=1, cancel_event=cancel_event)

    error = exc_info.value
    assert error.pid == StuckProcess.pid
    assert error.process_group_id is None
    assert error.termination_fence.persistent is True

    monkeypatch.setattr(
        checkpoint_manager,
        "run_captured_process",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(error),
    )
    with pytest.raises(RuntimeError) as checkpoint_error:
        checkpoint_manager._run_git(["status"], tmp_path, str(tmp_path))
    assert checkpoint_error.value is error

    manager = checkpoint_manager.CheckpointManager(enabled=True)
    manager._git_available = True
    monkeypatch.setattr(
        manager,
        "_take",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(error),
    )
    with pytest.raises(RuntimeError) as manager_error:
        manager.ensure_checkpoint(str(tmp_path))
    assert manager_error.value is error

    monkeypatch.setattr(
        dispatch,
        "_build_file_safety_plan",
        lambda *_args, **_kwargs: SimpleNamespace(
            mutates=True,
            workspace=str(tmp_path),
            target_paths=[],
            intent=SimpleNamespace(raw_command=""),
            decision=SimpleNamespace(level="normal", allowed=True),
        ),
    )
    with pytest.raises(RuntimeError) as preflight_error:
        dispatch._maybe_checkpoint_before_tool("write_file", {}, manager)
    assert preflight_error.value is error

    class Parent:
        def __init__(self):
            self.workers = set()

        def _register_turn_worker(self, worker):
            self.workers.add(worker)

        def _unregister_turn_worker(self, worker):
            self.workers.discard(worker)

    parent = Parent()
    result_slot = {"result": None, "finished_at": None}
    cancel_event.clear()
    from mclaw.tools.registry import ToolRegistry

    tool_registry = ToolRegistry()
    tool_registry.register(
        "fenced_test",
        "test",
        {"function": {"name": "fenced_test"}},
        lambda *_args, **_kwargs: (_ for _ in ()).throw(error),
    )
    monkeypatch.setattr(dispatch, "registry", tool_registry)
    dispatch._run_tool_worker(
        result_slot,
        {"function": {"name": "fenced_test", "arguments": "{}"}},
        {"fenced_test"},
        None,
        None,
        parent,
        cancel_event,
    )

    assert json.loads(result_slot["result"])["completion_unknown"] is True
    assert error.termination_fence in parent.workers


def test_terminal_parent_with_inherited_pipe_honors_total_cancel_budget(
    monkeypatch,
) -> None:
    cancel_event = threading.Event()
    communicate_timeouts: list[float] = []

    class Pipe:
        def close(self):
            return None

    class TerminalParent:
        pid = 12347
        returncode = 0
        stdin = Pipe()
        stdout = Pipe()
        stderr = Pipe()

        def poll(self):
            return self.returncode

        def communicate(self, *_args, timeout=None, **_kwargs):
            communicate_timeouts.append(timeout)
            cancel_event.set()
            time.sleep(timeout)
            raise subprocess.TimeoutExpired("inherited-pipe", timeout)

        def kill(self):
            return None

        def wait(self, timeout=None):
            time.sleep(timeout)
            raise subprocess.TimeoutExpired("inherited-pipe", timeout)

    monkeypatch.setattr(runtime_process.subprocess, "Popen", lambda *_a, **_k: TerminalParent())
    monkeypatch.setattr(runtime_process, "get_process_group_id", lambda _pid: None)

    def slow_unconfirmed_kill(_pid):
        time.sleep(0.9)
        return False

    monkeypatch.setattr(runtime_process, "kill_process_tree", slow_unconfirmed_kill)

    started = time.monotonic()
    with pytest.raises(RuntimeError) as exc_info:
        run_captured_process(["inherited-pipe"], timeout=60, cancel_event=cancel_event)
    elapsed = time.monotonic() - started

    assert elapsed < 2.0
    assert exc_info.value.termination_fence.persistent is True
    assert communicate_timeouts[0] <= 0.05
    assert all(timeout <= 0.5 for timeout in communicate_timeouts)


def test_captured_process_rechecks_completion_before_committing_cancel(
    monkeypatch,
) -> None:
    process_box = {}

    class CancelAtCommit:
        def __init__(self) -> None:
            self.checks = 0

        def is_set(self) -> bool:
            self.checks += 1
            if self.checks >= 2:
                process_box["proc"].returncode = 0
                return True
            return False

    class CompletedAtCommit:
        pid = 12348
        returncode = None

        def __init__(self) -> None:
            self.polls = 0

        def poll(self):
            self.polls += 1
            return None if self.polls == 1 else self.returncode

        def communicate(self, *_args, **_kwargs):
            return "committed-result", ""

    process = CompletedAtCommit()
    process_box["proc"] = process
    cancel_event = CancelAtCommit()
    monkeypatch.setattr(runtime_process.subprocess, "Popen", lambda *_a, **_k: process)
    monkeypatch.setattr(runtime_process, "get_process_group_id", lambda _pid: None)
    monkeypatch.setattr(
        runtime_process,
        "kill_process_tree",
        lambda _pid: (_ for _ in ()).throw(AssertionError("completed process was killed")),
    )

    result = run_captured_process(
        ["completed-at-commit"],
        timeout=10,
        cancel_event=cancel_event,
    )

    assert result.returncode == 0
    assert result.stdout == "committed-result"
    assert process.polls == 2


def test_captured_process_preserves_success_that_wins_signal_delivery(
    monkeypatch,
) -> None:
    class CancelAfterSpawn:
        def __init__(self) -> None:
            self.checks = 0

        def is_set(self) -> bool:
            self.checks += 1
            return self.checks >= 2

    class CompletesBeforeKill:
        pid = 12349
        returncode = None
        stdin = None
        stdout = None
        stderr = None

        def __init__(self) -> None:
            self.polls = 0

        def poll(self):
            self.polls += 1
            return None if self.polls <= 2 else self.returncode

        def communicate(self, *_args, **_kwargs):
            return "effect-committed", ""

    process = CompletesBeforeKill()
    cancel_event = CancelAfterSpawn()
    monkeypatch.setattr(runtime_process.subprocess, "Popen", lambda *_a, **_k: process)
    monkeypatch.setattr(runtime_process, "get_process_group_id", lambda _pid: None)

    def completion_wins_kill(_pid):
        process.returncode = 0
        return True

    monkeypatch.setattr(runtime_process, "kill_process_tree", completion_wins_kill)

    result = run_captured_process(
        ["completion-wins-kill"],
        timeout=10,
        cancel_event=cancel_event,
    )

    assert result.returncode == 0
    assert result.stdout == "effect-committed"
    assert process.polls == 3


def test_search_and_checkpoint_pass_the_bound_event_to_process_runner(
    monkeypatch,
    tmp_path: Path,
) -> None:
    cancel_event = threading.Event()
    seen = []

    def cancelled_runner(*_args, **kwargs):
        seen.append(kwargs.get("cancel_event"))
        cancel_event.set()
        raise InterruptedError("cancelled")

    monkeypatch.setattr("mclaw.runtime.search.run_captured_process", cancelled_runner)
    with pytest.raises(InterruptedError):
        SearchProfile._rg(
            SimpleNamespace(),
            str(tmp_path),
            "needle",
            None,
            1,
            cancel_event=cancel_event,
        )

    cancel_event.clear()
    monkeypatch.setattr(checkpoint_manager, "run_captured_process", cancelled_runner)
    with pytest.raises(InterruptedError):
        token = set_interrupt_event(cancel_event)
        try:
            checkpoint_manager._run_git(["status"], tmp_path, str(tmp_path))
        finally:
            reset_interrupt_event(token)

    assert seen == [cancel_event, cancel_event]


def test_operation_journal_hash_observes_cancel_between_real_file_chunks(
    monkeypatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "large.bin"
    target.write_bytes(b"x" * (3 * 1024 * 1024))
    cancel_event = threading.Event()
    real_open = Path.open

    class InterruptingReader:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            self.handle.__enter__()
            return self

        def __exit__(self, *args):
            return self.handle.__exit__(*args)

        def read(self, size=-1):
            data = self.handle.read(size)
            if data:
                cancel_event.set()
            return data

    def interrupting_open(path, *args, **kwargs):
        handle = real_open(path, *args, **kwargs)
        return InterruptingReader(handle) if Path(path) == target else handle

    monkeypatch.setattr(Path, "open", interrupting_open)

    with pytest.raises(InterruptedError):
        operation_journal._sha256_file(target, cancel_event=cancel_event)


def test_cancel_before_atomic_replace_keeps_original_and_removes_temp(
    monkeypatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "data.txt"
    target.write_text("old", encoding="utf-8")
    cancel_event = threading.Event()
    before = set(tmp_path.iterdir())
    real_named_temporary_file = file_operations.tempfile.NamedTemporaryFile

    class CancelAfterWrite:
        def __init__(self, wrapped):
            self.wrapped = wrapped
            self.handle = None

        def __enter__(self):
            self.handle = self.wrapped.__enter__()
            return self

        def __exit__(self, *args):
            try:
                return self.wrapped.__exit__(*args)
            finally:
                cancel_event.set()

        @property
        def name(self):
            return self.handle.name

        def write(self, content):
            return self.handle.write(content)

    monkeypatch.setattr(
        file_operations.tempfile,
        "NamedTemporaryFile",
        lambda **kwargs: CancelAfterWrite(real_named_temporary_file(**kwargs)),
    )

    with pytest.raises(InterruptedError):
        file_operations.write_file(
            str(target),
            "new",
            cancel_event=cancel_event,
        )

    assert target.read_text(encoding="utf-8") == "old"
    assert set(tmp_path.iterdir()) == before


def test_cancel_during_chunked_atomic_write_keeps_original_and_removes_temp(
    monkeypatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "data.txt"
    target.write_text("old", encoding="utf-8")
    before = set(tmp_path.iterdir())
    cancel_event = threading.Event()
    real_named_temporary_file = file_operations.tempfile.NamedTemporaryFile

    class CancelAfterFirstChunk:
        def __init__(self, wrapped):
            self.wrapped = wrapped
            self.handle = None

        def __enter__(self):
            self.handle = self.wrapped.__enter__()
            return self

        def __exit__(self, *args):
            return self.wrapped.__exit__(*args)

        @property
        def name(self):
            return self.handle.name

        def write(self, content):
            written = self.handle.write(content)
            cancel_event.set()
            return written

    monkeypatch.setattr(
        file_operations.tempfile,
        "NamedTemporaryFile",
        lambda **kwargs: CancelAfterFirstChunk(real_named_temporary_file(**kwargs)),
    )

    with pytest.raises(InterruptedError):
        file_operations.write_file(
            str(target),
            "x" * (file_operations._TEXT_IO_CHUNK_CHARS + 1),
            cancel_event=cancel_event,
        )

    assert target.read_text(encoding="utf-8") == "old"
    assert set(tmp_path.iterdir()) == before


@pytest.mark.parametrize("operation", ["patch", "edit"])
def test_cancel_during_chunked_file_read_has_no_side_effect(
    operation: str,
    monkeypatch,
    tmp_path: Path,
) -> None:
    original = "x" * (file_operations._TEXT_IO_CHUNK_CHARS + 1) + " old value"
    target = tmp_path / "data.txt"
    target.write_text(original, encoding="utf-8")
    cancel_event = threading.Event()
    real_open = Path.open

    class CancelAfterFirstChunk:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            self.handle.__enter__()
            return self

        def __exit__(self, *args):
            return self.handle.__exit__(*args)

        def read(self, size=-1):
            content = self.handle.read(size)
            if content:
                cancel_event.set()
            return content

    def interrupting_open(path, *args, **kwargs):
        handle = real_open(path, *args, **kwargs)
        return CancelAfterFirstChunk(handle) if Path(path) == target else handle

    monkeypatch.setattr(Path, "open", interrupting_open)
    call = file_operations.patch_file if operation == "patch" else file_operations.edit_file

    with pytest.raises(InterruptedError):
        call(str(target), "old", "new", cancel_event=cancel_event)

    with real_open(target, "r", encoding="utf-8") as handle:
        assert handle.read() == original


@pytest.mark.parametrize("operation", ["write", "patch", "edit"])
def test_atomic_file_replacement_preserves_mode(operation: str, tmp_path: Path) -> None:
    target = tmp_path / "script.py"
    target.write_text("old value", encoding="utf-8")
    if os.name != "nt":
        target.chmod(0o755)
    before_mode = stat.S_IMODE(target.stat().st_mode)

    if operation == "write":
        file_operations.write_file(str(target), "new value")
    elif operation == "patch":
        file_operations.patch_file(str(target), "old", "new")
    else:
        file_operations.edit_file(str(target), "old value", "new value")

    assert stat.S_IMODE(target.stat().st_mode) == before_mode
    if os.name != "nt":
        assert before_mode == 0o755


@pytest.mark.parametrize("operation", ["patch", "edit", "delete"])
def test_cancel_at_final_file_commit_barrier_has_no_side_effect(
    operation: str,
    monkeypatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "data.txt"
    target.write_text("old value", encoding="utf-8")
    cancel_event = threading.Event()

    if operation in {"patch", "edit"}:
        real_read_text = file_operations._read_text_cancellable

        def read_then_cancel(path, cancel):
            content = real_read_text(path, cancel)
            if path == target:
                cancel_event.set()
            return content

        monkeypatch.setattr(file_operations, "_read_text_cancellable", read_then_cancel)
        call = file_operations.patch_file if operation == "patch" else file_operations.edit_file
        with pytest.raises(InterruptedError):
            call(str(target), "old", "new", cancel_event=cancel_event)
    else:
        real_is_dir = Path.is_dir

        def inspect_then_cancel(path):
            result = real_is_dir(path)
            if Path(path) == target:
                cancel_event.set()
            return result

        monkeypatch.setattr(Path, "is_dir", inspect_then_cancel)
        with pytest.raises(InterruptedError):
            file_operations.delete_file(str(target), cancel_event=cancel_event)

    assert target.read_text(encoding="utf-8") == "old value"


def test_checkpoint_manager_normal_git_snapshot_still_succeeds(
    monkeypatch,
    tmp_path: Path,
) -> None:
    if not checkpoint_manager.shutil.which("git"):
        pytest.skip("git is unavailable")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "data.txt"
    target.write_text("before", encoding="utf-8")
    monkeypatch.setattr(checkpoint_manager, "CHECKPOINT_BASE", tmp_path / "checkpoints")
    manager = checkpoint_manager.CheckpointManager(enabled=True)

    assert manager.ensure_checkpoint(
        str(workspace),
        "interrupt regression test",
        target_paths=[str(target)],
    ) is True
    assert manager.last_attempt["status"] == "taken"
    assert manager.list_checkpoints(str(workspace))[0]["hash"] == manager.last_attempt["commit"]


def test_stopped_real_git_add_removes_index_lock_and_same_manager_retries(
    monkeypatch,
    tmp_path: Path,
) -> None:
    if not checkpoint_manager.shutil.which("git"):
        pytest.skip("git is unavailable")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "payload.txt"
    target.write_text("before", encoding="utf-8")
    monkeypatch.setattr(checkpoint_manager, "CHECKPOINT_BASE", tmp_path / "checkpoints")
    manager = checkpoint_manager.CheckpointManager(enabled=True)
    assert manager.ensure_checkpoint(
        str(workspace),
        "initial",
        target_paths=[str(target)],
    ) is True

    store = checkpoint_manager._store_path(checkpoint_manager.CHECKPOINT_BASE)
    started = tmp_path / "filter.started"
    filter_script = workspace / "slow_filter.py"
    filter_script.write_text(
        "import pathlib, sys, time\n"
        "pathlib.Path(sys.argv[1]).write_text('started', encoding='utf-8')\n"
        "time.sleep(60)\n"
        "sys.stdout.buffer.write(sys.stdin.buffer.read())\n",
        encoding="utf-8",
    )
    (workspace / ".gitattributes").write_text(
        "payload.txt filter=mclaw_interrupt_test\n",
        encoding="utf-8",
    )
    filter_command = shlex.join(
        [Path(sys.executable).as_posix(), filter_script.as_posix(), started.as_posix()]
    )
    configured, _, config_error = checkpoint_manager._run_git(
        ["config", "filter.mclaw_interrupt_test.clean", filter_command],
        store,
        str(workspace),
    )
    assert configured, config_error

    target.write_text("after cancellation", encoding="utf-8")
    manager.new_turn()
    cancel_event = threading.Event()
    outcome: dict[str, object] = {}

    def checkpoint() -> None:
        token = set_interrupt_event(cancel_event)
        try:
            outcome["value"] = manager.ensure_checkpoint(
                str(workspace),
                "cancel real git add",
                target_paths=[str(target)],
            )
        except BaseException as exc:
            outcome["exception"] = exc
        finally:
            reset_interrupt_event(token)

    worker = threading.Thread(target=checkpoint)
    worker.start()
    assert _wait_until(started.exists, timeout=10)
    cancel_event.set()
    worker.join(15)

    assert not worker.is_alive()
    assert isinstance(outcome.get("exception"), InterruptedError)
    index_file = checkpoint_manager._index_path(
        store,
        checkpoint_manager._project_hash(str(workspace)),
    )
    assert not Path(f"{index_file}.lock").exists()

    (workspace / ".gitattributes").unlink()
    filter_script.unlink()
    manager.new_turn()
    assert manager.ensure_checkpoint(
        str(workspace),
        "retry after cancelled git add",
        target_paths=[str(target)],
    ) is True
    assert manager.last_attempt["status"] == "taken"
    assert not Path(f"{index_file}.lock").exists()

    filter_script.write_text(
        "import pathlib, sys, time\n"
        "pathlib.Path(sys.argv[1]).write_text('started', encoding='utf-8')\n"
        "time.sleep(60)\n"
        "sys.stdout.buffer.write(sys.stdin.buffer.read())\n",
        encoding="utf-8",
    )
    (workspace / ".gitattributes").write_text(
        "payload.txt filter=mclaw_interrupt_test\n",
        encoding="utf-8",
    )
    target.write_text("after timeout", encoding="utf-8")
    manager.new_turn()
    monkeypatch.setattr(checkpoint_manager, "_GIT_TIMEOUT", 0.1)

    assert manager.ensure_checkpoint(
        str(workspace),
        "timeout real git add",
        target_paths=[str(target)],
    ) is False
    assert not Path(f"{index_file}.lock").exists()

    (workspace / ".gitattributes").unlink()
    filter_script.unlink()
    monkeypatch.setattr(checkpoint_manager, "_GIT_TIMEOUT", 2)
    manager.new_turn()
    assert manager.ensure_checkpoint(
        str(workspace),
        "retry after timed out git add",
        target_paths=[str(target)],
    ) is True
    assert manager.last_attempt["status"] == "taken"
    assert not Path(f"{index_file}.lock").exists()


def test_checkpoint_cancel_has_no_file_side_effect_and_next_turn_reuses_fence(
    monkeypatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "data.txt"
    target.write_text("old", encoding="utf-8")
    process_started = tmp_path / "checkpoint.started"
    cancel_event = threading.Event()

    plan = SimpleNamespace(
        mutates=True,
        workspace=str(tmp_path),
        target_paths=[str(target)],
        intent=SimpleNamespace(raw_command=""),
        decision=SimpleNamespace(allowed=True, level="normal"),
    )
    monkeypatch.setattr(dispatch, "_build_file_safety_plan", lambda *_args, **_kwargs: plan)

    class Parent:
        session_id = "checkpoint-cancel"
        config = {"file_safety": {"enabled": True, "journal_enabled": False}}

        def __init__(self):
            self.workers = set()

        def _register_turn_worker(self, worker):
            self.workers.add(worker)

        def _unregister_turn_worker(self, worker):
            self.workers.discard(worker)

        def _request_turn_abort(self, _reason, event):
            event.set()

    class Checkpoints:
        def __init__(self):
            self.calls = 0
            self.last_attempt = {}

        def ensure_checkpoint(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls != 1:
                return False
            script = (
                "import time;"
                f"open({str(process_started)!r},'w').write('started');"
                "time.sleep(60)"
            )
            run_captured_process(
                [sys.executable, "-c", script],
                timeout=60,
                env=dict(os.environ),
                cwd=str(tmp_path),
                cancel_event=cancel_event,
            )

    parent = Parent()
    checkpoints = Checkpoints()

    def cancel_after_checkpoint_starts() -> None:
        assert _wait_until(process_started.exists)
        cancel_event.set()

    canceller = threading.Thread(target=cancel_after_checkpoint_starts)
    canceller.start()
    [first] = dispatch.handle_function_calls(
        [{"id": "first", "function": {"name": "write_file", "arguments": json.dumps({"path": str(target), "content": "new"})}}],
        {"write_file"},
        checkpoint_manager=checkpoints,
        parent_agent=parent,
        cancel_event=cancel_event,
    )
    canceller.join(2)

    assert json.loads(first)["interrupted"] is True
    assert target.read_text(encoding="utf-8") == "old"
    assert _wait_until(lambda: not parent.workers)

    [second] = dispatch.handle_function_calls(
        [{"id": "second", "function": {"name": "write_file", "arguments": json.dumps({"path": str(target), "content": "new"})}}],
        {"write_file"},
        checkpoint_manager=checkpoints,
        parent_agent=parent,
        cancel_event=threading.Event(),
    )

    assert "error" not in json.loads(second)
    assert target.read_text(encoding="utf-8") == "new"
    assert not parent.workers
