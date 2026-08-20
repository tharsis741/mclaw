# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Weixin long-poll runtime orchestration.

The runtime wires account state, context-token persistence, iLink polling, and
the shared channel adapter into one private-chat gateway process.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time

from mclaw.channels.audio_transcription import build_inbound_pipeline
from mclaw.channels.runner import AgentRunner
from mclaw.channels.weixin.account_store import WeixinAccountStore
from mclaw.channels.weixin.adapter import WeixinAdapter
from mclaw.channels.weixin.command_router import WeixinCommandRouter
from mclaw.channels.weixin.config import WeixinConfig
from mclaw.channels.weixin.context_token_store import ContextTokenStore
from mclaw.channels.weixin.dedup import MessageDeduplicator
from mclaw.channels.weixin.ilink_client import ILinkClient
from mclaw.channels.weixin.outbound_registry import unregister_weixin_outbound_targets_for_adapter
from mclaw.channels.weixin.runtime_lock import WeixinRuntimeLock
from mclaw.providers.runtime import ProviderRuntimeContext
from mclaw.channels.weixin.session_router import WeixinSessionRouter
from mclaw.state import SessionDB

logger = logging.getLogger(__name__)


class WeixinRuntime:
    """Own the Weixin channel lifecycle, polling loop, and dispatch task set."""

    def __init__(
        self,
        *,
        config: dict,
        provider_runtime: ProviderRuntimeContext,
        session_db: SessionDB | None = None,
        account_store: WeixinAccountStore | None = None,
        token_store: ContextTokenStore | None = None,
        client: ILinkClient | None = None,
        runner: AgentRunner | None = None,
        session_router: WeixinSessionRouter | None = None,
        adapter: WeixinAdapter | None = None,
        runtime_lock: WeixinRuntimeLock | None = None,
    ) -> None:
        self.weixin_config = WeixinConfig.from_config(config)
        self._owns_session_db = session_db is None
        self.session_db = SessionDB() if session_db is None else session_db
        self.account_store = account_store or WeixinAccountStore()
        self.token_store = token_store or ContextTokenStore()
        self.client = client or ILinkClient(
            base_url=self.weixin_config.base_url,
            token=self.weixin_config.token,
            timeout_ms=self.weixin_config.api_timeout_ms,
        )
        self.runner = runner or AgentRunner(
            provider_runtime=provider_runtime,
            config=config,
            enabled_toolsets=self.weixin_config.toolsets,
            session_db=self.session_db,
            platform="weixin",
            inbound_pipeline=build_inbound_pipeline(config),
        )
        self.session_router = session_router or WeixinSessionRouter(
            account_id=self.weixin_config.account_id,
            session_db=self.session_db,
            scope=self.weixin_config.session_scope,
        )
        self.adapter = adapter or WeixinAdapter(
            config=self.weixin_config,
            client=self.client,
            runner=self.runner,
            session_router=self.session_router,
            token_store=self.token_store,
            command_router=WeixinCommandRouter(),
            dedup=MessageDeduplicator(ttl_seconds=self.weixin_config.dedup_ttl_seconds),
        )
        self.runtime_lock = runtime_lock
        self._dispatch_tasks: set[asyncio.Task] = set()
        self._running = False

    async def start(self) -> None:
        """Validate config, acquire the account lock, restore tokens, and poll forever."""
        errors = self.weixin_config.validate()
        if errors:
            raise RuntimeError("; ".join(errors))
        if self.runtime_lock is None:
            self.runtime_lock = WeixinRuntimeLock(
                account_id=self.weixin_config.account_id,
                token=self.weixin_config.token,
            )
        self.runtime_lock.acquire()
        self.token_store.restore(self.weixin_config.account_id)
        try:
            await self.client.open()
            self._running = True
            logger.info("weixin runtime started account=%s", self.weixin_config.account_id[:8])
            await self._poll_loop()
        except Exception:
            self.runtime_lock.release()
            raise

    async def stop(self) -> None:
        """Stop polling, drain active dispatch tasks, close iLink, and release state."""
        deadline = time.monotonic() + self.weixin_config.shutdown_timeout_seconds
        self._running = False
        interrupt_all = getattr(self.runner, "interrupt_all", None)
        if interrupt_all:
            interrupted = interrupt_all()
            if interrupted:
                logger.info("weixin runtime interrupted %d active session(s)", interrupted)
        tasks = [task for task in self._dispatch_tasks if not task.done()]
        if tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=max(0.001, deadline - time.monotonic()),
                )
            except asyncio.TimeoutError:
                logger.warning("weixin runtime shutdown timed out waiting for %d dispatch task(s)", len(tasks))
                for task in tasks:
                    task.cancel()
                remaining = max(0.0, deadline - time.monotonic())
                if remaining > 0:
                    try:
                        await asyncio.wait_for(
                            asyncio.gather(*tasks, return_exceptions=True),
                            timeout=remaining,
                        )
                    except asyncio.TimeoutError:
                        pass
        try:
            dispose_all = getattr(self.runner, "dispose_all", None)
            if callable(dispose_all):
                outcome = dispose_all(deadline=deadline, close_owned_session_db=False)
                if inspect.isawaitable(outcome):
                    await outcome
            remaining = max(0.0, deadline - time.monotonic())
            if remaining > 0:
                await asyncio.wait_for(self.client.close(), timeout=remaining)
        finally:
            unregister_weixin_outbound_targets_for_adapter(self.adapter)
            if self._owns_session_db and self.session_db is not None:
                close = getattr(self.session_db, "close_with_deadline", None)
                if callable(close):
                    close(deadline)
                self.session_db = None
            if self.runtime_lock is not None:
                self.runtime_lock.release()

    async def _poll_loop(self) -> None:
        """Run the iLink long-poll loop with conservative backoff after failures."""
        sync_buf = self.account_store.load_sync_buf(self.weixin_config.account_id)
        consecutive_failures = 0
        while self._running:
            try:
                sync_buf, ok = await self.poll_once(sync_buf)
                if not ok:
                    consecutive_failures += 1
                    await asyncio.sleep(30 if consecutive_failures >= 3 else 2)
                    if consecutive_failures >= 3:
                        consecutive_failures = 0
                    continue
                consecutive_failures = 0
            except asyncio.CancelledError:
                break
            except Exception as exc:
                consecutive_failures += 1
                logger.error("weixin poll error: %s", exc)
                await asyncio.sleep(30 if consecutive_failures >= 3 else 2)

    async def poll_once(self, sync_buf: str) -> tuple[str, bool]:
        """Poll one iLink batch, persist the new sync cursor, and fan out messages."""
        response = await self.client.get_updates(
            sync_buf,
            timeout_ms=self.weixin_config.poll_timeout_ms,
        )
        ret = response.get("ret", 0)
        errcode = response.get("errcode", 0)
        if ret not in {0, None} or errcode not in {0, None}:
            return sync_buf, False
        new_sync_buf = str(response.get("get_updates_buf") or "")
        if new_sync_buf:
            sync_buf = new_sync_buf
            self.account_store.save_sync_buf(self.weixin_config.account_id, sync_buf)
        for message in response.get("msgs") or []:
            # Message processing runs concurrently so long polling can continue.
            task = asyncio.create_task(self._dispatch_message(message))
            self._dispatch_tasks.add(task)
            task.add_done_callback(self._dispatch_tasks.discard)
        return sync_buf, True

    async def _dispatch_message(self, message: dict) -> None:
        """Run one inbound payload through the adapter without killing the poll loop."""
        try:
            await self.adapter.process_message(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("weixin message dispatch failed: %s", exc)

    async def run_forever(self) -> None:
        """Start the runtime and always run shutdown cleanup on exit."""
        try:
            await self.start()
        finally:
            await self.stop()
