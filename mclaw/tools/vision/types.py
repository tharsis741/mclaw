# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared types for vision analysis providers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class VisionInputError(ValueError):
    """An actionable image input failure that can be shown by entrypoints."""

    def __init__(self, message: str, code: str = "VISION_INPUT_FAILED") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class VisionCredentials:
    """Resolved provider credentials and validation state for one vision call."""
    provider: str
    api_key: str = field(repr=False)
    base_url: str
    model: str
    env_var: str = ""
    unsupported_reason: str = ""
    runtime: Any = field(default=None, repr=False)

    @property
    def available(self) -> bool:
        required = self.runtime is None or self.runtime.profile.credential_required
        return bool((self.api_key or not required) and not self.unsupported_reason)
