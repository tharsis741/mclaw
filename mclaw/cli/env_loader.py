# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Helpers for loading M-Claw .env files consistently across entrypoints."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

from mclaw.constants import get_mclaw_home


def _load_dotenv_with_fallback(path: Path, *, override: bool) -> None:
    """Load dotenv content while tolerating legacy non-UTF-8 files."""
    try:
        load_dotenv(dotenv_path=path, override=override, encoding="utf-8")
    except UnicodeDecodeError:
        load_dotenv(dotenv_path=path, override=override, encoding="latin-1")


def load_mclaw_dotenv(
    *,
    mclaw_home: str | os.PathLike | None = None,
) -> list[Path]:
    """Load the M-Claw home .env file into the process environment.

    The home file intentionally overrides existing process values so every
    entrypoint sees the same persisted credentials after setup or key updates.
    """
    loaded: list[Path] = []

    home_path = Path(mclaw_home).expanduser() if mclaw_home else get_mclaw_home()
    user_env = home_path / ".env"

    if user_env.exists():
        _load_dotenv_with_fallback(user_env, override=True)
        loaded.append(user_env)

    return loaded
