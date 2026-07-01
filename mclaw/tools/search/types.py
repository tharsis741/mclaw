# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared web-search request and response types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SearchRequest:
    """Backend-neutral search request passed from router to adapters."""
    query: str
    strategy: str = "turbo"
    freshness: int | None = None
    sites: str | None = None
    images: bool = False


@dataclass(frozen=True)
class SearchResponse:
    """Backend-neutral response envelope preserving fallback metadata."""
    success: bool
    results: str
    backend: str = ""
    hint: str = ""
    fallback_from: str = ""
    fallback_error: str = ""

    @classmethod
    def from_mapping(cls, data: dict[str, Any], *, default_backend: str = "") -> "SearchResponse":
        """Normalize a backend dict into the router response contract."""
        return cls(
            success=bool(data.get("success")),
            results=str(data.get("results") or ""),
            backend=str(data.get("_backend") or default_backend or ""),
            hint=str(data.get("_hint") or ""),
            fallback_from=str(data.get("_fallback_from") or ""),
            fallback_error=str(data.get("_fallback_error") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize the response using tool-facing metadata keys."""
        payload: dict[str, Any] = {
            "success": self.success,
            "results": self.results,
            "_backend": self.backend,
            "_hint": self.hint,
        }
        if self.fallback_from:
            payload["_fallback_from"] = self.fallback_from
        if self.fallback_error:
            payload["_fallback_error"] = self.fallback_error
        return payload
