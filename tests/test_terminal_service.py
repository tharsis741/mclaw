# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import asyncio
import copy
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from starlette.testclient import TestClient

from mclaw.application.client import ApplicationClient, ServiceError
from mclaw.application.sessions import SessionService
from mclaw.cli.service_app import ServiceInteractiveChat
from mclaw.providers.registry import PROVIDER_REGISTRY
from mclaw.providers.runtime import ProviderRuntimeContext
from mclaw.web.app import create_app


class Runner:
    def __init__(self, session):
        self.session = session
        self.done = threading.Event()
        self.started = threading.Event()
        self.tasks = []
        self.commands = []
        self.closed = False

    async def run(self, task, emit):
        self.tasks.append(copy.deepcopy(task))
        self.started.set()
        emit({"type": "process", "kind": "progress", "id": "progress-" + task["id"], "content": "正在检查文件"})
        await asyncio.to_thread(self.done.wait)
        stopped = any(command["type"] == "stop" for command in self.commands)
        return {"completed": not stopped, "interrupted": stopped, "final_response": "检查完成",
                "token_usage": {"input_tokens": 17, "output_tokens": 5}, "api_calls": 1}

    def send(self, command):
        self.commands.append(command)
        if command["type"] in {"stop", "shutdown"}:
            self.done.set()

    async def close(self):
        self.closed = True
        self.done.set()


class Transport(httpx.BaseTransport):
    def __init__(self, web):
        self.web = web

    def handle_request(self, request):
        headers = dict(request.headers)
        headers["host"] = "127.0.0.1"
        response = self.web.request(request.method, request.url.raw_path.decode(), headers=headers, content=request.content)
        return httpx.Response(response.status_code, headers=response.headers, content=response.content, request=request)


@pytest.fixture
def service_env(tmp_path, monkeypatch):
    home, workspace = tmp_path / "home", tmp_path / "工作 目录"
    home.mkdir()
    workspace.mkdir()
    monkeypatch.setenv("MCLAW_HOME", str(home))
    from mclaw.cli.config import save_config
    save_config({"scheduler": {"enabled": False}, "pet": {"enabled": False}, "dsoftbus": {"enabled": False},
                 "channels": {"weixin": {"enabled": False}, "dingtalk": {"enabled": False}}})
    service = SessionService(home, runner_factory=Runner)
    app = create_app(home, "test-token", service=service)
    with TestClient(app, base_url="http://127.0.0.1") as web:
        clients = []

        def client(name):
            value = ApplicationClient({"port": 80, "token": "test-token"}, client_id=name, transport=Transport(web))
            clients.append(value)
            return value

        yield SimpleNamespace(service=service, client=client, workspace=str(workspace), home=home, web=web)
        for value in clients:
            value.close()


def context(model="model-a"):
    profile = PROVIDER_REGISTRY["openai"]
    return ProviderRuntimeContext(profile=profile, model=model, api_key="fixture-key", base_url=profile.base_url)


