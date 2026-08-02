# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import io
import json
import math
import struct
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest
import httpx
from openai import APIConnectionError, AsyncOpenAI

from mclaw.channels.audio_transcription import AudioTranscriptionCapability
from mclaw.channels.base import (
    AttachmentKind,
    AttachmentOrigin,
    ChannelAttachment,
    ChannelMessage,
    ChannelSource,
)
from mclaw.channels.inbound_pipeline import InboundCapabilityError
from mclaw.voice.audio_probe import probe_audio
from mclaw.voice.codecs import AudioDecodeError
from mclaw.voice.codecs.silk import SilkDecoder
from mclaw.voice.config import resolve_inbound_audio_config
from mclaw.voice.providers.qwen_file import QwenFileTranscriber
from mclaw.voice.transcription import (
    AudioTranscriptionError,
    TranscriptionRequest,
    TranscriptionResult,
    validate_audio_service_url,
)


def _write_wav(path: Path, *, rate: int = 16000, duration_ms: int = 100) -> None:
    frames = rate * duration_ms // 1000
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\0\0" * frames)


def _write_amr(path: Path, *, frames: int) -> None:
    # AMR-NB mode 0: one TOC byte plus 12 payload bytes, 20 ms per frame.
    path.write_bytes(b"#!AMR\n" + (b"\x04" + b"\0" * 12) * frames)


def _ogg_page(*, body: bytes, granule: int, serial: int = 1, sequence: int = 0) -> bytes:
    assert len(body) < 256
    return b"".join((
        b"OggS",
        b"\0\0",
        granule.to_bytes(8, "little"),
        serial.to_bytes(4, "little"),
        sequence.to_bytes(4, "little"),
        b"\0\0\0\0",
        b"\x01",
        bytes([len(body)]),
        body,
    ))


def _config(**overrides):
    result = {
        "enabled": True,
        "auto_transcribe_voice_messages": True,
        "provider": "qwen",
        "api_key": "test-key",
        "base_url": "https://example.test/compatible-mode/v1",
        "model": "qwen3-asr-flash",
        "language": "auto",
        "enable_itn": False,
        "max_audio_bytes": 10 * 1024 * 1024,
        "max_duration_seconds": 120,
        "transcription_timeout_seconds": 10,
        "max_concurrency": 2,
        "max_retries": 0,
        "retry_backoff_seconds": 0,
        "retain_source_seconds": 0,
        "retain_decoded_seconds": 0,
        "silk_sample_rate": 24000,
    }
    result.update(overrides)
    return result


def test_probe_audio_prefers_magic_and_reads_wav_properties(tmp_path: Path) -> None:
    path = tmp_path / "wrong.mp3"
    _write_wav(path, rate=16000, duration_ms=250)

    info = probe_audio(path, mime_hint="audio/mpeg", codec_hint="mp3")

    assert info.container == "wav"
    assert info.codec == "pcm"
    assert info.mime_type == "audio/wav"
    assert info.sample_rate == 16000
    assert info.channels == 1
    assert info.duration_ms == 250
    assert info.duration_verified is True


def test_probe_audio_verifies_amr_duration_from_frames(tmp_path: Path) -> None:
    path = tmp_path / "voice.amr"
    _write_amr(path, frames=5)

    info = probe_audio(path, duration_ms_hint=1)

    assert info.codec == "amr-nb"
    assert info.duration_ms == 100
    assert info.duration_verified is True


