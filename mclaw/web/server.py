# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Detached local service entry point and instance publication."""

from __future__ import annotations

import argparse
import errno
import json
import os
import secrets
import socket
from pathlib import Path

import uvicorn

from mclaw.cli.env_loader import load_mclaw_dotenv
from mclaw.cli.runtime.session_lock import InteractiveSessionLock
from mclaw.constants import get_mclaw_home


DEFAULT_PORT = 18031


def _open_listener(port: int = DEFAULT_PORT) -> socket.socket:
    """Bind the first available loopback port, retaining ownership until shutdown."""
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("端口必须在 0 到 65535 之间；0 表示自动分配。")
    candidates = (0,) if port == 0 else range(port, 65536)
    retry_errors = {errno.EADDRINUSE}
    if os.name == "nt":
        # Windows also reports exclusive listeners and reserved ports as WSAEACCES.
        retry_errors.update({errno.EACCES, 10013, 10048})
    for candidate in candidates:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if os.name == "nt":
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", candidate))
            listener.listen(128)
            return listener
        except OSError as exc:
            listener.close()
            if port == 0 or exc.errno not in retry_errors:
                raise
        except BaseException:
            listener.close()
            raise
    raise OSError(errno.EADDRINUSE, f"端口 {port} 到 65535 均不可用。")


def serve(port: int = DEFAULT_PORT) -> None:
    from mclaw.cli.main import _setup_logging
    from mclaw.web.app import create_app

    home = get_mclaw_home().resolve()
    home.mkdir(parents=True, exist_ok=True)
    load_mclaw_dotenv()
    _setup_logging()
    lock = InteractiveSessionLock("mclaw-application-service", home / "web" / "locks")
    lock.acquire()
    record = home / "web" / "service.json"
    listener = None
    token = secrets.token_urlsafe(48)
    server = None

    def publish():
        document = {"pid": os.getpid(), "port": listener.getsockname()[1], "token": token,
                    "api_version": 1, "python": os.path.realpath(os.sys.executable)}
        descriptor = os.open(str(record), os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(document, output)

    try:
        listener = _open_listener(port)
        app = create_app(home, token, shutdown=lambda: setattr(server, "should_exit", True), on_ready=publish)
        # Bound disconnected HTTP/WebSocket handlers; lifespan still waits for
        # session tools and owned processes to finish their normal cleanup.
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", access_log=False, log_level="warning",
                                               ws="websockets-sansio", timeout_graceful_shutdown=10))
        server.run(sockets=[listener])
    finally:
        if listener is not None:
            listener.close()
        try:
            if json.loads(record.read_text(encoding="utf-8")).get("pid") == os.getpid():
                record.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass
        lock.release()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="M-Claw 本机后台服务")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"起始端口（默认 {DEFAULT_PORT}）；占用时递增，0 表示自动分配")
    serve(parser.parse_args().port)
