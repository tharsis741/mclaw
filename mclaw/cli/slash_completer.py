# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""prompt_toolkit completer for slash commands and skills."""

from __future__ import annotations

from prompt_toolkit.completion import Completer, Completion

from mclaw.cli.runtime.commands import iter_builtin_commands
from mclaw.cli.skill_registry import SkillRegistry


def slash_token_before_cursor(document) -> str | None:
    """Return the current slash token immediately before the cursor."""
    before_cursor = document.text_before_cursor
    if not before_cursor:
        return None

    slash_index = before_cursor.rfind("/")
    if slash_index < 0:
        return None

    token = before_cursor[slash_index:]
    if any(char.isspace() for char in token):
        return None
    return token


class SlashCompleter(Completer):
    """Provide completions for /commands and skills when user types '/'."""

    def __init__(self, skill_registry: SkillRegistry):
        self.skill_registry = skill_registry

    def get_completions(self, document, complete_event):
        token = slash_token_before_cursor(document)
        if not token:
            return

        prefix = token[1:].lower()
        start_position = -len(token)

        # Built-in commands
        for spec in iter_builtin_commands(visible_only=True):
            if spec.name.startswith(prefix):
                yield Completion(
                    f"/{spec.name}",
                    start_position=start_position,
                    display=f"/{spec.name}",
                    display_meta=spec.description,
                )

        # Skills (defensive refresh if cache is stale)
        if not self.skill_registry._last_scan:
            self.skill_registry.refresh()

        for skill in self.skill_registry.list_skills():
            if skill.name.startswith(prefix):
                yield Completion(
                    f"/{skill.name}",
                    start_position=start_position,
                    display=f"/{skill.name}",
                    display_meta=f"[技能] {skill.description}",
                )