def test_probe_audio_verifies_common_compressed_durations(tmp_path: Path) -> None:
    ogg = tmp_path / "voice.ogg"
    ogg.write_bytes(
        _ogg_page(body=b"OpusHead" + b"\0" * 11, granule=0)
        + _ogg_page(body=b"\0", granule=4800, sequence=1)
    )

    mp3 = tmp_path / "voice.mp3"
    mp3.write_bytes(b"\xff\xfb\x90\x64" + b"\0" * 413)

    aac = tmp_path / "voice.aac"
    aac.write_bytes(b"\xff\xf1\x50\x80\x00\xff\xfc")

    flac = tmp_path / "voice.flac"
    stream_info = bytearray(34)
    stream_info[10:18] = ((44100 << 44) | 44100).to_bytes(8, "big")
    flac.write_bytes(b"fLaC\x80\x00\x00\x22" + stream_info)

    assert probe_audio(ogg).duration_ms == 100
    assert probe_audio(mp3).duration_ms == 26
    assert probe_audio(aac, mime_hint="audio/aac", codec_hint="aac").duration_ms == 23
    assert probe_audio(flac).duration_ms == 1000
    assert all(probe_audio(path, **hints).duration_verified for path, hints in (
        (ogg, {}),
        (mp3, {}),
        (aac, {"mime_hint": "audio/aac", "codec_hint": "aac"}),
        (flac, {}),
    ))


def test_capability_rejects_forged_short_duration_using_audio_content(tmp_path: Path) -> None:
    path = tmp_path / "voice.amr"
    _write_amr(path, frames=51)  # 1.02 s, despite the forged 100 ms hint below.

    class UnexpectedTranscriber:
        async def transcribe(self, _request):
            raise AssertionError("overlong audio must not reach ASR")

    capability = AudioTranscriptionCapability(
        _config(max_duration_seconds=1),
        transcriber=UnexpectedTranscriber(),
    )
    message = ChannelMessage(
        text="",
        source=ChannelSource(channel="dingtalk", chat_id="chat"),
        attachments=(ChannelAttachment(
            kind=AttachmentKind.AUDIO,
            origin=AttachmentOrigin.VOICE_MESSAGE,
            path=str(path),
            mime_type="audio/amr",
            codec="amr-nb",
            duration_ms=100,
            metadata={"managed_cache": False},
        ),),
    )

    with pytest.raises(InboundCapabilityError) as captured:
        asyncio.run(capability.process(message))

    assert captured.value.code == "AUDIO_TOO_LONG"


def test_silk_decoder_round_trip_outputs_wav(tmp_path: Path) -> None:
    pysilk = pytest.importorskip("pysilk")
    rate = 24000
    pcm = b"".join(
        struct.pack("<h", int(1000 * math.sin(2 * math.pi * 440 * index / rate)))
        for index in range(rate // 10)
    )
    encoded = io.BytesIO()
    pysilk.encode(io.BytesIO(pcm), encoded, rate, 24000)
    source = tmp_path / "voice.silk"
    source.write_bytes(encoded.getvalue())

    decoded = asyncio.run(SilkDecoder(output_dir=tmp_path).decode(source, sample_rate=rate))

    output = Path(decoded.path)
    assert output.read_bytes().startswith(b"RIFF")
    assert decoded.info.mime_type == "audio/wav"
    assert decoded.info.duration_ms == 100

    with pytest.raises(AudioDecodeError, match="output limit"):
        asyncio.run(SilkDecoder(output_dir=tmp_path).decode(
            source,
            sample_rate=rate,
            max_output_bytes=100,
        ))


def test_qwen_file_provider_uses_base64_and_normalizes_response(tmp_path: Path) -> None:
    path = tmp_path / "voice.wav"
    _write_wav(path)
    calls = []

    class Completions:
        async def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(
                    content="你好，世界",
                    annotations=[{"type": "audio_info", "language": "zh"}],
                ))],
                usage=SimpleNamespace(seconds=1),
            )

    def factory(**kwargs):
        assert kwargs["api_key"] == "test-key"
        assert kwargs["base_url"] == "https://example.test/compatible-mode/v1"
        assert kwargs["max_retries"] == 0
        return SimpleNamespace(chat=SimpleNamespace(completions=Completions()))

    provider = QwenFileTranscriber(
        api_key="test-key",
        base_url="https://example.test/compatible-mode/v1",
        provider="qwen-intl",
        client_factory=factory,
    )
    result = asyncio.run(provider.transcribe(TranscriptionRequest(
        path=str(path),
        mime_type="audio/wav",
        codec="pcm",
    )))

    assert result.text == "你好，世界"
    assert result.provider == "qwen-intl"
    assert result.language == "zh"
    assert result.duration_ms == 1000
    assert result.source_hash.startswith("sha256:")
    audio_data = calls[0]["messages"][0]["content"][0]["input_audio"]["data"]
    assert audio_data.startswith("data:audio/wav;base64,")
    assert str(path) not in audio_data
    assert calls[0]["extra_body"] == {"asr_options": {"enable_itn": False}}


