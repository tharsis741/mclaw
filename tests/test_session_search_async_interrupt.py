import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from mclaw.agent import auxiliary_client
from mclaw.providers.registry import PROVIDER_REGISTRY
from mclaw.providers.runtime import ProviderRuntimeContext
from mclaw.tools.interrupt import reset_interrupt_event, set_interrupt_event


class _ServerState:
    def __init__(self, api_mode: str, mode: str) -> None:
        self.api_mode = api_mode
        self.mode = mode
        self.started = threading.Event()
        self.disconnected = threading.Event()
        self.requests = 0
        self.path = ""
        self.headers = {}
        self.body = {}
        self.disconnect_error = None


class _TestHTTPServer(ThreadingHTTPServer):
    daemon_threads = True


def _start_server(api_mode: str, mode: str = "success"):
    state = _ServerState(api_mode, mode)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args) -> None:
            pass

        def do_POST(self) -> None:
            state.requests += 1
            state.path = self.path
            state.headers = {key.casefold(): value for key, value in self.headers.items()}
            size = int(self.headers.get("Content-Length", "0"))
            state.body = json.loads(self.rfile.read(size) or b"{}")
            if state.mode == "error":
                payload = b'{"error":{"message":"forced failure"}}'
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return

            if state.mode == "stall":
                prefix = (
                    b'{"choices":[{"message":{"content":"late'
                    if state.api_mode == "chat_completions"
                    else b'{"content":[{"type":"text","text":"late'
                )
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "100000000")
                self.end_headers()
                self.wfile.write(prefix)
                self.wfile.flush()
                state.started.set()
                time.sleep(0.05)
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    try:
                        self.wfile.write(b"x" * 65_536)
                        self.wfile.flush()
                        time.sleep(0.01)
                    except OSError as exc:
                        state.disconnect_error = exc
                        state.disconnected.set()
                        return
                return

            if state.mode == "stream_success":
                chunks = [
                    {
                        "id": "completion-1",
                        "object": "chat.completion.chunk",
                        "model": "test-model",
                        "choices": [{
                            "index": 0,
                            "delta": {"role": "assistant", "reasoning_content": "internal"},
                            "finish_reason": None,
                        }],
                    },
                    {
                        "id": "completion-1",
                        "object": "chat.completion.chunk",
                        "model": "test-model",
                        "choices": [{
                            "index": 0,
                            "delta": {"content": "normal summary"},
                            "finish_reason": "stop",
                        }],
                    },
                    {
                        "id": "completion-1",
                        "object": "chat.completion.chunk",
                        "model": "test-model",
                        "choices": [],
                        "usage": {
                            "prompt_tokens": 7,
                            "completion_tokens": 3,
                            "total_tokens": 10,
                        },
                    },
                ]
                payload = b"".join(
                    b"data: " + json.dumps(chunk).encode() + b"\n\n"
                    for chunk in chunks
                ) + b"data: [DONE]\n\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                state.started.set()
                return

            if state.api_mode == "chat_completions":
                response = {
                    "id": "completion-1",
                    "object": "chat.completion",
                    "model": "test-model",
                    "choices": [{
                        "index": 0,
                        "message": {"role": "assistant", "content": "normal summary"},
                        "finish_reason": "stop",
                    }],
                    "usage": {
                        "prompt_tokens": 7,
                        "completion_tokens": 3,
                        "total_tokens": 10,
                    },
                }
            else:
                response = {
                    "id": "message-1",
                    "type": "message",
                    "role": "assistant",
                    "model": "test-model",
                    "content": [{"type": "text", "text": "normal summary"}],
                    "stop_reason": "end_turn",
                    "stop_sequence": None,
                    "usage": {"input_tokens": 7, "output_tokens": 3},
                }
            payload = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            state.started.set()

    server = _TestHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, state


