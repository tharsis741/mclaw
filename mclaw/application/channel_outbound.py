# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Session-bound file sends across the isolated worker transport."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from mclaw.channels.outbound_bridge import OutboundBridgeResult
from mclaw.runtime.paths import PathPolicy

_ACTIVE_SENDS: dict[int, set[asyncio.Task]] = {}


class _WorkerTarget:
    def __init__(self, worker, platform: str) -> None:
        self.worker, self.platform = worker, platform

    def send_file(self, *, file_path: str, caption: str = "", as_file: bool = False, **_kwargs):
        value = self.worker.request("channel_outbound", operation="send_file", platform=self.platform,
                                    file_path=str(Path(file_path).expanduser().resolve()),
                                    caption=caption, as_file=as_file)
        if not isinstance(value, dict):
            return OutboundBridgeResult(success=False, error="渠道未返回发送结果", completion_unknown=True)
        return OutboundBridgeResult(**{key: value[key] for key in (
            "success", "message_id", "error", "interrupted", "completion_unknown") if key in value})


def install_channel_outbound(worker) -> None:
    """Install only local IPC proxies; routing credentials stay in the owning service."""
    from mclaw.channels.weixin.outbound_registry import register_weixin_outbound_proxy
    from mclaw.channels.dingtalk.outbound_registry import register_dingtalk_outbound_proxy

    register_weixin_outbound_proxy(worker.session["id"], _WorkerTarget(worker, "weixin"))
    register_dingtalk_outbound_proxy(worker.session["id"], _WorkerTarget(worker, "dingtalk"))


async def handle_channel_outbound(home: Path, session_id: str, request: dict) -> dict:
    """Validate a worker request and dispatch to its exact registered peer."""
    from mclaw.channels.weixin.outbound_registry import get_weixin_outbound_target
    from mclaw.channels.dingtalk.outbound_registry import get_dingtalk_outbound_target

    platform = request.get("platform")
    lookup = {"weixin": get_weixin_outbound_target, "dingtalk": get_dingtalk_outbound_target}.get(platform)
    if lookup is None or request.get("operation") != "send_file":
        return {"success": False, "error": "渠道发送操作无效"}
    if not isinstance(request.get("id"), str) or not request["id"] or len(request["id"]) > 128:
        return {"success": False, "error": "渠道发送请求标识无效"}
    target = lookup(session_id)
    if target is None or not hasattr(target, "adapter"):
        return {"success": False, "error": "当前会话未关联可用的渠道连接"}
    value = request.get("file_path")
    if not isinstance(value, str) or not value.strip():
        return {"success": False, "error": "请选择要发送的文件"}
    home = Path(home).resolve()
    decision = PathPolicy(mclaw_home=home).check("read", value)
    path = decision.resolved
    if not decision.allowed or (path.is_relative_to(home) and not _allowed_attachment(home, session_id, platform, path)):
        return {"success": False, "error": "此文件不能通过渠道发送"}
    if not path.is_file():
        return {"success": False, "error": "发送文件不存在或已移动"}
    caption = str(request.get("caption") or "")
    if len(caption) > 20000:
        return {"success": False, "error": "文件说明过长"}
    kwargs = {"file_path": str(path), "caption": caption}
    if platform == "weixin":
        kwargs["force_file_attachment"] = bool(request.get("as_file"))
    from mclaw.application.store import ApplicationStore
    receipts = ApplicationStore(home / "application.sqlite3")
    key = "channel_outbound:" + session_id + ":" + str(request.get("id") or "")
    previous = receipts.setting(key)
    if previous:
        receipts.close()
        return previous.get("result") or {"success": False, "error": "此发送结果尚未确认，请先核对渠道会话", "completion_unknown": True}
    receipts.set_setting(key, {"state": "sending", "created_at": time.time(), "platform": platform, "path": str(path)})
    current = asyncio.current_task()
    active = _ACTIVE_SENDS.setdefault(id(target.adapter), set())
    active.add(current)
    try:
        result = await asyncio.wait_for(target.adapter.send_file(target.chat_id, **kwargs), timeout=600)
        value = {"success": bool(result.success), "message_id": result.message_id,
                 "error": None if result.success else "渠道未确认发送成功，请检查目标会话后重试",
                 "completion_unknown": bool(getattr(result, "completion_unknown", False))}
    except asyncio.TimeoutError:
        value = {"success": False, "error": "渠道发送超时，请核对目标会话后再重试", "completion_unknown": True}
    except asyncio.CancelledError:
        receipts.set_setting(key, {"state": "unknown", "finished_at": time.time()})
        receipts.close()
        raise
    except Exception:
        value = {"success": False, "error": "渠道发送失败，请核对目标会话后重试", "completion_unknown": True}
    finally:
        active.discard(current)
        if not active:
            _ACTIVE_SENDS.pop(id(target.adapter), None)
    receipts.set_setting(key, {"state": "finished", "finished_at": time.time(), "result": value})
    receipts.close()
    return value


def _allowed_attachment(home: Path, session_id: str, platform: str, path: Path) -> bool:
    if path.is_relative_to(home / "attachments" / session_id):
        from mclaw.application.files import FileService
        from mclaw.application.sessions import ApplicationError
        parts = path.relative_to(home / "attachments" / session_id).parts
        try:
            record = FileService(home).get(session_id, parts[0])
            return record["source"] == "upload" and Path(record["path"]).resolve() == path
        except (ApplicationError, IndexError):
            return False
    from mclaw.application.store import ApplicationStore
    store = ApplicationStore(home / "application.sqlite3")
    try:
        receipts = store.setting("channel_pending:" + platform, {})
        return any(
            Path(item.get("path") or "").resolve() == path
            for record in receipts.values() if record.get("session_id") == session_id
            for item in record.get("attachments", [])
        )
    finally:
        store.close()


async def drain_channel_outbound(adapter, timeout: float = 5) -> bool:
    """Fence live RPC sends before the gateway removes its routing registrations."""
    tasks = set(_ACTIVE_SENDS.get(id(adapter), set()))
    for task in tasks:
        task.cancel()
    if not tasks:
        return True
    _done, pending = await asyncio.wait(tasks, timeout=timeout)
    return not pending
