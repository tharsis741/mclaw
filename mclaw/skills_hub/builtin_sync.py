# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Built-in Skill discovery and setup-time synchronization."""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Sequence

from mclaw.constants import get_skills_dir
from mclaw.skills_hub.evolution_store import write_default
from mclaw.skills_hub.paths import SIDECAR_FILENAMES, validate_skill_name
from mclaw.skills_hub.skill_yaml_store import (
    build_skill_yaml,
    extract_short_description,
    read_skill_yaml,
    write_skill_yaml,
)


@dataclass(frozen=True)
class BuiltinSkill:
    name: str
    short_description: str
    source_key: str
    path: Path
    category: str = ""


def _get_app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def _get_builtin_skills_dir() -> Path:
    env_override = os.getenv("MCLAW_BUILTIN_SKILLS", "").strip()
    if env_override:
        return Path(env_override)

    if getattr(sys, "frozen", False):
        return _get_app_root() / "skills"

    return Path(__file__).resolve().parents[1] / "skills"


def get_builtin_skills_dir() -> Path:
    """Return the built-in Skill source directory used by setup-time sync."""
    return _get_builtin_skills_dir()


def _read_manifest(manifest_file: Path) -> Dict[str, str]:
    if not manifest_file.exists():
        return {}
    result: Dict[str, str] = {}
    for line in manifest_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        if ":" in line:
            name, _, hash_val = line.partition(":")
            result[name.strip()] = hash_val.strip()
        else:
            result[line] = ""
    return result


def _write_manifest(entries: Dict[str, str], manifest_file: Path) -> None:
    manifest_file.parent.mkdir(parents=True, exist_ok=True)
    data = "\n".join(f"{name}:{hash_val}" for name, hash_val in sorted(entries.items())) + "\n"
    tmp = manifest_file.with_suffix(".tmp")
    tmp.write_text(data, encoding="utf-8")
    os.replace(tmp, manifest_file)


def _discover_builtin_skills(skills_source_dir: Path) -> List[BuiltinSkill]:
    skills: List[BuiltinSkill] = []
    if not skills_source_dir.exists():
        return skills

    seen_names: set[str] = set()
    for manifest_path in sorted(skills_source_dir.rglob("mclaw_skill.yaml")):
        skill_dir = manifest_path.parent
        rel = skill_dir.relative_to(skills_source_dir)
        rel_key = rel.as_posix()
        if not rel.parts:
            continue
        if not (skill_dir / "SKILL.md").is_file():
            continue
        try:
            manifest = read_skill_yaml(skill_dir)
            name = validate_skill_name(str(manifest.get("name") or "").strip())
            short_description = str(manifest.get("short_description") or "").strip()
        except Exception:
            continue
        if name in seen_names:
            continue
        seen_names.add(name)
        skills.append(
            BuiltinSkill(
                name=name,
                short_description=short_description,
                source_key=rel_key,
                path=skill_dir,
                category=rel.parts[0] if len(rel.parts) > 1 else "",
            )
        )
    return skills


def _dir_hash(directory: Path, *, exclude_generated_sidecars: bool = False) -> str:
    hasher = hashlib.md5()
    for fpath in sorted(directory.rglob("*")):
        if fpath.is_file():
            rel = fpath.relative_to(directory)
            if exclude_generated_sidecars and rel.name in (SIDECAR_FILENAMES - {"SKILL.md"}):
                continue
            hasher.update(str(rel).encode("utf-8"))
            hasher.update(fpath.read_bytes())
    return hasher.hexdigest()


def _ensure_skill2_sidecars(skill_dir: Path, *, name: str, source_original: str, force: bool = False) -> None:
    skill_md = (skill_dir / "SKILL.md").read_text(encoding="utf-8", errors="replace")
    short_description = extract_short_description(skill_md, name)
    manifest_path = skill_dir / "mclaw_skill.yaml"
    if manifest_path.exists():
        try:
            import yaml

            existing = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
            short_description = str(existing.get("short_description") or short_description).strip()
        except Exception:
            pass
    if force or not (skill_dir / "mclaw_skill.yaml").exists():
        write_skill_yaml(
            skill_dir,
            build_skill_yaml(
                name=name,
                short_description=short_description,
                source_type="bundled",
                source_original=source_original,
                actor="bundled",
                status="enabled",
            ),
        )
    if not (skill_dir / "skill_evolution.json").exists():
        write_default(skill_dir)


def _is_existing_same_builtin(dest: Path, source_key: str) -> bool:
    manifest = dest / "mclaw_skill.yaml"
    if not manifest.exists():
        return False
    try:
        import yaml

        data = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
    except Exception:
        return False
    source = data.get("source") if isinstance(data, dict) else None
    if not isinstance(source, dict):
        return False
    return source.get("type") == "bundled" and source.get("original") == source_key


