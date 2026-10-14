# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared Skill management, reviewed installation and immutable input snapshots."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from pathlib import Path
from typing import Any

from mclaw.application.sessions import ApplicationError
from mclaw.prompts.skills import build_skill_runtime_content
from mclaw.skills_hub import install_service, skill_store
from mclaw.skills_hub.paths import (
    ensure_within_dir, get_enabled_skills_dir, get_skill_drafting_dir,
    validate_drafting_id, validate_mutable_skill_path, validate_relative_skill_path,
    validate_skill_name,
)
from mclaw.system.lock import file_lock
from mclaw.utils import atomic_json_write

MAX_TEXT_BYTES = 1024 * 1024


def _text(path: Path) -> str:
    if path.stat().st_size > MAX_TEXT_BYTES:
        raise ApplicationError("SKILL_FILE_TOO_LARGE", "此文件过大，请使用本机编辑器处理", 413)
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ApplicationError("SKILL_FILE_BINARY", "此文件不是 UTF-8 文本") from exc


def _digest(root: Path) -> str:
    """Bind every reviewed file and its relative path, not just timestamps."""
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if ".git" in path.relative_to(root).parts:
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ApplicationError("SKILL_PATH_UNSAFE", "技能包包含符号链接或越界路径")
        if not path.is_file():
            continue
        digest.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0")
        with path.open("rb") as source:
            while block := source.read(65536):
                digest.update(block)
        digest.update(b"\0")
    return digest.hexdigest()


def snapshot_references(snapshots: list[dict]) -> list[dict]:
    return [{key: item[key] for key in ("name", "source", "version")} for item in snapshots]


def apply_skill_snapshots(db, session_id: str, messages: list[dict], turn_id: str,
                          item: dict, applied_ids: set[str]) -> bool:
    """Audit each immutable input snapshot once, without deduplicating by name."""
    from mclaw.prompts.skills import build_skill_invocation_system

    changed = False
    persisted_ids = None
    for snapshot in item.get("skill_snapshots", []):
        name = snapshot["name"]
        version = snapshot.get("version") or hashlib.sha256(snapshot["content"].encode("utf-8")).hexdigest()
        legacy_operation = f"skill:{item['id']}:{name}"
        operation = f"{legacy_operation}:{version}"
        if operation in applied_ids or legacy_operation in applied_ids:
            continue
        if persisted_ids is None:
            persisted_ids = {row.get("operation_id") for row in db.get_messages(session_id, include_invalidated=True)}
        if operation in persisted_ids or legacy_operation in persisted_ids:
            applied_ids.add(operation)
            continue
        context = build_skill_invocation_system(name, snapshot["content"], user_intent=item.get("content", ""))
        db.append_message(session_id, "system", content=context, turn_id=turn_id, operation_id=operation)
        _remove_skill_contexts(messages, name=name)
        messages.append({"role": "system", "content": context})
        applied_ids.add(operation)
        changed = True
    return changed


def _remove_skill_contexts(messages: list[dict], *, name: str | None = None) -> None:
    from mclaw.prompts.skills import strip_skill_invocations

    retained = []
    for message in messages:
        content = message.get("content")
        if message.get("role") == "system" and isinstance(content, str):
            stripped = strip_skill_invocations(content, name=name)
            if stripped != content.strip():
                if stripped:
                    retained.append({**message, "content": stripped})
                continue
        retained.append(message)
    messages[:] = retained


