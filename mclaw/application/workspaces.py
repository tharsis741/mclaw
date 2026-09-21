# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared rules for service-allocated background task directories."""

from __future__ import annotations

from pathlib import Path


BACKGROUND_WORKSPACE_SOURCES = frozenset({"weixin", "dingtalk", "scheduler"})


def matches_managed_workspace(session: dict, root: str) -> bool:
    """Match one exact allocated directory, never a parent or linked path."""
    if session.get("workspace_explicit") is not False or not root or not session.get("workspace"):
        return False
    try:
        root_path = Path(root)
        expected = root_path / session["id"]
        return (
            root_path.is_absolute() and root_path.resolve() == root_path
            and expected.parent == root_path
            and expected.resolve() == expected == Path(session["workspace"])
            and expected.is_dir()
            and not root_path.is_symlink() and not expected.is_symlink()
            and not getattr(root_path, "is_junction", lambda: False)()
            and not getattr(expected, "is_junction", lambda: False)()
        )
    except (OSError, RuntimeError, TypeError, ValueError, KeyError):
        return False


def is_managed_background_workspace(session: dict, platform: str) -> bool:
    """Allow only the matching background producer to use its private directory."""
    return (
        platform in BACKGROUND_WORKSPACE_SOURCES
        and session.get("source") == platform
        and matches_managed_workspace(session, session.get("managed_workspace_root", ""))
    )
