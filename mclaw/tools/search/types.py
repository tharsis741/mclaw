# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared web-search request and response types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SearchRequest:
    """Backend-neutral search request passed from router to adapters."""
    query: str
    strategy: str = "turbo"
    freshness: int | None = None
    sites: str | None = None
    images: bool = False
    limit: int = 5


@dataclass(frozen=True)
class SearchResponse:
    """Backend-neutral response envelope preserving fallback metadata."""

    success: bool
    answer: str = ""
    sources: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""
    backend: str = ""
    backend_profile: dict[str, Any] = field(default_factory=dict)
    hint: str = ""
    fallback_from: str = ""
    fallback_error: str = ""

    @classmethod
    def from_mapping(cls, data: dict[str, Any], *, default_backend: str = "") -> "SearchResponse":
        """Normalize a backend dict into the router response contract."""
        success = bool(data.get("success"))
        raw_sources = data.get("sources")
        sources = (
            [dict(item) for item in raw_sources if isinstance(item, dict)]
            if isinstance(raw_sources, list)
            else []
        )
        raw_profile = data.get("_backend_profile")
        return cls(
            success=success,
            answer=str(data.get("answer") or (data.get("results") if success else "") or ""),
            sources=sources,
            error=str(data.get("error") or (data.get("results") if not success else "") or ""),
            backend=str(data.get("_backend") or default_backend or ""),
            backend_profile=dict(raw_profile) if isinstance(raw_profile, dict) else {},
            hint=str(data.get("_hint") or ""),
            fallback_from=str(data.get("_fallback_from") or ""),
            fallback_error=str(data.get("_fallback_error") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize the response using tool-facing metadata keys."""
        payload: dict[str, Any] = {
            "success": self.success,
            "answer": self.answer,
            "sources": self.sources,
            "_backend": self.backend,
            "_backend_profile": self.backend_profile,
            "_hint": self.hint,
        }
        if self.error:
            payload["error"] = self.error
        if self.fallback_from:
            payload["_fallback_from"] = self.fallback_from
        if self.fallback_error:
            payload["_fallback_error"] = self.fallback_error
        return payload
