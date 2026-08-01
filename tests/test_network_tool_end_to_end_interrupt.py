from __future__ import annotations

import importlib
import json
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from mclaw.agent.core import MClaw
from mclaw.skills_hub import (
    dependency_scan,
    install_service,
    security_scan,
    skill_store,
    source_resolver,
)
from mclaw.skills_hub.evolution_store import write_default
from mclaw.skills_hub.skill_yaml_store import build_skill_yaml, write_skill_yaml
from mclaw.tools import dispatch, web_extract_tool, web_search_tool
from mclaw.tools.interrupt import reset_interrupt_event, set_interrupt_event
from mclaw.tools.search import router, tavily_backend
from mclaw.tools.skill_tools import manage_tool, read_tools

skill_search_module = importlib.import_module("mclaw.skills_hub.search")


class _HalfResponseServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, body: bytes, content_type: str) -> None:
        super().__init__(("127.0.0.1", 0), _HalfResponseHandler)
        self.body = body
        self.content_type = content_type
        self.received = threading.Event()
        self.release = threading.Event()
        self.disconnected = threading.Event()
        self.request_count = 0
        self._request_lock = threading.Lock()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


class _HalfResponseHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        self._respond()

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", "0"))
        if length:
            self.rfile.read(length)
        self._respond()

    def _respond(self) -> None:
        server: _HalfResponseServer = self.server  # type: ignore[assignment]
        with server._request_lock:
            server.request_count += 1
        self.send_response(200)
        self.send_header("Content-Type", server.content_type)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        self._write_chunk(server.body)
        server.received.set()
        self.connection.settimeout(0.1)
        while not server.release.is_set():
            try:
                # Keep the response incomplete while forcing the peer to
                # observe a closed client transport on every platform.
                self._write_chunk(b" " * (64 * 1024))
                time.sleep(0.005)
            except OSError:
                server.disconnected.set()
                return

    def _write_chunk(self, chunk: bytes) -> None:
        self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii"))
        self.wfile.write(chunk)
        self.wfile.write(b"\r\n")
        self.wfile.flush()

    def log_message(self, *_args) -> None:
        pass


@contextmanager
def _half_response(body: bytes, content_type: str = "application/json"):
    server = _HalfResponseServer(body, content_type)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.release.set()
        server.shutdown()
        server.server_close()
        thread.join(1)


def _agent(name: str) -> MClaw:
    agent = MClaw.__new__(MClaw)
    agent.session_id = name
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
    agent._workspace_quarantine_key = f"test:{name}:{id(agent)}"
    return agent


def _run_and_cancel(agent, cancel_event, server, call):
    result: list[dict] = []
    errors: list[BaseException] = []

    def run() -> None:
        token = set_interrupt_event(cancel_event)
        try:
            result.append(json.loads(call()))
        except BaseException as exc:
            errors.append(exc)
        finally:
            reset_interrupt_event(token)

    worker = threading.Thread(target=run)
    worker.start()
    assert server.received.wait(5)
    agent.interrupt()
    worker.join(1)
    assert not worker.is_alive()
    assert errors == []
    assert result and result[0]["interrupted"] is True
    assert result[0]["status"] == "cancelled"
    assert server.disconnected.wait(2)
    assert agent._turn_workers_drained.wait(2)
    assert agent._outstanding_turn_workers == set()
    return result[0]


def _end_and_restart(agent, turn) -> None:
    agent.end_turn(turn)
    next_turn = agent.begin_turn(threading.Event())
    agent.end_turn(next_turn)


def _skill_manage_call(**arguments) -> dict:
    return {
        "id": "skill-manage-cancel",
        "type": "function",
        "function": {
            "name": "skill_manage",
            "arguments": json.dumps(arguments),
        },
    }


def _dispatch_skill_manage_and_cancel(agent, cancel_event, scan_started, **arguments) -> dict:
    # Real model turns discover the tool catalog before a tool call is emitted.
    # Keep that unrelated one-time import cost outside the cancellation window.
    dispatch._discover_tools()
    results: list[str] = []
    errors: list[BaseException] = []

    def run() -> None:
        try:
            results.extend(
                dispatch.handle_function_calls(
                    [_skill_manage_call(**arguments)],
                    {"skill_manage"},
                    parent_agent=agent,
                    cancel_event=cancel_event,
                )
            )
        except BaseException as exc:
            errors.append(exc)

    caller = threading.Thread(target=run)
    caller.start()
    assert scan_started.wait(5), "skill scan did not reach its blocking checkpoint"
    agent.interrupt()
    caller.join(2)
    assert not caller.is_alive()
    assert errors == []
    assert len(results) == 1
    payload = json.loads(results[0])
    assert payload["status"] == "cancelled"
    assert payload["interrupted"] is True
    assert agent._turn_workers_drained.wait(2)
    assert agent._outstanding_turn_workers == set()
    return payload


