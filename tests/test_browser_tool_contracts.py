# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import json
import threading
import time
from types import SimpleNamespace

import pytest

from mclaw.tools import browser_tool, file_operations


def stub_backend(monkeypatch, **methods):
    monkeypatch.setattr(browser_tool, "_start_cleanup_if_needed", lambda: None)
    monkeypatch.setattr(browser_tool, "_get_or_create_session",
                        lambda sid: SimpleNamespace(backend=SimpleNamespace(**methods)))


@pytest.mark.parametrize("operation", ["snapshot", "navigate"])
def test_snapshot_errors_and_recovery_metadata_survive_tool_wrapper(monkeypatch, operation):
    observation = {"success": False, "error": "frame detached", "error_code": "SNAPSHOT_FAILED",
                   "snapshot": "", "url": "about:blank", "title": "", "snapshot_id": "v1"}
    stub_backend(monkeypatch, snapshot=lambda sid: dict(observation),
                 navigate_and_snapshot=lambda sid, url: ({"url": url}, dict(observation)))
    raw = browser_tool.browser_snapshot() if operation == "snapshot" else browser_tool.browser_navigate("about:blank")
    result = json.loads(raw)
    assert not result["success"] and result["error_code"] == "SNAPSHOT_FAILED"
    assert result["snapshot_id"] == "v1"
    if operation == "navigate":
        assert result["action_completed"] is True and result["status"] == "partial"


@pytest.mark.parametrize("operation", ["screenshot", "download", "upload"])
def test_file_paths_resolve_against_agent_workspace_before_policy_check(tmp_path, monkeypatch, operation):
    target = tmp_path / "artifact.png"
    target.write_bytes(b"fixture")
    calls = []
    checks = []
    original = file_operations._checked_path

    def check(path, action):
        checks.append((path, action))
        return original(path, action)

    def invoke(sid, **arguments):
        calls.append((sid, arguments))
        return {"success": True}

    monkeypatch.setattr(file_operations, "_checked_path", check)
    stub_backend(monkeypatch, **{operation: invoke})
    parent = SimpleNamespace(workspace_path=str(tmp_path), session_id="workspace-session")
    if operation == "upload":
        raw = browser_tool.browser_upload(["artifact.png"], ref="e1", parent_agent=parent)
    else:
        raw = getattr(browser_tool, "browser_" + operation)(path="artifact.png", parent_agent=parent)
    assert json.loads(raw)["success"]
    assert checks == [(str(target), "read" if operation == "upload" else "write")]
    assert calls[0][0] == "browser_workspace-session"
    assert calls[0][1]["files" if operation == "upload" else "path"] == (
        [str(target)] if operation == "upload" else str(target)
    )


def test_upload_refuses_policy_denied_or_missing_files_before_browser_start(tmp_path, monkeypatch):
    monkeypatch.setattr(browser_tool, "_get_or_create_session", lambda sid: pytest.fail("must not start browser"))
    missing = json.loads(browser_tool.browser_upload([str(tmp_path / "missing")]))
    assert not missing["success"] and "does not exist" in missing["error"]

    def deny(path, action):
        assert action == "read"
        raise PermissionError("protected input")

    monkeypatch.setattr(file_operations, "_checked_path", deny)
    denied = json.loads(browser_tool.browser_upload([str(tmp_path / "secret")]))
    assert not denied["success"] and "protected input" in denied["error"]


@pytest.mark.parametrize("operation,arguments", [
    ("wait_for", {}), ("wait_for", {"text": "x", "ref": "e1"}),
    ("wait_for", {"text": "x", "timeout_ms": 30001}),
    ("wait_for", {"text": "x", "state": "attached"}),
    ("select", {"ref": "e1", "values": "red"}),
    ("check", {"ref": "e1", "checked": "false"}),
    ("hover", {"ref": "#button"}), ("tabs", {"action": "select"}),
    ("handle_dialog", {"accept": "false"}), ("upload", {"files": "file.txt"}),
    ("tabs", {"action": []}), ("tabs", {"tab_id": []}), ("tabs", {"offset": -1}),
    ("wait_for", {"text": "x", "state": []}), ("snapshot", {"offset": True}),
    ("scroll", {"direction": []}), ("scroll", {"ref": "#button"}),
    ("download", {"ref": ""}), ("download", {"url": []}),
    ("download", {"url": "data:text/plain,example"}), ("download", {"url": "file:///private.txt"}),
    ("download", {"url": "http://"}), ("download", {"url": "https://[invalid"}),
    ("screenshot", {"path": []}),
])
def test_invalid_arguments_fail_before_browser_start(monkeypatch, operation, arguments):
    monkeypatch.setattr(browser_tool, "_get_or_create_session", lambda sid: pytest.fail("must not start browser"))
    result = json.loads(getattr(browser_tool, "browser_" + operation)(**arguments))
    assert not result["success"] and result["error"]


