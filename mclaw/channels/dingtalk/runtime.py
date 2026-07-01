# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""DingTalk Stream Mode runtime orchestration.

This module wires configuration, the Stream client, the adapter, session
routing, and the shared agent runner into one long-lived channel process.
"""

from __future__ import annotations

import asyncio
import logging

from mclaw.channels.dingtalk.adapter import DingTalkAdapter
from mclaw.channels.dingtalk.command_router import DingTalkCommandRouter
from mclaw.channels.dingtalk.config import DingTalkConfig
from mclaw.channels.dingtalk.dedup import MessageDeduplicator
from mclaw.channels.dingtalk.runtime_lock import DingTalkRuntimeLock
from mclaw.channels.dingtalk.session_router import DingTalkSessionRouter
from mclaw.channels.dingtalk.stream_client import DingTalkClient
from mclaw.channels.runner import AgentRunner
from mclaw.state import SessionDB

logger = logging.getLogger(__name__)


class DingTalkRuntime:
    """Own the DingTalk channel lifecycle and reconnect loop."""

    def __init__(
        self,
        *,
        config: dict,
        model: str,
        api_key: str,
        base_url: str = "",
        api_mode: str = "chat_completions",
        provider: str = "",
        session_db: SessionDB | None = None,
        client: DingTalkClient | None = None,
        runner: AgentRunner | None = None,
        session_router: DingTalkSessionRouter | None = None,
        adapter: DingTalkAdapter | None = None,
        runtime_lock: DingTalkRuntimeLock | None = None,
    ) -> None:
        self.dingtalk_config = DingTalkConfig.from_config(config)
        self.session_db = session_db or SessionDB()
        self.client = client or DingTalkClient(self.dingtalk_config)
        self.runner = runner or AgentRunner(
            model=model,
            api_key=api_key,
            base_url=base_url,
            api_mode=api_mode,
            provider=provider,
            config=config,
            enabled_toolsets=self.dingtalk_config.toolsets,
            session_db=self.session_db,
            platform="dingtalk",
        )
        self.session_router = session_router or DingTalkSessionRouter(
            account_id=self.dingtalk_config.client_id,
            session_db=self.session_db,
            scope=self.dingtalk_config.session_scope,
        )
        self.adapter = adapter or DingTalkAdapter(
            config=self.dingtalk_config,
            client=self.client,
            runner=self.runner,
            session_router=self.session_router,
            command_router=DingTalkCommandRouter(),
            dedup=MessageDeduplicator(ttl_seconds=self.dingtalk_config.dedup_ttl_seconds),
        )
        self.runtime_lock = runtime_lock
        self._stream_task: asyncio.Task | None = None
        self._running = False
        self._stopping = False
        self._stop_event: asyncio.Event | None = None

    async def start(self) -> None:
        """Validate config, acquire the bot lock, and start the stream task."""
        errors = self.dingtalk_config.validate()
        if errors:
            raise RuntimeError("; ".join(errors))
        if self.runtime_lock is None:
            self.runtime_lock = DingTalkRuntimeLock(client_id=self.dingtalk_config.client_id)
        self.runtime_lock.acquire()
        try:
            await self.client.open()
            self.client.register_handler(loop=asyncio.get_running_loop(), handler=self.adapter.process_message)
            self._running = True
            self._stop_event = asyncio.Event()
            self._stream_task = asyncio.create_task(self._stream_loop())
            logger.info("dingtalk runtime started client=%s", self.dingtalk_config.client_id[:8])
        except Exception:
            self.runtime_lock.release()
            raise

    async def stop(self) -> None:
        """Stop the stream loop, interrupt active turns, close clients, and release state."""
        if self._stopping:
            return
        self._stopping = True
        self._running = False
        if self._stop_event is not None:
            self._stop_event.set()
        try:
            interrupt_all = getattr(self.runner, "interrupt_all", None)
            interrupted = 0
            if interrupt_all:
                interrupted = interrupt_all()
            if interrupted:
                logger.info("dingtalk runtime interrupted %d active session(s)", interrupted)
            close_task: asyncio.Task | None = None
            try:
                # Start client close before cancelling the stream task to unblock SDK waits.
                close_task = asyncio.create_task(self.client.close())
            except RuntimeError:
                close_task = None
            if self._stream_task:
                if self._stream_task.get_loop().is_closed():
                    self._stream_task = None
                else:
                    self._stream_task.cancel()
                    try:
                        await asyncio.wait_for(self._stream_task, timeout=self.dingtalk_config.shutdown_timeout_seconds)
                    except (asyncio.CancelledError, asyncio.TimeoutError):
                        pass
                    self._stream_task = None
            if close_task is not None:
                try:
                    await asyncio.wait_for(close_task, timeout=self.dingtalk_config.close_timeout_seconds)
                except asyncio.TimeoutError:
                    logger.warning("dingtalk client close timed out")
        finally:
            clear_state = getattr(self.adapter, "clear_runtime_state", None)
            if clear_state:
                clear_state()
            if self.runtime_lock is not None:
                self.runtime_lock.release()
            self._stop_event = None
            self._stopping = False

    async def _stream_loop(self) -> None:
        """Run the Stream client with bounded reconnect backoff until stopped."""
        backoff_idx = 0
        while self._running:
            try:
                await self.client.start_stream()
                backoff_idx = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self._running:
                    return
                delay = self.dingtalk_config.reconnect_backoff_seconds[
                    min(backoff_idx, len(self.dingtalk_config.reconnect_backoff_seconds) - 1)
                ]
                logger.warning("dingtalk stream error: %s; reconnecting in %.1fs", exc, delay)
                stop_event = self._stop_event
                if stop_event is None:
                    await asyncio.sleep(delay)
                else:
                    try:
                        await asyncio.wait_for(stop_event.wait(), timeout=delay)
                        return
                    except asyncio.TimeoutError:
                        pass
                backoff_idx += 1

    async def run_forever(self) -> None:
        """Start the runtime and keep it alive until the stream task exits."""
        try:
            await self.start()
            while self._stream_task and not self._stream_task.done():
                await asyncio.sleep(1)
            if self._stream_task:
                await self._stream_task
        finally:
            await asyncio.shield(self.stop())
