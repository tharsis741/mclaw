# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Runtime-aware file search providers.

SearchProfile chooses one available backend in priority order: ripgrep,
PowerShell on Windows, grep/find, then Python. If the selected native backend
cannot produce a result, the call falls back directly to Python scanning.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

from mclaw.runtime.paths import CREDENTIAL_GLOBS
from mclaw.runtime.process import run_captured_process
from mclaw.tools.cancellation import cancellation_checkpoint


class SearchProfile:
    """Search provider cascade bound to the active runtime path policy."""
    def __init__(self, runtime: object) -> None:
        self.runtime = runtime
        self.provider = self._select_provider()

    @property
    def kind(self) -> str:
        return str(getattr(self.runtime, "kind", ""))

    def _select_provider(self) -> str:
        """Choose the fastest available search backend for this host."""
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
        cancel_event: threading.Event | None = None,
    ) -> str:
        """Search a directory after path authorization and provider fallback."""
        cancellation_checkpoint(cancel_event)
        decision = self.runtime.paths.check("search", directory)
        cancellation_checkpoint(cancel_event)
        if not decision.allowed:
            raise PermissionError(decision.error_message())
        root = str(decision.resolved)
        effective_limit = max(1, min(int(limit or 50), 200))
        provider = self.provider
        if provider == "rg":
            result = self._rg(
                root,
                pattern,
                file_pattern,
                effective_limit,
                cancel_event=cancel_event,
            )
            if result is not None:
                return result
        if provider == "powershell":
            result = self._powershell(
                root,
                pattern,
                file_pattern,
                effective_limit,
                cancel_event=cancel_event,
            )
            if result is not None:
                return result
        if provider == "grep_find":
            result = self._grep_find(
                root,
                pattern,
                file_pattern,
                effective_limit,
                cancel_event=cancel_event,
            )
            if result is not None:
                return result
        cancellation_checkpoint(cancel_event)
        return self._python_regex(
            root,
            pattern,
            file_pattern,
            effective_limit,
            cancel_event=cancel_event,
        )

    def _rg(
        self,
        root: str,
        pattern: str,
        file_pattern: str | None,
        limit: int,
        *,
        cancel_event: threading.Event | None = None,
    ) -> str | None:
        cancellation_checkpoint(cancel_event)
        cmd = ["rg"]
        for credential_glob in CREDENTIAL_GLOBS:
            cmd.extend(("--glob", credential_glob))
        cmd.extend(("--glob", "!**/.git/**", "--color=never", "--no-heading", "-n", pattern, root))
        if file_pattern:
            cmd[1:1] = ["--glob", file_pattern]
        try:
            result = run_captured_process(cmd, timeout=30, cancel_event=cancel_event)
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            cancellation_checkpoint(cancel_event)
            return None
        cancellation_checkpoint(cancel_event)
        if result.returncode == 1 and not result.stdout:
            return "[no matches found]"
        if result.returncode in (0, 1):
            return self._cap_lines(result.stdout.splitlines(), limit)
        return result.stderr.strip() or None

    def _powershell(
        self,
        root: str,
        pattern: str,
        file_pattern: str | None,
        limit: int,
        *,
        cancel_event: threading.Event | None = None,
    ) -> str | None:
        cancellation_checkpoint(cancel_event)
        exe = shutil.which("pwsh") or shutil.which("powershell.exe")
        if not exe:
            return None
        escaped_root = root.replace("'", "''")
        escaped_pattern = pattern.replace("'", "''")
        escaped_filter = file_pattern.replace("'", "''") if file_pattern else ""
        name_filter = f"$_.Name -like '{escaped_filter}' -and " if escaped_filter else ""
        credential_names = "@('.git-credentials','.netrc','.npmrc','.pypirc')"
        private_keys = "@('id_rsa','id_dsa','id_ecdsa','id_ed25519')"
        filter_line = (
            f"| Where-Object {{ {name_filter}"
            "$_.Name -notlike '.env*' "
            f"-and $_.Name -notin {credential_names} "
            f"-and -not ($_.Name -in {private_keys} -and $_.Directory.Name -eq '.ssh') "
            "-and $_.FullName -notmatch '[\\/]\\.aws[\\/]credentials$' "
            "-and $_.FullName -notmatch '[\\/]\\.kube[\\/]config$' "
            "-and $_.FullName -notmatch '[\\/]\\.docker[\\/]config\\.json$' }"
        )
        script = (
            "$ErrorActionPreference='Stop';"
            f"$files = Get-ChildItem -LiteralPath '{escaped_root}' -Recurse -File {filter_line};"
            f"$rows = $files | Select-String -Pattern '{escaped_pattern}' | Select-Object -First {limit} "
            "@{Name='Path';Expression={$_.Path}},LineNumber,Line;"
            "$rows | ConvertTo-Json -Compress"
        )
        try:
            result = run_captured_process(
                [exe, "-NoLogo", "-NoProfile", "-Command", script],
                timeout=30,
                cancel_event=cancel_event,
            )
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            cancellation_checkpoint(cancel_event)
            return None
        cancellation_checkpoint(cancel_event)
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

    def _grep_find(
        self,
        root: str,
        pattern: str,
        file_pattern: str | None,
        limit: int,
        *,
        cancel_event: threading.Event | None = None,
    ) -> str | None:
        cancellation_checkpoint(cancel_event)
        try:
            find_result = run_captured_process(
                ["find", root, "-type", "f"],
                timeout=30,
                cancel_event=cancel_event,
            )
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            cancellation_checkpoint(cancel_event)
            return None
        cancellation_checkpoint(cancel_event)
        if find_result.returncode != 0:
            return None
        matches: list[str] = []
        for file_name in find_result.stdout.splitlines():
            cancellation_checkpoint(cancel_event)
            if file_pattern and not fnmatch.fnmatch(Path(file_name).name, file_pattern):
                continue
            if not self.runtime.paths.check("read", file_name).allowed:
                continue
            try:
                grep = run_captured_process(
                    ["grep", "-n", "-E", pattern, file_name],
                    timeout=10,
                    cancel_event=cancel_event,
                )
            except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
                cancellation_checkpoint(cancel_event)
                return None
            cancellation_checkpoint(cancel_event)
            if grep.returncode not in (0, 1):
                continue
            for line in grep.stdout.splitlines():
                cancellation_checkpoint(cancel_event)
                matches.append(f"{file_name}:{line}")
                if len(matches) >= limit:
                    return "\n".join(matches)
        return "\n".join(matches) if matches else "[no matches found]"

    def _python_regex(
        self,
        root: str,
        pattern: str,
        file_pattern: str | None,
        limit: int,
        *,
        cancel_event: threading.Event | None = None,
    ) -> str:
        """Final pure-Python fallback with scan and file-size guards."""
        cancellation_checkpoint(cancel_event)
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            return f"[invalid regex: {exc}]"
        matches: list[str] = []
        searched = 0
        for current_root, dirs, files in os.walk(root):
            cancellation_checkpoint(cancel_event)
            dirs[:] = [item for item in dirs if item != ".git"]
            for file_name in files:
                cancellation_checkpoint(cancel_event)
                if file_pattern and not fnmatch.fnmatch(file_name, file_pattern):
                    continue
                path = os.path.join(current_root, file_name)
                if not self.runtime.paths.check("read", path).allowed:
                    continue
                searched += 1
                if searched > 5000:
                    return "[search limit reached: scanned 5000 files, narrow the directory]"
                try:
                    if os.path.getsize(path) > 1024 * 1024:
                        continue
                    with open(path, encoding="utf-8", errors="replace") as handle:
                        for lineno, line in enumerate(handle, 1):
                            cancellation_checkpoint(cancel_event)
                            if compiled.search(line):
                                matches.append(f"{path}:{lineno}:{line.rstrip()}")
                                if len(matches) >= limit:
                                    return "\n".join(matches)
                except InterruptedError:
                    raise
                except OSError:
                    cancellation_checkpoint(cancel_event)
                    continue
        return "\n".join(matches) if matches else "[no matches found]"

    @staticmethod
    def _cap_lines(lines: list[str], limit: int) -> str:
        """Apply a consistent result cap across provider outputs."""
        if not lines:
            return "[no matches found]"
        if len(lines) <= limit:
            return "\n".join(lines)
        kept = lines[:limit]
        kept.append(f"\n[search truncated: {len(lines) - limit} more matches omitted. Use a narrower pattern or increase limit.]")
        return "\n".join(kept)
