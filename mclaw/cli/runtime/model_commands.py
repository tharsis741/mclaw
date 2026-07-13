# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Runtime coordination for interactive model switching commands."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mclaw.providers.runtime import ProviderRuntimeContext


@dataclass(frozen=True)
class RuntimeModelCommandHooks:
    """Host operations used by the UI-neutral model command flow."""

    current_runtime: Callable[[], ProviderRuntimeContext]
    config: Callable[[], dict[str, Any]]
    switch_model: Callable[..., Any]
    parse_model_flags: Callable[[str], tuple[str, str, str, bool]]
    render_model_status: Callable[[str, str], None]
    render_model_key_prompt: Callable[[str, str], None]
    render_model_error: Callable[[str], None]
    remember_pending_key_setup: Callable[[dict[str, Any]], None]
    apply_model_switch: Callable[[Any, bool], None]


class RuntimeModelCommandCoordinator:
    """Handles the `/model` command without depending on a concrete TUI."""

    def __init__(self, hooks: RuntimeModelCommandHooks) -> None:
        self.hooks = hooks

    def handle_model_switch(self, raw_args: str) -> None:
        """Resolve `/model` input and either apply it or request credentials."""
        raw_args = str(raw_args or "").strip()
        if not raw_args:
            runtime = self.hooks.current_runtime()
            self.hooks.render_model_status(
                runtime.model,
                runtime.provider,
            )
            return

        try:
            model_name, explicit_provider, explicit_profile, is_global = self.hooks.parse_model_flags(raw_args)
        except ValueError as exc:
            self.hooks.render_model_error(str(exc) or "模型命令参数无效。")
            return

        result = self.hooks.switch_model(
            model_input=model_name,
            current_runtime=self.hooks.current_runtime(),
            explicit_provider=explicit_provider,
            explicit_profile=explicit_profile,
            config=self.hooks.config(),
        )

        if not getattr(result, "success", False):
            if getattr(result, "needs_api_key", False):
                # Prompt ownership stays with the host UI; this coordinator only
                # records enough context to retry the same switch after key entry.
                self.hooks.remember_pending_key_setup({
                    "model": model_name,
                    "env_var": getattr(result, "key_env_var", ""),
                    "key_url": getattr(result, "key_url", ""),
                    "display_name": getattr(result, "provider_display_name", ""),
                    "explicit_provider": explicit_provider,
                    "explicit_profile": explicit_profile,
                    "is_global": is_global,
                })
                self.hooks.render_model_key_prompt(
                    str(getattr(result, "provider_display_name", "") or ""),
                    str(getattr(result, "key_url", "") or ""),
                )
                return

            self.hooks.render_model_error(str(getattr(result, "error_message", "") or "模型切换失败。"))
            return

        self.hooks.apply_model_switch(result, is_global)
