# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from mclaw.channels.base import AgentTurnResult, AttachmentKind, AttachmentOrigin
from mclaw.channels.dingtalk import adapter as adapter_module
from mclaw.channels.dingtalk.adapter import DingTalkAdapter, _to_channel_attachment
from mclaw.channels.dingtalk.config import DingTalkConfig
from mclaw.channels.dingtalk.stream_client import DingTalkClient
from mclaw.channels.dingtalk.media import (
    DingTalkMediaAttachment,
    DingTalkMediaCache,
    extract_audio_duration_ms,
    extract_media_refs,
    extract_platform_transcript,
    extract_text,
    format_media_for_agent,
    sniff_media_type,
)


def _voice_message(*, recognition: str = "钉钉平台转写") -> SimpleNamespace:
    content = {
        "duration": 1200,
        "downloadCode": "download-code",
        "recognition": recognition,
    }
    return SimpleNamespace(
        message_id="message-1",
        message_type="audio",
        robot_code="robot-code",
        text=None,
        content=content,
        _raw_payload={"msgtype": "audio", "content": dict(content)},
    )


def test_platform_recognition_is_metadata_not_user_text() -> None:
    message = _voice_message()

    assert extract_text(message) == ""
    assert extract_platform_transcript(message) == "钉钉平台转写"
    assert extract_audio_duration_ms(message) == 1200
    refs = extract_media_refs(message)
    assert len(refs) == 1
    assert refs[0].kind == "audio"
    assert refs[0].download_code == "download-code"
    assert refs[0].mime_type == "application/octet-stream"


def test_download_code_cannot_be_overridden_by_raw_url() -> None:
    message = _voice_message()
    message.content["downloadUrl"] = "https://attacker.example/internal"

    refs = extract_media_refs(message)

    assert refs[0].download_code == "download-code"
    assert refs[0].url == ""


def test_generic_rich_text_url_is_not_treated_as_media() -> None:
    message = SimpleNamespace(
        message_type="richText",
        content={"richText": [{"type": "text", "text": "link", "url": "http://127.0.0.1/"}]},
    )

    assert extract_media_refs(message) == []


def test_untrusted_direct_download_host_is_rejected_before_network(tmp_path: Path) -> None:
    class Client:
        async def download_bytes(self, *_args, **_kwargs) -> bytes:
            raise AssertionError("untrusted URL must not reach the HTTP client")

    message = SimpleNamespace(
        message_type="audio",
        content={"downloadUrl": "https://attacker.example/internal"},
    )
    config = DingTalkConfig(client_id="client-id", media_cache_dir=str(tmp_path))

    attachment = asyncio.run(DingTalkMediaCache(config=config, client=Client()).collect(message))[0]

    assert "host is not trusted" in attachment.error


def test_official_http_download_url_is_upgraded_without_plaintext_request() -> None:
    requested_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        return httpx.Response(200, content=b"#!AMR\n\x3c\x00\x01")

    async def run() -> bytes:
        client = DingTalkClient(DingTalkConfig())
        client.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            return await client.download_bytes(
                "http://static.dingtalk.com/media/voice.file?Signature=secret",
                max_bytes=1024,
            )
        finally:
            await client.http.aclose()
            client.http = None

    assert asyncio.run(run()).startswith(b"#!AMR")
    assert len(requested_urls) == 1
    assert requested_urls[0].startswith("https://static.dingtalk.com/")


def test_trusted_media_redirect_is_followed_and_size_limited() -> None:
    requested_hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_hosts.append(str(request.url.host))
        if request.url.host == "download.dingtalk.com":
            return httpx.Response(
                302,
                headers={
                    "Location": "https://voice.oss-cn-hangzhou.aliyuncs.com/audio.file?token=signed"
                },
            )
        return httpx.Response(200, content=b"#!AMR\n\x3c\x00\x01")

    async def run() -> bytes:
        client = DingTalkClient(DingTalkConfig())
        client.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            return await client.download_bytes(
                "https://download.dingtalk.com/voice.file",
                max_bytes=1024,
            )
        finally:
            await client.http.aclose()
            client.http = None

    assert asyncio.run(run()).startswith(b"#!AMR")
    assert requested_hosts == ["download.dingtalk.com", "voice.oss-cn-hangzhou.aliyuncs.com"]


@pytest.mark.parametrize(
    ("location", "error"),
    [
        ("https://attacker.example/voice.file", "host is not trusted"),
        ("http://download.dingtalk.com/voice.file", "must use https"),
    ],
)
def test_media_redirect_revalidates_every_hop(location: str, error: str) -> None:
    request_count = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(302, headers={"Location": location})

    async def run() -> None:
        client = DingTalkClient(DingTalkConfig())
        client.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            await client.download_bytes("https://download.dingtalk.com/voice.file")
        finally:
            await client.http.aclose()
            client.http = None

    with pytest.raises(ValueError, match=error):
        asyncio.run(run())
    assert request_count == 1


