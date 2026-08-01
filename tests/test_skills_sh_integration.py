from __future__ import annotations

import importlib
import zipfile
from pathlib import Path

import pytest
import yaml

from mclaw.skills_hub import install_service, source_resolver
from mclaw.skills_hub.models import ExternalSkill
from mclaw.skills_hub.search import ClawHubSearcher, SkillsShSearcher
from mclaw.tools.skill_tools import read_tools

search_module = importlib.import_module("mclaw.skills_hub.search")


def test_skills_sh_search_maps_installable_github_results(monkeypatch) -> None:
    calls = []

    async def fake_get(url, *, params=None, timeout=10.0):
        calls.append((url, params, timeout))
        return {
            "skills": [
                {
                    "id": "anthropics/skills/pdf",
                    "skillId": "pdf",
                    "name": "pdf",
                    "installs": 170006,
                    "source": "anthropics/skills",
                },
                {
                    "id": "mintlify.com/mintlify",
                    "name": "mintlify",
                    "installs": 20,
                    "source": "mintlify.com",
                },
            ]
        }

    monkeypatch.setattr(search_module, "_get_json_async", fake_get)

    results = SkillsShSearcher().search("pdf", limit=10)

    assert len(results) == 1
    result = results[0]
    assert result.source == "skills_sh"
    assert result.slug == "pdf"
    assert result.catalog_id == "anthropics/skills/pdf"
    assert result.repository == "anthropics/skills"
    assert result.installs == 170006
    assert result.url == "https://skills.sh/anthropics/skills/pdf"
    assert result.install_source == result.url
    assert calls == [
        (
            "https://skills.sh/api/search",
            {"q": "pdf", "limit": "10"},
            10.0,
        )
    ]


def test_external_skill_serialization_uses_skills_sh_install_source() -> None:
    skill = ExternalSkill(
        source="skills_sh",
        name="pdf",
        description="",
        slug="pdf",
        catalog_id="anthropics/skills/pdf",
        repository="anthropics/skills",
        url="https://skills.sh/anthropics/skills/pdf",
        install_source="https://skills.sh/anthropics/skills/pdf",
        installs=170006,
    )

    payload = read_tools._serialize_external_skill(skill)

    assert payload["catalog_id"] == "anthropics/skills/pdf"
    assert payload["repository"] == "anthropics/skills"
    assert payload["installs"] == 170006
    assert payload["install_command"] == (
        "skill_manage(action='install_prepare', "
        "source='https://skills.sh/anthropics/skills/pdf')"
    )


def test_resolve_skills_sh_url_without_credentials() -> None:
    resolved = source_resolver.resolve_source(
        "https://www.skills.sh/anthropics/skills/pdf"
    )

    assert resolved.type == "skills_sh"
    assert resolved.owner == "anthropics"
    assert resolved.repo == "skills"
    assert resolved.slug == "pdf"
    assert resolved.catalog_id == "anthropics/skills/pdf"
    assert resolved.fetch_url == "https://github.com/anthropics/skills/archive/HEAD.zip"


@pytest.mark.parametrize(
    "url",
    [
        "https://skills.sh/anthropics/skills",
        "https://skills.sh/anthropics/skills/pdf/extra",
        "https://skills.sh/anthropics/skills/%2E%2E",
    ],
)
def test_resolve_skills_sh_rejects_invalid_catalog_urls(url) -> None:
    with pytest.raises(source_resolver.SourceResolutionError):
        source_resolver.resolve_source(url)


def test_materialize_skills_sh_selects_only_requested_skill(tmp_path, monkeypatch) -> None:
    archive = tmp_path / "skills.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(
            "skills-main/skills/pdf/SKILL.md",
            "---\nname: pdf\ndescription: Work with PDFs.\n---\n\n# PDF\n",
        )
        zf.writestr("skills-main/skills/pdf/scripts/check.py", "print('ok')\n")
        zf.writestr(
            "skills-main/skills/docx/SKILL.md",
            "---\nname: docx\ndescription: Work with Word files.\n---\n\n# DOCX\n",
        )

    monkeypatch.setattr(source_resolver, "_download_archive", lambda *_args, **_kwargs: archive)
    target = tmp_path / "materialized"
    resolved = source_resolver.ResolvedSource(
        type="skills_sh",
        original="https://skills.sh/anthropics/skills/pdf",
        fetch_url="https://example.invalid/skills.zip",
        owner="anthropics",
        repo="skills",
        slug="pdf",
        catalog_id="anthropics/skills/pdf",
    )

    source_resolver.materialize_source(resolved, target)

    assert (target / "SKILL.md").is_file()
    assert (target / "scripts" / "check.py").is_file()
    assert not (target / "skills" / "docx").exists()