def wait_for(callback, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = callback()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("condition did not become true")


def make_ui(env, monkeypatch, name="tui:a", **kwargs):
    client = env.client(name)
    monkeypatch.setattr("mclaw.agent.core.MClaw", Mock(side_effect=AssertionError("frontend must not execute")))
    ui = ServiceInteractiveChat(service_client=client, provider_runtime=context(), workspace_path=env.workspace,
                                config={"pet": {"enabled": False}}, **kwargs)
    ui._render_response = Mock()
    ui._echo_user_message = Mock()
    ui._on_agent_event = Mock()
    ui._get_runtime_renderer = Mock(return_value=Mock())
    return ui


def test_terminal_and_web_exclusive_control_then_detach_keeps_task(service_env, monkeypatch):
    env = service_env
    ui = make_ui(env, monkeypatch)
    sid = ui.session_id
    assert ui._service_snapshot["source"] == "cli"
    assert not hasattr(ui.agent, "run_conversation")
    assert ui._submit_composer_text("检查这个目录")
    runner = wait_for(lambda: env.service.runners.get(sid))
    assert runner.started.wait(2)
    assert runner.tasks[0]["execution"]["platform"] == "cli"
    web = env.client("web:b")
    with pytest.raises(ServiceError, match="另一个"):
        web.session(sid)
    ui._shutdown_runtime_threads(None, None)
    ui._release_session_lock()
    assert not runner.closed
    assert not runner.commands
    assert web.session(sid)["active_task"]
    runner.done.set()
    state = wait_for(lambda: (value if (value := web.session(sid))["status"] == "completed" else None))
    assert state["messages"][-1]["content"] == "检查完成"
    assert state["messages"][-1]["result"]["completed"]


def test_two_frontends_keep_models_workspaces_and_queues_separate(service_env, monkeypatch, tmp_path):
    env = service_env
    ui = make_ui(env, monkeypatch)
    second_workspace = tmp_path / "second"
    second_workspace.mkdir()
    web = env.client("web:b")
    other = web.request("POST", "/sessions", {"workspace": str(second_workspace), "model": "provider-b::model-b"})
    ui._submit_composer_text("terminal one")
    ui._submit_composer_text("terminal two")
    web.action(other["id"], "messages", {"content": "web one", "request_id": "web-request"})
    first = wait_for(lambda: env.service.runners.get(ui.session_id))
    second = wait_for(lambda: env.service.runners.get(other["id"]))
    assert first.session["workspace"] == env.workspace
    assert second.session["workspace"] == str(second_workspace)
    assert first.tasks[0]["model"] == "openai::model-a"
    assert second.tasks[0]["model"] == "provider-b::model-b"
    ui.agent.interrupt()
    wait_for(lambda: ui.service_client.session(ui.session_id)["paused"])
    assert not second.commands
    assert len(ui.service_client.session(ui.session_id)["queue"]) == 1
    first.done.set()
    second.done.set()
    ui._release_session_lock()


def test_terminal_state_renders_process_and_final_once(service_env, monkeypatch):
    ui = make_ui(service_env, monkeypatch)
    ui._submit_composer_text("你好")
    runner = wait_for(lambda: service_env.service.runners.get(ui.session_id))
    assert runner.started.wait(2)
    ui._render_snapshot(ui.service_client.session(ui.session_id))
    assert ui._on_agent_event.call_args.args[0]["content"] == "正在检查文件"
    runner.done.set()
    state = wait_for(lambda: (value if (value := ui.service_client.session(ui.session_id))["status"] == "completed" else None))
    ui._render_snapshot(state)
    ui._render_snapshot(state)
    ui._echo_user_message.assert_called_once_with("你好")
    ui._render_response.assert_called_once_with("检查完成")
    assert ui.agent.session_input_tokens == 17
    ui._release_session_lock()


def test_queue_timeout_retry_uses_same_request_id(service_env, monkeypatch):
    ui = make_ui(service_env, monkeypatch)
    real_action = ui.service_client.action
    submitted = []

    def delayed(identifier, action, data=None):
        submitted.append(data["request_id"])
        state = real_action(identifier, action, data)
        if len(submitted) == 1:
            raise ServiceError("SERVICE_UNAVAILABLE", "lost reply")
        return state

    monkeypatch.setattr(ui.service_client, "action", delayed)
    assert ui._submit_composer_text("只提交一次") is False
    assert ui._submit_composer_text("只提交一次") is True
    assert submitted[0] == submitted[1]
    runner = wait_for(lambda: service_env.service.runners.get(ui.session_id))
    runner.done.set()
    assert len(ui.service_client.session(ui.session_id)["queue"]) == 0
    monkeypatch.setattr(ui.service_client, "action", real_action)
    ui._release_session_lock()


def test_scheduler_terminal_uses_same_store_and_rejects_lifecycle_rpc(service_env, monkeypatch):
    ui = make_ui(service_env, monkeypatch)
    wizard = ui._get_scheduler_coordinator()
    assert wizard.store.list_jobs(include_paused=True) == []
    assert wizard.engine.shutdown(cancel_running=True) is None
    with pytest.raises(ServiceError):
        ui.service_client.request("POST", "/terminal/scheduler", {"area": "engine", "operation": "shutdown"})
    ui._release_session_lock()


def test_terminal_history_is_redacted_and_reading_does_not_claim(service_env):
    env = service_env
    env.home.joinpath(".env").write_text("TEST_API_KEY=fixture-secret-0123456789\n", encoding="utf-8")
    env.service.history.create_session("old-session", "cli", model="old", workspace=env.workspace)
    env.service.history.append_message("old-session", "assistant", content="fixture-secret-0123456789")
    tui = env.client("tui:a")
    data = tui.request("GET", "/terminal/sessions/old-session")
    assert "fixture-secret" not in str(data)
    assert "old-session" not in env.service.owners
    assert env.client("web:b").session("old-session")["id"] == "old-session"


@pytest.mark.parametrize("exit_input", ["\x03", "/quit\r\r"])
def test_real_terminal_input_and_shortcuts_leave_backend_alive(service_env, monkeypatch, exit_input):
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    ui = make_ui(service_env, monkeypatch)
    ui._show_banner = Mock()
    errors = []
    with create_pipe_input() as pipe:
        def run():
            try:
                with create_app_session(input=pipe, output=DummyOutput()):
                    ui.run()
            except BaseException as exc:
                errors.append(exc)
        thread = threading.Thread(target=run)
        thread.start()
        try:
            wait_for(lambda: ui._app is not None and ui._app.is_running)
            pipe.send_text("你好\r")
            runner = wait_for(lambda: service_env.service.runners.get(ui.session_id))
            assert runner.started.wait(2)
            runner.done.set()
            wait_for(lambda: ui._render_response.called)
            wait_for(lambda: not ui._agent_running)
            pipe.send_text(exit_input)
            thread.join(8)
            assert not thread.is_alive(), (ui.runtime.should_exit, ui._agent_running, errors)
            assert not errors
            assert not runner.closed
            assert service_env.client("web:after").session(ui.session_id)["status"] == "completed"
        finally:
            if thread.is_alive():
                ui._app.loop.call_soon_threadsafe(ui._app.exit)
                thread.join(4)


def test_explicit_cli_credentials_are_session_scoped_and_never_persisted(service_env, monkeypatch):
    from dataclasses import replace

    ui = make_ui(service_env, monkeypatch)
    explicit = replace(context(), auth_source="explicit", api_key="unique-ephemeral-secret-98123")
    ui.agent.switch_model(explicit)
    ui._submit_composer_text("use explicit credential")
    runner = wait_for(lambda: service_env.service.runners.get(ui.session_id))
    assert runner.started.wait(2)
    assert runner.tasks[0]["_runtime_context"]["api_key"] == explicit.api_key
    assert explicit.api_key not in str(ui.service_client.session(ui.session_id))
    assert explicit.api_key not in str(service_env.service.store.get(ui.session_id))
    other = service_env.client("web:other").request("POST", "/sessions", {"workspace": service_env.workspace})
    assert "runtime_override_id" not in other
    runner.done.set()
    ui._release_session_lock()


def test_quit_during_terminal_task_detaches_without_stopping_it(service_env, monkeypatch):
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    ui = make_ui(service_env, monkeypatch)
    ui._show_banner = Mock()
    with create_pipe_input() as pipe:
        def run():
            with create_app_session(input=pipe, output=DummyOutput()):
                ui.run()
        thread = threading.Thread(target=run)
        thread.start()
        try:
            wait_for(lambda: ui._app is not None and ui._app.is_running)
            pipe.send_text("继续运行的任务\r")
            runner = wait_for(lambda: service_env.service.runners.get(ui.session_id))
            assert runner.started.wait(2)
            wait_for(lambda: ui._agent_running)
            pipe.send_text("/quit\r")
            thread.join(8)
            assert not thread.is_alive()
            assert not runner.commands and not runner.closed
            state = service_env.client("web:takeover").session(ui.session_id)
            assert state["active_task"] and not state["paused"]
            runner.done.set()
        finally:
            if thread.is_alive():
                ui._app.loop.call_soon_threadsafe(ui._app.exit)
                thread.join(4)


def test_switch_session_detaches_running_task_and_uses_new_workspace(service_env, monkeypatch, tmp_path):
    ui = make_ui(service_env, monkeypatch)
    original = ui.session_id
    ui._submit_composer_text("original")
    first = wait_for(lambda: service_env.service.runners.get(original))
    other_workspace = tmp_path / "other-work"
    other_workspace.mkdir()
    target = ui.service_client.attach(str(other_workspace), "openai::model-b")
    ui._apply_resume(target["id"], context("model-b"), [])
    assert ui.workspace_path == str(other_workspace)
    assert ui.model == "model-b"
    assert not first.commands
    assert service_env.client("web:old").session(original)["active_task"]
    ui._submit_composer_text("new")
    second = wait_for(lambda: service_env.service.runners.get(ui.session_id))
    assert second.tasks[0]["model"] == "openai::model-b"
    assert second.session["workspace"] == str(other_workspace)
    first.done.set()
    second.done.set()
    ui._release_session_lock()


def test_secret_confirmation_uses_existing_terminal_controls(service_env, monkeypatch):
    ui = make_ui(service_env, monkeypatch)
    sent = []
    monkeypatch.setattr(ui.service_client, "action", lambda _id, action, data: sent.append((action, data)) or ui._service_snapshot)
    ui._render_secret_request = Mock()
    request = {"id": "request-one", "kind": "secret", "required_for": "web_search",
               "needs": [{"env_var": "SEARCH_API_KEY", "state": "missing", "description": "Search key"}]}
    ui._present_request(request)
    ui._handle_secret_request_input("fixture-secret-do-not-log")
    assert sent[-1][0] == "reply"
    assert sent[-1][1]["value"]["values"] == {"SEARCH_API_KEY": "fixture-secret-do-not-log"}
    assert ui._pending_secret_request is None
    assert "fixture-secret-do-not-log" not in str(ui._service_snapshot)
    ui._attached_id = None


def test_real_worker_is_shared_by_terminal_and_web(tmp_path, monkeypatch, local_provider):
    from dataclasses import replace
    import yaml
    from mclaw.providers.resolver import resolve_provider_runtime_context

    port, requests = local_provider
    home, workspace = tmp_path / "home", tmp_path / "真实工作区"
    home.mkdir()
    workspace.mkdir()
    monkeypatch.setenv("MCLAW_HOME", str(home))
    config = {"active_provider": "custom", "model": "local-model", "compression": {"enabled": False},
              "checkpoints": {"enabled": False}, "memory": {"memory_review_round": 0},
              "skills": {"evolution_review_round": 0}, "scheduler": {"enabled": False}, "dsoftbus": {"enabled": False},
              "channels": {"weixin": {"enabled": False}, "dingtalk": {"enabled": False}}}
    home.joinpath("config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    provider = resolve_provider_runtime_context(provider="custom", model="local-model", config=config,
                                                api_key="ephemeral-test-key", base_url=f"http://127.0.0.1:{port}/v1")
    service = SessionService(home)
    with TestClient(create_app(home, "fixture-token", service=service), base_url="http://127.0.0.1") as web:
        client = ApplicationClient({"port": 80, "token": "fixture-token"}, client_id="tui:real", transport=Transport(web))
        ui = ServiceInteractiveChat(service_client=client, provider_runtime=provider, workspace_path=str(workspace), config=config)
        ui._get_runtime_renderer = Mock(return_value=Mock())
        ui._get_commands_renderer = Mock(return_value=Mock())
        try:
            assert ui._submit_composer_text("你好")
            pending = wait_for(lambda: client.session(ui.session_id).get("pending_request"), timeout=25)
            assert pending["kind"] == "workspace"
            ui._present_request(pending)
            ui._handle_skill_import_confirmation_input("y")
            first = wait_for(lambda: (state if (state := client.session(ui.session_id))["status"] == "completed" else None), timeout=25)
            pid = service.runners[ui.session_id].process.pid
            assert first["messages"][-1]["content"] == "本地测试回复"
            ui.agent.switch_model(replace(provider, model="next-model"))
            ui._submit_composer_text("接着做")
            second = wait_for(lambda: (state if (state := client.session(ui.session_id))["status"] == "completed" and len(state["messages"]) == 4 else None), timeout=25)
            assert requests[-1]["model"] == "next-model"
            assert service.runners[ui.session_id].process.pid == pid
            assert "ephemeral-test-key" not in str(service.store.get(ui.session_id))
            ui._update_usage(second)
            assert ui.agent.valid_tool_names
            # File management uses the same real worker, not a second frontend store.
            ui._file_command("checkpoints", "status")
            assert service.runners[ui.session_id].process.pid == pid
            second = client.session(ui.session_id)
            ui._release_session_lock()
            other = ApplicationClient({"port": 80, "token": "fixture-token"}, client_id="web:real", transport=Transport(web))
            try:
                assert other.session(ui.session_id)["messages"] == second["messages"]
            finally:
                other.close()
        finally:
            ui._shutdown_runtime_threads(None, None)
            ui._release_session_lock()
            client.close()


def test_backend_watcher_events_keep_paused_queue_and_deduplicate(service_env):
    client = service_env.client("tui:watcher")
    state = client.attach(service_env.workspace, "openai::model-a")
    client.action(state["id"], "stop")
    event = {"id": "process-finished", "notice": "后台进程已完成"}
    service_env.web.portal.call(service_env.service._worker_background, state["id"], event)
    service_env.web.portal.call(service_env.service._worker_background, state["id"], event)
    result = client.session(state["id"])
    assert result["paused"]
    assert len(result["queue"]) == 1
    assert result["queue"][0]["content"] == event["notice"]
    assert state["id"] not in service_env.service.runners


def test_idle_worker_pumps_owned_background_events_only_when_not_cancelled(tmp_path, monkeypatch):
    import queue
    from mclaw.application.worker import SessionWorker
    monkeypatch.setenv("MCLAW_HOME", str(tmp_path))
    events = []
    worker = SessionWorker(tmp_path, {"id": "worker", "workspace": str(tmp_path)}, events.append)
    registry = SimpleNamespace(pump_due_watchers=Mock(side_effect=OSError("fixture watcher failed")), completion_queue=queue.Queue())
    worker._processes = registry
    registry.completion_queue.put({"session_id": "process", "command": "echo test", "output": "done", "exit_code": 0})
    worker.cancel.set()
    worker._pump_background()
    assert not events
    worker.cancel.clear()
    worker._pump_background()
    assert events[0]["type"] == "background"
    assert "done" in events[0]["notice"]
    assert registry.completion_queue.empty()


def test_new_terminal_session_keeps_explicit_model_context(service_env, monkeypatch):
    from dataclasses import replace

    ui = make_ui(service_env, monkeypatch)
    explicit = replace(context(), auth_source="explicit", api_key="new-session-ephemeral-key")
    ui.agent.switch_model(explicit)
    previous = ui.session_id
    ui._new_session(clear_screen=False)
    assert ui.session_id != previous
    ui._submit_composer_text("use the same temporary model")
    runner = wait_for(lambda: service_env.service.runners.get(ui.session_id))
    assert runner.started.wait(2)
    assert runner.tasks[0]["_runtime_context"]["api_key"] == explicit.api_key
    assert explicit.api_key not in str(service_env.service.store.get(ui.session_id))
    runner.done.set()
    ui._release_session_lock()


def test_terminal_scheduler_typed_roundtrip_and_route_credentials(service_env):
    from mclaw.cli.service_scheduler import SchedulerClient
    from mclaw.application.scheduler_transport import encode, decode
    from mclaw.scheduler.models import SchedulerJob, SchedulerTarget

    client = service_env.client("tui:scheduler")
    store = SchedulerClient(client, "store")
    job = SchedulerJob.from_dict({"id": "terminal-test-job", "name": "fixture", "enabled": False,
                                  "prompt": "fixture only", "workdir": service_env.workspace})
    saved = store.create_job(job)
    assert isinstance(saved, SchedulerJob)
    assert store.get_job(saved.id).prompt == "fixture only"
    assert store.list_jobs(include_paused=True)[0].id == saved.id
    store.delete_job(saved.id)
    assert store.list_jobs(include_paused=True) == []
    target = SchedulerTarget("test", "dingtalk_private", "fixture", route_metadata={"sessionWebhook": "private-route-value"})
    public = encode(target, public=True)
    assert "private-route-value" not in str(public)
    assert decode(public).route_metadata == {}
    with pytest.raises(ServiceError) as caught:
        client.request("POST", "/terminal/scheduler", {"area": "store", "operation": "create_job",
                                                        "args": [{"$scheduler": "unknown", "value": {}}]})
    assert caught.value.code == "SCHEDULER_ACTION_INVALID"


@pytest.mark.parametrize("failure", ["connection", "authentication"])
def test_terminal_reconnects_to_restarted_backend_without_retrying_mutations(tmp_path, monkeypatch, failure):
    current = {"port": 12346, "token": "replacement"}
    calls = []
    def response(request):
        calls.append((request.method, request.url.port))
        if request.url.port == 12345:
            if failure == "connection":
                raise httpx.ConnectError("old service closed", request=request)
            return httpx.Response(403, json={"error": {"code": "UNAUTHORIZED"}})
        return httpx.Response(200, json={"id": "existing", "status": "interrupted"})
    monkeypatch.setattr("mclaw.web.launcher.running_service", lambda _home: current)
    client = ApplicationClient({"port": 12345, "token": "old"}, transport=httpx.MockTransport(response))
    client.home = tmp_path
    try:
        with pytest.raises(ServiceError):
            client.action("existing", "messages", {"content": "do not duplicate"})
        assert calls == [("POST", 12345)]
        assert client.session("existing")["status"] == "interrupted"
        assert calls[1:] == [("GET", 12345), ("GET", 12346)]
    finally:
        client.close()


# Shared local SSE fixture; no external provider or real credentials are used.
from test_application_worker import local_provider
