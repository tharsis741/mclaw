# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Requirement checks for Playwright-backed browser automation."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from functools import lru_cache
from pathlib import Path


_REGISTRY_ENV = (
    "PLAYWRIGHT_BROWSERS_PATH", "PLAYWRIGHT_NODEJS_PATH", "PLAYWRIGHT_HOST_PLATFORM_OVERRIDE",
    "LOCALAPPDATA", "XDG_CACHE_HOME", "HOME", "USERPROFILE", "INIT_CWD",
)


@lru_cache(maxsize=8)
def _driver_browser_paths(
    registry_file: str, modified_ns: int, environment: tuple[tuple[str, str | None], ...], cwd: str,
) -> tuple[str, str]:
    """Ask the installed driver for its exact platform/revision/headless binary.

    This reads registry metadata only; it neither launches nor installs a browser.
    Cache metadata, but check binary existence afresh on every diagnosis.
    """
    package_dir = Path(registry_file).parents[3]
    env = os.environ.copy()
    for key, value in environment:
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    node = env.get("PLAYWRIGHT_NODEJS_PATH") or str(package_dir.parent / ("node.exe" if sys.platform == "win32" else "node"))
    script = """
const {registry, registryDirectory} = require(process.argv[1]);
const executable = registry.findExecutable('chromium-headless-shell') || registry.findExecutable('chromium');
process.stdout.write(JSON.stringify([registryDirectory, executable?.executablePath() || '']));
"""
    result = subprocess.run(
        [node, "-e", script, registry_file], capture_output=True, text=True, encoding="utf-8",
        timeout=5, check=True, env=env, cwd=cwd,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    root, executable = json.loads(result.stdout)
    return str(root), str(executable)


def find_playwright_browsers_root(config: dict | None = None) -> tuple[bool, str]:
    """Verify the installed Playwright driver's expected headless executable."""
    spec = importlib.util.find_spec("playwright")
    if spec is None or spec.origin is None:
        return False, "Playwright Python package is missing"
    registry_file = Path(spec.origin).parent / "driver/package/lib/server/registry/index.js"
    try:
        root, executable = _driver_browser_paths(
            str(registry_file), registry_file.stat().st_mtime_ns,
            tuple((key, os.environ.get(key)) for key in _REGISTRY_ENV), os.getcwd(),
        )
        available = bool(executable) and Path(executable).is_file() and os.access(executable, os.X_OK)
        return available, f"{root}; chromium={'yes' if available else 'no'}; expected={executable}"
    except Exception as exc:
        return False, f"Could not inspect the installed Playwright driver: {type(exc).__name__}"


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
