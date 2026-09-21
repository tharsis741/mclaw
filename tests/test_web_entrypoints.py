# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from types import SimpleNamespace

import pytest

from mclaw.cli import main as entry
from mclaw.web import launcher


@pytest.mark.parametrize("argv,target", [(["mclaw"], "web"), (["mclaw", "cli"], "cli"), (["mclaw", "resume", "old-session"], "cli"), (["mclaw", "setup"], "setup"), (["mclaw", "status"], "status"), (["mclaw", "stop"], "stop")])
def test_default_web_and_existing_management_routes(monkeypatch, argv, target):
    called = []
    monkeypatch.setattr("sys.argv", argv)
    monkeypatch.setattr(entry, "_setup_logging", lambda *_args: None)
    monkeypatch.setattr(entry, "load_mclaw_dotenv", lambda: None)
    monkeypatch.setattr(entry, "_run_chat", lambda args: called.append(("cli", getattr(args, "resume", None))))
    monkeypatch.setattr(entry, "_run_setup", lambda args: called.append(("setup", None)))
    monkeypatch.setattr(launcher, "launch", lambda: called.append(("web", None)))
    monkeypatch.setattr(launcher, "show_status", lambda: called.append(("status", None)))
    monkeypatch.setattr(launcher, "stop", lambda: called.append(("stop", None)))
    entry.main()
    assert called[0][0] == target
    if "resume" in argv:
        assert called[0][1] == "old-session"


def test_repeated_launch_reuses_authenticated_service_without_spawning(monkeypatch, tmp_path):
    existing = {"pid": 123, "port": 12345, "token": "local-test"}
    monkeypatch.setattr(launcher, "running_service", lambda _home: existing)
    def should_not_spawn(*_args, **_kwargs):
        raise AssertionError("A second service was spawned")
    monkeypatch.setattr(launcher.subprocess, "Popen", should_not_spawn)
    assert launcher.ensure_service(tmp_path) is existing


@pytest.mark.parametrize("option,attribute", [
    ("--model", "model"), ("--provider", "provider"),
    ("--base-url", "base_url"), ("--api-key", "api_key"),
])
def test_legacy_explicit_runtime_overrides_remain_scoped_to_terminal(monkeypatch, option, attribute):
    called = []
    monkeypatch.setattr("sys.argv", ["mclaw", option, "local-test-value"])
    monkeypatch.setattr(entry, "_setup_logging", lambda *_args: None)
    monkeypatch.setattr(entry, "load_mclaw_dotenv", lambda: None)
    monkeypatch.setattr(entry, "_run_chat", lambda args: called.append(getattr(args, attribute)))
    monkeypatch.setattr(launcher, "launch", lambda: pytest.fail("runtime override was ignored"))
    entry.main()
    assert called == ["local-test-value"]


def test_advertised_endpoint_is_not_trusted_without_token_validation(monkeypatch, tmp_path):
    import json

    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "service.json").write_text(json.dumps({"pid": 123, "port": 12345, "token": "obsolete"}))
    monkeypatch.setattr(launcher.httpx, "get", lambda *_args, **_kwargs: SimpleNamespace(status_code=403))
    assert launcher.running_service(tmp_path) is None


def test_losing_concurrent_launcher_waits_for_live_service_owner(monkeypatch, tmp_path):
    from mclaw.cli.runtime import session_lock

    existing = {"pid": 456, "port": 12345, "token": "local-test"}
    probes = iter([None, None, None, existing])
    monkeypatch.setattr(launcher, "running_service", lambda _home: next(probes))
    monkeypatch.setattr(launcher.Path, "is_file", lambda _path: True)
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *_args, **_kwargs: SimpleNamespace(poll=lambda: 1))
    monkeypatch.setattr(session_lock.InteractiveSessionLock, "_read_lock", lambda _self: {"pid": 456})
    monkeypatch.setattr(session_lock, "_is_process_alive", lambda pid: pid == 456)
    monkeypatch.setattr(launcher.time, "sleep", lambda _seconds: None)
    assert launcher.ensure_service(tmp_path) is existing


@pytest.mark.parametrize("document", ["[]", '{"port": null}', '{"port": {}}',
                                      '{"port": 12345, "token": "test"}',
                                      '{"pid": 123, "port": 12345, "token": null}'])
def test_corrupt_service_record_is_treated_as_unavailable(tmp_path, document):
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "service.json").write_text(document, encoding="utf-8")
    assert launcher.running_service(tmp_path) is None


def test_launch_prints_local_url_and_commands_without_printing_auth_token(monkeypatch, capsys):
    service = {"pid": 123, "port": 12345, "token": "test-connection-token"}
    opened = []
    monkeypatch.setattr(launcher, "ensure_service", lambda: service)
    monkeypatch.setattr(launcher.webbrowser, "open", lambda url: opened.append(url) or True)
    launcher.launch()
    output = capsys.readouterr().out
    assert "M-CLAW Web UI已打开：http://127.0.0.1:12345/" in output
    assert "mclaw stop" in output and "mclaw status" in output
    assert service["token"] not in output
    assert opened == ["http://127.0.0.1:12345/#token=test-connection-token"]


