# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""skill_evolution.json creation and atomic update helpers."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from mclaw.system.lock import file_lock
from mclaw.skills_hub.schema import EVOLUTION_SECTIONS, SkillSchemaError, validate_evolution


def default_evolution() -> dict[str, list[str]]:
    return {section: [] for section in EVOLUTION_SECTIONS}


def normalize_initial_evolution(initial: dict[str, Any] | None = None) -> dict[str, list[str]]:
    data = default_evolution()
    if initial:
        for section, values in initial.items():
            if section not in data:
                raise SkillSchemaError(f"Invalid evolution section: {section}")
            if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
                raise SkillSchemaError(f"{section} must be an array of strings.")
            data[section] = list(dict.fromkeys(item.strip() for item in values if item.strip()))
    return validate_evolution(data)


def read_evolution(skill_dir: Path) -> dict[str, list[str]]:
    path = skill_dir / "skill_evolution.json"
    if not path.exists():
        raise SkillSchemaError("skill_evolution.json is missing.")
    data = json.loads(path.read_text(encoding="utf-8"))
    return validate_evolution(data)


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp", prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def write_default(skill_dir: Path, initial: dict[str, Any] | None = None) -> None:
    atomic_write_json(skill_dir / "skill_evolution.json", normalize_initial_evolution(initial))


def update_evolution(
    skill_dir: Path,
    *,
    section: str,
    operation: str,
    content: str | None = None,
    old_text: str | None = None,
) -> dict[str, Any]:
    if section not in EVOLUTION_SECTIONS:
        raise SkillSchemaError(f"Invalid section: {section}")
    if operation not in {"append", "replace", "remove"}:
        raise SkillSchemaError("operation must be append, replace, or remove.")

    content_value = (content or "").strip()
    old_value = (old_text or "").strip()
    if operation == "append":
        if not content_value:
            raise SkillSchemaError("content is required for append.")
        if old_value:
            raise SkillSchemaError("old_text must be empty for append.")
    elif operation == "replace":
        if not content_value or not old_value:
            raise SkillSchemaError("old_text and content are required for replace.")
    elif operation == "remove":
        if not old_value:
            raise SkillSchemaError("old_text is required for remove.")
        if content_value:
            raise SkillSchemaError("content must be empty for remove.")

    path = skill_dir / "skill_evolution.json"
    lock_path = skill_dir / "skill_evolution.json.lock"
    with file_lock(lock_path):
        data = read_evolution(skill_dir)
        entries = list(data[section])
        changed = False
        if operation == "append":
            if content_value not in entries:
                entries.append(content_value)
                changed = True
        else:
            matches = [idx for idx, item in enumerate(entries) if old_value in item]
            if len(matches) != 1:
                raise SkillSchemaError("old_text must uniquely match one entry.")
            if operation == "replace":
                entries[matches[0]] = content_value
            else:
                entries.pop(matches[0])
            changed = True
        data[section] = list(dict.fromkeys(item for item in entries if item.strip()))
        validate_evolution(data)
        if changed:
            atomic_write_json(path, data)
    return {
        "success": True,
        "action": "evolution_update",
        "section": section,
        "operation": operation,
        "changed": changed,
        "path": str(path),
    }
