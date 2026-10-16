# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Editable shared memories and inspectable local diagnostics."""

from __future__ import annotations

import datetime
import os
import re
import time
from dataclasses import asdict
from pathlib import Path

from mclaw.application.sessions import ApplicationError
from mclaw.system.lock import file_lock
from mclaw.tools.memory_tool import MemoryStore

_LABELS = {"memory": "长期记忆", "user": "个人偏好"}
_CHECK_NAMES = {
    "Agent Runtime": "任务执行", "Command Interface": "命令入口", "Local Storage": "本地存储",
    "TUI": "终端界面", "Pet": "桌面形象", "Model Client": "模型连接", "File Operation": "文件操作",
    "Terminal": "终端工具", "Vision Analysis": "图片理解", "Web Search": "网页搜索",
    "Browser Tools": "浏览器操作", "Weixin": "微信", "DingTalk": "钉钉", "Dsoftbus Runtime": "设备协作",
}


class _BoundMemoryStore(MemoryStore):
    def __init__(self, home: Path, **options) -> None:
        super().__init__(**options)
        self.home = home

    def get_memory_dir(self) -> Path:
        return self.home / "memories"

    def _path_for(self, target: str) -> Path:
        return self.get_memory_dir() / ("USER.md" if target == "user" else "MEMORY.md")


class PreferencesService:
    def __init__(self, home: Path) -> None:
        self.home = Path(home).resolve()

    def _store(self) -> MemoryStore:
        import yaml

        path = self.home / "config.yaml"
        try:
            config = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
            memory = (config or {}).get("memory") or {}
            limits = {key: max(1, int(memory.get(key, default)))
                      for key, default in (("memory_char_limit", 2200), ("user_char_limit", 1375))}
        except (ValueError, TypeError, AttributeError, OSError, yaml.YAMLError) as exc:
            raise ApplicationError("CONFIG_INVALID", "记忆配置无法读取，请检查设置", 409) from exc
        return _BoundMemoryStore(self.home, **limits)

    def _target(self, target: str) -> None:
        if target not in _LABELS:
            raise ApplicationError("MEMORY_NOT_FOUND", "找不到这份记忆", 404)

    def _decorate(self, document: dict) -> dict:
        target = document["target"]
        history = []
        audit = self.home / "memories" / "audit.log"
        if audit.exists():
            with audit.open("rb") as stream:
                stream.seek(max(0, audit.stat().st_size - 64000))
                lines = stream.read().decode("utf-8", errors="replace").splitlines()
            for line in reversed(lines):
                match = re.match(r"\[([^]]+)\]\s+(\S+)\s+(memory|user)\s*\|\s*(.*)", line)
                if match and match[3] == target:
                    history.append({"at": match[1], "action": match[2].lower(), "summary": match[4]})
                if len(history) == 20:
                    break
        return {**document, "label": _LABELS[target], "history": history}

    def read(self, target: str) -> dict:
        self._target(target)
        try:
            return self._decorate(self._store().read_document(target))
        except (OSError, UnicodeError) as exc:
            raise ApplicationError("MEMORY_UNAVAILABLE", "无法读取记忆文件，请检查文件权限与编码", 409) from exc

    def update(self, target: str, content: str, revision: str) -> dict:
        self._target(target)
        if not isinstance(content, str) or not isinstance(revision, str):
            raise ApplicationError("INVALID_MEMORY", "记忆内容或版本格式错误")
        try:
            result = self._store().update_document(target, content, revision)
        except (OSError, UnicodeError) as exc:
            raise ApplicationError("MEMORY_UNAVAILABLE", "无法保存记忆，请检查文件权限与编码", 409) from exc
        if not result["success"]:
            code = result["code"]
            messages = {"MEMORY_CONFLICT": "记忆已被更新，请对照最新内容后再保存",
                        "MEMORY_TOO_LONG": "内容超过记忆长度限制，请精简后保存",
                        "MEMORY_CONTENT_REJECTED": "内容未通过记忆检查，请移除指令覆盖或凭据读取内容"}
            error = ApplicationError(code, messages[code], 409 if code == "MEMORY_CONFLICT" else 422)
            if "current" in result:
                error.current = self._decorate(result["current"])
            raise error
        audit = self.home / "memories" / "audit.log"
        with file_lock(audit.with_suffix(".lock")):
            with audit.open("a", encoding="utf-8") as stream:
                stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                stream.write(f"[{stamp}] EDIT    {target:6s} | 手动保存，{len(result['document']['content'])} 字\n")
        return self._decorate(result["document"])

    def diagnostics(self) -> dict:
        from dotenv import dotenv_values
        from mclaw.doctor import _product_doctor_lines, format_doctor, run_doctor
        from mclaw.runtime.secrets import redact_secret_values

        results = run_doctor()
        checks = [asdict(line) for line in _product_doctor_lines(results)]
        for item in checks:
            item["name"] = _CHECK_NAMES.get(item["name"], item["name"])
        keys = {**os.environ, **dotenv_values(self.home / ".env")}
        secret_values = [value for key, value in keys.items()
                         if re.search(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL", key, re.I) and value]
        payload = {"checked_at": time.time(), "checks": checks, "report": format_doctor(results),
                   "summary": {"passed": sum(row["status"] == "OK" for row in checks),
                               "warnings": sum(row["status"] == "WARN" for row in checks),
                               "failed": sum(row["status"] == "FAIL" for row in checks)}}
        return redact_secret_values(payload, secret_values)