def _context(api_mode: str, port: int) -> ProviderRuntimeContext:
    profile = PROVIDER_REGISTRY[
        "openai" if api_mode == "chat_completions" else "anthropic"
    ]
    return ProviderRuntimeContext(
        profile=profile,
        model="gpt-4o-mini" if api_mode == "chat_completions" else "claude-3-haiku",
        api_key="test-secret",
        base_url=(
            f"http://127.0.0.1:{port}/v1"
            if api_mode == "chat_completions"
            else f"http://127.0.0.1:{port}"
        ),
    )


def _qwen_context(port: int) -> ProviderRuntimeContext:
    return ProviderRuntimeContext(
        profile=PROVIDER_REGISTRY["qwen"],
        model="qwen3.5-plus",
        api_key="test-secret",
        base_url=f"http://127.0.0.1:{port}/v1",
    )


class _Parent:
    def __init__(self, context: ProviderRuntimeContext) -> None:
        self.provider_runtime = context
        self.config = {}
        self.recorded = []
        self.workers = set()
        self.registered = []
        self.drained = threading.Event()
        self.drained.set()
        self._lock = threading.Lock()

    def _record_usage(self, usage) -> None:
        self.recorded.append(usage)

    def _register_turn_worker(self, worker) -> None:
        with self._lock:
            self.workers.add(worker)
            self.registered.append(worker)
            self.drained.clear()

    def _unregister_turn_worker(self, worker) -> None:
        with self._lock:
            self.workers.discard(worker)
            if not self.workers:
                self.drained.set()


def _close_server(server, thread) -> None:
    server.shutdown()
    server.server_close()
    thread.join(2)


@pytest.mark.parametrize("api_mode", ["chat_completions", "anthropic_messages"])
def test_auxiliary_async_provider_success_and_usage(monkeypatch, api_mode: str) -> None:
    server, thread, state = _start_server(api_mode)
    try:
        context = _context(api_mode, server.server_address[1])
        parent = _Parent(context)
        monkeypatch.setattr(
            auxiliary_client,
            "_resolve_auxiliary_runtime",
            lambda *_args, **_kwargs: (context, 2),
        )

        result = auxiliary_client.call_auxiliary_llm(
            "session_search",
            [{"role": "user", "content": "find this"}],
            parent_agent=parent,
        )

        assert result == "normal summary"
        assert state.requests == 1
        assert state.body["model"] == context.model
        assert parent.recorded[0].input_tokens == 7
        assert parent.recorded[0].output_tokens == 3
        assert parent.recorded[0].source == "auxiliary"
        assert parent.drained.wait(1)
        if api_mode == "chat_completions":
            assert state.path == "/v1/chat/completions"
            assert state.headers["authorization"] == "Bearer test-secret"
        else:
            assert state.path == "/v1/messages"
            assert state.headers["x-api-key"] == "test-secret"
    finally:
        _close_server(server, thread)


def test_auxiliary_openai_required_stream_keeps_summary_and_usage(monkeypatch) -> None:
    server, thread, state = _start_server("chat_completions", mode="stream_success")
    try:
        context = _qwen_context(server.server_address[1])
        parent = _Parent(context)
        monkeypatch.setattr(
            auxiliary_client,
            "_resolve_auxiliary_runtime",
            lambda *_args, **_kwargs: (context, 2),
        )

        assert auxiliary_client.call_auxiliary_llm(
            "session_search",
            [{"role": "user", "content": "find this"}],
            parent_agent=parent,
        ) == "normal summary"
        assert state.requests == 1
        assert state.body["stream"] is True
        assert state.body["stream_options"] == {"include_usage": True}
        assert parent.recorded[0].input_tokens == 7
        assert parent.recorded[0].output_tokens == 3
        assert parent.drained.wait(1)
    finally:
        _close_server(server, thread)


