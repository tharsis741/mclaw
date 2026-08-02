# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Adapt DingTalk SDK callbacks into normalized channel messages.

The adapter keeps vendor event parsing separate from session routing and agent
execution so the runtime can operate on channel-neutral payloads.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import tempfile
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from mclaw.channels.base import (
    AgentTurnResult,
    AttachmentKind,
    AttachmentOrigin,
    ChannelAttachment,
    ChannelMessage,
    ChannelSource,
    SendResult,
)
from mclaw.channels.dingtalk.command_router import DingTalkCommandRouter
from mclaw.channels.dingtalk.config import DingTalkConfig
from mclaw.channels.dingtalk.dedup import MessageDeduplicator
from mclaw.channels.dingtalk.formatter import split_text_for_dingtalk
from mclaw.channels.dingtalk.media import (
    DingTalkMediaAttachment,
    DingTalkMediaCache,
    extract_audio_duration_ms,
    extract_media_refs,
    extract_text,
    format_media_for_agent,
)
from mclaw.channels.dingtalk.outbound_registry import (
    register_dingtalk_outbound_target,
    unregister_dingtalk_outbound_targets_for_adapter,
)
from mclaw.channels.dingtalk.session_router import DingTalkSessionRouter
from mclaw.channels.dingtalk.stream_client import DingTalkClient
from mclaw.channels.runner import AgentRunner, IngressQueueFullError
from mclaw.prompts.channels import build_channel_context
from mclaw.scheduler.store import SchedulerStore
from mclaw.scheduler.targets import bind_pairing_from_channel, parse_schedule_bind_command

logger = logging.getLogger(__name__)

_DINGTALK_WEBHOOK_RE = re.compile(r"^https://(?:api|oapi)\.dingtalk\.com/")
_SESSION_WEBHOOK_EXPIRY_MARGIN_MS = 5 * 60 * 1000
_IMAGE_SUFFIXES = {".apng", ".avif", ".bmp", ".gif", ".jpeg", ".jpg", ".png", ".webp"}
_AUDIO_SUFFIXES = {".amr", ".ogg"}
_VIDEO_SUFFIXES = {".mp4"}
_TEXT_SUFFIXES = {
    ".bat",
    ".cfg",
    ".conf",
    ".csv",
    ".env",
    ".ini",
    ".js",
    ".json",
    ".log",
    ".md",
    ".ps1",
    ".py",
    ".sql",
    ".toml",
    ".ts",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
_NATIVE_FILE_SUFFIXES = {
    ".doc",
    ".docx",
    ".mp3",
    ".pdf",
    ".ppt",
    ".pptx",
    ".rar",
    ".xls",
    ".xlsx",
    ".zip",
    *_TEXT_SUFFIXES,
}
_TEXT_FILE_ENCODINGS = ("utf-8-sig", "utf-16", "gb18030")
_DEFAULT_VIDEO_COVER_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)


def _safe_id(value: str | None, keep: int = 8) -> str:
    raw = str(value or "")
    if len(raw) <= keep:
        return raw or "?"
    return raw[:keep]


def _log_unhandled_inbound_payload(message: Any, *, message_type: str, conversation_type: str) -> None:
    payload = getattr(message, "_raw_payload", None)
    if not isinstance(payload, dict):
        if message_type:
            logger.info(
                "dingtalk: inbound message ignored without text/media type=%s conversation_type=%s raw_payload=no",
                message_type,
                conversation_type,
            )
        return
    content = payload.get("content")
    content_keys = sorted(content.keys()) if isinstance(content, dict) else []
    logger.warning(
        "dingtalk: inbound message ignored without text/media type=%s conversation_type=%s raw_keys=%s content_keys=%s",
        message_type or "?",
        conversation_type or "?",
        sorted(payload.keys()),
        content_keys,
    )


def _to_channel_attachment(
    attachment: DingTalkMediaAttachment,
    *,
    duration_ms: int = 0,
) -> ChannelAttachment:
    """Normalize one cached DingTalk artifact for shared inbound capabilities."""
    kind_by_media = {
        "image": AttachmentKind.IMAGE,
        "video": AttachmentKind.VIDEO,
        "file": AttachmentKind.FILE,
    }
    is_audio = attachment.kind in {"voice", "audio"}
    if is_audio:
        kind = AttachmentKind.AUDIO
        origin = AttachmentOrigin.VOICE_MESSAGE
    else:
        kind = kind_by_media.get(attachment.kind, AttachmentKind.UNKNOWN)
        origin = (
            AttachmentOrigin.FILE_UPLOAD
            if attachment.kind == "file"
            else AttachmentOrigin.DINGTALK
        )
    return ChannelAttachment(
        kind=kind,
        origin=origin,
        path=attachment.path,
        filename=attachment.filename,
        mime_type=attachment.mime_type,
        size_bytes=attachment.size_bytes,
        duration_ms=max(0, duration_ms) if is_audio else 0,
        codec=attachment.codec,
        error=attachment.error,
        metadata={
            "channel": "dingtalk",
            "dingtalk_kind": attachment.kind,
            "managed_cache": bool(attachment.path and not attachment.error),
        },
    )