def _write_scan_package(package, *, name: str = "scan-heavy", files: int = 12) -> str:
    package.mkdir(parents=True, exist_ok=True)
    skill_md = (
        "---\n"
        f"name: {name}\n"
        "description: cancellation scan test\n"
        "---\n\n"
        f"# {name}\n\n"
        "Use these safe instructions while testing cancellation.\n"
    )
    (package / "SKILL.md").write_text(skill_md, encoding="utf-8")
    support = package / "references"
    support.mkdir()
    body = "safe instruction line without shell commands\n" * 128
    for index in range(files):
        (support / f"part_{index:03d}.md").write_text(body, encoding="utf-8")
    return skill_md


def _pause_security_scan(monkeypatch, cancel_event, scan_started) -> None:
    real_checkpoint = security_scan.cancellation_checkpoint
    checks = 0

    def observed_checkpoint(event) -> None:
        nonlocal checks
        if event is cancel_event:
            checks += 1
            # start + file + post-read + successive line checks prove that the
            # real scanner is inside its per-line scan loop.
            if checks == 6:
                scan_started.set()
                assert event.wait(3)
        real_checkpoint(event)

    monkeypatch.setattr(security_scan, "cancellation_checkpoint", observed_checkpoint)


@pytest.mark.parametrize("backend", ["tavily", "dashscope"])
def test_web_search_cancel_closes_provider_socket(backend, monkeypatch) -> None:
    with _half_response(b'{"output":{"choices":[{"message":{"content":"PARTIAL') as server:
        agent = _agent(f"web-search-{backend}")
        agent.config = {
            "auxiliary": {
                "web_search": {
                    "backend": backend,
                    "fallback": backend == "tavily",
                    "tavily_timeout": 30,
                    "dashscope_timeout": 30,
                    "base_url": f"{server.url}/api/v1",
                    "model": "qwen-plus",
                }
            }
        }
        monkeypatch.setattr(tavily_backend, "_TAVILY_API_URL", server.url)
        monkeypatch.setattr(router, "get_tavily_creds", lambda: {"api_key": "test"})
        fallback_calls = []

        def forbidden_fallback(**_kwargs):
            fallback_calls.append(True)
            raise AssertionError("cancelled Tavily request must not start fallback")

        if backend == "tavily":
            monkeypatch.setattr(router, "_dashscope_creds_ok", lambda **_kwargs: True)
            monkeypatch.setitem(router.BACKENDS, "dashscope", forbidden_fallback)
        monkeypatch.setattr(
            router,
            "resolve_dashscope_creds",
            lambda **_kwargs: {
                "api_key": "sk-dashscope-test",
                "base_url": f"{server.url}/api/v1",
                "model": "qwen-plus",
            },
        )
        cancel_event = threading.Event()
        turn = agent.begin_turn(cancel_event)
        payload = _run_and_cancel(
            agent,
            cancel_event,
            server,
            lambda: web_search_tool.web_search("cancel me", parent_agent=agent),
        )
        assert "PARTIAL" not in json.dumps(payload)
        assert fallback_calls == []
        assert server.request_count == 1
        _end_and_restart(agent, turn)


@pytest.mark.parametrize("backend", ["trafilatura", "tavily", "firecrawl"])
def test_web_extract_cancel_closes_backend_socket(backend, monkeypatch) -> None:
    content_type = "text/html" if backend == "trafilatura" else "application/json"
    with _half_response(b"<html>PARTIAL" if backend == "trafilatura" else b'{"PARTIAL":', content_type) as server:
        agent = _agent(f"web-extract-{backend}")
        agent.config = {
            "auxiliary": {
                "web_extract": {
                    "backend": backend,
                    "timeout": 30,
                    "firecrawl_api_url": server.url,
                }
            }
        }
        monkeypatch.setattr(web_extract_tool, "_is_safe_url", lambda _url: True)
        monkeypatch.setattr(web_extract_tool, "_url_error", lambda _url: "")
        monkeypatch.setattr(web_extract_tool, "_authorized_env_value", lambda _name: "test")
        monkeypatch.setattr(web_extract_tool, "_TAVILY_EXTRACT_URL", server.url)
        cancel_event = threading.Event()
        turn = agent.begin_turn(cancel_event)
        payload = _run_and_cancel(
            agent,
            cancel_event,
            server,
            lambda: web_extract_tool.web_extract(
                [
                    server.url if backend == "trafilatura" else "https://example.com/article",
                    "https://example.com/must-not-start",
                ],
                parent_agent=agent,
            ),
        )
        assert "PARTIAL" not in json.dumps(payload)
        assert server.request_count == 1
        _end_and_restart(agent, turn)


