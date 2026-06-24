# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Adapt Weixin private-chat events into normalized channel messages.

The adapter isolates Weixin-specific payload parsing and media extraction from
session routing and shared agent execution.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import time
import uuid
from typing import Any

from mclaw.channels.base import AgentTurnResult, ChannelMessage, ChannelSource, SendResult
from mclaw.channels.runner import AgentRunner
from mclaw.channels.weixin.command_router import WeixinCommandRouter
from mclaw.channels.weixin.config import WeixinConfig
from mclaw.channels.weixin.context_token_store import ContextTokenStore
from mclaw.channels.weixin.dedup import MessageDeduplicator
from mclaw.channels.weixin.formatter import split_text_for_weixin
from mclaw.channels.weixin.ilink_client import ILinkClient
from mclaw.channels.weixin.media import (
    ITEM_FILE,
    ITEM_IMAGE,
    ITEM_VIDEO,
    ITEM_VOICE,
    WeixinMediaCache,
    format_media_for_agent,
    has_media_items,
    media_dedup_key,
)
from mclaw.channels.weixin.outbound_media import (
    aes128_ecb_encrypt,
    aes_key_for_api,
    aes_padded_size,
    build_upload_payload,
    prepare_outbound_media,
    upload_url_from_response,
)
from mclaw.channels.weixin.outbound_registry import register_weixin_outbound_target
from mclaw.channels.weixin.session_router import WeixinSessionRouter
from mclaw.prompts.channels import build_channel_context
from mclaw.scheduler.store import SchedulerStore
from mclaw.scheduler.targets import bind_pairing_from_channel, parse_schedule_bind_command

logger = logging.getLogger(__name__)

ITEM_TEXT = 1
SESSION_EXPIRED_ERRCODE = -14
RATE_LIMIT_ERRCODE = -2


def _safe_id(value: str | None, keep: int = 8) -> str:
    raw = str(value or "")
    if len(raw) <= keep:
        return raw or "?"
    return raw[:keep]


def _is_stale_session_ret(ret: Any, errcode: Any, errmsg: Any) -> bool:
    """iLink sometimes reports stale context_token as -2 unknown error."""
    if ret != RATE_LIMIT_ERRCODE and errcode != RATE_LIMIT_ERRCODE:
        return False
    return str(errmsg or "").strip().lower() == "unknown error"


class TypingTicketCache:
    def __init__(self, ttl_seconds: float = 600.0) -> None:
        self.ttl_seconds = ttl_seconds
        self._cache: dict[str, tuple[str, float]] = {}

    def get(self, user_id: str) -> str | None:
        entry = self._cache.get(user_id)
        if not entry:
            return None
        ticket, ts = entry
        if time.time() - ts >= self.ttl_seconds:
            self._cache.pop(user_id, None)
            return None
        return ticket

    def set(self, user_id: str, ticket: str) -> None:
        if ticket:
            self._cache[user_id] = (ticket, time.time())


