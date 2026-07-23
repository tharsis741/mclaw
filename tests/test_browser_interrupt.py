from __future__ import annotations

import json
import logging
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from mclaw.agent.core import MClaw
from mclaw.tools import browser_tool
from mclaw.tools.browser_backend import BrowserBackend, BrowserOperationCancelled
from mclaw.tools.browser_requirements import check_browser_requirements
from mclaw.tools.interrupt import reset_interrupt_event, set_interrupt_event


def _bare_agent(session_id: str) -> MClaw:
    agent = MClaw.__new__(MClaw)
    agent.session_id = session_id
    agent._interrupted = False
    agent._interrupt_lock = threading.Lock()
    agent._turn_cancel_event = threading.Event()
    agent._turn_active = False
    agent._turn_abort_reason = None
    agent._workspace_abort_event = None
    agent._turn_workers_lock = threading.Lock()
    agent._outstanding_turn_workers = set()
    agent._turn_workers_drained = threading.Event()
    agent._turn_workers_drained.set()
    agent._turn_worker_parent = None
    agent._workspace_quarantine_key = f"test:browser:{session_id}:{id(agent)}"
    return agent


def _wait_until(predicate, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition was not reached before timeout")
        time.sleep(0.005)


def test_browser_cancel_skips_a_worker_task_that_has_not_started() -> None:
    backend = BrowserBackend()
    blocker_started = threading.Event()
    release_blocker = threading.Event()
    cancel_event = threading.Event()
    second_called = threading.Event()
    cancellation: list[Exception] = []

    def block_worker() -> None:
        blocker_started.set()
        release_blocker.wait(5)

    first = threading.Thread(target=backend._run_on_worker, args=(block_worker,))
    first.start()
    assert blocker_started.wait(1)

    def run_second() -> None:
        token = set_interrupt_event(cancel_event)
        try:
            backend._run_on_worker(second_called.set, cancel_session_id="queued-session")
        except BrowserOperationCancelled as exc:
            cancellation.append(exc)
        finally:
            reset_interrupt_event(token)

    second = threading.Thread(target=run_second)
    second.start()
    _wait_until(lambda: backend._task_queue.qsize() == 1)
    cancel_event.set()
    second.join(0.5)

    release_blocker.set()
    first.join(1)
    backend.stop()

    assert not second.is_alive()
    assert len(cancellation) == 1
    assert not second_called.is_set()


def test_browser_completed_result_wins_cancel_publication_race(monkeypatch) -> None:
    import mclaw.tools.browser_backend as browser_backend_module

    backend = BrowserBackend()
    worker_waiting_to_publish = threading.Event()
    caller_checked_completion = threading.Event()
    release_worker = threading.Event()

    class OrderedLock:
        def __init__(self) -> None:
            self._lock = threading.Lock()
            self._worker_acquires = 0

        def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
            if threading.get_ident() == backend._worker_thread_id:
                self._worker_acquires += 1
                if self._worker_acquires == 2:
                    worker_waiting_to_publish.set()
                    assert release_worker.wait(2)
            elif worker_waiting_to_publish.is_set():
                caller_checked_completion.set()
            if timeout == -1:
                return self._lock.acquire(blocking)
            return self._lock.acquire(blocking, timeout)

        def release(self) -> None:
            self._lock.release()

        def __enter__(self):
            self.acquire()
            return self

        def __exit__(self, *_args) -> None:
            self.release()

    original_task = browser_backend_module._WorkerTask

    def task_with_ordered_lock(*args, **kwargs):
        task = original_task(*args, **kwargs)
        task.completion_lock = OrderedLock()
        return task

    monkeypatch.setattr(browser_backend_module, "_WorkerTask", task_with_ordered_lock)
    cancel_event = threading.Event()
    expected = {"success": True, "effect": "committed"}
    outcome: list[object] = []

    def run_operation() -> None:
        token = set_interrupt_event(cancel_event)
        try:
            outcome.append(backend._run_on_worker(lambda: expected))
        except BaseException as exc:
            outcome.append(exc)
        finally:
            reset_interrupt_event(token)

    caller = threading.Thread(target=run_operation, name="browser-result-race")
    caller.start()
    try:
        assert worker_waiting_to_publish.wait(1)
        cancel_event.set()
        assert caller_checked_completion.wait(1)
        release_worker.set()
        caller.join(1)
        assert caller.is_alive() is False
        assert outcome == [expected]
    finally:
        release_worker.set()
        caller.join(1)
        backend.stop()


def test_browser_connection_abort_is_published_before_stop_submission(monkeypatch) -> None:
    import mclaw.tools.browser_backend as browser_backend_module

    backend = BrowserBackend()

    class Connection:
        async def stop_async(self) -> None:
            pass

    backend._browser = SimpleNamespace(
        _impl_obj=SimpleNamespace(_connection=Connection(), _loop=object())
    )
    task = browser_backend_module._WorkerTask(lambda: None, (), {}, object())
    task.phase = "closing_session"
    observed: list[bool] = []

    def submit(_loop, coro):
        observed.append(task.connection_aborted)
        coro.close()
        return object()

    monkeypatch.setattr(backend, "_submit_cancel_coro", submit)
    assert backend._request_connection_abort(task) is True
    assert observed == [True]
    assert task.connection_aborted is True

    def fail_submit(_loop, coro):
        observed.append(task.connection_aborted)
        coro.close()
        return None

    monkeypatch.setattr(backend, "_submit_cancel_coro", fail_submit)
    assert backend._request_connection_abort(task) is False
    assert observed == [True, True]
    assert task.connection_aborted is False


def test_browser_connection_abort_cannot_race_committed_worker_cleanup(monkeypatch) -> None:
    import mclaw.tools.browser_backend as browser_backend_module

    backend = BrowserBackend()
    stop_called = threading.Event()

    class Connection:
        async def stop_async(self) -> None:
            stop_called.set()

    backend._browser = SimpleNamespace(
        _impl_obj=SimpleNamespace(_connection=Connection(), _loop=object())
    )
    task = browser_backend_module._WorkerTask(
        lambda: None,
        (),
        {},
        SimpleNamespace(done=lambda: False),
    )
    with task.completion_lock:
        task.cancel_requested = True
        task.cleanup_committed = True
        task.phase = "closing_session"

    monkeypatch.setattr(
        backend,
        "_submit_cancel_coro",
        lambda _loop, coro: (coro.close(), object())[1],
    )

    assert backend._request_connection_abort(task) is False
    assert task.connection_aborted is False
    assert task.phase == "closing_session"
    assert stop_called.is_set() is False


def test_browser_worker_resets_if_connection_abort_wins_during_session_close(
    monkeypatch,
) -> None:
    from concurrent.futures import Future

    import mclaw.tools.browser_backend as browser_backend_module

    backend = BrowserBackend()
    operation_started = threading.Event()
    release_operation = threading.Event()
    close_started = threading.Event()
    release_close = threading.Event()
    reset_called = threading.Event()

    def operation() -> None:
        operation_started.set()
        assert release_operation.wait(1)

    def close_session(_session_id: str) -> None:
        close_started.set()
        assert release_close.wait(1)

    class Connection:
        async def stop_async(self) -> None:
            pass

    backend._browser = SimpleNamespace(
        _impl_obj=SimpleNamespace(_connection=Connection(), _loop=object())
    )
    monkeypatch.setattr(backend, "_close_session_impl", close_session)
    monkeypatch.setattr(backend, "_reset_aborted_browser_impl", reset_called.set)
    monkeypatch.setattr(
        backend,
        "_submit_cancel_coro",
        lambda _loop, coro: (coro.close(), object())[1],
    )
    future = Future()
    task = browser_backend_module._WorkerTask(
        operation,
        (),
        {},
        future,
        cancel_session_id="race-session",
    )
    backend._ensure_worker()
    backend._task_queue.put(task)
    try:
        assert operation_started.wait(1)
        with task.completion_lock:
            task.cancel_requested = True
        release_operation.set()
        assert close_started.wait(1)

        assert backend._request_connection_abort(task) is True
        release_close.set()
        with pytest.raises(BrowserOperationCancelled):
            future.result(timeout=1)

        assert reset_called.is_set()
        assert task.cleanup_committed is True
        assert task.phase == "finishing"
    finally:
        release_operation.set()
        release_close.set()
        backend._shutdown_worker()


def test_browser_cancel_closes_running_session_before_next_worker_task() -> None:
    backend = BrowserBackend()
    session_id = "running-session"
    operation_started = threading.Event()
    release_operation = threading.Event()
    cancel_event = threading.Event()
    cancellation: list[Exception] = []
    order: list[str] = []

    class Parent:
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

    class RecordingContext:
        def close(self) -> None:
            order.append("close")

    backend._sessions[session_id] = SimpleNamespace(context=RecordingContext())

    def operation() -> None:
        operation_started.set()
        release_operation.wait(5)
        order.append("operation")

    def run_operation() -> None:
        token = set_interrupt_event(cancel_event)
        try:
            backend._run_on_worker(
                operation,
                cancel_session_id=session_id,
            )
        except BrowserOperationCancelled as exc:
            cancellation.append(exc)
        finally:
            reset_interrupt_event(token)

    caller = threading.Thread(target=run_operation)
    caller.start()
    assert operation_started.wait(1)
    cancel_event.set()
    caller.join(0.8)
    browser_tool._browser_exception_result("browser_click", cancellation[0], parent)
    with parent.lock:
        assert len(parent.workers) == 1

    next_caller = threading.Thread(
        target=backend._run_on_worker,
        args=(lambda: order.append("next"),),
    )
    next_caller.start()
    time.sleep(0.05)
    assert "next" not in order

    release_operation.set()
    next_caller.join(1)
    backend.stop()

    assert not caller.is_alive()
    assert len(cancellation) == 1
    assert cancellation[0].completion_unknown is True
    assert order == ["operation", "close", "next"]
    assert session_id not in backend._sessions
    with parent.lock:
        assert parent.workers == set()


def test_browser_cancel_fence_lifecycle_survives_raising_logging_handlers() -> None:
    import mclaw.tools.browser_backend as browser_backend_module

    backend = BrowserBackend()
    session_id = "logging-failure-session"
    operation_started = threading.Event()
    release_operation = threading.Event()
    cancel_event = threading.Event()
    cancellation: list[BrowserOperationCancelled] = []
    session_closed = threading.Event()

    class RaisingHandler(logging.Handler):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def emit(self, _record) -> None:
            self.calls += 1
            raise RuntimeError("logging failed")

    class Parent:
        session_id = "browser-logging-parent"

        def __init__(self) -> None:
            self.workers = set()
            self.lock = threading.Lock()

        def _register_turn_worker(self, worker) -> None:
            with self.lock:
                self.workers.add(worker)

        def _unregister_turn_worker(self, worker) -> None:
            with self.lock:
                self.workers.discard(worker)

    class Context:
        def close(self) -> None:
            session_closed.set()

    parent = Parent()
    backend._sessions[session_id] = SimpleNamespace(context=Context())

    def operation() -> None:
        operation_started.set()
        release_operation.wait(2)

    def run_operation() -> None:
        token = set_interrupt_event(cancel_event)
        try:
            backend._run_on_worker(operation, cancel_session_id=session_id)
        except BrowserOperationCancelled as exc:
            cancellation.append(exc)
        finally:
            reset_interrupt_event(token)

    handler = RaisingHandler()
    loggers = (browser_backend_module.logger, browser_tool.logger)
    old_levels = [item.level for item in loggers]
    for item in loggers:
        item.setLevel(logging.INFO)
        item.addHandler(handler)
    caller = threading.Thread(target=run_operation)
    try:
        caller.start()
        assert operation_started.wait(1)
        cancel_event.set()
        caller.join(0.8)
        assert not caller.is_alive()
        assert len(cancellation) == 1
        assert cancellation[0].completion_unknown is True
        assert cancellation[0].fence is not None

        browser_tool._browser_exception_result(
            "browser_click",
            cancellation[0],
            parent,
        )
        with parent.lock:
            assert len(parent.workers) == 1

        release_operation.set()
        assert session_closed.wait(1)
        _wait_until(lambda: not parent.workers)
        assert backend._run_on_worker(lambda: "next") == "next"
        assert handler.calls >= 5
    finally:
        release_operation.set()
        caller.join(1)
        for item, old_level in zip(loggers, old_levels):
            item.removeHandler(handler)
            item.setLevel(old_level)
        backend.stop()


def test_browser_cancel_fence_blocks_real_agent_then_releases_with_trace_logs(
    caplog,
) -> None:
    caplog.set_level(logging.INFO)
    backend = BrowserBackend()
    agent = _bare_agent("browser-fence-agent")
    session_id = agent.session_id
    operation_started = threading.Event()
    release_operation = threading.Event()
    cancellation: list[BrowserOperationCancelled] = []

    class Context:
        def close(self) -> None:
            pass

    backend._sessions[session_id] = SimpleNamespace(context=Context())
    turn_event = agent.begin_turn()

    def operation() -> None:
        operation_started.set()
        release_operation.wait(2)

    def run_operation() -> None:
        token = set_interrupt_event(turn_event)
        try:
            backend._run_on_worker(operation, cancel_session_id=session_id)
        except BrowserOperationCancelled as exc:
            cancellation.append(exc)
        finally:
            reset_interrupt_event(token)

    caller = threading.Thread(target=run_operation)
    caller.start()
    assert operation_started.wait(1)
    agent.interrupt()
    caller.join(0.8)
    assert not caller.is_alive()
    result = json.loads(
        browser_tool._browser_exception_result(
            "browser_navigate",
            cancellation[0],
            agent,
        )
    )
    assert result["completion_unknown"] is True
    agent.end_turn(turn_event)

    with pytest.raises(RuntimeError, match="Browser operation.*still running"):
        agent.begin_turn()

    release_operation.set()
    _wait_until(lambda: not agent._has_outstanding_turn_workers())
    next_event = agent.begin_turn()
    agent.end_turn(next_event)
    backend.stop()

    messages = [record.getMessage() for record in caplog.records]
    cancel_id = getattr(turn_event, "_mclaw_cancel_id")
    for event_name in (
        "browser_cancel_detected",
        "browser_cancel_unresolved",
        "browser_fence_register",
        "browser_session_close",
        "browser_task_finished",
        "turn_blocked",
        "browser_fence_release",
        "quarantine_prune",
    ):
        assert any(event_name in message and cancel_id in message for message in messages)


def test_browser_tool_marks_cancellation_in_its_result(monkeypatch) -> None:
    session_id = "cancelled-tool-session"

    class CancelledBackend:
        def navigate_and_snapshot(self, _session_id: str, _url: str) -> None:
            raise BrowserOperationCancelled("Browser operation interrupted by user")

        def close_session(self, _session_id: str) -> None:
            pass

    session = browser_tool.BrowserSession(session_id, backend=CancelledBackend())
    monkeypatch.setattr(browser_tool, "_start_cleanup_if_needed", lambda: None)
    monkeypatch.setattr(browser_tool, "_resolve_session_id", lambda _parent=None: session_id)
    with browser_tool._sessions_lock:
        browser_tool._browser_sessions[session_id] = session
    try:
        result = json.loads(browser_tool.browser_navigate("https://example.test"))
    finally:
        with browser_tool._sessions_lock:
            browser_tool._browser_sessions.pop(session_id, None)

    assert result == {
        "error": "Browser operation interrupted by user",
        "success": False,
        "interrupted": True,
        "status": "cancelled",
    }


def test_browser_tool_reports_running_operation_as_completion_unknown() -> None:
    result = json.loads(
        browser_tool._browser_exception_result(
            "browser_click",
            BrowserOperationCancelled(
                "Browser cancellation requested; operation completion is unknown",
                completion_unknown=True,
            ),
        )
    )

    assert result["interrupted"] is True
    assert result["status"] == "cancel_requested"
    assert result["completion_unknown"] is True


def test_browser_worker_does_not_swallow_operation_timeout() -> None:
    backend = BrowserBackend()

    def fail() -> None:
        raise TimeoutError("operation timeout")

    try:
        with pytest.raises(TimeoutError, match="operation timeout"):
            backend._run_on_worker(fail)
    finally:
        backend.stop()


class _StallServer:
    def __init__(self) -> None:
        self.release = threading.Event()
        self.signals = {
            name: threading.Event()
            for name in ("navigate", "snapshot", "click", "type", "scroll", "press", "download")
        }
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def _reply(self, body: bytes, content_type: str = "text/html") -> None:
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
                name = self.path.rsplit("/", 1)[-1]
                signal = owner.signals.get(name)
                if signal is not None:
                    signal.set()
                self.send_response(204)
                self.end_headers()

            def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
                if self.path == "/navigate":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.end_headers()
                    self.wfile.write(b"<html><body>partial")
                    self.wfile.flush()
                    owner.signals["navigate"].set()
                    owner.release.wait(20)
                    return
                if self.path == "/download":
                    self.send_response(200)
                    self.send_header("Content-Disposition", "attachment; filename=blocked.bin")
                    self.send_header("Content-Length", "1000000")
                    self.end_headers()
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    owner.signals["download"].set()
                    owner.release.wait(20)
                    return
                if self.path == "/font":
                    self.send_response(200)
                    self.send_header("Content-Type", "font/woff2")
                    self.send_header("Content-Length", "1000000")
                    self.end_headers()
                    self.wfile.write(b"wOF2")
                    self.wfile.flush()
                    owner.release.wait(20)
                    return
                if self.path == "/font-page":
                    self._reply(
                        b"<style>@font-face{font-family:stall;src:url('/font')}"
                        b"body{font-family:stall}</style><h1>Screenshot stall</h1>"
                    )
                    return
                if self.path.startswith("/page/"):
                    name = self.path.rsplit("/", 1)[-1]
                    scripts = {
                        "snapshot": (
                            "Document.prototype.querySelectorAll=function(){"
                            "navigator.sendBeacon('/signal/snapshot');while(true){}}"
                        ),
                        "scroll": (
                            "Document.prototype.elementsFromPoint=function(){"
                            "navigator.sendBeacon('/signal/scroll');while(true){}}"
                        ),
                    }
                    handlers = {
                        "click": "onclick=\"navigator.sendBeacon('/signal/click');while(true){}\"",
                        "type": "oninput=\"navigator.sendBeacon('/signal/type');while(true){}\"",
                        "press": "onkeydown=\"navigator.sendBeacon('/signal/press');while(true){}\"",
                    }
                    body = (
                        "<html><head><title>Blocking operation</title></head>"
                        f"<body style='height:2000px'><script>{scripts.get(name, '')}</script>"
                        f"<button {handlers.get(name, '')}>Run</button>"
                        f"<input placeholder='Input' {handlers.get(name, '')}></body></html>"
                    ).encode()
                    self._reply(body)
                    return
                self.send_response(404)
                self.end_headers()

            def log_message(self, *_args) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def close(self) -> None:
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(1)


@pytest.fixture(scope="module")
def stall_server():
    server = _StallServer()
    yield server
    server.close()


@pytest.fixture(scope="module")
def real_browser_backend():
    backend = BrowserBackend()
    yield backend
    backend.stop()


def _element_ref(snapshot: str, element: str) -> str:
    match = re.search(rf"\[(e\d+)\] {element}", snapshot)
    assert match is not None
    return match.group(1)


@pytest.mark.skipif(
    not check_browser_requirements(),
    reason="Playwright Chromium is not installed",
)
@pytest.mark.parametrize(
    "operation",
    ["navigate", "snapshot", "click", "type", "scroll", "press", "screenshot", "download"],
)
def test_real_playwright_cancel_drains_each_browser_tool(
    operation,
    tmp_path,
    stall_server,
    real_browser_backend,
) -> None:
    backend = real_browser_backend
    session_id = f"real-cancel-{operation}"
    sentinel_id = f"real-sentinel-{operation}"
    started = stall_server.signals.get(operation, threading.Event())

    backend.navigate(sentinel_id, "data:text/html,<title>sentinel</title><p>still alive</p>")
    if operation == "navigate":
        invoke = lambda: backend.navigate(session_id, stall_server.base_url + "/navigate")
    else:
        page_url = "/font-page" if operation == "screenshot" else f"/page/{operation}"
        backend.navigate(session_id, stall_server.base_url + page_url)
        if operation == "snapshot":
            invoke = lambda: backend.snapshot(session_id)
        elif operation == "scroll":
            invoke = lambda: backend.scroll(session_id)
        elif operation == "download":
            invoke = lambda: backend.download(
                session_id,
                url=stall_server.base_url + "/download",
                path=str(tmp_path / "blocked.bin"),
            )
        elif operation == "screenshot":
            page = backend._sessions[session_id].page
            original_screenshot = page.screenshot

            def screenshot_with_signal(*args, **kwargs):
                started.set()
                return original_screenshot(*args, **kwargs)

            backend._run_on_worker(setattr, page, "screenshot", screenshot_with_signal)
            backend._sessions[session_id].needs_settle = False
            invoke = lambda: backend.screenshot(session_id, str(tmp_path / "blocked.png"))
        else:
            snapshot = backend.snapshot(session_id)["snapshot"]
            if operation == "click":
                ref = _element_ref(snapshot, "button")
                invoke = lambda: backend.click(session_id, ref)
            else:
                ref = _element_ref(snapshot, "text")
                if operation == "type":
                    invoke = lambda: backend.type_text(session_id, ref, "blocked")
                else:
                    assert backend.click(session_id, ref)["success"] is True
                    invoke = lambda: backend.press(session_id, "Enter")

    cancel_event = threading.Event()
    outcome: list[object] = []

    def run_operation() -> None:
        token = set_interrupt_event(cancel_event)
        try:
            outcome.append(invoke())
        except BaseException as exc:
            outcome.append(exc)
        finally:
            reset_interrupt_event(token)

    caller = threading.Thread(target=run_operation)
    caller.start()
    assert started.wait(5), f"real Playwright {operation} operation did not reach its blocking point"
    cancelled_at = time.monotonic()
    cancel_event.set()
    caller.join(1)
    elapsed = time.monotonic() - cancelled_at

    try:
        assert not caller.is_alive(), f"{operation} did not drain after cancellation"
        assert len(outcome) == 1
        assert isinstance(outcome[0], BrowserOperationCancelled)
        assert outcome[0].completion_unknown is False
        assert outcome[0].fence is None
        assert elapsed < 1
        assert session_id not in backend._sessions
        if operation == "download":
            assert not (tmp_path / "blocked.bin").exists()
        if operation == "screenshot":
            assert not (tmp_path / "blocked.png").exists()

        next_started = time.monotonic()
        sentinel = backend.snapshot(sentinel_id)
        assert time.monotonic() - next_started < 1
        assert "still alive" in sentinel["snapshot"]
    finally:
        backend.close_session(session_id)
        backend.close_session(sentinel_id)
