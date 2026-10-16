# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import base64
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from mclaw.runtime.base import Runtime
from mclaw.runtime.features import RuntimeFeatures
from mclaw.runtime.manager import RuntimeManager
from mclaw.runtime.paths import PathPolicy
from mclaw.runtime.process import ProcessProfile
from mclaw.runtime.shell import CWD_BASE64_MARKER, CWD_MARKER, ShellProfile
from mclaw.tools import terminal_tool


def _ps_literal(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


class _LegacyConsoleShell(ShellProfile):
    def argv(self, command: str, cwd: str | Path) -> list[str]:
        argv = super().argv(command, cwd)
        # Reproduce a Chinese Windows console independently of the test runner.
        argv[-1] = (
            "[Console]::OutputEncoding = [Text.Encoding]::GetEncoding(936)\n"
            + argv[-1]
        )
        return argv


@pytest.fixture(params=["pwsh", "powershell.exe"])
def windows_runtime(request, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Runtime:
    if os.name != "nt":
        pytest.skip("Windows shell integration test")
    executable = shutil.which(request.param)
    if executable is None:
        pytest.skip(f"{request.param} is not installed")
    runtime = Runtime(
        shell=_LegacyConsoleShell(
            name="powershell",
            executable=executable,
            family="powershell",
            args_prefix=("-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command"),
        ),
        paths=PathPolicy(mclaw_home=tmp_path / ".mclaw"),
        process=ProcessProfile(),
        features=RuntimeFeatures({}),
    )
    monkeypatch.setattr(RuntimeManager, "_current", runtime)
    monkeypatch.setattr(terminal_tool, "_env_registry", {})
    monkeypatch.setattr(terminal_tool, "build_scoped_env", lambda *_: ({}, set()))
    return runtime


def test_terminal_preserves_chinese_cwd_between_calls_and_sessions(
    windows_runtime: Runtime,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "中文 工作区"
    target = workspace / "成果[测试]'目录"
    target.mkdir(parents=True)

    first = json.loads(terminal_tool.terminal_tool(
        f"Set-Location -LiteralPath {_ps_literal(target)}; Write-Output '中文输出'",
        workdir=str(workspace),
        session_key="unicode-session",
    ))
    assert first["returncode"] == 0
    assert first["output"] == "中文输出\n"
    assert first["cwd"] == str(target)

    second = json.loads(terminal_tool.terminal_tool(
        "Write-Output (Get-Location).ProviderPath",
        session_key="unicode-session",
    ))
    assert second["returncode"] == 0
    assert second["output"].strip() == str(target)
    assert second["cwd"] == str(target)

    other = json.loads(terminal_tool.terminal_tool(
        "Write-Output (Get-Location).ProviderPath",
        workdir=str(workspace),
        session_key="other-session",
    ))
    assert other["returncode"] == 0
    assert other["cwd"] == str(workspace)
    assert terminal_tool.get_session_cwd("unicode-session") == str(target)


def test_powershell_cwd_survives_command_changing_output_encoding(
    windows_runtime: Runtime,
    tmp_path: Path,
) -> None:
    target = tmp_path / "中文目录"
    target.mkdir()
    result = windows_runtime.exec(
        "[Console]::OutputEncoding = [Text.Encoding]::GetEncoding(936)",
        cwd=target,
        timeout=10,
    )
    assert result.returncode == 0
    assert result.cwd == str(target)
    assert result.output == ""
    assert windows_runtime.exec("Write-Output 'ok'", cwd=result.cwd, timeout=10).returncode == 0


def test_powershell_preserves_failed_command_exit_code_and_chinese_cwd(
    windows_runtime: Runtime,
    tmp_path: Path,
) -> None:
    target = tmp_path / "失败命令目录"
    target.mkdir()
    result = windows_runtime.exec(
        f"& {_ps_literal(sys.executable)} -c 'import sys; sys.exit(7)'",
        cwd=target,
        timeout=10,
    )
    assert result.returncode == 7
    assert result.cwd == str(target)
    assert result.output == ""


def test_background_powershell_uses_utf8_in_chinese_directory(
    windows_runtime: Runtime,
    tmp_path: Path,
) -> None:
    target = tmp_path / "后台中文目录"
    target.mkdir()
    spawned = windows_runtime.spawn("Write-Output '后台输出'", cwd=target)
    proc = spawned.process
    assert proc is not None
    try:
        output, _ = proc.communicate(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=5)
    cleaned, cwd = windows_runtime._strip_cwd_marker(output)
    assert proc.returncode == 0
    assert cleaned == "后台输出\n"
    assert cwd == str(target)


@pytest.mark.parametrize("encoded", [False, True])
def test_cwd_marker_preserves_unicode_and_hides_internal_metadata(
    tmp_path: Path,
    encoded: bool,
) -> None:
    cwd = str(tmp_path / "中文 工作目录")
    marker = (
        CWD_BASE64_MARKER + base64.b64encode(cwd.encode("utf-8")).decode("ascii")
        if encoded else CWD_MARKER + cwd
    )
    cleaned, reported_cwd = Runtime._strip_cwd_marker(f"normal output\n{marker}\n")
    assert cleaned == "normal output\n"
    assert reported_cwd == cwd


@pytest.mark.parametrize("payload", ["invalid!!", "", "/w==", "AA==", "中文"])
def test_malformed_encoded_cwd_does_not_replace_valid_cwd(payload: str) -> None:
    cleaned, cwd = Runtime._strip_cwd_marker(
        f"{CWD_MARKER}/valid/path\nnormal output\n{CWD_BASE64_MARKER}{payload}\n"
    )
    assert cleaned == "normal output\n"
    assert cwd == "/valid/path"
