# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Model switching logic for M-Claw.

Handles the full flow:
  1. Parse model name and optional provider/profile/global flags
  2. Detect which provider the model belongs to
  3. Check if credentials exist
  4. Return a key-setup request when credentials are missing
  5. Resolve full credentials and return the switch result

Shared by the interactive /model command and key-setup retry flow.
"""

import shlex
from dataclasses import dataclass, replace
from typing import Any

from mclaw.cli.config import load_config, save_config
from mclaw.cli.model_resolver import resolve_model_input
from mclaw.providers.registry import PROVIDER_REGISTRY
from mclaw.providers.resolver import ProviderResolutionError, resolve_provider_runtime_context
from mclaw.providers.runtime import ProviderRuntimeContext

@dataclass
class ModelSwitchResult:
    """Transport-neutral result for applying a switch or requesting credentials."""

    success: bool
    runtime_context: ProviderRuntimeContext | None = None
    error_message: str = ""
    info_message: str = ""
    needs_api_key: bool = False
    key_env_var: str = ""
    key_url: str = ""
    provider_display_name: str = ""


class ModelFlagParseError(ValueError):
    """Raised when `/model` arguments cannot be parsed safely."""


def parse_model_flags(raw: str) -> tuple[str, str, str, bool]:
    """Parse model flags into model, provider, profile, and global scope."""
    try:
        parts = shlex.split(raw.strip())
    except ValueError as exc:
        raise ModelFlagParseError("模型命令参数包含未闭合的引号。") from exc
    model_name = ""
    explicit_provider = ""
    explicit_profile = ""
    is_global = False

    i = 0
    while i < len(parts):
        p = parts[i]
        if p in {"--provider", "--profile"}:
            if i + 1 >= len(parts) or parts[i + 1].startswith("--"):
                raise ModelFlagParseError(f"{p} 需要一个参数值。")
            if p == "--provider":
                explicit_provider = parts[i + 1]
            else:
                explicit_profile = parts[i + 1]
            i += 2
        elif p == "--global":
            is_global = True
            i += 1
        elif p.startswith("--"):
            raise ModelFlagParseError(f"未知模型命令参数: {p}")
        elif not model_name:
            model_name = p
            i += 1
        else:
            raise ModelFlagParseError(f"无法识别多余参数: {p}")

    return model_name, explicit_provider, explicit_profile, is_global


def switch_model(
    model_input: str,
    current_runtime: ProviderRuntimeContext,
    explicit_provider: str = "",
    explicit_profile: str = "",
    config: dict[str, Any] | None = None,
) -> ModelSwitchResult:
    """Resolve one model switch to a complete live provider context."""
    new_model = model_input.strip()
    if not new_model:
        return ModelSwitchResult(
            success=False,
            error_message="未指定模型。用法: /model <模型名> [--provider <供应商>]",
        )

    raw_user_providers = config.get("providers", {}) if isinstance(config, dict) else {}
    user_providers = raw_user_providers if isinstance(raw_user_providers, dict) else {}
    resolution = resolve_model_input(
        new_model,
        provider_input=explicit_provider,
        current_provider=current_runtime.provider,
        user_providers=user_providers,
    )
    if not resolution.ok:
        return ModelSwitchResult(
            success=False,
            error_message=resolution.message,
        )

    new_model = resolution.model
    target_provider = resolution.provider
    resolution_note = resolution.message if resolution.status == "unverified" else ""
    setup_entry = next(
        (
            entry
            for entry in getattr(PROVIDER_REGISTRY.get(target_provider), "setup_profiles", ())
            if entry.id.casefold() == explicit_profile.casefold()
        ),
        None,
    ) if explicit_profile else None
    same_runtime = (
        target_provider == current_runtime.provider and not explicit_profile
    ) or bool(
        setup_entry
        and setup_entry.callable
        and setup_entry.runtime_provider == current_runtime.provider
    )
    resolver_config = dict(config or {})
    if same_runtime and current_runtime.reasoning_config is None:
        resolver_config.pop("reasoning", None)
    try:
        context = resolve_provider_runtime_context(
            model=new_model,
            provider=target_provider,
            setup_profile_id=explicit_profile,
            base_url=current_runtime.base_url if same_runtime else "",
            api_key=current_runtime.api_key if same_runtime else "",
            reasoning_config=(
                dict(current_runtime.reasoning_config)
                if same_runtime and current_runtime.reasoning_config is not None
                else None
            ),
            config=resolver_config,
        )
    except ProviderResolutionError as exc:
        profile = PROVIDER_REGISTRY.get(exc.provider or target_provider)
        user_profile = user_providers.get(exc.provider or target_provider, {})
        display_name = (
            profile.display_name
            if profile
            else str(user_profile.get("display_name") or exc.provider or target_provider)
        )
        return ModelSwitchResult(
            success=False,
            error_message=str(exc),
            needs_api_key=exc.code == "missing_credential",
            key_env_var=exc.key_env_var,
            key_url=exc.key_url,
            provider_display_name=display_name,
        )

    if same_runtime:
        context = replace(
            context,
            base_url_source=current_runtime.base_url_source,
            auth_source=(
                current_runtime.auth_source
                if context.api_key == current_runtime.api_key
                else context.auth_source
            ),
        )
    info = f"已切换至 {context.model}（{context.profile.display_name}）"
    if resolution_note:
        info = f"{info}\n{resolution_note}"
    return ModelSwitchResult(success=True, runtime_context=context, info_message=info)


def persist_model_choice(model: str, provider: str = "") -> None:
    """Save model (and optionally provider) to config.yaml.

    Always writes active_provider. An empty provider leaves provider resolution
    to environment-configured custom endpoints.
    """
    from mclaw.cli.config import upsert_fallback_provider_model

    config = load_config(strict=True)
    config["model"] = model
    config["active_provider"] = provider
    config["active_provider_profile"] = ""
    upsert_fallback_provider_model(config, provider, model)
    save_config(config)
