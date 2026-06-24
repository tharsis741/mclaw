# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Model switching logic for M-Claw.

Handles the full flow:
  1. Parse model name + optional --provider flag
  2. Detect which provider the model belongs to
  3. Check if credentials exist
  4. If not → prompt user inline for API key (no need to edit .env)
  5. Save key, resolve full credentials, return result

Shared by TUI /model command and future CLI `mclaw model` subcommand.
"""

import getpass
import logging
import shlex
from dataclasses import dataclass
from typing import Optional, Callable

from mclaw.cli.auth import (
    PROVIDER_REGISTRY,
    resolve_api_key,
    resolve_base_url,
)
from mclaw.cli.config import save_env_value, load_config, save_config
from mclaw.cli.model_resolver import resolve_model_input, resolve_provider_key
from mclaw.constants import display_mclaw_path

logger = logging.getLogger(__name__)


@dataclass
class ModelSwitchResult:
    success: bool
    new_model: str = ""
    target_provider: str = ""
    provider_changed: bool = False
    api_key: str = ""
    base_url: str = ""
    api_mode: str = ""
    error_message: str = ""
    info_message: str = ""
    needs_api_key: bool = False
    key_env_var: str = ""
    key_url: str = ""
    provider_display_name: str = ""
    provider_profile: str = ""


def parse_model_flags(raw: str):
    """Parse model flags into model, provider, profile, and global scope."""
    try:
        parts = shlex.split(raw.strip())
    except ValueError:
        parts = raw.strip().split()
    model_name = ""
    explicit_provider = ""
    explicit_profile = ""
    is_global = False

    i = 0
    while i < len(parts):
        p = parts[i]
        if p == "--provider" and i + 1 < len(parts):
            explicit_provider = parts[i + 1]
            i += 2
        elif p == "--profile" and i + 1 < len(parts):
            explicit_profile = parts[i + 1]
            i += 2
        elif p == "--global":
            is_global = True
            i += 1
        elif not model_name:
            model_name = p
            i += 1
        else:
            i += 1

    return model_name, explicit_provider, explicit_profile, is_global


def switch_model(
    model_input: str,
    current_provider: str,
    current_model: str,
    current_base_url: str = "",
    current_api_key: str = "",
    explicit_provider: str = "",
    explicit_profile: str = "",
    is_global: bool = False,
    print_fn: Callable = print,
    input_fn: Callable = input,
    password_fn: Callable = getpass.getpass,
    user_providers: Optional[dict] = None,
    prompt_for_missing_key: bool = True,
) -> ModelSwitchResult:
    """Core model-switching pipeline.

    Resolution:
      1. If --provider given → use that provider directly
      2. Otherwise → detect provider from model name
      3. Check credentials → prompt inline if missing
      4. Build result with all info needed to rebuild API client

    When *prompt_for_missing_key* is False (e.g. TUI context where stdin is
    owned by prompt_toolkit), return a result with needs_api_key=True instead
    of trying to read input from a background thread.
    """
    new_model = model_input.strip()
    if not new_model:
        return ModelSwitchResult(
            success=False,
            error_message="未指定模型。用法: /model <模型名> [--provider <供应商>]",
        )

    user_providers = user_providers or {}

    # Step 1: resolve the model name and target provider.

    resolution = resolve_model_input(
        new_model,
        provider_input=explicit_provider,
        current_provider=current_provider,
        user_providers=user_providers,
    )
    if not resolution.ok:
        return ModelSwitchResult(
            success=False,
            new_model=resolution.model,
            target_provider=resolution.provider,
            error_message=resolution.message,
        )

    new_model = resolution.model
    target_provider = resolution.provider
    resolution_note = resolution.message if resolution.status == "unverified" else ""

    # ── Step 2: Handle user-defined providers from config.yaml ──

    if target_provider in user_providers:
        up = user_providers[target_provider]
        info = f"已切换至 {new_model}（{target_provider}）"
        if resolution_note:
            info = f"{info}\n{resolution_note}"
        return ModelSwitchResult(
            success=True,
            new_model=new_model,
            target_provider=target_provider,
            provider_changed=(target_provider != current_provider),
            api_key=up.get("api_key", ""),
            base_url=up.get("base_url", ""),
            api_mode=up.get("api_mode", "chat_completions"),
            info_message=info,
        )

    # Step 3: check whether the current provider can be kept.

    if not target_provider:
        return ModelSwitchResult(
            success=False,
            new_model=new_model,
            error_message=(
                "M-Claw 无法判断这个模型应该通过哪个接入方调用。\n"
                "请指定接入方，例如：/model <模型名> --provider openai\n"
                "如果不确定模型 ID，请到对应大模型平台官网查看官方模型 ID。"
            ),
        )

    # Step 4: resolve credentials and prompt when missing.

    pcfg = PROVIDER_REGISTRY.get(target_provider)
    if not pcfg:
        return ModelSwitchResult(
            success=True,
            new_model=new_model,
            target_provider=current_provider,
            provider_changed=False,
            api_key=current_api_key,
            base_url=current_base_url,
            api_mode="",
            info_message=f"模型已切换至 {new_model}",
        )

    profile_id = ""
    if pcfg:
        try:
            from mclaw.cli.provider_profiles import find_provider_profile, profile_help_lines

            profile = find_provider_profile(target_provider, explicit_profile)
            if profile is None:
                return ModelSwitchResult(
                    success=False,
                    new_model=new_model,
                    target_provider=target_provider,
                    error_message=(
                        f"未知接口类型: {explicit_profile}\n"
                        f"{pcfg.display_name} 可用接口:\n"
                        + "\n".join(f"- {line}" for line in profile_help_lines(target_provider))
                    ),
                )
            profile_id = profile.id
            if explicit_profile and not profile.callable:
                return ModelSwitchResult(
                    success=False,
                    new_model=new_model,
                    target_provider=target_provider,
                    provider_profile=profile_id,
                    error_message=(
                        f"{pcfg.display_name} 的 {profile.label} 不能直接作为 M-Claw 调用接口。\n"
                        f"{profile.note}\n"
                        "请改用官方 API profile，或配置自定义兼容接口。"
                    ),
                )
        except Exception:
            profile_id = explicit_profile or ""

    same_provider = target_provider == current_provider
    api_key = current_api_key if same_provider and current_api_key else resolve_api_key(target_provider)
    base_url = current_base_url if same_provider and current_base_url else resolve_base_url(target_provider)

    if not api_key:
        if current_provider in ("custom", "custom_anthropic") and not explicit_provider:
            # The detected provider differs from the current custom endpoint.
            # Warn the user clearly, but still allow it (the custom endpoint
            # may proxy multiple providers).
            print_fn("")
            print_fn(f"  ⚠ '{new_model}' 看起来属于 {pcfg.display_name}，")
            print_fn(f"    但尚未配置 {pcfg.display_name} 的 API 密钥。")
            print_fn(f"    请求将发送至当前接口: {current_base_url}")
            print_fn(f"    如需使用 {pcfg.display_name} 官方 API:")
            print_fn(f"      /model {new_model} --provider {target_provider}")
            print_fn("")
            return ModelSwitchResult(
                success=True,
                new_model=new_model,
                target_provider=current_provider,
                provider_changed=False,
                api_key=current_api_key,
                base_url=current_base_url,
                api_mode="",
                info_message=f"模型名 → {new_model}（仍使用 {current_base_url}）",
            )

        if not prompt_for_missing_key:
            return ModelSwitchResult(
                success=False,
                new_model=new_model,
                target_provider=target_provider,
                error_message=f"缺少 {pcfg.display_name} 的 API 密钥。",
                needs_api_key=True,
                key_env_var=pcfg.api_key_env_vars[0] if pcfg.api_key_env_vars else "",
            key_url=pcfg.key_url,
            provider_display_name=pcfg.display_name,
            provider_profile=profile_id,
        )

        api_key = _prompt_for_key(pcfg, print_fn, input_fn, password_fn)
        if not api_key:
            return ModelSwitchResult(
                success=False,
                error_message=f"未提供 {pcfg.display_name} 的密钥，切换已取消。",
            )

    provider_changed = (target_provider != current_provider)
    info = f"已切换至 {new_model}（{pcfg.display_name}）"
    if profile_id and profile_id != "api":
        info = f"{info} / {profile_id}"
    if resolution_note:
        info = f"{info}\n{resolution_note}"

    return ModelSwitchResult(
        success=True,
        new_model=new_model,
        target_provider=target_provider,
        provider_changed=provider_changed,
        api_key=api_key,
        base_url=base_url,
        api_mode=pcfg.api_mode,
        info_message=info,
        provider_profile=profile_id,
    )


def _prompt_for_key(
    pcfg,
    print_fn: Callable,
    input_fn: Callable,
    password_fn: Callable,
) -> str:
    """Interactively prompt the user for an API key, save it, and return it."""
    print_fn("")
    print_fn(f"  ⚠ {pcfg.display_name} 的 API 密钥尚未配置。")
    if pcfg.key_url:
        print_fn(f"  获取密钥: {pcfg.key_url}")
    print_fn("")

    try:
        api_key = password_fn(f"  API 密钥: ").strip()
    except (EOFError, KeyboardInterrupt):
        print_fn("")
        return ""

    if not api_key:
        return ""

    env_var = pcfg.api_key_env_vars[0]
    save_env_value(env_var, api_key)
    print_fn(f"  ✓ 密钥已保存至 {display_mclaw_path('.env')} ({env_var})")

    return api_key


def persist_model_choice(model: str, provider: str = "", profile: str = ""):
    """Save model (and optionally provider) to config.yaml.

    Always writes active_provider — empty string clears a previous override,
    letting resolve_provider fall back to env-var-based custom endpoints.
    """
    from mclaw.cli.config import upsert_fallback_provider_model

    config = load_config()
    config["model"] = model
    config["active_provider"] = provider
    config["active_provider_profile"] = profile
    upsert_fallback_provider_model(config, provider, model)
    save_config(config)
