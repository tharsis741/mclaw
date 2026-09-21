# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Channel presentation over the application's durable session queue."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
import hashlib
import json
import time
from types import SimpleNamespace

from mclaw.application.sessions import ApplicationError
from mclaw.channels.base import AgentTurnResult, ChannelMessage, ChannelSource
from mclaw.channels.inbound_pipeline import InboundCapabilityError
from mclaw.channels.runner import AgentRunner, IngressQueueFullError


class SharedChannelRunner(AgentRunner):
    """Reuse channel ingestion while SessionService alone schedules Agent turns."""

    def __init__(self, sessions, *, platform: str, config: dict, toolsets: list[str], account_id: str = "", inbound_pipeline=None):
        model = config.get("model") if isinstance(config.get("model"), str) else ""
        super().__init__(provider_runtime=SimpleNamespace(model=model), config=config,
                         enabled_toolsets=toolsets, session_db=sessions.history,
                         platform=platform, inbound_pipeline=inbound_pipeline)
        self.sessions = sessions
        self.account_id = account_id
        self._deliveries: dict[str, asyncio.Task] = {}
        self._delivery_sessions: dict[str, str] = {}
        self._control_errors: dict[str, str] = {}
        self._stopping = False
        self._activated = False
        self.receipt_key = "channel_pending:" + platform

    def _receipts(self) -> dict:
        return {key: record for key, record in self.sessions.store.setting(self.receipt_key, {}).items()
                if (record.get("source") or {}).get("account_id", "") == self.account_id}

    def _receipt(self, task_id: str, record: dict | None) -> None:
        values = self.sessions.store.setting(self.receipt_key, {})
        if record is None:
            values.pop(task_id, None)
        else:
            values[task_id] = record
        self.sessions.store.set_setting(self.receipt_key, values)

    def get_status(self, session_id: str) -> str:
        try:
            return self.sessions.snapshot(session_id)["status"]
        except ApplicationError:
            return "idle"

    def control_error(self, session_id: str) -> str:
        return self._control_errors.pop(session_id, "")

    def interrupt(self, session_id: str) -> bool:
        """A channel control request respects the same browser/TUI ownership lease."""
        client = "channel:" + self.platform
        try:
            snapshot = self.sessions.snapshot(session_id)
            had_work = bool(snapshot["active_task"] or snapshot["queue"])
            self.sessions.stop(session_id, client)
            self.sessions.release(session_id, client)
            self._cancel_ingress(session_id)
            self._control_errors.pop(session_id, None)
            return had_work
        except ApplicationError as exc:
            self._control_errors[session_id] = str(exc)
            return False

    def reset_session(self, session_id: str) -> bool:
        return self.interrupt(session_id)

    def session_replacement_block_reason(self, session_id: str) -> str | None:
        if session_id in self._control_errors:
            return self.control_error(session_id)
        if self.get_status(session_id) in {"running", "waiting", "stopping"}:
            return "原任务仍在收尾，请稍后再新建会话。"
        return None

    def interrupt_all(self) -> int:
        """Pause accepted messages durably without changing browser leases."""
        self._stopping = True
        if not self._activated:
            return 0
        by_session: dict[str, list[str]] = {}
        for task_id, record in self._receipts().items():
            by_session.setdefault(record["session_id"], []).append(task_id)
        interrupted = 0
        for session_id, task_ids in by_session.items():
            try:
                self.sessions.pause_background(session_id, task_ids)
                interrupted += len(task_ids)
            except ApplicationError:
                pass
        return interrupted

    async def dispose_all(self, *, deadline=None, close_owned_session_db=False):
        self.interrupt_all()
        tasks = list(self._deliveries.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._deliveries.clear()
        self._delivery_sessions.clear()
        return True

    async def handle_message(self, *, message: ChannelMessage, session_id: str,
                             conversation_history=None, reply_callback, extra_system="",
                             ingress_reservation=None, **_kwargs) -> AgentTurnResult:
        if self._stopping:
            self.release_ingress(ingress_reservation)
            return AgentTurnResult(session_id, error="渠道正在停止，请稍后重试")
        self._activated = True
        processed = message
        try:
            if ingress_reservation is not None and not await self.await_ingress(ingress_reservation):
                return AgentTurnResult(session_id, interrupted=True)
            self.sessions.prepare_background_session(session_id)
            before = self.sessions.snapshot(session_id)
            if len(before["queue"]) >= self.max_pending_messages:
                raise IngressQueueFullError("当前会话消息积压过多，请稍后重试。")
            processed = await self._process_inbound_message(message)
            source = processed.source
            identity = json.dumps([source.channel, source.account_id, source.chat_id, source.message_id], ensure_ascii=False)
            if not source.message_id:
                identity += processed.timestamp.isoformat() + processed.text
            request_id = "channel:" + hashlib.sha256(identity.encode()).hexdigest()
            submission = self.sessions.submit_background(session_id, processed.text, request_id, {
                "platform": self.platform, "extra_system": extra_system, "enabled_toolsets": self.enabled_toolsets,
                "channel_toolsets": True,
            })
            task_id = submission["task_id"]
            receipt = self._receipts().get(task_id)
            if self.sessions.store.setting("channel_delivered:" + task_id):
                return AgentTurnResult(session_id, queued=True)
            if receipt is None:
                receipt = {"session_id": session_id, "source": asdict(source), "raw_message": processed.raw_message,
                           "attachments": [item.to_dict() for item in processed.attachments],
                           "text": processed.text, "state": "pending", "created_at": time.time(), "last_result": ""}
                self._receipt(task_id, receipt)
            if task_id in self._deliveries:
                return AgentTurnResult(session_id, queued=True)
            first = asyncio.get_running_loop().create_future()
            watcher = self._start_delivery(task_id, processed, reply_callback, first)
        except (ApplicationError, InboundCapabilityError, IngressQueueFullError) as exc:
            result = AgentTurnResult(session_id, error=getattr(exc, "safe_message", str(exc)))
            await reply_callback(processed, result)
            return result
        finally:
            self.release_ingress(ingress_reservation)
        if before["active_task"] or before["queue"] or before["paused"]:
            return AgentTurnResult(session_id, queued=True)
        try:
            return await asyncio.shield(first)
        except asyncio.CancelledError:
            # The durable task still belongs to the service; gateway shutdown explicitly stops it.
            return AgentTurnResult(session_id, interrupted=True)

    def _start_delivery(self, task_id, message, reply_callback, first=None):
        self._delivery_sessions[task_id] = self._receipts()[task_id]["session_id"]
        watcher = asyncio.create_task(self._watch(task_id, message, reply_callback, first))
        self._deliveries[task_id] = watcher
        watcher.add_done_callback(lambda _task: self._deliveries.pop(task_id, None))
        return watcher

    async def _watch(self, task_id, message, reply_callback, first):
        sid = self._delivery_sessions[task_id]
        try:
            while not self._stopping:
                record = self._receipts().get(task_id)
                if record is None:
                    return
                result = self.sessions.task_result(sid, task_id)
                if result is None:
                    await asyncio.sleep(0.1)
                    continue
                signature = hashlib.sha256(json.dumps(result, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                outcome = AgentTurnResult(sid, final_response=str(result.get("final_response") or ""),
                    error=result.get("error"), interrupted=bool(result.get("interrupted")), raw_result=result)
                if signature != record.get("last_result"):
                    if record["state"] == "sending":
                        # A prior process may have sent this already. Leave an explicit uncertain receipt.
                        record.update(state="unknown", last_result=signature)
                        self._receipt(task_id, record)
                    else:
                        record.update(state="sending")
                        self._receipt(task_id, record)
                        try:
                            sent = await reply_callback(message, outcome)
                            state = "failed" if sent is not None and not getattr(sent, "success", True) else "sent"
                        except Exception:
                            state = "unknown"
                        record.update(state=state, last_result=signature, finished_at=time.time())
                        self._receipt(task_id, record)
                if first is not None and not first.done():
                    first.set_result(outcome)
                if result.get("completed") and result.get("status") == "completed":
                    self.sessions.store.set_setting("channel_delivered:" + task_id, {
                        "state": record["state"], "finished_at": time.time(), "session_id": sid,
                    })
                    self._receipt(task_id, None)
                    return
                await asyncio.sleep(0.2)
        finally:
            self._delivery_sessions.pop(task_id, None)
            if first is not None and not first.done():
                first.set_result(AgentTurnResult(sid, interrupted=True))

    def restore_deliveries(self, adapter) -> None:
        """Restore callbacks only; recovered session queues stay paused until user continuation."""
        self._activated = True
        from mclaw.channels.dingtalk.outbound_registry import register_dingtalk_outbound_target
        from mclaw.channels.weixin.outbound_registry import register_weixin_outbound_target
        registrar = register_weixin_outbound_target if self.platform == "weixin" else register_dingtalk_outbound_target
        for task_id, record in self._receipts().items():
            if task_id in self._deliveries:
                continue
            source = ChannelSource(**record["source"])
            message = ChannelMessage(record["text"], source, raw_message=record.get("raw_message", {}))
            registrar(session_id=record["session_id"], adapter=adapter, chat_id=source.chat_id, loop=asyncio.get_running_loop())
            self._start_delivery(task_id, message, adapter._reply_callback)


class PersistentChannelRouter:
    """Retain the existing router's /new bindings across gateway reconnects."""

    def __init__(self, router, store, platform: str) -> None:
        self.router, self.store = router, store
        self.key = "channel_routes:" + platform + ":" + hashlib.sha256(router.account_id.encode()).hexdigest()[:16]
        router._overrides.update(store.setting(self.key, {}))

    def route(self, *args, **kwargs):
        result = self.router.route(*args, **kwargs)
        if self.store.setting(f"deleted_session:{result.session_id}", False):
            # Native routers can recreate a deterministic ID. Rotate that binding
            # so a newly received message starts a genuinely new conversation.
            self.router.session_db.delete_session(result.session_id)
            return self.new_session(*args, **kwargs)
        return result

    def new_session(self, *args, **kwargs):
        result = self.router.new_session(*args, **kwargs)
        self.store.set_setting(self.key, self.router._overrides)
        return result
