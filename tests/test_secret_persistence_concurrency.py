# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Independent workers must preserve each other's scoped credential updates."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from mclaw.cli import config
from mclaw.runtime import secrets
from mclaw.system.lock import file_lock


def _wait_for(path: Path, process: subprocess.Popen) -> None:
    deadline = time.monotonic() + 10
    while not path.exists():
        assert process.poll() is None, "credential writer exited before synchronization"
        if time.monotonic() > deadline:
            pytest.fail("credential writer did not reach synchronization point")
        time.sleep(0.01)


def _spawn(home: Path, code: str) -> subprocess.Popen:
    environment = dict(os.environ, MCLAW_HOME=str(home))
    return subprocess.Popen([sys.executable, "-c", code], env=environment,
                            cwd=Path(__file__).resolve().parents[1],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            text=True)


@pytest.mark.parametrize("target", ["env", "allowlist"])
def test_two_process_credential_updates_preserve_both_changes(tmp_path, monkeypatch, target):
    monkeypatch.setenv("MCLAW_HOME", str(tmp_path))
    common = """
import os, time
from pathlib import Path
from mclaw.cli import config
from mclaw.runtime import secrets
home = Path(os.environ['MCLAW_HOME'])
"""
    pause = """
def pause_write(*args, **kwargs):
    (home / 'ready').touch()
    deadline = time.monotonic() + 10
    while not (home / 'release').exists():
        if time.monotonic() > deadline:
            raise TimeoutError('test synchronization timed out')
        time.sleep(0.01)
    return original(*args, **kwargs)
"""
    if target == "env":
        first_code = common + "original = os.replace\n" + pause + """
os.replace = pause_write
config.save_env_value('CONCURRENCY_FIRST_KEY', 'first-test-value')
"""
        second_action = "config.save_env_value('CONCURRENCY_SECOND_KEY', 'second-test-value')"
    else:
        first_code = common + "original = secrets.save_allowlist\n" + pause + """
secrets.save_allowlist = pause_write
secrets.authorize('tool:first', ['CONCURRENCY_FIRST_KEY'])
"""
        second_action = "secrets.authorize('tool:second', ['CONCURRENCY_SECOND_KEY'])"

    first = _spawn(tmp_path, first_code)
    second = None
    try:
        _wait_for(tmp_path / "ready", first)
        second = _spawn(tmp_path, common + "(home / 'second_started').touch()\n" + second_action)
        _wait_for(tmp_path / "second_started", second)
        # The first process has read its snapshot but has not published it yet.
        time.sleep(0.2)
        assert second.poll() is None
        (tmp_path / "release").touch()
        for process in (first, second):
            _, error = process.communicate(timeout=10)
            assert process.returncode == 0, error
    finally:
        (tmp_path / "release").touch()
        for process in (first, second):
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=5)

    if target == "env":
        values = config.load_env()
        assert values["CONCURRENCY_FIRST_KEY"] == "first-test-value"
        assert values["CONCURRENCY_SECOND_KEY"] == "second-test-value"
    else:
        assert secrets.authorized_env_vars("tool:first") == ["CONCURRENCY_FIRST_KEY"]
        assert secrets.authorized_env_vars("tool:second") == ["CONCURRENCY_SECOND_KEY"]


def test_removing_env_value_obeys_same_commit_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("MCLAW_HOME", str(tmp_path))
    (tmp_path / ".env").write_text("REMOVE_TEST_KEY=value\nKEEP_TEST_KEY=value\n", encoding="utf-8")
    code = """
import os
from pathlib import Path
from mclaw.cli.config import remove_env_value
home = Path(os.environ['MCLAW_HOME'])
(home / 'ready').touch()
assert remove_env_value('REMOVE_TEST_KEY')
"""
    process = None
    try:
        with file_lock(tmp_path / ".env.lock"):
            process = _spawn(tmp_path, code)
            _wait_for(tmp_path / "ready", process)
            time.sleep(0.2)
            assert process.poll() is None
            assert "REMOVE_TEST_KEY" in config.load_env()
        _, error = process.communicate(timeout=10)
        assert process.returncode == 0, error
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
    assert config.load_env() == {"KEEP_TEST_KEY": "value"}