def test_capability_passes_resolved_provider_to_default_transcriber() -> None:
    capability = AudioTranscriptionCapability(_config(provider="qwen-intl"))

    transcriber = capability._get_transcriber()

    assert isinstance(transcriber, QwenFileTranscriber)
    assert transcriber.provider == "qwen-intl"


def test_qwen_file_provider_matches_async_openai_wire_contract(tmp_path: Path) -> None:
    path = tmp_path / "voice.wav"
    _write_wav(path)
    requests: list[httpx.Request] = []

    async def scenario() -> TranscriptionResult:
        async def handle(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-test",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "qwen3-asr-flash",
                    "choices": [{
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": "真实 SDK 契约",
                            "annotations": [{"type": "audio_info", "language": "zh"}],
                        },
                    }],
                    "usage": {
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "total_tokens": 2,
                        "seconds": 0.1,
                    },
                },
            )

        http_client = httpx.AsyncClient(transport=httpx.MockTransport(handle))

        def factory(**kwargs):
            return AsyncOpenAI(**kwargs, http_client=http_client)

        try:
            provider = QwenFileTranscriber(
                api_key="test-key",
                base_url="https://example.test/compatible-mode/v1",
                client_factory=factory,
            )
            return await provider.transcribe(TranscriptionRequest(
                path=str(path),
                mime_type="audio/wav",
                codec="pcm",
            ))
        finally:
            await http_client.aclose()

    result = asyncio.run(scenario())

    assert result.text == "真实 SDK 契约"
    assert len(requests) == 1
    request = requests[0]
    assert request.url.path == "/compatible-mode/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer test-key"
    body = json.loads(request.content)
    assert body["model"] == "qwen3-asr-flash"
    assert body["stream"] is False
    assert body["asr_options"] == {"enable_itn": False}
    assert body["messages"][0]["content"][0]["type"] == "input_audio"
    assert body["messages"][0]["content"][0]["input_audio"]["data"].startswith(
        "data:audio/wav;base64,"
    )


def test_qwen_file_provider_cancels_inflight_request_on_timeout(tmp_path: Path) -> None:
    path = tmp_path / "voice.wav"
    _write_wav(path)

    async def scenario() -> None:
        request_started = asyncio.Event()
        request_cancelled = asyncio.Event()

        class Completions:
            async def create(self, **_kwargs):
                request_started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    request_cancelled.set()
                    raise

        provider = QwenFileTranscriber(
            api_key="test-key",
            base_url="https://example.test/compatible-mode/v1",
            timeout_seconds=0.5,
            client_factory=lambda **_kwargs: SimpleNamespace(
                chat=SimpleNamespace(completions=Completions())
            ),
        )
        with pytest.raises(AudioTranscriptionError) as captured:
            await provider.transcribe(TranscriptionRequest(
                path=str(path),
                mime_type="audio/wav",
                codec="pcm",
            ))
        assert captured.value.code == "ASR_TIMEOUT"
        assert request_started.is_set()
        assert request_cancelled.is_set()

    asyncio.run(scenario())


def test_qwen_file_provider_rejects_unsupported_container_before_request(tmp_path: Path) -> None:
    path = tmp_path / "voice.m4a"
    path.write_bytes(b"\x00\x00\x00\x18ftypM4A \x00\x00\x00\x00")
    provider = QwenFileTranscriber(
        api_key="test-key",
        base_url="https://example.test/compatible-mode/v1",
        client_factory=lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("unsupported audio must not reach the ASR client")
        ),
    )

    with pytest.raises(AudioTranscriptionError) as captured:
        asyncio.run(provider.transcribe(TranscriptionRequest(
            path=str(path),
            mime_type="audio/mp4",
            codec="aac",
        )))

    assert captured.value.code == "AUDIO_CODEC_UNSUPPORTED"


