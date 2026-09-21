# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import json
from types import SimpleNamespace

import pytest

from mclaw.tools import browser_tool


@pytest.mark.parametrize("operation", ["browser_download", "browser_screenshot"])
def test_browser_artifact_reaches_backend_with_explicit_output_path(tmp_path, monkeypatch, operation):
    target = tmp_path / ("paper.pdf" if operation == "browser_download" else "page.png")
    calls = []
    def save(*args, **kwargs):
        calls.append((args, kwargs))
        target.write_bytes(b"test-artifact")
        return {"success": True, "path": str(target)}
    backend = SimpleNamespace(download=save, screenshot=save)
    monkeypatch.setattr(browser_tool, "_start_cleanup_if_needed", lambda: None)
    monkeypatch.setattr(browser_tool, "_get_or_create_session", lambda _sid: SimpleNamespace(backend=backend))
    result = json.loads(getattr(browser_tool, operation)(path=str(target)))
    assert result["success"] is True
    assert target.read_bytes() == b"test-artifact" and len(calls) == 1


def test_browser_output_keeps_runtime_path_policy(tmp_path, monkeypatch):
    from mclaw.tools import file_operations
    def deny(*_args):
        raise PermissionError("protected path")
    monkeypatch.setattr(file_operations, "_checked_path", deny)
    monkeypatch.setattr(browser_tool, "_get_or_create_session", lambda _sid: pytest.fail("Denied output must not start a browser"))
    result = json.loads(browser_tool.browser_download(path=str(tmp_path / ".env")))
    assert not result["success"] and "protected path" in result["error"]
