# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import shutil
from pathlib import Path
from urllib.parse import urlparse

import pytest

from mclaw.skills_hub import builtin_sync, skill_store
from mclaw.skills_hub.evolution_store import write_default
from mclaw.skills_hub.skill_yaml_store import build_skill_yaml, read_skill_yaml, write_skill_yaml


BUNDLED_ROOT = Path(__file__).parents[1] / "mclaw" / "skills"
BUNDLED_NAMES = sorted(path.parent.name for path in BUNDLED_ROOT.glob("*/mclaw_skill.yaml"))


@pytest.mark.parametrize("name", BUNDLED_NAMES)
def test_bundled_install_preserves_verified_source_and_license(tmp_path, monkeypatch, name):
    monkeypatch.setenv("MCLAW_HOME", str(tmp_path))
    monkeypatch.setenv("MCLAW_BUILTIN_SKILLS", str(BUNDLED_ROOT))
    source = read_skill_yaml(BUNDLED_ROOT / name)["source"]
    for key in ("original", "license_url"):
        url = urlparse(source[key])
        assert url.scheme == "https" and url.hostname == "github.com"
    assert source["type"] == "github"
    assert source["author"]
    license_markers = {"MIT": b"MIT License", "Apache-2.0": b"Apache License"}
    assert source["license"] in license_markers
    license_bytes = (BUNDLED_ROOT / name / source["license_file"]).read_bytes()
    assert license_markers[source["license"]] in license_bytes
    if source.get("copyright"):
        assert source["copyright"].encode("utf-8") in license_bytes

    result = builtin_sync.sync_selected_skills([name], quiet=True)
    assert result["copied"] == [name]
    installed = tmp_path / "skills" / name
    data = read_skill_yaml(installed)
    assert data["source"] == source
    assert data["created_by"] == "bundled"
    assert (installed / source["license_file"]).read_bytes() == license_bytes
    assert skill_store.get_skill_record(name)["source"] == source
    assert builtin_sync.list_installed_builtin_skills() == [name]


@pytest.fixture
def example_builtin(tmp_path, monkeypatch):
    root = tmp_path / "bundled"
    source = root / "group" / "example"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text(
        "---\nname: example\ndescription: Example skill\n---\n\nProduce a report.\n",
        encoding="utf-8",
    )
    (source / "LICENSE").write_text("Example upstream license\n", encoding="utf-8")
    data = build_skill_yaml(
        name="example", short_description="Example skill", source_type="github",
        source_original="https://github.com/example/skills/tree/main/example",
        actor="install", status="enabled",
    )
    data["source"].update(author="Example author", license="MIT", license_file="LICENSE")
    write_skill_yaml(source, data)
    write_default(source)
    monkeypatch.setenv("MCLAW_BUILTIN_SKILLS", str(root))
    monkeypatch.setenv("MCLAW_HOME", str(tmp_path / "home"))
    return source, tmp_path / "home" / "skills" / "example"


@pytest.mark.parametrize("modified", [False, True])
@pytest.mark.parametrize("has_manifest", [False, True])
def test_legacy_sync_migrates_provenance_and_retains_user_data(example_builtin, modified, has_manifest):
    source, installed = example_builtin
    shutil.copytree(source, installed, ignore=shutil.ignore_patterns("LICENSE"))
    metadata = build_skill_yaml(
        name="example", short_description="My own description", source_type="bundled",
        source_original="group/example", actor="bundled", status="enabled",
        created_at="2026-01-01T00:00:00+00:00",
    )
    write_skill_yaml(installed, metadata)
    history = '{"adaptation_summary": ["User preferences"], "user_preferences": [], "known_failures": [], "runtime_notes": []}\n'
    (installed / "skill_evolution.json").write_text(history, encoding="utf-8")
    old_hash = builtin_sync._dir_hash(installed, exclude_generated_sidecars=True)
    if has_manifest:
        (installed.parent / ".builtin_manifest").write_text(f"example:{old_hash}\n", encoding="utf-8")
    if modified:
        with (installed / "SKILL.md").open("a", encoding="utf-8") as stream:
            stream.write("\nUser's project requirements.\n")
    body = (installed / "SKILL.md").read_bytes()

    assert builtin_sync.list_installed_builtin_skills() == ["example"]
    result = builtin_sync.sync_selected_skills(["example"], quiet=True)
    assert result["updated"] == (["example"] if has_manifest and not modified else [])
    data = read_skill_yaml(installed)
    assert data["source"] == read_skill_yaml(source)["source"]
    assert data["short_description"] == metadata["short_description"]
    assert data["created_at"] == metadata["created_at"]
    assert data["created_by"] == "bundled"
    assert (installed / "SKILL.md").read_bytes() == body
    assert (installed / "skill_evolution.json").read_text(encoding="utf-8") == history
    assert (installed / "LICENSE").read_bytes() == (source / "LICENSE").read_bytes()
    assert builtin_sync.list_installed_builtin_skills() == ["example"]

    second = builtin_sync.sync_selected_skills(["example"], quiet=True)
    assert second["user_modified"] == (["example"] if modified else [])
    assert second["skipped"] == (0 if modified else 1)
    assert read_skill_yaml(installed) == data


