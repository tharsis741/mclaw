# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Detached local service entry point and instance publication."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
from pathlib import Path

import uvicorn

from mclaw.cli.env_loader import load_mclaw_dotenv
from mclaw.cli.runtime.session_lock import InteractiveSessionLock
from mclaw.constants import get_mclaw_home


def serve(port: int = 0) -> None:
    from mclaw.cli.main import _setup_logging
    from mclaw.web.app import create_app

    home = get_mclaw_home().resolve()
    home.mkdir(parents=True, exist_ok=True)
    load_mclaw_dotenv()
    _setup_logging()
    lock = InteractiveSessionLock("mclaw-application-service", home / "web" / "locks")
    lock.acquire()
    record = home / "web" / "service.json"
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    token = secrets.token_urlsafe(48)
    server = None

    def publish():
        document = {"pid": os.getpid(), "port": listener.getsockname()[1], "token": token,
                    "api_version": 1, "python": os.path.realpath(os.sys.executable)}
        descriptor = os.open(str(record), os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(document, output)

    try:
        listener.bind(("127.0.0.1", port))
        listener.listen(128)
        app = create_app(home, token, shutdown=lambda: setattr(server, "should_exit", True), on_ready=publish)
        # Bound disconnected HTTP/WebSocket handlers; lifespan still waits for
        # session tools and owned processes to finish their normal cleanup.
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", access_log=False, log_level="warning",
                                               ws="websockets-sansio", timeout_graceful_shutdown=10))
        server.run(sockets=[listener])
    finally:
        listener.close()
        try:
            if json.loads(record.read_text(encoding="utf-8")).get("pid") == os.getpid():
                record.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass
        lock.release()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="M-Claw 本机后台服务")
    parser.add_argument("--port", type=int, default=0)
    serve(parser.parse_args().port)
