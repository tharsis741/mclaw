# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Load, merge, validate, and persist M-Claw configuration.

User-level config from M-Claw home may be overlaid with project `.mclaw.yaml`.
Project config is intentionally constrained so a repository cannot force local
security-sensitive settings or enable desktop GUI behavior for every checkout.
"""

import copy
import os
import platform
import re
import tempfile
from pathlib import Path
from typing import Any

import yaml

from mclaw.cli.default_soul import DEFAULT_SOUL_MD
from mclaw.cli.tui.console import print_plain
from mclaw.constants import get_mclaw_home

_IS_WINDOWS = platform.system() == "Windows"
_ENV_VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ConfigError(Exception):
    """Raised when M-Claw configuration is invalid or violates security rules."""


# Config paths.

def get_config_path() -> Path:
    return get_mclaw_home() / "config.yaml"


def get_env_path() -> Path:
    return get_mclaw_home() / ".env"


def find_project_config(start_dir: Path | None = None) -> Path | None:
    """Walk upward from start_dir looking for the first .mclaw.yaml file."""
    if start_dir is None:
        start_dir = Path.cwd()
    current = start_dir.resolve()
    root = current.anchor
    while True:
        candidate = current / ".mclaw.yaml"
        if candidate.is_file():
            return candidate
        if current == root:
            break
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


_FORBIDDEN_PROJECT_FIELDS = {"sandbox.root", "delegation_dir", "security.trusted_workspaces"}


def _check_forbidden_fields(config: dict, path: str = "") -> None:
    """Recursively check for forbidden fields. Raises ConfigError if found."""
    for key, value in config.items():
        full_key = f"{path}.{key}" if path else key
        if full_key in _FORBIDDEN_PROJECT_FIELDS:
            raise ConfigError(f"Forbidden field '{full_key}' in .mclaw.yaml")
        if isinstance(value, dict):
            _check_forbidden_fields(value, full_key)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    _check_forbidden_fields(item, full_key)


def load_project_config(path: Path) -> dict:
    """Load and validate a .mclaw.yaml file."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in .mclaw.yaml: {exc}") from exc
    except Exception as exc:
        raise ConfigError(f"Cannot read .mclaw.yaml: {exc}") from exc

    if raw is None:
        config = {}
    elif isinstance(raw, dict):
        config = raw
    else:
        raise ConfigError(".mclaw.yaml must contain a YAML mapping.")

    _check_forbidden_fields(config)
    # Project config may tune pet visuals, but cannot force desktop GUI startup
    # for every checkout. Runtime enablement remains controlled by user config.
    display_cfg = config.get("display")
    pet_cfg = display_cfg.get("pet") if isinstance(display_cfg, dict) else None
    if isinstance(pet_cfg, dict):
        pet_cfg.pop("enabled", None)
    return config


def _load_user_config_file(config_path: Path) -> dict[str, Any]:
    """Read config.yaml and require a YAML mapping."""
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {config_path}: {exc}") from exc
    except Exception as exc:
        raise ConfigError(f"Cannot read {config_path}: {exc}") from exc

    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{config_path} must contain a YAML mapping.")
    return raw


def load_merged_config(cwd: Path | None = None) -> dict:
    """Load user config merged with project-level .mclaw.yaml if present."""
    ensure_mclaw_home()
    config = copy.deepcopy(DEFAULT_CONFIG)

    # 1. User-level config
    user_path = get_config_path()
    if user_path.exists():
        user_config = _load_user_config_file(user_path)
        config = _deep_merge(config, user_config)

    # 2. Project-level config
    project_path = find_project_config(cwd)
    if project_path:
        project_config = load_project_config(project_path)
        config = _deep_merge(config, project_config)
        config["_project_config_dir"] = str(project_path.parent)

    return config


# Security helpers.

def _secure_dir(path):
    """Set directory to owner-only access (0700). Best-effort on Windows."""
    try:
        os.chmod(path, 0o700)
    except (OSError, NotImplementedError):
        pass


def _secure_file(path):
    """Set file to owner-only read/write (0600). Best-effort on Windows."""
    try:
        if os.path.exists(str(path)):
            os.chmod(path, 0o600)
    except (OSError, NotImplementedError):
        pass


