"""Delivery service for scheduler run final responses."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
import uuid
from typing import Any, Callable

from mclaw.scheduler.models import DeliverySpec, SchedulerJob, SchedulerRun
from mclaw.scheduler.store import SchedulerStore
from mclaw.scheduler.targets import TargetManager

logger = logging.getLogger(__name__)


class DeliveryService:
    def __init__(
        self,
        *,
        store: SchedulerStore,
        dingtalk_client: Any = None,
        weixin_client: Any = None,
        weixin_token_store: Any = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.store = store
        self.dingtalk_client = dingtalk_client
        self.weixin_client = weixin_client
        self.weixin_token_store = weixin_token_store
        self.sleep = sleep
        self.target_manager = TargetManager()

    def deliver(self, job: SchedulerJob, run: SchedulerRun, content: str) -> dict[str, Any]:
        return self.deliver_one(job.delivery, job, run, content)

    def deliver_one(self, spec: DeliverySpec, job: SchedulerJob, run: SchedulerRun, content: str) -> dict[str, Any]:
        target = self.store.get_target(spec.target_id)
        if not target:
            return {"success": False, "status": "target_missing", "error": f"target not found: {spec.target_id}"}
        target = self.target_manager.validate_target(target)
        if not target.enabled or target.route_status != "ready":
            return {
                "success": False,
                "status": target.route_status,
                "target_id": target.id,
                "target_type": target.type,
                "error": f"target is not deliverable: {target.route_status}",
            }
        if target.type == "local":
            return {
                "success": True,
                "status": "local",
                "target_id": target.id,
                "target_type": target.type,
                "output_path": run.output_path,
            }

        retry_count = max(1, int(spec.retry_count or 1))
        delays = [0, 2, 5, 10]
        attempts: list[dict[str, Any]] = []
        last: dict[str, Any] = {}
        for attempt_no in range(1, retry_count + 1):
            if attempt_no > 1:
                self.sleep(delays[min(attempt_no - 1, len(delays) - 1)])
            try:
                last = self._deliver_external(target, job, run, content)
            except Exception as exc:
                logger.exception("scheduler delivery attempt failed: %s", exc)
                last = {"success": False, "status": "failed", "error": str(exc)}
            attempt = {"attempt": attempt_no, **last}
            attempts.append(attempt)
            if last.get("success"):
                return {
                    **last,
                    "success": True,
                    "status": "sent",
                    "target_id": target.id,
                    "target_type": target.type,
                    "attempts": attempts,
                }
        return {
            **last,
            "success": False,
            "status": "failed",
            "target_id": target.id,
            "target_type": target.type,
            "attempts": attempts,
            "error": last.get("error") or "delivery failed",
        }

    def _deliver_external(self, target, job: SchedulerJob, run: SchedulerRun, content: str) -> dict[str, Any]:
        if target.type == "dingtalk_group":
            if self.dingtalk_client is None:
                return {"success": False, "error": "DingTalk client is not configured"}
            _ensure_client_open(self.dingtalk_client)
            result = _await_if_needed(
                self.dingtalk_client.send_group_robot_message(
                    open_conversation_id=target.route_metadata.get("open_conversation_id", ""),
                    msg_key="sampleMarkdown",
                    msg_param={"title": job.name or "M-Claw Scheduler", "text": content},
                )
            )
            return _send_result_dict(result)

        if target.type == "dingtalk_private":
            if self.dingtalk_client is None:
                return {"success": False, "error": "DingTalk client is not configured"}
            _ensure_client_open(self.dingtalk_client)
            result = _await_if_needed(
                self.dingtalk_client.send_oto_robot_message(
                    user_id=target.route_metadata.get("sender_staff_id", ""),
                    msg_key="sampleMarkdown",
                    msg_param={"title": job.name or "M-Claw Scheduler", "text": content},
                )
            )
            return _send_result_dict(result)

        if target.type == "weixin_private":
            return self._deliver_weixin(target, content)

        return {"success": False, "error": f"unsupported target type: {target.type}"}

    def _deliver_weixin(self, target, content: str) -> dict[str, Any]:
        if self.weixin_client is None:
            return {"success": False, "error": "Weixin client is not configured"}
        _ensure_client_open(self.weixin_client)
        context_token = target.route_metadata.get("context_token")
        if not context_token and self.weixin_token_store is not None:
            context_token = self.weixin_token_store.get(target.account_id, target.chat_id)
        client_id = f"mclaw-scheduler-{uuid.uuid4().hex}"
        response = _await_if_needed(
            self.weixin_client.send_text(
                to_user_id=target.chat_id,
                text=content,
                client_id=client_id,
                context_token=context_token,
            )
        )
        success, error, stale = _weixin_response_state(response)
        if success:
            return {"success": True, "message_id": client_id, "response": response}
        if stale and context_token:
            if self.weixin_token_store is not None:
                self.weixin_token_store.clear(target.account_id, target.chat_id)
            response = _await_if_needed(
                self.weixin_client.send_text(
                    to_user_id=target.chat_id,
                    text=content,
                    client_id=f"mclaw-scheduler-{uuid.uuid4().hex}",
                    context_token=None,
                )
            )
            success, error, _stale = _weixin_response_state(response)
            if success:
                return {"success": True, "message_id": client_id, "response": response, "stale_token_retried": True}
        return {"success": False, "error": error, "response": response}


def _await_if_needed(value: Any) -> Any:
    if not inspect.isawaitable(value):
        return value
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(value)

    async def _runner(awaitable):
        return await awaitable

    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(lambda: asyncio.run(_runner(value)))
        return future.result()


def _ensure_client_open(client: Any) -> None:
    opener = getattr(client, "open", None)
    if opener is None:
        return
    if getattr(client, "http", None) is not None or getattr(client, "_client", None) is not None:
        return
    _await_if_needed(opener())


def _send_result_dict(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        return {"success": bool(result.get("success")), **result}
    success = bool(getattr(result, "success", False))
    payload = {"success": success}
    message_id = getattr(result, "message_id", None)
    error = getattr(result, "error", None)
    if message_id:
        payload["message_id"] = message_id
    if error:
        payload["error"] = error
    return payload


def _weixin_response_state(response: Any) -> tuple[bool, str, bool]:
    if not isinstance(response, dict):
        return False, str(response), False
    ret = response.get("ret", response.get("errcode", 0))
    errmsg = str(response.get("errmsg") or response.get("message") or "")
    try:
        code = int(ret or 0)
    except (TypeError, ValueError):
        code = 0
    success = code == 0
    stale = code == -14 or (code == -2 and errmsg.strip().lower() == "unknown error")
    return success, errmsg or f"ret={ret}", stale
