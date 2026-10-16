# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import errno
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest

from mclaw.cli.runtime.session_lock import InteractiveSessionLock
from mclaw.web import launcher, server


@pytest.fixture
def listeners(monkeypatch):
    created, attempts, failures = [], [], {}

    def create(*_args):
        listener = Mock()

        def bind(address):
            attempts.append(address)
            if error := failures.get(address[1]):
                raise OSError(error, "test bind failure")

        listener.bind.side_effect = bind
        created.append(listener)
        return listener

    monkeypatch.setattr(server.socket, "socket", create)
    return created, attempts, failures


def test_default_port_uses_first_available_and_retains_listener(listeners):
    created, attempts, failures = listeners
    failures.update({18031: errno.EADDRINUSE, 18032: errno.EADDRINUSE})
    listener = server._open_listener()
    assert attempts == [("127.0.0.1", port) for port in (18031, 18032, 18033)]
    for failed in created[:-1]:
        failed.close.assert_called_once()
    assert listener is created[-1]
    listener.listen.assert_called_once_with(128)
    listener.close.assert_not_called()


@pytest.mark.skipif(os.name != "nt", reason="Windows reserved and exclusive ports")
@pytest.mark.parametrize("error", [errno.EACCES, 10013, 10048])
def test_windows_reserved_ports_are_skipped(listeners, error):
    created, attempts, failures = listeners
    failures[18031] = error
    listener = server._open_listener()
    assert attempts == [("127.0.0.1", 18031), ("127.0.0.1", 18032)]
    listener.setsockopt.assert_called_once_with(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    created[0].close.assert_called_once()


def test_unexpected_socket_error_is_reported_without_scanning_more_ports(listeners):
    created, attempts, failures = listeners
    failures[18031] = errno.EMFILE
    with pytest.raises(OSError) as error:
        server._open_listener()
    assert error.value.errno == errno.EMFILE
    assert attempts == [("127.0.0.1", 18031)]
    created[0].close.assert_called_once()


def test_exhausted_ports_do_not_wrap_around_to_automatic_allocation(listeners):
    created, attempts, failures = listeners
    failures[65535] = errno.EADDRINUSE
    with pytest.raises(OSError, match="65535"):
        server._open_listener(65535)
    assert attempts == [("127.0.0.1", 65535)]
    created[0].close.assert_called_once()


@pytest.mark.parametrize("port", [-1, 65536, 201831, True, "18031"])
def test_invalid_port_is_rejected_before_opening_socket(listeners, port):
    created, attempts, _failures = listeners
    with pytest.raises(ValueError, match="端口"):
        server._open_listener(port)
    assert not created and not attempts


def test_zero_port_keeps_os_assigned_loopback_listener():
    with server._open_listener(0) as listener:
        host, port = listener.getsockname()
        assert host == "127.0.0.1" and 0 < port <= 65535
        with socket.create_connection((host, port), timeout=2):
            accepted, _address = listener.accept()
            accepted.close()


def test_bind_failure_releases_service_ownership(tmp_path, monkeypatch):
    from mclaw.cli import main

    monkeypatch.setattr(main, "_setup_logging", lambda: None)
    monkeypatch.setattr(server, "load_mclaw_dotenv", lambda: None)
    monkeypatch.setattr(server, "get_mclaw_home", lambda: tmp_path)
    monkeypatch.setattr(server, "_open_listener", Mock(side_effect=OSError(errno.EADDRINUSE, "test")))
    with pytest.raises(OSError):
        server.serve()
    lock = InteractiveSessionLock("mclaw-application-service", tmp_path / "web/locks")
    assert not lock.path.exists()
    assert not (tmp_path / "web/service.json").exists()


def test_occupied_port_fallback_is_published_reused_and_preferred_again(tmp_path):
    blocker = server._open_listener(0)
    preferred = blocker.getsockname()[1]
    if preferred == 65535:
        blocker.close()
        pytest.skip("No higher port available for fallback test")
    (tmp_path / "config.yaml").write_text("{}\n", encoding="utf-8")
    (tmp_path / ".env").write_text("", encoding="utf-8")
    process = None
    try:
        for occupied in (True, False):
            process = subprocess.Popen(
                [sys.executable, "-m", "mclaw.web.server", "--port", str(preferred)],
                cwd=Path(__file__).resolve().parents[1],
                env=dict(os.environ, MCLAW_HOME=str(tmp_path), PYTHONIOENCODING="utf-8"),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            deadline = time.monotonic() + 20
            while (service := launcher.running_service(tmp_path)) is None:
                assert process.poll() is None, "Service exited before publishing its endpoint"
                assert time.monotonic() < deadline, "Service did not become ready"
                time.sleep(0.05)
            assert service["pid"] == process.pid
            if occupied:
                assert preferred < service["port"] <= 65535
            else:
                assert service["port"] == preferred
            assert launcher.ensure_service(tmp_path) == service
            response = httpx.post(
                f"http://127.0.0.1:{service['port']}/api/v1/service/stop",
                headers={"Authorization": f"Bearer {service['token']}"},
                timeout=5, trust_env=False,
            )
            response.raise_for_status()
            process.wait(timeout=15)
            assert process.returncode == 0
            assert not (tmp_path / "web/service.json").exists()
            blocker.close()
    finally:
        blocker.close()
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=5)
