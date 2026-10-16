# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import re
from pathlib import Path

import pytest

from mclaw.agent.prompt_builder import build_skills_system_prompt
from mclaw.agent.skill_utils import parse_frontmatter
from mclaw.application.skills import SkillService
from mclaw.cli.skill_registry import SkillRegistry
from mclaw.skills_hub import builtin_sync, skill_store
from mclaw.skills_hub.skill_yaml_store import read_skill_yaml


BUNDLED_ROOT = Path(__file__).parents[1] / "mclaw" / "skills"
PRODUCTIVITY_SKILLS = ("internal-comms", "humanizer-zh", "knowledge-synthesis")


@pytest.mark.parametrize("name", PRODUCTIVITY_SKILLS)
def test_productivity_skill_discovery_references_and_task_snapshot(tmp_path, monkeypatch, name):
    monkeypatch.setenv("MCLAW_HOME", str(tmp_path))
    monkeypatch.setenv("MCLAW_BUILTIN_SKILLS", str(BUNDLED_ROOT))
    monkeypatch.setattr(SkillService, "_disabled", staticmethod(lambda: set()))

    catalog = {entry["name"] for entry in builtin_sync.list_builtin_skills()}
    assert set(PRODUCTIVITY_SKILLS) <= catalog
    result = builtin_sync.sync_selected_skills([name], quiet=True)
    assert result["copied"] == [name]
    assert builtin_sync.list_installed_builtin_skills() == [name]

    registry = SkillRegistry()
    registry.refresh()
    assert registry.get_skill(name).path == tmp_path / "skills" / name / "SKILL.md"
    # Text-only synthesis and editing must remain discoverable without web tools.
    prompt = build_skills_system_prompt(available_tools={"skill_view"}, config={})
    assert name in prompt
    hidden = build_skills_system_prompt(
        available_tools={"skill_view"}, config={"skills": {"disabled": [name]}},
    )
    assert name not in hidden

    service = SkillService(tmp_path)
    detail = service.detail(name)
    metadata = read_skill_yaml(BUNDLED_ROOT / name)
    assert detail["source"] == metadata["source"]
    assert metadata["source"]["revision"] in metadata["source"]["original"]
    snapshot, = service.snapshots([name])
    assert snapshot["source"] == metadata["source"]
    assert snapshot["version"] == detail["version"]

    view = skill_store.view_skill(name)
    frontmatter, _ = parse_frontmatter(view["content"])
    assert frontmatter["name"] == name
    assert view["content"] in snapshot["content"]
    references = re.findall(r"\]\(((?:examples|references)/[^)]+\.md)\)", view["content"])
    assert references
    for relative in [*references, "LICENSE", "NOTICE.md"]:
        expected = (BUNDLED_ROOT / name / relative).read_text(encoding="utf-8")
        assert skill_store.view_skill(name, file_path=relative)["content"] == expected
        assert service.file(name, relative)["content"] == expected

    repeated = builtin_sync.sync_selected_skills([name], quiet=True)
    assert repeated["skipped"] == 1
    assert not repeated["copied"]
    assert not repeated["updated"]
