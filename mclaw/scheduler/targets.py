# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Delivery target validation, display, and channel binding helpers."""

from __future__ import annotations

from dataclasses import dataclass
import re
import time
from typing import Any

from mclaw.scheduler.ids import new_target_id
from mclaw.scheduler.models import SchedulerTarget, normalize_chat_type
from mclaw.scheduler.store import SchedulerStore

_SCHEDULE_BIND_RE = re.compile(r"(?:^|\s)/schedule-bind\s+(\S+)(?:\s+(.+))?\s*$", re.IGNORECASE)


class TargetManager:
    def validate_target(self, target: SchedulerTarget) -> SchedulerTarget:
        chat_type = normalize_chat_type(target.chat_type)
        metadata = dict(target.route_metadata or {})
        route_status = target.route_status
        capabilities = dict(target.capabilities or {})

        if target.type == "local":
            chat_type = "local"
            route_status = "ready" if target.enabled else "disabled"
            capabilities.setdefault("text", True)
            capabilities.setdefault("local_output", True)
        elif target.type == "dingtalk_group":
            chat_type = "group"
            capabilities.setdefault("text", True)
            route_status = "ready" if metadata.get("open_conversation_id") and target.enabled else "needs_route"
        elif target.type == "dingtalk_private":
            chat_type = "private"
            capabilities.setdefault("text", True)
            route_status = "ready" if metadata.get("sender_staff_id") and target.enabled else "needs_route"
        elif target.type == "weixin_private":
            chat_type = "private"
            capabilities.setdefault("text", True)
            route_status = "ready" if target.chat_id and target.enabled else "needs_route"
        else:
            route_status = "needs_route"

        if not target.enabled:
            route_status = "disabled"

        return SchedulerTarget(
            id=target.id,
            type=target.type,
            display_name=target.display_name,
            account_id=target.account_id,
            chat_id=target.chat_id,
            chat_type=chat_type,
            route_metadata=metadata,
            capabilities=capabilities,
            route_status=route_status,  # type: ignore[arg-type]
            source=target.source,
            first_seen_at=target.first_seen_at,
            last_seen_at=target.last_seen_at,
            enabled=target.enabled,
        )

    def display_target(self, target: SchedulerTarget) -> str:
        if target.type == "local":
            return f"本地输出：{target.display_name}"
        if target.type == "dingtalk_group":
            return f"钉钉群聊投递：{target.display_name}"
        if target.type == "dingtalk_private":
            return f"钉钉私聊投递：{target.display_name}"
        if target.type == "weixin_private":
            return f"微信私聊投递：{target.display_name}"
        return f"{target.type}：{target.display_name}"

    def target_capabilities(self, target: SchedulerTarget) -> dict[str, Any]:
        return dict(self.validate_target(target).capabilities or {})


@dataclass
class BindCommand:
    code: str
    display_name: str


@dataclass
class BindResult:
    handled: bool
    success: bool = False
    message: str = ""
    target: SchedulerTarget | None = None


def parse_schedule_bind_command(text: str) -> BindCommand | None:
    match = _SCHEDULE_BIND_RE.search(text or "")
    if not match:
        return None
    display_name = str(match.group(2) or "").strip()
    return BindCommand(code=str(match.group(1)).strip(), display_name=display_name or "未命名目标")


def bind_pairing_from_channel(
    *,
    store: SchedulerStore,
    code: str,
    display_name: str,
    target_type: str,
    account_id: str,
    chat_id: str,
    chat_type: str,
    route_metadata: dict[str, Any] | None = None,
) -> BindResult:
    now = time.time()
    pairing = store.get_pairing(code)
    if not pairing:
        return BindResult(handled=True, success=False, message="绑定失败：绑定码无效或已过期。")
    if pairing.status != "waiting":
        return BindResult(handled=True, success=False, message="绑定失败：绑定码无效或已过期。")
    if pairing.expires_at <= now:
        store.expire_pairings(now)
        return BindResult(handled=True, success=False, message="绑定失败：绑定码无效或已过期。")
    if pairing.requested_type != target_type:
        return BindResult(handled=True, success=False, message="绑定失败：当前聊天类型和绑定类型不匹配。")

    metadata = dict(route_metadata or {})
    target = SchedulerTarget(
        id=new_target_id(target_type),
        type=target_type,  # type: ignore[arg-type]
        display_name=display_name,
        account_id=account_id,
        chat_id=chat_id,
        chat_type=normalize_chat_type(chat_type),
        route_metadata=metadata,
        source="binding",
        first_seen_at=now,
        last_seen_at=now,
        enabled=True,
    )
    target = TargetManager().validate_target(target)
    if target.route_status != "ready":
        error = _route_error_for_target(target)
        store.fail_pairing(code, error=error)
        return BindResult(handled=True, success=False, message=f"绑定失败：{error}")

    bound = store.bind_pairing(code, target=target)
    target = store.get_target(bound.target_id) or target
    return BindResult(handled=True, success=True, message=f"已绑定投递目标：{display_name}", target=target)


def _route_error_for_target(target: SchedulerTarget) -> str:
    if target.type == "dingtalk_group":
        return "钉钉群聊需要先在 DingTalk setup 中连接群聊。"
    if target.type == "dingtalk_private":
        return "钉钉私聊缺少 sender_staff_id。"
    if target.type == "weixin_private":
        return "微信私聊缺少用户路由。"
    return "投递路由不完整。"
