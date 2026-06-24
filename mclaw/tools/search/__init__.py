# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Search backend modules for mclaw web search."""

from mclaw.tools.search.router import execute_search
from mclaw.tools.search.dashscope_backend import search as dashscope_search
from mclaw.tools.search.tavily_backend import search as tavily_search

__all__ = ["execute_search", "dashscope_search", "tavily_search"]
