# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared web-search request and response types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SearchRequest:
    query: str
    strategy: str = "turbo"
    freshness: int | None = None
    sites: str | None = None
    images: bool = False


@dataclass(frozen=True)
class SearchResponse:
    success: bool
    results: str
    backend: str = ""
    hint: str = ""

    @classmethod
    def from_mapping(cls, data: dict[str, Any], *, default_backend: str = "") -> "SearchResponse":
        return cls(
            success=bool(data.get("success")),
            results=str(data.get("results") or ""),
            backend=str(data.get("_backend") or default_backend or ""),
            hint=str(data.get("_hint") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "results": self.results,
            "_backend": self.backend,
            "_hint": self.hint,
        }
