# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from types import SimpleNamespace

import pytest

from mclaw.runtime.manager import RuntimeManager
from mclaw.web import launcher


@pytest.fixture
def kaihong(monkeypatch):
    monkeypatch.setattr(RuntimeManager, "detect", lambda: "kaihong")
    monkeypatch.setattr(launcher.shutil, "which", lambda _: "/bin/aa")
    monkeypatch.setattr(launcher.webbrowser, "open", lambda _: pytest.fail("Desktop launcher used on Kaihong"))


def test_kaihong_passes_authenticated_url_to_uri_and_legacy_jump_url(kaihong, monkeypatch):
    calls = []
    url = "http://127.0.0.1:18789/#token=local-test&next=/"

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout="start ability successfully.\n")

    monkeypatch.setattr(launcher.subprocess, "run", run)
    assert launcher.open_browser(url)
    assert calls[0][0] == ["/bin/aa", "start", "-b", "com.kaihong.browser", "-a", "MainAbility",
                           "-U", url, "--ps", "jumpUrl", url]
    assert calls[0][1].get("shell", False) is False
    assert calls[0][1]["timeout"] == 8
    assert calls[0][1]["capture_output"] is True


def test_other_kaihong_images_can_fall_back_to_a_system_uri_handler(kaihong, monkeypatch):
    calls = []
    def run(argv, **_kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="error: failed to start ability." if len(calls) == 1 else "start ability successfully.")
    monkeypatch.setattr(launcher.subprocess, "run", run)
    assert launcher.open_browser("http://127.0.0.1:18789/#token=local-test")
    assert calls[1][2:4] == ["-A", "ohos.want.action.viewData"]
    assert "-A" not in calls[0]


@pytest.mark.parametrize("code,output", [(0, "error: failed to start ability."), (1, ""), (0, "")])
def test_kaihong_launch_failure_is_not_reported_as_opened(kaihong, monkeypatch, code, output):
    monkeypatch.setattr(launcher.subprocess, "run", lambda *_a, **_k: SimpleNamespace(returncode=code, stdout=output))
    assert not launcher.open_browser("http://127.0.0.1:18789/")


@pytest.mark.parametrize("error", [FileNotFoundError(), launcher.subprocess.TimeoutExpired("aa", 8)])
def test_kaihong_missing_or_unresponsive_launcher_returns_manual_link(kaihong, monkeypatch, error):
    def run(*_a, **_k):
        raise error
    monkeypatch.setattr(launcher.subprocess, "run", run)
    assert not launcher.open_browser("http://127.0.0.1:18789/")
