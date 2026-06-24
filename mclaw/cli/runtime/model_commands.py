"""Runtime coordination for interactive model switching commands."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RuntimeModelCommandHooks:
    """Host operations used by the UI-neutral model command flow."""

    current_provider: Callable[[], str]
    current_model: Callable[[], str]
    current_base_url: Callable[[], str]
    current_api_key: Callable[[], str]
    user_providers: Callable[[], dict[str, Any]]
    switch_model: Callable[..., Any]
    parse_model_flags: Callable[[str], tuple[str, str, str, bool]]
    render_model_status: Callable[[str, str], None]
    render_model_key_prompt: Callable[[str, str], None]
    render_model_error: Callable[[str], None]
    remember_pending_key_setup: Callable[[dict[str, Any]], None]
    apply_model_switch: Callable[[Any, bool], None]
    print_fn: Callable[..., Any] = print


class RuntimeModelCommandCoordinator:
    """Handles the `/model` command without depending on a concrete TUI."""

    def __init__(self, hooks: RuntimeModelCommandHooks) -> None:
        self.hooks = hooks

    def handle_model_switch(self, raw_args: str) -> None:
        raw_args = str(raw_args or "").strip()
        if not raw_args:
            self.hooks.render_model_status(
                self.hooks.current_model(),
                self.hooks.current_provider(),
            )
            return

        model_name, explicit_provider, explicit_profile, is_global = self.hooks.parse_model_flags(raw_args)
        result = self.hooks.switch_model(
            model_input=model_name,
            current_provider=self.hooks.current_provider(),
            current_model=self.hooks.current_model(),
            current_base_url=self.hooks.current_base_url(),
            current_api_key=self.hooks.current_api_key(),
            explicit_provider=explicit_provider,
            explicit_profile=explicit_profile,
            is_global=is_global,
            print_fn=self.hooks.print_fn,
            user_providers=self.hooks.user_providers(),
            prompt_for_missing_key=False,
        )

        if not getattr(result, "success", False):
            if getattr(result, "needs_api_key", False):
                self.hooks.remember_pending_key_setup({
                    "model": model_name,
                    "provider": getattr(result, "target_provider", ""),
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
