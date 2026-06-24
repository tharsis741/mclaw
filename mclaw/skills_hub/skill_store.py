"""Enabled Skill 2.0 create/read/update/delete service."""

from __future__ import annotations

import base64
import json
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from mclaw.agent.skill_utils import parse_frontmatter, render_skill_vars
from mclaw.system.lock import file_lock
from mclaw.skills_hub.evolution_store import read_evolution, update_evolution, write_default
from mclaw.skills_hub.paths import (
    RECOMMENDED_SUPPORT_DIRS,
    RUNTIME_ARTIFACT_FILENAMES,
    SIDECAR_FILENAMES,
    ensure_runtime_roots,
    ensure_within_dir,
    get_drafting_locks_dir,
    get_enabled_skills_dir,
    get_skill_drafting_dir,
    validate_mutable_skill_path,
    validate_relative_skill_path,
    validate_skill_name,
)
from mclaw.skills_hub.schema import SkillSchemaError, validate_evolution, validate_skill_yaml
from mclaw.skills_hub.security import review_skill_package
from mclaw.skills_hub.skill_yaml_store import (
    build_skill_yaml,
    extract_short_description,
    read_skill_yaml,
    touch_skill_yaml,
    write_skill_yaml,
)


class SkillStoreError(ValueError):
    """Raised by enabled Skill store operations."""


_SCAFFOLD_BODY = (
    "TODO: Replace this body with concise reusable instructions before finalizing this Skill."
)


def _drafting_id(prefix: str) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{ts}_{uuid.uuid4().hex[:8]}"


def _lock_path(name: str) -> Path:
    get_drafting_locks_dir().mkdir(parents=True, exist_ok=True)
    return get_drafting_locks_dir() / f"{validate_skill_name(name)}.lock"


def _scaffold_skill_md(name: str, description: str) -> str:
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {json.dumps(str(description), ensure_ascii=False)}\n"
        "---\n\n"
        f"# {name}\n\n"
        f"{_SCAFFOLD_BODY}\n"
    )


