# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Search backend modules for mclaw web search."""

from mclaw.tools.search.router import execute_search
from mclaw.tools.search.dashscope_backend import search as dashscope_search
from mclaw.tools.search.profiles import (
    SEARCH_BACKEND_PROFILES,
    VALID_SEARCH_BACKENDS,
    get_search_backend_profile,
)
from mclaw.tools.search.tavily_backend import search as tavily_search

__all__ = [
    "SEARCH_BACKEND_PROFILES",
    "VALID_SEARCH_BACKENDS",
    "dashscope_search",
    "execute_search",
    "get_search_backend_profile",
    "tavily_search",
]
