# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Input routing and turn coordination for interactive runtimes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .commands import is_slash_command
from .interactive import InteractiveRuntime


@dataclass(frozen=True)
class RuntimeTurnHooks:
    """Host operations used by the UI-neutral turn coordinator."""

    drain_pet_commands: Callable[[], None]
    pump_background_watchers: Callable[[], None]
    drain_background_completions: Callable[[], None]
    has_pending_secret_request: Callable[[], bool]
    handle_secret_request: Callable[[str], None]
    has_pending_skill_import_confirmation: Callable[[], bool]
    handle_skill_import_confirmation: Callable[[str], None]
    has_pending_key_setup: Callable[[], bool]
    cancel_key_setup: Callable[[], None]
    complete_key_setup: Callable[[str], None]
    render_key_input_cancelled: Callable[[], None]
    should_skip_next_prompt: Callable[[], bool]
    consume_skip_next_prompt: Callable[[], None]
    echo_user_message: Callable[[str], None]
    echo_command: Callable[[str], None]
    dispatch_slash: Callable[[str], bool]
    is_ui_running: Callable[[], bool]
    exit_ui: Callable[[], None]
    begin_turn: Callable[[str], None]
    run_turn: Callable[[str], None]
    finish_turn: Callable[[], None]
    pump_scheduler: Callable[[], None] = lambda: None
    has_pending_scheduler_input: Callable[[], bool] = lambda: False
    handle_scheduler_input: Callable[[str], None] = lambda _text: None


class RuntimeTurnCoordinator:
    """Routes submitted input and owns the agent turn state machine."""

    def __init__(self, runtime: InteractiveRuntime, hooks: RuntimeTurnHooks) -> None:
        self.runtime = runtime
        self.hooks = hooks

    def on_idle(self) -> None:
        if not self.runtime.agent_running:
            self.hooks.drain_pet_commands()
            self.hooks.pump_background_watchers()
            self.hooks.drain_background_completions()
            self.hooks.pump_scheduler()

    def handle_input(self, user_input: str) -> None:
        if self.hooks.has_pending_secret_request():
            self.hooks.handle_secret_request(user_input)
            return

        if not user_input:
            self.hooks.drain_pet_commands()
            self.hooks.pump_scheduler()
            return

        if self.hooks.has_pending_scheduler_input():
            self.hooks.handle_scheduler_input(user_input)
            return

        if self.hooks.has_pending_skill_import_confirmation():
            self.hooks.handle_skill_import_confirmation(user_input)
            return

        if self.hooks.has_pending_key_setup():
            if user_input.strip().lower() == "/cancel":
                self.hooks.render_key_input_cancelled()
                self.hooks.cancel_key_setup()
                return
            self.hooks.complete_key_setup(user_input.strip())
            return

        if is_slash_command(user_input):
            if not self.hooks.dispatch_slash(user_input):
                self.runtime.request_exit()
                if self.hooks.is_ui_running():
                    self.hooks.exit_ui()
            return

        if self.hooks.should_skip_next_prompt():
            self.hooks.consume_skip_next_prompt()
        else:
            self.hooks.echo_user_message(user_input)

        self.hooks.begin_turn(user_input)
        try:
            self.hooks.run_turn(user_input)
        finally:
            self.hooks.finish_turn()

        self.hooks.pump_background_watchers()
        self.hooks.drain_background_completions()
        self.hooks.pump_scheduler()
