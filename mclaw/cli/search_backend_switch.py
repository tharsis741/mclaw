# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Web search and extraction backend switching for the M-Claw CLI.

Handles slash-command status, credential checks, scoped authorization, and
persistence for the two web backend settings.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from mclaw.cli.config import ConfigError, get_env_value, load_config, save_config
from mclaw.providers.registry import get_runtime_profile
from mclaw.tools.extract.profiles import (
    EXTRACT_BACKEND_PROFILES,
    VALID_EXTRACT_BACKENDS,
)
from mclaw.tools.search.config import parse_bool_config
from mclaw.tools.search.profiles import SEARCH_BACKEND_PROFILES


_QWEN_PROFILE = get_runtime_profile("qwen")
_QWEN_INTL_PROFILE = get_runtime_profile("qwen-intl")
_QWEN_ENV_VARS = tuple(dict.fromkeys((*_QWEN_PROFILE.env_vars, *_QWEN_INTL_PROFILE.env_vars)))
_QWEN_CREDENTIAL_HINT = " or ".join(_QWEN_ENV_VARS)


@dataclass
class BackendSwitchResult:
    """Outcome returned to CLI coordinators after a backend switch attempt."""

    success: bool
    backend: str = ""
    error_message: str = ""
    info_message: str = ""
    needs_api_key: bool = False
    key_env_var: str = ""


@dataclass
class BackendStatus:
    """Current backend selection plus credential availability flags."""

    current: str = "auto"
    dashscope_available: bool = False
    tavily_available: bool = False


@dataclass
class ExtractBackendStatus:
    """Current extraction backend plus local/cloud availability flags."""

    current: str = "trafilatura"
    trafilatura_available: bool = False
    firecrawl_available: bool = False
    tavily_available: bool = False


VALID_BACKENDS = ("dashscope", "tavily", "auto")


def _section(config: dict, name: str) -> dict:
    auxiliary = config.get("auxiliary", {}) if isinstance(config, dict) else {}
    if not isinstance(auxiliary, dict):
        return {}
    section = auxiliary.get(name, {})
    return section if isinstance(section, dict) else {}


def _save_backend(config: dict, section_name: str, backend: str) -> None:
    auxiliary = config.setdefault("auxiliary", {})
    if not isinstance(auxiliary, dict):
        auxiliary = {}
        config["auxiliary"] = auxiliary
    section = auxiliary.setdefault(section_name, {})
    if not isinstance(section, dict):
        section = {}
        auxiliary[section_name] = section
    section["backend"] = backend
    save_config(config)


def _authorize(required_for: str, env_vars: list[str]) -> str:
    if not env_vars:
        return ""
    try:
        from mclaw.runtime.secrets import authorize

        authorize(required_for, env_vars)
        return ""
    except Exception as exc:
        return f"密钥作用域授权失败: {type(exc).__name__}: {exc}"


def get_search_backend_status() -> BackendStatus:
    """Return current search backend status without writing to the terminal."""
    cfg = _section(load_config(strict=True), "web_search")
    return BackendStatus(
        current=cfg.get("backend", "auto"),
        dashscope_available=_dashscope_available(),
        tavily_available=_tavily_available(),
    )


def format_search_backend_status(status: BackendStatus) -> list[str]:
    """Format search availability together with maintained backend behavior."""
    availability = {
        "dashscope": "✓ 可用" if status.dashscope_available else "✗ 未配置",
        "tavily": "✓ 可用" if status.tavily_available else "✗ 未配置",
    }
    lines = [f"当前后端: {status.current}"]
    for name in ("dashscope", "tavily"):
        profile = SEARCH_BACKEND_PROFILES[name]
        current = "（当前）" if name == status.current else ""
        lines.append(
            f"{profile.display_name}{current}: {availability[name]}｜{profile.status_description_zh}"
        )
    lines.append("用法: /search-backend dashscope|tavily|auto")
    return lines


def switch_search_backend(
    raw_input: str,
    print_fn: Callable = print,
) -> BackendSwitchResult:
    """Switch or display the auxiliary web-search backend configuration."""
    backend = raw_input.strip().lower()

    if not backend:
        # Show current configuration.
        status = get_search_backend_status()
        for line in format_search_backend_status(status):
            print_fn(f"  {line}")
        return BackendSwitchResult(success=True, backend=status.current)

    if backend not in VALID_BACKENDS:
        return BackendSwitchResult(
            success=False,
            error_message=f"未知后端 '{backend}'。可用: {', '.join(VALID_BACKENDS)}",
        )

    try:
        config = load_config(strict=True)
    except ConfigError as exc:
        return BackendSwitchResult(success=False, error_message=f"配置错误: {exc}")

    # Check target backend credentials.
    if backend == "tavily":
        if not _tavily_available():
            return BackendSwitchResult(
                success=False,
                backend=backend,
                needs_api_key=True,
                key_env_var="TAVILY_API_KEY",
                error_message="Tavily API 密钥尚未配置。",
            )

    elif backend == "dashscope":
        if not _dashscope_available():
            return BackendSwitchResult(
                success=False,
                backend=backend,
                needs_api_key=True,
                key_env_var=_QWEN_PROFILE.env_vars[0],
                error_message=f"DashScope 未配置（{_QWEN_CREDENTIAL_HINT}）。",
            )

    env_vars: list[str] = []
    if backend in {"tavily", "auto"} and _tavily_available():
        env_vars.append("TAVILY_API_KEY")
    try:
        fallback_enabled = parse_bool_config(_section(config, "web_search").get("fallback"), default=True)
    except ConfigError as exc:
        return BackendSwitchResult(success=False, backend=backend, error_message=f"配置错误: {exc}")
    if backend in {"dashscope", "auto"} or (backend == "tavily" and fallback_enabled):
        env_vars.extend(name for name in _QWEN_ENV_VARS if get_env_value(name))
    authorization_error = _authorize("tool:web_search", list(dict.fromkeys(env_vars)))
    if authorization_error:
        return BackendSwitchResult(success=False, backend=backend, error_message=authorization_error)

    try:
        _save_backend(config, "web_search", backend)
    except OSError as exc:
        return BackendSwitchResult(success=False, backend=backend, error_message=f"保存配置失败: {exc}")

    return BackendSwitchResult(
        success=True,
        backend=backend,
        info_message=f"搜索后端已切换至 {backend}",
    )


