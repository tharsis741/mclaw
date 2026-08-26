# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Private task workspaces used by trusted-device collaboration.

Runtime metadata remains under ``MCLAW_HOME/dsoftbus``.  Peer-provided bytes,
editable task copies, and transferred artifacts live under a separate root so
generic Agent tools can safely mutate a task workspace without gaining write
access to M-Claw's private runtime state.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path


_CONTEXT_DIRECTORY = re.compile(r"[0-9a-f]{32}\Z")
_PEER_DIRECTORY = re.compile(r"[0-9a-f]{64}\Z")
_TASK_ROLES = frozenset({"requested", "executing"})


class RemoteWorkspaceError(RuntimeError):
    """The requested remote workspace could not be created securely."""


@dataclass(frozen=True, slots=True)
class TaskWorkspacePaths:
    """Absolute paths owned by one local side of one remote Task."""

    root: Path
    incoming: Path | None = None
    input: Path | None = None
    work: Path | None = None
    outgoing: Path | None = None


def peer_directory(peer_device_id: str) -> str:
    """Return a non-identifying directory key for a verified peer ID."""

    import hashlib

    if not isinstance(peer_device_id, str) or not peer_device_id:
        raise RemoteWorkspaceError("Remote workspace peer identity is invalid")
    return hashlib.sha256(peer_device_id.encode("utf-8")).hexdigest()


def _task_directory(task_id: str) -> str:
    try:
        parsed = uuid.UUID(task_id)
    except (AttributeError, TypeError, ValueError) as error:
        raise RemoteWorkspaceError("Remote workspace task identity is invalid") from error
    if parsed.version != 4 or str(parsed) != task_id:
        raise RemoteWorkspaceError("Remote workspace task identity is invalid")
    return task_id


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
    except RemoteWorkspaceError:
        os.close(descriptor)
        raise
    except OSError as error:
        os.close(descriptor)
        raise RemoteWorkspaceError(
            "Remote workspace parent is not a directory"
        ) from error
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
    descriptor: int | None = None
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
    except RemoteWorkspaceError:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except OSError as error:
        if descriptor is not None:
            os.close(descriptor)
        raise RemoteWorkspaceError(
            "Remote workspace child is not a private directory"
        ) from error
    except BaseException:
        if descriptor is not None:
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


def _ensure_private_hierarchy(root: Path, components: tuple[str, ...]) -> Path:
    """Create an owner-only hierarchy without following POSIX symlinks."""

    if not root.is_absolute() or root == root.parent:
        raise RemoteWorkspaceError("Remote workspace root is invalid")
    if any(
        not component
        or component in {".", ".."}
        or "/" in component
        or "\\" in component
        or "\x00" in component
        for component in components
    ):
        raise RemoteWorkspaceError("Remote workspace component is invalid")
    if os.name != "posix":
        current = root
        _ensure_windows_directory(current)
        for component in components:
            current = current / component
            _ensure_windows_directory(current)
        return current

    parent_fd = _open_absolute_directory_no_follow(root.parent)
    descriptors: list[int] = []
    try:
        current_fd = _open_or_create_private_child(parent_fd, root.name)
        descriptors.append(current_fd)
        for component in components:
            current_fd = _open_or_create_private_child(current_fd, component)
            descriptors.append(current_fd)
        os.fsync(current_fd)
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        os.close(parent_fd)
    return root.joinpath(*components)


def ensure_private_subdirectory(
    root: str | Path,
    components: tuple[str, ...],
) -> Path:
    """Create validated owner-only children below an absolute private root."""

    return _ensure_private_hierarchy(Path(root), components)


def open_private_directory_no_follow(path: str | Path) -> int:
    """Open an existing POSIX directory through a no-follow absolute walk."""

    if os.name != "posix":
        raise RemoteWorkspaceError("Secure directory descriptors are unavailable")
    return _open_absolute_directory_no_follow(Path(path))


def default_workspace_root(state_root: str | Path) -> Path:
    """Return the product root while keeping host tests on their local drive."""

    if os.name == "posix":
        return Path("/data/local/tmp/.mclaw_dsoftbus_workspace")
    state = Path(state_root)
    return state.parent / ".mclaw_dsoftbus_workspace"


