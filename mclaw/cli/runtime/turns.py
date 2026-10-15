# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Input routing and turn coordination for interactive runtimes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

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
    get_turn_result: Callable[[], dict[str, Any]] = lambda: {"completed": True}
    queue_notice: Callable[[str], None] = lambda _text: None
    current_model: Callable[[], str] = lambda: ""
    task_input: Callable[[dict[str, Any]], str] = lambda task: task["content"]


class RuntimeTurnCoordinator:
    """Routes submitted input and owns the agent turn state machine."""

    def __init__(self, runtime: InteractiveRuntime, hooks: RuntimeTurnHooks) -> None:
        self.runtime = runtime
        self.hooks = hooks

    def on_idle(self) -> None:
        """Run background maintenance hooks while no agent turn is active."""
        if not self.runtime.agent_running:
            if self.runtime.task_queue is not None:
                if not self.runtime.pending_input.empty():
                    return
                if (
                    self.hooks.has_pending_secret_request()
                    or self.hooks.has_pending_skill_import_confirmation()
                    or self.hooks.has_pending_key_setup()
                    or self.hooks.has_pending_scheduler_input()
                ):
                    return
                if self._run_next_task():
                    return
                if self.runtime.task_queue.snapshot()["paused"]:
                    self.hooks.pump_scheduler()
                    return
            self.hooks.drain_pet_commands()
            self.hooks.pump_background_watchers()
            self.hooks.drain_background_completions()
            self.hooks.pump_scheduler()

    def handle_input(self, user_input: str) -> None:
        """Route submitted text through pending prompts before starting a turn.

        Secret, scheduler, Skill confirmation, and key-setup continuations own
        the next input before slash commands or normal agent turns can run.
        """
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
            if self.runtime.task_queue is not None:
                self.runtime.task_queue.update_model(self.hooks.current_model())
            return

        if self.runtime.task_queue is not None and self._handle_queue_command(user_input):
            return

        if is_slash_command(user_input):
            if not self.hooks.dispatch_slash(user_input):
                self.runtime.request_exit()
                if self.hooks.is_ui_running():
                    self.hooks.exit_ui()
            if self.runtime.task_queue is not None:
                self.runtime.task_queue.update_model(self.hooks.current_model())
            return

        if self.runtime.task_queue is not None:
            self.runtime.task_queue.submit(user_input, self.hooks.current_model())
            # Legacy ASR, pet and Skill adapters can still submit through the
            # control-input inbox. Let the worker drain pending model changes
            # before on_idle starts any of these newly queued tasks.
            return

        self._run_turn(user_input)

    def _run_turn(self, user_input: str, *, pump_background: bool = True) -> None:

        if self.hooks.should_skip_next_prompt():
            self.hooks.consume_skip_next_prompt()
        else:
            self.hooks.echo_user_message(user_input)

        self.hooks.begin_turn(user_input)
        try:
            self.hooks.run_turn(user_input)
        finally:
            self.hooks.finish_turn()

        if pump_background:
            self.hooks.pump_background_watchers()
            self.hooks.drain_background_completions()
            self.hooks.pump_scheduler()

    def _run_next_task(self) -> bool:
        task_queue = self.runtime.task_queue
        task = task_queue.take_next() if task_queue is not None else None
        if task is None:
            return False
        content = self.hooks.task_input(task)
        try:
            self._run_turn(content, pump_background=False)
        except BaseException:
            task_queue.finish({"completed": False, "error": "任务执行异常"})
            raise
        else:
            state = task_queue.finish(self.hooks.get_turn_result())
            if state != "completed":
                self.hooks.queue_notice("当前任务与队列已暂停；/continue 优先继续原任务，Alt+Enter 可补充并继续。")
        return True

    def _handle_queue_command(self, text: str) -> bool:
        command, _, argument = text.strip().partition(" ")
        task_queue = self.runtime.task_queue
        if command == "/continue":
            task_queue.resume(argument)
            task_queue.update_model(self.hooks.current_model())
            self._run_next_task()
            return True
        if command == "/queue":
            action, _, rest = argument.partition(" ")
            if action in {"remove", "edit"}:
                number, _, content = rest.partition(" ")
                try:
                    task_queue.edit(int(number) - 1, None if action == "remove" else content)
                except ValueError as exc:
                    self.hooks.queue_notice(str(exc))
                return True
            snapshot = task_queue.snapshot()
            lines = [f"队列 {len(snapshot['pending'])} 条" + ("（已暂停）" if snapshot["paused"] else "")]
            lines.extend(f"{index}. {item['content']}" for index, item in enumerate(snapshot["pending"], 1))
            lines.append("/queue edit 编号 内容 · /queue remove 编号 · /continue [补充]")
            self.hooks.queue_notice("\n".join(lines))
            return True
        return False
