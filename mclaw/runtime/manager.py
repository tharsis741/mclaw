"""Runtime detection and singleton access."""

from __future__ import annotations

import sys
from typing import ClassVar

from mclaw.runtime.base import Runtime
from mclaw.runtime.bootstrap import BootstrapPathResolver
from mclaw.runtime.kaihong import KaihongRuntime
from mclaw.runtime.linux import LinuxRuntime
from mclaw.runtime.windows import WindowsRuntime


class RuntimeManager:
    _current: ClassVar[Runtime | None] = None

    @classmethod
    def detect(cls) -> str:
        if sys.platform == "win32":
            return "windows"
        if BootstrapPathResolver.is_kaihong_host():
            return "kaihong"
        return "linux"

    @classmethod
    def create(cls, kind: str | None = None) -> Runtime:
        selected = (kind or cls.detect()).lower()
        if selected == "windows":
            return WindowsRuntime()
        if selected == "kaihong":
            return KaihongRuntime()
        if selected in {"linux", "posix", "darwin"}:
            return LinuxRuntime()
        raise ValueError(f"Unsupported runtime kind: {kind}")

    @classmethod
    def current(cls, config: dict | None = None) -> Runtime:
        if cls._current is None:
            cls._current = cls.create()
        return cls._current

    @classmethod
    def reset(cls) -> None:
        cls._current = None
