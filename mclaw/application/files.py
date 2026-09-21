# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Session-bound local file references and browser uploads."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path

from mclaw.application.sessions import ApplicationError
from mclaw.platform.detect import get_local_device_name
from mclaw.runtime.paths import PathPolicy

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_PREVIEW_BYTES = 256 * 1024
MAX_ATTACHMENTS = 20
_IMAGES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
           ".gif": "image/gif", ".webp": "image/webp"}


class FileService:
    """Keep imports outside the workspace; original local files stay in place."""

    def __init__(self, home: Path) -> None:
        self.home = Path(home).resolve()
        self.root = self.home / "attachments"
        self.database = self.home / "attachments.sqlite3"
        self.home.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("CREATE TABLE IF NOT EXISTS files (session_id TEXT, id TEXT, document TEXT NOT NULL, PRIMARY KEY(session_id, id))")

    def _policy(self):
        from mclaw.runtime.manager import RuntimeManager
        return RuntimeManager.current().paths

    def _path(self, value: str, *, uploaded: bool = False) -> Path:
        if not isinstance(value, str) or not value.strip() or "\0" in value:
            raise ApplicationError("FILE_PATH_INVALID", "请选择一个文件")
        policy = self._policy()
        decision = policy.check("read", value)
        path = decision.resolved
        if not decision.allowed or (policy.is_runtime_internal_path(path) and not
                                    (uploaded and path.is_relative_to(self.root))):
            raise ApplicationError("FILE_ACCESS_DENIED", "此文件不能通过附件入口读取", 403)
        if not path.is_file():
            raise ApplicationError("FILE_UNAVAILABLE", "文件已移动、删除或无法读取", 404)
        return path

    @staticmethod
    def _session(session_id: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session_id):
            raise ApplicationError("SESSION_NOT_FOUND", "会话不存在", 404)

    @staticmethod
    def _name(name: str) -> str:
        if (not isinstance(name, str) or not name.strip() or len(name) > 180
                or any(character in name for character in '<>:"/\\|?*')
                or any(ord(character) < 32 for character in name)
                or name.endswith((".", " ")) or name in {".", ".."}
                or name.split(".", 1)[0].upper() in {"CON", "PRN", "AUX", "NUL", *[f"{prefix}{i}" for prefix in ("COM", "LPT") for i in range(1, 10)]}
                or PathPolicy.is_credential_path(Path(name))):
            raise ApplicationError("FILE_NAME_INVALID", "文件名不受支持，请重命名后添加")
        return name

    def _save(self, session_id: str, record: dict) -> dict:
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("INSERT INTO files VALUES (?, ?, ?)",
                               (session_id, record["id"], json.dumps(record, ensure_ascii=False)))
        return record

    def _record(self, path: Path, *, source: str, workspace: str = "", identifier: str = "") -> dict:
        stat = path.stat()
        suffix = path.suffix.lower()
        return {"id": identifier or f"file_{uuid.uuid4().hex}", "name": path.name, "path": str(path),
                "source": source, "size": stat.st_size, "modified_at": stat.st_mtime,
                "created_at": time.time(), "device": get_local_device_name(), "workspace": workspace,
                "mime_type": _IMAGES.get(suffix) or ("application/pdf" if suffix == ".pdf" else mimetypes.guess_type(path.name)[0] or "application/octet-stream")}

    def upload(self, session_id: str, name: str, content: bytes, workspace: str = "") -> dict:
        self._session(session_id)
        name = self._name(name)
        if len(content) > MAX_UPLOAD_BYTES:
            raise ApplicationError("FILE_TOO_LARGE", "单个附件不能超过 20 MB", 413)
        identifier = f"file_{uuid.uuid4().hex}"
        folder = self.root / session_id / identifier
        folder.mkdir(parents=True, exist_ok=False)
        path = folder / name
        try:
            path.write_bytes(content)
            return self._save(session_id, self._record(path, source="upload", workspace=workspace, identifier=identifier))
        except BaseException:
            path.unlink(missing_ok=True)
            folder.rmdir()
            raise

    def link(self, session_id: str, path_value: str, workspace: str = "", *, source: str = "local") -> dict:
        self._session(session_id)
        path = self._path(path_value)
        return self._save(session_id, self._record(path, source=source, workspace=workspace))

    def receive(self, session_id: str, identifier: str, name: str, content: bytes, *,
                device: str, device_id: str, task_id: str) -> dict:
        """Register an already verified remote receipt; replay keeps one attachment."""
        self._session(session_id)
        name = self._name(name)
        if not re.fullmatch(r"remote_[0-9a-f]{32}", identifier) or len(content) > MAX_UPLOAD_BYTES:
            raise ApplicationError("FILE_RECEIPT_INVALID", "回传文件记录无效")
        try:
            return self.get(session_id, identifier)
        except ApplicationError as error:
            if error.code != "FILE_NOT_FOUND":
                raise
        folder = self.root / session_id / identifier
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.write_bytes(content)
        record = self._record(path, source="remote", identifier=identifier)
        record.update(device=device, remote_device_id=device_id, remote_task_id=task_id)
        return self._save(session_id, record)

    def get(self, session_id: str, identifier: str) -> dict:
        with closing(sqlite3.connect(self.database)) as connection:
            row = connection.execute("SELECT document FROM files WHERE session_id=? AND id=?", (session_id, identifier)).fetchone()
        if row is None:
            raise ApplicationError("FILE_NOT_FOUND", "此文件不属于当前会话", 404)
        return json.loads(row[0])

    def resolve(self, session_id: str, identifier: str) -> tuple[dict, Path]:
        record = self.get(session_id, identifier)
        path = self._path(record["path"], uploaded=record["source"] in {"upload", "remote"})
        return record, path

    def snapshots(self, session_id: str, identifiers: list[str] | None) -> list[dict]:
        if identifiers is None:
            return []
        if (not isinstance(identifiers, list) or len(identifiers) > MAX_ATTACHMENTS
                or any(not isinstance(item, str) for item in identifiers)):
            raise ApplicationError("INVALID_ATTACHMENTS", "一次最多添加 20 个文件")
        result = []
        for identifier in dict.fromkeys(identifiers):
            record, path = self.resolve(session_id, identifier)
            # A task retains the selected source and file metadata. Local originals
            # remain live files and are read through the standard versioned tools.
            result.append(record | {"size": path.stat().st_size, "modified_at": path.stat().st_mtime})
        return result

    def task_inputs(self, session_id: str, attachments: list[dict]) -> list[dict]:
        """Make selected cached files usable by tools without sharing private state."""
        self._session(session_id)
        result = []
        for selected in attachments:
            record, source = self.resolve(session_id, selected["id"])
            if record["source"] not in {"upload", "remote"}:
                result.append(record)
                continue
            with source.open("rb") as stream:
                content = stream.read(MAX_UPLOAD_BYTES + 1)
            if len(content) > MAX_UPLOAD_BYTES:
                raise ApplicationError("FILE_TOO_LARGE", "单个附件不能超过 20 MB", 413)
            digest = hashlib.sha256(content).hexdigest()
            folder = self.home / "_temp" / "inputs" / session_id / (record["id"] + "_" + digest[:16])
            name = self._name(record["name"])
            target = folder / name
            if target.resolve() != target:
                raise ApplicationError("FILE_ACCESS_DENIED", "附件副本目录不能使用链接", 403)
            folder.mkdir(parents=True, exist_ok=True)
            try:
                with target.open("xb") as stream:
                    stream.write(content)
            except FileExistsError:
                with target.open("rb") as stream:
                    existing = stream.read(MAX_UPLOAD_BYTES + 1)
                if existing != content:
                    raise ApplicationError("FILE_CHANGED", "附件副本已修改，请重新添加文件")
            result.append({**record, "path": str(target)})
        return result

    def preview(self, session_id: str, identifier: str) -> dict:
        record, path = self.resolve(session_id, identifier)
        suffix = path.suffix.lower()
        result = {"file": record, "kind": "download", "content": "", "truncated": False}
        if suffix in _IMAGES:
            result["kind"] = "image"
        elif suffix == ".pdf":
            result["kind"] = "pdf"
        else:
            with path.open("rb") as stream:
                raw = stream.read(MAX_PREVIEW_BYTES + 1)
            if b"\0" not in raw:
                try:
                    # Trim a partial last UTF-8 character only when limiting output.
                    text = raw[:MAX_PREVIEW_BYTES].decode("utf-8-sig", errors="replace" if len(raw) > MAX_PREVIEW_BYTES else "strict")
                except UnicodeDecodeError:
                    pass
                else:
                    result.update(kind="markdown" if suffix in {".md", ".markdown"} else "text",
                                  content=text, truncated=len(raw) > MAX_PREVIEW_BYTES)
        return result

    def browse(self, path_value: str) -> dict:
        policy = self._policy()
        decision = policy.check("read", path_value)
        root = decision.resolved
        if not decision.allowed or policy.is_runtime_internal_path(root):
            raise ApplicationError("FILE_ACCESS_DENIED", "此目录不能通过文件入口浏览", 403)
        try:
            entries = []
            truncated = False
            for path in root.iterdir():
                try:
                    if path.name.startswith(".") or getattr(path.stat(), "st_file_attributes", 0) & 6:
                        continue
                except OSError:
                    continue
                current = policy.check("read", path)
                if not current.allowed or policy.is_runtime_internal_path(current.resolved):
                    continue
                if not current.resolved.is_file() and not current.resolved.is_dir():
                    continue
                try:
                    entries.append({"name": path.name, "path": str(current.resolved), "directory": current.resolved.is_dir(),
                                    "size": current.resolved.stat().st_size if current.resolved.is_file() else None})
                except OSError:
                    continue
                if len(entries) == 500:
                    truncated = True
                    break
        except OSError as exc:
            raise ApplicationError("DIRECTORY_UNAVAILABLE", "无法浏览此目录", 404) from exc
        parent = root if policy.is_runtime_internal_path(root.parent) else root.parent
        return {"path": str(root), "parent": str(parent), "entries": sorted(entries, key=lambda item: (not item["directory"], item["name"].casefold())),
                "truncated": truncated, "max_upload_bytes": MAX_UPLOAD_BYTES}


def attachment_context(attachments: list[dict]) -> str:
    """Describe user-selected paths without treating file contents as instructions."""
    if not attachments:
        return ""
    selected = [{"name": item["name"], "path": item["path"], "source": item["source"]} for item in attachments]
    return "\n\n[Attached files]\nThe user attached these local files. Read the relevant files with available tools before using their contents. Uploaded files are copies; local references retain their original paths.\n" + json.dumps(selected, ensure_ascii=False)