def _ensure_default_soul_md(home: Path) -> None:
    soul_path = home / "SOUL.md"
    if soul_path.exists():
        return
    soul_path.write_text(DEFAULT_SOUL_MD, encoding="utf-8")
    _secure_file(soul_path)


def ensure_mclaw_home():
    """Ensure M-Claw home directory structure exists with secure permissions."""
    home = get_mclaw_home()
    home.mkdir(parents=True, exist_ok=True)
    _secure_dir(home)
    for subdir in ("sessions", "logs", "memories"):
        d = home / subdir
        d.mkdir(parents=True, exist_ok=True)
        _secure_dir(d)
    _ensure_default_soul_md(home)



# Default configuration.

DEFAULT_CONFIG: dict[str, Any] = {
    "model": "",
    "active_provider": "",
    "active_provider_profile": "",
    "providers": {},
    "fallback_providers": [],
    "reasoning": {
        "effort": "",
    },
    "prompt_cache": {
        "enabled": True,
    },
    "toolsets": ["mclaw-required"],
    "tools": {
        "disabled": [],
    },
    "terminal": {
        "cwd": ".",
        "timeout": 180,
        "max_timeout": 600,
        "env": {},
    },
    "checkpoints": {
        "enabled": True,
        "max_snapshots": 50,
        "max_total_size_mb": 500,
        "max_file_size_mb": 10,
        "include_terminal": True,
        "restore_chat_context": True,
        "prune_retention_days": 30,
        "delete_orphans": True,
    },
    "file_read_max_chars": 100_000,
    "compression": {
        "enabled": True,
        "threshold": 0.50,
        "target_ratio": 0.20,
        "summary_model": "",
        "summary_provider": "auto",
        "summary_base_url": None,
        "summary_timeout": 180,
    },
    "auxiliary": {
        "vision": {
            "provider": "qwen",
            "model": "qwen-vl-max",
            "base_url": "",
            "timeout": 30,
            "download_timeout": 30,
        },
        "web_search": {
            "backend": "auto",
            "tavily_timeout": 30,
            "dashscope_timeout": 90,
            "dashscope_deep_timeout": 120,
            "fallback": True,
        },
        "web_extract": {
            "backend": "trafilatura",
            "timeout": 30,
            "firecrawl_api_url": "https://api.firecrawl.dev/v2/scrape",
        },
        "session_search": {
            "provider": "auto",
            "model": "",
            "base_url": "",
            "timeout": 30,
        },
        "asr": {
            "enabled": "auto",
            "provider": "qwen",
            "backend": "qwen_realtime",
            "model": "qwen3-asr-flash-realtime",
            "websocket_url": "",
            "force_ipv4": "auto",
            "ca_bundle": "auto",
            "recorder_backend": "auto",
            "arecord_device": "auto",
            "language": "zh",
            "sample_rate": 16000,
            "input_audio_format": "pcm",
            "channels": 1,
            "enable_server_vad": True,
            "vad_threshold": 0.0,
            "silence_duration_ms": 400,
            "listen_mode": "wake_word",
            "require_wake_word": True,
            "wake_words": ["小爪", "老麦"],
            "dedupe_seconds": 1.5,
            "allow_voice_slash_commands": False,
            "min_audio_rms": 1,
            "min_voice_chunks": 1,
            "push_to_talk_key": "f8",
            "push_to_talk_behavior": "tap_once",
        },
    },
    "channels": {
        "weixin": {
            "enabled": False,
            "base_url": "https://ilinkai.weixin.qq.com",
            "dm_policy": "open",
            "allowed_users": [],
            "session_scope": "user",
            "max_message_length": 2000,
            "send_chunk_delay_seconds": 1.5,
            "send_chunk_retries": 4,
            "send_chunk_retry_delay_seconds": 1.0,
            "poll_timeout_ms": 35000,
            "api_timeout_ms": 15000,
            "shutdown_timeout_seconds": 5.0,
            "dedup_ttl_seconds": 300.0,
            "media_cache_enabled": True,
            "media_cache_dir": "",
            "media_cdn_base_url": "https://novac2c.cdn.weixin.qq.com/c2c",
            "media_download_timeout_seconds": 60.0,
            "media_upload_timeout_seconds": 120.0,
            "media_max_bytes": 104857600,
            "toolsets": ["mclaw-required"],
        },
        "dingtalk": {
            "enabled": False,
            "dm_policy": "open",
            "group_policy": "mention_only",
            "require_mention": True,
            "allowed_users": [],
            "allowed_chats": [],
            "free_response_chats": [],
            "mention_patterns": [],
            "session_scope": "chat_user",
            "max_message_length": 20000,
            "reconnect_backoff_seconds": [2, 5, 10, 30, 60],
            "shutdown_timeout_seconds": 5.0,
            "close_timeout_seconds": 3.0,
            "dedup_ttl_seconds": 300.0,
            "session_webhooks_max": 500,
            "open_conversation_map": {},
            "media_cache_enabled": True,
            "media_cache_dir": "",
            "media_download_timeout_seconds": 60.0,
            "media_max_bytes": 104857600,
            "toolsets": ["mclaw-required"],
        },
    },
    "scheduler": {
        "enabled": True,
        "tick_interval_seconds": 30,
        "max_due_per_tick": 5,
        "max_workers": 2,
        "default_timezone": "Asia/Shanghai",
        "default_timeout_seconds": 3600,
        "default_max_iterations": 200,
        "max_consecutive_failures": 5,
        "stale_run_after_seconds": 3600,
        "output_dir": str(get_mclaw_home() / "scheduler" / "output"),
        "delivery": {
            "dingtalk": {
                "enabled": True,
            },
            "weixin": {
                "enabled": True,
            },
        },
    },
    "display": {
        "live_status_animation": True,
        "pet": {
            "enabled": "auto",
            "backend": "pyside6",
            "asset": "robot-dark",
            "scale": 1.0,
            "always_on_top": True,
            "click_through": False,
            "position": "bottom_right",
            "x": None,
            "y": None,
            "show_bubble": True,
            "bubble_seconds": 2.5,
            "sleep_after_seconds": 120.0,
            "notify": {
                "turn_completed": True,
                "tool_finished": False,
                "background_completed": True,
                "delegation_completed": True,
                "sound": False,
            },
        },
    },
    "memory": {
        "memory_enabled": True,
        "user_profile_enabled": True,
        "memory_review_round": 10,
        "memory_char_limit": 2200,
        "user_char_limit": 1375,
    },
    "delegation": {
        "model": "",
        "provider": "",
        "base_url": "",
        "max_iterations": 10,
        "timeout_seconds": 600,
    },
    "skills": {
        "evolution_review_round": 10,
        "disabled": [],
        "platform_disabled": {},
    },
    "security": {
        "trusted_workspaces": [],
    },
}