def test_skill_search_cancel_closes_skills_sh_socket(monkeypatch) -> None:
    with _half_response(b'{"skills":[{"id":"PARTIAL') as server:
        monkeypatch.setattr(skill_search_module, "SKILLS_SH_BASE_URL", server.url)
        agent = _agent("skill-search")
        cancel_event = threading.Event()
        turn = agent.begin_turn(cancel_event)
        payload = _run_and_cancel(
            agent,
            cancel_event,
            server,
            lambda: read_tools.skill_search("cancel me", parent_agent=agent),
        )
        assert "PARTIAL" not in json.dumps(payload)
        assert server.request_count == 1
        _end_and_restart(agent, turn)


def test_skill_manage_cancel_cleans_download_and_draft(tmp_path, monkeypatch) -> None:
    with _half_response(b"PK\x03\x04PARTIAL", "application/zip") as server:
        resolved = source_resolver.ResolvedSource(
            type="github",
            original="https://github.com/test/skill",
            fetch_url=server.url,
        )
        monkeypatch.setattr(install_service, "ensure_runtime_roots", lambda: None)
        monkeypatch.setattr(install_service, "get_skill_drafting_dir", lambda: tmp_path / "drafts")
        monkeypatch.setattr(install_service, "_drafting_id", lambda: "cancelled")
        monkeypatch.setattr(install_service, "resolve_source", lambda *_args, **_kwargs: resolved)
        real_mkstemp = source_resolver.tempfile.mkstemp
        monkeypatch.setattr(
            source_resolver.tempfile,
            "mkstemp",
            lambda **kwargs: real_mkstemp(dir=tmp_path, **kwargs),
        )
        agent = _agent("skill-manage")
        cancel_event = threading.Event()
        turn = agent.begin_turn(cancel_event)
        _run_and_cancel(
            agent,
            cancel_event,
            server,
            lambda: manage_tool.skill_manage(
                "install_prepare",
                source="https://github.com/test/skill",
                parent_agent=agent,
            ),
        )
        assert not (tmp_path / "drafts" / "cancelled").exists()
        assert list(tmp_path.glob("mclaw_skill_download_*.zip")) == []
        assert server.request_count == 1
        _end_and_restart(agent, turn)


@pytest.mark.parametrize("scanner", ["dependency", "security"])
def test_skill_manage_install_cancel_during_real_package_scan_cleans_and_reuses(
    scanner,
    tmp_path,
    monkeypatch,
) -> None:
    cancel_event = threading.Event()
    scan_started = threading.Event()
    drafting_root = tmp_path / "drafts"
    drafting_id = f"cancel-{scanner}-scan"
    resolved = source_resolver.ResolvedSource(
        type="github",
        original="https://github.com/test/scan-heavy",
        fetch_url="https://example.invalid/archive.zip",
    )

    monkeypatch.setattr(install_service, "ensure_runtime_roots", lambda: None)
    monkeypatch.setattr(install_service, "get_skill_drafting_dir", lambda: drafting_root)
    monkeypatch.setattr(install_service, "_drafting_id", lambda: drafting_id)
    monkeypatch.setattr(install_service, "resolve_source", lambda *_args, **_kwargs: resolved)
    monkeypatch.setattr(
        install_service,
        "materialize_source",
        lambda _resolved, target, **_kwargs: _write_scan_package(target),
    )

    if scanner == "dependency":
        real_iter = dependency_scan._iter_scan_files

        def observed_scan_files(root, cancel_event=None):
            for index, path in enumerate(real_iter(root, cancel_event=cancel_event)):
                if index == 5:
                    scan_started.set()
                    assert cancel_event is not None and cancel_event.wait(3)
                yield path

        monkeypatch.setattr(dependency_scan, "_iter_scan_files", observed_scan_files)
    else:
        _pause_security_scan(monkeypatch, cancel_event, scan_started)

    agent = _agent(f"skill-manage-{scanner}-scan")
    turn = agent.begin_turn(cancel_event)
    payload = _dispatch_skill_manage_and_cancel(
        agent,
        cancel_event,
        scan_started,
        action="install_prepare",
        source=resolved.original,
    )

    assert "scan-heavy" not in json.dumps(payload)
    assert not (drafting_root / drafting_id).exists()
    _end_and_restart(agent, turn)


