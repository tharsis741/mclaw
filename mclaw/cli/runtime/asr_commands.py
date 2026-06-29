# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Runtime coordination for interactive ASR commands."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RuntimeAsrCommandHooks:
    """Host operations used by UI-neutral ASR command flows."""

    sync_asr_config: Callable[[dict[str, Any]], None]
    ensure_asr_service: Callable[[], Any]
    stop_asr_service: Callable[[], None]
    set_input_mode: Callable[[str], None]
    set_asr_status_text: Callable[[str], None]
    push_to_talk_label: Callable[[], str]
    resolve_status_config: Callable[[], dict[str, Any]]
    get_service_status: Callable[[], dict[str, Any]]
    render_notice: Callable[[str, str], None]
    render_success: Callable[[str], None]
    render_error: Callable[[str], None]
    render_asr_status: Callable[[dict[str, Any], dict[str, Any]], None]


class RuntimeAsrCommandCoordinator:
    """Handles ASR slash commands without depending on a concrete TUI."""

    def __init__(self, hooks: RuntimeAsrCommandHooks) -> None:
        self.hooks = hooks

    def handle_asr_mode(self, raw_args: str = "") -> None:
        mode = str(raw_args or "").strip().split()[0].lower() if raw_args else ""
        if mode not in {"", "wake_word", "push_to_talk"}:
            self.hooks.render_notice("用法: /asr-mode [wake_word|push_to_talk]", "warning")
            return

        if mode == "wake_word":
            self.hooks.sync_asr_config({"listen_mode": "wake_word", "require_wake_word": True})
        elif mode == "push_to_talk":
            self.hooks.sync_asr_config({
                "listen_mode": "push_to_talk",
                "require_wake_word": False,
                "push_to_talk_behavior": "tap_once",
            })

        try:
            service = self.hooks.ensure_asr_service()
        except RuntimeError as exc:
            self.hooks.render_error(f"ASR 启动失败: {exc}")
            return

        listen_mode = mode or getattr(service, "config", {}).get("listen_mode", "wake_word")
        if service.start(listen_mode):
            self.hooks.set_input_mode("asr")
            if listen_mode == "push_to_talk":
                key_label = self.hooks.push_to_talk_label()
                self.hooks.render_success(f"ASR push-to-talk 已开启。按 {key_label} 录一句，再按 {key_label} 可停止。")
            else:
                self.hooks.render_success("ASR wake-word 已开启。说“小爪”或“老麦”后再说指令。")
            return

        self.hooks.render_error(f"ASR 启动失败: {getattr(service, 'last_error', '')}")

    def handle_keyboard_mode(self) -> None:
        self.hooks.stop_asr_service()
        self.hooks.set_input_mode("keyboard")
        self.hooks.set_asr_status_text("off")
        self.hooks.render_success("已切回 keyboard-mode。")

    def handle_asr_once(self) -> None:
        self.hooks.sync_asr_config({"listen_mode": "once", "require_wake_word": False})
        try:
            service = self.hooks.ensure_asr_service()
        except RuntimeError as exc:
            self.hooks.render_error(f"ASR once 启动失败: {exc}")
            return

        if service.start_once():
            self.hooks.set_input_mode("asr")
            self.hooks.render_success("ASR once 已开始录一句。")
            return

        self.hooks.render_error(f"ASR once 启动失败: {getattr(service, 'last_error', '')}")

    def handle_push_to_talk_key(self) -> None:
        try:
            service = self.hooks.ensure_asr_service()
        except RuntimeError as exc:
            self.hooks.stop_asr_service()
            self.hooks.set_asr_status_text("error")
            self.hooks.render_error(f"ASR push-to-talk 不可用: {exc}")
            return

        if getattr(service, "listen_mode", "") != "push_to_talk":
            self.hooks.sync_asr_config({
                "listen_mode": "push_to_talk",
                "require_wake_word": False,
                "push_to_talk_behavior": "tap_once",
            })
            try:
                service = self.hooks.ensure_asr_service()
            except RuntimeError as exc:
                self.hooks.stop_asr_service()
                self.hooks.set_asr_status_text("error")
                self.hooks.render_error(f"ASR push-to-talk 不可用: {exc}")
                return

        if service.toggle_push_to_talk():
            self.hooks.set_input_mode("asr")
            return

        self.hooks.stop_asr_service()
        self.hooks.set_asr_status_text("error")
        self.hooks.render_error(f"ASR push-to-talk 不可用: {getattr(service, 'last_error', '')}")

    def show_status(self) -> None:
        self.hooks.render_asr_status(
            self.hooks.resolve_status_config(),
            self.hooks.get_service_status(),
        )
