"""External Skill install lifecycle for Skill 2.0."""

from __future__ import annotations

import json
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from mclaw.agent.skill_utils import parse_frontmatter
from mclaw.system.lock import file_lock
from mclaw.skills_hub.evolution_store import write_default
from mclaw.skills_hub.paths import (
    ensure_runtime_roots,
    get_drafting_locks_dir,
    get_enabled_skills_dir,
    get_skill_drafting_dir,
    validate_drafting_id,
    validate_skill_name,
)
from mclaw.skills_hub.schema import validate_manifest
from mclaw.skills_hub.security import review_skill_package, write_security_review
from mclaw.skills_hub.dependency_scan import scan_skill_dependencies
from mclaw.skills_hub.skill_store import SkillStoreError, _validate_package, _validate_skill_md, clear_skills_cache
from mclaw.skills_hub.skill_yaml_store import (
    build_skill_yaml,
    extract_short_description,
    mark_enabled,
    read_skill_yaml,
    write_skill_yaml,
)
from mclaw.skills_hub.source_resolver import ResolvedSource, materialize_source, resolve_source


INSTALL_AUDIT_FILENAME = "install_audit.json"
SECURITY_REVIEW_FILENAME = "security_review.json"


def _drafting_id() -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"skill_drafting_{ts}_{uuid.uuid4().hex[:8]}"


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _lock_path(name: str) -> Path:
    get_drafting_locks_dir().mkdir(parents=True, exist_ok=True)
    return get_drafting_locks_dir() / f"{validate_skill_name(name)}.lock"


def _read_manifest(drafting_id: str) -> tuple[Path, dict[str, Any]]:
    did = validate_drafting_id(drafting_id)
    root = get_skill_drafting_dir() / did
    manifest_path = root / "import_manifest.yaml"
    if not manifest_path.exists():
        raise SkillStoreError(f"Drafting manifest not found: {did}")
    data = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    return root, validate_manifest(data)