def list_builtin_skills() -> list[dict]:
    """Return installable built-in Skills shipped with this M-Claw build."""
    return [
        {
            "name": skill.name,
            "short_description": skill.short_description,
            "source_key": skill.source_key,
            "category": skill.category,
            "path": str(skill.path),
        }
        for skill in _discover_builtin_skills(_get_builtin_skills_dir())
    ]


def sync_selected_skills(skill_names: Sequence[str], quiet: bool = False) -> dict:
    """Copy selected built-in Skills into the user's M-Claw home skills root."""
    selected = {validate_skill_name(str(name).strip()) for name in skill_names if str(name).strip()}
    return sync_skills(quiet=quiet, selected_names=selected)


def sync_skills(quiet: bool = False, selected_names: set[str] | None = None) -> dict:
    builtins_dir = _get_builtin_skills_dir()
    skills_dir = get_skills_dir()
    manifest_file = skills_dir / ".builtin_manifest"
    if not builtins_dir.exists():
        return {
            "copied": [],
            "updated": [],
            "skipped": 0,
            "user_modified": [],
            "cleaned": [],
            "selected": sorted(selected_names or []),
            "total_builtin": 0,
            "builtin_dir": str(builtins_dir),
        }

    skills_dir.mkdir(parents=True, exist_ok=True)
    manifest = _read_manifest(manifest_file)
    builtin_skills = _discover_builtin_skills(builtins_dir)
    if selected_names is not None:
        builtin_skills = [skill for skill in builtin_skills if skill.name in selected_names]
    builtin_names = {skill.name for skill in _discover_builtin_skills(builtins_dir)}

    copied: List[str] = []
    updated: List[str] = []
    user_modified: List[str] = []
    skipped = 0

    for skill in builtin_skills:
        source_key = skill.source_key
        skill_name = skill.name
        skill_src = skill.path
        dest = skills_dir / skill_name
        bundled_hash = _dir_hash(skill_src, exclude_generated_sidecars=True)

        if skill_name not in manifest:
            if dest.exists():
                if _is_existing_same_builtin(dest, source_key):
                    _ensure_skill2_sidecars(dest, name=skill_name, source_original=source_key)
                    skipped += 1
                    manifest[skill_name] = bundled_hash
                else:
                    user_modified.append(skill_name)
            else:
                shutil.copytree(skill_src, dest)
                _ensure_skill2_sidecars(dest, name=skill_name, source_original=source_key, force=True)
                copied.append(skill_name)
                manifest[skill_name] = bundled_hash
                if not quiet:
                    print(f"  + {skill_name}")
            continue

        if dest.exists():
            _ensure_skill2_sidecars(dest, name=skill_name, source_original=source_key)
            origin_hash = manifest.get(skill_name, "")
            user_hash = _dir_hash(dest, exclude_generated_sidecars=True)

            if not origin_hash:
                manifest[skill_name] = user_hash
                skipped += 1
                continue

            if user_hash != origin_hash:
                user_modified.append(skill_name)
                if not quiet:
                    print(f"  ~ {skill_name} (user-modified, skipping)")
                continue

            if bundled_hash != origin_hash:
                backup = dest.with_suffix(".bak")
                shutil.move(str(dest), str(backup))
                try:
                    shutil.copytree(skill_src, dest)
                    _ensure_skill2_sidecars(dest, name=skill_name, source_original=source_key, force=True)
                    manifest[skill_name] = bundled_hash
                    updated.append(skill_name)
                    if not quiet:
                        print(f"  ↑ {skill_name} (updated)")
                    shutil.rmtree(backup, ignore_errors=True)
                except Exception:
                    if backup.exists() and not dest.exists():
                        shutil.move(str(backup), str(dest))
                    raise
            else:
                skipped += 1
        else:
            skipped += 1

    cleaned = sorted(set(manifest.keys()) - builtin_names)
    for name in cleaned:
        del manifest[name]

    _write_manifest(manifest, manifest_file)

    try:
        from mclaw.cli.skill_registry import get_skill_registry
        get_skill_registry().invalidate()
    except Exception:
        pass

    selected_result = sorted(selected_names) if selected_names is not None else [skill.name for skill in builtin_skills]
    return {
        "copied": copied,
        "updated": updated,
        "skipped": skipped,
        "user_modified": user_modified,
        "cleaned": cleaned,
        "selected": selected_result,
        "total_builtin": len(_discover_builtin_skills(builtins_dir)),
        "builtin_dir": str(builtins_dir),
    }
