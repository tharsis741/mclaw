# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Runtime coordination for interactive desktop pet commands."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RuntimePetStartResult:
    ok: bool
    last_error: str = ""


@dataclass(frozen=True)
class RuntimePetStatus:
    pet_cfg: dict[str, Any]
    running: bool
    last_error: str | None = None


@dataclass(frozen=True)
class RuntimePetCommandHooks:
    """Host operations used by UI-neutral desktop pet commands."""

    get_pet_config: Callable[[], dict[str, Any]]
    start_pet_from_config: Callable[[], RuntimePetStartResult]
    stop_pet: Callable[[], None]
    sync_pet_config: Callable[[dict[str, Any]], None]
    save_pet_config: Callable[[], bool]
    restart_pet_if_enabled: Callable[[], RuntimePetStartResult | None]
    reset_pet_position: Callable[[], None]
    get_pet_status: Callable[[], RuntimePetStatus]
    emit_test_event: Callable[[str], bool]
    render_pet_notice: Callable[[str], None]
    render_pet_status: Callable[[dict[str, Any], bool, str | None], None]
    render_pet_usage: Callable[[], None]


class RuntimePetCommandCoordinator:
    """Handles `/pet` without depending on a concrete TUI."""

    _NOTIFY_KEY_MAP = {
        "turn": "turn_completed",
        "background": "background_completed",
        "delegation": "delegation_completed",
        "tool": "tool_finished",
    }

    def __init__(self, hooks: RuntimePetCommandHooks) -> None:
        self.hooks = hooks

    def handle_pet_command(self, raw_args: str = "") -> None:
        args = str(raw_args or "").strip().split()
        action = args[0].lower() if args else "status"

        if action == "on":
            self.hooks.get_pet_config()["enabled"] = True
            result = self.hooks.start_pet_from_config()
            self.hooks.render_pet_notice("桌面宠物已开启" if result.ok else f"桌面宠物启动失败: {result.last_error or '未知错误'}")
            return

        if action == "off":
            self.hooks.stop_pet()
            self.hooks.sync_pet_config({"enabled": False})
            self.hooks.render_pet_notice("桌面宠物已关闭")
            return

        if action == "status":
            status = self.hooks.get_pet_status()
            self.hooks.render_pet_status(status.pet_cfg, status.running, status.last_error)
            return

        if action == "scale":
            if len(args) < 2:
                self.hooks.render_pet_notice("用法: /pet scale <数字>")
                return
            try:
                scale = max(0.25, min(float(args[1]), 4.0))
            except ValueError:
                self.hooks.render_pet_notice("用法: /pet scale <数字>")
                return
            self.hooks.sync_pet_config({"scale": scale})
            self.hooks.render_pet_notice(f"桌面宠物缩放已设为 {scale}")
            return

        if action == "position":
            if len(args) < 2:
                self.hooks.render_pet_notice("用法: /pet position <bottom_right|bottom_left|top_right|top_left|reset>")
                return
            if args[1].lower() == "reset":
                try:
                    self.hooks.reset_pet_position()
                except Exception as exc:
                    self.hooks.render_pet_notice(f"桌面宠物位置重置失败: {exc}")
                    return
                self.hooks.render_pet_notice("桌面宠物位置记忆已清除，重启宠物后生效。")
                return
            self.hooks.sync_pet_config({"position": args[1]})
            self.hooks.render_pet_notice(f"桌面宠物位置已设为 {args[1]}")
            return

        if action == "bubble":
            if len(args) < 2 or args[1].lower() not in {"on", "off"}:
                self.hooks.render_pet_notice("用法: /pet bubble <on|off>")
                return
            value = args[1].lower()
            self.hooks.sync_pet_config({"show_bubble": value == "on"})
            self.hooks.render_pet_notice(f"桌面宠物气泡已设为 {value}（Pet bubble: {value}）")
            return

        if action == "notify":
            if len(args) < 3 or args[1].lower() not in self._NOTIFY_KEY_MAP or args[2].lower() not in {"on", "off"}:
                self.hooks.render_pet_notice("用法: /pet notify <turn|background|delegation|tool> <on|off>")
                return
            event_name = args[1].lower()
            value = args[2].lower()
            notify = self.hooks.get_pet_config().setdefault("notify", {})
            notify[self._NOTIFY_KEY_MAP[event_name]] = value == "on"
            self.hooks.sync_pet_config({"notify": notify})
            self.hooks.render_pet_notice(f"桌面宠物通知 {event_name} 已设为 {value}（Pet notify {event_name}: {value}）")
            return

        if action == "save":
            self.hooks.render_pet_notice("桌面宠物配置已保存" if self.hooks.save_pet_config() else "桌面宠物配置未保存")
            return

        if action == "restart":
            result = self.hooks.restart_pet_if_enabled()
            if result is None:
                self.hooks.render_pet_notice("桌面宠物当前未启用")
            else:
                self.hooks.render_pet_notice("桌面宠物已重启" if result.ok else f"桌面宠物重启失败: {result.last_error or '未知错误'}")
            return

        if action == "asset":
            if len(args) < 2:
                self.hooks.render_pet_notice("用法: /pet asset <名称或路径>")
                return
            self.hooks.sync_pet_config({"asset": args[1]})
            self.hooks.render_pet_notice(f"桌面宠物资源已设为 {args[1]}")
            return

        if action == "test":
            name = args[1].lower() if len(args) > 1 else "completed"
            sent = self.hooks.emit_test_event(name)
            self.hooks.render_pet_notice(f"桌面宠物测试事件已发送: {sent}")
            return

        self.hooks.render_pet_usage()
