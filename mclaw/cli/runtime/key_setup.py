# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Pending API-key setup coordination for interactive runtimes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from mclaw.cli.config import mask_api_key


@dataclass(frozen=True)
class RuntimeKeySetupHooks:
    """Host operations used after a user enters a missing API key."""

    save_env_value: Callable[[str, str], None]
    render_missing_key: Callable[[], None]
    render_key_saved: Callable[[str, str], None]
    retry_search_backend: Callable[[], Any]
    render_search_error: Callable[[str], None]
    sync_search_backend: Callable[[str], None]
    render_search_success: Callable[[str], None]
    retry_model_switch: Callable[[dict[str, Any]], Any]
    render_model_error: Callable[[str], None]
    apply_model_switch: Callable[[Any, bool], None]


class RuntimeKeySetupCoordinator:
    """Completes a pending key setup and retries the original operation."""

    def __init__(self, hooks: RuntimeKeySetupHooks) -> None:
        self.hooks = hooks

    def complete(self, setup: dict[str, Any] | None, api_key: str) -> None:
        """Persist the entered key and retry the operation that requested it."""
        if not api_key:
            self.hooks.render_missing_key()
            return
        if not setup:
            self.hooks.render_model_error("缺少待处理的密钥配置请求。")
            return

        env_var = str(setup.get("env_var") or "").strip()
        if not env_var:
            message = "缺少待保存的环境变量名。"
            if setup.get("_search_backend"):
                self.hooks.render_search_error(message)
            else:
                self.hooks.render_model_error(message)
            return
        self.hooks.save_env_value(env_var, api_key)
        if setup.get("_search_backend") and env_var:
            # Web search keys must be authorized for the scoped tool after save.
            try:
                from mclaw.runtime.secrets import authorize

                authorize("tool:web_search", [env_var])
            except Exception:
                pass
        self.hooks.render_key_saved(env_var, mask_api_key(api_key))

        if setup.get("_search_backend"):
            result = self.hooks.retry_search_backend()
            if not getattr(result, "success", False):
                self.hooks.render_search_error(str(getattr(result, "error_message", "") or "搜索后端配置失败。"))
                return
            info_message = str(getattr(result, "info_message", "") or "")
            if info_message:
                self.hooks.sync_search_backend(str(getattr(result, "backend", "") or ""))
                self.hooks.render_search_success(info_message)
            return

        result = self.hooks.retry_model_switch(setup)
        if not getattr(result, "success", False):
            self.hooks.render_model_error(str(getattr(result, "error_message", "") or "模型切换失败。"))
            return
        self.hooks.apply_model_switch(result, bool(setup.get("is_global")))