class DsoftbusWorkspace:
    """Create and clean exact task/artifact directories for this device."""

    def __init__(self, root: str | Path) -> None:
        value = Path(root)
        if not value.is_absolute():
            raise RemoteWorkspaceError("Remote workspace root must be absolute")
        self.root = value

    def ensure_task(
        self,
        role: str,
        peer_device_id: str,
        task_id: str,
    ) -> TaskWorkspacePaths:
        if role not in _TASK_ROLES:
            raise RemoteWorkspaceError("Remote workspace role is invalid")
        peer = peer_directory(peer_device_id)
        task = _task_directory(task_id)
        base_components = ("tasks", role, peer, task)
        task_root = _ensure_private_hierarchy(self.root, base_components)
        if role == "requested":
            outgoing = _ensure_private_hierarchy(
                self.root, (*base_components, "outgoing")
            )
            return TaskWorkspacePaths(root=task_root, outgoing=outgoing)
        paths = {
            name: _ensure_private_hierarchy(self.root, (*base_components, name))
            for name in ("incoming", "input", "work")
        }
        return TaskWorkspacePaths(
            root=task_root,
            incoming=paths["incoming"],
            input=paths["input"],
            work=paths["work"],
        )

    def bind_requested_task(
        self,
        peer_device_id: str,
        request_message_id: str,
        task_id: str,
    ) -> TaskWorkspacePaths:
        """Rename request staging to the authoritative Task ID atomically."""

        peer = peer_directory(peer_device_id)
        source_name = _task_directory(request_message_id)
        target_name = _task_directory(task_id)
        parent = _ensure_private_hierarchy(
            self.root,
            ("tasks", "requested", peer),
        )
        source = parent / source_name
        target = parent / target_name
        try:
            if target.exists():
                metadata = target.lstat()
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(
                    metadata.st_mode
                ):
                    raise RemoteWorkspaceError(
                        "Requested task workspace target is invalid"
                    )
            elif source.exists():
                metadata = source.lstat()
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(
                    metadata.st_mode
                ):
                    raise RemoteWorkspaceError(
                        "Requested task workspace source is invalid"
                    )
                os.replace(source, target)
            else:
                raise RemoteWorkspaceError("Requested task workspace is missing")
        except RemoteWorkspaceError:
            raise
        except OSError as error:
            raise RemoteWorkspaceError(
                "Requested task workspace binding failed"
            ) from error
        outgoing = target / "outgoing"
        if not outgoing.is_dir() or outgoing.is_symlink():
            raise RemoteWorkspaceError("Requested outgoing directory is invalid")
        return TaskWorkspacePaths(root=target, outgoing=outgoing)

    def ensure_artifact_directory(
        self,
        direction: str,
        peer_device_id: str,
        task_id: str,
        artifact_id: str | None = None,
    ) -> Path:
        if direction not in {"produced", "received"}:
            raise RemoteWorkspaceError("Artifact direction is invalid")
        peer = peer_directory(peer_device_id)
        task = _task_directory(task_id)
        components = ["artifacts", direction, peer, task]
        if artifact_id is not None:
            components.append(_task_directory(artifact_id))
        return _ensure_private_hierarchy(self.root, tuple(components))

    def clear_task(self, role: str, peer_device_id: str, task_id: str) -> bool:
        """Remove one exact validated task directory; never follow a link."""

        if role not in _TASK_ROLES:
            raise RemoteWorkspaceError("Remote workspace role is invalid")
        target = (
            self.root
            / "tasks"
            / role
            / peer_directory(peer_device_id)
            / _task_directory(task_id)
        )
        if not target.exists() and not target.is_symlink():
            return False
        try:
            metadata = target.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise RemoteWorkspaceError("Remote task workspace is not a directory")
            # ``rmtree`` never follows directory symlinks encountered below the
            # exact validated root.  A link is unlinked as an entry instead.
            shutil.rmtree(target)
        except RemoteWorkspaceError:
            raise
        except OSError as error:
            raise RemoteWorkspaceError("Remote task workspace cleanup failed") from error
        return True

    def clear_artifacts(
        self,
        direction: str,
        peer_device_id: str,
        task_id: str,
    ) -> bool:
        """Remove one exact produced/received Artifact tree without following links."""

        if direction not in {"produced", "received"}:
            raise RemoteWorkspaceError("Artifact direction is invalid")
        target = (
            self.root
            / "artifacts"
            / direction
            / peer_directory(peer_device_id)
            / _task_directory(task_id)
        )
        if not target.exists() and not target.is_symlink():
            return False
        try:
            metadata = target.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise RemoteWorkspaceError("Artifact task tree is not a directory")
            shutil.rmtree(target)
        except RemoteWorkspaceError:
            raise
        except OSError as error:
            raise RemoteWorkspaceError("Artifact task cleanup failed") from error
        return True

    def clear_artifact(
        self,
        direction: str,
        peer_device_id: str,
        task_id: str,
        artifact_id: str,
    ) -> bool:
        """Remove one exact Artifact directory."""

        if direction not in {"produced", "received"}:
            raise RemoteWorkspaceError("Artifact direction is invalid")
        target = (
            self.root
            / "artifacts"
            / direction
            / peer_directory(peer_device_id)
            / _task_directory(task_id)
            / _task_directory(artifact_id)
        )
        if not target.exists() and not target.is_symlink():
            return False
        try:
            metadata = target.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise RemoteWorkspaceError("Artifact directory is invalid")
            shutil.rmtree(target)
        except RemoteWorkspaceError:
            raise
        except OSError as error:
            raise RemoteWorkspaceError("Artifact cleanup failed") from error
        return True


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


__all__ = [
    "DsoftbusWorkspace",
    "RemoteWorkspaceError",
    "TaskWorkspacePaths",
    "default_workspace_root",
    "ensure_private_subdirectory",
    "ensure_remote_workspace",
    "open_private_directory_no_follow",
    "peer_directory",
]
