# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from mclaw.channels.base import AgentTurnResult, AttachmentKind, AttachmentOrigin
from mclaw.channels.weixin import adapter as adapter_module
from mclaw.channels.weixin.adapter import (
    WeixinAdapter,
    _extract_platform_transcript,
    _extract_text,
    _to_channel_attachment,
)
from mclaw.channels.weixin.config import WeixinConfig
from mclaw.channels.weixin.media import (
    WeixinMediaAttachment,
    WeixinMediaCache,
    format_media_for_agent,
)


def _voice_item(*, transcript: str = "平台提供的文字") -> dict:
    return {
        "type": 3,
        "voice_item": {
            "text": transcript,
            "encode_type": 6,
            "sample_rate": 24000,
            "playtime": 1850,
            "bits_per_sample": 16,
            "media": {"full_url": "https://novac2c.cdn.weixin.qq.com/c2c/voice"},
        },
    }


def test_voice_platform_transcript_is_metadata_not_user_text() -> None:
    voice = _voice_item()

    assert _extract_text([voice]) == ""
    assert _extract_platform_transcript([voice]) == "平台提供的文字"
    assert _extract_text([voice, {"type": 1, "text_item": {"text": "用户输入"}}]) == "用户输入"


def test_media_cache_preserves_weixin_voice_codec_metadata(tmp_path: Path) -> None:
    class Client:
        async def download_bytes(self, _url: str, *, timeout_ms: int, max_bytes: int) -> bytes:
            assert timeout_ms == 60_000
            assert max_bytes == 7 * 1024 * 1024 + 16
            return b"#!SILK_V3-test"

    config = WeixinConfig(
        account_id="bot-account",
        media_cache_dir=str(tmp_path),
        media_download_timeout_seconds=60,
    )
    cache = WeixinMediaCache(config=config, client=Client())

    attachments = asyncio.run(cache.collect([_voice_item()], message_id="voice-message"))

    assert len(attachments) == 1
    attachment = attachments[0]
    assert attachment.kind == "voice"
    assert attachment.mime_type == "audio/silk"
    assert Path(attachment.path).read_bytes() == b"#!SILK_V3-test"
    if os.name == "posix":
        assert stat.S_IMODE(Path(attachment.path).stat().st_mode) == 0o600
    assert attachment.metadata == {
        "channel": "weixin",
        "item_type": 3,
        "encode_type": 6,
        "sample_rate": 24000,
        "playtime": 1850,
        "bits_per_sample": 16,
    }


def test_weixin_voice_rejects_plain_http_before_download(tmp_path: Path) -> None:
    class Client:
        async def download_bytes(self, *_args, **_kwargs) -> bytes:
            raise AssertionError("plain HTTP URL must not reach the client")

    voice = _voice_item()
    voice["voice_item"]["media"]["full_url"] = (
        "http://novac2c.cdn.weixin.qq.com/c2c/voice"
    )
    cache = WeixinMediaCache(
        config=WeixinConfig(account_id="bot-account", media_cache_dir=str(tmp_path)),
        client=Client(),
    )

    attachment = asyncio.run(cache.collect([voice], message_id="voice-message"))[0]

    assert "must use https" in attachment.error


def test_weixin_media_collection_cancellation_removes_prior_cache(tmp_path: Path) -> None:
    calls = 0

    class Client:
        async def download_bytes(self, _url: str, **_kwargs) -> bytes:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise asyncio.CancelledError
            return b"#!SILK_V3-test"

    cache = WeixinMediaCache(
        config=WeixinConfig(account_id="bot-account", media_cache_dir=str(tmp_path)),
        client=Client(),
    )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(cache.collect([_voice_item(), _voice_item()], message_id="cancelled"))

    assert list(tmp_path.rglob("*.silk")) == []


def test_voice_attachment_is_typed_for_pipeline_and_hidden_from_media_prompt() -> None:
    voice = WeixinMediaAttachment(
        kind="voice",
        mime_type="audio/silk",
        path="C:/cache/private-voice.silk",
        filename="voice.silk",
        size_bytes=321,
        metadata={
            "channel": "weixin",
            "item_type": 3,
            "encode_type": 6,
            "sample_rate": 24000,
            "playtime": 1850,
        },
    )

    normalized = _to_channel_attachment(voice)

    assert normalized.kind is AttachmentKind.AUDIO
    assert normalized.origin is AttachmentOrigin.VOICE_MESSAGE
    assert normalized.path == voice.path
    assert normalized.mime_type == "audio/silk"
    assert normalized.size_bytes == 321
    assert normalized.codec == "silk"
    assert normalized.duration_ms == 1850
    assert normalized.metadata["encode_type"] == 6
    assert format_media_for_agent([voice]) == ""