def test_upstream_provenance_still_identifies_builtin_without_sync_manifest(example_builtin):
    _, installed = example_builtin
    builtin_sync.sync_selected_skills(["example"], quiet=True)
    (installed.parent / ".builtin_manifest").unlink()
    data = read_skill_yaml(installed)
    del data["source"]["author"]
    data["source"]["user_note"] = "Keep this note"
    write_skill_yaml(installed, data)

    result = builtin_sync.sync_selected_skills(["example"], quiet=True)
    assert result["skipped"] == 1
    assert not result["user_modified"]
    source = read_skill_yaml(installed)["source"]
    assert source["author"] == "Example author"
    assert source["user_note"] == "Keep this note"


@pytest.mark.parametrize("modified", [False, True])
def test_new_bundle_revision_only_replaces_provenance_when_content_updates(example_builtin, modified):
    source, installed = example_builtin
    builtin_sync.sync_selected_skills(["example"], quiet=True)
    old_source = read_skill_yaml(installed)["source"]
    if modified:
        (installed / "SKILL.md").write_text("User's customized instructions\n", encoding="utf-8")
    data = read_skill_yaml(source)
    data["source"]["original"] = "https://github.com/example/skills/tree/new-revision/example"
    data["source"]["revision"] = "new-revision"
    write_skill_yaml(source, data)
    with (source / "SKILL.md").open("a", encoding="utf-8") as stream:
        stream.write("\nNew bundled instructions.\n")

    assert builtin_sync.list_installed_builtin_skills() == ["example"]
    result = builtin_sync.sync_selected_skills(["example"], quiet=True)
    assert result["updated"] == ([] if modified else ["example"])
    assert result["user_modified"] == (["example"] if modified else [])
    assert read_skill_yaml(installed)["source"] == (old_source if modified else data["source"])


def test_independently_installed_same_upstream_is_not_claimed_as_builtin(example_builtin):
    source, installed = example_builtin
    shutil.copytree(source, installed)
    original = (installed / "mclaw_skill.yaml").read_bytes()
    (installed / "LICENSE").write_text("User's license notice", encoding="utf-8")

    result = builtin_sync.sync_selected_skills(["example"], quiet=True)
    assert result["user_modified"] == ["example"]
    assert builtin_sync.list_installed_builtin_skills() == []
    assert (installed / "mclaw_skill.yaml").read_bytes() == original
    assert (installed / "LICENSE").read_text(encoding="utf-8") == "User's license notice"


def test_setup_preselects_installed_skill_with_upstream_source(example_builtin, monkeypatch):
    from mclaw.cli import main as cli_main
    from mclaw.cli.tui import console, selection_prompt

    builtin_sync.sync_selected_skills(["example"], quiet=True)
    defaults = []
    monkeypatch.setattr(
        selection_prompt, "prompt_builtin_skill_selection",
        lambda _skills, **kwargs: defaults.append(kwargs["default_selected"]) or [],
    )
    monkeypatch.setattr(console, "print_plain", lambda *_args, **_kwargs: None)
    cli_main._run_setup_builtin_skill_selection()
    assert defaults == [["example"]]
