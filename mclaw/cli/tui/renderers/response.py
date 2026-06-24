"""Assistant response rendering."""

from __future__ import annotations

from typing import Callable


DEFAULT_ASSISTANT_TITLE = "M-Claw回复:"
DEFAULT_ASSISTANT_TITLE_STYLE = "bold #6CB4EE"
INTERMEDIATE_ASSISTANT_TITLE = "- 任务进展："
INTERMEDIATE_ASSISTANT_TITLE_STYLE = "#6B8E23"
THINKING_ASSISTANT_TITLE = "- 思考中..."
THINKING_ASSISTANT_TITLE_STYLE = "bold #D99A2B"


class ResponseRenderer:
    def __init__(
        self,
        *,
        run_external_output: Callable,
        box_factory: Callable,
        message_sink: Callable[[str], None] | None = None,
    ):
        self._run_external_output = run_external_output
        self._box_factory = box_factory
        self._message_sink = message_sink

    def render_response(
        self,
        text: str,
        *,
        title: str = DEFAULT_ASSISTANT_TITLE,
        title_style: str = DEFAULT_ASSISTANT_TITLE_STYLE,
    ) -> None:
        content = str(text or "").strip()
        if not content:
            return
        if self._message_sink is not None:
            self._message_sink(content)
            return
        self._run_external_output(
            lambda: _print_assistant_response(
                content,
                title=title,
                title_style=title_style,
            )
        )


def _print_assistant_response(
    text: str,
    *,
    title: str = DEFAULT_ASSISTANT_TITLE,
    title_style: str = DEFAULT_ASSISTANT_TITLE_STYLE,
) -> None:
    from rich.markdown import Markdown
    from rich.text import Text

    from mclaw.cli.tui.components import vertical_stack
    from mclaw.cli.tui.console import MClawConsole

    content = str(text or "").strip()
    console = MClawConsole()
    console.print(vertical_stack(Text(title, style=title_style), Markdown(content or " ")))
