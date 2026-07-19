# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from types import SimpleNamespace

from mclaw.cli.tui import console as console_module
from mclaw.cli.tui.console import MClawConsole
from mclaw.cli.tui.renderers.response import _print_assistant_response


def test_console_sends_multiline_render_in_one_printer_call() -> None:
    rendered: list[str] = []

    MClawConsole(printer=rendered.append, color_system=None, width=80).print("first\nsecond")

    assert rendered == ["first\nsecond"]


def test_response_uses_one_direct_ansi_block(monkeypatch) -> None:
    rendered: list[str] = []
    monkeypatch.setattr(console_module, "write_ansi_block", rendered.append)

    _print_assistant_response("first\n\nsecond")

    assert len(rendered) == 1
    assert "first" in rendered[0]
    assert "second" in rendered[0]


def test_direct_ansi_writer_handles_partial_writes(monkeypatch) -> None:
    chunks: list[bytes] = []

    def partial_write(_fd, data) -> int:
        chunk = bytes(data[:3])
        chunks.append(chunk)
        return len(chunk)

    def raw_write(_stream, payload) -> bool:
        console_module._write_all(7, payload)
        return True

    stream = SimpleNamespace(flush=lambda: None, fileno=lambda: 7, isatty=lambda: True)
    monkeypatch.setattr(console_module.sys, "__stdout__", stream)
    monkeypatch.setattr(console_module, "_write_raw_ansi", raw_write)
    monkeypatch.setattr(console_module.os, "write", partial_write)

    console_module.write_ansi_block("一\n二")

    assert b"".join(chunks) == "一\n二\n".encode()


def test_direct_ansi_writer_falls_back_for_unsupported_terminal(monkeypatch) -> None:
    rendered: list[str] = []
    stream = SimpleNamespace(flush=lambda: None, isatty=lambda: True)
    monkeypatch.setattr(console_module.sys, "__stdout__", stream)
    monkeypatch.setattr(console_module, "_write_raw_ansi", lambda _stream, _payload: False)
    monkeypatch.setattr(console_module, "cprint", rendered.append)

    console_module.write_ansi_block("answer")

    assert rendered == ["answer"]
