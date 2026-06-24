"""Runtime process helpers."""

from __future__ import annotations

import hashlib
import os
import re
import signal
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SENSITIVE_ENV_RE = re.compile(r"(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH)", re.IGNORECASE)


def sanitize_subprocess_env(env: dict[str, str] | os._Environ, *, allowed_sensitive: set[str] | None = None) -> dict[str, str]:
    allowed = {str(item) for item in (allowed_sensitive or set())}
    clean: dict[str, str] = {}
    for key, value in dict(env).items():
        text_key = str(key)
        if _SENSITIVE_ENV_RE.search(text_key) and text_key not in allowed:
            continue
        clean[text_key] = str(value)
    return clean


def env_hash(env: dict[str, str]) -> str:
    payload = "\n".join(f"{key}={env[key]}" for key in sorted(env))
    return hashlib.sha256(payload.encode("utf-8", errors="ignore")).hexdigest()[:16]


def kill_process_tree(pid: int) -> None:
    if pid <= 0:
        return
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, timeout=10)
            return
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass
        try:
            os.kill(pid, 9)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        return
    try:
        pgid = os.getpgid(pid)
        os.killpg(pgid, signal.SIGTERM)
        time.sleep(0.5)
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    except (ProcessLookupError, PermissionError, OSError):
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass


@dataclass(frozen=True)
class ProcessProfile:
    start_new_session: bool = True
    stdout_pipe: bool = True
    stderr_to_stdout: bool = True
    stdin_pipe: bool = False


@dataclass
class SpawnResult:
    pid: int
    process: subprocess.Popen | None = None
    pty: Any = None
    cwd: str = ""
    env_hash: str = ""
    shell_profile: str = ""
    used_pty: bool = False
    pty_disabled_reason: str = ""


@contextmanager
def null_file_lock(path: str | Path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+", encoding="utf-8") as lock_file:
        yield lock_file