def _write_manifest(drafting_root: Path, manifest: dict[str, Any]) -> None:
    validate_manifest(manifest)
    (drafting_root / "import_manifest.yaml").write_text(
        yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _relative_file_inventory(root: Path) -> list[str]:
    files: list[str] = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            files.append(path.relative_to(root).as_posix())
    return files


def _source_summary(source: Any) -> dict[str, Any]:
    if isinstance(source, dict):
        return {
            "type": str(source.get("type") or ""),
            "original": str(source.get("original") or ""),
        }
    return {"type": str(source or ""), "original": ""}


def _write_install_audit(
    target: Path,
    *,
    manifest: dict[str, Any],
    review: dict[str, Any],
    target_path: Path,
    status: str,
    confirmed_at: str,
    enabled_at: str | None = None,
) -> Path:
    source = _source_summary(manifest.get("source"))
    audit = {
        "schema_version": 1,
        "event": "skill_install_enable",
        "status": status,
        "drafting_id": manifest.get("drafting_id", ""),
        "skill_name": manifest.get("skill_name", ""),
        "source": source,
        "user_intent": manifest.get("user_intent", ""),
        "prepared_at": manifest.get("created_at", ""),
        "confirmed_at": confirmed_at,
        "enabled_at": enabled_at or "",
        "confirmation": {
            "action": "enable_drafting",
            "decision": "approved",
            "actor": "user",
            "confirmed_at": confirmed_at,
        },
        "security_review": {
            "path": SECURITY_REVIEW_FILENAME,
            "risk_level": review.get("risk_level", "unknown"),
            "verdict": review.get("verdict", "unknown"),
            "install_allowed": review.get("install_allowed"),
            "install_policy_reason": review.get("install_policy_reason", ""),
            "summary": review.get("summary", ""),
            "findings_count": len(review.get("findings") or []),
        },
        "install": {
            "target_path": str(target_path),
            "package_path": ".",
            "files": _relative_file_inventory(target),
        },
    }
    path = target / INSTALL_AUDIT_FILENAME
    _write_json(path, audit)
    return path


def _compact_findings(review: dict[str, Any], *, limit: int = 5) -> list[dict[str, Any]]:
    findings = review.get("findings")
    if not isinstance(findings, list):
        return []
    result: list[dict[str, Any]] = []
    for item in findings[:limit]:
        if not isinstance(item, dict):
            continue
        result.append(
            {
                "pattern_id": item.get("pattern_id"),
                "severity": item.get("severity"),
                "category": item.get("category"),
                "file": item.get("file"),
                "line": item.get("line"),
                "description": item.get("description"),
            }
        )
    return result


def _read_root_skill_md(package: Path) -> str:
    skill_md = package / "SKILL.md"
    if not skill_md.exists():
        raise SkillStoreError("Source package must contain root SKILL.md.")
    return skill_md.read_text(encoding="utf-8", errors="replace")


def _read_package_metadata(package: Path, skill_md: str, fallback: str) -> tuple[str, str]:
    if (package / "mclaw_skill.yaml").is_file():
        data = read_skill_yaml(package)
        name = validate_skill_name(str(data.get("name") or "").strip())
        short_description = str(data.get("short_description") or "").strip()
        return name, short_description

    frontmatter, _ = parse_frontmatter(skill_md)
    if not str(frontmatter.get("name") or "").strip() or not str(frontmatter.get("description") or "").strip():
        raise SkillStoreError(
            "Source package without mclaw_skill.yaml must include SKILL.md frontmatter name and description."
    )
    name = str(frontmatter.get("name") or fallback or package.name).strip()
    short_description = extract_short_description(skill_md, name)
    return validate_skill_name(name), short_description


def _fallback_name(resolved: ResolvedSource, source: str) -> str:
    if resolved.slug:
        return resolved.slug
    if resolved.package_root:
        return Path(resolved.package_root).name
    if resolved.repo:
        return resolved.repo
    if resolved.local_path:
        return resolved.local_path.name
    return Path(source.rstrip("/\\")).name


def install_prepare(source: str, user_intent: str | None = None) -> dict[str, Any]:
    ensure_runtime_roots()
    resolved = resolve_source(source)
    did = _drafting_id()
    drafting_root = get_skill_drafting_dir() / did
    package = drafting_root / "skill"
    try:
        materialize_source(resolved, package)
        skill_md = _read_root_skill_md(package)
        name, short_description = _read_package_metadata(package, skill_md, _fallback_name(resolved, source))
        _validate_skill_md(skill_md, name)
        write_skill_yaml(
            package,
            build_skill_yaml(
                name=name,
                short_description=short_description,
                source_type=resolved.type,
                source_original=resolved.original,
                actor="install",
                status="prepared",
            ),
        )
        write_default(package)
        dependency_hints = scan_skill_dependencies(package)
        review = review_skill_package(package, source=resolved.type)
        write_security_review(drafting_root, review)
        allowed = review.get("install_allowed")
        blocked = allowed is False
        manifest = {
            "schema_version": 1,
            "drafting_id": did,
            "skill_name": name,
            "source": {
                "type": resolved.type,
                "original": resolved.original,
            },
            "status": "blocked" if blocked else "prepared",
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "security_review_path": "security_review.json",
            "package_path": "skill",
            "user_intent": user_intent or "",
            "dependency_hints": dependency_hints,
        }
        _write_manifest(drafting_root, manifest)
        if blocked:
            return {
                "success": False,
                "blocked": True,
                "requires_confirmation": False,
                "confirmation_type": "skill_enable_drafting",
                "drafting_id": did,
                "skill_name": name,
                "risk_level": review.get("risk_level", "unknown"),
                "verdict": review.get("verdict", "dangerous"),
                "summary": "安全策略已拒绝安装该 Skill。",
                "findings": _compact_findings(review),
                "source": {
                    "type": resolved.type,
                    "original": resolved.original,
                },
                "security_review_path": str(drafting_root / SECURITY_REVIEW_FILENAME),
                "user_intent": user_intent or "",
                "dependency_hints": dependency_hints,
            }
        return {
            "success": False,
            "requires_confirmation": True,
            "confirmation_type": "skill_enable_drafting",
            "drafting_id": did,
            "skill_name": name,
            "risk_level": review.get("risk_level", "unknown"),
            "verdict": review.get("verdict", "safe"),
            "summary": f"Skill '{name}' 已准备好启用，安全审查已保存到草稿包。",
            "findings": _compact_findings(review),
            "source": {
                "type": resolved.type,
                "original": resolved.original,
            },
            "security_review_path": str(drafting_root / SECURITY_REVIEW_FILENAME),
            "user_intent": user_intent or "",
            "dependency_hints": dependency_hints,
        }
    except BaseException:
        shutil.rmtree(drafting_root, ignore_errors=True)
        raise


def enable_drafting(drafting_id: str) -> dict[str, Any]:
    drafting_root, manifest = _read_manifest(drafting_id)
    skill_name = validate_skill_name(str(manifest.get("skill_name") or ""))
    package = drafting_root / "skill"
    review_path = drafting_root / SECURITY_REVIEW_FILENAME
    if not package.is_dir():
        raise SkillStoreError("Drafting package is missing.")
    if not review_path.is_file():
        raise SkillStoreError("security_review.json is required before enable_drafting.")
    if str(manifest.get("status") or "") == "blocked":
        raise SkillStoreError("Blocked Skill drafting package cannot be enabled.")
    review = json.loads(review_path.read_text(encoding="utf-8"))
    if str(review.get("verdict") or "").lower() == "dangerous":
        raise SkillStoreError("Dangerous Skill drafting package cannot be enabled.")
    _validate_package(package, expected_status="prepared")
    target = get_enabled_skills_dir() / skill_name
    confirmed_at = _now_iso()
    source = _source_summary(manifest.get("source"))
    audit_path: Path | None = None
    target_review_path: Path | None = None
    with file_lock(_lock_path(skill_name)):
        if target.exists():
            raise SkillStoreError(f"Skill '{skill_name}' already exists at {target}.")
        moved = False
        try:
            shutil.move(str(package), str(target))
            moved = True
            target_review_path = target / SECURITY_REVIEW_FILENAME
            _write_json(target_review_path, review)
            _write_install_audit(
                target,
                manifest=manifest,
                review=review,
                target_path=target,
                status="enabling",
                confirmed_at=confirmed_at,
            )
            mark_enabled(target, actor="install")
            enabled_at = _now_iso()
            audit_path = _write_install_audit(
                target,
                manifest=manifest,
                review=review,
                target_path=target,
                status="enabled",
                confirmed_at=confirmed_at,
                enabled_at=enabled_at,
            )
        except BaseException:
            if moved and target.exists():
                package.parent.mkdir(parents=True, exist_ok=True)
                if package.exists():
                    shutil.rmtree(package, ignore_errors=True)
                shutil.move(str(target), str(package))
            elif target.exists():
                shutil.rmtree(target, ignore_errors=True)
            raise
    shutil.rmtree(drafting_root, ignore_errors=True)
    clear_skills_cache()
    return {
        "success": True,
        "action": "enable_drafting",
        "name": skill_name,
        "path": str(target),
        "drafting_id": str(manifest.get("drafting_id") or drafting_id),
        "source": source,
        "user_intent": str(manifest.get("user_intent") or ""),
        "risk_level": review.get("risk_level", "unknown"),
        "verdict": review.get("verdict", "unknown"),
        "security_review_path": str(target_review_path or (target / SECURITY_REVIEW_FILENAME)),
        "audit_path": str(audit_path or (target / INSTALL_AUDIT_FILENAME)),
    }


def cancel_drafting(drafting_id: str) -> dict[str, Any]:
    drafting_root, manifest = _read_manifest(drafting_id)
    shutil.rmtree(drafting_root, ignore_errors=True)
    return {
        "success": True,
        "action": "cancel_drafting",
        "drafting_id": drafting_id,
        "name": manifest.get("skill_name", ""),
    }


def security_review(drafting_id: str) -> dict[str, Any]:
    drafting_root, manifest = _read_manifest(drafting_id)
    package = drafting_root / "skill"
    if not package.is_dir():
        raise SkillStoreError("Drafting package is missing.")
    review = review_skill_package(package, source=str(manifest.get("source", {}).get("type") or "community"))
    review_path = write_security_review(drafting_root, review)
    return {
        "success": True,
        "action": "security_review",
        "drafting_id": drafting_id,
        "risk_level": review.get("risk_level", "unknown"),
        "summary": review.get("summary", ""),
        "review_path": review_path.name,
    }
