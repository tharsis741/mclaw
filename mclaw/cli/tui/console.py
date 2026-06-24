"""Console adapters for prompt_toolkit + Rich output."""

from __future__ import annotations

import os
import sys

from rich.console import Console


def configure_text_output() -> None:
    """Prefer UTF-8 at the CLI output boundary on Windows terminals."""

    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleOutputCP(65001)
            kernel32.SetConsoleCP(65001)
        except Exception:
            pass

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


class MClawConsole:
    """Rich Console that routes output through prompt_toolkit's renderer."""

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
        text = capture.get()
        if not text:
            return
        for line in text.rstrip("\n").splitlines():
            self._printer(line)


def cprint(text: str):
    """Print ANSI-colored text through prompt_toolkit's renderer."""

    configure_text_output()
    from prompt_toolkit import print_formatted_text
    from prompt_toolkit.formatted_text import ANSI

    try:
        print_formatted_text(ANSI(text))
    except Exception:
        print(text)


def print_rich(*args, **kwargs) -> None:
    """Print Rich content directly to stdout for non-interactive CLI commands."""

    configure_text_output()
    Console(highlight=False).print(*args, **kwargs)


def print_plain(*args, **kwargs) -> None:
    """Print plain CLI text through the shared TUI output boundary."""

    configure_text_output()
    print(*args, **kwargs)
