# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Maintained behavior profiles for ``web_search`` backends."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SearchBackendProfile:
    """Provider behavior hidden behind the shared answer/sources contract."""

    name: str
    display_name: str
    protocol: str
    answer_format: str
    source_format: str
    source_snippets: bool
    default_mode: str
    status_description_zh: str

    def tool_metadata(self) -> dict[str, object]:
        return {
            "protocol": self.protocol,
            "answer_format": self.answer_format,
            "source_format": self.source_format,
            "source_snippets": self.source_snippets,
        }


SEARCH_BACKEND_PROFILES: dict[str, SearchBackendProfile] = {
    "tavily": SearchBackendProfile(
        name="tavily",
        display_name="Tavily",
        protocol="tavily_search_api",
        answer_format="generated_summary",
        source_format="structured_results",
        source_snippets=True,
        default_mode="basic",
        status_description_zh="生成答案 + 带摘要的结构化来源；默认 basic",
    ),
    "dashscope": SearchBackendProfile(
        name="dashscope",
        display_name="DashScope",
        protocol="dashscope_native",
        answer_format="generated_summary",
        source_format="structured_results",
        source_snippets=False,
        default_mode="turbo",
        status_description_zh="Qwen 生成答案 + 结构化来源链接（无摘要）；默认 turbo",
    ),
}

VALID_SEARCH_BACKENDS = tuple(SEARCH_BACKEND_PROFILES)


def get_search_backend_profile(name: str) -> SearchBackendProfile:
    normalized = str(name or "").strip().lower()
    try:
        return SEARCH_BACKEND_PROFILES[normalized]
    except KeyError as exc:
        available = ", ".join(VALID_SEARCH_BACKENDS)
        raise KeyError(f"unknown search backend {name!r}; available: {available}") from exc
