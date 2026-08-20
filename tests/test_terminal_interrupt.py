# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from mclaw.runtime.base import ExecResult, Runtime
from mclaw.tools.interrupt import get_cancel_id


class _DirectRuntime(Runtime):
    kind = "test"

    def __init__(self, cwd: Path) -> None:
        self.paths = SimpleNamespace(
            check=lambda _action, _path: SimpleNamespace(
                allowed=True,
                resolved=cwd,
                error_message=lambda: "",
            )
        )
        self.shell = SimpleNamespace(
            name="direct-python",
            argv=lambda command, _cwd: [sys.executable, command],
        )

    def build_env(self, extra=None, *, allowed_sensitive=None):
        env = dict(os.environ)
        env.update(extra or {})
        return env


def test_runtime_exec_interrupt_kills_foreground_process(tmp_path: Path, caplog) -> None:
    caplog.set_level(logging.INFO)
    started = tmp_path / "started"
    completed = tmp_path / "completed"
    script = tmp_path / "slow.py"
    script.write_text(
        "from pathlib import Path\n"
        "import time\n"
        f"Path({str(started)!r}).write_text('started')\n"
        "print('started-output', flush=True)\n"
        "time.sleep(5)\n"
        f"Path({str(completed)!r}).write_text('completed')\n",
        encoding="utf-8",
    )
    cancel_event = threading.Event()

    def cancel_after_start() -> None:
        deadline = time.monotonic() + 2
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        cancel_event.set()

    canceller = threading.Thread(target=cancel_after_start)
    canceller.start()
    began = time.monotonic()
    result = _DirectRuntime(tmp_path).exec(
        str(script),
        cwd=tmp_path,
        timeout=10,
        cancel_event=cancel_event,
    )
    elapsed = time.monotonic() - began
    canceller.join(timeout=1)

    assert started.exists()
    assert result.returncode == 130
    assert "started-output" in result.output
    assert elapsed < 3
    time.sleep(0.2)
    assert not completed.exists()
    cancel_id = get_cancel_id(cancel_event)
    messages = [record.getMessage() for record in caplog.records]
    assert any("termination_start" in message and cancel_id in message for message in messages)
    assert any(
        "termination_result" in message
        and cancel_id in message
        and "termination_confirmed=True" in message
        for message in messages
    )


