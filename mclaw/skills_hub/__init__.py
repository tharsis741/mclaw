# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Skill 2.0 service package for external Skill discovery."""

from __future__ import annotations

from typing import List

from mclaw.skills_hub import install_service, skill_store
from mclaw.skills_hub.models import ExternalSkill
from mclaw.skills_hub.search import search_all


def search(query: str, limit: int = 10) -> List[ExternalSkill]:
    """Search ClawHub for Skills."""
    return search_all(query, limit=limit)


__all__ = [
    "ExternalSkill",
    "install_service",
    "search",
    "skill_store",
]
