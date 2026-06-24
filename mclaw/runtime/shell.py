"""Shell profile definitions for runtimes."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

CWD_MARKER = "__MCLAW_CWD__="


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _sh_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


@dataclass(frozen=True)
class ShellProfile:
    name: str
    executable: str
    family: str
    args_prefix: tuple[str, ...]
    login: bool = False
    supports_tty: bool = False

    def script(self, command: str, cwd: str | Path) -> str:
        cwd_text = str(cwd)
        if self.family == "powershell":
            return "\n".join(
                [
                    "$ErrorActionPreference = 'Continue'",
                    f"Set-Location -LiteralPath {_ps_quote(cwd_text)}",
                    "& {",
                    command,
                    "}",
                    "$mclaw_ec = if ($global:LASTEXITCODE -is [int]) { $global:LASTEXITCODE } elseif ($?) { 0 } else { 1 }",
                    f"Write-Output ({_ps_quote(CWD_MARKER)} + (Get-Location).ProviderPath)",
                    "exit $mclaw_ec",
                ]
            )
        if self.family == "cmd":
            return f'cd /d "{cwd_text}" && ({command}) & echo {CWD_MARKER}%CD%'
        return "\n".join(
            [
                f"cd {_sh_quote(cwd_text)} || exit 1",
                command,
                "mclaw_ec=$?",
                f"printf '\\n{CWD_MARKER}%s\\n' \"$(pwd -P 2>/dev/null || pwd)\"",
                "exit $mclaw_ec",
            ]
        )

    def argv(self, command: str, cwd: str | Path) -> list[str]:
        script = self.script(command, cwd)
        return [self.executable, *self.args_prefix, script]
