# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

import pytest

from mclaw.agent.skill_utils import parse_frontmatter
from mclaw.skills_hub import builtin_sync, skill_store


BUNDLED_ROOT = Path(__file__).parents[1] / "mclaw" / "skills"
FRONTEND_SKILLS = {
    "design-taste-frontend": (
        "references/layout.md",
        "assets/base.css",
        "library/directions.json",
        "library/sections.json",
        "scripts/draw.mjs",
        "scripts/lib/cdp.mjs",
        "scripts/lib/probe.mjs",
    ),
}
RETIRED_SKILLS = {
    "data-analyzer", "file-organizer", "storage-analyzer",
    "local-camera", "kaihong-robot-dog", "popular-web-designs",
    "impeccable",
}


@pytest.mark.parametrize("name", FRONTEND_SKILLS)
def test_frontend_skill_installs_with_resources_and_resolved_paths(tmp_path, monkeypatch, name):
    monkeypatch.setenv("MCLAW_HOME", str(tmp_path))
    monkeypatch.setenv("MCLAW_BUILTIN_SKILLS", str(BUNDLED_ROOT))

    catalog = {entry["name"] for entry in builtin_sync.list_builtin_skills()}
    assert set(FRONTEND_SKILLS) <= catalog
    assert RETIRED_SKILLS.isdisjoint(catalog)

    result = builtin_sync.sync_selected_skills([name], quiet=True)
    assert result["copied"] == [name]
    installed = tmp_path / "skills" / name
    for relative in (*FRONTEND_SKILLS[name], "LICENSE", "NOTICE.md"):
        assert (installed / relative).read_bytes() == (BUNDLED_ROOT / name / relative).read_bytes()

    view = skill_store.view_skill(name)
    frontmatter, _ = parse_frontmatter(view["content"])
    assert frontmatter["name"] == name
    assert "{{SKILL_DIR}}" not in view["content"]
    assert str(installed.resolve()) in view["content"]
    reference = skill_store.view_skill(name, file_path=FRONTEND_SKILLS[name][0])
    assert reference["content"]
    assert "{{SKILL_DIR}}" not in reference["content"]


def test_frontend_sync_preserves_user_edits_and_retired_installed_skills(tmp_path, monkeypatch):
    monkeypatch.setenv("MCLAW_HOME", str(tmp_path))
    monkeypatch.setenv("MCLAW_BUILTIN_SKILLS", str(BUNDLED_ROOT))
    skills_dir = tmp_path / "skills"
    retired = skills_dir / "file-organizer"
    retired.mkdir(parents=True)
    user_file = retired / "SKILL.md"
    user_file.write_text("User's existing file organizer instructions\n", encoding="utf-8")
    (skills_dir / ".builtin_manifest").write_text("file-organizer:previous-hash\n", encoding="utf-8")

    first = builtin_sync.sync_selected_skills(list(FRONTEND_SKILLS), quiet=True)
    assert first["cleaned"] == ["file-organizer"]
    assert user_file.read_text(encoding="utf-8") == "User's existing file organizer instructions\n"

    customized = skills_dir / "design-taste-frontend" / "SKILL.md"
    edited = customized.read_text(encoding="utf-8") + "\nUser's brand requirements.\n"
    customized.write_text(edited, encoding="utf-8")
    second = builtin_sync.sync_selected_skills(list(FRONTEND_SKILLS), quiet=True)
    assert second["user_modified"] == ["design-taste-frontend"]
    assert customized.read_text(encoding="utf-8") == edited
    assert not second["copied"]