def test_media_redirect_loop_is_rejected() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "/voice.file"})

    async def run() -> None:
        client = DingTalkClient(DingTalkConfig())
        client.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            await client.download_bytes("https://download.dingtalk.com/voice.file")
        finally:
            await client.http.aclose()
            client.http = None

    with pytest.raises(RuntimeError, match="redirect loop"):
        asyncio.run(run())


def test_extract_text_keeps_explicit_text_and_rich_text() -> None:
    explicit = SimpleNamespace(
        message_type="text",
        text=SimpleNamespace(content=" 用户输入 "),
        content={"recognition": "平台转写不应覆盖用户输入"},
    )
    rich = SimpleNamespace(
        message_type="richText",
        text=None,
        content={
            "recognition": "平台转写",
            "richText": [{"text": "第一段"}, {"type": "picture"}, {"text": "第二段"}],
        },
    )

    assert extract_text(explicit) == "用户输入"
    assert extract_text(rich) == "第一段 第二段"


@pytest.mark.parametrize(
    ("payload", "mime_type", "codec", "extension"),
    [
        (b"#!AMR\n\x3c\x00\x01", "audio/amr", "amr-nb", ".amr"),
        (b"#!AMR-WB\n\x44\x00\x01", "audio/amr-wb", "amr-wb", ".amr"),
        (b"OggS" + b"\x00" * 24 + b"OpusHead" + b"\x00" * 8, "audio/ogg", "opus", ".ogg"),
        (b"ID3\x04\x00\x00\x00\x00\x00\x00", "audio/mpeg", "mp3", ".mp3"),
        (b"fLaC\x00\x00\x00\x22", "audio/flac", "flac", ".flac"),
    ],
)
def test_sniff_media_type_uses_audio_magic(
    payload: bytes,
    mime_type: str,
    codec: str,
    extension: str,
) -> None:
    detected = sniff_media_type(
        payload,
        kind="audio",
        fallback_mime="application/octet-stream",
    )

    assert detected.mime_type == mime_type
    assert detected.codec == codec
    assert detected.extension == extension


def test_unknown_voice_payload_is_not_assumed_to_be_mp3() -> None:
    detected = sniff_media_type(
        b"not-a-known-audio-container",
        kind="audio",
        fallback_mime="application/octet-stream",
    )

    assert detected.mime_type == "application/octet-stream"
    assert detected.codec == ""
    assert detected.extension == ".bin"


def test_magic_overrides_misleading_filename_and_declared_mime() -> None:
    detected = sniff_media_type(
        b"#!AMR\n\x3c\x00\x01",
        kind="audio",
        filename="voice.mp3",
        fallback_mime="audio/mpeg",
    )

    assert detected.mime_type == "audio/amr"
    assert detected.codec == "amr-nb"
    assert detected.extension == ".amr"


