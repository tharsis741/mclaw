# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Command-line entry point, first-run setup, and startup wiring for M-Claw.

The module keeps process bootstrap, configuration discovery, provider setup, and
channel launch decisions in one place so the interactive app receives a fully
resolved runtime configuration.
"""

import argparse
import multiprocessing as mp
import logging
import os
import sys
from pathlib import Path

from mclaw.cli.env_loader import load_mclaw_dotenv
from mclaw.cli.runtime.session_commands import RESUME_LATEST_SESSION
from mclaw.cli.tui.console import configure_text_output, print_plain
from mclaw.constants import display_mclaw_path


def _setup_logging(level: str = "INFO"):
    """Configure file-only process logging before any runtime components start."""
    from mclaw.constants import get_mclaw_home
    log_dir = get_mclaw_home() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(log_dir / "mclaw.log", encoding="utf-8"),
        ],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)


def _format_cli_help() -> str:
    """Return the curated top-level help shown before argparse dispatch."""
    return """M-CLAW 命令指引

核心命令
  mclaw                         启动交互式 Agent Runtime
  mclaw resume [session_id]      恢复最近会话，或按 ID/前缀恢复指定会话
  mclaw setup                   配置模型、工具、通道和可选能力
  mclaw doctor                  检查运行环境、依赖、凭据、工具和通道配置
  mclaw help                    显示这份帮助

通道命令
  mclaw weixin login            微信 iLink Bot 扫码登录
  mclaw weixin                  启动微信私聊网关
  mclaw dingtalk login          配置钉钉 Stream 网关凭据
  mclaw dingtalk check          检查钉钉依赖和配置
  mclaw dingtalk                启动钉钉 Stream 网关

推荐流程
  1. mclaw setup
  2. mclaw doctor
  3. mclaw
