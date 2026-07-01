# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Runtime coordination for interactive search backend commands."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from mclaw.cli.config import ConfigError


@dataclass(frozen=True)
class RuntimeSearchCommandHooks:
    """Host operations used by the UI-neutral search command flow."""

    get_status: Callable[[], Any]
    switch_search_backend: Callable[..., Any]
    render_backend_status: Callable[[Any], None]
    render_key_prompt: Callable[[], None]
    render_success: Callable[[str], None]
    render_error: Callable[[str], None]
    remember_pending_key_setup: Callable[[dict[str, Any]], None]
    sync_search_backend: Callable[[str], None]
    print_fn: Callable[..., Any] = print


class RuntimeSearchCommandCoordinator:
    """Handles `/search-backend` without depending on a concrete TUI."""

    def __init__(self, hooks: RuntimeSearchCommandHooks) -> None:
        self.hooks = hooks

    def handle_search_backend(self, raw_args: str) -> None:
        """Show backend status or switch backend with deferred key setup."""
        raw_args = str(raw_args or "").strip()
        if not raw_args:
            try:
                status = self.hooks.get_status()
            except ConfigError as exc:
                self.hooks.render_error(f"配置错误: {exc}")
                return
            self.hooks.render_backend_status(status)
            return

        try:
            result = self.hooks.switch_search_backend(
                raw_args,
                print_fn=self.hooks.print_fn,
            )
        except ConfigError as exc:
            self.hooks.render_error(f"配置错误: {exc}")
            return
        if getattr(result, "needs_api_key", False):
            # Key entry is completed by the host prompt loop so the command can
            # run safely inside prompt_toolkit without blocking on stdin.
            self.hooks.remember_pending_key_setup({
                "_search_backend": True,
                "env_var": getattr(result, "key_env_var", ""),
            })
            self.hooks.render_key_prompt()
            return

        info_message = str(getattr(result, "info_message", "") or "")
        if info_message:
            self.hooks.sync_search_backend(str(getattr(result, "backend", "") or ""))
            self.hooks.render_success(info_message)

        error_message = str(getattr(result, "error_message", "") or "")
        if error_message:
            self.hooks.render_error(error_message)