def test_media_cache_writes_sniffed_audio_extension(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []

    class Client:
        async def fetch_download_url(self, *, download_code: str, robot_code: str) -> str:
            calls.append((download_code, robot_code))
            return "https://download.dingtalk.com/voice.file"

        async def download_bytes(self, _url: str, *, timeout_seconds: float, max_bytes: int) -> bytes:
            assert timeout_seconds == 7
            assert max_bytes == 7 * 1024 * 1024
            return b"#!AMR\n\x3c\x00\x01"

    config = DingTalkConfig(
        client_id="client-id",
        robot_code="configured-robot",
        media_cache_dir=str(tmp_path),
        media_download_timeout_seconds=7,
    )
    attachment = asyncio.run(DingTalkMediaCache(config=config, client=Client()).collect(_voice_message()))[0]

    assert calls == [("download-code", "robot-code")]
    assert attachment.error == ""
    assert attachment.kind == "audio"
    assert attachment.mime_type == "audio/amr"
    assert attachment.codec == "amr-nb"
    assert attachment.size_bytes == len(b"#!AMR\n\x3c\x00\x01")
    path = Path(attachment.path)
    assert path.suffix == ".amr"
    assert path.read_bytes() == b"#!AMR\n\x3c\x00\x01"
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_media_cache_preserves_regular_file_behavior(tmp_path: Path) -> None:
    class Client:
        async def fetch_download_url(self, *, download_code: str, robot_code: str) -> str:
            return "https://download.dingtalk.com/report.file"

        async def download_bytes(self, _url: str, *, timeout_seconds: float, max_bytes: int) -> bytes:
            assert max_bytes > len(b"%PDF-1.7\n")
            return b"%PDF-1.7\n"

    message = SimpleNamespace(
        message_id="message-2",
        message_type="file",
        robot_code="robot-code",
        content={"downloadCode": "file-code", "fileName": "report.file"},
    )
    config = DingTalkConfig(
        client_id="client-id",
        robot_code="configured-robot",
        media_cache_dir=str(tmp_path),
    )

    attachment = asyncio.run(DingTalkMediaCache(config=config, client=Client()).collect(message))[0]

    assert attachment.kind == "file"
    assert attachment.mime_type == "application/pdf"
    assert attachment.codec == ""
    assert Path(attachment.path).suffix == ".pdf"


def test_voice_attachment_is_typed_for_pipeline_and_hidden_from_media_prompt() -> None:
    voice = DingTalkMediaAttachment(
        kind="audio",
        mime_type="audio/amr",
        codec="amr-nb",
        path="C:/cache/private-voice.amr",
        filename="voice.amr",
        size_bytes=321,
    )

    normalized = _to_channel_attachment(voice, duration_ms=1200)

    assert normalized.kind is AttachmentKind.AUDIO
    assert normalized.origin is AttachmentOrigin.VOICE_MESSAGE
    assert normalized.path == voice.path
    assert normalized.mime_type == "audio/amr"
    assert normalized.size_bytes == 321
    assert normalized.codec == "amr-nb"
    assert normalized.duration_ms == 1200
    assert normalized.metadata == {
        "channel": "dingtalk",
        "dingtalk_kind": "audio",
        "managed_cache": True,
    }
    assert format_media_for_agent([voice]) == ""


def test_regular_file_attachment_is_marked_as_file_upload() -> None:
    upload = DingTalkMediaAttachment(
        kind="file",
        mime_type="application/pdf",
        path="C:/cache/report.pdf",
        filename="report.pdf",
        size_bytes=123,
    )

    normalized = _to_channel_attachment(upload)

    assert normalized.kind is AttachmentKind.FILE
    assert normalized.origin is AttachmentOrigin.FILE_UPLOAD
    assert "C:/cache/report.pdf" in format_media_for_agent([upload])


def test_voice_only_message_reaches_runner_without_platform_text_or_path(monkeypatch) -> None:
    captured_messages = []
    voice = DingTalkMediaAttachment(
        kind="audio",
        mime_type="audio/amr",
        codec="amr-nb",
        path="C:/cache/private-voice.amr",
        filename="voice.amr",
        size_bytes=321,
    )
    adapter = DingTalkAdapter.__new__(DingTalkAdapter)
    adapter.config = SimpleNamespace(client_id="client-id")
    adapter.dedup = SimpleNamespace(
        is_duplicate=lambda _key: False,
        content_key=lambda *_args: "content-key",
    )
    adapter._message_contexts = {}
    adapter._done_reaction_fired = set()
    adapter._is_user_allowed = lambda *_args, **_kwargs: True
    adapter._should_process_message = lambda **_kwargs: True
    adapter._remember_session_webhook = lambda *_args: None
    adapter._fire_thinking_reaction = lambda *_args: None
    adapter._status_context_text = lambda **_kwargs: ""
    adapter._session_context_text = lambda _source: ""
    adapter._agent_event_callback = lambda _chat_id: lambda *_args: None
    adapter.session_router = SimpleNamespace(
        route=lambda *_args, **_kwargs: SimpleNamespace(session_id="dingtalk-session")
    )
    adapter.command_router = SimpleNamespace(
        handle=lambda text, **_kwargs: SimpleNamespace(handled=False, action="", text=text)
    )

    async def collect(_message, *, message_id: str):
        assert message_id == "message-1"
        return [voice]

    async def no_bind(**_kwargs):
        return None

    async def handle_message(**kwargs):
        captured_messages.append(kwargs["message"])
        return AgentTurnResult(session_id=kwargs["session_id"], final_response="收到")

    adapter.media_cache = SimpleNamespace(collect=collect)
    adapter._maybe_handle_schedule_bind = no_bind
    adapter.runner = SimpleNamespace(
        startup_provider_runtime=SimpleNamespace(model="test-model"),
        get_status=lambda _session_id: "idle",
        session_db=SimpleNamespace(get_messages_as_conversation=lambda _session_id: []),
        handle_message=handle_message,
    )
    monkeypatch.setattr(adapter_module, "register_dingtalk_outbound_target", lambda **_kwargs: None)

    message = _voice_message()
    message.conversation_id = "dm-conversation"
    message.conversation_type = "1"
    message.sender_id = "user-1"
    message.sender_nick = "User One"
    message.sender_staff_id = "staff-1"
    result = asyncio.run(adapter.process_message(message))

    assert result is not None
    assert result.session_id == "dingtalk-session"
    assert len(captured_messages) == 1
    channel_message = captured_messages[0]
    assert channel_message.text == ""
    assert "private-voice.amr" not in channel_message.text
    assert "_mclaw_platform_transcript" not in channel_message.raw_message
    assert "钉钉平台转写" not in str(channel_message.raw_message)
    assert "downloadCode" not in str(channel_message.raw_message)
    assert "download-code" not in str(adapter._message_contexts["dm-conversation"])
    assert not hasattr(adapter._message_contexts["dm-conversation"], "content")
    assert len(channel_message.attachments) == 1
    assert channel_message.attachments[0].kind is AttachmentKind.AUDIO
    assert channel_message.attachments[0].origin is AttachmentOrigin.VOICE_MESSAGE
