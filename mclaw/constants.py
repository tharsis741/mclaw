# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared constants for M-Claw.

Import-safe module with no dependencies — can be imported from anywhere
without risk of circular imports.
"""

import os
from pathlib import Path


def get_mclaw_home() -> Path:
    """Return the M-Claw home directory.

    Reads MCLAW_HOME env var, then falls back through runtime bootstrap.
    This is the single source of truth; runtime state paths should import this.
    """
    configured = os.getenv("MCLAW_HOME")
    if configured:
        return Path(configured).expanduser()
    try:
        from mclaw.runtime.bootstrap import BootstrapPathResolver

        return BootstrapPathResolver.resolve_mclaw_home()
    except Exception:
        return Path.home() / ".mclaw"


def get_skills_dir() -> Path:
    """Return the user skills directory under M-Claw home."""
    return get_mclaw_home() / "skills"


def _display_path(path: Path) -> str:
    try:
        return str(path.expanduser().resolve())
    except OSError:
        return str(path.expanduser().absolute())


def display_mclaw_home() -> str:
    """Return an exact user-facing M-Claw home path."""
    return _display_path(get_mclaw_home())


def display_mclaw_path(*parts: str) -> str:
    """Return an exact user-facing path under M-Claw home."""
    path = get_mclaw_home()
    for part in parts:
        path = path / part
    return _display_path(path)


VALID_REASONING_EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max")


def parse_reasoning_effort(effort: str) -> dict | None:
    """Normalize user-facing reasoning effort text for provider adapters."""
    if not effort or not effort.strip():
        return None
    effort = effort.strip().lower()
    if effort == "none":
        return {"enabled": False}
    if effort in VALID_REASONING_EFFORTS:
        return {"enabled": True, "effort": effort}
    return None