def sync_task_skill_context(messages: list[dict], task: dict, applied_ids: set[str], *,
                            disabled_names: set[str] | frozenset[str] = frozenset()) -> None:
    """Project only this task's latest selections; keep the durable audit untouched.

    Rebuild from immutable task inputs after reconnect or compression. Applied
    supplements override earlier selections of the same Skill within this task.
    """
    from mclaw.prompts.skills import build_skill_invocation_system, strip_skill_invocations

    inputs = [task, *(item for item in task.get("supplements", [])
                     if item.get("applied") or "supplement:" + str(item.get("id")) in applied_ids)]
    selected: dict[str, str] = {}
    for item in inputs:
        for snapshot in item.get("skill_snapshots", []):
            name = snapshot["name"]
            if name not in disabled_names:
                selected[name] = build_skill_invocation_system(
                    name, snapshot["content"], user_intent=item.get("content", ""),
                )
    desired = list(selected.values())
    current = []
    embedded = False
    for message in messages:
        content = message.get("content")
        if message.get("role") != "system" or not isinstance(content, str):
            continue
        stripped = strip_skill_invocations(content)
        if stripped != content.strip():
            if stripped:
                embedded = True
            else:
                current.append(content)
    if current == desired and not embedded:
        return
    _remove_skill_contexts(messages)
    messages.extend({"role": "system", "content": content} for content in desired)