# Config I/O.

def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base, returning a new dict."""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(*, strict: bool = False) -> dict[str, Any]:
    """Load configuration from M-Claw home config.yaml, merged with defaults."""
    ensure_mclaw_home()
    config_path = get_config_path()

    config = copy.deepcopy(DEFAULT_CONFIG)

    if config_path.exists():
        try:
            user_config = _load_user_config_file(config_path)
            config = _deep_merge(config, user_config)
        except ConfigError as exc:
            if strict:
                raise
            print_plain(f"Warning: Failed to load config: {exc}")

    return config


def save_config(config: dict[str, Any]) -> None:
    """Save configuration to M-Claw home config.yaml."""
    from mclaw.utils import atomic_yaml_write
    ensure_mclaw_home()
    atomic_yaml_write(get_config_path(), config)
    _secure_file(get_config_path())


def upsert_fallback_provider_model(config: dict[str, Any], provider: str, model: str) -> None:
    """Record the model a user configured for a provider.

    fallback_providers stores provider-specific model choices and the provider
    failover order used when no explicit provider is selected. Only complete
    provider+model dictionaries are kept on rewrite.
    """
    provider_name = str(provider or "").strip()
    model_name = str(model or "").strip()
    if not provider_name or not model_name:
        return

    raw_entries = config.get("fallback_providers")
    entries = raw_entries if isinstance(raw_entries, list) else []
    rewritten: list[dict[str, Any]] = []
    updated = False

    for raw in entries:
        if not isinstance(raw, dict):
            continue
        existing_provider = str(raw.get("provider") or "").strip()
        existing_model = str(raw.get("model") or "").strip()
        if not existing_provider:
            continue

        entry = dict(raw)
        if existing_provider == provider_name:
            entry["provider"] = provider_name
            entry["model"] = model_name
            updated = True
        elif existing_model:
            entry["provider"] = existing_provider
            entry["model"] = existing_model
        else:
            continue
        rewritten.append(entry)

    if not updated:
        rewritten.append({"provider": provider_name, "model": model_name})
    config["fallback_providers"] = rewritten


# .env I/O.

def load_env() -> dict[str, str]:
    """Load environment variables from M-Claw home .env."""
    env_path = get_env_path()
    env_vars = {}
    if env_path.exists():
        open_kw = {"encoding": "utf-8", "errors": "replace"} if _IS_WINDOWS else {}
        with open(env_path, **open_kw) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, _, value = line.partition('=')
                    env_vars[key.strip()] = value.strip().strip('"\'')
    return env_vars


def _sanitize_api_key(value: str) -> str:
    """Strip accidental prefixes like 'api_key=sk-...' that users paste by mistake."""
    for prefix in ("api_key=", "api-key=", "apikey=", "key="):
        if value.lower().startswith(prefix):
            value = value[len(prefix):]
            break
    return value.strip()


def save_env_value(key: str, value: str) -> None:
    """Save or update a value in M-Claw home .env."""
    if not _ENV_VAR_NAME_RE.match(key):
        raise ValueError(f"Invalid environment variable name: {key!r}")
    value = value.replace("\n", "").replace("\r", "")
    if "API_KEY" in key.upper():
        value = _sanitize_api_key(value)
    ensure_mclaw_home()
    env_path = get_env_path()

    read_kw = {"encoding": "utf-8", "errors": "replace"} if _IS_WINDOWS else {}
    write_kw = {"encoding": "utf-8"} if _IS_WINDOWS else {}

    lines = []
    if env_path.exists():
        with open(env_path, **read_kw) as f:
            lines = f.readlines()

    found = False
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{key}="):
            lines[i] = f"{key}={value}\n"
            found = True
            break

    if not found:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(f"{key}={value}\n")

    fd, tmp_path = tempfile.mkstemp(dir=str(env_path.parent), suffix='.tmp', prefix='.env_')
    try:
        with os.fdopen(fd, 'w', **write_kw) as f:
            f.writelines(lines)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, env_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    _secure_file(env_path)
    os.environ[key] = value


def remove_env_value(key: str) -> bool:
    """Remove a key from M-Claw home .env and os.environ."""
    if not _ENV_VAR_NAME_RE.match(key):
        raise ValueError(f"Invalid environment variable name: {key!r}")
    env_path = get_env_path()
    if not env_path.exists():
        os.environ.pop(key, None)
        return False

    read_kw = {"encoding": "utf-8", "errors": "replace"} if _IS_WINDOWS else {}
    write_kw = {"encoding": "utf-8"} if _IS_WINDOWS else {}

    with open(env_path, **read_kw) as f:
        lines = f.readlines()

    new_lines = [line for line in lines if not line.strip().startswith(f"{key}=")]
    found = len(new_lines) < len(lines)

    if found:
        fd, tmp_path = tempfile.mkstemp(dir=str(env_path.parent), suffix='.tmp', prefix='.env_')
        try:
            with os.fdopen(fd, 'w', **write_kw) as f:
                f.writelines(new_lines)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, env_path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        _secure_file(env_path)

    os.environ.pop(key, None)
    return found


def get_env_value(key: str) -> str | None:
    """Get a value from environment or M-Claw home .env."""
    val = os.environ.get(key)
    if val:
        return val
    return load_env().get(key)


def mask_api_key(key: str | None) -> str:
    """Return a partially masked version of an API key for display.

    Display rules:
    - None or ≤4 chars → "***"
    - 5–12 chars → first 4 + "..."
    - >12 chars → first 4 + "..." + last 4
    """
    if not key:
        return "***"
    n = len(key)
    if n <= 4:
        return "***"
    if n <= 12:
        return key[:4] + "..."
    return key[:4] + "..." + key[-4:]
