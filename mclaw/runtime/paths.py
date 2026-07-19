# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Host filesystem access with credential and destructive-anchor guards."""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path


CREDENTIAL_GLOBS = (
    "!**/.env*",
    "!**/.ssh/id_rsa",
    "!**/.ssh/id_dsa",
    "!**/.ssh/id_ecdsa",
    "!**/.ssh/id_ed25519",
    "!**/.aws/credentials",
    "!**/.kube/config",
    "!**/.docker/config.json",
    "!**/.git-credentials",
    "!**/.netrc",
    "!**/.npmrc",
    "!**/.pypirc",
)

_RUNTIME_MUTATIONS = {
    "write",
    "delete",
    "directory_delete",
    "move",
    "move_destination",
    "rename",
    "copy",
    "overwrite",
    "unknown_destructive",
}
_ANCHOR_MUTATIONS = {
    "write",
    "delete",
    "directory_delete",
    "move",
    "rename",
    "overwrite",
    "unknown_destructive",
}
_PSEUDO_DESTRUCTIVE_ACTIONS = {
    "delete",
    "directory_delete",
    "move",
    "move_destination",
    "rename",
    "unknown_destructive",
}
_PRIVATE_SSH_KEY_NAMES = {"id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"}
_CREDENTIAL_NAMES = {".git-credentials", ".netrc", ".npmrc", ".pypirc"}
_CREDENTIAL_SUFFIXES = {
    (".aws", "credentials"),
    (".kube", "config"),
    (".docker", "config.json"),
}


@dataclass(frozen=True)
class PathDecision:
    """Result of normalizing and authorizing one filesystem path."""

    original: str
    resolved: Path
    action: str
    scope: str
    allowed: bool
    reason: str

    def error_message(self) -> str:
        return (
            f"PathPolicy denied {self.action} for {self.resolved} "
            f"(scope={self.scope}): {self.reason}"
        )


def _normcase(value: str) -> str:
    return os.path.normcase(os.path.abspath(value))


def is_relative_to(path: Path, root: Path) -> bool:
    """Return whether path is within root, with a normcase fallback."""
    try:
        path.relative_to(root)
        return True
    except ValueError:
        try:
            return os.path.commonpath([_normcase(str(path)), _normcase(str(root))]) == _normcase(str(root))
        except ValueError:
            return False