def test_skill_manage_patch_cancel_during_real_security_scan_rolls_back_and_reuses(
    tmp_path,
    monkeypatch,
) -> None:
    cancel_event = threading.Event()
    scan_started = threading.Event()
    enabled_root = tmp_path / "enabled"
    package = enabled_root / "scan-heavy"
    original = _write_scan_package(package, name="scan-heavy")
    write_skill_yaml(
        package,
        build_skill_yaml(
            name="scan-heavy",
            short_description="中断扫描测试",
            source_type="agent_created",
            actor="main_agent",
            status="enabled",
        ),
    )
    write_default(package)
    monkeypatch.setattr(skill_store, "get_enabled_skills_dir", lambda: enabled_root)
    _pause_security_scan(monkeypatch, cancel_event, scan_started)

    agent = _agent("skill-manage-patch-scan")
    turn = agent.begin_turn(cancel_event)
    _dispatch_skill_manage_and_cancel(
        agent,
        cancel_event,
        scan_started,
        action="patch",
        name="scan-heavy",
        old_text="Use these safe instructions while testing cancellation.",
        new_text="Use these changed instructions while testing cancellation.",
    )

    assert (package / "SKILL.md").read_text(encoding="utf-8") == original
    assert not list(package.glob("*.tmp"))
    _end_and_restart(agent, turn)


def test_skill_manage_security_review_cancel_preserves_existing_review_and_reuses(
    tmp_path,
    monkeypatch,
) -> None:
    drafting_root = tmp_path / "drafts"
    drafting_id = "skill_drafting_20260722_180000_deadbeef"
    resolved = source_resolver.ResolvedSource(
        type="github",
        original="https://github.com/test/scan-heavy",
        fetch_url="https://example.invalid/archive.zip",
    )
    monkeypatch.setattr(install_service, "ensure_runtime_roots", lambda: None)
    monkeypatch.setattr(install_service, "get_skill_drafting_dir", lambda: drafting_root)
    monkeypatch.setattr(install_service, "_drafting_id", lambda: drafting_id)
    monkeypatch.setattr(install_service, "resolve_source", lambda *_args, **_kwargs: resolved)
    monkeypatch.setattr(
        install_service,
        "materialize_source",
        lambda _resolved, target, **_kwargs: _write_scan_package(target, files=1),
    )
    prepared = install_service.install_prepare(resolved.original)
    assert prepared["drafting_id"] == drafting_id
    review_path = drafting_root / drafting_id / "security_review.json"
    original_review = review_path.read_bytes()

    cancel_event = threading.Event()
    scan_started = threading.Event()
    _pause_security_scan(monkeypatch, cancel_event, scan_started)
    agent = _agent("skill-manage-security-review-scan")
    turn = agent.begin_turn(cancel_event)
    _dispatch_skill_manage_and_cancel(
        agent,
        cancel_event,
        scan_started,
        action="security_review",
        drafting_id=drafting_id,
    )

    assert review_path.read_bytes() == original_review
    assert (drafting_root / drafting_id).is_dir()
    _end_and_restart(agent, turn)


def test_skill_manage_validate_cancel_during_real_security_scan_reuses(
    tmp_path,
    monkeypatch,
) -> None:
    enabled_root = tmp_path / "enabled"
    package = enabled_root / "scan-heavy"
    original = _write_scan_package(package, name="scan-heavy", files=1)
    write_skill_yaml(
        package,
        build_skill_yaml(
            name="scan-heavy",
            short_description="中断扫描测试",
            source_type="agent_created",
            actor="main_agent",
            status="enabled",
        ),
    )
    write_default(package)
    monkeypatch.setattr(skill_store, "get_enabled_skills_dir", lambda: enabled_root)

    cancel_event = threading.Event()
    scan_started = threading.Event()
    _pause_security_scan(monkeypatch, cancel_event, scan_started)
    agent = _agent("skill-manage-validate-scan")
    turn = agent.begin_turn(cancel_event)
    _dispatch_skill_manage_and_cancel(
        agent,
        cancel_event,
        scan_started,
        action="validate",
        name="scan-heavy",
    )

    assert (package / "SKILL.md").read_text(encoding="utf-8") == original
    _end_and_restart(agent, turn)
