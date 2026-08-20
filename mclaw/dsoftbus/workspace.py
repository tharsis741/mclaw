# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Private, device-local workspaces for inbound DSoftBus Agent contexts."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path


_CONTEXT_DIRECTORY = re.compile(r"[0-9a-f]{32}\Z")


class RemoteWorkspaceError(RuntimeError):
    """The requested remote workspace could not be created securely."""


def _directory_flags() -> int:
    required = ("O_DIRECTORY", "O_NOFOLLOW")
    if any(not hasattr(os, name) for name in required):
        raise RemoteWorkspaceError("Secure directory primitives are unavailable")
    return (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
    )


def _open_absolute_directory_no_follow(path: Path) -> int:
    if not path.is_absolute() or path == path.parent:
        raise RemoteWorkspaceError("Remote workspace parent is invalid")
    flags = _directory_flags()
    try:
        descriptor = os.open("/", flags)
    except OSError as error:
        raise RemoteWorkspaceError("Remote workspace root is unavailable") from error
    try:
        for component in path.parts[1:]:
            if component in {"", ".", ".."} or "/" in component or "\x00" in component:
                raise RemoteWorkspaceError("Remote workspace component is invalid")
            child = os.open(component, flags, dir_fd=descriptor)
            metadata = os.fstat(child)
            if not stat.S_ISDIR(metadata.st_mode):
                os.close(child)
                raise RemoteWorkspaceError("Remote workspace parent is not a directory")
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_or_create_private_child(parent_fd: int, name: str) -> int:
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
        os.fsync(parent_fd)
    except FileExistsError:
        pass
    except OSError as error:
        raise RemoteWorkspaceError("Remote workspace directory creation failed") from error
    try:
        descriptor = os.open(name, _directory_flags(), dir_fd=parent_fd)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
        ):
            raise RemoteWorkspaceError("Remote workspace ownership is invalid")
        if stat.S_IMODE(metadata.st_mode) != 0o700:
            os.fchmod(descriptor, 0o700)
            metadata = os.fstat(descriptor)
            if stat.S_IMODE(metadata.st_mode) != 0o700:
                raise RemoteWorkspaceError("Remote workspace mode is invalid")
        return descriptor
    except BaseException:
        if "descriptor" in locals():
            os.close(descriptor)
        raise


def _ensure_windows_directory(path: Path) -> None:
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise RemoteWorkspaceError("Remote workspace path is not a directory")
        os.chmod(path, 0o700)
    except RemoteWorkspaceError:
        raise
    except OSError as error:
        raise RemoteWorkspaceError("Remote workspace directory creation failed") from error


def ensure_remote_workspace(root: str | Path, context_directory: str) -> Path:
    """Create one owner-only context directory without following POSIX links."""
    workspace_root = Path(root)
    if not workspace_root.is_absolute() or not _CONTEXT_DIRECTORY.fullmatch(
        context_directory
    ):
        raise RemoteWorkspaceError("Remote workspace identity is invalid")
    workspace = workspace_root / context_directory
    if os.name != "posix":
        _ensure_windows_directory(workspace_root)
        _ensure_windows_directory(workspace)
        return workspace

    parent_fd = _open_absolute_directory_no_follow(workspace_root.parent)
    root_fd: int | None = None
    workspace_fd: int | None = None
    try:
        root_fd = _open_or_create_private_child(parent_fd, workspace_root.name)
        workspace_fd = _open_or_create_private_child(root_fd, context_directory)
        os.fsync(workspace_fd)
        os.fsync(root_fd)
    finally:
        if workspace_fd is not None:
            os.close(workspace_fd)
        if root_fd is not None:
            os.close(root_fd)
        os.close(parent_fd)
    return workspace


__all__ = ["RemoteWorkspaceError", "ensure_remote_workspace"]