def test_all_browser_operations_follow_runtime_gate_and_cancellation_contract():
    from mclaw.runtime.features import FeatureState, RuntimeFeature, RuntimeFeatures
    from mclaw.tools.cancellation import cancellation_strategy
    from mclaw.tools.toolsets import BROWSER_TOOLS

    disabled = RuntimeFeatures({"browser_tool": RuntimeFeature("browser_tool", FeatureState.DISABLED)})
    enabled = RuntimeFeatures({"browser_tool": RuntimeFeature("browser_tool", FeatureState.ENABLED)})
    assert not disabled.filter_tool_names(set(BROWSER_TOOLS))
    assert enabled.filter_tool_names(set(BROWSER_TOOLS)) == set(BROWSER_TOOLS)
    assert all(cancellation_strategy(name) == "async-or-fenced" for name in BROWSER_TOOLS)


def test_individual_disabled_tool_does_not_disable_whole_browser():
    from mclaw.tools.toolsets import effective_disabled_tools

    disabled = ["browser_download", "browser_upload", "web_search"]
    assert effective_disabled_tools(disabled) == set(disabled)


def test_missing_text_does_not_clear_an_input(monkeypatch):
    monkeypatch.setattr(browser_tool, "_get_or_create_session", lambda sid: pytest.fail("must not start browser"))
    tool = next(tool for tool in browser_tool._BROWSER_TOOLS if tool["name"] == "browser_type")
    result = json.loads(tool["handler"]({"ref": "e1"}))
    assert not result["success"]


def test_whitespace_ref_is_normalized_but_multiple_prefixes_are_rejected():
    assert browser_tool._clean_ref("  @e42  ") == ("e42", None)
    assert browser_tool._clean_ref("@@e42")[1]


def test_timeout_is_reported_separately_from_user_interruption():
    from mclaw.tools.browser_backend import BrowserOperationTimedOut
    result = json.loads(browser_tool._browser_exception_result("browser_click", BrowserOperationTimedOut("timed out")))
    assert result["error_code"] == "BROWSER_TIMEOUT" and result["completion_unknown"]
    assert not result.get("interrupted")


def test_session_lease_protects_active_work_from_idle_cleanup(monkeypatch):
    closed = []
    backend = SimpleNamespace(close_session=closed.append)
    monkeypatch.setattr(browser_tool, "_browser_backend", backend)
    monkeypatch.setattr(browser_tool, "_browser_sessions", {})
    with browser_tool._session_lease("active") as sess:
        assert browser_tool._cleanup_expired_sessions(now=sess.last_activity + 1000) == 0
    assert browser_tool._cleanup_expired_sessions(now=sess.last_activity + 1000) == 1
    assert closed == ["active"]


def test_cleanup_does_not_lock_other_sessions_or_reuse_a_closing_id(monkeypatch):
    closing, release = threading.Event(), threading.Event()

    def close(sid):
        closing.set()
        release.wait(3)

    monkeypatch.setattr(browser_tool, "_browser_backend", SimpleNamespace(close_session=close))
    monkeypatch.setattr(browser_tool, "_browser_sessions", {})
    old = browser_tool._get_or_create_session("expired")
    old.last_activity = time.time() - 1000
    cleanup = threading.Thread(target=browser_tool._cleanup_expired_sessions)
    cleanup.start()
    assert closing.wait(1)
    replacements = []
    retry = threading.Thread(target=lambda: replacements.append(browser_tool._get_or_create_session("expired")))
    retry.start()
    try:
        assert browser_tool._get_or_create_session("unrelated").session_id == "unrelated"
        assert not replacements
    finally:
        release.set()
        cleanup.join(2)
        retry.join(2)
    assert len(replacements) == 1 and replacements[0] is not old


def test_queued_timeout_does_not_execute_or_cancel_running_work():
    from mclaw.tools.browser_backend import BrowserBackend, BrowserOperationTimedOut
    backend = BrowserBackend()
    started, release = threading.Event(), threading.Event()
    called = []

    def block():
        started.set()
        release.wait(3)

    running = threading.Thread(target=lambda: backend._run_on_worker(block))
    running.start()
    assert started.wait(1)
    try:
        with pytest.raises(BrowserOperationTimedOut):
            backend._run_on_worker(lambda: called.append(True), cancel_session_id="queued", operation_timeout=0.1)
        assert running.is_alive() and not called
    finally:
        release.set()
        running.join(2)
        backend.stop()
    assert not called


def test_failed_download_transfer_removes_partial_output_and_retains_capture(tmp_path):
    from pathlib import Path
    from mclaw.tools.browser_backend import BrowserBackend, _SessionState

    target = tmp_path / "download.txt"
    target.write_text("original", encoding="utf-8")

    def fail(path):
        Path(path).write_bytes(b"partial")
        raise RuntimeError("Transfer failed")

    download = SimpleNamespace(suggested_filename="download.txt", save_as=fail)
    state = _SessionState(context=None, page=None, pending_downloads=[download])
    with pytest.raises(RuntimeError, match="Transfer failed"):
        BrowserBackend()._save_download(state, download, str(target))
    assert state.pending_downloads == [download]
    assert target.read_text() == "original"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["download.txt"]
