# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""DingTalk channel configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from mclaw.cli.config import get_env_value
from mclaw.constants import display_mclaw_path

_DINGTALK_REQUIRED_FOR = "channel:dingtalk"


def _authorized_secret_env(name: str, get_raw) -> str:
    """Read DingTalk secrets only when the runtime scope has been authorized."""
    try:
        from mclaw.runtime.features import authorized_env_value

        return authorized_env_value(_DINGTALK_REQUIRED_FOR, name, get_raw)
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
        stripped = value.strip()
        if not stripped:
            return []
        return [item.strip() for item in stripped.replace("\n", ",").split(",") if item.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    raw = str(value).strip()
    return [raw] if raw else []


def _coerce_str_map(value: Any) -> dict[str, str]:
    """Normalize named conversation aliases into open conversation ids."""
    if not isinstance(value, dict):
        return {}
    result: dict[str, str] = {}
    for key, item in value.items():
        name = str(key).strip()
        if not isinstance(item, dict):
            continue
        mapped = str(item.get("open_conversation_id") or "").strip()
        if name and mapped:
            result[name] = mapped
    return result


def _coerce_int(value: Any, default: int) -> int:
    if value is None or value == "":
        return default
    return int(value)


def _coerce_float(value: Any, default: float) -> float:
    if value is None or value == "":
        return default
    return float(value)


def _channel_toolsets(value: Any) -> list[str]:
    from mclaw.tools.toolsets import validate_toolset

    result = [
        name
        for name in _coerce_list(value)
        if validate_toolset(name, allow_platform=False, allow_scoped=False)
    ]
    return result or ["mclaw-required"]


@dataclass
class DingTalkConfig:
    """Resolved DingTalk channel settings from config and scoped environment."""

    enabled: bool = False
    client_id: str = ""
    client_secret: str = ""
    robot_code: str = ""
    dm_policy: str = "open"
    group_policy: str = "mention_only"
    require_mention: bool = True
    allowed_users: list[str] = field(default_factory=list)
    allowed_chats: list[str] = field(default_factory=list)
    free_response_chats: list[str] = field(default_factory=list)
    mention_patterns: list[str] = field(default_factory=list)
    session_scope: str = "chat_user"
    max_message_length: int = 20000
    reconnect_backoff_seconds: list[float] = field(default_factory=lambda: [2, 5, 10, 30, 60])
    shutdown_timeout_seconds: float = 5.0
    close_timeout_seconds: float = 3.0
    dedup_ttl_seconds: float = 300.0
    session_webhooks_max: int = 500
    open_conversation_map: dict[str, str] = field(default_factory=dict)
    media_cache_enabled: bool = True
    media_cache_dir: str = ""
    media_download_timeout_seconds: float = 60.0
    media_max_bytes: int = 100 * 1024 * 1024
    toolsets: list[str] = field(default_factory=lambda: ["mclaw-required"])

    @classmethod
    def from_config(cls, config: dict | None) -> "DingTalkConfig":
        """Build a config object from channels.dingtalk and authorized env vars."""
        root = config or {}
        channels = root.get("channels", {}) if isinstance(root.get("channels"), dict) else {}
        raw = channels.get("dingtalk", {}) if isinstance(channels.get("dingtalk"), dict) else {}

        def env(name: str) -> str:
            return os.environ.get(name, "") or (get_env_value(name) or "")

        client_id = str(_authorized_secret_env("DINGTALK_CLIENT_ID", env) or "").strip()
        return cls(
            enabled=_coerce_bool(raw.get("enabled"), False),
            client_id=client_id,
            client_secret=str(_authorized_secret_env("DINGTALK_CLIENT_SECRET", env) or "").strip(),
            robot_code=str(_authorized_secret_env("DINGTALK_ROBOT_CODE", env) or "").strip(),
            dm_policy=str(raw.get("dm_policy") or env("DINGTALK_DM_POLICY") or "open").strip().lower(),
            group_policy=str(raw.get("group_policy") or env("DINGTALK_GROUP_POLICY") or "mention_only").strip().lower(),
            require_mention=_coerce_bool(
                raw.get("require_mention") if raw.get("require_mention") is not None else env("DINGTALK_REQUIRE_MENTION"),
                True,
            ),
            allowed_users=_coerce_list(raw.get("allowed_users") if raw.get("allowed_users") is not None else env("DINGTALK_ALLOWED_USERS")),
            allowed_chats=_coerce_list(raw.get("allowed_chats") if raw.get("allowed_chats") is not None else env("DINGTALK_ALLOWED_CHATS")),
            free_response_chats=_coerce_list(raw.get("free_response_chats") if raw.get("free_response_chats") is not None else env("DINGTALK_FREE_RESPONSE_CHATS")),
            mention_patterns=_coerce_list(raw.get("mention_patterns") if raw.get("mention_patterns") is not None else env("DINGTALK_MENTION_PATTERNS")),
            session_scope=str(raw.get("session_scope") or "chat_user").strip().lower(),
            max_message_length=_coerce_int(raw.get("max_message_length"), 20000),
            reconnect_backoff_seconds=[
                _coerce_float(item, 2.0)
                for item in (_coerce_list(raw.get("reconnect_backoff_seconds")) or ["2", "5", "10", "30", "60"])
            ],
            shutdown_timeout_seconds=_coerce_float(raw.get("shutdown_timeout_seconds"), 5.0),
            close_timeout_seconds=_coerce_float(raw.get("close_timeout_seconds"), 3.0),
            dedup_ttl_seconds=_coerce_float(raw.get("dedup_ttl_seconds"), 300.0),
            session_webhooks_max=_coerce_int(raw.get("session_webhooks_max"), 500),
            open_conversation_map=_coerce_str_map(raw.get("open_conversation_map")),
            media_cache_enabled=_coerce_bool(raw.get("media_cache_enabled"), True),
            media_cache_dir=str(raw.get("media_cache_dir") or env("DINGTALK_MEDIA_CACHE_DIR") or "").strip(),
            media_download_timeout_seconds=_coerce_float(raw.get("media_download_timeout_seconds"), 60.0),
            media_max_bytes=_coerce_int(raw.get("media_max_bytes"), 100 * 1024 * 1024),
            toolsets=_channel_toolsets(
                raw.get("toolsets") or root.get("toolsets") or ["mclaw-required"]
            ),
        )

    def allowed_user_set(self) -> set[str]:
        """Return normalized allowlist values for inbound user checks."""
        return {item.lower() for item in self.allowed_users if item}

    def validate(self) -> list[str]:
        """Return user-facing configuration errors without exposing secrets."""
        errors: list[str] = []
        if not self.client_id:
            errors.append(f"DINGTALK_CLIENT_ID is required in {display_mclaw_path('.env')}")
        if not self.client_secret:
            errors.append(f"DINGTALK_CLIENT_SECRET is required in {display_mclaw_path('.env')}")
        if not self.robot_code:
            errors.append(f"DINGTALK_ROBOT_CODE is required in {display_mclaw_path('.env')}")
        if self.dm_policy not in {"open", "allowlist", "disabled"}:
            errors.append("channels.dingtalk.dm_policy must be one of: open, allowlist, disabled")
        if self.group_policy not in {"open", "mention_only", "disabled"}:
            errors.append("channels.dingtalk.group_policy must be one of: open, mention_only, disabled")
        if self.session_scope not in {"chat", "user", "chat_user"}:
            errors.append("channels.dingtalk.session_scope must be one of: chat, user, chat_user")
        if self.max_message_length <= 0:
            errors.append("channels.dingtalk.max_message_length must be > 0")
        if self.media_download_timeout_seconds <= 0:
            errors.append("channels.dingtalk.media_download_timeout_seconds must be > 0")
        if self.media_max_bytes <= 0:
            errors.append("channels.dingtalk.media_max_bytes must be > 0")
        return errors
