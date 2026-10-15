# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Real service shutdown must reap an isolated worker and release ownership."""

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from websockets.sync.client import connect

from mclaw.cli.runtime.session_lock import InteractiveSessionLock, _is_process_alive


@pytest.mark.parametrize("connection_mode", ["open", "closed", "reset", "completed", "cli"])
def test_http_shutdown_reaps_worker_and_service(tmp_path, connection_mode):
    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            chunks = []
            for index in range(200 if connection_mode == "cli" else 30):
                payload = {"id": "shutdown-test", "object": "chat.completion.chunk", "created": 1,
                           "model": "test-model", "choices": [{"index": 0, "delta": {"content": "x"},
                                                                 "finish_reason": None}]}
                chunks.append(("data: " + json.dumps(payload) + "\n\n").encode())
            if connection_mode == "completed":
                has_result = any(item.get("tool_call_id") == "shutdown-command" for item in request["messages"])
                delta = {"content": "completed"} if has_result else {"tool_calls": [{"index": 0,
                    "id": "shutdown-command", "type": "function", "function": {
                        "name": "terminal", "arguments": '{"command":"echo shutdown-check"}'}}]}
                payload["choices"] = [{"index": 0, "delta": delta, "finish_reason": "stop" if has_result else "tool_calls"}]
                chunks = [("data: " + json.dumps(payload) + "\n\n").encode()]
            chunks.append(b"data: [DONE]\n\n")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(sum(map(len, chunks))))
            self.end_headers()
            try:
                for chunk in chunks:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    time.sleep(0.05)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    provider = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    provider.daemon_threads = True
    thread = threading.Thread(target=provider.serve_forever, daemon=True)
    thread.start()
    home, workspace = tmp_path / "state", tmp_path / "work"
    home.mkdir()
    workspace.mkdir()
    (home / "config.yaml").write_text(
        "active_provider: custom\nmodel: test-model\ncompression:\n  enabled: false\n"
        "checkpoints:\n  enabled: false\nmemory:\n  memory_review_round: 0\n"
        "skills:\n  evolution_review_round: 0\n", encoding="utf-8")
    (home / ".env").write_text(
        f"MCLAW_BASE_URL=http://127.0.0.1:{provider.server_port}/v1\nMCLAW_API_KEY=local-test-credential\n",
        encoding="utf-8")
    process = subprocess.Popen([sys.executable, "-m", "mclaw.web.server", "--port", "0"],
                               env=dict(os.environ, MCLAW_HOME=str(home)),
                               cwd=Path(__file__).resolve().parents[1],
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    worker_pid = None
    websocket = None
    try:
        record = home / "web" / "service.json"
        deadline = time.monotonic() + 20
        while not record.exists():
            assert process.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.02)
        info = json.loads(record.read_text(encoding="utf-8"))

        def api(path, data=None):
            request = urllib.request.Request(
                f"http://127.0.0.1:{info['port']}/api/v1/{path}",
                data=json.dumps(data).encode() if data is not None else None,
                headers={"Authorization": f"Bearer {info['token']}", "Content-Type": "application/json",
                         "X-MClaw-Client": "shutdown-test"})
            with urllib.request.urlopen(request, timeout=3) as response:
                return json.load(response)

        sid = api("sessions", {"workspace": str(workspace)})["id"]
        api(f"sessions/{sid}/messages", {"content": "test cancellation", "request_id": "first"})
        lock = InteractiveSessionLock(sid, home / "sessions" / "locks")
        deadline = time.monotonic() + 20
        while True:
            state = api(f"sessions/{sid}")
            if state.get("pending_request"):
                api(f"sessions/{sid}/reply", {"request_id": state["pending_request"]["id"], "value": True})
            if any(message["role"] == "assistant" and message["content"] for message in state["messages"]):
                break
            assert time.monotonic() < deadline, state.get("status")
            time.sleep(0.02)
        worker_pid = json.loads(lock.path.read_text(encoding="utf-8"))["worker_pid"]
        if connection_mode not in {"completed", "cli"}:
            api(f"sessions/{sid}/stop", {})
        deadline = time.monotonic() + 20
        expected_state = "completed" if connection_mode == "completed" else "interrupted"
        while connection_mode != "cli" and api(f"sessions/{sid}")["status"] != expected_state:
            assert time.monotonic() < deadline
            time.sleep(0.02)
        # A socket that has no changing snapshots must still notice a browser
        # disconnect or Uvicorn shutdown and finish before lifespan cleanup.
        websocket = connect(
            f"ws://127.0.0.1:{info['port']}/api/v1/sessions/{sid}/events"
            f"?token={info['token']}&client=shutdown-test", close_timeout=3)
        assert json.loads(websocket.recv(timeout=3))["type"] == "snapshot"
        if connection_mode == "closed":
            websocket.close()
        elif connection_mode == "reset":
            websocket.socket.shutdown(socket.SHUT_RDWR)
            websocket.socket.close()
        if connection_mode == "cli":
            result = subprocess.run([sys.executable, "-m", "mclaw", "stop"],
                                    env=dict(os.environ, MCLAW_HOME=str(home), PYTHONIOENCODING="utf-8"),
                                    cwd=Path(__file__).resolve().parents[1],
                                    capture_output=True, text=True, encoding="utf-8", timeout=40)
            assert result.returncode == 0, result.stdout + result.stderr
            assert "服务已停止" in result.stdout
            assert info["token"] not in result.stdout
        else:
            api("service/stop", {})
        process.wait(timeout=15)
        assert process.returncode == 0
        assert not _is_process_alive(worker_pid)
        assert not record.exists()
        assert not lock.path.exists()
    finally:
        if websocket is not None:
            websocket.close()
        if worker_pid and _is_process_alive(worker_pid):
            os.kill(worker_pid, signal.SIGTERM)
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)
        provider.shutdown()
        provider.server_close()
        thread.join(timeout=3)