def test_skills_sh_package_selection_rejects_ambiguous_name(tmp_path) -> None:
    for folder in ("first", "second"):
        package = tmp_path / "repo-main" / folder
        package.mkdir(parents=True)
        (package / "SKILL.md").write_text(
            "---\nname: pdf\ndescription: Work with PDFs.\n---\n",
            encoding="utf-8",
        )

    with pytest.raises(source_resolver.SourceResolutionError, match="ambiguous"):
        source_resolver._find_skill_package_root(tmp_path, "pdf")


def test_install_prepare_accepts_skills_sh_provenance(tmp_path, monkeypatch) -> None:
    drafting_root = tmp_path / "drafts"
    drafting_id = "skill_drafting_20260801_120000_deadbeef"
    resolved = source_resolver.ResolvedSource(
        type="skills_sh",
        original="https://skills.sh/anthropics/skills/pdf",
        fetch_url="https://example.invalid/skills.zip",
        owner="anthropics",
        repo="skills",
        slug="pdf",
        catalog_id="anthropics/skills/pdf",
    )

    def fake_materialize(_resolved, target, **_kwargs):
        target.mkdir(parents=True)
        (target / "SKILL.md").write_text(
            "---\nname: pdf\ndescription: Work with PDF documents.\n---\n\n"
            "# PDF\n\nFollow the user's document request.\n",
            encoding="utf-8",
        )

    monkeypatch.setattr(install_service, "ensure_runtime_roots", lambda: None)
    monkeypatch.setattr(install_service, "get_skill_drafting_dir", lambda: drafting_root)
    monkeypatch.setattr(install_service, "_drafting_id", lambda: drafting_id)
    monkeypatch.setattr(install_service, "resolve_source", lambda *_args, **_kwargs: resolved)
    monkeypatch.setattr(install_service, "materialize_source", fake_materialize)

    result = install_service.install_prepare(resolved.original)

    assert result["requires_confirmation"] is True
    assert result["source"] == {"type": "skills_sh", "original": resolved.original}
    sidecar = yaml.safe_load(
        (drafting_root / drafting_id / "skill" / "mclaw_skill.yaml").read_text(encoding="utf-8")
    )
    assert sidecar["source"] == {"type": "skills_sh", "original": resolved.original}


@pytest.mark.parametrize(
    "url",
    [
        "https://clawhub.ai/demo/weather",
        "https://clawhub.ai/demo/skills/weather",
    ],
)
def test_clawhub_manual_install_supports_legacy_and_current_urls(url, monkeypatch) -> None:
    detail_calls = []

    def fake_detail(_self, slug, **kwargs):
        detail_calls.append((slug, kwargs.get("owner_handle")))
        return {
            "skill": {"slug": "weather"},
            "owner": {"handle": "demo"},
        }

    monkeypatch.setattr(
        ClawHubSearcher,
        "get_skill_detail",
        fake_detail,
    )

    resolved = source_resolver.resolve_source(url)

    assert resolved.type == "clawhub"
    assert resolved.owner == "demo"
    assert resolved.slug == "weather"
    assert resolved.fetch_url.endswith("/download?slug=weather&ownerHandle=demo")
    assert detail_calls == [("weather", "demo")]


def test_clawhub_detail_lookup_is_owner_qualified(monkeypatch) -> None:
    calls = []

    async def fake_get(url, *, params=None, timeout=10.0):
        calls.append((url, params, timeout))
        return {
            "skill": {"slug": "weather"},
            "owner": {"handle": "steipete"},
        }

    monkeypatch.setattr(search_module, "_get_json_async", fake_get)

    detail = ClawHubSearcher().get_skill_detail("weather", owner_handle="steipete")

    assert detail["owner"]["handle"] == "steipete"
    assert calls == [
        (
            "https://clawhub.ai/api/v1/skills/weather",
            {"owner": "steipete"},
            10.0,
        )
    ]


def test_clawhub_search_enrichment_is_owner_qualified(monkeypatch) -> None:
    calls = []

    async def fake_get(url, *, params=None, timeout=10.0):
        calls.append((url, params))
        if url.endswith("/search"):
            return {
                "results": [
                    {
                        "slug": "weather",
                        "displayName": "Weather",
                        "ownerHandle": "steipete",
                        "canonicalUrl": "/steipete/skills/weather",
                    }
                ]
            }
        return {
            "skill": {"slug": "weather", "stats": {"downloads": 10}},
            "owner": {"handle": "steipete"},
        }

    monkeypatch.setattr(search_module, "_get_json_async", fake_get)

    results = ClawHubSearcher().search("weather")

    assert results[0].url == "https://clawhub.ai/steipete/skills/weather"
    assert results[0].downloads == 10
    assert calls[1] == (
        "https://clawhub.ai/api/v1/skills/weather",
        {"owner": "steipete"},
    )
