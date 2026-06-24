"""Runtime path policy and workspace roots."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class PathDecision:
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
    try:
        path.relative_to(root)
        return True
    except ValueError:
        try:
            return os.path.commonpath([_normcase(str(path)), _normcase(str(root))]) == _normcase(str(root))
        except ValueError:
            return False


class PathPolicy:
    """Classify paths and make read/write/search/list/execute decisions."""

    def __init__(
        self,
        *,
        kind: str,
        mclaw_home: Path,
        workspace_root: Path,
        workspace_roots: Iterable[Path] = (),
        workspace_prefixes: Iterable[str] = (),
        exchange_roots: Iterable[Path] = (),
        runtime_roots: Iterable[Path] = (),
        system_roots: Iterable[Path] = (),
        device_roots: Iterable[Path] = (),
        tmp_root: Path | None = None,
    ) -> None:
        self.kind = kind
        self.mclaw_home = self._resolve_root(mclaw_home)
        self.workspace_root = self._resolve_root(workspace_root)
        self.tmp_root = self._resolve_root(tmp_root or self.mclaw_home / "tmp")
        self.workspace_roots = tuple(self._resolve_root(path) for path in workspace_roots)
        self.workspace_prefixes = tuple(str(Path(prefix).expanduser()) for prefix in workspace_prefixes)
        self.exchange_roots = tuple(self._resolve_root(path) for path in exchange_roots)
        self.runtime_roots = tuple(self._resolve_root(path) for path in runtime_roots)
        self.system_roots = tuple(self._resolve_root(path) for path in system_roots)
        self.device_roots = tuple(self._resolve_root(path) for path in device_roots)
        self._internal_roots = (
            self.mclaw_home / "logs",
            self.mclaw_home / "sessions",
            self.mclaw_home / "checkpoints",
            self.process_log_root(),
        )

    @staticmethod
    def _resolve_root(path: Path) -> Path:
        try:
            return Path(path).expanduser().resolve()
        except OSError:
            return Path(path).expanduser().absolute()

    def normalize(self, path_value: str | os.PathLike, base: str | os.PathLike | None = None) -> Path:
        value = os.path.expandvars(str(path_value or "")).strip().strip('"').strip("'")
        if not value:
            value = "."
        path = Path(value).expanduser()
        if not path.is_absolute() and base:
            path = Path(base).expanduser() / path
        try:
            return path.resolve()
        except OSError:
            return path.absolute()

    def default_workspace(self) -> Path:
        return self.workspace_root

    def delegation_root(self) -> Path:
        return self.mclaw_home / "delegations"

    def session_root(self, session_id: str) -> Path:
        safe = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in str(session_id or "session"))
        return self.mclaw_home / "workspace" / safe

    def process_log_root(self) -> Path:
        return self.mclaw_home / "process_logs"

    def temp_root(self) -> Path:
        return self.tmp_root

    def is_within(self, path: Path, root: Path) -> bool:
        return is_relative_to(path, self._resolve_root(root))

    def is_runtime_internal_path(self, path_value: str | os.PathLike) -> bool:
        path = self.normalize(path_value)
        return any(is_relative_to(path, root) for root in self._internal_roots)

    def classify(self, path: Path) -> str:
        workspace_roots = (self.workspace_root, self.delegation_root(), self.tmp_root, *self.workspace_roots)
        if any(is_relative_to(path, root) for root in workspace_roots):
            return "workspace"
        path_text = str(path)
        if any(path_text == prefix or path_text.startswith(prefix) for prefix in self.workspace_prefixes):
            return "workspace"
        if any(is_relative_to(path, root) for root in self.exchange_roots):
            return "exchange"
        if any(is_relative_to(path, root) for root in self.runtime_roots):
            return "runtime"
        if any(is_relative_to(path, root) for root in self.device_roots):
            return "device"
        if any(is_relative_to(path, root) for root in self.system_roots):
            return "system"
        home = Path.home().expanduser()
        try:
            if is_relative_to(path, home):
                return "home"
        except OSError:
            pass
        tmp = Path(tempfile.gettempdir()).expanduser()
        if is_relative_to(path, tmp):
            return "tmp"
        return "unknown"

    def check(
        self,
        action: str,
        path_value: str | os.PathLike,
        *,
        base: str | os.PathLike | None = None,
        maintenance: bool = False,
    ) -> PathDecision:
        path = self.normalize(path_value, base=base)
        action = str(action or "read").lower()
        scope = self.classify(path)

        if scope in {"workspace", "exchange", "home", "tmp", "unknown"}:
            return PathDecision(str(path_value), path, action, scope, True, "allowed by host runtime policy")

        if scope == "runtime":
            if action == "write" and not maintenance:
                return PathDecision(str(path_value), path, action, scope, False, "runtime roots are read-only outside maintenance mode")
            return PathDecision(str(path_value), path, action, scope, True, "runtime diagnostic/read access")

        if scope == "system":
            if action in {"write", "execute"}:
                return PathDecision(str(path_value), path, action, scope, False, "system path mutation is denied")
            return PathDecision(str(path_value), path, action, scope, True, "system diagnostic read/list access")

        if scope == "device":
            if self.kind == "kaihong" and is_relative_to(path, Path("/proc/self")) and action in {"read", "list", "search"}:
                return PathDecision(str(path_value), path, action, scope, True, "Kaihong /proc/self diagnostic access")
            return PathDecision(str(path_value), path, action, scope, False, "device path is restricted")

        return PathDecision(str(path_value), path, action, scope, False, "unhandled path scope")
