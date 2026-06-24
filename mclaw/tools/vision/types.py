"""Shared types for vision analysis providers."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VisionCredentials:
    provider: str
    api_key: str
    base_url: str
    model: str
    env_var: str = ""
    unsupported_reason: str = ""

    @property
    def available(self) -> bool:
        return bool(self.api_key and not self.unsupported_reason)