def test_qwen_file_provider_marks_connection_failures_retryable(tmp_path: Path) -> None:
    path = tmp_path / "voice.wav"
    _write_wav(path)

    class Completions:
        async def create(self, **_kwargs):
            raise APIConnectionError(request=httpx.Request(
                "POST",
                "https://example.test/compatible-mode/v1/chat/completions",
            ))

    provider = QwenFileTranscriber(
        api_key="test-key",
        base_url="https://example.test/compatible-mode/v1",
        client_factory=lambda **_kwargs: SimpleNamespace(
            chat=SimpleNamespace(completions=Completions())
        ),
    )

    with pytest.raises(AudioTranscriptionError) as captured:
        asyncio.run(provider.transcribe(TranscriptionRequest(
            path=str(path),
            mime_type="audio/wav",
            codec="pcm",
        )))

    assert captured.value.code == "ASR_PROVIDER_ERROR"
    assert captured.value.retryable is True


def test_capability_ignores_platform_transcript_and_composes_asr_text(tmp_path: Path) -> None:
    path = tmp_path / "voice.wav"
    _write_wav(path)
    requests = []

    class Transcriber:
        async def transcribe(self, request):
            requests.append(request)
            return TranscriptionResult(
                text="M-Claw 自己识别的内容",
                provider="fake",
                model="fake-asr",
                language="zh",
            )

    capability = AudioTranscriptionCapability(_config(), transcriber=Transcriber())
    message = ChannelMessage(
        text="",
        source=ChannelSource(channel="dingtalk", chat_id="chat"),
        attachments=(ChannelAttachment(
            kind=AttachmentKind.AUDIO,
            origin=AttachmentOrigin.VOICE_MESSAGE,
            path=str(path),
            mime_type="audio/wav",
            metadata={"managed_cache": False},
        ),),
        raw_message={"_mclaw_platform_transcript": "钉钉平台识别的内容"},
    )

    processed = asyncio.run(capability.process(message))

    assert processed.text == "M-Claw 自己识别的内容"
    assert "钉钉平台识别" not in processed.text
    assert len(requests) == 1
    assert processed.capability_results["audio_transcription"][0]["provider"] == "fake"


def test_capability_does_not_auto_transcribe_uploaded_audio_file(tmp_path: Path) -> None:
    path = tmp_path / "meeting.wav"
    _write_wav(path)

    class UnexpectedTranscriber:
        async def transcribe(self, _request):
            raise AssertionError("file upload must not be auto-transcribed")

    capability = AudioTranscriptionCapability(_config(), transcriber=UnexpectedTranscriber())
    message = ChannelMessage(
        text="请看附件",
        source=ChannelSource(channel="dingtalk", chat_id="chat"),
        attachments=(ChannelAttachment(
            kind=AttachmentKind.AUDIO,
            origin=AttachmentOrigin.FILE_UPLOAD,
            path=str(path),
            mime_type="audio/wav",
        ),),
    )

    assert asyncio.run(capability.process(message)) is message


def test_disabled_capability_fails_before_agent_with_safe_message(tmp_path: Path) -> None:
    path = tmp_path / "voice.wav"
    _write_wav(path)
    capability = AudioTranscriptionCapability(_config(enabled=False))
    message = ChannelMessage(
        text="",
        source=ChannelSource(channel="weixin", chat_id="chat"),
        attachments=(ChannelAttachment(
            kind=AttachmentKind.AUDIO,
            origin=AttachmentOrigin.VOICE_MESSAGE,
            path=str(path),
            mime_type="audio/wav",
            metadata={"managed_cache": True},
        ),),
    )

    with pytest.raises(InboundCapabilityError) as captured:
        asyncio.run(capability.process(message))

    assert captured.value.code == "ASR_NOT_CONFIGURED"
    assert "语音识别尚未配置" in captured.value.safe_message
    assert not path.exists()


