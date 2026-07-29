from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from mclaw.agent.core import MClaw
from mclaw.tools import dispatch, vision_tool, web_search_tool
from mclaw.tools.interrupt import reset_interrupt_event, set_interrupt_event
from mclaw.tools.search import router
from mclaw.tools.search.types import SearchResponse
from mclaw.tools.vision import image_io


class _ScenarioServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        body: bytes,
        *,
        content_type: str,
        status: int = 200,
        stall: bool = False,
        headers: dict[str, str] | None = None,
        include_content_length: bool = True,
    ) -> None:
        super().__init__(("127.0.0.1", 0), _ScenarioHandler)
        self.body = body
        self.content_type = content_type
        self.status = status
        self.stall = stall
        self.headers = headers or {}
        self.include_content_length = include_content_length
        self.received = threading.Event()
        self.release = threading.Event()
        self.disconnected = threading.Event()
        self.request_count = 0

    @property
    def url(self) -> str:
        host, port = self.server_address
        return f"http://{host}:{port}"


class _ScenarioHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        self._respond()

    def do_POST(self) -> None:
        content_length = int(self.headers.get("content-length", "0"))
        if content_length:
            self.rfile.read(content_length)
        self._respond()

    def _respond(self) -> None:
        server: _ScenarioServer = self.server  # type: ignore[assignment]
        server.request_count += 1
        self.send_response(server.status)
        self.send_header("Content-Type", server.content_type)
        declared_length = len(server.body) + (4096 if server.stall else 0)
        if server.include_content_length:
            self.send_header("Content-Length", str(declared_length))
        else:
            self.send_header("Connection", "close")
            self.close_connection = True
        for name, value in server.headers.items():
            self.send_header(name, value)
        self.end_headers()
        try:
            self.wfile.write(server.body)
            self.wfile.flush()
        except OSError:
            server.disconnected.set()
            return
        server.received.set()
        if not server.stall:
            return

        self.connection.settimeout(0.05)
        while not server.release.is_set():
            try:
                if not self.connection.recv(1):
                    server.disconnected.set()
                    return
            except socket.timeout:
                continue
            except OSError:
                server.disconnected.set()
                return

    def log_message(self, _format: str, *_args) -> None:
        pass


