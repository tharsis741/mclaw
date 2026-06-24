"""Workspace trust checks for interactive local access."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from mclaw.cli.config import load_config, save_config


def normalize_workspace_path(path: str | os.PathLike[str]) -> str:
    """Return a stable, human-readable workspace path for config storage."""
    workspace = Path(path).expanduser()
    try:
        workspace = workspace.resolve()
    except OSError:
        workspace = workspace.absolute()
    return os.path.normpath(str(workspace))


def workspace_compare_key(path: str | os.PathLike[str]) -> str:
    normalized = normalize_workspace_path(path)
    if os.name == "nt":
        return os.path.normcase(normalized)
    return normalized


def get_trusted_workspaces(config: dict) -> list[str]:
    security = config.get("security", {}) if isinstance(config, dict) else {}
    values = security.get("trusted_workspaces", []) if isinstance(security, dict) else []
    if not isinstance(values, list):
        return []
    return [str(item) for item in values if str(item).strip()]


def is_workspace_trusted(config: dict, workspace: str | os.PathLike[str]) -> bool:
    target = workspace_compare_key(workspace)
    return any(workspace_compare_key(item) == target for item in get_trusted_workspaces(config))


def trust_workspace(config: dict, workspace: str | os.PathLike[str]) -> None:
    security = config.setdefault("security", {})
    if not isinstance(security, dict):
        security = {}
        config["security"] = security

    trusted = security.setdefault("trusted_workspaces", [])
    if not isinstance(trusted, list):
        trusted = []
        security["trusted_workspaces"] = trusted

    normalized = normalize_workspace_path(workspace)
    target = workspace_compare_key(normalized)
    if not any(workspace_compare_key(item) == target for item in trusted):
        trusted.append(normalized)


def ensure_workspace_trusted(
    workspace: str | os.PathLike[str],
    *,
    config: dict | None = None,
    prompt: Callable[[str], bool],
) -> bool:
    """Ensure the current workspace is trusted before starting local agent access."""
    user_config = load_config()
    if is_workspace_trusted(user_config, workspace):
        if config is not None:
            trust_workspace(config, workspace)
        return True

    normalized = normalize_workspace_path(workspace)
    if not prompt(normalized):
        return False

    trust_workspace(user_config, normalized)
    save_config(user_config)
    if config is not None:
        trust_workspace(config, normalized)
    return True