def test_disabled_voice_auto_transcription_cleans_cache_and_fails_safely(tmp_path: Path) -> None:
    path = tmp_path / "voice.wav"
    _write_wav(path)
    capability = AudioTranscriptionCapability(_config(auto_transcribe_voice_messages=False))
    message = ChannelMessage(
        text="",
        source=ChannelSource(channel="dingtalk", chat_id="chat"),
        attachments=(ChannelAttachment(
            kind=AttachmentKind.AUDIO,
            origin=AttachmentOrigin.VOICE_MESSAGE,
            path=str(path),
            mime_type="audio/wav",
            metadata={"managed_cache": True},
        ),),
    )

    with pytest.raises(InboundCapabilityError) as captured:
        asyncio.run(capability.process(message))

    assert captured.value.code == "ASR_VOICE_DISABLED"
    assert not path.exists()


def test_inbound_config_auto_enables_with_authorized_asr_key(monkeypatch) -> None:
    monkeypatch.setattr(
        "mclaw.voice.config._authorized_env_value",
        lambda name: "authorized-key" if name == "DASHSCOPE_API_KEY" else "",
    )

    config = resolve_inbound_audio_config(config={
        "capabilities": {"inbound_audio": {"enabled": "auto"}},
    })

    assert config["enabled"] is True
    assert config["enabled_mode"] == "auto"
    assert config["api_key"] == "authorized-key"
    assert config["model"] == "qwen3-asr-flash"


def test_inbound_config_unknown_provider_fails_closed(monkeypatch) -> None:
    credential_reads = []
    monkeypatch.setattr(
        "mclaw.voice.config._authorized_env_value",
        lambda name: credential_reads.append(name) or "must-not-be-used",
    )

    config = resolve_inbound_audio_config(config={
        "capabilities": {"inbound_audio": {
            "enabled": True,
            "provider": "local-whisper",
        }},
    })

    assert config["provider"] == "local-whisper"
    assert config["enabled"] is False
    assert config["api_key"] == ""
    assert config["base_url"] == ""
    assert "unsupported inbound ASR provider" in config["configuration_error"]
    assert credential_reads == []


def test_inbound_config_rejects_plain_http_remote_endpoint(monkeypatch) -> None:
    monkeypatch.setattr(
        "mclaw.voice.config._authorized_env_value",
        lambda name: "authorized-key" if name == "DASHSCOPE_API_KEY" else "",
    )

    config = resolve_inbound_audio_config(config={
        "capabilities": {"inbound_audio": {
            "enabled": True,
            "provider": "qwen",
            "base_url": "http://asr.example.test/v1",
        }},
    })

    assert config["enabled"] is False
    assert config["api_key"] == ""
    assert "loopback" in config["configuration_error"]
    assert validate_audio_service_url("http://127.0.0.1:8000/v1") == "http://127.0.0.1:8000/v1"


@pytest.mark.parametrize(
    ("enabled_raw", "expected_mode"),
    [(True, "on"), ("auto", "auto")],
)
def test_inbound_config_rejects_unsupported_silk_sample_rate(
    monkeypatch,
    enabled_raw,
    expected_mode: str,
) -> None:
    monkeypatch.setattr(
        "mclaw.voice.config._authorized_env_value",
        lambda name: "authorized-key" if name == "DASHSCOPE_API_KEY" else "",
    )

    config = resolve_inbound_audio_config(config={
        "capabilities": {"inbound_audio": {
            "enabled": enabled_raw,
            "silk_sample_rate": 44100,
        }},
    })

    assert config["silk_sample_rate"] == 44100
    assert config["enabled"] is False
    assert config["enabled_mode"] == expected_mode
    assert "unsupported SILK sample rate: 44100" in config["configuration_error"]
    assert "8000, 12000, 16000, 24000" in config["configuration_error"]


def test_doctor_rejects_unsupported_silk_sample_rate() -> None:
    from mclaw import doctor

    results: list[doctor.CheckResult] = []
    doctor._append_inbound_audio_checks(results, {
        "capabilities": {"inbound_audio": {
            "enabled": True,
            "silk_sample_rate": 44100,
        }},
    })

    assert results[0].name == "inbound audio configuration"
    assert results[0].ok is False
    assert "unsupported SILK sample rate: 44100" in results[0].detail
