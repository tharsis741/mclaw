"""Weixin channel configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from mclaw.cli.config import get_env_value
from mclaw.constants import display_mclaw_path

ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
_WEIXIN_REQUIRED_FOR = "channel:weixin"


def _authorized_secret_env(name: str, get_raw) -> str:
    try:
        from mclaw.runtime.features import authorized_env_value

        return authorized_env_value(_WEIXIN_REQUIRED_FOR, name, get_raw)
    except Exception:
        return ""


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return default


def _coerce_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    raw = str(value).strip()
    return [raw] if raw else []


def _coerce_int(value: Any, default: int) -> int:
    if value is None or value == "":
        return default
    return int(value)


def _coerce_float(value: Any, default: float) -> float:
    if value is None or value == "":
        return default
    return float(value)


@dataclass
class WeixinConfig:
    enabled: bool = False
    account_id: str = ""
    token: str = ""
    base_url: str = ILINK_BASE_URL
    dm_policy: str = "open"
    allow_from: list[str] = field(default_factory=list)
    group_policy: str = "disabled"
    session_scope: str = "user"
    max_message_length: int = 2000
    send_chunk_delay_seconds: float = 1.5
    send_chunk_retries: int = 4
    send_chunk_retry_delay_seconds: float = 1.0
    poll_timeout_ms: int = 35000
    api_timeout_ms: int = 15000
    shutdown_timeout_seconds: float = 5.0
    dedup_ttl_seconds: float = 300.0
    media_cache_enabled: bool = True
    media_cache_dir: str = ""
    media_cdn_base_url: str = "https://novac2c.cdn.weixin.qq.com/c2c"
    media_download_timeout_seconds: float = 60.0
    media_upload_timeout_seconds: float = 120.0
    media_max_bytes: int = 100 * 1024 * 1024
    toolsets: list[str] = field(default_factory=lambda: ["mclaw-required"])

    @classmethod
    def from_config(cls, config: dict | None) -> "WeixinConfig":
        root = config or {}
        channels = root.get("channels", {}) if isinstance(root.get("channels"), dict) else {}
        raw = channels.get("weixin", {}) if isinstance(channels.get("weixin"), dict) else {}

        def env(name: str) -> str:
            return os.environ.get(name, "") or (get_env_value(name) or "")

        return cls(
            enabled=_coerce_bool(raw.get("enabled"), False),
            account_id=str(env("WEIXIN_ACCOUNT_ID") or "").strip(),
            token=str(_authorized_secret_env("WEIXIN_TOKEN", env) or "").strip(),
            base_url=str(raw.get("base_url") or env("WEIXIN_BASE_URL") or ILINK_BASE_URL).strip().rstrip("/"),
            dm_policy=str(raw.get("dm_policy") or env("WEIXIN_DM_POLICY") or "open").strip().lower(),
            allow_from=_coerce_list(raw.get("allow_from") if raw.get("allow_from") is not None else env("WEIXIN_ALLOWED_USERS")),
            group_policy=str(raw.get("group_policy") or "disabled").strip().lower(),
            session_scope=str(raw.get("session_scope") or "user").strip().lower(),
            max_message_length=_coerce_int(raw.get("max_message_length"), 2000),
            send_chunk_delay_seconds=_coerce_float(raw.get("send_chunk_delay_seconds"), 1.5),
            send_chunk_retries=_coerce_int(raw.get("send_chunk_retries"), 4),
            send_chunk_retry_delay_seconds=_coerce_float(raw.get("send_chunk_retry_delay_seconds"), 1.0),
            poll_timeout_ms=_coerce_int(raw.get("poll_timeout_ms"), 35000),
            api_timeout_ms=_coerce_int(raw.get("api_timeout_ms"), 15000),
            shutdown_timeout_seconds=_coerce_float(raw.get("shutdown_timeout_seconds"), 5.0),
            dedup_ttl_seconds=_coerce_float(raw.get("dedup_ttl_seconds"), 300.0),
            media_cache_enabled=_coerce_bool(raw.get("media_cache_enabled"), True),
            media_cache_dir=str(raw.get("media_cache_dir") or env("WEIXIN_MEDIA_CACHE_DIR") or "").strip(),
            media_cdn_base_url=str(raw.get("media_cdn_base_url") or "https://novac2c.cdn.weixin.qq.com/c2c").strip().rstrip("/"),
            media_download_timeout_seconds=_coerce_float(raw.get("media_download_timeout_seconds"), 60.0),
            media_upload_timeout_seconds=_coerce_float(raw.get("media_upload_timeout_seconds"), 120.0),
            media_max_bytes=_coerce_int(raw.get("media_max_bytes"), 100 * 1024 * 1024),
            toolsets=list(raw.get("toolsets") or root.get("toolsets") or ["mclaw-required"]),
        )

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.account_id:
            errors.append(f"WEIXIN_ACCOUNT_ID is required in {display_mclaw_path('.env')}")
        if not self.token:
            errors.append(f"WEIXIN_TOKEN is required in {display_mclaw_path('.env')}")
        if self.dm_policy not in {"open", "allowlist", "disabled"}:
            errors.append("channels.weixin.dm_policy must be one of: open, allowlist, disabled")
        if self.group_policy != "disabled":
            errors.append("channels.weixin.group_policy must remain disabled in the private-chat release")
        if self.session_scope not in {"chat", "user", "chat_user"}:
            errors.append("channels.weixin.session_scope must be one of: chat, user, chat_user")
        if self.media_download_timeout_seconds <= 0:
            errors.append("channels.weixin.media_download_timeout_seconds must be > 0")
        if self.media_upload_timeout_seconds <= 0:
            errors.append("channels.weixin.media_upload_timeout_seconds must be > 0")
        if self.media_max_bytes <= 0:
            errors.append("channels.weixin.media_max_bytes must be > 0")
        return errors
