# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Skill Hub data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ExternalSkill:
    """Catalog result shape shared by ClawHub search and UI serialization."""

    name: str
    description: str
    source: str
    slug: str = ""
    url: str = ""
    author: str = ""
    downloads: int = 0
    stars: int = 0
    platforms: Optional[List[str]] = None
    frontmatter: Dict[str, Any] = field(default_factory=dict)