def _extract_text(item_list: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for item in item_list:
        if item.get("type") == ITEM_TEXT:
            text = str((item.get("text_item") or {}).get("text") or "")
            ref = item.get("ref_msg") or {}
            ref_item = ref.get("message_item") or {}
            ref_type = ref_item.get("type") if isinstance(ref_item, dict) else None
            if ref_type in {ITEM_IMAGE, ITEM_VIDEO, ITEM_FILE, ITEM_VOICE}:
                title = str(ref.get("title") or "")
                prefix = f"[Quoted Weixin media: {title}]\n" if title else "[Quoted Weixin media]\n"
                text = f"{prefix}{text}".strip()
            if text:
                parts.append(text)
    if parts:
        return "\n".join(parts).strip()
    for item in item_list:
        if item.get("type") == ITEM_VOICE:
            text = str((item.get("voice_item") or {}).get("text") or "")
            if text:
                return text.strip()
    return "\n".join(parts).strip()


def _guess_chat_type(message: dict[str, Any], account_id: str) -> tuple[str, str]:
    room_id = str(message.get("room_id") or message.get("chat_room_id") or "").strip()
    to_user_id = str(message.get("to_user_id") or "").strip()
    if room_id:
        return "group", room_id
    if to_user_id and account_id and to_user_id != account_id and message.get("msg_type") == 1:
        return "group", to_user_id
    return "dm", str(message.get("from_user_id") or "").strip()


class WeixinAdapter:
    def __init__(
        self,
        *,
        config: WeixinConfig,
        client: ILinkClient,
        runner: AgentRunner,
        session_router: WeixinSessionRouter,
        token_store: ContextTokenStore,
        command_router: WeixinCommandRouter | None = None,
        dedup: MessageDeduplicator | None = None,
    ) -> None:
        self.config = config
        self.client = client
        self.runner = runner
        self.session_router = session_router
        self.token_store = token_store
        self.command_router = command_router or WeixinCommandRouter()
        self.dedup = dedup or MessageDeduplicator(ttl_seconds=config.dedup_ttl_seconds)
        self.typing_cache = TypingTicketCache()
        self.media_cache = WeixinMediaCache(config=config, client=client)

    def is_dm_allowed(self, sender_id: str) -> bool:
        if self.config.dm_policy == "disabled":
            return False
        if self.config.dm_policy == "allowlist":
            return sender_id in self.config.allow_from
        return True

    async def process_message(self, message: dict[str, Any]) -> AgentTurnResult | None:
        sender_id = str(message.get("from_user_id") or "").strip()
        if not sender_id or sender_id == self.config.account_id:
            return None

        item_list = message.get("item_list") or []
        if not isinstance(item_list, list):
            item_list = []
        text = _extract_text(item_list)
        contains_media = has_media_items(item_list)
        if not text and not contains_media:
            return None

        message_id = str(message.get("message_id") or "").strip()
        chat_type, chat_id = _guess_chat_type(message, self.config.account_id)
        logger.info(
            "weixin: inbound message id=%s sender=%s chat_type=%s len=%d media=%s",
            _safe_id(message_id),
            _safe_id(sender_id),
            chat_type,
            len(text),
            contains_media,
        )
        if chat_type != "dm":
            logger.debug("weixin: ignoring non-dm message chat_id=%s", chat_id)
            return None
        if not self.is_dm_allowed(sender_id):
            logger.info("weixin: dm from %s rejected by policy", sender_id[:8])
            return None

        if message_id and self.dedup.is_duplicate(f"id:{message_id}"):
            logger.info("weixin: duplicate message ignored id=%s sender=%s", _safe_id(message_id), _safe_id(sender_id))
            return None
        if not message_id:
            fallback_key = (
                self.dedup.content_key(sender_id, f"{text}\n{media_dedup_key(sender_id, item_list)}")
                if contains_media
                else self.dedup.content_key(sender_id, text)
            )
            if self.dedup.is_duplicate(fallback_key):
                logger.info(
                    "weixin: duplicate fallback content ignored sender=%s len=%d media=%s",
                    _safe_id(sender_id),
                    len(text),
                    contains_media,
                )
                return None

        context_token = str(message.get("context_token") or "").strip()
        if context_token:
            self.token_store.set(self.config.account_id, sender_id, context_token)
        await self._maybe_fetch_typing_ticket(sender_id, context_token or None)

        bind_result = await self._maybe_handle_schedule_bind(
            text=text,
            chat_id=chat_id,
            sender_id=sender_id,
            context_token=context_token,
        )
        if bind_result is not None:
            return bind_result

        source = ChannelSource(
            channel="weixin",
            chat_id=chat_id,
            chat_type=chat_type,
            user_id=sender_id,
            user_name=sender_id,
            message_id=message_id,
            account_id=self.config.account_id,
        )
        routed = self.session_router.route(source, model=self.runner.model)

        command = self.command_router.handle(text, status=self.runner.get_status(routed.session_id))
        if command.handled:
            logger.info(
                "weixin: command action=%s session=%s sender=%s",
                command.action,
                routed.session_id,
                _safe_id(sender_id),
            )
            if command.action == "new":
                reset = getattr(self.runner, "reset_session", None)
                if reset:
                    reset(routed.session_id)
                routed = self.session_router.new_session(source, model=self.runner.model)
                await self.send(chat_id, command.text)
            elif command.action == "stop":
                interrupted = self.runner.interrupt(routed.session_id)
                await self.send(chat_id, "Interrupted current turn." if interrupted else "No running turn.")
            else:
                await self.send(chat_id, command.text)
            return AgentTurnResult(session_id=routed.session_id)

        register_weixin_outbound_target(
            session_id=routed.session_id,
            adapter=self,
            chat_id=chat_id,
            loop=asyncio.get_running_loop(),
        )
        attachments = await self.media_cache.collect(item_list, message_id=message_id) if contains_media else []
        media_context = format_media_for_agent(attachments)
        if contains_media and not media_context and not self.config.media_cache_enabled:
            media_context = "## Weixin Attachments\nMedia attachment(s) were present, but media caching is disabled."
        if media_context:
            text = f"{text}\n\n{media_context}".strip() if text else f"User sent Weixin attachment(s).\n\n{media_context}"
        if attachments:
            message = {**message, "_mclaw_media": [attachment.to_dict() for attachment in attachments]}
        channel_message = ChannelMessage(text=text, source=source, raw_message=message)

        await self.send_typing(chat_id)
        try:
            history = self.runner.session_db.get_messages_as_conversation(routed.session_id)
            logger.info("weixin: agent turn start session=%s history=%d", routed.session_id, len(history))
            bind_events = getattr(self.runner, "bind_session_events", None)
            if bind_events:
                bind_events(
                    session_id=routed.session_id,
                    loop=asyncio.get_running_loop(),
                    callback=self._agent_event_callback(chat_id),
                )
            result = await self.runner.handle_message(
                message=channel_message,
                session_id=routed.session_id,
                conversation_history=history,
                reply_callback=self._reply_callback,
                extra_system=self._session_context_text(source),
            )
            if result.queued:
                await self.send(chat_id, "Previous request is still running; queued the latest message.")
                logger.info("weixin: message queued session=%s", routed.session_id)
            else:
                logger.info(
                    "weixin: agent turn done session=%s error=%s final_len=%d",
                    routed.session_id,
                    bool(result.error),
                    len(result.final_response or ""),
                )
            return result
        finally:
            await self.stop_typing(chat_id)

    async def _maybe_handle_schedule_bind(
        self,
        *,
        text: str,
        chat_id: str,
        sender_id: str,
        context_token: str,
    ) -> AgentTurnResult | None:
        command = parse_schedule_bind_command(text)
        if command is None:
            return None
        result = bind_pairing_from_channel(
            store=SchedulerStore(self.runner.session_db),
            code=command.code,
            display_name=command.display_name,
            target_type="weixin_private",
            account_id=self.config.account_id,
            chat_id=chat_id or sender_id,
            chat_type="private",
            route_metadata={"context_token": context_token} if context_token else {},
        )
        await self.send(chat_id or sender_id, result.message)
        return AgentTurnResult(session_id="scheduler-bind", final_response=result.message)

    def _agent_event_callback(self, chat_id: str):
        async def _callback(_session_id: str, event: dict[str, Any]) -> None:
            if event.get("type") != "skills_saved":
                return
            text = str(event.get("text") or event.get("summary") or "").strip()
            if text:
                await self.send(chat_id, text)

        return _callback

    async def _reply_callback(self, message: ChannelMessage, result: AgentTurnResult) -> None:
        if result.error:
            await self.send(message.source.chat_id, f"Error: {result.error}")
            return
        text = result.final_response.strip()
        if text:
            await self.send(message.source.chat_id, text)

    def _session_context_text(self, source: ChannelSource) -> str:
        return build_channel_context("weixin", user_id=source.user_id or "")

    async def send(self, chat_id: str, content: str) -> SendResult:
        context_token = self.token_store.get(self.config.account_id, chat_id)
        last_message_id: str | None = None
        chunks = split_text_for_weixin(content, self.config.max_message_length)
        try:
            for idx, chunk in enumerate(chunks):
                client_id = f"mclaw-weixin-{uuid.uuid4().hex}"
                context_token = await self._send_text_chunk(
                    chat_id=chat_id,
                    chunk=chunk,
                    context_token=context_token,
                    client_id=client_id,
                )
                logger.info(
                    "weixin: sent text chunk to=%s len=%d token=%s",
                    _safe_id(chat_id),
                    len(chunk),
                    bool(context_token),
                )
                last_message_id = client_id
                if idx < len(chunks) - 1 and self.config.send_chunk_delay_seconds > 0:
                    await asyncio.sleep(self.config.send_chunk_delay_seconds)
            return SendResult(success=True, message_id=last_message_id)
        except Exception as exc:
            logger.error("weixin send failed to=%s: %s", chat_id[:8], exc)
            return SendResult(success=False, error=str(exc))

    async def send_file(
        self,
        chat_id: str,
        *,
        file_path: str,
        caption: str = "",
        force_file_attachment: bool = False,
    ) -> SendResult:
        path = Path(file_path).expanduser().resolve()
        if not path.is_file():
            return SendResult(success=False, error=f"File not found: {file_path}")
        try:
            plaintext = path.read_bytes()
            if len(plaintext) > self.config.media_max_bytes:
                return SendResult(
                    success=False,
                    error=f"file exceeds max size ({len(plaintext)} > {self.config.media_max_bytes} bytes)",
                )
            media_type, item_builder = prepare_outbound_media(path, force_file_attachment=force_file_attachment)
            upload_payload = build_upload_payload(path, plaintext)
            ciphertext = aes128_ecb_encrypt(plaintext, upload_payload["aes_key"])
            upload_response = await self.client.get_upload_url(
                to_user_id=chat_id,
                media_type=media_type,
                filekey=upload_payload["filekey"],
                rawsize=upload_payload["rawsize"],
                rawfilemd5=upload_payload["rawfilemd5"],
                filesize=aes_padded_size(upload_payload["rawsize"]),
                aeskey_hex=upload_payload["aes_key"].hex(),
            )
            upload_url = upload_url_from_response(
                cdn_base_url=self.config.media_cdn_base_url,
                upload_response=upload_response,
                filekey=upload_payload["filekey"],
            )
            encrypted_query_param = await self.client.upload_bytes(
                upload_url,
                ciphertext,
                timeout_ms=int(self.config.media_upload_timeout_seconds * 1000),
            )
            media_item = item_builder(
                encrypt_query_param=encrypted_query_param,
                aes_key_for_api=aes_key_for_api(upload_payload["aes_key"]),
                ciphertext_size=len(ciphertext),
                plaintext_size=upload_payload["rawsize"],
                filename=path.name,
                rawfilemd5=upload_payload["rawfilemd5"],
            )

            last_message_id: str | None = None
            if caption:
                caption_result = await self.send(chat_id, caption)
                if not caption_result.success:
                    return caption_result
                last_message_id = caption_result.message_id

            context_token = self.token_store.get(self.config.account_id, chat_id)
            last_message_id = f"mclaw-weixin-{uuid.uuid4().hex}"
            response = await self.client.send_item(
                to_user_id=chat_id,
                item=media_item,
                client_id=last_message_id,
                context_token=context_token,
            )
            ret = response.get("ret")
            errcode = response.get("errcode")
            if ret not in {None, 0} or errcode not in {None, 0}:
                return SendResult(success=False, error=f"iLink sendmessage error: ret={ret} errcode={errcode}")
            logger.info("weixin: sent media file to=%s path=%s", _safe_id(chat_id), path.name)
            return SendResult(success=True, message_id=last_message_id)
        except Exception as exc:
            logger.error("weixin media send failed to=%s path=%s: %s", _safe_id(chat_id), path, exc)
            return SendResult(success=False, error=str(exc))

    async def _send_text_chunk(
        self,
        *,
        chat_id: str,
        chunk: str,
        context_token: str | None,
        client_id: str,
    ) -> str | None:
        last_error: Exception | None = None
        retried_without_token = False
        for attempt in range(self.config.send_chunk_retries + 1):
            try:
                response = await self.client.send_text(
                    to_user_id=chat_id,
                    text=chunk,
                    client_id=client_id,
                    context_token=context_token,
                )
                ret = response.get("ret")
                errcode = response.get("errcode")
                errmsg = response.get("errmsg") or response.get("error_message") or response.get("message")
                if ret in {None, 0} and errcode in {None, 0}:
                    return context_token
                if (
                    (
                        ret == SESSION_EXPIRED_ERRCODE
                        or errcode == SESSION_EXPIRED_ERRCODE
                        or _is_stale_session_ret(ret, errcode, errmsg)
                    )
                    and context_token
                    and not retried_without_token
                ):
                    logger.info("weixin: clearing stale context_token for peer=%s", _safe_id(chat_id))
                    retried_without_token = True
                    context_token = None
                    self.token_store.clear(self.config.account_id, chat_id)
                    continue
                if ret == RATE_LIMIT_ERRCODE or errcode == RATE_LIMIT_ERRCODE:
                    if attempt < self.config.send_chunk_retries:
                        await asyncio.sleep(self.config.send_chunk_retry_delay_seconds * 3)
                        continue
                raise RuntimeError(f"iLink sendmessage error: ret={ret} errcode={errcode}")
            except Exception as exc:
                last_error = exc
                if attempt >= self.config.send_chunk_retries:
                    break
                await asyncio.sleep(self.config.send_chunk_retry_delay_seconds * (attempt + 1))
        assert last_error is not None
        raise last_error

    async def _maybe_fetch_typing_ticket(self, user_id: str, context_token: str | None) -> None:
        if self.typing_cache.get(user_id):
            return
        try:
            response = await self.client.get_config(user_id=user_id, context_token=context_token)
            ticket = str(response.get("typing_ticket") or "")
            self.typing_cache.set(user_id, ticket)
        except Exception:
            logger.debug("weixin get_config failed for %s", user_id[:8])

    async def send_typing(self, chat_id: str) -> None:
        ticket = self.typing_cache.get(chat_id)
        if not ticket:
            return
        try:
            await self.client.send_typing(user_id=chat_id, typing_ticket=ticket, started=True)
        except Exception:
            logger.debug("weixin typing start failed for %s", chat_id[:8])

    async def stop_typing(self, chat_id: str) -> None:
        ticket = self.typing_cache.get(chat_id)
        if not ticket:
            return
        try:
            await self.client.send_typing(user_id=chat_id, typing_ticket=ticket, started=False)
        except Exception:
            logger.debug("weixin typing stop failed for %s", chat_id[:8])