def test_regular_file_attachment_is_marked_as_file_upload() -> None:
    upload = WeixinMediaAttachment(
        kind="file",
        mime_type="application/pdf",
        path="C:/cache/report.pdf",
        filename="report.pdf",
        size_bytes=123,
        metadata={"channel": "weixin", "item_type": 4},
    )

    normalized = _to_channel_attachment(upload)

    assert normalized.kind is AttachmentKind.FILE
    assert normalized.origin is AttachmentOrigin.FILE_UPLOAD
    assert "C:/cache/report.pdf" in format_media_for_agent([upload])


def test_voice_only_message_reaches_runner_without_platform_text_or_path(monkeypatch) -> None:
    captured_messages = []
    voice = WeixinMediaAttachment(
        kind="voice",
        mime_type="audio/silk",
        path="C:/cache/private-voice.silk",
        filename="voice.silk",
        size_bytes=321,
        metadata={
            "channel": "weixin",
            "item_type": 3,
            "encode_type": 6,
            "sample_rate": 24000,
            "playtime": 1850,
        },
    )
    adapter = WeixinAdapter.__new__(WeixinAdapter)
    adapter.config = SimpleNamespace(account_id="bot-account", media_cache_enabled=True)
    adapter.dedup = SimpleNamespace(
        is_duplicate=lambda _key: False,
        content_key=lambda *_args: "content-key",
    )
    adapter.token_store = SimpleNamespace(set=lambda *_args: None)
    adapter.is_dm_allowed = lambda _sender_id: True
    adapter.session_router = SimpleNamespace(
        route=lambda *_args, **_kwargs: SimpleNamespace(session_id="weixin-session")
    )
    adapter.command_router = SimpleNamespace(
        handle=lambda text, **_kwargs: SimpleNamespace(handled=False, action="", text=text)
    )

    async def collect(_items, *, message_id: str):
        assert message_id == "voice-message"
        return [voice]

    async def no_op(*_args, **_kwargs):
        return None

    async def no_bind(**_kwargs):
        return None

    async def handle_message(**kwargs):
        captured_messages.append(kwargs["message"])
        return AgentTurnResult(session_id=kwargs["session_id"], final_response="收到")

    adapter.media_cache = SimpleNamespace(collect=collect)
    adapter._maybe_fetch_typing_ticket = no_op
    adapter._maybe_handle_schedule_bind = no_bind
    adapter.send_typing = no_op
    adapter.stop_typing = no_op
    adapter._session_context_text = lambda _source: ""
    adapter.runner = SimpleNamespace(
        startup_provider_runtime=SimpleNamespace(model="test-model"),
        get_status=lambda _session_id: "idle",
        session_db=SimpleNamespace(get_messages_as_conversation=lambda _session_id: []),
        handle_message=handle_message,
    )
    monkeypatch.setattr(adapter_module, "register_weixin_outbound_target", lambda **_kwargs: None)

    inbound_voice = _voice_item()
    inbound_voice["voice_item"]["media"]["aes_key"] = "private-aes-key"
    result = asyncio.run(adapter.process_message({
        "from_user_id": "user-1",
        "to_user_id": "bot-account",
        "message_id": "voice-message",
        "context_token": "context-token",
        "item_list": [inbound_voice],
    }))

    assert result is not None
    assert result.session_id == "weixin-session"
    assert len(captured_messages) == 1
    channel_message = captured_messages[0]
    assert channel_message.text == ""
    assert "private-voice.silk" not in channel_message.text
    raw_text = str(channel_message.raw_message)
    assert "_mclaw_platform_transcript" not in channel_message.raw_message
    assert "平台提供的文字" not in raw_text
    assert "context-token" not in raw_text
    assert "private-aes-key" not in raw_text
    assert "novac2c.cdn.weixin.qq.com" not in raw_text
    assert len(channel_message.attachments) == 1
    assert channel_message.attachments[0].kind is AttachmentKind.AUDIO
    assert channel_message.attachments[0].origin is AttachmentOrigin.VOICE_MESSAGE
