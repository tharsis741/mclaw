# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import os
import subprocess
import time
from types import SimpleNamespace

import pytest

from mclaw.system import process


def test_current_process_has_stable_birth_time():
    if os.name != "nt" and not process.Path("/proc/self/stat").exists():
        pytest.skip("Birth time is available on Windows and procfs hosts")
    born = process.process_started_at(os.getpid())
    assert born is not None and 0 < born <= time.time()
    assert process.process_started_at(os.getpid()) == born
    assert process.is_process_alive(os.getpid())
    assert process.process_matches_record(os.getpid(), born)
    assert not process.process_matches_record(os.getpid(), born - 60)


@pytest.mark.parametrize("started,recorded,actual,expected", [
    (100, None, 100, True),
    (100, None, 200, False),
    (100.123456, None, 100.123, True),
    (100, None, None, True),
    (None, "1970-01-01T00:01:40Z", 99, True),
    (None, "1970-01-01T00:01:40Z", 100.8, True),
    (None, "1970-01-01T00:01:40Z", 101, False),
    (None, "1970-01-01T00:01:40Z", 200, False),
    (None, "invalid", 200, True),
    (None, None, 200, True),
])
def test_birth_time_rejects_recycled_pid_but_preserves_uncertain_owner(
    monkeypatch, started, recorded, actual, expected,
):
    monkeypatch.setattr(process, "process_started_at", lambda _pid: actual)
    assert process.process_matches_record(123, started, recorded_at=recorded) is expected


def test_proc_birth_time_handles_spaces_and_parentheses_in_process_name(tmp_path, monkeypatch):
    (tmp_path / "123").mkdir()
    fields = ["S"] + ["0"] * 18 + ["250"]
    (tmp_path / "123/stat").write_text("123 (a ) name) " + " ".join(fields))
    (tmp_path / "stat").write_text("cpu 1 2 3\nbtime 1000\n")
    monkeypatch.setattr(process.os, "sysconf", lambda _name: 100, raising=False)
    assert process._proc_started_at(123, tmp_path) == 1002.5


@pytest.mark.skipif(os.name != "nt", reason="Windows CIM fallback")
def test_cim_birth_time_agrees_with_native_process_time():
    native = process.process_started_at(os.getpid())
    cim = process._windows_cim_started_at(os.getpid())
    assert native is not None and cim is not None
    assert abs(native - cim) < 0.001


@pytest.mark.skipif(os.name != "nt", reason="Windows process probe")
def test_access_denied_uses_birth_time_without_treating_process_as_dead(monkeypatch):
    import ctypes

    monkeypatch.setattr(process, "_windows_api", lambda: SimpleNamespace(OpenProcess=lambda *_args: 0))
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5)
    monkeypatch.setattr(process, "_windows_cim_started_at", lambda _pid: 200)
    assert process.is_process_alive(123)
    assert process.process_started_at(123) == 200
    assert not process.process_matches_record(123, 100)


@pytest.mark.skipif(os.name != "nt", reason="Windows CIM fallback")
@pytest.mark.parametrize("output", [b"", b"unavailable"])
def test_unavailable_cim_metadata_remains_unknown(monkeypatch, output):
    def run(args, **kwargs):
        assert kwargs["creationflags"] == subprocess.CREATE_NO_WINDOW
        assert kwargs["timeout"] == 3
        assert "-NoProfile" in args
        return SimpleNamespace(stdout=output)
    monkeypatch.setattr(subprocess, "run", run)
    assert process._windows_cim_started_at(123) is None