def test_foreground_cancel_kills_and_keeps_unknown_fence_when_logging_fails(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import mclaw.runtime.base as base_module

    cancel_event = threading.Event()
    killed: list[int] = []

    class RaisingHandler(logging.Handler):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def emit(self, _record) -> None:
            self.calls += 1
            raise RuntimeError("logging failed")

    class FakeProcess:
        pid = 43210

        def __init__(self) -> None:
            self.returncode = None
            self.communicate_calls = 0

        def communicate(self, _input=None, timeout=None):
            self.communicate_calls += 1
            if self.communicate_calls == 1:
                cancel_event.set()
                raise subprocess.TimeoutExpired("fake", timeout or 0.05)
            self.returncode = -9
            return "cancelled-output", None

        def poll(self):
            return self.returncode

    process = FakeProcess()
    monkeypatch.setattr(base_module.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(
        base_module,
        "kill_process_tree",
        lambda pid: (killed.append(pid), False)[1],
    )
    handler = RaisingHandler()
    old_level = base_module.logger.level
    base_module.logger.setLevel(logging.WARNING)
    base_module.logger.addHandler(handler)
    try:
        result = _DirectRuntime(tmp_path).exec(
            "unused.py",
            cwd=tmp_path,
            timeout=10,
            cancel_event=cancel_event,
        )
    finally:
        base_module.logger.removeHandler(handler)
        base_module.logger.setLevel(old_level)

    assert killed == [process.pid]
    assert result.returncode == 130
    assert result.termination_confirmed is False
    assert result.termination_fence is not None
    assert handler.calls >= 2


def test_runtime_prefers_completed_process_over_raced_cancel(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import mclaw.runtime.base as base_module

    cancel_event = threading.Event()
    popen_kwargs: dict[str, object] = {}

    class CompletedProcess:
        pid = 43211
        returncode = None

        def __init__(self) -> None:
            self.communicate_calls = 0

        def poll(self):
            return self.returncode

        def communicate(self, _input=None, timeout=None):
            self.communicate_calls += 1
            if self.communicate_calls == 1:
                self.returncode = 0
                cancel_event.set()
                raise subprocess.TimeoutExpired("completed", timeout or 0.05)
            return "effect-committed", None

    process = CompletedProcess()

    def popen(*_args, **kwargs):
        popen_kwargs.update(kwargs)
        return process

    monkeypatch.setattr(base_module.subprocess, "Popen", popen)
    monkeypatch.setattr(base_module, "get_process_group_id", lambda _pid: None)
    monkeypatch.setattr(
        base_module,
        "kill_process_tree",
        lambda _pid: (_ for _ in ()).throw(AssertionError("completed process was killed")),
    )

    result = _DirectRuntime(tmp_path).exec(
        "ignored.py",
        cwd=tmp_path,
        timeout=10,
        cancel_event=cancel_event,
    )

    assert result.returncode == 0
    assert result.output == "effect-committed"
    assert result.termination_confirmed is True
    assert "preexec_fn" not in popen_kwargs
    assert popen_kwargs["start_new_session"] is (os.name != "nt")


def test_runtime_rechecks_completion_before_committing_cancel(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import mclaw.runtime.base as base_module

    cancel_event = threading.Event()

    class CompletedAfterFirstPoll:
        pid = 43212
        returncode = None

        def __init__(self):
            self.poll_calls = 0

        def poll(self):
            self.poll_calls += 1
            if self.poll_calls == 1:
                self.returncode = 0
                cancel_event.set()
                return None
            return self.returncode

        def communicate(self, *_args, **_kwargs):
            return "committed-between-poll-and-cancel", None

    process = CompletedAfterFirstPoll()
    monkeypatch.setattr(base_module.subprocess, "Popen", lambda *_a, **_k: process)
    monkeypatch.setattr(base_module, "get_process_group_id", lambda _pid: None)
    monkeypatch.setattr(
        base_module,
        "kill_process_tree",
        lambda _pid: (_ for _ in ()).throw(AssertionError("completed process was killed")),
    )

    result = _DirectRuntime(tmp_path).exec(
        "ignored.py",
        cwd=tmp_path,
        timeout=10,
        cancel_event=cancel_event,
    )

    assert result.returncode == 0
    assert result.output == "committed-between-poll-and-cancel"
    assert result.termination_confirmed is True


def test_runtime_terminal_parent_never_blocks_on_inherited_pipe(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import mclaw.runtime.base as base_module

    cancel_event = threading.Event()

    class ParentExitedWithOpenPipe:
        pid = 43212
        returncode = 0

        def __init__(self) -> None:
            self.timeouts = []

        def poll(self):
            return self.returncode

        def communicate(self, _input=None, timeout=None):
            self.timeouts.append(timeout)
            if timeout == 0.05:
                raise subprocess.TimeoutExpired("open-pipe", timeout)
            return "drained-after-tree-stop", None

    process = ParentExitedWithOpenPipe()

    def popen(*_args, **_kwargs):
        cancel_event.set()
        return process

    monkeypatch.setattr(base_module.subprocess, "Popen", popen)
    monkeypatch.setattr(base_module, "get_process_group_id", lambda _pid: 43212)
    monkeypatch.setattr(base_module, "kill_process_group", lambda _pgid: True)
    monkeypatch.setattr(
        base_module,
        "wait_for_process_group_exit",
        lambda _pgid, timeout: True,
    )

    result = _DirectRuntime(tmp_path).exec(
        "ignored.py",
        cwd=tmp_path,
        timeout=10,
        cancel_event=cancel_event,
    )

    assert process.timeouts == [0.05, 0.05, 0.5]
    assert result.returncode == 0
    assert result.output == "drained-after-tree-stop"
    assert result.termination_confirmed is True
    assert result.termination_fence is None


def test_runtime_preserves_success_that_wins_signal_delivery(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import mclaw.runtime.base as base_module

    cancel_event = threading.Event()

    class CompletesBeforeKill:
        pid = 43214
        returncode = None
        stdin = None
        stdout = None

        def __init__(self) -> None:
            self.polls = 0

        def poll(self):
            self.polls += 1
            return None if self.polls <= 2 else self.returncode

        def communicate(self, *_args, **_kwargs):
            return "effect-committed", None

    process = CompletesBeforeKill()

    def popen(*_args, **_kwargs):
        cancel_event.set()
        return process

    def completion_wins_kill(_pid):
        process.returncode = 0
        return True

    monkeypatch.setattr(base_module.subprocess, "Popen", popen)
    monkeypatch.setattr(base_module, "get_process_group_id", lambda _pid: None)
    monkeypatch.setattr(base_module, "kill_process_tree", completion_wins_kill)

    result = _DirectRuntime(tmp_path).exec(
        "ignored.py",
        cwd=tmp_path,
        timeout=10,
        cancel_event=cancel_event,
    )

    assert result.returncode == 0
    assert result.output == "effect-committed"
    assert result.termination_confirmed is True
    assert result.termination_fence is None
    assert process.polls == 3


def test_runtime_cancel_cleanup_has_one_wall_clock_budget(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import mclaw.runtime.base as base_module

    cancel_event = threading.Event()
    communicate_timeouts: list[float] = []

    class Pipe:
        def close(self):
            raise AssertionError("cancellation must not synchronously close a busy pipe")

    class StuckProcess:
        pid = 43213
        returncode = None
        stdin = Pipe()
        stdout = Pipe()

        def poll(self):
            return None

        def communicate(self, *_args, timeout=None, **_kwargs):
            communicate_timeouts.append(timeout)
            time.sleep(timeout)
            raise subprocess.TimeoutExpired("stuck", timeout, output="partial")

        def kill(self):
            return None

        def wait(self, timeout=None):
            time.sleep(timeout)
            raise subprocess.TimeoutExpired("stuck", timeout)

    process = StuckProcess()

    def popen(*_args, **_kwargs):
        cancel_event.set()
        return process

    monkeypatch.setattr(base_module.subprocess, "Popen", popen)
    monkeypatch.setattr(base_module, "get_process_group_id", lambda _pid: None)

    def slow_unconfirmed_kill(_pid):
        time.sleep(0.9)
        return False

    monkeypatch.setattr(base_module, "kill_process_tree", slow_unconfirmed_kill)

    started = time.monotonic()
    result = _DirectRuntime(tmp_path).exec(
        "ignored.py",
        cwd=tmp_path,
        timeout=10,
        cancel_event=cancel_event,
    )
    elapsed = time.monotonic() - started

    assert elapsed < 2.0
    assert result.returncode == 130
    assert result.termination_confirmed is False
    assert result.termination_fence.persistent is True
    assert all(timeout <= 0.5 for timeout in communicate_timeouts)


def test_runtime_assigns_shared_cancel_id_to_bare_pre_cancelled_event(
    tmp_path: Path,
    caplog,
) -> None:
    caplog.set_level(logging.INFO)
    cancel_event = threading.Event()
    cancel_event.set()

    result = _DirectRuntime(tmp_path).exec(
        "not-started.py",
        cwd=tmp_path,
        cancel_event=cancel_event,
    )

    cancel_id = get_cancel_id(cancel_event)
    messages = [record.getMessage() for record in caplog.records]
    trace = [message for message in messages if "process_cancel_before_spawn" in message]
    assert result.returncode == 130
    assert len(trace) == 1
    assert cancel_id in trace[0]
    assert "event-" not in trace[0]


def test_runtime_exec_interrupt_kills_real_child_process_tree(tmp_path: Path) -> None:
    started = tmp_path / "tree-started"
    child_started = tmp_path / "child-started"
    child_completed = tmp_path / "child-completed"
    child_script = tmp_path / "child.py"
    parent_script = tmp_path / "parent.py"
    child_script.write_text(
        "from pathlib import Path\n"
        "import time\n"
        f"Path({str(child_started)!r}).write_text('started')\n"
        "time.sleep(1.5)\n"
        f"Path({str(child_completed)!r}).write_text('completed')\n",
        encoding="utf-8",
    )
    parent_script.write_text(
        "from pathlib import Path\n"
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, {str(child_script)!r}])\n"
        f"Path({str(started)!r}).write_text('started')\n"
        "time.sleep(5)\n",
        encoding="utf-8",
    )
    cancel_event = threading.Event()

    def cancel_after_start() -> None:
        deadline = time.monotonic() + 2
        while not child_started.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        cancel_event.set()

    canceller = threading.Thread(target=cancel_after_start)
    canceller.start()
    result = _DirectRuntime(tmp_path).exec(
        str(parent_script),
        cwd=tmp_path,
        timeout=10,
        cancel_event=cancel_event,
    )
    canceller.join(1)

    assert started.exists()
    assert child_started.exists()
    assert result.returncode == 130
    assert result.termination_confirmed is True
    time.sleep(1.8)
    assert child_completed.exists() is False


def test_runtime_exec_timeout_remains_124(tmp_path: Path) -> None:
    script = tmp_path / "timeout.py"
    script.write_text("import time\ntime.sleep(5)\n", encoding="utf-8")

    result = _DirectRuntime(tmp_path).exec(str(script), cwd=tmp_path, timeout=0.05)

    assert result.returncode == 124


def test_runtime_cancel_never_waits_unbounded_when_tree_kill_is_unconfirmed(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import mclaw.runtime.base as base_module

    cancel_event = threading.Event()

    class Pipe:
        closed = False

        def close(self) -> None:
            raise AssertionError("busy pipes must not be synchronously closed")

    class StuckProcess:
        pid = 123
        returncode = None

        def __init__(self) -> None:
            self.stdin = Pipe()
            self.stdout = Pipe()
            self.killed = False

        def communicate(self, *_args, **_kwargs):
            raise subprocess.TimeoutExpired("stuck", 1, output="partial-output")

        def kill(self) -> None:
            self.killed = True

        def wait(self, **_kwargs):
            raise subprocess.TimeoutExpired("stuck", 0.2)

        def poll(self):
            return None

    process = StuckProcess()

    def popen(*_args, **_kwargs):
        cancel_event.set()
        return process

    monkeypatch.setattr(base_module.subprocess, "Popen", popen)
    monkeypatch.setattr(base_module, "kill_process_tree", lambda _pid: False)

    started = time.monotonic()
    result = _DirectRuntime(tmp_path).exec(
        "ignored.py",
        cwd=tmp_path,
        timeout=10,
        cancel_event=cancel_event,
    )

    assert time.monotonic() - started < 0.5
    assert result.returncode == 130
    assert result.termination_confirmed is False
    assert result.output == "partial-output"
    assert process.killed is True
    assert process.stdin.closed is False
    assert process.stdout.closed is False


def test_windows_tree_kill_falls_back_when_taskkill_reports_failure(monkeypatch) -> None:
    import mclaw.runtime.process as process_module

    killed = []
    monkeypatch.setattr(process_module.os, "name", "nt")
    monkeypatch.setattr(
        process_module.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1),
    )
    monkeypatch.setattr(process_module.os, "kill", lambda pid, signal: killed.append((pid, signal)))

    assert process_module.kill_process_tree(321) is False
    assert killed == [(321, 9)]


def test_posix_tree_kill_targets_the_spawned_process_group(monkeypatch) -> None:
    import mclaw.runtime.process as process_module

    signals = []
    monkeypatch.setattr(process_module.os, "name", "posix")
    monkeypatch.setattr(process_module.signal, "SIGTERM", 15, raising=False)
    monkeypatch.setattr(process_module.signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(process_module.os, "getpgid", lambda _pid: 654, raising=False)
    monkeypatch.setattr(
        process_module.os,
        "killpg",
        lambda pgid, signal_number: signals.append((pgid, signal_number)),
        raising=False,
    )
    monkeypatch.setattr(process_module.time, "sleep", lambda _seconds: None)

    assert process_module.kill_process_tree(321) is True
    assert signals == [
        (654, process_module.signal.SIGTERM),
        (654, process_module.signal.SIGKILL),
    ]


def test_posix_foreground_confirms_group_only_after_reaping_parent(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import mclaw.runtime.base as base_module

    cancel_event = threading.Event()
    events: list[str] = []

    class FakeProcess:
        pid = 321

        def __init__(self) -> None:
            self.returncode = None
            self.calls = 0

        def communicate(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                cancel_event.set()
                raise subprocess.TimeoutExpired("running", 0.05)
            events.append("reap_parent")
            self.returncode = -15
            return "partial", None

        def poll(self):
            return self.returncode

    monkeypatch.setattr(base_module.subprocess, "Popen", lambda *_args, **_kwargs: FakeProcess())
    monkeypatch.setattr(base_module.os, "name", "posix")
    monkeypatch.setattr(base_module.os, "setsid", lambda: None, raising=False)
    monkeypatch.setattr(base_module, "get_process_group_id", lambda _pid: 654)
    monkeypatch.setattr(
        base_module,
        "kill_process_group",
        lambda _pgid: (events.append("signal_group"), True)[1],
    )

    def confirm_group(_pgid, timeout):
        assert timeout <= 1.5
        assert events == ["signal_group", "reap_parent"]
        events.append("probe_group")
        return True

    monkeypatch.setattr(base_module, "wait_for_process_group_exit", confirm_group)

    result = _DirectRuntime(tmp_path).exec(
        "unused.py",
        cwd=tmp_path,
        timeout=10,
        cancel_event=cancel_event,
    )

    assert events == ["signal_group", "reap_parent", "probe_group"]
    assert result.termination_confirmed is True
    assert result.termination_fence is None


def test_posix_foreground_targeted_group_is_not_confirmation(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import mclaw.runtime.base as base_module

    cancel_event = threading.Event()

    class FakeProcess:
        pid = 321
        returncode = None

        def __init__(self) -> None:
            self.calls = 0

        def communicate(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                cancel_event.set()
                raise subprocess.TimeoutExpired("running", 0.05)
            self.returncode = -15
            return "partial", None

        def poll(self):
            return self.returncode

    monkeypatch.setattr(base_module.subprocess, "Popen", lambda *_args, **_kwargs: FakeProcess())
    monkeypatch.setattr(base_module.os, "name", "posix")
    monkeypatch.setattr(base_module.os, "setsid", lambda: None, raising=False)
    monkeypatch.setattr(base_module, "get_process_group_id", lambda _pid: 654)
    monkeypatch.setattr(base_module, "kill_process_group", lambda _pgid: True)
    monkeypatch.setattr(
        base_module,
        "wait_for_process_group_exit",
        lambda _pgid, timeout: False,
    )

    result = _DirectRuntime(tmp_path).exec(
        "unused.py",
        cwd=tmp_path,
        timeout=10,
        cancel_event=cancel_event,
    )

    assert result.termination_confirmed is False
    assert result.termination_fence is not None


def test_posix_background_reaps_zombie_parent_before_group_probe(monkeypatch) -> None:
    import mclaw.tools.process_registry as process_module

    events: list[str] = []

    class FakeProcess:
        returncode = None

        def wait(self, timeout=None):
            events.append("reap_parent")
            self.returncode = -15
            return self.returncode

        def poll(self):
            return self.returncode

    registry = process_module.ProcessRegistry()
    session = process_module.ProcessSession(
        id="posix-order",
        command="parent",
        pid=321,
        process=FakeProcess(),
        process_group_id=654,
    )
    monkeypatch.setattr(process_module, "_IS_WINDOWS", False)
    monkeypatch.setattr(
        process_module,
        "kill_process_group",
        lambda _pgid: (events.append("signal_group"), True)[1],
    )

    def confirm_group(_pgid, timeout):
        assert timeout <= 1.5
        # A killed but unreaped group leader is still visible to killpg(..., 0).
        # Reaping first prevents that zombie from creating a permanent fence.
        assert events == ["signal_group", "reap_parent"]
        events.append("probe_group")
        return True

    monkeypatch.setattr(process_module, "wait_for_process_group_exit", confirm_group)

    assert registry._terminate_uncommitted_spawn(session) is True
    assert events == ["signal_group", "reap_parent", "probe_group"]


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX process groups")
def test_posix_real_parent_and_child_group_is_reaped_and_gone(tmp_path: Path) -> None:
    parent_pid_path = tmp_path / "parent-pid"
    parent_pgid_path = tmp_path / "parent-pgid"
    child_started = tmp_path / "child-started"
    child_script = tmp_path / "posix-child.py"
    parent_script = tmp_path / "posix-parent.py"
    child_script.write_text(
        "from pathlib import Path\n"
        "import os, time\n"
        f"Path({str(child_started)!r}).write_text(f'{{os.getpid()}}:{{os.getpgrp()}}')\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    parent_script.write_text(
        "from pathlib import Path\n"
        "import os, subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, {str(child_script)!r}])\n"
        f"Path({str(parent_pid_path)!r}).write_text(str(os.getpid()))\n"
        f"Path({str(parent_pgid_path)!r}).write_text(str(os.getpgrp()))\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    cancel_event = threading.Event()

    def cancel_after_child_start() -> None:
        deadline = time.monotonic() + 5
        while not child_started.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        cancel_event.set()

    canceller = threading.Thread(target=cancel_after_child_start)
    canceller.start()
    result = _DirectRuntime(tmp_path).exec(
        str(parent_script),
        cwd=tmp_path,
        timeout=20,
        cancel_event=cancel_event,
    )
    canceller.join(1)

    parent_pid = int(parent_pid_path.read_text())
    process_group_id = int(parent_pgid_path.read_text())
    child_group_id = int(child_started.read_text().split(":", 1)[1])
    assert child_group_id == process_group_id
    assert result.termination_confirmed is True
    with pytest.raises(ChildProcessError):
        os.waitpid(parent_pid, os.WNOHANG)
    with pytest.raises(ProcessLookupError):
        os.killpg(process_group_id, 0)


def test_terminal_passes_turn_interrupt_to_foreground_and_background_start(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import mclaw.tools.terminal_tool as terminal_module

    cancel_event = threading.Event()
    captured: dict = {}

    class FakeRuntime:
        def exec(self, command, **kwargs):
            captured.update(kwargs)
            return ExecResult("ok", 0, str(tmp_path), "test", "test")

    monkeypatch.setattr(terminal_module, "get_interrupt_event", lambda: cancel_event)
    monkeypatch.setattr(terminal_module.RuntimeManager, "current", lambda: FakeRuntime())
    terminal_module._env_registry.clear()

    result = json.loads(terminal_module.terminal_tool("echo ok", workdir=str(tmp_path)))

    assert result["returncode"] == 0
    assert captured["cancel_event"] is cancel_event

    from mclaw.tools.process_registry import process_registry

    monkeypatch.setattr(
        process_registry,
        "spawn_local",
        lambda **_kwargs: SimpleNamespace(id="background-id", pid=123),
    )
    monkeypatch.setattr(terminal_module, "get_interrupt_event", lambda: cancel_event)
    background = json.loads(terminal_module.terminal_tool("long task", background=True))

    assert background["status"] == "running"


def test_terminal_does_not_spawn_detached_process_after_turn_cancel(monkeypatch) -> None:
    import mclaw.tools.terminal_tool as terminal_module

    from mclaw.tools.process_registry import process_registry

    cancel_event = threading.Event()
    cancel_event.set()
    spawned = []
    monkeypatch.setattr(terminal_module, "get_interrupt_event", lambda: cancel_event)
    monkeypatch.setattr(
        process_registry,
        "spawn_local",
        lambda **kwargs: spawned.append(kwargs),
    )

    result = json.loads(terminal_module.terminal_tool("long task", background=True))

    assert spawned == []
    assert result["returncode"] == 130
    assert result["status"] == "cancelled"
    assert result["interrupted"] is True


def test_background_spawn_race_kills_process_when_logging_handler_raises(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import mclaw.tools.process_registry as process_module
    from mclaw.runtime.process import SpawnResult

    cancel_event = threading.Event()

    class RaisingHandler(logging.Handler):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def emit(self, _record) -> None:
            self.calls += 1
            raise RuntimeError("logging failed")

    class FakeProcess:
        returncode = None

        def wait(self, timeout=None):
            self.returncode = -9
            return self.returncode

        def poll(self):
            return self.returncode

    process = FakeProcess()

    class FakeRuntime:
        kind = "test"
        paths = SimpleNamespace(process_log_root=lambda: tmp_path / "logs")

        def spawn(self, *_args, **_kwargs):
            cancel_event.set()
            return SpawnResult(
                pid=4321,
                process=process,
                cwd=str(tmp_path),
                env_hash="env",
                shell_profile="test",
            )

    monkeypatch.setattr(process_module.RuntimeManager, "current", lambda: FakeRuntime())
    monkeypatch.setattr(process_module, "kill_process_tree", lambda _pid: True)
    registry = process_module.ProcessRegistry()
    handler = RaisingHandler()
    old_level = process_module.logger.level
    process_module.logger.setLevel(logging.INFO)
    process_module.logger.addHandler(handler)
    try:
        with pytest.raises(InterruptedError, match="cancelled"):
            registry.spawn_local(
                "slow",
                cwd=str(tmp_path),
                cancel_event=cancel_event,
            )
    finally:
        process_module.logger.removeHandler(handler)
        process_module.logger.setLevel(old_level)

    assert process.poll() == -9
    assert registry._running == {}
    assert handler.calls >= 3


def test_background_spawn_cancel_at_registry_commit_is_not_persisted(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import mclaw.tools.process_registry as process_module
    from mclaw.runtime.process import SpawnResult

    cancel_event = threading.Event()

    class FakeProcess:
        returncode = None
        stdout = []

        def wait(self, timeout=None):
            self.returncode = -9
            return self.returncode

        def poll(self):
            return self.returncode

    process = FakeProcess()

    class FakeRuntime:
        kind = "test"
        paths = SimpleNamespace(process_log_root=lambda: tmp_path / "logs")

        def spawn(self, *_args, **_kwargs):
            return SpawnResult(
                pid=5432,
                process=process,
                cwd=str(tmp_path),
                env_hash="env",
                shell_profile="test",
            )

    monkeypatch.setattr(process_module.RuntimeManager, "current", lambda: FakeRuntime())
    monkeypatch.setattr(process_module, "kill_process_tree", lambda _pid: True)
    registry = process_module.ProcessRegistry()
    register = registry._register_running_or_abort

    def cancel_at_commit(session, event=None):
        cancel_event.set()
        return register(session, event)

    monkeypatch.setattr(registry, "_register_running_or_abort", cancel_at_commit)

    with pytest.raises(InterruptedError, match="cancelled"):
        registry.spawn_local(
            "slow",
            cwd=str(tmp_path),
            cancel_event=cancel_event,
        )

    assert process.poll() == -9
    assert registry._running == {}


def test_exited_background_parent_does_not_confirm_descendant_termination(
    monkeypatch,
) -> None:
    import mclaw.tools.process_registry as process_module

    class ExitedParent:
        returncode = 0

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    registry = process_module.ProcessRegistry()
    session = process_module.ProcessSession(
        id="exited-parent",
        command="parent",
        pid=6789,
        process=ExitedParent(),
    )
    monkeypatch.setattr(process_module, "kill_process_tree", lambda _pid: False)

    with pytest.raises(process_module.BackgroundSpawnCancellationError):
        registry._abort_cancelled_spawn(session)


def test_stopped_pty_parent_does_not_confirm_descendant_termination(
    monkeypatch,
) -> None:
    import mclaw.tools.process_registry as process_module

    class StoppedPty:
        def isalive(self):
            return False

        def terminate(self, force=False):
            raise AssertionError("already-stopped PTY should not be terminated again")

        def wait(self):
            raise AssertionError("unbounded PTY wait must not run during cancellation")

    registry = process_module.ProcessRegistry()
    session = process_module.ProcessSession(
        id="stopped-pty-parent",
        command="parent",
        pid=6790,
    )
    session._pty = StoppedPty()
    monkeypatch.setattr(process_module, "kill_process_tree", lambda _pid: False)

    started = time.monotonic()
    with pytest.raises(process_module.BackgroundSpawnCancellationError):
        registry._abort_cancelled_spawn(session)
    assert time.monotonic() - started < 2.0


def test_uncommitted_background_spawn_cleanup_has_one_wall_clock_budget(
    monkeypatch,
) -> None:
    import mclaw.tools.process_registry as process_module

    wait_timeouts: list[float] = []

    class StuckProcess:
        returncode = None

        def poll(self):
            return None

        def wait(self, timeout=None):
            wait_timeouts.append(timeout)
            time.sleep(timeout)
            raise subprocess.TimeoutExpired("stuck-background", timeout)

    registry = process_module.ProcessRegistry()
    session = process_module.ProcessSession(
        id="bounded-uncommitted",
        command="parent",
        pid=6792,
        process=StuckProcess(),
    )

    def slow_unconfirmed_kill(_pid):
        time.sleep(0.9)
        return False

    monkeypatch.setattr(process_module, "kill_process_tree", slow_unconfirmed_kill)

    started = time.monotonic()
    with pytest.raises(process_module.BackgroundSpawnCancellationError) as exc_info:
        registry._abort_cancelled_spawn(session)
    elapsed = time.monotonic() - started

    assert elapsed < 2.0
    assert wait_timeouts and all(timeout <= 0.5 for timeout in wait_timeouts)
    assert exc_info.value.termination_fence.persistent is True


def test_checkpoint_failure_with_unconfirmed_tree_keeps_registry_and_fence(
    monkeypatch,
) -> None:
    import mclaw.tools.process_registry as process_module

    class StuckProcess:
        returncode = None

        def poll(self):
            return None

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired("stuck", timeout)

    registry = process_module.ProcessRegistry()
    session = process_module.ProcessSession(
        id="checkpoint-unconfirmed",
        command="parent",
        pid=6791,
        process=StuckProcess(),
    )
    monkeypatch.setattr(process_module, "kill_process_tree", lambda _pid: False)
    monkeypatch.setattr(
        registry,
        "_write_checkpoint",
        lambda: (_ for _ in ()).throw(OSError("disk unavailable")),
    )

    with pytest.raises(process_module.BackgroundSpawnCancellationError) as exc_info:
        registry._register_running_or_abort(session)

    assert registry._running[session.id] is session
    assert exc_info.value.termination_fence.persistent is True
    assert "could not be persisted" in str(exc_info.value)


def test_unconfirmed_background_spawn_race_fails_closed(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import mclaw.tools.process_registry as process_module
    from mclaw.runtime.process import SpawnResult
    from mclaw.tools.dispatch import handle_function_calls

    cancel_event = threading.Event()

    class StuckProcess:
        returncode = None

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired("stuck", timeout)

        def poll(self):
            return None

    class FakeRuntime:
        kind = "test"
        paths = SimpleNamespace(process_log_root=lambda: tmp_path / "logs")

        def spawn(self, *_args, **_kwargs):
            cancel_event.set()
            return SpawnResult(
                pid=9876,
                process=StuckProcess(),
                cwd=str(tmp_path),
                env_hash="env",
                shell_profile="test",
            )

    class Parent:
        session_id = "background-race"
        workspace_path = str(tmp_path)
        config = {}

        def __init__(self) -> None:
            self.workers = set()
            self.abort_reasons = []

        def _register_turn_worker(self, worker) -> None:
            self.workers.add(worker)

        def _unregister_turn_worker(self, worker) -> None:
            self.workers.discard(worker)

        def _request_turn_abort(self, reason, event) -> None:
            self.abort_reasons.append(reason)
            event.set()

    monkeypatch.setattr(process_module.RuntimeManager, "current", lambda: FakeRuntime())
    monkeypatch.setattr(process_module, "kill_process_tree", lambda _pid: False)
    before_sessions = set(process_module.process_registry._running)
    parent = Parent()
    call = {
        "id": "background-terminal",
        "type": "function",
        "function": {
            "name": "terminal",
            "arguments": json.dumps(
                {"command": "slow", "workdir": str(tmp_path), "background": True}
            ),
        },
    }

    results = handle_function_calls(
        [call],
        {"terminal"},
        parent_agent=parent,
        cancel_event=cancel_event,
    )
    result = json.loads(results[0])

    assert result["status"] == "cancel_requested"
    assert result["completion_unknown"] is True
    assert parent.abort_reasons[-1] == "tool_completion_unknown"
    assert any(getattr(worker, "persistent", False) for worker in parent.workers)
    assert set(process_module.process_registry._running) == before_sessions


def test_terminal_reports_unconfirmed_tree_kill_as_completion_unknown(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import mclaw.tools.terminal_tool as terminal_module

    class Fence:
        def is_alive(self) -> bool:
            return True

    fence = Fence()
    registered = []
    parent = SimpleNamespace(_register_turn_worker=registered.append)

    class FakeRuntime:
        def exec(self, _command, **_kwargs):
            return ExecResult(
                "partial",
                130,
                str(tmp_path),
                "test",
                "test",
                termination_confirmed=False,
                termination_fence=fence,
            )

    monkeypatch.setattr(terminal_module.RuntimeManager, "current", lambda: FakeRuntime())
    result = json.loads(
        terminal_module.terminal_tool(
            "slow",
            workdir=str(tmp_path),
            parent_agent=parent,
        )
    )

    assert result["status"] == "cancel_requested"
    assert result["completion_unknown"] is True
    assert result["interrupted"] is True
    assert registered == [fence]


def test_process_wait_interrupt_terminates_owned_background(monkeypatch) -> None:
    import mclaw.tools.process_registry as process_module
    from mclaw.tools.interrupt import reset_interrupt_event, set_interrupt_event

    registry = process_module.ProcessRegistry()
    session = process_module.ProcessSession(
        id="background-id",
        command="long task",
        started_at=time.time(),
    )
    registry._running[session.id] = session
    registry._ensure_checkpoint_present = lambda: None
    registry._default_wait_timeout = lambda: 5
    killed: list[str] = []

    def kill_process(session_id: str) -> dict:
        killed.append(session_id)
        session.exited = True
        session.exit_code = -15
        return {
            "status": "killed",
            "termination_confirmed": True,
            "exit_code": -15,
        }

    monkeypatch.setattr(registry, "kill_process", kill_process)
    cancel_event = threading.Event()
    token = set_interrupt_event(cancel_event)
    threading.Timer(0.05, cancel_event.set).start()

    began = time.monotonic()
    try:
        result = registry.wait(session.id)
    finally:
        reset_interrupt_event(token)

    assert result["status"] == "interrupted"
    assert result["interrupted"] is True
    assert result["termination_confirmed"] is True
    assert time.monotonic() - began < 0.5
    assert killed == [session.id]
    assert registry.get(session.id) is session
    assert session.exited is True


def test_registered_background_kill_reaps_parent_before_group_confirmation(
    monkeypatch,
) -> None:
    import mclaw.tools.process_registry as process_module

    events: list[str] = []

    class FakeProcess:
        returncode = None

        def wait(self, timeout=None):
            events.append("reap_parent")
            self.returncode = -15
            return self.returncode

        def poll(self):
            return self.returncode

    registry = process_module.ProcessRegistry()
    session = process_module.ProcessSession(
        id="owned-background",
        command="long task",
        task_id="task-1",
        session_key="dsoftbus:session-1",
        pid=321,
        process=FakeProcess(),
        process_group_id=654,
        started_at=time.time(),
    )
    registry._running[session.id] = session
    monkeypatch.setattr(process_module, "_IS_WINDOWS", False)
    monkeypatch.setattr(
        process_module,
        "kill_process_group",
        lambda process_group_id: (
            events.append(f"signal_group:{process_group_id}"),
            True,
        )[1],
    )

    def confirm_group(process_group_id: int, *, timeout: float) -> bool:
        if timeout == 0:
            events.append(f"probe_initial:{process_group_id}")
            return False
        assert events[-2:] == ["signal_group:654", "reap_parent"]
        events.append(f"probe_final:{process_group_id}")
        return True

    monkeypatch.setattr(process_module, "wait_for_process_group_exit", confirm_group)

    result = registry.kill_process(session.id)

    assert result["status"] == "killed"
    assert result["termination_confirmed"] is True
    assert events == [
        "probe_initial:654",
        "signal_group:654",
        "reap_parent",
        "probe_final:654",
    ]


def test_terminate_scope_matches_both_remote_task_and_session(monkeypatch) -> None:
    import mclaw.tools.process_registry as process_module

    registry = process_module.ProcessRegistry()
    matching = process_module.ProcessSession(
        id="matching",
        command="one",
        task_id="task-1",
        session_key="dsoftbus:session-1",
    )
    wrong_task = process_module.ProcessSession(
        id="wrong-task",
        command="two",
        task_id="task-2",
        session_key="dsoftbus:session-1",
    )
    wrong_session = process_module.ProcessSession(
        id="wrong-session",
        command="three",
        task_id="task-1",
        session_key="dsoftbus:session-2",
    )
    registry._running = {
        session.id: session
        for session in (matching, wrong_task, wrong_session)
    }
    killed: list[str] = []

    def kill_process(session_id: str) -> dict:
        killed.append(session_id)
        return {"status": "killed", "termination_confirmed": True}

    monkeypatch.setattr(registry, "kill_process", kill_process)

    report = registry.terminate_scope(
        task_id="task-1",
        session_key="dsoftbus:session-1",
    )

    assert report["termination_confirmed"] is True
    assert report["target_count"] == 1
    assert killed == ["matching"]


def test_dispatcher_to_terminal_cancel_kills_the_real_process(tmp_path: Path) -> None:
    from mclaw.tools import terminal_tool as terminal_module
    from mclaw.tools.dispatch import handle_function_calls, set_tool_context

    started = tmp_path / "dispatch-started"
    completed = tmp_path / "dispatch-completed"
    script = tmp_path / "dispatch-slow.py"
    script.write_text(
        "from pathlib import Path\n"
        "import time\n"
        f"Path({str(started)!r}).write_text('started')\n"
        "print('dispatch-output', flush=True)\n"
        "time.sleep(5)\n"
        f"Path({str(completed)!r}).write_text('completed')\n",
        encoding="utf-8",
    )
    cancel_event = threading.Event()
    result_box: list[str] = []

    class Parent:
        session_id = "dispatch-terminal-test"
        config = {}

        def __init__(self) -> None:
            self.workers = set()
            self.lock = threading.Lock()

        def _register_turn_worker(self, worker) -> None:
            with self.lock:
                self.workers.add(worker)

        def _unregister_turn_worker(self, worker) -> None:
            with self.lock:
                self.workers.discard(worker)

    parent = Parent()
    call = {
        "id": "terminal-call",
        "type": "function",
        "function": {
            "name": "terminal",
            "arguments": json.dumps(
                {
                    "command": f'python "{script}"',
                    "workdir": str(tmp_path),
                    "timeout": 10,
                }
            ),
        },
    }

    def dispatch_call() -> None:
        set_tool_context(session_id="dispatch-terminal-test", cancel_event=cancel_event)
        result_box.extend(
            handle_function_calls(
                [call],
                {"terminal"},
                parent_agent=parent,
                cancel_event=cancel_event,
            )
        )

    caller = threading.Thread(target=dispatch_call)
    caller.start()
    deadline = time.monotonic() + 2
    while not started.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert started.exists()

    cancelled_at = time.monotonic()
    cancel_event.set()
    caller.join(2)
    try:
        assert not caller.is_alive()
        assert time.monotonic() - cancelled_at < 2
        result = json.loads(result_box[0])
        assert result["interrupted"] is True
        if "returncode" in result:
            assert result["returncode"] == 130
            assert result["status"] == "cancelled"
            assert "dispatch-output" in result["output"]
        else:
            assert result["status"] == "cancel_requested"
            assert result["completion_unknown"] is True

        worker_deadline = time.monotonic() + 6
        while time.monotonic() < worker_deadline:
            with parent.lock:
                if not parent.workers:
                    break
            time.sleep(0.01)
        with parent.lock:
            assert parent.workers == set()
        time.sleep(0.2)
        assert not completed.exists()
    finally:
        terminal_module.cleanup_session("dispatch-terminal-test")
