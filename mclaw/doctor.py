# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Runtime diagnostics for M-Claw installations.

Doctor checks local environment, dependency, browser, runtime, and integration
state so setup issues can be reported before an agent turn fails.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from mclaw.constants import display_mclaw_home, get_mclaw_home
from mclaw.tools.browser_requirements import find_playwright_browsers_root

logger = logging.getLogger(__name__)


@dataclass
class CheckResult:
    """Raw diagnostic result before product-level grouping."""
    name: str
    ok: bool
    detail: str
    fix: str = ""
    severity: str = "error"


@dataclass
class DoctorLine:
    """Rendered product-facing diagnostic row."""
    section: str
    name: str
    status: str
    detail: str
    fix: str = ""


def _load_config() -> dict:
    from mclaw.cli.config import load_config

    return load_config(strict=True)


def _append_secret_allowlist_check(results: list[CheckResult]) -> None:
    """Validate the scoped secret allowlist shape without exposing values."""
    allowlist_path = get_mclaw_home() / "secret_allowlist.json"
    if not allowlist_path.exists():
        results.append(CheckResult("secret allowlist", True, "not created yet"))
        return
    try:
        data = json.loads(allowlist_path.read_text(encoding="utf-8"))
    except Exception as exc:
        results.append(CheckResult("secret allowlist", False, f"{allowlist_path}; invalid JSON: {exc}", "Delete or fix secret_allowlist.json."))
        return
    valid = isinstance(data, dict) and data.get("version") == 1
    for bucket in ("skills", "tools", "runtime", "channels"):
        scopes = data.get(bucket, {}) if isinstance(data, dict) else {}
        if not isinstance(scopes, dict):
            valid = False
            break
        if any(not isinstance(values, list) for values in scopes.values()):
            valid = False
            break
    results.append(
        CheckResult(
            "secret allowlist",
            valid,
            str(allowlist_path)
            if valid
            else f"{allowlist_path}; expected version=1 and scopes skills/tools/runtime/channels with list values",
            "Delete or fix secret_allowlist.json manually.",
        )
    )


def _config_path_value(config: dict | None, path: str) -> object:
    current: object = config or {}
    for part in str(path or "").split("."):
        if not part:
            continue
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _truthy_enabled(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}
    return bool(value)


def _feature_enabled(config: dict | None, spec: object) -> bool:
    cfg = config if isinstance(config, dict) else {}
    config_path = str(getattr(spec, "config_path", "") or "")
    if config_path:
        return _truthy_enabled(_config_path_value(cfg, config_path))

    toolset = str(getattr(spec, "toolset", "") or "")
    if toolset:
        raw_toolsets = cfg.get("toolsets", []) if isinstance(cfg, dict) else []
        if isinstance(raw_toolsets, list) and toolset in {str(item) for item in raw_toolsets}:
            return True

    kind = str(getattr(spec, "kind", "") or "")
    name = str(getattr(spec, "name", "") or "")
    if kind == "channel" and name:
        channels = cfg.get("channels", {}) if isinstance(cfg, dict) else {}
        section = channels.get(name, {}) if isinstance(channels, dict) else {}
        if isinstance(section, dict):
            return _truthy_enabled(section.get("enabled"))
    return False


def _format_required_envs(spec: object) -> str:
    required = list(getattr(spec, "requires", ()) or ())
    groups = [list(group) for group in (getattr(spec, "requires_any", ()) or ())]
    parts: list[str] = []
    if required:
        parts.extend(str(item) for item in required)
    for group in groups:
        if group:
            parts.append("/".join(str(item) for item in group))
    return ", ".join(parts) if parts else "none"


