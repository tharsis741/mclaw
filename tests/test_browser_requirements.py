# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Browser discovery must match the installed driver's revision and layout."""

from pathlib import Path

import pytest

from mclaw.tools import browser_requirements as requirements


pytestmark = pytest.mark.skipif(requirements.importlib.util.find_spec("playwright") is None,
                                reason="Playwright is not installed")


def test_empty_and_outdated_browser_directories_do_not_enable_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    (tmp_path / "chromium-1").mkdir()
    (tmp_path / "chromium-1" / "chrome.exe").write_bytes(b"outdated fixture")
    available, detail = requirements.find_playwright_browsers_root()
    assert not available and str(tmp_path) in detail and "expected=" in detail


def test_headless_only_executable_is_detected_and_removal_is_not_cached(tmp_path, monkeypatch):
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    available, detail = requirements.find_playwright_browsers_root()
    expected = Path(detail.split("expected=", 1)[1])
    assert not available and expected.is_relative_to(tmp_path)
    expected.parent.mkdir(parents=True)
    assert not requirements.check_browser_requirements()
    expected.write_bytes(b"binary fixture; never executed")
    expected.chmod(0o700)
    assert requirements.check_browser_requirements()
    expected.unlink()
    assert not requirements.check_browser_requirements()


def test_hermetic_cache_setting_uses_driver_package_not_directory_zero(monkeypatch):
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "0")
    _, detail = requirements.find_playwright_browsers_root()
    assert ".local-browsers" in detail and "expected=" in detail