def get_extract_backend_status() -> ExtractBackendStatus:
    """Return current page-extraction backend status."""
    config = load_config(strict=True)
    cfg = _section(config, "web_extract")
    return ExtractBackendStatus(
        current=str(cfg.get("backend") or "trafilatura"),
        trafilatura_available=_trafilatura_available(),
        firecrawl_available=_firecrawl_available(cfg),
        tavily_available=_tavily_available(),
    )


def format_extract_backend_status(status: ExtractBackendStatus) -> list[str]:
    """Format one shared status view for plain CLI and the interactive TUI."""
    availability = {
        "trafilatura": "✓ 可用" if status.trafilatura_available else "✗ 未安装",
        "firecrawl": "✓ 可用" if status.firecrawl_available else "✗ 未配置",
        "tavily": "✓ 可用" if status.tavily_available else "✗ 未配置",
    }
    lines = [f"当前后端: {status.current}"]
    for name in VALID_EXTRACT_BACKENDS:
        profile = EXTRACT_BACKEND_PROFILES[name]
        state = availability[name]
        current = "（当前）" if name == status.current else ""
        lines.append(
            f"{profile.display_name}{current}: {state}｜{profile.status_description_zh}"
        )
    lines.append(f"用法: /extract-backend {'|'.join(VALID_EXTRACT_BACKENDS)}")
    return lines


def switch_extract_backend(
    raw_input: str,
    print_fn: Callable = print,
) -> BackendSwitchResult:
    """Switch or display the auxiliary web-extraction backend."""
    backend = raw_input.strip().lower()

    if not backend:
        status = get_extract_backend_status()
        for line in format_extract_backend_status(status):
            print_fn(f"  {line}")
        return BackendSwitchResult(success=True, backend=status.current)

    if backend not in VALID_EXTRACT_BACKENDS:
        return BackendSwitchResult(
            success=False,
            error_message=f"未知后端 '{backend}'。可用: {', '.join(VALID_EXTRACT_BACKENDS)}",
        )

    try:
        config = load_config(strict=True)
    except ConfigError as exc:
        return BackendSwitchResult(success=False, error_message=f"配置错误: {exc}")
    extract_cfg = _section(config, "web_extract")

    if backend == "trafilatura" and not _trafilatura_available():
        return BackendSwitchResult(
            success=False,
            backend=backend,
            error_message="Trafilatura 未安装，请重新安装项目依赖。",
        )

    key_env_var = ""
    if backend == "tavily" and not _tavily_available():
        key_env_var = "TAVILY_API_KEY"
    elif backend == "firecrawl" and not _firecrawl_available(extract_cfg):
        key_env_var = "FIRECRAWL_API_KEY"
    if key_env_var:
        return BackendSwitchResult(
            success=False,
            backend=backend,
            needs_api_key=True,
            key_env_var=key_env_var,
            error_message=f"{backend} API 密钥尚未配置。",
        )

    env_vars = []
    if backend == "tavily":
        env_vars = ["TAVILY_API_KEY"]
    elif backend == "firecrawl" and get_env_value("FIRECRAWL_API_KEY"):
        env_vars = ["FIRECRAWL_API_KEY"]
    authorization_error = _authorize("tool:web_extract", env_vars)
    if authorization_error:
        return BackendSwitchResult(success=False, backend=backend, error_message=authorization_error)

    try:
        _save_backend(config, "web_extract", backend)
    except OSError as exc:
        return BackendSwitchResult(success=False, backend=backend, error_message=f"保存配置失败: {exc}")
    return BackendSwitchResult(
        success=True,
        backend=backend,
        info_message=f"网页提取后端已切换至 {backend}",
    )


def _dashscope_available() -> bool:
    """Return whether any supported DashScope-compatible key is configured."""
    return any(bool(get_env_value(name)) for name in _QWEN_ENV_VARS)


def _tavily_available() -> bool:
    """Return whether Tavily search credentials are configured."""
    return bool(get_env_value("TAVILY_API_KEY"))


def _trafilatura_available() -> bool:
    try:
        import trafilatura  # noqa: F401

        return True
    except ImportError:
        return False


def _firecrawl_available(config: dict) -> bool:
    return bool(get_env_value("FIRECRAWL_API_KEY"))