def _append_feature_checks(results: list[CheckResult], cfg: dict | None) -> None:
    """Check optional feature credentials against config and secret scopes."""
    try:
        from mclaw.cli.config import get_env_value
        from mclaw.runtime.features import configured_env_vars, list_feature_specs
        from mclaw.runtime.secrets import is_authorized
    except Exception as exc:
        results.append(
            CheckResult(
                "feature registry",
                False,
                f"failed to import feature registry: {type(exc).__name__}: {exc}",
                "Check mclaw.runtime.features and mclaw.runtime.secrets imports.",
            )
        )
        return

    for spec in list_feature_specs():
        display = getattr(spec, "display_name", "") or getattr(spec, "name", "")
        required_for = getattr(spec, "required_for", "")
        enabled = _feature_enabled(cfg, spec)
        configured = configured_env_vars(spec, get_env_value)
        authorized = [env_var for env_var in configured if is_authorized(required_for, env_var)]
        name = f"feature {display}"
        if enabled and not configured:
            results.append(
                CheckResult(
                    name,
                    False,
                    f"enabled; missing credentials: {_format_required_envs(spec)}",
                    f"Run setup or secret_request_many(required_for='{required_for}', ...) to configure scoped credentials.",
                )
            )
            continue
        if enabled and configured and not authorized:
            results.append(
                CheckResult(
                    name,
                    False,
                    f"enabled; configured={','.join(configured)}; allowlist=missing",
                    f"Run setup or secret_request_many(required_for='{required_for}', ...) to authorize these credentials.",
                )
            )
            continue
        if enabled:
            results.append(CheckResult(name, True, f"enabled; configured={','.join(configured)}; allowlist=ok"))
            continue
        if configured:
            auth_detail = "authorized" if authorized else "not authorized"
            results.append(CheckResult(name, True, f"not enabled; credentials present ({auth_detail}); optional"))
        else:
            results.append(CheckResult(name, True, "not enabled; optional"))


def _append_provider_checks(results: list[CheckResult], cfg: dict | None) -> None:
    """Report registry catalog metadata and the typed active provider result."""
    try:
        from mclaw.providers.registry import iter_runtime_profiles
        from mclaw.providers.resolver import ProviderResolutionError, resolve_provider_runtime_context
    except Exception as exc:
        results.append(
            CheckResult(
                "provider registry",
                False,
                f"failed to import provider metadata: {type(exc).__name__}: {exc}",
                "Check mclaw.providers imports.",
            )
        )
        return

    profiles = tuple(iter_runtime_profiles())
    callable_setup = sum(entry.callable for profile in profiles for entry in profile.setup_profiles)
    results.append(
        CheckResult(
            "provider registry",
            True,
            f"canonical={len(profiles)}; callable_setup={callable_setup}",
        )
    )

    config = cfg if isinstance(cfg, dict) else {}
    try:
        context = resolve_provider_runtime_context(
            model=str(config.get("model") or ""),
            provider=str(config.get("active_provider") or ""),
            setup_profile_id=str(config.get("active_provider_profile") or ""),
            config=config,
        )
    except ProviderResolutionError as exc:
        profile = next((item for item in profiles if item.name == exc.provider), None)
        display_name = profile.display_name if profile else (exc.provider or "unresolved")
        credential_names = "/".join(profile.env_vars) if profile else exc.key_env_var
        detail = f"provider={display_name}; code={exc.code}; {exc.message}"
        if credential_names:
            detail += f"; credential_candidates={credential_names}"
        fix = f"Configure {credential_names or 'the active provider'} and rerun mclaw doctor."
        if exc.key_url:
            fix += f" Key setup: {exc.key_url}"
        results.append(CheckResult("agent provider", False, detail, fix))
        return
    except Exception as exc:
        results.append(
            CheckResult(
                "agent provider",
                False,
                f"provider resolution failed: {type(exc).__name__}",
                "Check active provider configuration and mclaw.providers.resolver.",
            )
        )
        return

    profile = context.profile
    results.append(
        CheckResult(
            "agent provider",
            True,
            (
                f"provider={profile.name}; display={profile.display_name}; model={context.model}; "
                f"api_mode={context.api_mode}; base_url={context.safe_base_url}; "
                f"credential_source={context.auth_source or 'none'}; "
                f"credential_candidates={'/'.join(profile.env_vars) or 'optional'}"
            ),
        )
    )


def _exists_env_path(name: str) -> tuple[bool, str]:
    value = os.environ.get(name, "").strip().strip('"')
    if not value:
        return False, "not set"
    path = Path(value)
    return path.exists(), value