@contextmanager
def _http_scenario(
    body: bytes,
    *,
    content_type: str,
    status: int = 200,
    stall: bool = False,
    headers: dict[str, str] | None = None,
    include_content_length: bool = True,
):
    server = _ScenarioServer(
        body,
        content_type=content_type,
        status=status,
        stall=stall,
        headers=headers,
        include_content_length=include_content_length,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.release.set()
        server.shutdown()
        server.server_close()
        thread.join(1)


def _bare_agent(session_id: str) -> MClaw:
    agent = MClaw.__new__(MClaw)
    agent.session_id = session_id
    agent.config = {}
    agent._interrupted = False
    agent._interrupt_lock = threading.Lock()
    agent._turn_cancel_event = threading.Event()
    agent._turn_active = False
    agent._turn_abort_reason = None
    agent._turn_workers_lock = threading.Lock()
    agent._outstanding_turn_workers = set()
    agent._turn_workers_drained = threading.Event()
    agent._turn_workers_drained.set()
    agent._turn_worker_parent = None
    agent._workspace_quarantine_key = f"test:{session_id}:{id(agent)}"
    return agent


def _configure_local_vision(monkeypatch, image, base_url: str, timeout: float) -> None:
    _bypass_test_proxy(monkeypatch)
    image.write_bytes(b"image")
    monkeypatch.setattr(vision_tool, "_detect_image_mime_type", lambda _path: "image/png")
    monkeypatch.setattr(vision_tool, "_compress_image_if_needed", lambda path: path)
    monkeypatch.setattr(
        vision_tool,
        "_image_to_base64_data_url",
        lambda _path, mime_type: f"data:{mime_type};base64,aW1hZ2U=",
    )
    monkeypatch.setattr(
        vision_tool,
        "resolve_vision_credentials",
        lambda **_kwargs: SimpleNamespace(
            model="vision-test",
            api_key="test",
            base_url=f"{base_url}/v1",
            provider="qwen",
            unsupported_reason="",
        ),
    )
    monkeypatch.setattr(vision_tool, "_resolve_timeout", lambda _parent: timeout)


def _bypass_test_proxy(monkeypatch) -> None:
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")


def test_web_search_does_not_route_when_turn_is_already_cancelled(monkeypatch) -> None:
    cancel_event = threading.Event()
    cancel_event.set()
    called = False

    def fail_if_called(**_kwargs) -> dict:
        nonlocal called
        called = True
        return {"success": True}

    monkeypatch.setattr(router, "execute_search", fail_if_called)
    token = set_interrupt_event(cancel_event)
    try:
        result = json.loads(web_search_tool.web_search("cancelled query"))
    finally:
        reset_interrupt_event(token)

    assert called is False
    assert result["interrupted"] is True
    assert result["status"] == "cancelled"


def test_web_search_does_not_start_fallback_after_primary_returns_cancelled(monkeypatch) -> None:
    cancel_event = threading.Event()
    calls: list[str] = []
    fallback_check_called = False
    config = SimpleNamespace(
        backend="tavily",
        fallback=True,
        timeout_for_backend=lambda _backend, _strategy: 1,
    )

    monkeypatch.setattr(router, "load_search_config", lambda **_kwargs: config)
    monkeypatch.setattr(router, "_effective_backend", lambda *_args, **_kwargs: "tavily")
    monkeypatch.setattr(router, "get_tavily_creds", lambda: {"api_key": "test"})

    def fallback_available(**_kwargs) -> bool:
        nonlocal fallback_check_called
        fallback_check_called = True
        return True

    def invoke(backend_name, *_args, **_kwargs) -> SearchResponse:
        calls.append(backend_name)
        cancel_event.set()
        return SearchResponse(success=False, error="primary failed", backend=backend_name)

    monkeypatch.setattr(router, "_dashscope_creds_ok", fallback_available)
    monkeypatch.setattr(router, "_invoke_backend", invoke)

    result = router.execute_search("query", cancel_event=cancel_event)

    assert calls == ["tavily"]
    assert fallback_check_called is False
    assert result["interrupted"] is True
    assert result["status"] == "cancelled"


def test_image_download_retry_wait_is_woken_by_cancellation(tmp_path, monkeypatch) -> None:
    import httpx

    cancel_event = threading.Event()
    first_attempt = threading.Event()
    errors: list[Exception] = []
    attempts = 0

    class FailingClient:
        def __init__(self, **_kwargs) -> None:
            pass

        async def __aenter__(self):
            nonlocal attempts
            attempts += 1
            first_attempt.set()
            raise RuntimeError("network failed")

        async def __aexit__(self, *_args) -> None:
            pass

    monkeypatch.setattr(httpx, "AsyncClient", FailingClient)

    def download() -> None:
        try:
            asyncio.run(
                image_io._download_image_async(
                    "https://example.test/image.png",
                    tmp_path / "image.png",
                    cancel_event=cancel_event,
                )
            )
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=download)
    started = time.monotonic()
    worker.start()
    assert first_attempt.wait(1)
    time.sleep(0.05)
    cancel_event.set()
    worker.join(0.5)

    assert not worker.is_alive()
    assert time.monotonic() - started < 0.6
    assert attempts == 1
    assert len(errors) == 1
    assert isinstance(errors[0], InterruptedError)


def test_image_download_blocks_unsafe_redirect_before_request(
    tmp_path,
    monkeypatch,
) -> None:
    _bypass_test_proxy(monkeypatch)
    monkeypatch.setattr(image_io, "resolve_download_timeout", lambda _parent: 1)
    destination = tmp_path / "redirected.png"

    with _http_scenario(b"target", content_type="image/png") as target:
        with _http_scenario(
            b"",
            content_type="text/plain",
            status=302,
            headers={"Location": f"{target.url}/private.png"},
        ) as source:
            monkeypatch.setattr(
                image_io,
                "_is_safe_url",
                lambda url: str(url).startswith(source.url),
            )
            with pytest.raises(ValueError, match="private or internal"):
                asyncio.run(image_io._download_image_async(
                    f"{source.url}/image.png",
                    destination,
                    max_retries=3,
                ))

            assert source.request_count == 1
            assert target.request_count == 0
            assert not destination.exists()