"""


class _MClawArgumentParser(argparse.ArgumentParser):
    """ArgumentParser variant that renders short product-native errors."""

    def error(self, message: str) -> None:
        msg = str(message or "")
        if "invalid choice" in msg:
            print_plain("未知命令。")
        elif "unrecognized arguments:" in msg:
            unknown = msg.split("unrecognized arguments:", 1)[1].strip()
            print_plain(f"未知参数：{unknown}")
        else:
            print_plain(f"命令参数无效：{msg}")
        print_plain("运行 `mclaw help` 查看可用命令。")
        raise SystemExit(2)


def _prompt_secret(prompt: str) -> str:
    """Prompt for a secret; echo one '*' per typed character when interactive."""
    if os.name != "nt":
        try:
            import termios
            import tty
        except ImportError:
            termios = None
            tty = None

        if termios is not None and tty is not None:
            try:
                if sys.stdin.isatty() and sys.stdout.isatty():
                    chars: list[str] = []
                    sys.stdout.write(prompt)
                    sys.stdout.flush()
                    fd = sys.stdin.fileno()
                    old_settings = termios.tcgetattr(fd)
                    try:
                        tty.setraw(fd)
                        while True:
                            ch = sys.stdin.read(1)
                            if ch in ("\r", "\n"):
                                sys.stdout.write("\n")
                                sys.stdout.flush()
                                return "".join(chars)
                            if ch == "\x03":
                                sys.stdout.write("\n")
                                sys.stdout.flush()
                                raise KeyboardInterrupt
                            if ch == "\x04":
                                sys.stdout.write("\n")
                                sys.stdout.flush()
                                raise EOFError
                            if ch in ("\x7f", "\b"):
                                if chars:
                                    chars.pop()
                                    sys.stdout.write("\b \b")
                                    sys.stdout.flush()
                                continue
                            if ch == "\x15":
                                while chars:
                                    chars.pop()
                                    sys.stdout.write("\b \b")
                                sys.stdout.flush()
                                continue
                            if ch >= " ":
                                chars.append(ch)
                                sys.stdout.write("*")
                                sys.stdout.flush()
                    finally:
                        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
            except (termios.error, OSError):
                pass

        import getpass

        return getpass.getpass(prompt)

    import msvcrt

    chars: list[str] = []
    sys.stdout.write(prompt)
    sys.stdout.flush()
    while True:
        ch = msvcrt.getwch()
        if ch in ("\r", "\n"):
            sys.stdout.write("\n")
            sys.stdout.flush()
            return "".join(chars)
        if ch == "\x03":
            sys.stdout.write("\n")
            sys.stdout.flush()
            raise KeyboardInterrupt
        if ch == "\x1a":
            sys.stdout.write("\n")
            sys.stdout.flush()
            raise EOFError
        if ch in ("\x00", "\xe0"):
            msvcrt.getwch()
            continue
        if ch in ("\b", "\x7f"):
            if chars:
                chars.pop()
                sys.stdout.write("\b \b")
                sys.stdout.flush()
            continue
        chars.append(ch)
        sys.stdout.write("*")
        sys.stdout.flush()


class _SetupCancelled(Exception):
    """Raised when the interactive setup flow is intentionally interrupted."""


_SETUP_CONFIGURED_COUNT = 0


def _setup_input(prompt: str, *, dim: bool = False) -> str:
    from mclaw.cli.colors import Colors, color

    try:
        style = Colors.DIM if dim else Colors.YELLOW
        return input(color(prompt, style)).strip()
    except (EOFError, KeyboardInterrupt):
        print_plain()
        raise _SetupCancelled


def _setup_secret(prompt: str) -> str:
    from mclaw.cli.colors import Colors, color

    try:
        return _prompt_secret(color(prompt, Colors.YELLOW)).strip()
    except (EOFError, KeyboardInterrupt):
        print_plain()
        raise _SetupCancelled


def _print_setup_cancelled(configured_count: int = 0) -> None:
    from mclaw.cli.colors import Colors, color

    print_plain(color("\n  设置向导已中止。", Colors.YELLOW, Colors.BOLD))
    if configured_count > 0:
        print_plain(color("  已完成的接入方配置已保存，可稍后运行 mclaw setup 继续配置。", Colors.DIM))
        print_plain(color("  启动: mclaw", Colors.CYAN))
    else:
        print_plain(color("  未保存新的接入方配置，可稍后运行 mclaw setup 重新开始。", Colors.DIM))
    print_plain()


def _parse_secret_batch_values(text: str, env_vars: list[str]) -> tuple[dict[str, str], str]:
    """Parse one masked setup input into the exact credential variables requested."""
    import json as _json
    import re as _re

    env_vars = [str(item or "").strip().upper() for item in env_vars if str(item or "").strip()]
    raw = str(text or "").strip()
    if not raw:
        return {}, ""
    if len(env_vars) == 1 and "=" not in raw and not raw.startswith("{"):
        return {env_vars[0]: raw}, ""

    values: dict[str, str] = {}
    if raw.startswith("{"):
        try:
            parsed = _json.loads(raw)
        except Exception:
            return {}, "JSON 格式无效。请使用 KEY=value; KEY2=value 或 JSON 对象。"
        if not isinstance(parsed, dict):
            return {}, "JSON 格式无效。顶层必须是对象。"
        values = {str(k).strip().upper(): str(v).strip() for k, v in parsed.items() if str(k).strip()}
    else:
        parts = [part.strip() for part in _re.split(r"[\n;]+", raw) if part.strip()]
        if all("=" in part for part in parts):
            for part in parts:
                key, _, value = part.partition("=")
                values[key.strip().upper()] = value.strip().strip('"\'')
        elif len(parts) == len(env_vars):
            values = {env_var: value for env_var, value in zip(env_vars, parts)}
        else:
            return {}, "多个凭据请使用 KEY=value; KEY2=value、JSON，或按顺序用分号分隔。"

    unknown = sorted(set(values) - set(env_vars))
    if unknown:
        return {}, f"不需要这些凭据名称: {', '.join(unknown)}。"
    missing = [env_var for env_var in env_vars if not values.get(env_var)]
    if missing:
        return {}, f"缺少凭据: {', '.join(missing)}。"
    return {env_var: values[env_var] for env_var in env_vars}, ""


def _prompt_secret_batch(env_vars: list[str], *, prompt: str, print_plain, color, Colors) -> dict[str, str] | None:
    """Collect one or more setup secrets through a single hidden prompt."""
    requested = [str(item or "").strip().upper() for item in env_vars if str(item or "").strip()]
    if not requested:
        return {}
    if len(requested) > 1:
        print_plain(color("  请在一个加密输入中填写所有凭据。", Colors.DIM))
        print_plain(color("  格式：KEY=value; KEY2=value，JSON 对象，或按顺序用分号分隔。", Colors.DIM))
        print_plain(color(f"  需要填写：{', '.join(requested)}", Colors.DIM))
    while True:
        try:
            raw = _prompt_secret(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print_plain()
            raise _SetupCancelled
        if not raw:
            return None
        values, error = _parse_secret_batch_values(raw, requested)
        if not error:
            return values
        print_plain(color(f"  {error}", Colors.YELLOW))


def _print_setup_header(title: str, subtitle: str = "", *, indent: str = "  ") -> None:
    from mclaw.cli.colors import Colors, color
    from mclaw.cli.tui.assets import MCLAW_LOGO
    from mclaw.cli.tui.console import print_rich

    print_rich(MCLAW_LOGO.strip("\n"))
    print_plain()
    print_rich(f"[bold #6CB4EE]{indent}Powered by M-Robots OS[/]")
    print_rich(f"[#2C5F8A]{indent}────────────────────────────────────────[/]")
    print_plain()
    print_plain(color(f"{indent}{title}", Colors.BOLD, Colors.CYAN))
    if subtitle:
        print_plain(color(f"{indent}{subtitle}", Colors.DIM))


def _print_setup_intro() -> bool:
    from mclaw.cli.colors import Colors, color

    _print_setup_header("欢迎使用 M-Claw", "本向导将帮助你配置模型供应商与本地运行权限。", indent="")
    print_plain()
    print_plain(color("使用前确认", Colors.BOLD, Colors.BLUE))
    print_plain(color("以下内容用于说明在本地运行M-CLAW会涉及的权限边界。", Colors.DIM))
    print_plain()
    print_plain(color("工作区   ", Colors.BOLD, Colors.CYAN) + color("读取当前工作区，必要上下文可能发送给你选择的模型供应商", Colors.DIM))
    print_plain(color("凭据     ", Colors.BOLD, Colors.CYAN) + color(f"API Key 等敏感凭据会保存到 {display_mclaw_path('.env')}", Colors.DIM))
    print_plain(color("操作     ", Colors.BOLD, Colors.CYAN) + color("交互过程中，M-Claw 可能按你的指令读写文件、执行命令或进行敏感操作", Colors.DIM))
    print_plain()
    if not sys.stdin.isatty():
        return False
    while True:
        try:
            choice = _setup_input("输入 Y 继续设置，输入 N 退出设置向导：").lower()
        except EOFError:
            return False
        if choice == "y":
            return True
        if choice == "n":
            print_plain(color("已退出设置向导。", Colors.DIM))
            sys.exit(0)
        print_plain(color("请输入 Y 或 N。", Colors.YELLOW))


def _print_setup_step(title: str, detail: str = "", *, leading_blank: bool = True) -> None:
    from mclaw.cli.colors import Colors, color

    prefix = "\n" if leading_blank else ""
    print_plain(_setup_brand_color(f"{prefix}  {title}", bold=True))
    if detail:
        print_plain(color(f"  {detail}", Colors.DIM))


def _setup_brand_color(text: str, *, bold: bool = False) -> str:
    from mclaw.cli.colors import Colors, should_use_color

    if not should_use_color():
        return text
    prefix = "\033["
    if bold:
        prefix += "1;"
    prefix += "38;2;108;180;238m"
    return prefix + text + Colors.RESET


def _setup_index(index: int | str) -> str:
    from mclaw.cli.colors import Colors, color

    return color(str(index), Colors.BOLD, Colors.BLUE)


def _setup_label(label: str, width: int = 8) -> str:
    from mclaw.cli.colors import Colors, color

    return color(f"  {label:<{width}}", Colors.BOLD, Colors.BLUE)


def _has_any_provider_configured() -> bool:
    """Check if any API provider is configured (env vars or config)."""
    from mclaw.cli.auth import PROVIDER_REGISTRY, resolve_api_key
    from mclaw.cli.config import get_env_value, load_config

    # Check all registered providers.
    for pname in PROVIDER_REGISTRY:
        if resolve_api_key(pname):
            return True

    # Check custom endpoint environment variables.
    for var in ("MCLAW_API_KEY", "MCLAW_ANTHROPIC_API_KEY", "OPENAI_BASE_URL"):
        if get_env_value(var):
            return True

    # Check active_provider and user-defined providers in config.yaml.
    cfg = load_config(strict=True)
    if cfg.get("active_provider") or cfg.get("providers"):
        return True

    return False


def _first_run_check() -> bool:
    """Detect first run. Returns True if setup wizard should be shown."""
    from mclaw.constants import get_mclaw_home
    home = get_mclaw_home()
    if not home.exists():
        return True
    if not (home / "config.yaml").exists():
        return True

    # If the config file exists, keep checking whether it contains usable content.
    from mclaw.cli.config import load_config
    cfg = load_config(strict=True)
    has_model = bool(cfg.get("model"))
    fallback_entries = cfg.get("fallback_providers")
    has_fallback_model = any(
        isinstance(entry, dict)
        and str(entry.get("provider") or "").strip()
        and str(entry.get("model") or "").strip()
        for entry in fallback_entries
    ) if isinstance(fallback_entries, list) else False
    providers = cfg.get("providers")
    has_user_provider_model = any(
        isinstance(provider_cfg, dict)
        and str(provider_cfg.get("model") or "").strip()
        for provider_cfg in providers.values()
    ) if isinstance(providers, dict) else False
    if has_model or has_fallback_model or has_user_provider_model:
        return False

    return True


def _run_chat(args):
    """Resolve first-run setup, provider credentials, and then enter the TUI."""
    from mclaw.cli.config import load_merged_config, ensure_mclaw_home, ConfigError
    from mclaw.cli.auth import resolve_provider
    from mclaw.cli.colors import Colors, color

    ensure_mclaw_home()

    try:
        first_run = _first_run_check()
    except ConfigError as exc:
        print_plain(color(f"\n  配置错误: {exc}\n", Colors.RED))
        sys.exit(1)

    if first_run:
        _run_setup(args)
        try:
            provider_configured = _has_any_provider_configured()
        except ConfigError as exc:
            print_plain(color(f"\n  配置错误: {exc}\n", Colors.RED))
            sys.exit(1)
        if not provider_configured:
            print_plain(color("  未配置任何供应商，退出。\n", Colors.RED))
            sys.exit(1)

    try:
        config = load_merged_config()
    except ConfigError as exc:
        print_plain(color(f"\n  配置错误: {exc}\n", Colors.RED))
        sys.exit(1)

    # Apply project-level terminal.cwd if present (TERMINAL_CWD env var takes priority)
    project_dir = config.get("_project_config_dir")
    terminal_cwd = os.environ.get("TERMINAL_CWD", "")
    if not terminal_cwd and project_dir:
        raw_cwd = config.get("terminal", {}).get("cwd", ".")
        target = Path(project_dir) / raw_cwd
        try:
            if target.exists() and target.is_dir():
                os.chdir(target)
            else:
                print_plain(color(
                    f"\n  警告: terminal.cwd '{raw_cwd}' 不存在，使用项目根目录。\n",
                    Colors.YELLOW,
                ))
                os.chdir(project_dir)
        except OSError as exc:
            print_plain(color(
                f"\n  错误: 无法切换到工作目录: {exc}\n",
                Colors.RED,
            ))
            sys.exit(1)

    config["_launch_cwd"] = os.getcwd()

    model = getattr(args, "model", "") or config.get("model", "")
    provider_name = getattr(args, "provider", "") or config.get("active_provider", "")

    resolved = resolve_provider(
        model=model,
        provider=provider_name,
        base_url=getattr(args, "base_url", ""),
        api_key=getattr(args, "api_key", ""),
        config=config,
    )

    if not resolved["api_key"]:
        print_plain(color(
            "\n  未找到 API 密钥。请设置以下环境变量之一:\n"
            "    OPENROUTER_API_KEY, OPENAI_API_KEY, ANTHROPIC_API_KEY 或 GOOGLE_API_KEY\n"
            "  或运行: mclaw setup\n",
            Colors.RED,
        ))
        sys.exit(1)

    if not resolved["model"]:
        print_plain(color(
            "\n  未指定模型。请运行 mclaw setup 配置默认模型。\n",
            Colors.RED,
        ))
        sys.exit(1)

    resume_id = getattr(args, "resume", None) or ""
    enabled_toolsets = config.get("toolsets", ["mclaw-required"])
    from mclaw.cli.app import run_interactive
    run_interactive(
        model=resolved["model"],
        api_key=resolved["api_key"],
        base_url=resolved["base_url"],
        api_mode=resolved["api_mode"],
        provider=resolved["provider"],
        resume_session_id=resume_id,
        enabled_toolsets=enabled_toolsets,
        config=config,
    )


def _run_weixin(args):
    """Run the Weixin private-chat gateway."""
    import asyncio

    from mclaw.cli.auth import resolve_provider
    from mclaw.cli.colors import Colors, color
    from mclaw.cli.config import ConfigError, ensure_mclaw_home, load_merged_config
    from mclaw.channels.weixin import WeixinRuntime
    from mclaw.channels.weixin.runtime_lock import WeixinRuntimeLockError

    ensure_mclaw_home()
    try:
        config = load_merged_config()
    except ConfigError as exc:
        print_plain(color(f"\n  配置错误: {exc}\n", Colors.RED))
        sys.exit(1)

    model = getattr(args, "model", "") or config.get("model", "")
    provider_name = getattr(args, "provider", "") or config.get("active_provider", "")
    resolved = resolve_provider(
        model=model,
        provider=provider_name,
        base_url=getattr(args, "base_url", ""),
        api_key=getattr(args, "api_key", ""),
        config=config,
    )
    if not resolved["api_key"]:
        print_plain(color("\n  未找到 API 密钥。请先运行 mclaw setup 配置模型供应商。\n", Colors.RED))
        sys.exit(1)
    if not resolved["model"]:
        print_plain(color("\n  未指定模型。请先运行 mclaw setup 配置默认模型。\n", Colors.RED))
        sys.exit(1)

    runtime = WeixinRuntime(
        config=config,
        model=resolved["model"],
        api_key=resolved["api_key"],
        base_url=resolved["base_url"],
        api_mode=resolved["api_mode"],
        provider=resolved["provider"],
    )
    errors = runtime.weixin_config.validate()
    if errors:
        print_plain(color("\n  微信未登录：缺少 WEIXIN_ACCOUNT_ID 或 WEIXIN_TOKEN。", Colors.RED))
        print_plain(color("  请先运行: mclaw weixin login", Colors.DIM))
        print_plain(color("  或扫码成功后直接连接: mclaw weixin login --connect\n", Colors.DIM))
        sys.exit(1)

    print_plain(color("\n  微信私聊网关已启动。", Colors.GREEN))
    print_plain(color(f"  account: {runtime.weixin_config.account_id[:8]}...", Colors.DIM))
    print_plain(color(f"  base:    {runtime.weixin_config.base_url}", Colors.DIM))
    print_plain(color("  正在长轮询微信消息；按 Ctrl+C 停止。\n", Colors.DIM))
    try:
        asyncio.run(runtime.run_forever())
    except KeyboardInterrupt:
        print_plain(color("\n  微信私聊网关已停止。\n", Colors.DIM))
    except RuntimeError as exc:
        message = str(exc)
        if "WEIXIN_ACCOUNT_ID" in message or "WEIXIN_TOKEN" in message:
            print_plain(color("\n  微信未登录：缺少 WEIXIN_ACCOUNT_ID 或 WEIXIN_TOKEN。", Colors.RED))
            print_plain(color("  请先运行: mclaw weixin login", Colors.DIM))
            print_plain(color("  或扫码成功后直接连接: mclaw weixin login --connect\n", Colors.DIM))
            sys.exit(1)
        if isinstance(exc, WeixinRuntimeLockError) or "already running" in message:
            print_plain(color("\n  微信私聊网关已经在运行。", Colors.YELLOW))
            print_plain(color(f"  {message}", Colors.DIM))
            print_plain(color("  请先在旧终端按 Ctrl+C 停止，再重新启动。\n", Colors.DIM))
            sys.exit(1)
        raise


_WEIXIN_CREDENTIAL_ENV_VARS = (
    "WEIXIN_ACCOUNT_ID",
    "WEIXIN_TOKEN",
)


def _render_weixin_login_header() -> None:
    _print_setup_header(
        "M-Claw 微信登录",
        "通过 iLink Bot 扫码获取微信私聊网关凭据。",
    )


def _load_weixin_credentials() -> dict[str, str]:
    from mclaw.cli.config import get_env_value

    return {
        env_var: str(get_env_value(env_var) or "").strip()
        for env_var in _WEIXIN_CREDENTIAL_ENV_VARS
    }


def _weixin_credentials_configured(credentials: dict[str, str] | None = None) -> bool:
    values = credentials or _load_weixin_credentials()
    return all(bool(values.get(env_var)) for env_var in _WEIXIN_CREDENTIAL_ENV_VARS)


def _authorize_weixin_credentials() -> None:
    try:
        from mclaw.runtime.secrets import authorize

        authorize("channel:weixin", _WEIXIN_CREDENTIAL_ENV_VARS)
    except Exception:
        logging.getLogger(__name__).debug("Failed to authorize Weixin channel secret", exc_info=True)


def _run_weixin_login(args, *, optional: bool = False, skip_existing: bool = False) -> bool:
    """Run the Weixin iLink QR login setup flow."""
    import asyncio

    from mclaw.channels.weixin.qr_login import qr_login
    from mclaw.cli.colors import Colors, color
    from mclaw.cli.config import ensure_mclaw_home, save_env_value

    ensure_mclaw_home()
    _render_weixin_login_header()

    if skip_existing and _weixin_credentials_configured():
        _authorize_weixin_credentials()
        print_plain(color("  已检测到已保存的微信登录信息，跳过扫码登录。", Colors.GREEN))
        return True

    _print_setup_step("步骤 1/1  扫码登录", "请在终端二维码出现后使用微信完成确认。")
    credentials = asyncio.run(
        qr_login(
            bot_type=str(getattr(args, "bot_type", "3") or "3"),
            timeout_seconds=int(getattr(args, "timeout", 480) or 480),
            render_qr=not bool(getattr(args, "no_qr", False)),
        )
    )
    if credentials is None:
        print_plain(color("\n  微信扫码登录失败或已超时。\n", Colors.RED))
        if optional:
            return False
        sys.exit(1)

    save_env_value("WEIXIN_ACCOUNT_ID", credentials.account_id)
    save_env_value("WEIXIN_TOKEN", credentials.token)
    save_env_value("WEIXIN_BASE_URL", credentials.base_url)
    _authorize_weixin_credentials()

    print_plain(color(f"\n  微信凭据已保存到 {display_mclaw_path('.env')}。", Colors.GREEN))
    if bool(getattr(args, "connect", False)):
        print_plain(color("  正在启动微信私聊网关...\n", Colors.DIM))
        _run_weixin(args)
    else:
        print_plain(color("  下一步: mclaw weixin\n", Colors.CYAN))
    return True


def _run_dingtalk(args):
    """Run the DingTalk Stream Mode gateway."""
    import asyncio

    from mclaw.channels.dingtalk import DingTalkRuntime
    from mclaw.channels.dingtalk.runtime_lock import DingTalkRuntimeLockError
    from mclaw.cli.auth import resolve_provider
    from mclaw.cli.colors import Colors, color
    from mclaw.cli.config import ConfigError, ensure_mclaw_home, load_merged_config

    ensure_mclaw_home()
    try:
        config = load_merged_config()
    except ConfigError as exc:
        print_plain(color(f"\n  配置错误: {exc}\n", Colors.RED))
        sys.exit(1)

    model = getattr(args, "model", "") or config.get("model", "")
    provider_name = getattr(args, "provider", "") or config.get("active_provider", "")
    resolved = resolve_provider(
        model=model,
        provider=provider_name,
        base_url=getattr(args, "base_url", ""),
        api_key=getattr(args, "api_key", ""),
        config=config,
    )
    if not resolved["api_key"]:
        print_plain(color("\n  未找到 API 密钥。请先运行 mclaw setup 配置模型供应商。\n", Colors.RED))
        sys.exit(1)
    if not resolved["model"]:
        print_plain(color("\n  未指定模型。请先运行 mclaw setup 配置默认模型。\n", Colors.RED))
        sys.exit(1)

    runtime = DingTalkRuntime(
        config=config,
        model=resolved["model"],
        api_key=resolved["api_key"],
        base_url=resolved["base_url"],
        api_mode=resolved["api_mode"],
        provider=resolved["provider"],
    )
    errors = runtime.dingtalk_config.validate()
    if errors:
        print_plain(color("\n  钉钉未配置：缺少 DINGTALK_CLIENT_ID 或 DINGTALK_CLIENT_SECRET。", Colors.RED))
        print_plain(color(f"  请运行 mclaw dingtalk login，或在 {display_mclaw_path('.env')} 中配置 DINGTALK_CLIENT_ID / DINGTALK_CLIENT_SECRET / DINGTALK_ROBOT_CODE。\n", Colors.DIM))
        for error in errors:
            print_plain(color(f"  - {error}", Colors.DIM))
        print_plain()
        sys.exit(1)

    async def _run_connected_runtime() -> None:
        await runtime.start()
        print_plain(color("\n  钉钉 Stream 网关已启动。", Colors.GREEN))
        print_plain(color(f"  client: {runtime.dingtalk_config.client_id[:8]}...", Colors.DIM))
        print_plain(color(f"  group:  {runtime.dingtalk_config.group_policy}", Colors.DIM))
        print_plain(color("  正在通过 Stream Mode 接收钉钉消息；按 Ctrl+C 停止。\n", Colors.DIM))
        try:
            while runtime._stream_task and not runtime._stream_task.done():
                await asyncio.sleep(1)
            if runtime._stream_task:
                await runtime._stream_task
        except asyncio.CancelledError:
            await asyncio.shield(runtime.stop())
            raise
        else:
            await runtime.stop()

    try:
        asyncio.run(_run_connected_runtime())
    except KeyboardInterrupt:
        print_plain(color("\n  钉钉 Stream 网关已停止。\n", Colors.DIM))
    except RuntimeError as exc:
        message = str(exc)
        if "dingtalk-stream" in message:
            print_plain(color("\n  缺少钉钉运行依赖。", Colors.RED))
            print_plain(color("  请在 M-Claw 源码目录安装依赖: pip install -e .\n", Colors.DIM))
            sys.exit(1)
        if isinstance(exc, DingTalkRuntimeLockError) or "already running" in message:
            print_plain(color("\n  钉钉 Stream 网关已经在运行。", Colors.YELLOW))
            print_plain(color(f"  {message}", Colors.DIM))
            print_plain(color("  请先在旧终端按 Ctrl+C 停止，再重新启动。\n", Colors.DIM))
            sys.exit(1)
        raise


_DINGTALK_CREDENTIAL_ENV_VARS = (
    "DINGTALK_CLIENT_ID",
    "DINGTALK_CLIENT_SECRET",
    "DINGTALK_ROBOT_CODE",
)

def _render_dingtalk_login_header() -> None:
    _print_setup_header(
        "M-Claw 钉钉配置",
        "连接钉钉 Stream 网关，需要 client_id、client_secret 和 robot_code。",
    )


def _load_dingtalk_credentials() -> dict[str, str]:
    from mclaw.cli.config import get_env_value

    return {
        env_var: str(get_env_value(env_var) or "").strip()
        for env_var in _DINGTALK_CREDENTIAL_ENV_VARS
    }


def _dingtalk_credentials_configured(credentials: dict[str, str] | None = None) -> bool:
    values = credentials or _load_dingtalk_credentials()
    return all(bool(values.get(env_var)) for env_var in _DINGTALK_CREDENTIAL_ENV_VARS)


def _authorize_dingtalk_credentials() -> None:
    try:
        from mclaw.runtime.secrets import authorize

        authorize("channel:dingtalk", _DINGTALK_CREDENTIAL_ENV_VARS)
    except Exception:
        logging.getLogger(__name__).debug("Failed to authorize DingTalk channel secrets", exc_info=True)


def _prompt_required_text(prompt: str) -> str:
    from mclaw.cli.colors import Colors, color

    try:
        return input(color(prompt, Colors.YELLOW)).strip()
    except (EOFError, KeyboardInterrupt):
        print_plain()
        sys.exit(1)


def _prompt_required_secret(prompt: str) -> str:
    from mclaw.cli.colors import Colors, color

    try:
        return _prompt_secret(color(prompt, Colors.YELLOW)).strip()
    except (EOFError, KeyboardInterrupt):
        print_plain()
        sys.exit(1)


def _run_dingtalk_setup(args, *, optional: bool = False) -> bool:
    """Configure DingTalk Stream Mode credentials through the interactive login flow."""
    from mclaw.cli.colors import Colors, color
    from mclaw.cli.config import ensure_mclaw_home, save_env_value

    ensure_mclaw_home()
    _render_dingtalk_login_header()

    credentials = _load_dingtalk_credentials()
    already_configured = _dingtalk_credentials_configured(credentials)
    if already_configured:
        print_plain(color("  已检测到已保存的钉钉凭据，跳过凭据输入。", Colors.GREEN))
    else:
        _print_setup_step("步骤 1/2  填写应用凭据", f"这些值会保存到 {display_mclaw_path('.env')}，并只授权给钉钉渠道。")
        client_id = credentials.get("DINGTALK_CLIENT_ID", "")
        client_secret = credentials.get("DINGTALK_CLIENT_SECRET", "")
        robot_code = credentials.get("DINGTALK_ROBOT_CODE", "")

        if client_id:
            print_plain(color("  已检测到钉钉 client_id。", Colors.DIM))
        else:
            client_id = _prompt_required_text("  钉钉 client_id: ")

        if client_secret:
            print_plain(color("  已检测到钉钉 client_secret。", Colors.DIM))
        else:
            client_secret = _prompt_required_secret("  钉钉 client_secret: ")

        if robot_code:
            print_plain(color("  已检测到钉钉 robot_code。", Colors.DIM))
        else:
            robot_code = _prompt_required_text("  钉钉 robot_code: ")

        credentials = {
            "DINGTALK_CLIENT_ID": client_id,
            "DINGTALK_CLIENT_SECRET": client_secret,
            "DINGTALK_ROBOT_CODE": robot_code,
        }

        if not _dingtalk_credentials_configured(credentials):
            print_plain(color("\n  钉钉 client_id、client_secret 和 robot_code 均必填。\n", Colors.RED))
            if optional:
                return False
            sys.exit(1)

        for env_var, value in credentials.items():
            save_env_value(env_var, value)
        print_plain(color(f"\n  钉钉凭据已保存到 {display_mclaw_path('.env')}。", Colors.GREEN))

    _authorize_dingtalk_credentials()
    _print_setup_step("步骤 2/2  配置群聊连接", "可添加群聊 open_conversation_id；暂时不用也可以跳过。")
    _run_dingtalk_group_connection_setup(args)

    if already_configured:
        print_plain(color("\n  钉钉凭据已配置。", Colors.GREEN))

    print_plain(color("  下一步: mclaw dingtalk check", Colors.CYAN))
    print_plain(color("  启动:   mclaw dingtalk\n", Colors.CYAN))
    return True


def _run_dingtalk_group_connection_setup(_args=None) -> None:
    from mclaw.cli.colors import Colors, color
    from mclaw.cli.config import load_config

    if not sys.stdin.isatty():
        return

    while True:
        print_plain(color("\n  钉钉群聊连接", Colors.BOLD, Colors.BLUE))
        print_plain(color("  选择是否补充群聊映射。", Colors.DIM))
        print_plain(f"    {color('1', Colors.BOLD)}. 添加群聊连接")
        print_plain(f"    {color('2', Colors.BOLD)}. 查看已连接群聊")
        print_plain(f"    {color('3', Colors.BOLD)}. 跳过")
        try:
            choice = input(color("  请选择 (1/2/3，默认 3 跳过): ", Colors.YELLOW)).strip() or "3"
        except (EOFError, KeyboardInterrupt):
            print_plain()
            return
        if choice == "1":
            try:
                group_name = input(color("  群聊备注名: ", Colors.YELLOW)).strip()
                conversation_id = input(color("  conversation_id: ", Colors.YELLOW)).strip()
                open_conversation_id = input(color("  open_conversation_id: ", Colors.YELLOW)).strip()
            except (EOFError, KeyboardInterrupt):
                print_plain()
                return
            if not conversation_id or not open_conversation_id:
                print_plain(color("  conversation_id 和 open_conversation_id 必填。", Colors.RED))
                continue
            _save_dingtalk_group_connection(conversation_id, open_conversation_id, group_name)
            continue
        if choice == "2":
            config = load_config()
            raw_mapping = (((config.get("channels") or {}).get("dingtalk") or {}).get("open_conversation_map") or {})
            mapping = raw_mapping if isinstance(raw_mapping, dict) else {}
            if not mapping:
                print_plain(color("  暂无已连接群聊。", Colors.DIM))
                continue
            for conversation_id, entry in mapping.items():
                display_name, open_conversation_id = _dingtalk_group_connection_display(conversation_id, entry)
                print_plain(color(f"  {display_name}: {open_conversation_id}", Colors.DIM))
            continue
        if choice == "3":
            return
        print_plain(color("  无效选项。", Colors.RED))


def _dingtalk_group_connection_display(conversation_id: str, entry) -> tuple[str, str]:
    conversation_id = str(conversation_id or "").strip()
    item = entry if isinstance(entry, dict) else {}
    display_name = str(item.get("display_name") or "").strip()
    open_conversation_id = str(item.get("open_conversation_id") or "").strip()
    return display_name or conversation_id or "未命名群聊", open_conversation_id


def _save_dingtalk_group_connection(conversation_id: str, open_conversation_id: str, display_name: str = "") -> None:
    from mclaw.cli.colors import Colors, color
    from mclaw.cli.config import load_config, save_config

    config = load_config()
    channels = config.get("channels")
    if not isinstance(channels, dict):
        channels = {}
        config["channels"] = channels
    dingtalk = channels.get("dingtalk")
    if not isinstance(dingtalk, dict):
        dingtalk = {}
        channels["dingtalk"] = dingtalk
    mapping = dingtalk.get("open_conversation_map")
    if not isinstance(mapping, dict):
        mapping = {}
        dingtalk["open_conversation_map"] = mapping
    for existing_conversation_id, entry in mapping.items():
        _existing_name, existing_open_id = _dingtalk_group_connection_display(existing_conversation_id, entry)
        if existing_open_id == open_conversation_id and existing_conversation_id != conversation_id:
            print_plain(color("  群聊已添加，添加新群聊需要提供新的 open_conversation_id。", Colors.YELLOW))
            return
    mapping[conversation_id] = {
        "display_name": display_name or conversation_id,
        "open_conversation_id": open_conversation_id,
    }
    save_config(config)
    group_label = display_name or conversation_id
    print_plain(color(f"  已保存钉钉群聊连接: {group_label}: {open_conversation_id}", Colors.GREEN))


def _run_dingtalk_check(_args):
    """Print DingTalk channel dependency/config status."""
    import json

    from mclaw.channels.dingtalk.config import DingTalkConfig
    from mclaw.channels.dingtalk.stream_client import check_dingtalk_requirements
    from mclaw.cli.auth import resolve_provider
    from mclaw.cli.colors import Colors, color
    from mclaw.cli.config import ConfigError, load_merged_config

    try:
        config = load_merged_config()
    except ConfigError as exc:
        print_plain(color(f"\n  配置错误: {exc}\n", Colors.RED))
        sys.exit(1)

    requirements = check_dingtalk_requirements()
    dingtalk_config = DingTalkConfig.from_config(config)
    errors = dingtalk_config.validate()
    model = getattr(_args, "model", "") or config.get("model", "")
    provider_name = getattr(_args, "provider", "") or config.get("active_provider", "")
    resolved_provider = resolve_provider(
        model=model,
        provider=provider_name,
        base_url=getattr(_args, "base_url", ""),
        api_key=getattr(_args, "api_key", ""),
        config=config,
    )
    agent_api_key_ok = bool(resolved_provider.get("api_key"))
    agent_model = resolved_provider.get("model") or ""
    agent_provider = resolved_provider.get("provider") or "unresolved"

    required_api_ok = (
        requirements.get("dingtalk_stream")
        and requirements.get("httpx")
        and requirements.get("stream_api")
        and requirements.get("robot_api")
    )
    ready = bool(required_api_ok and agent_api_key_ok and agent_model and not errors)

    report = {
        "ready": ready,
        "requirements": {
            "dingtalk_stream": bool(requirements.get("dingtalk_stream")),
            "httpx": bool(requirements.get("httpx")),
            "stream_api": bool(requirements.get("stream_api")),
            "robot_api": bool(requirements.get("robot_api")),
        },
        "config": {
            "client_id_configured": bool(dingtalk_config.client_id),
            "client_secret_configured": bool(dingtalk_config.client_secret),
            "robot_code_configured": bool(dingtalk_config.robot_code),
            "group_policy": dingtalk_config.group_policy,
            "dm_policy": dingtalk_config.dm_policy,
            "session_scope": dingtalk_config.session_scope,
        },
        "agent": {
            "provider": agent_provider,
            "model": agent_model,
            "api_key_configured": agent_api_key_ok,
            "api_mode": resolved_provider.get("api_mode") or "chat_completions",
        },
        "errors": errors,
    }

    if getattr(_args, "json", False):
        print_plain(json.dumps(report, ensure_ascii=False, indent=2))
        if getattr(_args, "strict", False) and not ready:
            sys.exit(1)
        return

    print_plain(color("\n  钉钉渠道检查", Colors.BOLD))
    print_plain(color(f"  dingtalk-stream: {'OK' if requirements['dingtalk_stream'] else 'missing'}", Colors.GREEN if requirements["dingtalk_stream"] else Colors.RED))
    print_plain(color(f"  httpx:           {'OK' if requirements['httpx'] else 'missing'}", Colors.GREEN if requirements["httpx"] else Colors.RED))
    print_plain(color(f"  Stream API:      {'OK' if requirements.get('stream_api') else 'incomplete'}", Colors.GREEN if requirements.get("stream_api") else Colors.RED))
    print_plain(color(f"  Robot API:       {'OK' if requirements.get('robot_api') else 'incomplete'}", Colors.GREEN if requirements.get("robot_api") else Colors.YELLOW))
    print_plain(color(f"  client_id:       {'configured' if dingtalk_config.client_id else 'missing'}", Colors.GREEN if dingtalk_config.client_id else Colors.RED))
    print_plain(color(f"  client_secret:   {'configured' if dingtalk_config.client_secret else 'missing'}", Colors.GREEN if dingtalk_config.client_secret else Colors.RED))
    print_plain(color(f"  group_policy:    {dingtalk_config.group_policy}", Colors.DIM))
    print_plain(color(f"  agent_provider:  {agent_provider}", Colors.GREEN if agent_api_key_ok else Colors.RED))
    print_plain(color(f"  agent_model:     {agent_model or 'missing'}", Colors.DIM if agent_model else Colors.RED))
    print_plain(color(f"  agent_api_key:   {'configured' if agent_api_key_ok else 'missing'}", Colors.GREEN if agent_api_key_ok else Colors.RED))
    print_plain(color(f"  ready:           {'yes' if ready else 'no'}", Colors.GREEN if ready else Colors.RED))
    if errors:
        print_plain(color("\n  配置问题:", Colors.RED))
        for error in errors:
            print_plain(color(f"  - {error}", Colors.DIM))
    if not requirements["dingtalk_stream"]:
        print_plain(color("\n  安装依赖: pip install -e .", Colors.DIM))
    if not requirements.get("robot_api"):
        print_plain(color("\n  Robot API incomplete: 媒体下载和 Thinking/Done reaction 不可用。", Colors.DIM))
    if not agent_api_key_ok:
        print_plain(color("  Agent provider missing: 请先运行 mclaw setup 配置模型供应商 API key。", Colors.DIM))
    if agent_api_key_ok and not agent_model:
        print_plain(color("  Agent model missing: 请先运行 mclaw setup 配置默认模型。", Colors.DIM))
    print_plain()
    if getattr(_args, "strict", False) and not ready:
        sys.exit(1)


def _run_setup(args):
    try:
        return _run_setup_impl(args)
    except _SetupCancelled:
        _print_setup_cancelled(_SETUP_CONFIGURED_COUNT)
        sys.exit(0)


def _run_setup_impl(args):
    """Run interactive setup wizard with provider-first flow."""
    import sys
    from mclaw.cli.auth import PROVIDER_REGISTRY
    from mclaw.cli.config import (
        ensure_mclaw_home, load_config, save_config, upsert_fallback_provider_model,
    )
    from mclaw.cli.colors import Colors, color
    from mclaw.cli.model_resolver import resolve_provider_key
    from mclaw.cli.provider_profiles import CORE_PROVIDER_KEYS
    from mclaw.cli.tui.console import print_plain

    ensure_mclaw_home()
    config = load_config()

    intro_paused = _print_setup_intro()

    global _SETUP_CONFIGURED_COUNT
    _SETUP_CONFIGURED_COUNT = 0
    configured: dict = {}
    common_providers = [p for p in CORE_PROVIDER_KEYS if p in PROVIDER_REGISTRY]
    provider_step_leading_blank = not intro_paused

    while True:
        _print_setup_step(
            "步骤 1/3  选择大模型接入方",
            "可输入编号、接入方名称，或选择自定义兼容接口。",
            leading_blank=provider_step_leading_blank,
        )
        provider_step_leading_blank = True
        for idx, provider_key in enumerate(common_providers, 1):
            pcfg = PROVIDER_REGISTRY[provider_key]
            print_plain(f"    {_setup_index(idx)}. {pcfg.display_name} {color(f'({provider_key})', Colors.DIM)}")
        search_more_idx = len(common_providers) + 1
        custom_openai_idx = len(common_providers) + 2
        custom_anthropic_idx = len(common_providers) + 3
        print_plain(f"    {_setup_index(search_more_idx)}. {color('更多已知接入方', Colors.CYAN)} {color('按名称匹配', Colors.DIM)}")
        print_plain(f"    {_setup_index(custom_openai_idx)}. {color('自定义 OpenAI 兼容接口', Colors.CYAN)}")
        print_plain(f"    {_setup_index(custom_anthropic_idx)}. {color('自定义 Anthropic 兼容接口', Colors.CYAN)}")
        print_plain()

        choice = _setup_input("  输入编号或接入方 [1]: ")

        choice = choice or "1"
        result = None
        if choice.isdigit() and int(choice) == custom_openai_idx:
            result = _setup_custom_endpoint(config, api_format="openai", provider_key="custom")
        elif choice.isdigit() and int(choice) == custom_anthropic_idx:
            result = _setup_custom_endpoint(config, api_format="anthropic", provider_key="custom_anthropic")
        else:
            provider_key = ""
            if choice.isdigit() and int(choice) == search_more_idx:
                provider_query = _setup_input("  输入接入方名称或 provider key: ")
                provider_key = resolve_provider_key(provider_query)
                if not provider_key or provider_key not in PROVIDER_REGISTRY:
                    models_dev_provider = _select_models_dev_provider_from_query(provider_query)
                    if models_dev_provider:
                        result = _setup_models_dev_provider(config, models_dev_provider)
                        provider_key = ""
            elif choice.isdigit():
                idx = int(choice) - 1
                if 0 <= idx < len(common_providers):
                    provider_key = common_providers[idx]
            else:
                provider_key = resolve_provider_key(choice)

            if result is None and (not provider_key or provider_key not in PROVIDER_REGISTRY):
                print_plain(color("\n  未识别这个接入方。请重新输入编号或接入方名称。", Colors.RED))
                print_plain(color("  如果它是私有部署或网关服务，请选择自定义兼容接口。", Colors.DIM))
                print_plain(color("  如果不确定官方 provider 名称，请到对应大模型平台官网查看接入说明。", Colors.DIM))
                _pause_setup_warning()
                continue

            if result is None:
                pcfg = PROVIDER_REGISTRY[provider_key]
                profile = _select_setup_profile(provider_key)
                if profile is None:
                    continue
                result = _setup_api_key_provider(
                    config,
                    provider_key,
                    pcfg.api_key_env_vars[0],
                    pcfg.display_name,
                    pcfg.key_url,
                    profile_id=profile.id,
                )

        if result:
            configured[result["provider"]] = result
            upsert_fallback_provider_model(config, result.get("provider", ""), result.get("model", ""))
            save_config(config)
            _SETUP_CONFIGURED_COUNT = len(configured)

        print_plain()
        more = _setup_input("  是否添加更多接入方？[Y/N]: ").lower()
        if more != "y":
            break

    if not configured:
        print_plain(color("\n  未配置任何接入方，退出。\n", Colors.RED))
        sys.exit(1)

    # Select the default model.
    if len(configured) == 1:
        default = next(iter(configured.values()))
        _print_setup_step("步骤 2/3  确认默认模型", "只配置了一个接入方，已自动设为默认。")
        print_plain(
            _setup_label("默认模型")
            + color(default["model"], Colors.GREEN)
            + color(f" ({default['provider']})", Colors.DIM)
        )
    else:
        _print_setup_step("步骤 2/3  选择默认模型", "多个接入方已配置，选择启动时默认使用的模型。")
        items = list(configured.items())
        for i, (pname, pdat) in enumerate(items):
            pcfg = PROVIDER_REGISTRY.get(pname)
            pdisplay = pcfg.display_name if pcfg else pname
            print_plain(f"    {_setup_index(i + 1)}. {color(pdat['model'], Colors.GREEN)} {color(f'({pdisplay})', Colors.DIM)}")
        print_plain()
        def_choice = _setup_input("  输入编号 [1]: ")
        def_idx = int(def_choice) - 1 if def_choice.isdigit() else 0
        def_idx = max(0, min(def_idx, len(items) - 1))
        default = items[def_idx][1]

    config["model"] = default["model"]
    config["active_provider"] = default["provider"]
    if default.get("profile"):
        config["active_provider_profile"] = default["profile"]
    for result in configured.values():
        upsert_fallback_provider_model(config, result.get("provider", ""), result.get("model", ""))
    save_config(config)
    _print_setup_step("步骤 3/3  配置可选能力", "可启用网页搜索、视觉分析、浏览器自动化、IM 渠道和语音输入。")
    _run_setup_capability_selection(config)
    save_config(config)
    _run_setup_builtin_skill_selection()

    print_plain(color("\n  设置完成。", Colors.GREEN, Colors.BOLD))
    print_plain(color("  启动: mclaw\n", Colors.CYAN))


def _run_setup_weixin_login() -> bool:
    from types import SimpleNamespace

    args = SimpleNamespace(bot_type="3", timeout=480, no_qr=False, connect=False)
    try:
        return _run_weixin_login(args, optional=True, skip_existing=True)
    except SystemExit as exc:
        return bool(exc.code == 0)


def _run_setup_dingtalk_login() -> bool:
    from types import SimpleNamespace

    try:
        return _run_dingtalk_setup(SimpleNamespace(), optional=True)
    except SystemExit as exc:
        return bool(exc.code == 0)


def _merge_channel_config_from_disk(config: dict, channel_name: str, keys: tuple[str, ...]) -> None:
    from mclaw.cli.config import load_config

    try:
        disk_config = load_config()
    except Exception:
        return
    channels = disk_config.get("channels") if isinstance(disk_config, dict) else {}
    disk_channel = channels.get(channel_name) if isinstance(channels, dict) else {}
    if not isinstance(disk_channel, dict):
        return

    target_channels = config.setdefault("channels", {})
    if not isinstance(target_channels, dict):
        target_channels = {}
        config["channels"] = target_channels
    target_channel = target_channels.setdefault(channel_name, {})
    if not isinstance(target_channel, dict):
        target_channel = {}
        target_channels[channel_name] = target_channel

    for key in keys:
        if key in disk_channel:
            target_channel[key] = disk_channel[key]


def _run_setup_capability_selection(config: dict) -> None:
    """Select optional toolsets, channels, and ASR mode during setup."""
    from mclaw.cli.colors import Colors, color
    from mclaw.cli.tui.console import print_plain
    from mclaw.cli.tui.selection_prompt import prompt_multi_select
    from mclaw.runtime.features import get_feature
    from mclaw.runtime.manager import RuntimeManager

    runtime = RuntimeManager.current(config)

    web_feature = get_feature("web_search")
    vision_feature = get_feature("vision_analyze")
    asr_feature = get_feature("asr")
    capability_items = [
        {
            "id": "web",
            "label": web_feature.display_name if web_feature else "网页搜索",
            "description": web_feature.description if web_feature else "优先通过 Tavily 搜索网页，可选 DashScope/Qwen 作为第二搜索源。",
        },
        {
            "id": "vision",
            "label": vision_feature.display_name if vision_feature else "视觉分析",
            "description": vision_feature.description if vision_feature else "通过 Qwen 视觉模型分析图片。",
        },
    ]
    if runtime.features.toolset_enabled("browser"):
        capability_items.append(
            {"id": "browser", "label": "浏览器自动化", "description": "使用本地浏览器运行时执行网页操作。"}
        )
    channel_items = [
        {
            "id": "weixin",
            "label": "微信私聊网关",
            "description": "",
        },
        {
            "id": "dingtalk",
            "label": "钉钉私聊/群聊网关",
            "description": "",
        },
    ]
    current_toolsets = [str(item) for item in config.get("toolsets", []) if str(item).strip()]
    default_optional = [
        item
        for item in current_toolsets
        if item in {"web", "vision", "browser"} and runtime.features.toolset_enabled(item)
    ]
    default_channels = [item for item in current_toolsets if item in {"weixin", "dingtalk"}]

    try:
        optional = prompt_multi_select(
            "M-CLAW 可选工具配置",
            capability_items,
            hint="选择需要启用的可选工具；必需工具始终启用。",
            default_selected=default_optional,
        )
        channels = prompt_multi_select(
            "M-Claw IM交互配置",
            channel_items,
            hint="选择需要接入的消息渠道；选中后进入对应登录流程。",
            default_selected=default_channels,
        )
        asr_choice = prompt_multi_select(
            "M-Claw 语音输入",
            [
                {
                    "id": "asr",
                    "label": asr_feature.display_name if asr_feature else "语音输入",
                    "description": asr_feature.description if asr_feature else "需要语音识别凭据。",
                }
            ],
            hint="仅在需要麦克风语音输入时启用。",
            default_selected=["asr"] if config.get("auxiliary", {}).get("asr", {}).get("enabled") is True else [],
        )
    except (EOFError, KeyboardInterrupt):
        print_plain()
        raise _SetupCancelled

    feature_by_toolset = {"web": "web_search", "vision": "vision_analyze"}
    enabled_optional: list[str] = []
    for name in optional:
        feature_name = feature_by_toolset.get(name)
        if feature_name == "web_search":
            if not _setup_configure_web_search_keys(config, print_plain=print_plain, color=color, Colors=Colors):
                continue
        elif feature_name and not _setup_configure_feature_keys(feature_name, print_plain=print_plain, color=color, Colors=Colors):
            continue
        elif feature_name == "vision_analyze":
            _ensure_vision_config(config)
        if name not in enabled_optional:
            enabled_optional.append(name)

    enabled_channels: list[str] = []
    for name in channels:
        if name == "weixin":
            if not _run_setup_weixin_login():
                print_plain(color("  已跳过微信私聊网关。", Colors.DIM))
                continue
        elif name == "dingtalk":
            if not _run_setup_dingtalk_login():
                print_plain(color("  已跳过钉钉 Stream 网关。", Colors.DIM))
                continue
            _merge_channel_config_from_disk(config, "dingtalk", ("open_conversation_map",))
        elif not _setup_configure_feature_keys(name, print_plain=print_plain, color=color, Colors=Colors):
            continue
        if name not in enabled_channels:
            enabled_channels.append(name)

    asr_enabled = False
    if "asr" in asr_choice:
        asr_enabled = _setup_configure_feature_keys("asr", print_plain=print_plain, color=color, Colors=Colors)

    selected_toolsets = ["mclaw-required"]
    for name in [*enabled_optional, *enabled_channels]:
        if runtime.features.toolset_enabled(name) and name not in selected_toolsets:
            selected_toolsets.append(name)
    config["toolsets"] = selected_toolsets

    channels_cfg = config.setdefault("channels", {})
    if isinstance(channels_cfg, dict):
        for name in ("weixin", "dingtalk"):
            section = channels_cfg.setdefault(name, {})
            if isinstance(section, dict):
                section["enabled"] = name in enabled_channels
                channel_toolsets = ["mclaw-required", *enabled_optional]
                if name in enabled_channels:
                    channel_toolsets.append(name)
                section["toolsets"] = list(dict.fromkeys(channel_toolsets))

    auxiliary = config.setdefault("auxiliary", {})
    if isinstance(auxiliary, dict):
        asr = auxiliary.setdefault("asr", {})
        if isinstance(asr, dict):
            asr["enabled"] = True if asr_enabled else (False if "asr" in asr_choice else "auto")

    print_plain(color(f"\n  已启用工具集: {', '.join(selected_toolsets)}", Colors.DIM))


def _setup_configure_web_search_keys(config: dict, *, print_plain, color, Colors) -> bool:
    """Configure web_search with Tavily as the preferred source."""
    from mclaw.cli import config as cli_config
    from mclaw.runtime.secrets import authorize

    required_for = "tool:web_search"

    def _configured(name: str) -> bool:
        return bool(str(cli_config.get_env_value(name) or "").strip())

    def _existing_dashscope_env() -> str:
        if _configured("DASHSCOPE_API_KEY"):
            return "DASHSCOPE_API_KEY"
        if _configured("QWEN_API_KEY"):
            return "QWEN_API_KEY"
        return ""

    def _ensure_web_config() -> None:
        auxiliary = config.setdefault("auxiliary", {})
        if not isinstance(auxiliary, dict):
            auxiliary = {}
            config["auxiliary"] = auxiliary
        web_cfg = auxiliary.setdefault("web_search", {})
        if not isinstance(web_cfg, dict):
            web_cfg = {}
            auxiliary["web_search"] = web_cfg
        web_cfg["backend"] = "auto"
        web_cfg.setdefault("tavily_timeout", 30)
        web_cfg.setdefault("dashscope_timeout", 90)
        web_cfg.setdefault("dashscope_deep_timeout", 120)
        web_cfg.setdefault("fallback", True)

    tavily_ready = _configured("TAVILY_API_KEY")
    dashscope_env = _existing_dashscope_env()

    if tavily_ready:
        authorize(required_for, ["TAVILY_API_KEY"])
        print_plain(color("  网页搜索: 已检测到 Tavily API key，将作为首选搜索源。", Colors.DIM))
    else:
        print_plain(color("\n  网页搜索首选 Tavily。按 Enter 可跳过 Tavily。", Colors.YELLOW))
        print_plain(color("  获取 Tavily API key: https://www.tavily.com/", Colors.DIM))
        values = _prompt_secret_batch(
            ["TAVILY_API_KEY"],
            prompt=color("  Tavily API Key: ", Colors.YELLOW),
            print_plain=print_plain,
            color=color,
            Colors=Colors,
        )
        if values and values.get("TAVILY_API_KEY"):
            cli_config.save_env_value("TAVILY_API_KEY", values["TAVILY_API_KEY"])
            authorize(required_for, ["TAVILY_API_KEY"])
            tavily_ready = True
            print_plain(color("  网页搜索: Tavily 凭据已保存并授权。", Colors.DIM))
        else:
            print_plain(color("  已跳过 Tavily 首选搜索源。", Colors.DIM))

    if dashscope_env:
        authorize(required_for, [dashscope_env])
        print_plain(color(f"  网页搜索: 已检测到 {dashscope_env}，将作为第二搜索源。", Colors.DIM))
    elif tavily_ready:
        choice = _setup_input("  是否配置 Qwen/DashScope API 作为第二搜索源? [y/N]: ").lower()
        if choice == "y":
            print_plain(color("  DashScope API key 可在 https://dashscope.console.aliyun.com/apiKey 获取。", Colors.DIM))
            values = _prompt_secret_batch(
                ["DASHSCOPE_API_KEY"],
                prompt=color("  DashScope API Key: ", Colors.YELLOW),
                print_plain=print_plain,
                color=color,
                Colors=Colors,
            )
            if values and values.get("DASHSCOPE_API_KEY"):
                cli_config.save_env_value("DASHSCOPE_API_KEY", values["DASHSCOPE_API_KEY"])
                authorize(required_for, ["DASHSCOPE_API_KEY"])
                dashscope_env = "DASHSCOPE_API_KEY"
                print_plain(color("  网页搜索: DashScope 第二搜索源已保存并授权。", Colors.DIM))
            else:
                print_plain(color("  已跳过 DashScope 第二搜索源。", Colors.DIM))

    if not tavily_ready and not dashscope_env:
        print_plain(color("  已跳过网页搜索，未配置 Tavily 或 DashScope/Qwen 凭据。", Colors.DIM))
        return False

    _ensure_web_config()
    return True


def _ensure_vision_config(config: dict) -> None:
    auxiliary = config.setdefault("auxiliary", {})
    if not isinstance(auxiliary, dict):
        auxiliary = {}
        config["auxiliary"] = auxiliary
    vision_cfg = auxiliary.setdefault("vision", {})
    if not isinstance(vision_cfg, dict):
        vision_cfg = {}
        auxiliary["vision"] = vision_cfg
    provider = str(vision_cfg.get("provider") or "").strip().lower()
    if provider in {"", "auto", "dashscope"}:
        vision_cfg["provider"] = "qwen"
    vision_cfg.setdefault("model", "qwen-vl-max")
    if not str(vision_cfg.get("model") or "").strip():
        vision_cfg["model"] = "qwen-vl-max"
    vision_cfg.setdefault("base_url", "")
    vision_cfg.setdefault("timeout", 30)
    vision_cfg.setdefault("download_timeout", 30)


def _setup_configure_feature_keys(feature_name: str, *, print_plain, color, Colors) -> bool:
    """Ensure setup-selected built-in features have scoped credentials."""
    from mclaw.cli import config as cli_config
    from mclaw.runtime.features import configured_env_vars, get_feature, secret_requests_for_feature
    from mclaw.runtime.secrets import authorize

    spec = get_feature(feature_name)
    if spec is None:
        return True

    existing = configured_env_vars(spec, cli_config.get_env_value)
    if existing:
        authorize(spec.required_for, existing)
        print_plain(color(f"  {spec.display_name or spec.name}: 已检测到 {display_mclaw_path('.env')} 中的凭据。", Colors.DIM))
        return True

    requests = secret_requests_for_feature(spec, cli_config.get_env_value)
    if not requests:
        return True

    print_plain(
        color(
            f"\n  {spec.display_name or spec.name} 需要 {display_mclaw_path('.env')} 中的凭据。按 Enter 跳过此功能。",
            Colors.YELLOW,
        )
    )
    requested_env_vars = [request["env_var"] for request in requests if request.get("env_var")]
    values = _prompt_secret_batch(
        requested_env_vars,
        prompt=color(
            f"  {requested_env_vars[0] if len(requested_env_vars) == 1 else '凭据'}: ",
            Colors.YELLOW,
        ),
        print_plain=print_plain,
        color=color,
        Colors=Colors,
    )
    if not values:
        print_plain(color(f"  已跳过 {spec.display_name or spec.name}，不会启用。", Colors.DIM))
        return False

    for env_var, value in values.items():
        cli_config.save_env_value(env_var, value)

    configured = configured_env_vars(spec, cli_config.get_env_value) or list(values)
    authorize(spec.required_for, configured)
    print_plain(color(f"  {spec.display_name or spec.name}: 凭据已保存并授权。", Colors.DIM))
    return True


def _run_setup_builtin_skill_selection() -> None:
    """Install the built-in Skills selected during setup."""
    from mclaw.cli.colors import Colors, color
    from mclaw.cli.tui.console import print_plain
    from mclaw.cli.tui.selection_prompt import prompt_builtin_skill_selection
    from mclaw.skills_hub.builtin_sync import (
        get_builtin_skills_dir,
        list_builtin_skills,
        sync_selected_skills,
    )

    skills = list_builtin_skills()
    if not skills:
        source_dir = get_builtin_skills_dir()
        print_plain(color("\n  未发现可导入的内置 Skill，已跳过内置 Skill 导入。", Colors.YELLOW))
        print_plain(color(f"  查找路径: {source_dir}", Colors.DIM))
        print_plain(
            color(
                "  如果这是 Kaihong/source 模式，请确认 mclaw/skills 已同步，"
                "或设置 MCLAW_BUILTIN_SKILLS 指向内置 Skill 目录。\n",
                Colors.DIM,
            )
        )
        return

    try:
        selected = prompt_builtin_skill_selection(skills)
    except (EOFError, KeyboardInterrupt):
        print_plain()
        raise _SetupCancelled

    if not selected:
        print_plain(color("\n  未启用内置 Skill。之后仍可用 /skill install 安装外部 Skill。\n", Colors.DIM))
        return

    result = sync_selected_skills(selected, quiet=True)
    copied = result.get("copied", [])
    updated = result.get("updated", [])
    skipped = result.get("skipped", 0)
    user_modified = result.get("user_modified", [])
    summary_parts = []
    if copied:
        summary_parts.append(f"新增 {len(copied)}")
    if updated:
        summary_parts.append(f"更新 {len(updated)}")
    if skipped:
        summary_parts.append(f"跳过 {skipped}")
    if user_modified:
        summary_parts.append(f"保留已有 {len(user_modified)}")
    summary = "，".join(summary_parts) if summary_parts else "无变化"
    print_plain(color(f"\n  内置 Skill 已处理：{summary}。\n", Colors.GREEN))


def _pause_setup_warning():
    """Keep setup warnings visible before the provider menu is redrawn."""
    _setup_input("\n  按 Enter 继续...", dim=True)


def _setup_configured_model(config: dict, provider_key: str) -> str:
    """Return the model the user previously configured for this provider."""
    provider_key = str(provider_key or "").strip()
    if not provider_key or not isinstance(config, dict):
        return ""
    if str(config.get("active_provider") or "").strip() == provider_key:
        active_model = str(config.get("model") or "").strip()
        if active_model:
            return active_model
    fallback_entries = config.get("fallback_providers")
    if not isinstance(fallback_entries, list):
        return ""
    for entry in fallback_entries:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("provider") or "").strip() == provider_key:
            return str(entry.get("model") or "").strip()
    return ""


def _select_setup_profile(provider_key: str):
    """Select a callable provider profile for setup."""
    from mclaw.cli.colors import Colors, color
    from mclaw.cli.provider_profiles import find_provider_profile, get_default_provider_profile, get_provider_profiles

    profiles = get_provider_profiles(provider_key)
    if len(profiles) <= 1:
        return profiles[0]

    default_profile = get_default_provider_profile(provider_key)
    print_plain(color("\n  选择接口类型", Colors.BOLD, Colors.BLUE))
    for idx, profile in enumerate(profiles, 1):
        marker = color(" *", Colors.BOLD, Colors.CYAN) if profile.id == default_profile.id else ""
        status_color = Colors.GREEN if profile.callable else Colors.YELLOW
        status = color(profile.status_label, status_color)
        print_plain(
            f"    {_setup_index(idx)}. {profile.label} "
            f"{color(f'({profile.id})', Colors.DIM)}  {status}{marker}"
        )
        if profile.note:
            print_plain(color(f"       {profile.note}", Colors.DIM))
    print_plain()
    choice = _setup_input(f"  输入接口编号或名称 [{default_profile.id}]: ")
    selected = default_profile
    if choice:
        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(profiles):
                selected = profiles[idx]
            else:
                print_plain(color("\n  无效接口编号。可用接口如下：", Colors.RED))
                for profile in profiles:
                    print_plain(color(f"  - {profile.id}: {profile.label}", Colors.DIM))
                _pause_setup_warning()
                return None
        else:
            selected = find_provider_profile(provider_key, choice)
            if selected is None:
                print_plain(color(f"\n  未识别接口类型: {choice}", Colors.RED))
                print_plain(color("  可用接口：", Colors.DIM))
                for profile in profiles:
                    print_plain(color(f"  - {profile.id}: {profile.label}", Colors.DIM))
                _pause_setup_warning()
                return None

    if not selected.callable:
        print_plain(color("\n  这个接口类型当前只用于模型库展示，不能直接配置为 M-Claw 调用接口。", Colors.RED))
        if selected.note:
            print_plain(color(f"  {selected.note}", Colors.DIM))
        print_plain(color("  如需私有部署、网关或特殊套餐，请选择自定义兼容接口。", Colors.DIM))
        _pause_setup_warning()
        return None
    return selected


def _select_setup_model(
    provider_key: str,
    config: dict,
    profile_id: str = "",
) -> str:
    """Select a model for setup from models.dev/cache, with manual fallback."""
    from mclaw.agent import models_dev
    from mclaw.cli.auth import DEFAULT_PROVIDER_MODELS
    from mclaw.cli.colors import Colors, color
    from mclaw.cli.provider_profiles import get_provider_profile

    fallback_model = _setup_configured_model(config, provider_key)
    profile = get_provider_profile(provider_key, profile_id)
    models = models_dev.list_provider_models(provider_key, limit=20, profile_id=profile.id)
    source = f"models.dev 模型库 / {profile.models_dev_provider}"
    if not models:
        models = DEFAULT_PROVIDER_MODELS.get(provider_key, [])[:20]
        source = "本地默认候选"

    if fallback_model and fallback_model not in models:
        models = [fallback_model, *models]
    models = list(dict.fromkeys(models))[:20]

    if models:
        print_plain(color("  可选模型", Colors.BOLD, Colors.BLUE) + color(f"  来自 {source}", Colors.DIM))
        for idx, model in enumerate(models, 1):
            marker = color(" *", Colors.BOLD, Colors.CYAN) if fallback_model and model == fallback_model else ""
            print_plain(f"    {_setup_index(idx)}. {model}{marker}")
        other_idx = len(models) + 1
        print_plain(f"    {_setup_index(other_idx)}. {color('其他模型名称', Colors.CYAN)}")
        print_plain()
        default_prompt = fallback_model or models[0]
        model_input = _setup_input(f"  输入模型编号或名称 [{default_prompt}]: ")
        if not model_input:
            return default_prompt
        if model_input.isdigit():
            idx = int(model_input)
            if 1 <= idx <= len(models):
                return models[idx - 1]
            if idx == other_idx:
                return _setup_input("  输入其他模型名称: ")
        return model_input

    prompt = f"  模型名称 [{fallback_model}]: " if fallback_model else "  模型名称: "
    return _setup_input(prompt) or fallback_model


def _select_models_dev_provider_from_query(query: str) -> str:
    """Resolve a non-front-door provider name through the local models.dev cache."""
    from mclaw.agent import models_dev
    from mclaw.cli.colors import Colors, color
    from mclaw.cli.provider_profiles import search_models_dev_provider_ids

    provider_ids = models_dev.list_models_dev_provider_ids()
    matches = search_models_dev_provider_ids(query, provider_ids, limit=10)
    if not matches:
        return ""

    print_plain(color("\n  在 models.dev 模型库中找到这些 provider", Colors.BOLD, Colors.BLUE))
    for idx, provider_id in enumerate(matches, 1):
        models = models_dev.list_models_dev_provider(provider_id, limit=3)
        preview = ", ".join(models[:3]) if models else "暂无模型列表"
        print_plain(f"    {_setup_index(idx)}. {color(provider_id, Colors.CYAN)}  {color(preview, Colors.DIM)}")
    print_plain(f"    {_setup_index(len(matches) + 1)}. {color('都不是，返回接入方列表', Colors.CYAN)}")
    print_plain()
    choice = _setup_input("  选择 provider 编号 [1]: ")
    if not choice:
        return matches[0]
    if choice.isdigit():
        idx = int(choice) - 1
        if 0 <= idx < len(matches):
            return matches[idx]
    return ""


def _setup_models_dev_provider(config: dict, models_dev_provider: str):
    """Configure a provider found only through models.dev via a custom endpoint."""
    from mclaw.agent import models_dev
    from mclaw.cli.colors import Colors, color

    models = models_dev.list_models_dev_provider(models_dev_provider, limit=20)
    print_plain(_setup_label("模型库") + color(models_dev_provider, Colors.GREEN))
    if models:
        print_plain(color("  模型库模型", Colors.BOLD, Colors.BLUE))
        for idx, model in enumerate(models, 1):
            print_plain(f"    {_setup_index(idx)}. {model}")
    else:
        print_plain(color("  当前缓存没有模型列表，后续可手动输入模型名。", Colors.DIM))
    print_plain(color("\n  这个 provider 不是 M-Claw 前置维护接入方，需要通过自定义兼容接口调用。", Colors.DIM))
    print_plain(color("  如果官方接口不是 OpenAI/Anthropic 兼容格式，请以官方文档为准。", Colors.DIM))
    print_plain()

    api_format = _setup_input("  API 格式: 1.OpenAI 兼容  2.Anthropic 兼容 [1]: ")
    fmt = "anthropic" if api_format == "2" else "openai"
    provider_key = "custom_anthropic" if fmt == "anthropic" else "custom"
    suggested_model = models[0] if models else ""
    result = _setup_custom_endpoint(
        config,
        api_format=fmt,
        provider_key=provider_key,
        suggested_model=suggested_model,
        candidate_models=models,
        source_label=f"models.dev / {models_dev_provider}",
    )
    return result


def _setup_custom_endpoint(
    config: dict,
    api_format: str = "openai",
    provider_key: str = "custom",
    suggested_model: str = "",
    candidate_models: list[str] | None = None,
    source_label: str = "",
):
    """Setup flow for custom endpoint (OpenAI or Anthropic compatible)."""
    from mclaw.cli.config import save_env_value, get_env_value, save_config
    from mclaw.cli.colors import Colors, color
    from mclaw.cli.auth import PROVIDER_REGISTRY

    is_anthropic = api_format == "anthropic"
    format_label = "Anthropic" if is_anthropic else "OpenAI"
    url_env = "MCLAW_ANTHROPIC_BASE_URL" if is_anthropic else "MCLAW_BASE_URL"
    key_env = "MCLAW_ANTHROPIC_API_KEY" if is_anthropic else "MCLAW_API_KEY"
    fallback_key_env = "ANTHROPIC_API_KEY" if is_anthropic else "OPENAI_API_KEY"

    current_url = get_env_value(url_env) or ""
    current_key = get_env_value(key_env) or get_env_value(fallback_key_env) or ""

    print_plain(_setup_label("API 格式") + color(f"{format_label} Messages API", Colors.DIM))
    if is_anthropic:
        print_plain(color("  使用 x-api-key 请求头和 /v1/messages 接口", Colors.DIM))
    print_plain()

    example = "https://api.example.com" if is_anthropic else "https://api.example.com/v1"
    hint = f" [{current_url}]" if current_url else f" [例如 {example}]"
    base_url = _setup_input(f"  API 接口地址{hint}: ")
    if not base_url:
        base_url = current_url
    if not base_url:
        print_plain(color("  自定义接口必须提供接口地址。", Colors.RED))
        return None
    base_url = base_url.rstrip("/")

    matched_provider = _match_builtin_provider_by_base_url(base_url, is_anthropic=is_anthropic)
    if matched_provider:
        pcfg = PROVIDER_REGISTRY[matched_provider]
        print_plain()
        print_plain(color(f"  检测到这个接口地址属于已知接入方：{pcfg.display_name} ({matched_provider})", Colors.YELLOW))
        print_plain(color("  建议使用预置接入方，这样 /provider、/model 和模型库识别会保持一致。", Colors.DIM))
        use_builtin = _setup_input(f"  改用 {pcfg.display_name} 接入方? [Y/n]: ").lower()
        if use_builtin not in {"n", "no"}:
            profile = _select_setup_profile(matched_provider)
            if profile is None:
                return None
            return _setup_api_key_provider(
                config,
                matched_provider,
                pcfg.api_key_env_vars[0],
                pcfg.display_name,
                pcfg.key_url,
                profile_id=profile.id,
            )
        print_plain(color("  继续按自定义接口保存。", Colors.DIM))

    key_hint = f" [{current_key[:8]}...]" if current_key else " [无需密钥可直接回车]"
    api_key = _setup_secret(f"  API 密钥{key_hint}: ")
    if not api_key:
        api_key = current_key

    print_plain(color("\n  正在测试连接...", Colors.DIM), end="", flush=True)
    if is_anthropic:
        ok = _probe_anthropic(base_url, api_key)
        if ok:
            print_plain(color(" 已连接", Colors.GREEN))
        else:
            print_plain(color(" 无法验证（将继续配置）", Colors.YELLOW))
        models = None
    else:
        models = _probe_models(base_url, api_key)
        if models is None:
            print_plain(color(" 连接失败（将继续配置）", Colors.YELLOW))
        elif len(models) == 0:
            print_plain(color(" 已连接，未发现模型列表", Colors.YELLOW))
        else:
            print_plain(color(f" 发现 {len(models)} 个模型", Colors.GREEN))
            print_plain()
            display_models = models[:20]
            for i, m in enumerate(display_models):
                print_plain(f"    {_setup_index(i + 1)}. {m}")
            if len(models) > 20:
                print_plain(color(f"    ... 还有 {len(models) - 20} 个", Colors.DIM))
            print_plain()

    current_model = str(config.get("model") or "").strip() if str(config.get("active_provider") or "").strip() == provider_key else ""
    selectable_models = models or candidate_models or []
    if selectable_models:
        preferred = suggested_model or current_model
        default_idx = 0
        if preferred:
            for idx, m in enumerate(selectable_models):
                if m.lower() == preferred.lower():
                    default_idx = idx
                    break
        display_models = selectable_models[:20]
        if not models and source_label:
            print_plain(color("  可选模型", Colors.BOLD, Colors.BLUE) + color(f"  来自 {source_label}", Colors.DIM))
        for i, m in enumerate(display_models):
            marker = color(" *", Colors.BOLD, Colors.CYAN) if i == default_idx else ""
            print_plain(f"    {_setup_index(i + 1)}. {m}{marker}")
        other_idx = len(display_models) + 1
        print_plain(f"    {_setup_index(other_idx)}. {color('其他模型名称', Colors.CYAN)}")
        if len(selectable_models) > 20:
            print_plain(color(f"    ... 还有 {len(selectable_models) - 20} 个", Colors.DIM))
        print_plain()
        model_input = _setup_input(f"  输入模型编号或名称 [{selectable_models[default_idx]}]: ")
        if not model_input:
            model_name = selectable_models[default_idx]
        elif model_input.isdigit():
            idx = int(model_input)
            if 1 <= idx <= len(display_models):
                model_name = display_models[idx - 1]
            elif idx == other_idx:
                model_name = _setup_input("  输入其他模型名称: ")
                if not model_name:
                    model_name = selectable_models[default_idx]
            else:
                model_name = model_input
        else:
            model_name = model_input
    else:
        model_hint = suggested_model or current_model
        prompt = f"  模型名称 [{model_hint}]: " if model_hint else "  模型名称: "
        model_name = _setup_input(prompt)
        if not model_name:
            model_name = suggested_model or current_model
        if not model_name:
            print_plain(color("  自定义接口必须提供模型名称。", Colors.RED))
            return None

    save_env_value(url_env, base_url)
    if api_key:
        save_env_value(key_env, api_key)
    config["model"] = model_name
    config["active_provider"] = provider_key
    config["active_provider_profile"] = ""
    save_config(config)

    print_plain()
    print_plain(_setup_label("供应商") + color(f"自定义 {format_label} 接口", Colors.GREEN))
    print_plain(_setup_label("接口地址") + color(base_url, Colors.DIM))
    print_plain(_setup_label("模型") + color(model_name, Colors.GREEN))
    print_plain()

    return {
        "provider": provider_key,
        "profile": "",
        "model": model_name,
        "api_key": api_key,
        "base_url": base_url.rstrip("/"),
        "api_mode": "anthropic_messages" if is_anthropic else "chat_completions",
    }


def _match_builtin_provider_by_base_url(base_url: str, *, is_anthropic: bool) -> str:
    """Return a built-in provider key when a custom setup URL is already known."""
    from mclaw.cli.auth import PROVIDER_REGISTRY

    target = (base_url or "").rstrip("/").lower()
    if not target:
        return ""

    for provider_key, pcfg in PROVIDER_REGISTRY.items():
        provider_url = (pcfg.base_url or "").rstrip("/").lower()
        provider_is_anthropic = pcfg.api_mode == "anthropic_messages"
        if provider_url == target and provider_is_anthropic == is_anthropic:
            return provider_key
    return ""


def _setup_api_key_provider(
    config: dict,
    provider_key: str,
    env_var: str,
    display_name: str,
    url: str,
    profile_id: str = "",
):
    """Setup flow for a named provider (key only)."""
    from mclaw.cli.config import save_env_value, get_env_value, save_config
    from mclaw.cli.colors import Colors, color
    from mclaw.cli.auth import PROVIDER_REGISTRY, resolve_base_url
    from mclaw.cli.provider_profiles import get_provider_profile

    current_key = get_env_value(env_var) or ""
    profile = get_provider_profile(provider_key, profile_id)

    print_plain()
    print_plain(_setup_label("接入方") + color(display_name, Colors.GREEN))
    print_plain(_setup_label("接口类型") + color(profile.label, Colors.DIM) + color(f" ({profile.models_dev_provider})", Colors.DIM))
    print_plain(_setup_label("获取密钥") + color(url, Colors.DIM))
    key_hint = f" [{current_key[:8]}...]" if current_key else ""
    api_key = _setup_secret(f"  {display_name} API 密钥{key_hint}: ")
    if not api_key:
        api_key = current_key
    if not api_key:
        print_plain(color("  未提供 API 密钥。", Colors.RED))
        return None

    save_env_value(env_var, api_key)
    print_plain(color(f"  已保存 {env_var}\n", Colors.GREEN))

    model_name = _select_setup_model(provider_key, config, profile_id=profile.id)
    if not model_name:
        print_plain(color("  未提供模型名，跳过。", Colors.YELLOW))
        return None
    config["model"] = model_name
    config["active_provider"] = provider_key
    config["active_provider_profile"] = profile.id
    save_config(config)
    print_plain(_setup_label("模型") + color(model_name, Colors.GREEN))
    print_plain()
    pcfg = PROVIDER_REGISTRY.get(provider_key)
    return {
        "provider": provider_key,
        "profile": profile.id,
        "model": model_name,
        "api_key": api_key,
        "base_url": resolve_base_url(provider_key),
        "api_mode": pcfg.api_mode if pcfg else "chat_completions",
    }


def _probe_models(base_url: str, api_key: str) -> list | None:
    """Probe an OpenAI-compatible endpoint for available models."""
    import httpx
    try:
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        with httpx.Client(timeout=10) as client:
            resp = client.get(f"{base_url}/models", headers=headers)
        if resp.status_code != 200:
            return None
        data = resp.json()
        models = data.get("data", [])
        return [m["id"] for m in models if isinstance(m, dict) and "id" in m]
    except Exception:
        return None


def _probe_anthropic(base_url: str, api_key: str) -> bool:
    """Probe an Anthropic-compatible endpoint. Returns True if reachable."""
    import httpx
    try:
        headers = {
            "x-api-key": api_key or "",
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload = {
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "hi"}],
        }
        with httpx.Client(timeout=15) as client:
            resp = client.post(f"{base_url}/v1/messages", headers=headers, json=payload)
        return resp.status_code in (200, 400, 401, 403, 429)
    except Exception:
        return False


def main():
    """Bootstrap the CLI process and dispatch to chat, setup, doctor, or channel runtimes."""
    mp.freeze_support()
    configure_text_output()
    from mclaw.runtime.bootstrap import BootstrapPathResolver

    BootstrapPathResolver.ensure_env()
    load_mclaw_dotenv()

    if len(sys.argv) > 1 and sys.argv[1] == "help":
        print_plain(_format_cli_help())
        return

    parser = _MClawArgumentParser(
        prog="mclaw",
        description="M-Claw — 跨平台桌面 CLI AI 智能体",
        add_help=False,
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("help", help=argparse.SUPPRESS)
    resume_parser = subparsers.add_parser("resume", help="恢复历史会话")
    resume_parser.add_argument("session_id", nargs="?", default=RESUME_LATEST_SESSION, help="可选会话 ID 或前缀")
    subparsers.add_parser("setup", help="运行初始设置向导")
    subparsers.add_parser("doctor", help="检查运行环境、依赖、工具和通道配置")
    weixin_parser = subparsers.add_parser("weixin", help="启动微信私聊网关")
    weixin_subparsers = weixin_parser.add_subparsers(dest="weixin_command")
    weixin_login_parser = weixin_subparsers.add_parser("login", help="扫码登录微信 iLink Bot")
    weixin_login_parser.add_argument("--bot-type", default="3", help="iLink bot_type，默认 3")
    weixin_login_parser.add_argument("--timeout", type=int, default=480, help="扫码等待秒数，默认 480")
    weixin_login_parser.add_argument("--no-qr", action="store_true", help="只打印二维码链接，不渲染终端二维码")
    weixin_login_parser.add_argument("--connect", action="store_true", help="扫码成功后立即启动微信私聊网关")
    dingtalk_parser = subparsers.add_parser("dingtalk", help="启动钉钉 Stream 网关")
    dingtalk_subparsers = dingtalk_parser.add_subparsers(dest="dingtalk_command")
    dingtalk_subparsers.add_parser("login", help="配置钉钉 Stream 网关凭据")
    dingtalk_nested_check_parser = dingtalk_subparsers.add_parser("check", help="检查钉钉 Stream 网关依赖和配置")
    dingtalk_nested_check_parser.add_argument("--strict", action="store_true", help="未 ready 时返回非零退出码")
    dingtalk_nested_check_parser.add_argument("--json", action="store_true", help="输出机器可读 JSON 报告")
    args = parser.parse_args()
    _setup_logging("INFO")

    if args.command == "help":
        print_plain(_format_cli_help())
    elif args.command == "resume":
        args.resume = getattr(args, "session_id", None) or RESUME_LATEST_SESSION
        _run_chat(args)
    elif args.command == "setup":
        _run_setup(args)
    elif args.command == "doctor":
        from mclaw.doctor import format_doctor, run_doctor
        print_plain(format_doctor(run_doctor()))
    elif args.command == "weixin":
        weixin_command = getattr(args, "weixin_command", None)
        if weixin_command == "login":
            _run_weixin_login(args)
        else:
            _run_weixin(args)
    elif args.command == "dingtalk":
        dingtalk_command = getattr(args, "dingtalk_command", None)
        if dingtalk_command == "login":
            _run_dingtalk_setup(args)
        elif dingtalk_command == "check":
            _run_dingtalk_check(args)
        else:
            _run_dingtalk(args)
    else:
        _run_chat(args)


if __name__ == "__main__":
    main()