class PathPolicy:
    """Allow normal host access while protecting a few high-impact path classes."""

    def __init__(
        self,
        *,
        mclaw_home: Path,
        protected_anchors: Iterable[Path] = (),
        pseudo_roots: Iterable[Path] = (),
        protect_mount_points: bool = False,
    ) -> None:
        self.mclaw_home = self._resolve_root(mclaw_home)
        self.protected_anchors = self._dedupe_roots(protected_anchors)
        self.pseudo_roots = self._dedupe_roots(pseudo_roots)
        self.protect_mount_points = bool(protect_mount_points)

    @staticmethod
    def _resolve_root(path: Path) -> Path:
        try:
            return Path(path).expanduser().resolve()
        except OSError:
            return Path(path).expanduser().absolute()

    @classmethod
    def _dedupe_roots(cls, roots: Iterable[Path]) -> tuple[Path, ...]:
        unique: dict[str, Path] = {}
        for root in roots:
            resolved = cls._resolve_root(Path(root))
            unique.setdefault(_normcase(str(resolved)), resolved)
        return tuple(unique.values())

    def normalize(self, path_value: str | os.PathLike, base: str | os.PathLike | None = None) -> Path:
        """Expand user/env syntax and resolve relative paths against a base."""
        value = os.path.expandvars(str(path_value or "")).strip().strip('"').strip("'") or "."
        path = Path(value).expanduser()
        if not path.is_absolute() and base:
            path = Path(base).expanduser() / path
        try:
            return path.resolve()
        except OSError:
            return path.absolute()

    def delegation_root(self) -> Path:
        return self.mclaw_home / "delegations"

    def process_log_root(self) -> Path:
        return self.mclaw_home / "process_logs"

    def is_runtime_internal_path(self, path_value: str | os.PathLike) -> bool:
        """Return whether a path belongs to M-Claw's private runtime state."""
        path = self.normalize(path_value)
        return is_relative_to(path, self.mclaw_home)

    @staticmethod
    def is_credential_path(path: Path) -> bool:
        """Recognize common plaintext credentials and sensitive proc files."""
        parts = tuple(part.casefold() for part in path.parts if part not in {path.anchor, "", "."})
        if not parts:
            return False
        name = parts[-1]
        if name.startswith(".env") or name in _CREDENTIAL_NAMES:
            return True
        if name in _PRIVATE_SSH_KEY_NAMES and ".ssh" in parts[:-1]:
            return True
        if any(len(parts) >= len(suffix) and parts[-len(suffix):] == suffix for suffix in _CREDENTIAL_SUFFIXES):
            return True
        if "proc" not in parts:
            return False
        proc_index = parts.index("proc")
        if proc_index != 0:
            return False
        proc_tail = parts[proc_index + 1:]
        return proc_tail == ("kcore",) or (
            len(proc_tail) == 2
            and proc_tail[-1] in {"environ", "mem"}
        )

    @staticmethod
    def _same_path(left: Path, right: Path) -> bool:
        return _normcase(str(left)) == _normcase(str(right))

    def _is_pseudo_path(self, path: Path) -> bool:
        return any(is_relative_to(path, root) for root in self.pseudo_roots)

    def _search_intersects_pseudo(self, path: Path) -> bool:
        return any(is_relative_to(path, root) or is_relative_to(root, path) for root in self.pseudo_roots)

    def _is_protected_anchor(self, path: Path) -> bool:
        if any(self._same_path(path, root) for root in self.protected_anchors):
            return True
        if path.anchor and self._same_path(path, self._resolve_root(Path(path.anchor))):
            return True
        if self.protect_mount_points:
            try:
                return path.is_mount()
            except OSError:
                return False
        return False

    def classify(self, path: Path) -> str:
        if self.is_credential_path(path):
            return "credential"
        if is_relative_to(path, self.mclaw_home):
            return "runtime_internal"
        if self._is_pseudo_path(path):
            return "pseudo"
        if self._is_protected_anchor(path):
            return "protected_anchor"
        return "host"

    def check(
        self,
        action: str,
        path_value: str | os.PathLike,
        *,
        base: str | os.PathLike | None = None,
    ) -> PathDecision:
        """Authorize a filesystem action using exact-anchor, not subtree, guards."""
        raw_value = os.path.expandvars(str(path_value or "")).strip().strip('"').strip("'") or "."
        raw_path = Path(raw_value).expanduser()
        if not raw_path.is_absolute() and base:
            raw_path = Path(base).expanduser() / raw_path
        path = self.normalize(path_value, base=base)
        action = str(action or "read").lower()
        raw_scope = self.classify(raw_path)
        scope = raw_scope if raw_scope != "host" else self.classify(path)
        if scope == "credential":
            return PathDecision(
                str(path_value),
                path,
                action,
                scope,
                False,
                "credential files are only available through scoped secret APIs",
            )
        if action == "search" and self._search_intersects_pseudo(path):
            return PathDecision(
                str(path_value),
                path,
                action,
                "pseudo",
                False,
                "recursive search across /proc, /sys, or /dev is denied",
            )
        if scope == "runtime_internal" and action in _RUNTIME_MUTATIONS:
            return PathDecision(
                str(path_value),
                path,
                action,
                scope,
                False,
                "MCLAW_HOME runtime state is read-only to generic tools",
            )
        if scope == "protected_anchor" and action in _ANCHOR_MUTATIONS:
            return PathDecision(
                str(path_value),
                path,
                action,
                scope,
                False,
                "destructive mutation of this exact filesystem anchor is denied",
            )
        if scope == "pseudo" and action in _PSEUDO_DESTRUCTIVE_ACTIONS:
            return PathDecision(
                str(path_value),
                path,
                action,
                scope,
                False,
                "deleting or moving pseudo-filesystem nodes is denied",
            )
        return PathDecision(str(path_value), path, action, scope, True, "full host filesystem access")