def _validate_skill_md(
    skill_md: str,
    expected_name: str | None = None,
    *,
    allow_placeholder: bool = False,
) -> None:
    text = str(skill_md or "")
    if not text.strip():
        raise SkillStoreError("skill_md cannot be empty.")
    frontmatter, body = parse_frontmatter(text)
    if not frontmatter:
        raise SkillStoreError("SKILL.md must include YAML frontmatter.")
    fm_name = str(frontmatter.get("name") or "").strip()
    fm_description = str(frontmatter.get("description") or "").strip()
    if not fm_name:
        raise SkillStoreError("SKILL.md frontmatter name is required.")
    fm_name = validate_skill_name(fm_name)
    if expected_name and fm_name != validate_skill_name(expected_name):
        raise SkillStoreError("SKILL.md frontmatter name must match the skill directory.")
    if not fm_description:
        raise SkillStoreError("SKILL.md frontmatter description is required.")
    instruction_body = body if frontmatter else text
    if not instruction_body.strip():
        raise SkillStoreError("SKILL.md must contain reusable instructions.")
    meaningful_lines = [
        line.strip()
        for line in instruction_body.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not allow_placeholder:
        if _SCAFFOLD_BODY in instruction_body:
            raise SkillStoreError("SKILL.md scaffold placeholder must be replaced before validation.")
        if not meaningful_lines or all(line.lower().startswith("todo") for line in meaningful_lines):
            raise SkillStoreError("SKILL.md must contain reusable instructions, not only TODO placeholders.")


def _validate_package(
    skill_dir: Path,
    *,
    expected_status: str = "enabled",
    allow_placeholder: bool = False,
) -> dict[str, Any]:
    for required in SIDECAR_FILENAMES:
        if not (skill_dir / required).exists():
            raise SkillStoreError(f"{required} is required.")
    yaml_data = read_skill_yaml(skill_dir)
    if yaml_data.get("status") != expected_status:
        raise SkillStoreError(f"mclaw_skill.yaml status must be {expected_status}.")
    read_evolution(skill_dir)
    _validate_skill_md(
        (skill_dir / "SKILL.md").read_text(encoding="utf-8", errors="replace"),
        str(yaml_data.get("name") or skill_dir.name),
        allow_placeholder=allow_placeholder,
    )
    return yaml_data


def clear_skills_cache() -> None:
    try:
        from mclaw.agent.prompt_builder import clear_skills_system_prompt_cache
        clear_skills_system_prompt_cache(clear_snapshot=True)
    except Exception:
        pass
    try:
        from mclaw.cli.skill_registry import get_skill_registry
        get_skill_registry().invalidate()
    except Exception:
        pass


def list_skills() -> list[dict[str, Any]]:
    root = get_enabled_skills_dir()
    skills: list[dict[str, Any]] = []
    if not root.exists():
        return skills
    for child in sorted(root.iterdir(), key=lambda p: p.name):
        if not child.is_dir():
            continue
        try:
            data = _validate_package(child, expected_status="enabled")
            skills.append(
                {
                    "name": data["name"],
                    "short_description": data["short_description"],
                    "description": data["short_description"],
                    "path": str(child),
                    "source": data["source"],
                }
            )
        except Exception:
            continue
    return skills


def get_skill_record(name: str) -> dict[str, Any]:
    target = get_enabled_skills_dir() / validate_skill_name(name)
    if not target.is_dir():
        raise SkillStoreError(f"Skill '{name}' not found.")
    data = _validate_package(target, expected_status="enabled", allow_placeholder=True)
    return {
        "name": data["name"],
        "short_description": data["short_description"],
        "path": str(target),
        "source": data["source"],
    }


def view_skill(name: str, file_path: str | None = None) -> dict[str, Any]:
    record = get_skill_record(name)
    skill_dir = Path(record["path"])
    if file_path:
        rel = validate_relative_skill_path(file_path, support_only=False)
        target = ensure_within_dir(skill_dir, rel)
        if not target.is_file():
            raise SkillStoreError(f"File '{file_path}' not found in skill '{name}'.")
        try:
            content = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise SkillStoreError(str(exc)) from exc
        return {
            "success": True,
            "name": record["name"],
            "file": rel.as_posix(),
            "content": render_skill_vars(content, skill_dir, record["name"]),
        }

    skill_md = (skill_dir / "SKILL.md").read_text(encoding="utf-8", errors="replace")
    yaml_data = read_skill_yaml(skill_dir)
    evolution = read_evolution(skill_dir)
    payload = (
        f"# Skill: {record['name']}\n\n"
        f"{render_skill_vars(skill_md, skill_dir, record['name']).strip()}\n\n"
        "## M-Claw Skill Metadata\n\n"
        f"```yaml\n{json.dumps(yaml_data, ensure_ascii=False, indent=2)}\n```\n\n"
        "## M-Claw Skill Memory\n\n"
        f"```json\n{json.dumps(evolution, ensure_ascii=False, indent=2)}\n```"
    )
    return {
        "success": True,
        "name": record["name"],
        "short_description": record["short_description"],
        "content": payload,
        "mclaw_skill": yaml_data,
        "skill_evolution": evolution,
    }


def tree_skill(name: str, *, max_entries: int = 500) -> dict[str, Any]:
    record = get_skill_record(name)
    skill_dir = Path(record["path"])
    try:
        limit = int(max_entries)
    except (TypeError, ValueError):
        limit = 500
    limit = min(max(limit, 1), 2000)

    entries: list[dict[str, Any]] = []
    truncated = False

    def add_entry(path: Path) -> None:
        rel = path.relative_to(skill_dir).as_posix()
        is_symlink = path.is_symlink()
        try:
            stat = path.lstat()
        except OSError:
            stat = None
        if is_symlink:
            kind = "symlink"
        elif path.is_dir():
            kind = "directory"
        elif path.is_file():
            kind = "file"
        else:
            kind = "other"
        sidecar = path.name in SIDECAR_FILENAMES
        runtime_artifact = path.name in RUNTIME_ARTIFACT_FILENAMES
        entry: dict[str, Any] = {
            "path": rel,
            "type": kind,
            "sidecar": sidecar,
            "runtime_artifact": runtime_artifact,
            "system": bool(sidecar or runtime_artifact),
            "readable_with_skill_view": kind == "file" and not sidecar,
            "mutable_with_skill_manage": kind == "file" and not sidecar and not runtime_artifact,
        }
        if stat is not None and kind == "file":
            entry["size_bytes"] = stat.st_size
        entries.append(entry)

    def visit(directory: Path) -> None:
        nonlocal truncated
        try:
            children = sorted(
                directory.iterdir(),
                key=lambda child: (not child.is_dir(), child.name.lower()),
            )
        except OSError:
            return
        for child in children:
            if child.name == ".git":
                continue
            if len(entries) >= limit:
                truncated = True
                return
            add_entry(child)
            if not child.is_symlink() and child.is_dir():
                visit(child)
                if truncated:
                    return

    visit(skill_dir)
    return {
        "success": True,
        "name": record["name"],
        "root": str(skill_dir),
        "entries": entries,
        "count": len(entries),
        "file_count": sum(1 for item in entries if item["type"] == "file"),
        "directory_count": sum(1 for item in entries if item["type"] == "directory"),
        "truncated": truncated,
        "max_entries": limit,
    }


def create_skill(
    *,
    name: str,
    skill_md: str,
    short_description: str,
    initial_evolution: dict[str, Any] | None = None,
    actor: str = "main_agent",
) -> dict[str, Any]:
    name = validate_skill_name(name)
    _validate_skill_md(skill_md, name)
    ensure_runtime_roots()
    target = get_enabled_skills_dir() / name
    tx_id = _drafting_id("create")
    tx_root = get_skill_drafting_dir() / tx_id
    package = tx_root / "skill"
    with file_lock(_lock_path(name)):
        if target.exists():
            raise SkillStoreError(f"Skill '{name}' already exists at {target}.")
        try:
            package.mkdir(parents=True, exist_ok=False)
            (package / "SKILL.md").write_text(skill_md, encoding="utf-8")
            write_skill_yaml(
                package,
                build_skill_yaml(
                    name=name,
                    short_description=short_description,
                    source_type="agent_created",
                    source_original="",
                    actor=actor,
                    status="enabled",
                ),
            )
            write_default(package, initial_evolution)
            _validate_package(package, expected_status="enabled")
            review = review_skill_package(package, source="agent_created")
            if review.get("verdict") == "dangerous":
                raise SkillStoreError(f"Security scan blocked this skill: {review.get('summary')}")
            shutil.move(str(package), str(target))
        except BaseException:
            shutil.rmtree(tx_root, ignore_errors=True)
            raise
        finally:
            if tx_root.exists():
                shutil.rmtree(tx_root, ignore_errors=True)
    clear_skills_cache()
    return {"success": True, "action": "create", "name": name, "path": str(target)}


def create_skill_scaffold(
    *,
    name: str,
    short_description: str,
    user_intent: str | None = None,
    actor: str = "main_agent",
) -> dict[str, Any]:
    name = validate_skill_name(name)
    description = str(user_intent or short_description or "").strip()
    if not description:
        raise SkillStoreError("user_intent or short_description is required for create_scaffold.")
    ensure_runtime_roots()
    target = get_enabled_skills_dir() / name
    tx_id = _drafting_id("create")
    tx_root = get_skill_drafting_dir() / tx_id
    package = tx_root / "skill"
    with file_lock(_lock_path(name)):
        if target.exists():
            raise SkillStoreError(f"Skill '{name}' already exists at {target}.")
        try:
            package.mkdir(parents=True, exist_ok=False)
            (package / "SKILL.md").write_text(_scaffold_skill_md(name, description), encoding="utf-8")
            write_skill_yaml(
                package,
                build_skill_yaml(
                    name=name,
                    short_description=short_description,
                    source_type="agent_created",
                    source_original="",
                    actor=actor,
                    status="enabled",
                ),
            )
            write_default(package, None)
            _validate_package(package, expected_status="enabled", allow_placeholder=True)
            shutil.move(str(package), str(target))
        except BaseException:
            shutil.rmtree(tx_root, ignore_errors=True)
            raise
        finally:
            if tx_root.exists():
                shutil.rmtree(tx_root, ignore_errors=True)
    clear_skills_cache()
    return {
        "success": True,
        "action": "create_scaffold",
        "name": name,
        "path": str(target),
        "recommended_file_dirs": sorted(RECOMMENDED_SUPPORT_DIRS),
        "skill_md_contract": [
            "SKILL.md must include YAML frontmatter with name and description.",
            "Frontmatter name must match the skill directory name.",
            "Frontmatter description must explain what the Skill does and when to use it.",
            "Replace the scaffold placeholder through skill_manage(action=\"edit\") before final validation.",
            "Write non-system Skill files anywhere under the Skill root through skill_manage(action=\"write_file\"); assets/, references/, scripts/, and templates/ are recommended organization directories, not hard limits.",
            "Run skill_manage(action=\"validate\") and a temporary-directory smoke test before reporting success.",
        ],
    }


def edit_skill(*, name: str, skill_md: str, actor: str = "main_agent") -> dict[str, Any]:
    _validate_skill_md(skill_md, name)
    record = get_skill_record(name)
    skill_dir = Path(record["path"])
    skill_path = skill_dir / "SKILL.md"
    backup = skill_path.read_text(encoding="utf-8", errors="replace")
    try:
        skill_path.write_text(skill_md, encoding="utf-8")
        review = review_skill_package(skill_dir, source="agent_created")
        if review.get("verdict") == "dangerous":
            raise SkillStoreError(f"Security scan blocked this skill: {review.get('summary')}")
        _validate_package(skill_dir, expected_status="enabled")
        touch_skill_yaml(skill_dir, actor=actor)
    except BaseException:
        skill_path.write_text(backup, encoding="utf-8")
        raise
    clear_skills_cache()
    return {"success": True, "action": "edit", "name": name, "path": str(skill_path)}


def patch_skill(*, name: str, old_text: str, new_text: str, actor: str = "main_agent") -> dict[str, Any]:
    record = get_skill_record(name)
    skill_dir = Path(record["path"])
    skill_path = skill_dir / "SKILL.md"
    original = skill_path.read_text(encoding="utf-8", errors="replace")
    occurrences = original.count(old_text or "")
    if not old_text or not new_text:
        raise SkillStoreError("old_text and new_text are required for patch.")
    if occurrences != 1:
        raise SkillStoreError("old_text must uniquely match one location.")
    updated = original.replace(old_text, new_text, 1)
    try:
        skill_path.write_text(updated, encoding="utf-8")
        _validate_skill_md(updated, name)
        review = review_skill_package(skill_dir, source="agent_created")
        if review.get("verdict") == "dangerous":
            raise SkillStoreError(f"Security scan blocked this skill: {review.get('summary')}")
        touch_skill_yaml(skill_dir, actor=actor)
    except BaseException:
        skill_path.write_text(original, encoding="utf-8")
        raise
    clear_skills_cache()
    return {"success": True, "action": "patch", "name": name, "path": str(skill_path), "replacements": 1}


def write_skill_file(
    *,
    name: str,
    file_path: str,
    content: str,
    encoding: str = "text",
    overwrite: bool = False,
    actor: str = "main_agent",
) -> dict[str, Any]:
    record = get_skill_record(name)
    skill_dir = Path(record["path"])
    rel = validate_mutable_skill_path(file_path)
    target = ensure_within_dir(skill_dir, rel)
    if target.exists() and not overwrite:
        raise SkillStoreError(f"File already exists: {rel.as_posix()}")
    target.parent.mkdir(parents=True, exist_ok=True)
    backup = target.read_bytes() if target.exists() else None
    try:
        if encoding == "base64":
            target.write_bytes(base64.b64decode(content or "", validate=True))
        elif encoding == "text":
            target.write_text(content or "", encoding="utf-8")
        else:
            raise SkillStoreError("encoding must be text or base64.")
        review = review_skill_package(skill_dir, source="agent_created")
        if review.get("verdict") == "dangerous":
            raise SkillStoreError(f"Security scan blocked this skill: {review.get('summary')}")
        touch_skill_yaml(skill_dir, actor=actor)
    except BaseException:
        if backup is None:
            target.unlink(missing_ok=True)
        else:
            target.write_bytes(backup)
        raise
    clear_skills_cache()
    return {
        "success": True,
        "action": "write_file",
        "name": name,
        "path": str(target),
        "bytes_written": target.stat().st_size,
    }


def validate_skill(*, name: str) -> dict[str, Any]:
    name = validate_skill_name(name)
    skill_dir = get_enabled_skills_dir() / name
    if not skill_dir.is_dir():
        raise SkillStoreError(f"Skill '{name}' not found.")
    yaml_data = _validate_package(skill_dir, expected_status="enabled", allow_placeholder=False)
    review = review_skill_package(skill_dir, source="agent_created")
    if review.get("verdict") == "dangerous":
        raise SkillStoreError(f"Security scan blocked this skill: {review.get('summary')}")
    return {
        "success": True,
        "action": "validate",
        "name": name,
        "path": str(skill_dir),
        "mclaw_skill": yaml_data,
        "security_review": {
            "risk_level": review.get("risk_level"),
            "verdict": review.get("verdict"),
            "summary": review.get("summary"),
            "findings": review.get("findings", []),
        },
        "checks": [
            "frontmatter_name",
            "frontmatter_description",
            "sidecars",
            "skill_evolution",
            "security_review",
        ],
    }


def remove_skill_file(*, name: str, file_path: str, actor: str = "main_agent") -> dict[str, Any]:
    record = get_skill_record(name)
    skill_dir = Path(record["path"])
    rel = validate_mutable_skill_path(file_path)
    target = ensure_within_dir(skill_dir, rel)
    if not target.is_file():
        raise SkillStoreError(f"File not found: {rel.as_posix()}")
    backup = target.read_bytes()
    try:
        target.unlink()
        touch_skill_yaml(skill_dir, actor=actor)
    except BaseException:
        target.write_bytes(backup)
        raise
    clear_skills_cache()
    return {"success": True, "action": "remove_file", "name": name, "path": str(target)}


def delete_skill(*, name: str) -> dict[str, Any]:
    name = validate_skill_name(name)
    target = get_enabled_skills_dir() / name
    with file_lock(_lock_path(name)):
        if not target.is_dir():
            raise SkillStoreError(f"Skill '{name}' not found.")
        _validate_package(target, expected_status="enabled")
        shutil.rmtree(target)
    clear_skills_cache()
    return {"success": True, "action": "delete", "name": name, "path": str(target)}


def evolution_update(
    *,
    name: str,
    section: str,
    operation: str,
    content: str | None = None,
    old_text: str | None = None,
) -> dict[str, Any]:
    record = get_skill_record(name)
    result = update_evolution(
        Path(record["path"]),
        section=section,
        operation=operation,
        content=content,
        old_text=old_text,
    )
    result["name"] = record["name"]
    return result