class SkillService:
    """Adapt existing Skill governance without introducing another package store."""

    def __init__(self, home: Path) -> None:
        self.home = Path(home).resolve()

    def _guard(self, identifier: str):
        key = hashlib.sha256(identifier.encode()).hexdigest()
        return file_lock(self.home / "web" / "skill-locks" / (key + ".lock"))

    def _root(self, name: str) -> Path:
        name = validate_skill_name(name)
        root = get_enabled_skills_dir() / name
        if root.is_symlink() or not root.resolve().is_relative_to(get_enabled_skills_dir().resolve()):
            raise ApplicationError("SKILL_PATH_UNSAFE", "技能目录越界")
        if not root.is_dir():
            raise ApplicationError("SKILL_NOT_FOUND", "技能不存在", 404)
        return root

    @staticmethod
    def _disabled() -> set[str]:
        from mclaw.cli.config import load_config
        value = load_config(strict=True).get("skills", {}).get("disabled", [])
        return set(value if isinstance(value, list) else [value])

    def list(self, query: str = "") -> dict:
        disabled = self._disabled()
        result = []
        for record in skill_store.list_skills():
            name = record["name"]
            if query.casefold() not in (name + " " + record["description"]).casefold():
                continue
            with self._guard(name):
                root = self._root(name)
                result.append({"name": name, "description": record["description"],
                               "source": record["source"], "enabled": name not in disabled,
                               "version": _digest(root)})
        return {"skills": result}

    def detail(self, name: str) -> dict:
        with self._guard(name):
            return self._detail(name)

    def _detail(self, name: str) -> dict:
        root = self._root(name)
        view = skill_store.view_skill(name)
        tree = skill_store.tree_skill(name)
        for entry in tree["entries"]:
            if entry["path"] == "SKILL.md":
                entry.update(mutable_with_skill_manage=True, readable_with_skill_view=True)
        return {"name": name, "description": view["short_description"],
                "source": view["mclaw_skill"]["source"], "enabled": name not in self._disabled(),
                "version": _digest(root), "content": _text(root / "SKILL.md"),
                "metadata": view["mclaw_skill"], "evolution": view["skill_evolution"],
                "tree": tree}

    def file(self, name: str, path: str) -> dict:
        with self._guard(name):
            return self._file(name, path)

    def _file(self, name: str, path: str) -> dict:
        root = self._root(name)
        if path == "SKILL.md":
            rel = Path(path)
        elif path in {"mclaw_skill.yaml", "skill_evolution.json"}:
            rel = Path(path)
        else:
            rel = validate_relative_skill_path(path)
        target = ensure_within_dir(root, rel)
        if target.is_symlink() or not target.is_file():
            raise ApplicationError("SKILL_FILE_NOT_FOUND", "技能文件不存在", 404)
        editable = path == "SKILL.md"
        if not editable:
            try:
                validate_mutable_skill_path(path)
                editable = True
            except ValueError:
                pass
        return {"name": name, "path": rel.as_posix(), "content": _text(target),
                "version": _digest(root), "editable": editable}

    @staticmethod
    def _check_version(root: Path, version: str | None) -> None:
        if not version or not hmac.compare_digest(_digest(root), version):
            raise ApplicationError("SKILL_VERSION_CONFLICT", "技能已变化，请重新读取并核对后保存", 409)

    def create(self, name: str, description: str, content: str) -> dict:
        with self._guard(name):
            skill_store.create_skill(name=name, short_description=description, skill_md=content, actor="user")
            return self._detail(name)

    def update(self, name: str, changes: dict) -> dict:
        from mclaw.cli.config import load_config, save_config

        if "enabled" in changes and type(changes["enabled"]) is not bool:
            raise ApplicationError("INVALID_SKILL_STATE", "启用状态须为布尔值")
        if "description" in changes and (
            not isinstance(changes["description"], str) or not changes["description"].strip()
        ):
            raise ApplicationError("INVALID_SKILL_DESCRIPTION", "请填写简短描述")
        with self._guard(name):
            root = self._root(name)
            if "content" in changes or "description" in changes:
                self._check_version(root, changes.get("version"))
                skill_store.edit_skill(
                    name=name,
                    skill_md=str(changes["content"]) if "content" in changes else None,
                    short_description=changes.get("description"),
                    actor="user",
                )
            if "enabled" in changes:
                with file_lock(self.home / "config.lock"):
                    config = load_config(strict=True)
                    settings = config.setdefault("skills", {})
                    disabled = set(settings.get("disabled", []))
                    if changes["enabled"]:
                        disabled.discard(name)
                    else:
                        disabled.add(name)
                    settings["disabled"] = sorted(disabled)
                    save_config(config)
                skill_store.clear_skills_cache()
            return self._detail(name)

    def write_file(self, name: str, path: str, content: str, version: str) -> dict:
        with self._guard(name):
            self._check_version(self._root(name), version)
            if path == "SKILL.md":
                skill_store.edit_skill(name=name, skill_md=content, actor="user")
            else:
                skill_store.write_skill_file(name=name, file_path=path, content=content, overwrite=True, actor="user")
            return self._file(name, path)

    def delete(self, name: str, version: str) -> dict:
        with self._guard(name):
            self._check_version(self._root(name), version)
            return skill_store.delete_skill(name=name)

    def snapshots(self, names: list[str] | None) -> list[dict]:
        if names is None or names == []:
            return []
        if not isinstance(names, list) or len(names) > 20 or any(not isinstance(name, str) for name in names):
            raise ApplicationError("INVALID_SKILLS", "技能引用须为名称列表，最多 20 项")
        snapshots = []
        disabled = self._disabled()
        for name in dict.fromkeys(names):
            with self._guard(name):
                if name in disabled:
                    raise ApplicationError("SKILL_DISABLED", f"技能 {name} 已停用，请重新选择", 409)
                root = self._root(name)
                before = _digest(root)
                view = skill_store.view_skill(name)
                if _digest(root) != before:
                    raise ApplicationError("SKILL_VERSION_CONFLICT", "技能正在变化，请重试", 409)
                snapshots.append({"name": name, "content": build_skill_runtime_content(view),
                                  "source": view["mclaw_skill"]["source"], "version": before})
        if sum(len(item["content"].encode("utf-8")) for item in snapshots) > MAX_TEXT_BYTES:
            raise ApplicationError("SKILL_SNAPSHOT_TOO_LARGE", "所选技能正文过大，请减少选择", 413)
        return snapshots

    def prepare(self, source: str, user_intent: str = "") -> dict:
        result = install_service.install_prepare(source, user_intent=user_intent)
        return self.draft_detail(result["drafting_id"])

    def _draft_root(self, drafting_id: str) -> Path:
        root = get_skill_drafting_dir() / validate_drafting_id(drafting_id)
        if root.is_symlink() or not root.resolve().is_relative_to(get_skill_drafting_dir().resolve()):
            raise ApplicationError("SKILL_PATH_UNSAFE", "安装草稿目录越界")
        return root

    def _confirmation_path(self, drafting_id: str) -> Path:
        return self.home / "web" / "skill-confirmations" / (validate_drafting_id(drafting_id) + ".json")

    def draft_detail(self, drafting_id: str) -> dict:
        with self._guard("draft:" + drafting_id):
            root = self._draft_root(drafting_id)
            if not root.is_dir():
                raise ApplicationError("SKILL_DRAFT_NOT_FOUND", "安装草稿不存在或已处理", 404)
            _digest(root)
            _, manifest = install_service._read_manifest(drafting_id)
            # Refresh the review if a local editor changed the prepared files.
            from mclaw.skills_hub.security import review_skill_package, write_security_review
            review = review_skill_package(root / "skill", source=manifest["source"]["type"])
            write_security_review(root, review)
            blocked = review.get("install_allowed") is False or review.get("verdict") == "dangerous"
            manifest["status"] = "blocked" if blocked else "prepared"
            install_service._write_manifest(root, manifest)
            content = _text(root / "skill" / "SKILL.md")
            version = _digest(root)
            token = secrets.token_urlsafe(32)
            confirmation_path = self._confirmation_path(drafting_id)
            try:
                previous = json.loads(confirmation_path.read_text(encoding="utf-8"))
                if previous.get("version") == version:
                    token = previous["token"]
            except (OSError, ValueError, KeyError):
                pass
            confirmation_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_json_write(confirmation_path, {"token": token, "version": version})
            files = [path.relative_to(root / "skill").as_posix() for path in sorted((root / "skill").rglob("*"))
                     if path.is_file()]
            return {"drafting_id": drafting_id, "skill_name": manifest["skill_name"],
                    "source": manifest["source"], "content": content, "files": files,
                    "review": review, "dependency_hints": manifest.get("dependency_hints", []),
                    "blocked": blocked, "confirmation_token": token}

    def confirm(self, drafting_id: str, token: str) -> dict:
        with self._guard("draft:" + drafting_id):
            root = self._draft_root(drafting_id)
            try:
                confirmation = json.loads(self._confirmation_path(drafting_id).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                confirmation = {}
            if token and hmac.compare_digest(str(confirmation.get("token", "")), token) and confirmation.get("result"):
                return confirmation["result"]
            if (not token or not hmac.compare_digest(str(confirmation.get("token", "")), token)
                    or not root.is_dir() or confirmation.get("version") != _digest(root)):
                raise ApplicationError("SKILL_CONFIRMATION_CHANGED", "安装内容已变化或确认已过期，请重新查看详情", 409)
            result = install_service.enable_drafting(drafting_id)
            atomic_json_write(self._confirmation_path(drafting_id), {**confirmation, "result": result})
            return result

    def draft_file(self, drafting_id: str, path: str) -> dict:
        with self._guard("draft:" + drafting_id):
            root = self._draft_root(drafting_id) / "skill"
            if not root.is_dir():
                raise ApplicationError("SKILL_DRAFT_NOT_FOUND", "安装草稿不存在或已处理", 404)
            version = _digest(root)
            rel = Path(path) if path in {"SKILL.md", "mclaw_skill.yaml", "skill_evolution.json"} else validate_relative_skill_path(path)
            target = ensure_within_dir(root, rel)
            if not target.is_file():
                raise ApplicationError("SKILL_FILE_NOT_FOUND", "技能文件不存在", 404)
            return {"path": rel.as_posix(), "content": _text(target), "version": version, "editable": False}

    def cancel(self, drafting_id: str) -> dict:
        with self._guard("draft:" + drafting_id):
            self._draft_root(drafting_id)
            result = install_service.cancel_drafting(drafting_id)
            path = self._confirmation_path(drafting_id)
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                record = {}
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_json_write(path, {**record, "result": result})
            return result
