# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Search backend switching logic for the M-Claw CLI.

Handles /search-backend status display, backend selection, credential prompts,
and persistence of the auxiliary web-search backend setting.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from mclaw.cli.config import get_env_value, load_config, save_config, save_env_value
from mclaw.constants import display_mclaw_path

logger = logging.getLogger(__name__)


@dataclass
class BackendSwitchResult:
    success: bool
    backend: str = ""
    error_message: str = ""
    info_message: str = ""
    needs_api_key: bool = False
    key_env_var: str = ""


@dataclass
class BackendStatus:
    current: str = "auto"
    dashscope_available: bool = False
    tavily_available: bool = False


VALID_BACKENDS = ["dashscope", "tavily", "auto"]


def get_search_backend_status() -> BackendStatus:
    """Return current search backend status without writing to the terminal."""
    cfg = load_config().get("auxiliary", {}).get("web_search", {})
    return BackendStatus(
        current=cfg.get("backend", "auto"),
        dashscope_available=_dashscope_available(),
        tavily_available=_tavily_available(),
    )


def switch_search_backend(
    raw_input: str,
    print_fn: Callable = print,
    input_fn: Callable = input,
    prompt_for_missing_key: bool = True,
) -> BackendSwitchResult:
    """Switch search backend."""
    backend = raw_input.strip().lower()

    if not backend:
        # Show current configuration.
        status = get_search_backend_status()
        print_fn(f"  当前搜索后端: {status.current}")
        print_fn(f"  DashScope: {'✓ 可用' if status.dashscope_available else '✗ 未配置'}")
        print_fn(f"  Tavily: {'✓ 可用' if status.tavily_available else '✗ 未配置'}")
        print_fn(f"  用法: /search-backend dashscope|tavily|auto")
        return BackendSwitchResult(success=True, backend=status.current)

    if backend not in VALID_BACKENDS:
        return BackendSwitchResult(
            success=False,
            error_message=f"未知后端 '{backend}'。可用: {', '.join(VALID_BACKENDS)}",
        )

    # Check target backend credentials.
    if backend == "tavily":
        if not _tavily_available():
            if not prompt_for_missing_key:
                return BackendSwitchResult(
                    success=False,
                    needs_api_key=True,
                    key_env_var="TAVILY_API_KEY",
                    error_message="Tavily API 密钥尚未配置。",
                )
            print_fn("  ⚠ Tavily API 密钥尚未配置。")
            try:
                key = input_fn("  Tavily API Key: ").strip()
            except (EOFError, KeyboardInterrupt):
                return BackendSwitchResult(success=False, error_message="未提供密钥，切换已取消。")
            if not key:
                return BackendSwitchResult(success=False, error_message="未提供密钥，切换已取消。")
            save_env_value("TAVILY_API_KEY", key)
            try:
                from mclaw.runtime.secrets import authorize

                authorize("tool:web_search", ["TAVILY_API_KEY"])
            except Exception:
                logger.debug("Failed to authorize TAVILY_API_KEY for web_search", exc_info=True)
            print_fn(f"  ✓ 密钥已保存至 {display_mclaw_path('.env')} (TAVILY_API_KEY)")

    elif backend == "dashscope":
        if not _dashscope_available():
            return BackendSwitchResult(
                success=False,
                error_message=f"DashScope 未配置。请在 {display_mclaw_path('.env')} 设置 DASHSCOPE_API_KEY 或 QWEN_API_KEY。",
            )

    # Save selection.
    config = load_config()
    if "auxiliary" not in config:
        config["auxiliary"] = {}
    if "web_search" not in config["auxiliary"]:
        config["auxiliary"]["web_search"] = {}
    config["auxiliary"]["web_search"]["backend"] = backend
    save_config(config)

    return BackendSwitchResult(
        success=True,
        backend=backend,
        info_message=f"搜索后端已切换至 {backend}",
    )


def _dashscope_available() -> bool:
    return bool(get_env_value("DASHSCOPE_API_KEY") or get_env_value("QWEN_API_KEY"))


def _tavily_available() -> bool:
    return bool(get_env_value("TAVILY_API_KEY"))