def _minimal_message_context(message: Any) -> SimpleNamespace:
    """Retain only routing/display fields, never the inbound content payload."""

    return SimpleNamespace(
        message_id=str(getattr(message, "message_id", "") or ""),
        conversation_id=str(getattr(message, "conversation_id", "") or ""),
        conversation_type=str(getattr(message, "conversation_type", "1") or "1"),
        conversation_title=str(getattr(message, "conversation_title", "") or ""),
        sender_staff_id=str(getattr(message, "sender_staff_id", "") or ""),
    )


def _discard_unowned_channel_message(message: ChannelMessage | None) -> None:
    """Delete adapter-owned cache if the runner never takes message ownership."""

    if message is None:
        return
    for attachment in message.attachments:
        metadata = attachment.metadata if isinstance(attachment.metadata, dict) else {}
        if metadata.get("managed_cache") is not True or not attachment.path:
            continue
        try:
            Path(attachment.path).unlink(missing_ok=True)
        except OSError:
            pass


class DingTalkAdapter:
    """Normalize DingTalk callbacks and route them through M-Claw agent sessions."""

    def __init__(
        self,
        *,
        config: DingTalkConfig,
        client: DingTalkClient,
        runner: AgentRunner,
        session_router: DingTalkSessionRouter,
        command_router: DingTalkCommandRouter | None = None,
        dedup: MessageDeduplicator | None = None,
    ) -> None:
        self.config = config
        self.client = client
        self.runner = runner
        self.session_router = session_router
        self.command_router = command_router or DingTalkCommandRouter()
        self.dedup = dedup or MessageDeduplicator(ttl_seconds=config.dedup_ttl_seconds)
        self.media_cache = DingTalkMediaCache(config=config, client=client)
        self._session_webhooks: dict[str, tuple[str, int]] = {}
        self._message_contexts: dict[str, Any] = {}
        self._done_reaction_fired: set[str] = set()
        self._mention_patterns = self._compile_mention_patterns()

    @property
    def SUPPORTS_MESSAGE_EDITING(self) -> bool:  # noqa: N802
        """DingTalk replies are append-only in the current channel implementation."""
        return False

    @property
    def REQUIRES_EDIT_FINALIZE(self) -> bool:  # noqa: N802
        """No finalize pass is needed because responses are sent once."""
        return False

    def clear_runtime_state(self) -> None:
        """Clear per-runtime caches and outbound targets during shutdown."""
        self._session_webhooks.clear()
        self._message_contexts.clear()
        self._done_reaction_fired.clear()
        unregister_dingtalk_outbound_targets_for_adapter(self)
        self.dedup.clear()

    async def process_message(self, message: Any) -> AgentTurnResult | None:
        """Process one SDK callback into commands, scheduler binds, or an agent turn."""
        message_id = str(
            getattr(message, "message_id", None)
            or getattr(message, "msg_id", None)
            or getattr(message, "msgId", None)
            or ""
        )
        if message_id and self.dedup.is_duplicate(f"id:{message_id}"):
            logger.debug("dingtalk: duplicate message ignored id=%s", _safe_id(message_id))
            return None

        conversation_id = str(getattr(message, "conversation_id", "") or "")
        conversation_type = str(getattr(message, "conversation_type", "1") or "1")
        is_group = conversation_type == "2"
        message_type = str(getattr(message, "message_type", "") or getattr(message, "msgtype", "") or "")
        sender_id = str(getattr(message, "sender_id", "") or "")
        sender_nick = str(getattr(message, "sender_nick", "") or sender_id)
        sender_staff_id = str(getattr(message, "sender_staff_id", "") or "")
        chat_id = conversation_id or sender_id
        chat_type = "group" if is_group else "dm"
        text = extract_text(message)
        media_refs = extract_media_refs(message)
        contains_voice = any(ref.kind in {"voice", "audio"} for ref in media_refs)

        if not sender_id and not chat_id:
            return None
        if not text and not media_refs:
            _log_unhandled_inbound_payload(message, message_type=message_type, conversation_type=conversation_type)
            return None
        if not self._is_user_allowed(sender_id, sender_staff_id, is_group=is_group):
            logger.info("dingtalk: sender rejected by allowlist sender=%s staff=%s", _safe_id(sender_id), _safe_id(sender_staff_id))
            return None
        if not self._should_process_message(message=message, text=text, is_group=is_group, chat_id=chat_id):
            logger.debug("dingtalk: group message ignored chat=%s id=%s", _safe_id(chat_id), _safe_id(message_id))
            return None
        if not message_id:
            media_signature = "|".join(
                f"{ref.kind}:{ref.download_code}:{ref.url}:{ref.filename}" for ref in media_refs
            )
            content_key = self.dedup.content_key(f"{chat_id}:{sender_id}", f"{text}\n{media_signature}")
            if self.dedup.is_duplicate(content_key):
                logger.debug("dingtalk: duplicate message ignored sender=%s chat=%s", _safe_id(sender_id), _safe_id(chat_id))
                return None

        reply_route: tuple[str, int] | None = None
        if chat_id:
            # Later tool calls need a few routing/display fields, not the SDK
            # payload containing recognition text and media download handles.
            self._message_contexts[chat_id] = _minimal_message_context(message)
            self._done_reaction_fired.discard(chat_id)
            reply_route = self._remember_session_webhook(chat_id, message)

        bind_result = await self._maybe_handle_schedule_bind(
            text=text,
            chat_id=chat_id,
            chat_type=chat_type,
            conversation_id=conversation_id,
            sender_id=sender_id,
            sender_staff_id=sender_staff_id,
            message=message,
            message_id=message_id,
            reply_route=reply_route,
        )
        if bind_result is not None:
            return bind_result

        logger.info(
            "dingtalk: inbound message id=%s sender=%s chat_type=%s len=%d media=%s",
            _safe_id(message_id),
            _safe_id(sender_id),
            chat_type,
            len(text),
            bool(media_refs),
        )

        self._fire_thinking_reaction(message)

        source = ChannelSource(
            channel="dingtalk",
            chat_id=chat_id,
            chat_type=chat_type,
            user_id=sender_id,
            user_name=sender_nick,
            message_id=message_id,
            account_id=self.config.client_id,
        )
        routed = self.session_router.route(source, model=self.runner.startup_provider_runtime.model)

        command = self.command_router.handle(
            text,
            status=self.runner.get_status(routed.session_id),
            status_details=self._status_context_text(
                source=source,
                session_id=routed.session_id,
                message=message,
                route=reply_route,
            ),
        )
        if command.handled:
            logger.info("dingtalk: command action=%s session=%s", command.action, routed.session_id)
            if command.action == "new":
                reset = getattr(self.runner, "reset_session", None)
                if reset:
                    reset(routed.session_id)
                replacement_block_reason = getattr(
                    self.runner,
                    "session_replacement_block_reason",
                    None,
                )
                reason = (
                    replacement_block_reason(routed.session_id)
                    if callable(replacement_block_reason)
                    else None
                )
                if reason:
                    message_text = f"A new session was not created yet. {reason}"
                    await self.send(
                        chat_id,
                        message_text,
                        reply_to=message_id,
                        route=reply_route,
                        done_message=message,
                    )
                    return AgentTurnResult(
                        session_id=routed.session_id,
                        final_response=message_text,
                    )
                routed = self.session_router.new_session(
                    source,
                    model=self.runner.startup_provider_runtime.model,
                )
                await self.send(chat_id, command.text, reply_to=message_id, route=reply_route, done_message=message)
            elif command.action == "stop":
                interrupted = self.runner.interrupt(routed.session_id)
                await self.send(
                    chat_id,
                    (
                        "Cancellation requested. Cleanup continues in the background; "
                        "if process termination cannot be confirmed, restart the runtime."
                        if interrupted
                        else "No running turn."
                    ),
                    reply_to=message_id,
                    route=reply_route,
                    done_message=message,
                )
            else:
                await self.send(chat_id, command.text, reply_to=message_id, route=reply_route, done_message=message)
            return AgentTurnResult(session_id=routed.session_id)

        reserve_ingress = getattr(self.runner, "reserve_ingress", None)
        try:
            reservation = (
                reserve_ingress(routed.session_id) if callable(reserve_ingress) else None
            )
        except IngressQueueFullError:
            safe_text = "当前会话消息积压过多，请稍后重试。"
            await self.send(
                chat_id,
                safe_text,
                reply_to=message_id,
                route=reply_route,
                done_message=message,
            )
            return AgentTurnResult(
                session_id=routed.session_id,
                final_response=safe_text,
                error="CHANNEL_QUEUE_FULL",
                raw_result={"queue_full": True},
            )
        release_ingress = getattr(self.runner, "release_ingress", None)
        channel_message: ChannelMessage | None = None
        try:
            # Download may finish out of order; the runner reservation retains
            # the original callback order when each message is admitted.
            await_ingress = getattr(self.runner, "await_ingress", None)
            if (
                reservation is not None
                and callable(await_ingress)
                and not await await_ingress(reservation)
            ):
                if callable(release_ingress):
                    release_ingress(reservation)
                return AgentTurnResult(session_id=routed.session_id, interrupted=True)
            if media_refs:
                bounded_download = getattr(self.runner, "run_bounded_media_download", None)
                operation = lambda: self.media_cache.collect(message, message_id=message_id)
                attachments = (
                    await bounded_download(operation)
                    if callable(bounded_download)
                    else await operation()
                )
            else:
                attachments = []
            duration_ms = extract_audio_duration_ms(message)
            channel_attachments = tuple(
                _to_channel_attachment(attachment, duration_ms=duration_ms)
                for attachment in attachments
            )
            if contains_voice and not getattr(self.config, "media_cache_enabled", True):
                channel_attachments += (ChannelAttachment(
                    kind=AttachmentKind.AUDIO,
                    origin=AttachmentOrigin.VOICE_MESSAGE,
                    mime_type="application/octet-stream",
                    duration_ms=duration_ms,
                    error="media caching is disabled",
                    metadata={"channel": "dingtalk", "managed_cache": False},
                ),)
            media_context = format_media_for_agent(attachments)
            if media_context:
                text = f"{text}\n\n{media_context}".strip() if text else f"User sent DingTalk attachment(s).\n\n{media_context}"
            raw_message = {
                "message_id": message_id,
                "chat_id": chat_id,
                "chat_type": chat_type,
                "sender_id": sender_id,
                "sender_staff_id": sender_staff_id,
                "session_webhook": reply_route[0] if reply_route else "",
                "session_webhook_expired_time": reply_route[1] if reply_route else 0,
                "reaction_open_msg_id": message_id,
                "reaction_open_conversation_id": conversation_id,
                "dingtalk_media": [attachment.to_dict() for attachment in attachments],
            }
            channel_message = ChannelMessage(
                text=text,
                source=source,
                raw_message=raw_message,
                attachments=channel_attachments,
            )
        except BaseException:
            _discard_unowned_channel_message(channel_message)
            if reservation is not None and callable(release_ingress):
                release_ingress(reservation)
            raise

        runner_accepted = False
        try:
            bind_events = getattr(self.runner, "bind_session_events", None)
            if bind_events:
                bind_events(
                    session_id=routed.session_id,
                    loop=asyncio.get_running_loop(),
                    callback=self._agent_event_callback(chat_id),
                )
            register_dingtalk_outbound_target(
                session_id=routed.session_id,
                adapter=self,
                chat_id=chat_id,
                loop=asyncio.get_running_loop(),
            )
            history = self.runner.session_db.get_messages_as_conversation(routed.session_id)
            logger.info("dingtalk: agent turn start session=%s history=%d", routed.session_id, len(history))
            handle_kwargs = dict(
                message=channel_message,
                session_id=routed.session_id,
                conversation_history=history,
                reply_callback=self._reply_callback,
                extra_system=self._session_context_text(source),
            )
            if reservation is not None:
                handle_kwargs["ingress_reservation"] = reservation
            result = await self.runner.handle_message(**handle_kwargs)
            runner_accepted = True
        except BaseException:
            if not runner_accepted:
                _discard_unowned_channel_message(channel_message)
            raise
        finally:
            if reservation is not None and callable(release_ingress):
                release_ingress(reservation)
        if result.queued:
            await self.send(chat_id, "Previous request is still running; queued the latest message.", route=reply_route)
            logger.info("dingtalk: message queued session=%s", routed.session_id)
        else:
            logger.info(
                "dingtalk: agent turn done session=%s error=%s final_len=%d",
                routed.session_id,
                bool(result.error),
                len(result.final_response or ""),
            )
        return result

    async def _maybe_handle_schedule_bind(
        self,
        *,
        text: str,
        chat_id: str,
        chat_type: str,
        conversation_id: str,
        sender_id: str,
        sender_staff_id: str,
        message: Any,
        message_id: str,
        reply_route: tuple[str, int] | None,
    ) -> AgentTurnResult | None:
        """Bind a scheduler target before the text enters the general agent loop."""
        command = parse_schedule_bind_command(text)
        if command is None:
            return None
        is_group = chat_type == "group"
        target_type = "dingtalk_group" if is_group else "dingtalk_private"
        route_metadata: dict[str, Any]
        if is_group:
            open_conversation_id = (
                str(getattr(message, "open_conversation_id", "") or "")
                or str(getattr(message, "openConversationId", "") or "")
                or self.config.open_conversation_map.get(conversation_id, "")
            )
            route_metadata = {"open_conversation_id": open_conversation_id}
        else:
            route_metadata = {"sender_staff_id": sender_staff_id}
        result = bind_pairing_from_channel(
            store=SchedulerStore(self.runner.session_db),
            code=command.code,
            display_name=command.display_name,
            target_type=target_type,
            account_id=self.config.client_id,
            chat_id=conversation_id or chat_id or sender_id,
            chat_type="group" if is_group else "private",
            route_metadata=route_metadata,
        )
        await self.send(chat_id, result.message, reply_to=message_id, route=reply_route, done_message=message)
        return AgentTurnResult(session_id="scheduler-bind", final_response=result.message)

    def _compile_mention_patterns(self) -> list[re.Pattern]:
        """Compile configured group mention patterns, skipping invalid entries."""
        compiled: list[re.Pattern] = []
        for pattern in self.config.mention_patterns:
            try:
                compiled.append(re.compile(pattern, re.IGNORECASE))
            except re.error as exc:
                logger.warning("dingtalk: invalid mention pattern %r: %s", pattern, exc)
        return compiled

    def _is_user_allowed(self, sender_id: str, sender_staff_id: str, *, is_group: bool) -> bool:
        """Apply DM policy and sender allowlist before any agent work starts."""
        allowed = self.config.allowed_user_set()
        if not is_group and self.config.dm_policy == "disabled":
            return False
        if not is_group and self.config.dm_policy == "allowlist" and not allowed:
            return False
        if not is_group and self.config.dm_policy != "allowlist" and not allowed:
            return True
        if not allowed or "*" in allowed:
            return True
        candidates = {sender_id.lower(), sender_staff_id.lower()}
        candidates.discard("")
        return bool(candidates & allowed)

    def _should_process_message(self, *, message: Any, text: str, is_group: bool, chat_id: str) -> bool:
        """Decide whether a group or private message should reach M-Claw."""
        if not is_group:
            return self.config.dm_policy != "disabled"
        if self.config.group_policy == "disabled":
            return False
        allowed_chats = set(self.config.allowed_chats)
        if allowed_chats and chat_id not in allowed_chats:
            return False
        if self.config.group_policy == "open":
            return True
        if chat_id in set(self.config.free_response_chats):
            return True
        if not self.config.require_mention:
            return True
        if bool(getattr(message, "is_in_at_list", False)):
            return True
        return any(pattern.search(text or "") for pattern in self._mention_patterns)

    def _remember_session_webhook(self, chat_id: str, message: Any) -> tuple[str, int] | None:
        """Cache a validated session webhook for future replies in the same chat."""
        session_webhook = str(getattr(message, "session_webhook", "") or "")
        expires_ms = self._session_webhook_expiry_ms(message)
        if not session_webhook or not _DINGTALK_WEBHOOK_RE.match(session_webhook):
            return None
        if len(self._session_webhooks) >= self.config.session_webhooks_max:
            try:
                self._session_webhooks.pop(next(iter(self._session_webhooks)))
            except StopIteration:
                pass
        self._session_webhooks[chat_id] = (session_webhook, expires_ms)
        return session_webhook, expires_ms

    def _session_webhook_expiry_ms(self, message: Any) -> int:
        raw = getattr(message, "session_webhook_expired_time", 0) or 0
        try:
            return int(raw)
        except (TypeError, ValueError):
            logger.debug("dingtalk: invalid session_webhook_expired_time=%r", raw)
            return 0

    def _get_valid_webhook(self, chat_id: str) -> tuple[str, int] | None:
        """Return a cached webhook only if it is still inside the expiry margin."""
        entry = self._session_webhooks.get(chat_id)
        if not entry:
            return None
        webhook, expires_ms = entry
        if expires_ms and int(time.time() * 1000) + _SESSION_WEBHOOK_EXPIRY_MARGIN_MS >= expires_ms:
            self._session_webhooks.pop(chat_id, None)
            return None
        return entry

    def _is_route_valid(self, route: tuple[str, int] | None) -> bool:
        if not route:
            return False
        webhook, expires_ms = route
        if not webhook or not _DINGTALK_WEBHOOK_RE.match(webhook):
            return False
        return not (expires_ms and int(time.time() * 1000) + _SESSION_WEBHOOK_EXPIRY_MARGIN_MS >= expires_ms)

    def _route_from_channel_message(self, message: ChannelMessage) -> tuple[str, int] | None:
        raw = message.raw_message or {}
        webhook = str(raw.get("session_webhook") or "")
        try:
            expires_ms = int(raw.get("session_webhook_expired_time") or 0)
        except (TypeError, ValueError):
            expires_ms = 0
        route = (webhook, expires_ms)
        return route if self._is_route_valid(route) else None

    def _has_send_route(self, chat_id: str) -> bool:
        return self._get_valid_webhook(chat_id) is not None

    async def _reply_callback(self, message: ChannelMessage, result: AgentTurnResult) -> None:
        """Send the final agent result through the route captured on the inbound message."""
        route = self._route_from_channel_message(message)
        if result.error:
            safe_text = result.final_response.strip() or f"Error: {result.error}"
            await self.send(
                message.source.chat_id,
                safe_text,
                reply_to=message.source.message_id,
                route=route,
                done_message=message,
            )
            return
        text = result.final_response.strip()
        if text:
            await self.send(
                message.source.chat_id,
                text,
                reply_to=message.source.message_id,
                route=route,
                done_message=message,
            )

    def _agent_event_callback(self, chat_id: str):
        """Create a session event callback that reports saved skill updates."""
        async def _callback(_session_id: str, event: dict[str, Any]) -> None:
            if event.get("type") != "skills_saved":
                return
            text = str(event.get("text") or event.get("summary") or "").strip()
            if text:
                await self.send(chat_id, text)

        return _callback

    def _session_context_text(self, source: ChannelSource) -> str:
        return build_channel_context(
            "dingtalk",
            chat_type=source.chat_type or "",
            user_name=source.user_name or "",
            user_id=source.user_id or "",
        )

    def _status_context_text(
        self,
        *,
        source: ChannelSource,
        session_id: str,
        message: Any,
        route: tuple[str, int] | None,
    ) -> str:
        conversation_id = str(getattr(message, "conversation_id", "") or source.chat_id or "")
        conversation_type = str(getattr(message, "conversation_type", "") or "")
        sender_staff_id = str(getattr(message, "sender_staff_id", "") or "")
        open_conversation_id = self.config.open_conversation_map.get(conversation_id, "")
        webhook_state = "available" if self._is_route_valid(route) else "unavailable"
        chat_type = source.chat_type or ("group" if conversation_type == "2" else "dm")
        lines = [
            "DingTalk context:",
            f"chat_id: {source.chat_id}",
            f"conversation_id: {conversation_id}",
            f"conversation_type: {chat_type}",
            f"session_id: {session_id}",
            f"session_scope: {self.config.session_scope}",
            f"session_webhook: {webhook_state}",
            f"open_conversation_id: {open_conversation_id or 'not configured'}",
        ]
        if source.user_id:
            lines.append(f"sender_id: {source.user_id}")
        if sender_staff_id:
            lines.append(f"sender_staff_id: {sender_staff_id}")
        return "\n".join(lines)

    async def send(
        self,
        chat_id: str,
        content: str,
        *,
        reply_to: str | None = None,
        route: tuple[str, int] | None = None,
        done_message: ChannelMessage | Any | None = None,
    ) -> SendResult:
        """Send chunked markdown text through a valid DingTalk reply route."""
        chunks = split_text_for_dingtalk(content, self.config.max_message_length)
        if not chunks:
            return SendResult(success=True)
        last_result = SendResult(success=True)
        for chunk in chunks:
            last_result = await self._send_one(chat_id, chunk, reply_to=reply_to, route=route, done_message=done_message)
            if not last_result.success:
                return last_result
        return last_result

    async def send_file(self, chat_id: str, *, file_path: str, caption: str = "") -> SendResult:
        """Send a local file using the safest DingTalk route for its media type."""
        path = Path(file_path)
        if not path.is_file():
            return SendResult(success=False, error=f"File not found: {file_path}")
        try:
            size_bytes = path.stat().st_size
        except OSError as exc:
            return SendResult(success=False, error=f"File not readable: {file_path}: {exc}")
        if size_bytes > self.config.media_max_bytes:
            return SendResult(
                success=False,
                error=f"File exceeds DingTalk media_max_bytes ({size_bytes} > {self.config.media_max_bytes})",
            )
        if path.suffix.lower() in _TEXT_SUFFIXES:
            if self._openapi_destination_for_chat(chat_id) is not None:
                return await self._send_openapi_file(chat_id, path=path, caption=caption)
            text, error = self._read_text_file(path)
            if error:
                return SendResult(success=False, error=error)
            heading = caption.strip() or f"File: {path.name}"
            body = (text or "(empty file)").replace("```", "```\u200b")
            return await self.send(chat_id, f"{heading}\n\n```text\n{body}\n```")
        if path.suffix.lower() in _AUDIO_SUFFIXES:
            return await self._send_openapi_audio(chat_id, path=path, caption=caption)
        if path.suffix.lower() in _VIDEO_SUFFIXES:
            return await self._send_openapi_video(chat_id, path=path, caption=caption)
        if path.suffix.lower() in _IMAGE_SUFFIXES:
            if not self._has_send_route(chat_id):
                return SendResult(
                    success=False,
                    error="No valid session_webhook available. Reply must follow an incoming DingTalk message.",
                )
            uploader = getattr(self.client, "upload_media", None)
            if uploader is None:
                return SendResult(success=False, error="DingTalk media upload is not available in this runtime")
            upload = await uploader(file_path=str(path), media_type="image")
            if not upload.success or not upload.message_id:
                return SendResult(success=False, error=upload.error or "DingTalk image upload failed")
            image_markdown = f"![image]({upload.message_id})"
            content = f"{caption}\n\n{image_markdown}" if caption else image_markdown
            return await self.send(chat_id, content)
        upload_path = path if path.suffix.lower() in _NATIVE_FILE_SUFFIXES else self._zip_for_dingtalk(path)
        return await self._send_openapi_file(chat_id, path=upload_path, caption=caption)

    def _openapi_destination_for_chat(self, chat_id: str) -> tuple[str, str] | None:
        """Resolve group or private OpenAPI targets from config and recent context."""
        open_conversation_id = self.config.open_conversation_map.get(chat_id, "")
        if open_conversation_id:
            return "group", open_conversation_id
        message = self._message_contexts.get(chat_id)
        if message is None:
            return None
        conversation_type = str(getattr(message, "conversation_type", "") or "")
        sender_staff_id = str(getattr(message, "sender_staff_id", "") or "")
        if conversation_type == "1" and sender_staff_id:
            return "oto", sender_staff_id
        return None

    async def _send_openapi_message(
        self,
        destination: tuple[str, str],
        *,
        msg_key: str,
        msg_param: dict[str, Any],
    ) -> SendResult:
        """Dispatch an OpenAPI message to the group or private endpoint."""
        kind, target_id = destination
        if kind == "group":
            return await self.client.send_group_robot_message(
                open_conversation_id=target_id,
                msg_key=msg_key,
                msg_param=msg_param,
            )
        return await self.client.send_oto_robot_message(
            user_id=target_id,
            msg_key=msg_key,
            msg_param=msg_param,
        )

    async def _send_openapi_caption(self, destination: tuple[str, str], caption: str) -> SendResult:
        if not caption.strip():
            return SendResult(success=True)
        return await self._send_openapi_message(
            destination,
            msg_key="sampleMarkdown",
            msg_param={"title": "M-Claw", "text": caption.strip()},
        )

    async def _send_openapi_file(self, chat_id: str, *, path: Path, caption: str = "") -> SendResult:
        """Upload and send a file card through DingTalk robot OpenAPI."""
        destination = self._openapi_destination_for_chat(chat_id)
        if destination is None:
            return SendResult(
                success=False,
                error=(
                    "No DingTalk OpenAPI destination available for this chat. "
                    "For groups, add channels.dingtalk.open_conversation_map[conversation_id]; "
                    "for private chats, reply after an incoming message that includes senderStaffId."
                ),
            )
        size_result = self._check_upload_size(path)
        if size_result is not None:
            return size_result
        uploader = getattr(self.client, "upload_media", None)
        if uploader is None:
            return SendResult(success=False, error="DingTalk OpenAPI media sending is not available in this runtime")
        caption_result = await self._send_openapi_caption(destination, caption)
        if not caption_result.success:
            return caption_result
        upload = await uploader(file_path=str(path), media_type="file")
        if not upload.success or not upload.message_id:
            return SendResult(success=False, error=upload.error or "DingTalk file upload failed")
        return await self._send_openapi_message(
            destination,
            msg_key="sampleFile",
            msg_param={
                "mediaId": upload.message_id,
                "fileName": path.name,
                "fileType": path.suffix.lower().lstrip(".") or "file",
            },
        )

    async def _send_openapi_audio(self, chat_id: str, *, path: Path, caption: str = "") -> SendResult:
        """Upload and send an audio card through DingTalk robot OpenAPI."""
        destination = self._openapi_destination_for_chat(chat_id)
        if destination is None:
            return SendResult(
                success=False,
                error=(
                    "No DingTalk OpenAPI destination available for this chat. "
                    "For groups, add channels.dingtalk.open_conversation_map[conversation_id]; "
                    "for private chats, reply after an incoming message that includes senderStaffId."
                ),
            )
        size_result = self._check_upload_size(path)
        if size_result is not None:
            return size_result
        uploader = getattr(self.client, "upload_media", None)
        if uploader is None:
            return SendResult(success=False, error="DingTalk OpenAPI media sending is not available in this runtime")
        caption_result = await self._send_openapi_caption(destination, caption)
        if not caption_result.success:
            return caption_result
        upload = await uploader(file_path=str(path), media_type="voice")
        if not upload.success or not upload.message_id:
            return SendResult(success=False, error=upload.error or "DingTalk audio upload failed")
        return await self._send_openapi_message(
            destination,
            msg_key="sampleAudio",
            msg_param={
                "mediaId": upload.message_id,
                "duration": "1000",
            },
        )

    async def _send_openapi_video(self, chat_id: str, *, path: Path, caption: str = "") -> SendResult:
        """Upload video plus generated cover image through DingTalk robot OpenAPI."""
        destination = self._openapi_destination_for_chat(chat_id)
        if destination is None:
            return SendResult(
                success=False,
                error=(
                    "No DingTalk OpenAPI destination available for this chat. "
                    "For groups, add channels.dingtalk.open_conversation_map[conversation_id]; "
                    "for private chats, reply after an incoming message that includes senderStaffId."
                ),
            )
        size_result = self._check_upload_size(path)
        if size_result is not None:
            return size_result
        uploader = getattr(self.client, "upload_media", None)
        if uploader is None:
            return SendResult(success=False, error="DingTalk OpenAPI media sending is not available in this runtime")
        caption_result = await self._send_openapi_caption(destination, caption)
        if not caption_result.success:
            return caption_result
        video_upload = await uploader(file_path=str(path), media_type="video")
        if not video_upload.success or not video_upload.message_id:
            return SendResult(success=False, error=video_upload.error or "DingTalk video upload failed")
        cover_upload = await uploader(file_path=str(self._default_video_cover_path()), media_type="image")
        if not cover_upload.success or not cover_upload.message_id:
            return SendResult(success=False, error=cover_upload.error or "DingTalk video cover upload failed")
        return await self._send_openapi_message(
            destination,
            msg_key="sampleVideo",
            msg_param={
                "duration": "1",
                "videoMediaId": video_upload.message_id,
                "videoType": "mp4",
                "picMediaId": cover_upload.message_id,
                "height": "1",
                "width": "1",
            },
        )

    def _default_video_cover_path(self) -> Path:
        """Create the tiny cover image required by DingTalk video messages."""
        cache_dir = Path(self.config.media_cache_dir).expanduser() if self.config.media_cache_dir else Path(tempfile.gettempdir())
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            cache_dir = Path(tempfile.gettempdir())
        path = cache_dir / "dingtalk-video-cover.png"
        if not path.exists():
            path.write_bytes(base64.b64decode(_DEFAULT_VIDEO_COVER_PNG_B64))
        return path

    def _zip_for_dingtalk(self, path: Path) -> Path:
        """Wrap unsupported file types in a zip accepted by DingTalk file cards."""
        cache_dir = Path(self.config.media_cache_dir).expanduser() if self.config.media_cache_dir else Path(tempfile.gettempdir())
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            cache_dir = Path(tempfile.gettempdir())
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", path.name).strip("._") or "file"
        zip_path = cache_dir / f"{safe_name}.zip"
        with zipfile.ZipFile(zip_path, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(path, arcname=path.name)
        return zip_path

    def _check_upload_size(self, path: Path) -> SendResult | None:
        try:
            size_bytes = path.stat().st_size
        except OSError as exc:
            return SendResult(success=False, error=f"File not readable: {path}: {exc}")
        if size_bytes > self.config.media_max_bytes:
            return SendResult(
                success=False,
                error=f"File exceeds DingTalk media_max_bytes ({size_bytes} > {self.config.media_max_bytes})",
            )
        return None

    def _read_text_file(self, path: Path) -> tuple[str, str | None]:
        """Decode a text file for webhook markdown delivery with common encodings."""
        data = path.read_bytes()
        if b"\x00" in data[:4096]:
            return "", "File looks binary; DingTalk text-file delivery only supports readable text files"
        for encoding in _TEXT_FILE_ENCODINGS:
            try:
                return data.decode(encoding), None
            except UnicodeDecodeError:
                continue
        return data.decode("utf-8", errors="replace"), None

    async def send_typing(self, chat_id: str, metadata: dict[str, Any] | None = None) -> None:
        """Typing indicators are intentionally unsupported for DingTalk webhook replies."""
        return None

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        *,
        caption: str = "",
        reply_to: str | None = None,
    ) -> SendResult:
        image_block = f"![image]({image_url})"
        content = f"{caption}\n\n{image_block}" if caption else image_block
        return await self.send(chat_id, content, reply_to=reply_to)

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        *,
        caption: str = "",
        reply_to: str | None = None,
        **_kwargs: Any,
    ) -> SendResult:
        result = await self.send_file(chat_id, file_path=image_path, caption=caption)
        if result.success and reply_to:
            self._fire_done_reaction(chat_id)
        return result

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        *,
        caption: str = "",
        file_name: str | None = None,
        reply_to: str | None = None,
        **_kwargs: Any,
    ) -> SendResult:
        result = await self.send_file(chat_id, file_path=file_path, caption=caption or (file_name or ""))
        if result.success and reply_to:
            self._fire_done_reaction(chat_id)
        return result

    async def get_chat_info(self, chat_id: str) -> dict[str, str]:
        message = self._message_contexts.get(chat_id)
        if message is not None:
            title = str(getattr(message, "conversation_title", "") or chat_id)
            conversation_type = str(getattr(message, "conversation_type", "1") or "1")
            return {
                "name": title,
                "type": "group" if conversation_type == "2" else "dm",
            }
        return {
            "name": chat_id,
            "type": "group" if "group" in chat_id.lower() else "dm",
        }

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        content: str,
        *,
        finalize: bool = False,
    ) -> SendResult:
        if not message_id:
            return SendResult(success=False, error="message_id required")
        return SendResult(success=False, error="DingTalk message editing is disabled")

    async def _send_one(
        self,
        chat_id: str,
        content: str,
        *,
        reply_to: str | None = None,
        route: tuple[str, int] | None = None,
        done_message: ChannelMessage | Any | None = None,
    ) -> SendResult:
        """Send one markdown chunk and fire the completion reaction on final replies."""
        is_final_reply = reply_to is not None
        webhook_info = route if self._is_route_valid(route) else self._get_valid_webhook(chat_id)
        if not webhook_info:
            return SendResult(
                success=False,
                error="No valid session_webhook available. Reply must follow an incoming DingTalk message.",
            )
        result = await self.client.send_markdown(session_webhook=webhook_info[0], text=content)
        if result.success and is_final_reply:
            self._fire_done_reaction(chat_id, done_message=done_message)
        return result

    def _fire_thinking_reaction(self, message: Any) -> None:
        """Start a non-blocking reaction that marks an inbound turn as in progress."""
        msg_id = str(getattr(message, "message_id", "") or "")
        conversation_id = str(getattr(message, "conversation_id", "") or "")
        if not msg_id or not conversation_id:
            return
        self.client.spawn_bg(
            self.client.send_reaction(
                open_msg_id=msg_id,
                open_conversation_id=conversation_id,
                emoji_name="🤔Thinking",
            )
        )

    def _fire_done_reaction(self, chat_id: str, *, done_message: ChannelMessage | Any | None = None) -> None:
        """Swap the thinking reaction for a done reaction once a final reply succeeds."""
        msg_id = ""
        conversation_id = ""
        if isinstance(done_message, ChannelMessage):
            raw = done_message.raw_message or {}
            msg_id = str(raw.get("reaction_open_msg_id") or done_message.source.message_id or "")
            conversation_id = str(raw.get("reaction_open_conversation_id") or done_message.source.chat_id or "")
        elif done_message is not None:
            msg_id = str(getattr(done_message, "message_id", "") or "")
            conversation_id = str(getattr(done_message, "conversation_id", "") or "")
        message = None
        if not msg_id or not conversation_id:
            message = self._message_contexts.get(chat_id)
            if message:
                msg_id = str(getattr(message, "message_id", "") or "")
                conversation_id = str(getattr(message, "conversation_id", "") or "")
        done_key = f"{conversation_id}:{msg_id}" if msg_id and conversation_id else chat_id
        if done_key in self._done_reaction_fired:
            return
        self._done_reaction_fired.add(done_key)
        if not msg_id or not conversation_id:
            return

        async def _swap() -> None:
            await self.client.send_reaction(
                open_msg_id=msg_id,
                open_conversation_id=conversation_id,
                emoji_name="🤔Thinking",
                recall=True,
            )
            await self.client.send_reaction(
                open_msg_id=msg_id,
                open_conversation_id=conversation_id,
                emoji_name="🥳Done",
            )

        self.client.spawn_bg(_swap())
