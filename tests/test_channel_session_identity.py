# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from pathlib import Path
from unittest.mock import Mock

import pytest

from mclaw.application.sessions import SessionService
from mclaw.application.worker import SessionWorker
from mclaw.application.workspaces import is_managed_background_workspace
from mclaw.cli import config as config_io
from mclaw.cli.runtime.workspace_trust import is_workspace_trusted


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setenv("MCLAW_HOME", str(tmp_path))
    config_io.save_config({"toolsets": ["mclaw-required"], "channels": {"weixin": {"enabled": False}}})
    instance = SessionService(tmp_path, runner_factory=lambda _: None)
    yield instance
    for lock in instance.locks.values():
        lock.release()
    instance.store.close()
    instance.history.close()


def channel(service, identifier, source="weixin", **changes):
    service.history.create_session(identifier, source=source, **changes)
    return service.prepare_background_session(identifier)


def test_channel_numbers_are_separate_durable_and_manual_titles_survive(service):
    assert channel(service, "wx-one")["title"] == "微信消息1"
    assert channel(service, "wx-two")["title"] == "微信消息2"
    assert channel(service, "ding-one", "dingtalk")["title"] == "钉钉消息1"
    assert channel(service, "wx-one")["title"] == "微信消息1"
    service.patch("wx-one", "browser", {"title": "项目沟通"})
    assert channel(service, "wx-one")["title"] == "项目沟通"
    from mclaw.application.store import ApplicationStore
    reopened = ApplicationStore(service.home / "application.sqlite3")
    try:
        assert reopened.channel_title("weixin", "wx-three", set()) == "微信消息3"
    finally:
        reopened.close()
    assert {row["source"] for row in service.list()} == {"weixin", "dingtalk"}
    assert service.history.get_session("wx-two")["title"] == "微信消息2"


def test_existing_history_names_and_unknown_workspaces_are_preserved(service):
    work = service.home.parent / "historical-work"
    work.mkdir(exist_ok=True)
    service.history.create_session("old", source="weixin", workspace=str(work))
    service.history.set_session_title("old", "历史项目")
    result = service.prepare_background_session("old")
    assert result["title"] == "历史项目" and result["source"] == "weixin"
    assert result["workspace_explicit"]
    assert "managed_workspace_root" not in service.store.get("old")


def test_existing_automatically_allocated_channel_directory_gains_the_scoped_marker(service):
    channel(service, "pre-upgrade")
    old = service.store.get("pre-upgrade")
    old.pop("managed_workspace_root")
    service.store.save(old)
    service.prepare_background_session("pre-upgrade")
    restored = service.store.get("pre-upgrade")
    assert restored["workspace"] == old["workspace"]
    assert restored["title"] == old["title"]
    assert is_managed_background_workspace(restored, "weixin")


@pytest.mark.parametrize("source", ["weixin", "dingtalk"])
def test_allocated_channel_directory_skips_only_workspace_prompt(service, source):
    result = channel(service, "managed", source)
    assert "managed_workspace_root" not in result
    worker = SessionWorker(service.home, service.store.get("managed"), lambda _: None)
    worker.request = Mock(side_effect=AssertionError("managed channel directory must not prompt"))
    config = config_io.load_config()
    worker._ensure_workspace_trusted(config, source)
    assert is_workspace_trusted(config_io.load_config(), Path(result["workspace"]))
    assert not is_workspace_trusted(config_io.load_config(), Path(result["workspace"]).parent)
    assert not worker.cancel.is_set()


@pytest.mark.parametrize("mutation", ["web", "explicit", "different-directory", "missing-marker", "wrong-channel"])
def test_exemption_cannot_cover_a_user_workspace_or_other_task_source(service, mutation):
    channel(service, "managed")
    session = service.store.get("managed")
    platform = "weixin"
    if mutation == "web":
        platform = "web"
    elif mutation == "explicit":
        session["workspace_explicit"] = True
    elif mutation == "different-directory":
        another = service.home.parent / "outside-work"
        another.mkdir(exist_ok=True)
        session["workspace"] = str(another)
    elif mutation == "missing-marker":
        session.pop("managed_workspace_root")
    else:
        platform = "dingtalk"
    worker = SessionWorker(service.home, session, lambda _: None)
    worker.request = Mock(return_value=False)
    with pytest.raises(PermissionError):
        worker._ensure_workspace_trusted(config_io.load_config(), platform)
    worker.request.assert_called_once()
    assert not is_workspace_trusted(config_io.load_config(), worker.workspace)


def test_symlinked_task_directory_is_not_implicitly_trusted(service, monkeypatch):
    channel(service, "managed")
    worker = SessionWorker(service.home, service.store.get("managed"), lambda _: None)
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == worker.workspace or original(path))
    assert not is_managed_background_workspace(worker.session, "weixin")
