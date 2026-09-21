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

import pytest

from mclaw.cli.runtime import session_lock
from mclaw.cli.runtime.session_lock import InteractiveSessionLock, InteractiveSessionLockError


def test_live_worker_blocks_takeover_after_service_owner_exits(tmp_path, monkeypatch):
    owner = InteractiveSessionLock("session", tmp_path)
    owner.acquire()
    payload = owner._read_lock()
    payload.update(pid=111, worker_pid=222)
    owner.path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(session_lock, "_is_process_alive", lambda pid: pid == 222)
    contender = InteractiveSessionLock("session", tmp_path)
    with pytest.raises(InteractiveSessionLockError):
        contender.acquire()
    monkeypatch.setattr(session_lock, "_is_process_alive", lambda _pid: False)
    contender.acquire()
    contender.release()


def test_parent_release_retains_live_worker_fence_until_cleanup(tmp_path, monkeypatch):
    owner = InteractiveSessionLock("session", tmp_path)
    owner.acquire()
    owner.attach_worker(222)
    monkeypatch.setattr(session_lock, "_is_process_alive", lambda pid: pid == 222)
    owner.release()
    assert owner.path.exists()
    assert owner._acquired
    monkeypatch.setattr(session_lock, "_is_process_alive", lambda _pid: False)
    owner.release()
    assert not owner.path.exists()


def test_worker_attachment_requires_original_owner(tmp_path):
    owner = InteractiveSessionLock("session", tmp_path)
    owner.acquire()
    payload = owner._read_lock()
    payload["pid"] = os.getpid() + 10000
    owner.path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(InteractiveSessionLockError):
        owner.attach_worker(222)


def test_empty_metadata_publication_window_cannot_be_stolen(tmp_path, monkeypatch):
    first = InteractiveSessionLock("session", tmp_path)
    second = InteractiveSessionLock("session", tmp_path)
    created, publish, contending = threading.Event(), threading.Event(), threading.Event()
    real_dump = session_lock.json.dump

    def paused_dump(data, handle, **kwargs):
        created.set()
        assert publish.wait(5)
        return real_dump(data, handle, **kwargs)

    monkeypatch.setattr(session_lock.json, "dump", paused_dump)
    def compete():
        contending.set()
        second.acquire()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first_result = pool.submit(first.acquire)
        assert created.wait(3)
        second_result = pool.submit(compete)
        assert contending.wait(3)
        assert not second_result.done()
        publish.set()
        first_result.result(timeout=5)
        with pytest.raises(InteractiveSessionLockError):
            second_result.result(timeout=5)
    first.release()


def test_two_processes_claiming_same_session_have_exactly_one_owner(tmp_path):
    script = """
import sys
from pathlib import Path
from mclaw.cli.runtime.session_lock import InteractiveSessionLock, InteractiveSessionLockError
lock = InteractiveSessionLock('shared-session', Path(sys.argv[1]))
print('ready', flush=True)
sys.stdin.readline()
try:
    lock.acquire()
except InteractiveSessionLockError:
    print('occupied', flush=True)
else:
    print('acquired', flush=True)
    sys.stdin.readline()
    lock.release()
"""
    processes = [subprocess.Popen([sys.executable, "-c", script, str(tmp_path)],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True) for _ in range(2)]
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert [future.result(timeout=10).strip() for future in [pool.submit(p.stdout.readline) for p in processes]] == ["ready", "ready"]
            for process in processes:
                process.stdin.write("start\n")
                process.stdin.flush()
            results = [future.result(timeout=10).strip() for future in [pool.submit(p.stdout.readline) for p in processes]]
        assert sorted(results) == ["acquired", "occupied"]
        winner = processes[results.index("acquired")]
        winner.communicate("release\n", timeout=10)
        for process in processes:
            process.communicate(timeout=10)
            assert process.returncode == 0
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)


def test_exited_process_is_not_alive_even_while_popen_handle_exists():
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait(timeout=10)
    assert not session_lock._is_process_alive(process.pid)
