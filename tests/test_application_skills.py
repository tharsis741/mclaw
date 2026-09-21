# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.testclient import TestClient

from mclaw.application.sessions import ApplicationError
from mclaw.application.skills import SkillService, apply_skill_snapshots
from mclaw.application.task_queue import apply_pending_supplement
from mclaw.skills_hub import skill_store
from mclaw.skills_hub.paths import get_enabled_skills_dir, get_skill_drafting_dir
from mclaw.state import SessionDB
from mclaw.web.skills import build_skill_routes


def document(name="demo", body="Read the task and produce a concise useful answer."):
    return f"---\nname: {name}\ndescription: A local example skill\n---\n\n# Instructions\n\n{body}\n"


@pytest.fixture
def skills(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("MCLAW_HOME", str(home))
    return SkillService(home)


def test_skill_crud_versions_files_and_disabled_selection(skills):
    first = skills.create("demo", "示例技能", document())
    assert first["enabled"] and first["metadata"]["name"] == "demo"
    assert skills.list("示例")["skills"][0]["name"] == "demo"
    assert skills.list("unmatched")["skills"] == []
    saved = skills.write_file("demo", "references/info.md", "Useful reference", first["version"])
    assert saved["editable"] and saved["content"] == "Useful reference"
    with pytest.raises(ApplicationError, match="重新读取"):
        skills.update("demo", {"content": document(body="new instructions"), "version": first["version"]})
    with pytest.raises(ValueError):
        skills.write_file("demo", "mclaw_skill.yaml", "changed", saved["version"])
    with pytest.raises(ValueError):
        skills.file("demo", "../other")
    assert skills.file("demo", "mclaw_skill.yaml")["editable"] is False
    disabled = skills.update("demo", {"enabled": False})
    assert disabled["enabled"] is False
    with pytest.raises(ApplicationError, match="停用"):
        skills.snapshots(["demo"])
    skills.update("demo", {"enabled": True})
    updated = skills.update("demo", {"content": document(body="Updated useful instructions."), "version": disabled["version"]})
    assert "Updated useful" in updated["content"]
    assert skills.delete("demo", updated["version"])["success"]
    assert skills.list()["skills"] == []


def test_invalid_or_blocked_edit_preserves_old_body(skills, monkeypatch):
    first = skills.create("demo", "示例技能", document())
    with pytest.raises(ValueError):
        skills.update("demo", {"content": "invalid frontmatter", "version": first["version"]})
    monkeypatch.setattr(skill_store, "review_skill_package", lambda *_a, **_k: {"verdict": "dangerous", "summary": "test blocked"})
    with pytest.raises(ValueError, match="blocked"):
        skills.update("demo", {"content": document(body="Updated body."), "version": first["version"]})
    assert skills.detail("demo")["content"] == first["content"]


def test_description_edit_preserves_body_and_history_and_updates_shared_catalog(skills):
    first = skills.create("demo", "示例技能", document())
    root = get_enabled_skills_dir() / "demo"
    body = root / "SKILL.md"
    original_body = body.read_bytes()
    original_mtime = body.stat().st_mtime_ns
    original_history = (root / "skill_evolution.json").read_bytes()
    assert skill_store.list_skills()[0]["short_description"] == "示例技能"
    updated = skills.update("demo", {"description": "  整理项目资料  ", "version": first["version"]})
    assert updated["description"] == "整理项目资料"
    assert updated["version"] != first["version"]
    assert updated["metadata"]["source"] == first["metadata"]["source"]
    assert updated["metadata"]["created_at"] == first["metadata"]["created_at"]
    assert updated["metadata"]["updated_by"] == "user"
    assert body.read_bytes() == original_body
    assert body.stat().st_mtime_ns == original_mtime
    assert (root / "skill_evolution.json").read_bytes() == original_history
    assert skills.list("整理项目")["skills"][0]["description"] == "整理项目资料"
    assert skill_store.list_skills()[0]["short_description"] == "整理项目资料"
    with pytest.raises(ApplicationError) as error:
        skills.update("demo", {"description": "过期描述", "version": first["version"]})
    assert error.value.code == "SKILL_VERSION_CONFLICT"
    assert skills.detail("demo")["description"] == "整理项目资料"


@pytest.mark.parametrize("description", ["", " \n ", None, 123, {}, []])
def test_description_requires_nonempty_text_before_any_mutation(skills, description):
    first = skills.create("demo", "示例技能", document())
    with pytest.raises(ApplicationError) as error:
        skills.update("demo", {"description": description, "content": document(body="Changed."),
                               "version": first["version"]})
    assert error.value.code == "INVALID_SKILL_DESCRIPTION"
    assert skills.detail("demo")["version"] == first["version"]


def test_description_keeps_existing_language_validation(skills):
    first = skills.create("demo", "示例技能", document())
    with pytest.raises(ValueError):
        skills.update("demo", {"description": "English only", "version": first["version"]})
    assert skills.detail("demo")["version"] == first["version"]


def test_content_and_description_roll_back_together_on_review_failure(skills, monkeypatch):
    first = skills.create("demo", "示例技能", document())
    root = get_enabled_skills_dir() / "demo"
    original = {path.name: path.read_bytes() for path in root.iterdir() if path.is_file()}
    monkeypatch.setattr(skill_store, "review_skill_package", lambda *_a, **_k: {
        "verdict": "dangerous", "summary": "test blocked"})
    with pytest.raises(ValueError, match="blocked"):
        skills.update("demo", {"description": "新描述", "content": document(body="Changed."),
                               "version": first["version"]})
    assert {path.name: path.read_bytes() for path in root.iterdir() if path.is_file()} == original
    assert skills.detail("demo")["version"] == first["version"]


def test_prepare_review_binding_tamper_and_idempotent_install(skills, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_text(document(), encoding="utf-8")
    detail = skills.prepare(str(source))
    assert detail["blocked"] is False
    assert "SKILL.md" in detail["files"]
    assert not (get_enabled_skills_dir() / "demo").exists()
    draft = get_skill_drafting_dir() / detail["drafting_id"] / "skill" / "SKILL.md"
    draft.write_text(document(body="Different reviewed instructions."), encoding="utf-8")
    with pytest.raises(ApplicationError) as error:
        skills.confirm(detail["drafting_id"], detail["confirmation_token"])
    assert error.value.code == "SKILL_CONFIRMATION_CHANGED"
    reviewed = skills.draft_detail(detail["drafting_id"])
    assert reviewed["confirmation_token"] != detail["confirmation_token"]
    installed = skills.confirm(reviewed["drafting_id"], reviewed["confirmation_token"])
    assert installed["success"]
    assert skills.confirm(reviewed["drafting_id"], reviewed["confirmation_token"]) == installed
    audit = json.loads((get_enabled_skills_dir() / "demo" / "install_audit.json").read_text(encoding="utf-8"))
    assert audit["drafting_id"] == detail["drafting_id"]


def test_blocked_install_remains_blocked_and_can_be_cancelled(skills, tmp_path, monkeypatch):
    from mclaw.skills_hub import security
    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_text(document(), encoding="utf-8")
    detail = skills.prepare(str(source))
    monkeypatch.setattr(security, "review_skill_package", lambda *_a, **_k: {
        "verdict": "dangerous", "install_allowed": False, "summary": "blocked", "findings": []})
    blocked = skills.draft_detail(detail["drafting_id"])
    assert blocked["blocked"]
    with pytest.raises(ValueError, match="Blocked"):
        skills.confirm(blocked["drafting_id"], blocked["confirmation_token"])
    assert skills.cancel(blocked["drafting_id"])["success"]
    assert not (get_enabled_skills_dir() / "demo").exists()


def test_snapshot_stays_fixed_after_edits_and_applies_once_to_history(skills):
    first = skills.create("demo", "示例技能", document(body="Original frozen guidance."))
    snapshots = skills.snapshots(["demo", "demo"])
    assert len(snapshots) == 1
    skills.update("demo", {"content": document(body="Later changed guidance."), "version": first["version"]})
    db = SessionDB(skills.home / "state.db")
    db.create_session(session_id="skills-session", source="test")
    item = {"id": "original-task", "content": "Do useful work", "skill_snapshots": snapshots}
    messages, applied = [{"role": "system", "content": "Base prompt"}], set()
    assert apply_skill_snapshots(db, "skills-session", messages, "turn", item, applied)
    assert "Original frozen guidance" in messages[-1]["content"]
    assert "Later changed" not in messages[-1]["content"]
    assert not apply_skill_snapshots(db, "skills-session", messages, "turn", item, applied)
    messages[0]["content"] = "Refreshed base prompt"
    assert "Original frozen guidance" in messages[-1]["content"]
    restored = db.get_messages_as_conversation("skills-session")
    assert not apply_skill_snapshots(db, "skills-session", restored, "next", item, set())
    assert len(db.get_messages("skills-session")) == 1
    db.close()


def test_supplement_recovers_skill_application_after_text_commit(skills):
    skills.create("demo", "示例技能", document())
    item = {"id": "addition", "content": "Additional instruction", "skill_snapshots": skills.snapshots(["demo"])}
    db = SessionDB(skills.home / "state.db")
    db.create_session(session_id="session", source="test")
    db.append_message("session", "user", content=item["content"], operation_id="supplement:addition")
    messages = db.get_messages_as_conversation("session")
    assert not apply_pending_supplement(db, "session", messages, "turn", item, {"supplement:addition"})
    assert [message["role"] for message in db.get_messages("session")] == ["user", "system"]
    db.close()


def test_skill_http_routes_validate_body_and_expose_details(skills):
    async def error(_request, exc):
        return JSONResponse({"error": {"code": exc.code}}, status_code=exc.status)
    app = Starlette(routes=build_skill_routes(SimpleNamespace(home=skills.home)),
                    exception_handlers={ApplicationError: error})
    with TestClient(app) as client:
        assert client.post("/api/v1/skills", content="{").status_code == 400
        created = client.post("/api/v1/skills", json={"name": "demo", "description": "示例技能", "content": document()})
        assert created.status_code == 201
        detail = client.get("/api/v1/skills/demo").json()
        assert detail["tree"]["file_count"] == 3
        assert client.get("/api/v1/skills/demo/file?path=SKILL.md").json()["editable"]
        assert client.patch("/api/v1/skills/demo", json={"content": document()}).status_code == 409
        assert client.patch("/api/v1/skills/demo", json={"description": "新描述"}).status_code == 409
        assert client.patch("/api/v1/skills/demo", json={"description": "", "version": detail["version"]}).status_code == 400
        updated = client.patch("/api/v1/skills/demo", json={"description": "新描述", "version": detail["version"]})
        assert updated.status_code == 200
        assert updated.json()["description"] == "新描述"
        assert updated.json()["content"] == detail["content"]
        detail = updated.json()
        assert client.delete("/api/v1/skills/demo", params={"version": detail["version"]}).status_code == 200


def test_terminal_skill_selection_captures_content_when_input_is_accepted(skills):
    from mclaw.application.task_queue import TaskQueue
    from mclaw.cli.runtime.skill_commands import RuntimeSkillCommandCoordinator
    skills.create("demo", "示例技能", document(body="Before selection."))
    queue = TaskQueue()
    messages = [{"role": "system", "content": "Unchanged context"}]
    coordinator = RuntimeSkillCommandCoordinator(SimpleNamespace(
        queue_skill_intent=lambda name, _intent: (queue.select_skill(name) or True),
        render_loaded=lambda _name: None, render_processing=lambda _name: None,
        set_skip_next_prompt=lambda: None, render_read_error=lambda error: pytest.fail(error),
        get_agent_messages=lambda: messages,
    ))
    coordinator.invoke_skill(SimpleNamespace(name="demo"), "")
    assert messages == [{"role": "system", "content": "Unchanged context"}]
    latest = skills.detail("demo")
    skills.update("demo", {"content": document(body="Accepted selection."), "version": latest["version"]})
    queue.submit("Do the task")
    accepted = queue.take_next()
    assert "Accepted selection." in accepted["skill_snapshots"][0]["content"]
    assert queue.snapshot()["selected_skills"] == []
    queue.select_skill("demo")
    queue.supplement("Additional requirement")
    assert queue.snapshot()["active"]["supplements"][0]["skills"][0]["name"] == "demo"