def test_url_safety_ignores_disabled_ipv6_artifacts_but_blocks_private_ipv4(
    monkeypatch,
) -> None:
    def getaddrinfo(hostname, *_args, **_kwargs):
        ipv4 = "125.73.212.153" if hostname == "public.example" else "10.0.0.1"
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ipv4, 0)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", (10, b"invalid-ipv6")),
        ]

    monkeypatch.setattr(image_io.socket, "has_ipv6", False)
    monkeypatch.setattr(image_io.socket, "getaddrinfo", getaddrinfo)

    assert image_io._is_safe_url("https://public.example/")
    assert not image_io._is_safe_url("https://private.example/")


def test_image_download_stream_size_cap_removes_partial_file(
    tmp_path,
    monkeypatch,
) -> None:
    _bypass_test_proxy(monkeypatch)
    monkeypatch.setattr(image_io, "_is_safe_url", lambda _url: True)
    monkeypatch.setattr(image_io, "resolve_download_timeout", lambda _parent: 1)
    destination = tmp_path / "oversized.png"

    with _http_scenario(
        b"x" * 32,
        content_type="image/png",
        include_content_length=False,
    ) as server:
        with pytest.raises(ValueError, match="Image too large"):
            asyncio.run(image_io._download_image_async(
                f"{server.url}/image.png",
                destination,
                max_retries=1,
                max_bytes=8,
            ))

        assert server.request_count == 1
        assert not destination.exists()


def test_vision_does_not_download_when_turn_is_already_cancelled(monkeypatch) -> None:
    cancel_event = threading.Event()
    cancel_event.set()
    downloaded = False

    async def fail_download(*_args, **_kwargs) -> None:
        nonlocal downloaded
        downloaded = True

    monkeypatch.setattr(vision_tool, "_download_image_async", fail_download)
    token = set_interrupt_event(cancel_event)
    try:
        result = json.loads(
            vision_tool.vision_analyze("https://example.test/image.png", "describe")
        )
    finally:
        reset_interrupt_event(token)

    assert downloaded is False
    assert result["interrupted"] is True
    assert result["status"] == "cancelled"


def test_vision_cancel_after_download_skips_processing_and_cleans_temp(monkeypatch) -> None:
    cancel_event = threading.Event()
    downloaded: list = []
    processing_called = False

    async def download(_url, destination, **_kwargs):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"image")
        downloaded.append(destination)
        cancel_event.set()
        return destination

    def fail_processing(_path):
        nonlocal processing_called
        processing_called = True
        raise AssertionError("processing started after cancellation")

    monkeypatch.setattr(vision_tool, "_download_image_async", download)
    monkeypatch.setattr(vision_tool, "_compress_image_if_needed", fail_processing)

    token = set_interrupt_event(cancel_event)
    try:
        result = json.loads(
            vision_tool.vision_analyze("https://example.test/image.png", "describe")
        )
    finally:
        reset_interrupt_event(token)

    assert result["interrupted"] is True
    assert result["status"] == "cancelled"
    assert processing_called is False
    assert downloaded and not downloaded[0].exists()


