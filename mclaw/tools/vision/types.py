# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared types for vision analysis providers."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VisionCredentials:
    """Resolved provider credentials and validation state for one vision call."""
    provider: str
    api_key: str
    base_url: str
    model: str
    env_var: str = ""
    unsupported_reason: str = ""

    @property
    def available(self) -> bool:
        return bool(self.api_key and not self.unsupported_reason)
