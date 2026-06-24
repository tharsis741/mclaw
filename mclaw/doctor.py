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
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from mclaw.constants import display_mclaw_home, get_mclaw_home


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str
    fix: str = ""
    severity: str = "error"


def _load_config() -> dict:
    from mclaw.cli.config import load_config

    return load_config()


def _append_secret_allowlist_check(results: list[CheckResult]) -> None:
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


def _has_chromium_browser(root: Path) -> bool:
    return any(root.glob("chromium-*")) or any(root.glob("chrome-*")) or any(root.glob("**/chrome.exe"))


def _find_browser_executable(config: dict | None = None) -> tuple[bool, str]:
    env_value = os.environ.get("MCLAW_BROWSER_EXECUTABLE_PATH", "").strip().strip('"')
    if env_value:
        path = Path(env_value)
        return path.exists() and path.is_file(), f"{env_value}; source=env MCLAW_BROWSER_EXECUTABLE_PATH"
    if os.name != "nt":
        for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "microsoft-edge"):
            found = shutil.which(name)
            if found:
                return True, f"{found}; source=system PATH"
    return False, ""


def _find_playwright_browsers_root(config: dict | None = None) -> tuple[bool, str]:
    exe_ok, exe_detail = _find_browser_executable(config)
    if exe_ok:
        return True, exe_detail

    env_value = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip().strip('"')
    if env_value:
        root = Path(env_value)
        if not root.exists():
            return False, f"{env_value}; path does not exist"
        return _has_chromium_browser(root), f"{env_value}; chromium={'yes' if _has_chromium_browser(root) else 'no'}"

    candidates = []
    local_appdata = os.environ.get("LOCALAPPDATA", "").strip()
    if local_appdata:
        candidates.append(Path(local_appdata) / "ms-playwright")
    candidates.append(Path.home() / ".cache" / "ms-playwright")
    if os.name == "nt":
        candidates.append(Path.home() / "AppData" / "Local" / "ms-playwright")

    seen: set[str] = set()
    for root in candidates:
        key = os.path.normcase(str(root))
        if key in seen:
            continue
        seen.add(key)
        if root.exists():
            return _has_chromium_browser(root), f"{root}; chromium={'yes' if _has_chromium_browser(root) else 'no'}; source=default cache"
    return False, "not found in MCLAW_BROWSER_EXECUTABLE_PATH, PLAYWRIGHT_BROWSERS_PATH, system PATH, or default cache"

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
        return CheckResult("sqlite3 + FTS5", True, "available")
    except Exception as exc:
        return CheckResult(
            "sqlite3 + FTS5",
            False,
            f"unavailable: {type(exc).__name__}: {exc}",
            "Install a Python build with sqlite3 and SQLite FTS5 support.",
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
                "Install DingTalk optional dependencies: pip install -e .[dingtalk]",
            )
        )
        return

    config = DingTalkConfig.from_config(cfg or {})
    configured = bool(config.enabled or config.client_id or config.client_secret)
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
    results: list[CheckResult] = []
    cfg: dict | None = None
    try:
        cfg = _load_config()
    except Exception:
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

    browser_ok, browser_detail = _find_playwright_browsers_root(cfg)
    if not browser_ok:
        browser_detail += "; not required unless browser tool is used"
    results.append(
        CheckResult(
            "playwright browsers",
            True,
            browser_detail,
            "Run playwright install chromium, or set MCLAW_BROWSER_EXECUTABLE_PATH.",
        )
    )

    for module in ("openai", "httpx", "pydantic", "rich", "prompt_toolkit", "yaml"):
        available = _module_available(module)
        results.append(CheckResult(f"main module {module}", available, "importable" if available else "missing", f"Install Python package: {module}"))

    results.append(_check_sqlite_fts5())

    for module in ("PySide6", "shiboken6", "dashscope", "sounddevice"):
        available = _module_available(module)
        results.append(
            CheckResult(
                f"optional module {module}",
                available,
                "importable" if available else "missing; optional capability may be unavailable",
                f"Install Python package {module} when using the related capability.",
                severity="warn",
            )
        )

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
            pass

        diagnostics = registry.get_tool_diagnostics(tool_names=tool_names, config=cfg)
        unavailable = [item for item in diagnostics if not item.get("available")]
        if unavailable:
            for item in unavailable:
                detail = item.get("reason") or "unavailable"
                fix_text = item.get("fix") or "Check tool configuration and dependencies."
                tool_name = str(item.get("tool") or "")
                severity = "warn" if tool_name.startswith("browser_") else "error"
                results.append(CheckResult(f"tool {tool_name}", False, detail, fix_text, severity=severity))
        else:
            results.append(CheckResult("tool diagnostics", True, f"all registered tools available ({len(diagnostics)})"))
    except Exception as exc:
        results.append(CheckResult("tool diagnostics", False, f"failed: {type(exc).__name__}: {exc}", "Check tool registry imports."))

    return results


def format_doctor(results: list[CheckResult]) -> str:
    lines = ["M-Claw runtime doctor"]
    for item in results:
        mark = "OK" if item.ok else ("WARN" if item.severity == "warn" else "FAIL")
        lines.append(f"[{mark}] {item.name}: {item.detail}")
        if not item.ok and item.fix:
            lines.append(f"      fix: {item.fix}")
    failed = sum(1 for item in results if not item.ok and item.severity != "warn")
    warnings = sum(1 for item in results if not item.ok and item.severity == "warn")
    lines.append(f"Summary: {len(results) - failed - warnings}/{len(results)} checks passed; warnings={warnings}; failures={failed}")
    return "\n".join(lines)


def main() -> None:
    try:
        from mclaw.cli.tui.console import configure_text_output

        configure_text_output()
    except Exception:
        pass
    print(format_doctor(run_doctor()))


if __name__ == "__main__":
    main()