def _module_available(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def _check_sqlite_fts5() -> CheckResult:
    try:
        import sqlite3

        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("CREATE VIRTUAL TABLE mclaw_fts_check USING fts5(content)")
        finally:
            conn.close()
        return CheckResult("local storage", True, "ready")
    except Exception as exc:
        return CheckResult(
            "local storage",
            False,
            f"unavailable: {type(exc).__name__}: {exc}",
            "Install a Python build with SQLite FTS5 support.",
        )


def _append_module_group_check(
    results: list[CheckResult],
    name: str,
    modules: tuple[str, ...],
    *,
    detail: str = "ready",
    missing_detail: str = "not ready",
    fix: str = "Run pip install -e . in the M-Claw source directory.",
    severity: str = "error",
) -> None:
    missing = [module for module in modules if not _module_available(module)]
    if missing:
        results.append(CheckResult(name, False, missing_detail, fix, severity=severity))
        return
    results.append(CheckResult(name, True, detail, severity=severity))


def _append_runtime_module_checks(results: list[CheckResult]) -> None:
    _append_module_group_check(
        results,
        "model client",
        ("openai", "httpx", "pydantic"),
        detail="ready",
        missing_detail="not ready; install project dependencies",
    )
    _append_module_group_check(
        results,
        "command interface",
        ("yaml",),
        detail="ready",
        missing_detail="not ready; install project dependencies",
    )
    _append_module_group_check(
        results,
        "tui",
        ("rich", "prompt_toolkit"),
        detail="ready",
        missing_detail="not ready; install project dependencies",
    )
    results.append(_check_sqlite_fts5())
    _append_module_group_check(
        results,
        "desktop companion",
        ("PySide6", "shiboken6"),
        detail="ready",
        missing_detail="not installed; desktop companion optional",
        severity="warn",
    )
    _append_module_group_check(
        results,
        "multimodal services",
        ("dashscope",),
        detail="ready",
        missing_detail="not installed; vision and speech services optional",
        severity="warn",
    )
    _append_module_group_check(
        results,
        "voice device",
        ("sounddevice",),
        detail="ready",
        missing_detail="not installed; voice input optional",
        severity="warn",
    )


def _append_tool_diagnostics(results: list[CheckResult], diagnostics: list[dict]) -> None:
    """Condense registry diagnostics into user-actionable doctor checks."""
    unavailable = [item for item in diagnostics if not item.get("available")]
    if not unavailable:
        results.append(CheckResult("tool diagnostics", True, f"all registered tools available ({len(diagnostics)})"))
        return

    grouped: dict[tuple[str, str, str], list[str]] = {}
    for item in unavailable:
        tool_name = str(item.get("tool") or "")
        reason = str(item.get("reason") or "unavailable")
        fix_text = str(item.get("fix") or "Check tool configuration and dependencies.")
        severity = "warn" if tool_name.startswith("browser_") else "error"
        grouped.setdefault((reason, fix_text, severity), []).append(tool_name)

    for (reason, fix_text, severity), tool_names in grouped.items():
        tools = sorted(name for name in tool_names if name)
        if len(tools) == 1:
            results.append(CheckResult(f"tool {tools[0]}", False, reason, fix_text, severity=severity))
            continue
        label = "toolset browser" if all(name.startswith("browser_") for name in tools) else f"tools ({len(tools)})"
        shown = ", ".join(tools[:8])
        if len(tools) > 8:
            shown += f", +{len(tools) - 8} more"
        results.append(CheckResult(label, False, f"{reason}; affected={shown}", fix_text, severity=severity))


def _append_weixin_checks(results: list[CheckResult], cfg: dict | None) -> None:
    try:
        from mclaw.channels.weixin.config import WeixinConfig
    except Exception as exc:
        results.append(
            CheckResult(
                "weixin channel",
                False,
                f"failed to import diagnostics: {type(exc).__name__}: {exc}",
                "Check mclaw.channels.weixin imports.",
            )
        )
        return

    config = WeixinConfig.from_config(cfg or {})
    configured = bool(config.enabled or config.account_id or config.token)
    if not configured:
        results.append(CheckResult("weixin channel", True, "not configured; optional"))
        return

    errors = config.validate()
    missing = []
    for module in ("httpx", "qrcode"):
        if not _module_available(module):
            missing.append(module)

    detail_parts = [
        f"account_id={'yes' if config.account_id else 'no'}",
        f"token={'yes' if config.token else 'no'}",
        f"dm_policy={config.dm_policy}",
        f"session_scope={config.session_scope}",
        f"base_url={config.base_url or 'unset'}",
    ]
    if missing:
        detail_parts.append("missing=" + ",".join(missing))
    if errors:
        detail_parts.append("config_errors=" + "; ".join(errors))

    ok = not missing and not errors
    results.append(
        CheckResult(
            "weixin channel",
            ok,
            "; ".join(detail_parts),
            "Run mclaw weixin login, then mclaw weixin. Install M-Claw dependencies with: pip install -e .",
        )
    )


def _append_dingtalk_checks(results: list[CheckResult], cfg: dict | None) -> None:
    try:
        from mclaw.channels.dingtalk.config import DingTalkConfig
        from mclaw.channels.dingtalk.stream_client import check_dingtalk_requirements
        from mclaw.cli.auth import resolve_provider
    except Exception as exc:
        results.append(
            CheckResult(
                "dingtalk channel",
                False,
                f"failed to import diagnostics: {type(exc).__name__}: {exc}",
                "Install M-Claw dependencies from the source directory: pip install -e .",
            )
        )
        return

    config = DingTalkConfig.from_config(cfg or {})
    configured = bool(config.enabled or config.client_id or config.client_secret or config.robot_code)
    if not configured:
        results.append(CheckResult("dingtalk channel", True, "not configured; optional"))
        return

    requirements = check_dingtalk_requirements()
    errors = config.validate()
    resolved_provider = resolve_provider(
        model=str((cfg or {}).get("model") or ""),
        provider=str((cfg or {}).get("active_provider") or ""),
        config=cfg or {},
    )
    agent_api_key_ok = bool(resolved_provider.get("api_key"))
    missing = []
    if not requirements.get("dingtalk_stream"):
        missing.append("dingtalk-stream")
    if not requirements.get("httpx"):
        missing.append("httpx")
    if not requirements.get("stream_api"):
        missing.append("Stream API")
    if not requirements.get("robot_api"):
        missing.append("Robot API")

    detail_parts = [
        f"client_id={'yes' if config.client_id else 'no'}",
        f"client_secret={'yes' if config.client_secret else 'no'}",
        f"robot_code={'yes' if config.robot_code else 'no'}",
        f"group_policy={config.group_policy}",
        f"agent_provider={resolved_provider.get('provider') or 'unresolved'}",
        f"agent_api_key={'yes' if agent_api_key_ok else 'no'}",
    ]
    if missing:
        detail_parts.append("missing=" + ",".join(missing))
    if errors:
        detail_parts.append("config_errors=" + "; ".join(errors))

    ok = not missing and not errors and agent_api_key_ok
    results.append(
        CheckResult(
            "dingtalk channel",
            ok,
            "; ".join(detail_parts),
            "Run mclaw dingtalk login, then mclaw dingtalk check. Install M-Claw dependencies with: pip install -e .",
        )
    )


def run_doctor() -> list[CheckResult]:
    """Collect diagnostics across config, runtime, tools, channels, and host state."""
    from mclaw.cli.config import ConfigError

    results: list[CheckResult] = []
    cfg: dict | None = None
    try:
        cfg = _load_config()
        results.append(CheckResult("configuration", True, "ready"))
    except ConfigError as exc:
        results.append(
            CheckResult(
                "configuration",
                False,
                str(exc),
                "Fix config.yaml under MCLAW_HOME, then run mclaw doctor again.",
            )
        )
    except Exception as exc:
        results.append(
            CheckResult(
                "configuration",
                False,
                f"failed: {type(exc).__name__}: {exc}",
                "Check mclaw.cli.config imports and MCLAW_HOME access.",
            )
        )
        cfg = None
    results.append(CheckResult("runtime mode", True, "source"))
    try:
        from mclaw.runtime.manager import RuntimeManager

        runtime = RuntimeManager.current(cfg)
        info = runtime.doctor()
        results.append(
            CheckResult(
                "runtime",
                True,
                (
                    f"kind={info['kind']}; shell={info['shell']}; "
                    f"search={info['search_provider']}; workspace={info['workspace']}"
                ),
            )
        )
        launch_domain = info.get("launch_domain")
        if isinstance(launch_domain, dict):
            results.append(
                CheckResult(
                    "runtime launch domain",
                    True,
                    (
                        f"name={launch_domain.get('name')}; "
                        f"stdin_tty={launch_domain.get('stdin_tty')}; "
                        f"stdout_tty={launch_domain.get('stdout_tty')}; "
                        f"size={launch_domain.get('terminal_columns')}x{launch_domain.get('terminal_rows')}; "
                        f"uid={launch_domain.get('uid')}; "
                        f"context={launch_domain.get('selinux_context') or 'unknown'}"
                    ),
                )
            )
        for name, feature in sorted(info.get("features", {}).items()):
            results.append(
                CheckResult(
                    f"runtime feature {name}",
                    bool(feature.get("enabled") or feature.get("state") in {"disabled", "available_with_install", "available_with_config"}),
                    f"state={feature.get('state')}; reason={feature.get('reason') or ''}".rstrip(),
                    severity="warn" if feature.get("state") != "enabled" else "error",
                )
            )
    except Exception as exc:
        results.append(CheckResult("runtime", False, f"failed: {type(exc).__name__}: {exc}", "Check mclaw.runtime imports."))
    _append_secret_allowlist_check(results)
    _append_feature_checks(results, cfg)
    _append_provider_checks(results, cfg)

    try:
        from mclaw.platform import get_platform_info

        platform_info = get_platform_info(config=cfg)
        detail = f"{platform_info.os_name} {platform_info.os_release}; shell={platform_info.shell_name}"
        if platform_info.is_wsl:
            detail += "; wsl=yes"
        results.append(CheckResult("platform", True, detail))
        results.append(
            CheckResult(
                "desktop GUI",
                platform_info.gui_available,
                "available" if platform_info.gui_available else "not detected; pet will stay disabled in auto mode",
                "Start an X11/Wayland session or set DISPLAY/WAYLAND_DISPLAY before enabling display.pet.",
                severity="warn",
            )
        )
        results.append(
            CheckResult(
                "audio input",
                platform_info.audio_input_available,
                "available" if platform_info.audio_input_available else "not detected; ASR will stay disabled in auto mode",
                "Install/configure audio input and PortAudio before enabling ASR.",
                severity="warn",
            )
        )
    except Exception as exc:
        results.append(CheckResult("platform", False, f"failed: {type(exc).__name__}: {exc}", "Check platform detection imports."))

    ok, value = _exists_env_path("MCLAW_HOME")
    if not ok:
        value = display_mclaw_home() + " (default)"
    results.append(CheckResult("MCLAW_HOME", True, value, "Set MCLAW_HOME to the active M-Claw home directory."))

    browser_ok, browser_detail = find_playwright_browsers_root(cfg)
    if not browser_ok:
        browser_detail += "; not required unless browser tool is used"
    results.append(
        CheckResult(
            "playwright browsers",
            True,
            browser_detail,
            "Run python -m playwright install chromium.",
        )
    )

    _append_runtime_module_checks(results)

    _append_weixin_checks(results, cfg)
    _append_dingtalk_checks(results, cfg)

    try:
        from mclaw.tools.registry import registry
        from mclaw.tools.toolsets import resolve_multiple_toolsets

        enabled_toolsets = (cfg or {}).get("toolsets", ["mclaw-required"])
        if not isinstance(enabled_toolsets, list):
            enabled_toolsets = ["mclaw-required"]
        tool_names = resolve_multiple_toolsets([str(item) for item in enabled_toolsets])
        try:
            from mclaw.runtime.manager import RuntimeManager

            tool_names = RuntimeManager.current(cfg).features.filter_tool_names(tool_names)
        except Exception:
            logger.debug("Runtime feature filtering failed during doctor tool diagnostics", exc_info=True)

        diagnostics = registry.get_tool_diagnostics(tool_names=tool_names, config=cfg)
        _append_tool_diagnostics(results, diagnostics)
    except Exception as exc:
        results.append(CheckResult("tool diagnostics", False, f"failed: {type(exc).__name__}: {exc}", "Check tool registry imports."))

    return results


def _doctor_detail(item: CheckResult) -> str:
    detail = str(item.detail or "")
    name = str(item.name or "").lower()
    if name == "runtime mode":
        return f"{detail} mode" if detail else "ready"
    if name == "playwright browsers":
        if "not found" in detail or "path does not exist" in detail or "chromium=no" in detail:
            return "not installed; optional for browser tools"
        if "source=default cache" in detail:
            return "ready; source=default cache"
        return "ready"
    if name == "desktop gui" or name == "audio input":
        return "ready" if detail == "available" else detail
    if name == "secret allowlist":
        if detail == "not created yet":
            return "not initialized; will be created when needed"
        if "invalid JSON" in detail:
            return "invalid configuration"
        return "ready"
    if name.startswith("feature "):
        if detail.startswith("enabled;"):
            if "allowlist=ok" in detail:
                return "configured; secret scope ready"
            if "allowlist=missing" in detail:
                return "configured; secret scope missing"
            if "missing credentials" in detail:
                return "missing credentials"
            return "enabled"
        if detail.startswith("not enabled; credentials present"):
            return "optional; credentials present"
        if detail.startswith("not enabled"):
            return "optional; not configured"
    if name == "tool diagnostics" and detail.startswith("all registered tools available"):
        return detail.replace("registered", "enabled")
    if name in {"weixin channel", "dingtalk channel"}:
        if detail.startswith("not configured"):
            return "optional; not configured"
        if "missing=" in detail or "config_errors=" in detail or "agent_api_key=no" in detail:
            return "configuration incomplete"
        if "token=yes" in detail or "client_secret=yes" in detail:
            return "configured"
    if name.startswith("runtime feature ") and detail.startswith("state="):
        parts = dict(
            part.split("=", 1)
            for part in detail.split("; ")
            if "=" in part
        )
        state = parts.get("state", detail)
        reason = parts.get("reason", "").strip()
        if state == "enabled":
            if reason in {"", "PySide6 dependency probe", "Playwright dependency probe"}:
                return "enabled"
            if reason == "git available":
                return "enabled; checkpoint backend available"
        return state if not reason else f"{state}; {reason}"
    return detail


def _select_checks(
    results: list[CheckResult],
    names: tuple[str, ...] = (),
    prefixes: tuple[str, ...] = (),
) -> list[CheckResult]:
    exact = {name.lower() for name in names}
    lowered_prefixes = tuple(prefix.lower() for prefix in prefixes)
    selected: list[CheckResult] = []
    for item in results:
        lower = item.name.lower()
        if lower in exact or any(lower.startswith(prefix) for prefix in lowered_prefixes):
            selected.append(item)
    return selected


def _line_status(checks: list[CheckResult]) -> str:
    if not checks:
        return "WARN"
    if any(not item.ok and item.severity != "warn" for item in checks):
        return "FAIL"
    if any(not item.ok and item.severity == "warn" for item in checks):
        return "WARN"
    return "OK"


def _first_unhealthy(checks: list[CheckResult]) -> CheckResult | None:
    for item in checks:
        if not item.ok and item.severity != "warn":
            return item
    for item in checks:
        if not item.ok:
            return item
    return None


def _runtime_feature_state(item: CheckResult | None) -> str:
    if item is None:
        return ""
    detail = str(item.detail or "")
    if not detail.startswith("state="):
        rendered = _doctor_detail(item)
        return "enabled" if rendered.startswith("enabled") else rendered
    parts = dict(part.split("=", 1) for part in detail.split("; ") if "=" in part)
    return parts.get("state", "")


def _first_check(checks: list[CheckResult], name: str) -> CheckResult | None:
    lower = name.lower()
    for item in checks:
        if item.name.lower() == lower:
            return item
    return None


def _enabled_detail(checks: list[CheckResult]) -> str:
    feature = next((item for item in checks if item.name.lower().startswith("runtime feature ")), None)
    state = _runtime_feature_state(feature)
    if state == "enabled":
        return "enabled"
    return state or "not enabled"


def _pet_detail(checks: list[CheckResult]) -> str:
    feature = _first_check(checks, "runtime feature pet")
    state = _runtime_feature_state(feature)
    if state and state != "enabled":
        return "not configured"
    return "ready"


def _browser_tools_detail(checks: list[CheckResult]) -> str:
    browser_runtime = _first_check(checks, "playwright browsers")
    if browser_runtime is not None and _doctor_detail(browser_runtime).startswith("not installed"):
        return "not configured"
    return _enabled_detail(checks)


def _configured_detail(checks: list[CheckResult]) -> str:
    if not checks:
        return "not checked"
    detail = _doctor_detail(checks[0])
    if detail.startswith("configured"):
        return "configured"
    if detail.startswith("optional; not configured"):
        return "not configured"
    if detail.startswith("optional; credentials present"):
        return "not configured"
    return detail


def _channel_detail(checks: list[CheckResult]) -> str:
    if not checks:
        return "not checked"
    detail = _doctor_detail(checks[0])
    if detail.startswith("optional; not configured"):
        return "not configured"
    return detail


def _doctor_line(
    section: str,
    name: str,
    checks: list[CheckResult],
    ok_detail: str | object,
) -> DoctorLine:
    status = _line_status(checks)
    if status == "OK":
        detail = ok_detail(checks) if callable(ok_detail) else str(ok_detail)
        return DoctorLine(section, name, status, detail)
    unhealthy = _first_unhealthy(checks)
    if unhealthy is None:
        return DoctorLine(section, name, status, "not checked")
    return DoctorLine(section, name, status, _doctor_detail(unhealthy), unhealthy.fix)


def _product_doctor_lines(results: list[CheckResult]) -> list[DoctorLine]:
    """Map low-level checks into the stable product-facing doctor sections."""
    lines: list[DoctorLine] = []
    mapped: set[int] = set()

    def add(section: str, name: str, checks: list[CheckResult], detail: str | object) -> None:
        mapped.update(id(item) for item in checks)
        lines.append(_doctor_line(section, name, checks, detail))

    add("核心运行时", "Agent Runtime", _select_checks(results, ("runtime mode", "runtime")), "ready")
    add("核心运行时", "Command Interface", _select_checks(results, ("command interface",)), "ready")
    add("核心运行时", "Local Storage", _select_checks(results, ("local storage",)), "ready")
    add("核心运行时", "TUI", _select_checks(results, ("tui",)), "ready")
    add("核心运行时", "Pet", _select_checks(results, ("runtime feature pet", "desktop companion", "desktop gui")), _pet_detail)
    add("核心运行时", "Model Client", _select_checks(results, ("model client",)), "ready")

    add("工具能力", "File Operation", _select_checks(results, ("runtime feature file",)), _enabled_detail)
    add("工具能力", "Terminal", _select_checks(results, ("runtime feature terminal",)), _enabled_detail)
    add("工具能力", "Vision Analysis", _select_checks(results, ("feature 视觉分析",)), _configured_detail)
    add("工具能力", "Web Search", _select_checks(results, ("feature 网页搜索",)), _configured_detail)
    add(
        "工具能力",
        "Browser Tools",
        _select_checks(
            results,
            ("runtime feature browser_tool", "playwright browsers", "toolset browser"),
            ("tool browser_",),
        ),
        _browser_tools_detail,
    )

    add("IM通道", "Weixin", _select_checks(results, ("weixin channel",)), _channel_detail)
    add("IM通道", "DingTalk", _select_checks(results, ("dingtalk channel",)), _channel_detail)

    for item in results:
        if id(item) in mapped or item.ok:
            continue
        lines.append(
            DoctorLine(
                "其他",
                item.name,
                _line_status([item]),
                _doctor_detail(item),
                item.fix,
            )
        )

    return lines


def format_doctor(results: list[CheckResult]) -> str:
    """Render doctor results as a compact terminal report."""
    product_lines = _product_doctor_lines(results)
    failed = sum(1 for item in product_lines if item.status == "FAIL")
    warnings = sum(1 for item in product_lines if item.status == "WARN")
    passed = len(product_lines) - failed - warnings
    state = "可运行" if failed == 0 and warnings == 0 else ("可运行，存在警告" if failed == 0 else "需要修复")

    lines = [
        "M-CLAW 运行环境诊断",
        "",
        "总览",
        f"  状态      {state}",
        f"  检查项    {passed}/{len(product_lines)} 就绪",
        f"  警告      {warnings}",
        f"  失败      {failed}",
    ]

    section_order = ["核心运行时", "工具能力", "IM通道", "其他"]
    grouped: dict[str, list[DoctorLine]] = {section: [] for section in section_order}
    for item in product_lines:
        grouped.setdefault(item.section, []).append(item)

    for section in section_order:
        items = grouped.get(section) or []
        if not items:
            continue
        lines.extend(["", section])
        for item in items:
            lines.append(f"  {item.status:<5} {item.name:<22} {item.detail}")
            if item.status != "OK" and item.fix:
                lines.append(f"        修复: {item.fix}")

    return "\n".join(lines)


def main() -> None:
    try:
        from mclaw.cli.tui.console import configure_text_output

        configure_text_output()
    except Exception:
        logger.debug("Failed to configure doctor text output", exc_info=True)
    print(format_doctor(run_doctor()))


if __name__ == "__main__":
    main()
