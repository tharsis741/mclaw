# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Console adapters for prompt_toolkit + Rich output."""

from __future__ import annotations

import logging
import os
import sys

from rich.console import Console

logger = logging.getLogger(__name__)


def configure_text_output() -> None:
    """Prefer UTF-8 at the CLI output boundary on Windows terminals."""

    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleOutputCP(65001)
            kernel32.SetConsoleCP(65001)
        except Exception as exc:
            logger.debug("Windows console UTF-8 codepage setup failed: %s", exc)

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception as exc:
            logger.debug("Stream UTF-8 reconfigure failed: %s", exc)


class MClawConsole:
    """Rich Console that emits one rendered block through the selected printer."""

    def __init__(self, printer=None, *, color_system: str = "truecolor", width: int | None = None):
        configure_text_output()
        self._printer = printer or cprint
        self._inner = Console(
            force_terminal=True,
            color_system=color_system,
            width=width,
            soft_wrap=False,
            legacy_windows=False,
        )

    def print(self, *args, **kwargs):
        with self._inner.capture() as capture:
            self._inner.print(*args, **kwargs)
        text = capture.get().rstrip("\n")
        if not text:
            return
        self._printer(text)


def cprint(text: str):
    """Print ANSI-colored text through prompt_toolkit's renderer."""

    configure_text_output()
    from prompt_toolkit import print_formatted_text
    from prompt_toolkit.formatted_text import ANSI

    try:
        print_formatted_text(ANSI(text))
    except Exception as exc:
        logger.debug("prompt_toolkit ANSI print failed: %s", exc)
        print(text)


def write_ansi_block(text: str) -> None:
    """Write one rendered ANSI block directly when the terminal supports it."""

    configure_text_output()
    stream = sys.__stdout__
    if stream is None or not getattr(stream, "isatty", lambda: False)():
        cprint(text)
        return

    payload = (text.rstrip("\n") + "\n").encode("utf-8", errors="replace")
    try:
        stream.flush()
        if not _write_raw_ansi(stream, payload):
            cprint(text)
    except (AttributeError, OSError, ValueError) as exc:
        logger.debug("Direct ANSI block write failed: %s", exc)
        cprint(text)


def _write_raw_ansi(stream, payload: bytes) -> bool:
    if os.name != "nt":
        _write_all(stream.fileno(), payload)
        return True

    try:
        import ctypes
        import msvcrt

        handle = ctypes.c_void_p(msvcrt.get_osfhandle(stream.fileno()))
        original_mode = ctypes.c_ulong()
        kernel32 = ctypes.windll.kernel32
        if not kernel32.GetConsoleMode(handle, ctypes.byref(original_mode)):
            return False
        if not kernel32.SetConsoleMode(handle, original_mode.value | 0x0001 | 0x0004):
            return False
        try:
            _write_all(stream.fileno(), payload)
        finally:
            kernel32.SetConsoleMode(handle, original_mode.value)
        return True
    except Exception:
        return False


def _write_all(fd: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(fd, remaining)
        if written <= 0:
            raise OSError("terminal write returned no bytes")
        remaining = remaining[written:]


def print_rich(*args, **kwargs) -> None:
    """Print Rich content directly to stdout for non-interactive CLI commands."""

    configure_text_output()
    Console(highlight=False).print(*args, **kwargs)


def print_plain(*args, **kwargs) -> None:
    """Print plain CLI text through the shared TUI output boundary."""

    configure_text_output()
    print(*args, **kwargs)
