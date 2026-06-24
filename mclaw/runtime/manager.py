# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Runtime detection and singleton access.

RuntimeManager chooses the host runtime once per process and exposes a cached
instance to tools that need command execution, filesystem policy, or feature
detection.
"""

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
