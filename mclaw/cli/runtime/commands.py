# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Runtime-owned slash command catalog and parsing helpers."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SlashCommandSpec:
    """User-visible metadata for one built-in slash command."""

    name: str
    description: str
    visible: bool = True


@dataclass(frozen=True)
class ParsedSlashCommand:
    """Normalized slash command text separated from its raw argument string."""

    name: str
    args: str = ""
    canonical_name: str = ""


@dataclass(frozen=True)
class CommandDispatchResult:
    """Outcome of routing one slash command through the runtime dispatcher."""

    parsed: ParsedSlashCommand
    handled: bool
    continue_running: bool = True


CommandHandler = Callable[[ParsedSlashCommand], bool | None]
UnknownCommandHandler = Callable[[ParsedSlashCommand], bool | None]
SkillGetter = Callable[[str], Any]
SkillInvoker = Callable[[Any, str, str], bool | None]
UnknownSlashInputHandler = Callable[[str], bool | None]


_COMMANDS: tuple[SlashCommandSpec, ...] = (
    SlashCommandSpec("help", "查看命令列表"),
    SlashCommandSpec("clear", "清屏并开启新会话"),
    SlashCommandSpec("model", "切换大模型"),
    SlashCommandSpec("model-update", "刷新/查看 models.dev 模型库缓存"),
    SlashCommandSpec("search-backend", "切换联网搜索后端"),
    SlashCommandSpec("extract-backend", "切换网页提取后端"),
    SlashCommandSpec("asr-mode", "启用语音输入"),
    SlashCommandSpec("asr-status", "查看语音输入状态"),
    SlashCommandSpec("keyboard-mode", "关闭语音输入并切回键盘"),
    SlashCommandSpec("pet", "控制桌面宠物"),
    SlashCommandSpec("usage", "查看会话用量"),
    SlashCommandSpec("doctor", "检查运行环境和工具可用性"),
    SlashCommandSpec("history", "查看历史会话"),
    SlashCommandSpec("resume", "恢复历史会话"),
    SlashCommandSpec("rollback", "撤销文件变更"),
    SlashCommandSpec("checkpoints", "管理 checkpoint 存储"),
    SlashCommandSpec("title", "设置会话标题"),
    SlashCommandSpec("provider", "查看大模型接入状态"),
    SlashCommandSpec("save", "导出当前会话"),
    SlashCommandSpec("schedule", "管理本地定时任务"),
    SlashCommandSpec("skills", "管理技能"),
    SlashCommandSpec("skill", "Skill subcommands", visible=False),
    SlashCommandSpec("quit", "退出 M-Claw"),
)

_COMMAND_COMPLETIONS: tuple[SlashCommandSpec, ...] = (
    SlashCommandSpec("skill install", "安装外部 Skill"),
    SlashCommandSpec("skill creation", "创建新 Skill"),
)

_COMMAND_BY_NAME = {spec.name: spec for spec in _COMMANDS}


def iter_builtin_commands(*, visible_only: bool = False) -> tuple[SlashCommandSpec, ...]:
    """Return built-in command specs, optionally excluding hidden aliases."""
    commands = _COMMANDS
    if not visible_only:
        return commands
    return tuple(spec for spec in commands if spec.visible)


def iter_builtin_completions() -> tuple[SlashCommandSpec, ...]:
    """Return slash completions, including multi-word command shortcuts."""
    return (*iter_builtin_commands(visible_only=True), *_COMMAND_COMPLETIONS)


def builtin_command_names(*, visible_only: bool = False) -> frozenset[str]:
    """Return canonical built-in names used to reserve the slash namespace."""
    return frozenset(spec.name for spec in iter_builtin_commands(visible_only=visible_only))


def get_builtin_command(name: str) -> SlashCommandSpec | None:
    """Resolve a command name with or without a leading slash."""
    return _COMMAND_BY_NAME.get((name or "").lstrip("/").lower())


def split_slash_command(text: str) -> tuple[str, str]:
    """Split raw slash input into a lower-case command name and raw args."""
    parts = (text or "").strip().split(maxsplit=1)
    if not parts:
        return "", ""
    command = parts[0].lstrip("/").lower()
    args = parts[1] if len(parts) > 1 else ""
    return command, args


def is_slash_command(text: str) -> bool:
    """Return True only for a single leading slash command token."""
    if not text or not text.startswith("/"):
        return False
    first_word = text.split(maxsplit=1)[0]
    return "/" not in first_word[1:]


def canonical_command_name(name: str) -> str:
    """Map known aliases to their canonical command name."""
    spec = get_builtin_command(name)
    return spec.name if spec else (name or "").lstrip("/").lower()


def parse_slash_command(text: str) -> ParsedSlashCommand:
    """Parse raw slash input while preserving the original argument text."""
    name, args = split_slash_command(text)
    return ParsedSlashCommand(
        name=name,
        args=args,
        canonical_name=canonical_command_name(name),
    )


class CommandRouter:
    """Runtime-owned slash command dispatcher.

    Concrete handlers are supplied by the host runtime. This keeps command
    parsing and dispatch semantics out of individual TUI frontends.
    """

    def __init__(
        self,
        handlers: Mapping[str, CommandHandler],
        *,
        unknown_handler: UnknownCommandHandler | None = None,
    ) -> None:
        self._handlers = dict(handlers)
        self._unknown_handler = unknown_handler

    def dispatch(self, text: str) -> CommandDispatchResult:
        parsed = parse_slash_command(text)
        handler = self._handlers.get(parsed.canonical_name)
        if handler is None:
            if self._unknown_handler is None:
                return CommandDispatchResult(parsed=parsed, handled=False)
            result = self._unknown_handler(parsed)
            return CommandDispatchResult(
                parsed=parsed,
                handled=False,
                continue_running=True if result is None else bool(result),
            )

        result = handler(parsed)
        return CommandDispatchResult(
            parsed=parsed,
            handled=True,
            continue_running=True if result is None else bool(result),
        )

    @property
    def command_names(self) -> frozenset[str]:
        return frozenset(self._handlers)


class SlashInputDispatcher:
    """Routes raw slash input to built-in command handlers or Skills.

    Built-in commands win namespace conflicts. Skill invocation is only tried
    after the runtime command catalog declines the slash name.
    """

    def __init__(
        self,
        *,
        builtin_commands: Callable[[], set[str] | frozenset[str]],
        dispatch_builtin: Callable[[str], bool | None],
        get_skill: SkillGetter,
        invoke_skill: SkillInvoker,
        unknown_handler: UnknownSlashInputHandler,
    ) -> None:
        self._builtin_commands = builtin_commands
        self._dispatch_builtin = dispatch_builtin
        self._get_skill = get_skill
        self._invoke_skill = invoke_skill
        self._unknown_handler = unknown_handler

    def dispatch(self, text: str) -> bool:
        command, args = split_slash_command(text)
        if command in self._builtin_commands():
            result = self._dispatch_builtin(text)
            return True if result is None else bool(result)

        skill = self._get_skill(command)
        if skill:
            result = self._invoke_skill(skill, args, text)
            return True if result is None else bool(result)

        result = self._unknown_handler(text)
        return True if result is None else bool(result)