def test_launch_retains_authenticated_manual_link_when_browser_cannot_open(monkeypatch, capsys):
    monkeypatch.setattr(launcher, "ensure_service", lambda: {"pid": 123, "port": 12345, "token": "test-connection-token"})
    monkeypatch.setattr(launcher.webbrowser, "open", lambda _url: False)
    launcher.launch()
    output = capsys.readouterr().out
    assert "已打开" not in output
    assert "请在本机浏览器打开：http://127.0.0.1:12345/#token=test-connection-token" in output


def test_status_reports_address_and_pid_without_launching(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(launcher, "get_mclaw_home", lambda: tmp_path)
    monkeypatch.setattr(launcher, "running_service", lambda _home: {"pid": 123, "port": 12345, "token": "test-private"})
    monkeypatch.setattr(launcher, "ensure_service", lambda *_a: pytest.fail("Status must not start a service"))
    launcher.show_status()
    output = capsys.readouterr().out
    assert "PID 123" in output and "http://127.0.0.1:12345/" in output
    assert "test-private" not in output


@pytest.mark.parametrize("command", [launcher.show_status, launcher.stop])
def test_service_commands_do_not_start_an_absent_service(monkeypatch, tmp_path, capsys, command):
    monkeypatch.setattr(launcher, "get_mclaw_home", lambda: tmp_path)
    monkeypatch.setattr(launcher, "running_service", lambda _home: None)
    monkeypatch.setattr(launcher, "ensure_service", lambda *_a: pytest.fail("Must not start a service"))
    monkeypatch.setattr(launcher.httpx, "post", lambda *_a, **_k: pytest.fail("No process to stop"))
    command()
    assert "服务未运行" in capsys.readouterr().out


def test_stop_authenticates_and_waits_for_owned_service_cleanup(monkeypatch, tmp_path, capsys):
    requests = []
    owner_pids = iter([123, 123, None])
    monkeypatch.setattr(launcher, "get_mclaw_home", lambda: tmp_path)
    monkeypatch.setattr(launcher, "running_service", lambda _home: {"pid": 123, "port": 12345, "token": "test-private"})
    monkeypatch.setattr(launcher, "_service_owner_pid", lambda _home: next(owner_pids))
    monkeypatch.setattr(launcher.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(launcher.httpx, "post", lambda url, **kwargs: (
        requests.append((url, kwargs)) or SimpleNamespace(raise_for_status=lambda: None)))
    launcher.stop()
    assert len(requests) == 1
    assert requests[0][0] == "http://127.0.0.1:12345/api/v1/service/stop"
    assert requests[0][1]["headers"] == {"Authorization": "Bearer test-private"}
    assert requests[0][1]["trust_env"] is False
    output = capsys.readouterr().out
    assert "等待任务收尾" in output and "服务已停止（PID 123）" in output
    assert "test-private" not in output


def test_stop_does_not_claim_success_while_tools_are_still_closing(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(launcher, "get_mclaw_home", lambda: tmp_path)
    monkeypatch.setattr(launcher, "running_service", lambda _home: {"pid": 123, "port": 12345, "token": "test-private"})
    monkeypatch.setattr(launcher, "_service_owner_pid", lambda _home: 123)
    monkeypatch.setattr(launcher.httpx, "post", lambda *_a, **_k: SimpleNamespace(raise_for_status=lambda: None))
    with pytest.raises(SystemExit) as error:
        launcher.stop(timeout=0)
    assert error.value.code == 1
    output = capsys.readouterr().out
    assert "服务仍在收尾" in output and "mclaw status" in output
    assert "服务已停止" not in output


def test_stop_reports_uncertain_request_without_exposing_connection_details(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(launcher, "get_mclaw_home", lambda: tmp_path)
    monkeypatch.setattr(launcher, "running_service", lambda _home: {"pid": 123, "port": 12345, "token": "test-private"})
    def fail(*_args, **_kwargs):
        raise launcher.httpx.ReadTimeout("test-private")
    monkeypatch.setattr(launcher.httpx, "post", fail)
    with pytest.raises(SystemExit):
        launcher.stop()
    output = capsys.readouterr().out
    assert "未能确认退出请求" in output and "服务已停止" not in output
    assert "test-private" not in output


def test_stop_does_not_signal_an_unreachable_live_owner(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(launcher, "get_mclaw_home", lambda: tmp_path)
    monkeypatch.setattr(launcher, "running_service", lambda _home: None)
    monkeypatch.setattr(launcher, "_service_owner_pid", lambda _home: 123)
    monkeypatch.setattr(launcher.httpx, "post", lambda *_a, **_k: pytest.fail("Cannot authenticate this service"))
    with pytest.raises(SystemExit):
        launcher.stop()
    assert "服务暂时无法连接（PID 123）" in capsys.readouterr().out