@pytest.mark.parametrize("api_mode", ["chat_completions", "anthropic_messages"])
def test_auxiliary_cancel_closes_socket_and_drains_real_task(monkeypatch, api_mode: str) -> None:
    server, thread, state = _start_server(api_mode, mode="stall")
    try:
        from mclaw.tools import dispatch

        dispatch._get_worker_loop()
        baseline_threads = set(threading.enumerate())
        context = _context(api_mode, server.server_address[1])
        parent = _Parent(context)
        cancel_event = threading.Event()
        outcome = {}
        monkeypatch.setattr(
            auxiliary_client,
            "_resolve_auxiliary_runtime",
            lambda *_args, **_kwargs: (context, 10),
        )

        def invoke() -> None:
            token = set_interrupt_event(cancel_event)
            try:
                outcome["value"] = auxiliary_client.call_auxiliary_llm(
                    "session_search",
                    [{"role": "user", "content": "find this"}],
                    parent_agent=parent,
                )
            except BaseException as exc:
                outcome["exception"] = exc
            finally:
                reset_interrupt_event(token)

        worker = threading.Thread(target=invoke)
        worker.start()
        assert state.started.wait(2)
        cancel_event.set()
        worker.join(2)

        assert not worker.is_alive()
        assert isinstance(outcome.get("exception"), InterruptedError)
        assert "value" not in outcome
        assert state.disconnected.wait(2)
        assert isinstance(state.disconnect_error, OSError)
        assert parent.drained.wait(2)
        assert parent.workers == set()
        assert parent.registered and all(not fence.is_alive() for fence in parent.registered)
        assert parent.recorded == []

        # Reuse the same provider immediately after the real Task and socket
        # drain; no result or usage from the cancelled request may arrive late.
        state.mode = "success"
        assert auxiliary_client.call_auxiliary_llm(
            "session_search",
            [{"role": "user", "content": "next turn"}],
            parent_agent=parent,
        ) == "normal summary"
        assert state.requests == 2
        assert len(parent.recorded) == 1
        assert parent.drained.wait(1)
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            if not [item for item in threading.enumerate() if item not in baseline_threads]:
                break
            time.sleep(0.01)
        assert [item for item in threading.enumerate() if item not in baseline_threads] == []
    finally:
        _close_server(server, thread)


def test_auxiliary_timeout_is_absolute_and_closes_socket(monkeypatch) -> None:
    server, thread, state = _start_server("chat_completions", mode="stall")
    try:
        context = _context("chat_completions", server.server_address[1])
        parent = _Parent(context)
        monkeypatch.setattr(
            auxiliary_client,
            "_resolve_auxiliary_runtime",
            lambda *_args, **_kwargs: (context, 0.2),
        )

        started = time.monotonic()
        with pytest.raises(TimeoutError):
            auxiliary_client.call_auxiliary_llm(
                "session_search",
                [{"role": "user", "content": "find this"}],
                parent_agent=parent,
            )

        assert time.monotonic() - started < 0.8
        assert state.disconnected.wait(2)
        assert isinstance(state.disconnect_error, OSError)
        assert parent.drained.wait(2)
        assert parent.recorded == []
    finally:
        _close_server(server, thread)


@pytest.mark.parametrize("api_mode", ["chat_completions", "anthropic_messages"])
def test_auxiliary_provider_errors_are_not_retried(monkeypatch, api_mode: str) -> None:
    server, thread, state = _start_server(api_mode, mode="error")
    try:
        context = _context(api_mode, server.server_address[1])
        parent = _Parent(context)
        monkeypatch.setattr(
            auxiliary_client,
            "_resolve_auxiliary_runtime",
            lambda *_args, **_kwargs: (context, 2),
        )

        with pytest.raises(Exception, match="forced failure"):
            auxiliary_client.call_auxiliary_llm(
                "session_search",
                [{"role": "user", "content": "find this"}],
                parent_agent=parent,
            )

        assert state.requests == 1
        assert parent.drained.wait(1)
        assert parent.recorded == []
    finally:
        _close_server(server, thread)
