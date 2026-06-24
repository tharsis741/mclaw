# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Runtime-aware file search providers.

SearchProfile selects the fastest available provider for the active host and
falls back through ripgrep, shell tools, and Python scanning with consistent
result shaping.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
from pathlib import Path


class SearchProfile:
    def __init__(self, runtime: object) -> None:
        self.runtime = runtime
        self.provider = self._select_provider()

    @property
    def kind(self) -> str:
        return str(getattr(self.runtime, "kind", ""))

    def _select_provider(self) -> str:
        if shutil.which("rg"):
            return "rg"
        if self.kind == "windows" and (shutil.which("powershell.exe") or shutil.which("pwsh")):
            return "powershell"
        if shutil.which("grep") and shutil.which("find"):
            return "grep_find"
        return "python_regex"

    def search(
        self,
        directory: str,
        pattern: str,
        *,
        file_pattern: str | None = None,
        limit: int = 50,
    ) -> str:
        decision = self.runtime.paths.check("search", directory)
        if not decision.allowed:
            raise PermissionError(decision.error_message())
        root = str(decision.resolved)
        effective_limit = max(1, min(int(limit or 50), 200))
        provider = self.provider
        if provider == "rg":
            result = self._rg(root, pattern, file_pattern, effective_limit)
            if result is not None:
                return result
        if provider == "powershell":
            result = self._powershell(root, pattern, file_pattern, effective_limit)
            if result is not None:
                return result
        if provider == "grep_find":
            result = self._grep_find(root, pattern, file_pattern, effective_limit)
            if result is not None:
                return result
        return self._python_regex(root, pattern, file_pattern, effective_limit)

    def _rg(self, root: str, pattern: str, file_pattern: str | None, limit: int) -> str | None:
        cmd = ["rg", "--color=never", "--no-heading", "-n", pattern, root]
        if file_pattern:
            cmd[1:1] = ["--glob", file_pattern, "--glob", "!**/.git/**"]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode == 1 and not result.stdout:
            return "[no matches found]"
        if result.returncode in (0, 1):
            return self._cap_lines(result.stdout.splitlines(), limit)
        return result.stderr.strip() or None

    def _powershell(self, root: str, pattern: str, file_pattern: str | None, limit: int) -> str | None:
        exe = shutil.which("pwsh") or shutil.which("powershell.exe")
        if not exe:
            return None
        escaped_root = root.replace("'", "''")
        escaped_pattern = pattern.replace("'", "''")
        escaped_filter = file_pattern.replace("'", "''") if file_pattern else ""
        filter_line = f"| Where-Object {{ $_.Name -like '{escaped_filter}' }}" if escaped_filter else ""
        script = (
            "$ErrorActionPreference='Stop';"
            f"$files = Get-ChildItem -LiteralPath '{escaped_root}' -Recurse -File {filter_line};"
            f"$rows = $files | Select-String -Pattern '{escaped_pattern}' | Select-Object -First {limit} "
            "@{Name='Path';Expression={$_.Path}},LineNumber,Line;"
            "$rows | ConvertTo-Json -Compress"
        )
        try:
            result = subprocess.run([exe, "-NoLogo", "-NoProfile", "-Command", script], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            return None
        raw = result.stdout.strip()
        if not raw:
            return "[no matches found]"
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        rows = data if isinstance(data, list) else [data]
        lines = [
            f"{row.get('Path')}:{row.get('LineNumber')}:{str(row.get('Line') or '').rstrip()}"
            for row in rows
            if isinstance(row, dict)
        ]
        return "\n".join(lines) if lines else "[no matches found]"

    def _grep_find(self, root: str, pattern: str, file_pattern: str | None, limit: int) -> str | None:
        try:
            find_result = subprocess.run(["find", root, "-type", "f"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            return None
        if find_result.returncode != 0:
            return None
        matches: list[str] = []
        for file_name in find_result.stdout.splitlines():
            if file_pattern and not fnmatch.fnmatch(Path(file_name).name, file_pattern):
                continue
            try:
                grep = subprocess.run(["grep", "-n", "-E", pattern, file_name], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10)
            except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
                return None
            if grep.returncode not in (0, 1):
                continue
            for line in grep.stdout.splitlines():
                matches.append(f"{file_name}:{line}")
                if len(matches) >= limit:
                    return "\n".join(matches)
        return "\n".join(matches) if matches else "[no matches found]"

    def _python_regex(self, root: str, pattern: str, file_pattern: str | None, limit: int) -> str:
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            return f"[invalid regex: {exc}]"
        matches: list[str] = []
        searched = 0
        for current_root, dirs, files in os.walk(root):
            dirs[:] = [item for item in dirs if item != ".git"]
            for file_name in files:
                if file_pattern and not fnmatch.fnmatch(file_name, file_pattern):
                    continue
                path = os.path.join(current_root, file_name)
                searched += 1
                if searched > 5000:
                    return "[search limit reached: scanned 5000 files, narrow the directory]"
                try:
                    if os.path.getsize(path) > 1024 * 1024:
                        continue
                    with open(path, encoding="utf-8", errors="replace") as handle:
                        for lineno, line in enumerate(handle, 1):
                            if compiled.search(line):
                                matches.append(f"{path}:{lineno}:{line.rstrip()}")
                                if len(matches) >= limit:
                                    return "\n".join(matches)
                except OSError:
                    continue
        return "\n".join(matches) if matches else "[no matches found]"

    @staticmethod
    def _cap_lines(lines: list[str], limit: int) -> str:
        if not lines:
            return "[no matches found]"
        if len(lines) <= limit:
            return "\n".join(lines)
        kept = lines[:limit]
        kept.append(f"\n[search truncated: {len(lines) - limit} more matches omitted. Use a narrower pattern or increase limit.]")
        return "\n".join(kept)