def test_vision_download_completion_race_cleans_temp(tmp_path, monkeypatch) -> None:
    cancel_event = threading.Event()
    downloaded = []

    async def download(_url, destination, **_kwargs):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"complete")
        downloaded.append(destination)
        return destination

    def cancel_before_proxy_result(coro, **_kwargs):
        asyncio.run(coro)
        cancel_event.set()
        raise image_io.VisionOperationCancelled("cancel won proxy propagation")

    monkeypatch.setattr(vision_tool.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(vision_tool, "_download_image_async", download)
    monkeypatch.setattr(vision_tool, "_run_vision_io", cancel_before_proxy_result)

    token = set_interrupt_event(cancel_event)
    try:
        result = json.loads(
            vision_tool.vision_analyze("https://example.test/image.png", "describe")
        )
    finally:
        reset_interrupt_event(token)

    assert result["status"] == "cancelled"
    assert downloaded and not downloaded[0].exists()


@pytest.mark.parametrize("trigger", ["event", "deadline"])
def test_vision_download_landing_after_outer_cleanup_removes_temp(
    trigger,
    tmp_path,
    monkeypatch,
    caplog,
) -> None:
    cancel_event = threading.Event()
    download_started = threading.Event()
    release_download = threading.Event()
    downloaded = []
    task_threads = []
    task_errors = []

    async def download(_url, destination, **_kwargs):
        download_started.set()
        while not release_download.is_set():
            await asyncio.sleep(0.001)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"late complete")
        downloaded.append(destination)
        return destination

    def abandon_while_task_is_alive(coro, **_kwargs):
        def run_task():
            try:
                asyncio.run(coro)
            except BaseException as exc:
                task_errors.append(exc)

        task = threading.Thread(target=run_task, daemon=True)
        task_threads.append(task)
        task.start()
        assert download_started.wait(1)
        if trigger == "event":
            cancel_event.set()
            raise InterruptedError("event cancellation")
        raise TimeoutError("bridge deadline")

    monkeypatch.setattr(vision_tool.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(vision_tool, "_download_image_async", download)
    monkeypatch.setattr(dispatch, "_run_async", abandon_while_task_is_alive)
    caplog.set_level("INFO", logger=vision_tool.__name__)

    token = set_interrupt_event(cancel_event)
    try:
        result = json.loads(
            vision_tool.vision_analyze("https://example.test/image.png", "describe")
        )
        if trigger == "event":
            assert result["status"] == "cancelled"
        else:
            assert result["success"] is False
            assert "bridge deadline" in result["error"]
        assert downloaded == []
    finally:
        reset_interrupt_event(token)
        release_download.set()
        for task in task_threads:
            task.join(1)

    assert not task_threads[0].is_alive()
    assert task_errors == []
    assert downloaded and not downloaded[0].exists()
    traces = "\n".join(record.getMessage() for record in caplog.records)
    assert "[CANCEL_TRACE]" in traces
    assert f"trigger={trigger}" in traces


def test_vision_cancel_after_model_response_skips_empty_response_retry(tmp_path, monkeypatch) -> None:
    image = tmp_path / "image.png"
    image.write_bytes(b"image")
    cancel_event = threading.Event()
    model_calls = 0

    monkeypatch.setattr(vision_tool, "_detect_image_mime_type", lambda _path: "image/png")
    monkeypatch.setattr(vision_tool, "_compress_image_if_needed", lambda path: path)
    monkeypatch.setattr(
        vision_tool,
        "_image_to_base64_data_url",
        lambda _path, mime_type: f"data:{mime_type};base64,aW1hZ2U=",
    )
    monkeypatch.setattr(
        vision_tool,
        "resolve_vision_credentials",
        lambda **_kwargs: SimpleNamespace(
            model="vision-test",
            api_key="test",
            base_url="https://example.test/v1",
            provider="qwen",
            unsupported_reason="",
        ),
    )
    monkeypatch.setattr(vision_tool, "_resolve_timeout", lambda _parent: 1)

    async def call_model(*_args, **_kwargs) -> str:
        nonlocal model_calls
        model_calls += 1
        cancel_event.set()
        return ""

    monkeypatch.setattr(vision_tool, "_call_vision_llm", call_model)

    token = set_interrupt_event(cancel_event)
    try:
        result = json.loads(vision_tool.vision_analyze(str(image), "describe"))
    finally:
        reset_interrupt_event(token)

    assert model_calls == 1
    assert result["interrupted"] is True
    assert result["status"] == "cancelled"
    assert image.exists()


def test_vision_model_http_cancel_discards_partial_and_releases_next_turn(
    tmp_path,
    monkeypatch,
) -> None:
    partial = b'{"id":"partial","choices":[{"message":{"content":"PARTIAL'
    with _http_scenario(
        partial,
        content_type="application/json",
        stall=True,
    ) as server:
        image = tmp_path / "image.png"
        _configure_local_vision(monkeypatch, image, server.url, timeout=30)
        agent = _bare_agent("vision-model-cancel")
        cancel_event = threading.Event()
        turn_event = agent.begin_turn(cancel_event)
        results: list[dict] = []
        errors: list[BaseException] = []

        def run() -> None:
            token = set_interrupt_event(cancel_event)
            try:
                results.append(json.loads(vision_tool.vision_analyze(
                    str(image),
                    "describe",
                    parent_agent=agent,
                )))
            except BaseException as exc:
                errors.append(exc)
            finally:
                reset_interrupt_event(token)

        worker = threading.Thread(target=run)
        worker.start()
        assert server.received.wait(5)
        started = time.monotonic()
        agent.interrupt()
        worker.join(1)

        assert not worker.is_alive()
        assert time.monotonic() - started < 1
        assert errors == []
        assert len(results) == 1
        assert results[0]["interrupted"] is True
        assert results[0]["status"] == "cancelled"
        assert "analysis" not in results[0]
        assert "PARTIAL" not in json.dumps(results[0])
        assert agent._turn_workers_drained.wait(1)

        agent.end_turn(turn_event)
        next_event = agent.begin_turn()
        agent.end_turn(next_event)


def test_vision_remote_download_cancel_removes_partial_and_skips_model(
    tmp_path,
    monkeypatch,
) -> None:
    image_prefix = b"\x89PNG\r\n\x1a\n" + (b"x" * 1024)
    with _http_scenario(
        image_prefix,
        content_type="image/png",
        stall=True,
    ) as server:
        _bypass_test_proxy(monkeypatch)
        monkeypatch.setattr(image_io, "_is_safe_url", lambda _url: True)
        monkeypatch.setattr(vision_tool.tempfile, "gettempdir", lambda: str(tmp_path))
        model_calls = 0

        async def fail_model(*_args, **_kwargs) -> str:
            nonlocal model_calls
            model_calls += 1
            raise AssertionError("model started after download cancellation")

        monkeypatch.setattr(vision_tool, "_call_vision_llm", fail_model)
        agent = _bare_agent("vision-download-cancel")
        agent.config = {"auxiliary": {"vision": {"download_timeout": 30}}}
        cancel_event = threading.Event()
        turn_event = agent.begin_turn(cancel_event)
        results: list[dict] = []

        def run() -> None:
            token = set_interrupt_event(cancel_event)
            try:
                results.append(json.loads(vision_tool.vision_analyze(
                    f"{server.url}/image.png",
                    "describe",
                    parent_agent=agent,
                )))
            finally:
                reset_interrupt_event(token)

        worker = threading.Thread(target=run)
        worker.start()
        assert server.received.wait(5)
        temp_dir = tmp_path / "mclaw-vision"
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and not list(temp_dir.glob("temp_image_*.jpg")):
            time.sleep(0.01)
        assert list(temp_dir.glob("temp_image_*.jpg"))

        agent.interrupt()
        worker.join(1)

        assert not worker.is_alive()
        assert len(results) == 1
        assert results[0]["interrupted"] is True
        assert model_calls == 0
        assert agent._turn_workers_drained.wait(1)
        assert list(temp_dir.glob("temp_image_*.jpg")) == []

        agent.end_turn(turn_event)
        next_event = agent.begin_turn()
        agent.end_turn(next_event)


def test_vision_model_deadline_is_absolute_and_has_no_sdk_retry(
    tmp_path,
    monkeypatch,
) -> None:
    partial = b'{"id":"partial"'
    with _http_scenario(
        partial,
        content_type="application/json",
        stall=True,
    ) as server:
        image = tmp_path / "image.png"
        _configure_local_vision(monkeypatch, image, server.url, timeout=1)
        cancel_event = threading.Event()
        token = set_interrupt_event(cancel_event)
        started = time.monotonic()
        try:
            result = json.loads(vision_tool.vision_analyze(str(image), "describe"))
        finally:
            reset_interrupt_event(token)

        assert time.monotonic() - started < 2
        assert server.received.is_set()
        assert server.request_count == 1
        assert result["success"] is False
        assert result.get("interrupted") is not True


def test_vision_complete_response_wins(tmp_path, monkeypatch) -> None:
    body = json.dumps({
        "id": "complete",
        "object": "chat.completion",
        "created": 0,
        "model": "vision-test",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "complete result"},
            "finish_reason": "stop",
        }],
    }).encode()
    with _http_scenario(body, content_type="application/json") as server:
        image = tmp_path / "image.png"
        _configure_local_vision(monkeypatch, image, server.url, timeout=2)
        token = set_interrupt_event(threading.Event())
        try:
            result = json.loads(vision_tool.vision_analyze(str(image), "describe"))
        finally:
            reset_interrupt_event(token)

        assert server.request_count == 1
        assert result == {"success": True, "analysis": "complete result"}


def test_qwen_international_uses_the_qwen_vision_client() -> None:
    from mclaw.tools.vision.client import get_vision_client

    assert get_vision_client("qwen-intl") is get_vision_client("qwen")
