# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Requirement checks for Playwright-backed browser automation."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path


def _has_chromium_browser(root: Path) -> bool:
    """Return whether a Playwright browser cache contains a Chromium binary."""
    return any(root.glob("chromium-*")) or any(root.glob("chrome-*")) or any(root.glob("**/chrome.exe"))


def find_playwright_browsers_root(config: dict | None = None) -> tuple[bool, str]:
    """Locate Playwright's browser cache and report Chromium availability."""
    env_value = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip().strip('"')
    if env_value:
        root = Path(env_value)
        if not root.exists():
            return False, f"{env_value}; path does not exist"
        has_chromium = _has_chromium_browser(root)
        return has_chromium, f"{env_value}; chromium={'yes' if has_chromium else 'no'}"

    candidates: list[Path] = []
    local_appdata = os.environ.get("LOCALAPPDATA", "").strip()
    if local_appdata:
        candidates.append(Path(local_appdata) / "ms-playwright")
    candidates.append(Path.home() / ".cache" / "ms-playwright")
    if os.name == "nt":
        candidates.append(Path.home() / "AppData" / "Local" / "ms-playwright")

    seen: set[str] = set()
    for root in candidates:
        key = os.path.normcase(str(root))
        if key in seen:
            continue
        seen.add(key)
        if root.exists():
            has_chromium = _has_chromium_browser(root)
            return has_chromium, f"{root}; chromium={'yes' if has_chromium else 'no'}; source=default cache"
    return False, "not found in PLAYWRIGHT_BROWSERS_PATH or default Playwright cache"


def diagnose_browser_requirements(config: dict | None = None) -> dict:
    """Return registry diagnostics for optional browser automation support."""
    if importlib.util.find_spec("playwright") is None:
        return {
            "available": False,
            "reason": "Playwright Python package is missing",
            "fix": "Install playwright in the active Python environment.",
        }

    has_chromium, detail = find_playwright_browsers_root(config)
    if not has_chromium:
        return {
            "available": False,
            "reason": detail,
            "fix": "Run python -m playwright install chromium.",
        }
    return {"available": True, "reason": detail, "fix": ""}


def check_browser_requirements(config: dict | None = None) -> bool:
    """Return True when Playwright and a Chromium browser binary are available."""
    if importlib.util.find_spec("playwright") is None:
        return False
    has_chromium, _ = find_playwright_browsers_root(config)
    return has_chromium
