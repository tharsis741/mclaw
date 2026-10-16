# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import asyncio
import io
import wave
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from mclaw.application.sessions import ApplicationError
from mclaw.application.voice import VoiceService
from mclaw.voice.transcription import TranscriptionResult
from mclaw.web.app import create_app


@pytest.fixture
def voice_home(tmp_path, monkeypatch):
    from mclaw.providers.registry import get_runtime_profile

    monkeypatch.setenv("MCLAW_HOME", str(tmp_path))
    for provider in ("qwen", "qwen-intl"):
        for key in get_runtime_profile(provider).env_vars:
            monkeypatch.delenv(key, raising=False)
    (tmp_path / "config.yaml").write_text("active_provider: custom\nmodel: original-model\n", encoding="utf-8")
    return tmp_path


def wav_data(seconds=1):
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\0\0" * (16000 * seconds))
    return output.getvalue()


def test_voice_reuses_qwen_credentials_with_only_asr_scope_and_keeps_model_and_im_settings(voice_home):
    from mclaw.cli.config import load_config, save_env_value
    from mclaw.runtime.secrets import is_authorized

    save_env_value("DASHSCOPE_API_KEY", "fake-local-asr-key")
    service = VoiceService(voice_home)
    initial = service.status()
    assert initial["credential_configured"] and not initial["ready"]
    before = load_config()
    configured = service.configure({"enabled": True, "provider": "qwen"})
    assert configured["ready"] and configured["authorized"]
    assert "fake-local-asr-key" not in str(configured)
    assert is_authorized("runtime:asr", "DASHSCOPE_API_KEY")
    after = load_config()
    assert after["model"] == "original-model"
    assert after["capabilities"]["inbound_audio"] == before["capabilities"]["inbound_audio"]
    assert not service.configure({"enabled": False})["ready"]
    assert service.status()["credential_configured"]


def test_voice_explicit_international_region_uses_international_credential(voice_home):
    from mclaw.cli.config import get_env_value
    from mclaw.providers.registry import get_runtime_profile

    service = VoiceService(voice_home)
    state = service.configure({"enabled": True, "provider": "qwen-intl", "api_key": "international-test-key"})
    assert state["ready"] and state["region"] == "intl"
    assert get_env_value(get_runtime_profile("qwen-intl").env_vars[0]) == "international-test-key"


def test_voice_requires_key_and_waits_for_active_setup(voice_home):
    from mclaw.application.setup import _WORKFLOW_LOCK

    service = VoiceService(voice_home)
    with pytest.raises(ApplicationError, match="Qwen API Key"):
        service.configure({"enabled": True})
    _WORKFLOW_LOCK.acquire()
    try:
        with pytest.raises(ApplicationError) as error:
            service.configure({"enabled": False})
        assert error.value.code == "SETUP_IN_PROGRESS"
    finally:
        _WORKFLOW_LOCK.release()


def test_invalid_voice_configuration_does_not_save_submitted_credential(voice_home):
    (voice_home / "config.yaml").write_text("[invalid yaml", encoding="utf-8")
    service = VoiceService(voice_home)
    for operation in (service.status, lambda: service.configure({"enabled": True, "provider": "qwen", "api_key": "must-not-save"})):
        with pytest.raises(ApplicationError) as error:
            operation()
        assert error.value.code == "CONFIG_INVALID"
    assert not (voice_home / ".env").exists()


@pytest.mark.asyncio
async def test_voice_transcribes_verified_recording_and_removes_temporary_audio(voice_home):
    requests = []
    class FakeTranscriber:
        def __init__(self, **_config):
            pass
        async def transcribe(self, request):
            requests.append(request)
            assert Path(request.path).read_bytes().startswith(b"RIFF")
            return TranscriptionResult(text="保留原草稿后追加这句话", provider="qwen", model="qwen3-asr-flash")

    service = VoiceService(voice_home, transcriber_factory=FakeTranscriber)
    service.configure({"enabled": True, "api_key": "test-only-key"})
    result = await service.transcribe(wav_data(), "audio/wav", 1000)
    assert result["duration_ms"] == 1000
    assert result["text"] == "保留原草稿后追加这句话"
    assert not Path(requests[0].path).exists()


@pytest.mark.parametrize("audio,mime,duration,code", [
    (b"invalid data", "audio/webm", 1000, "AUDIO_INVALID"),
    (b"", "audio/wav", 1000, "AUDIO_TOO_LARGE"),
    (b"test", "audio/mp4", 1000, "AUDIO_FORMAT_UNSUPPORTED"),
    (b"test", "audio/wav", 120001, "AUDIO_DURATION_INVALID"),
    (wav_data(121), "audio/wav", 1000, "AUDIO_DURATION_INVALID"),
], ids=["unrecognized", "empty", "unsupported", "duration-header", "duration-content"])
@pytest.mark.asyncio
async def test_invalid_voice_upload_never_reaches_provider(voice_home, audio, mime, duration, code):
    service = VoiceService(voice_home, transcriber_factory=lambda **_: pytest.fail("invalid audio reached provider"))
    service.configure({"enabled": True, "api_key": "test-only-key"})
    with pytest.raises(ApplicationError) as error:
        await service.transcribe(audio, mime, duration)
    assert error.value.code == code
    assert not list((voice_home / "web" / "recordings").glob("*.bin"))


@pytest.mark.asyncio
async def test_service_shutdown_cancels_transcription_and_cleans_recording(voice_home):
    started = asyncio.Event()
    class WaitingTranscriber:
        def __init__(self, **_config):
            pass
        async def transcribe(self, _request):
            started.set()
            await asyncio.Event().wait()

    service = VoiceService(voice_home, transcriber_factory=WaitingTranscriber)
    service.configure({"enabled": True, "api_key": "test-only-key"})
    task = asyncio.create_task(service.transcribe(wav_data(), "audio/wav", 1000))
    await started.wait()
    await service.close()
    assert task.cancelled()
    assert not list((voice_home / "web" / "recordings").glob("*.bin"))


def test_voice_configuration_requires_local_authentication(voice_home):
    with TestClient(create_app(voice_home, "test-local"), base_url="http://127.0.0.1") as client:
        assert client.put("/api/v1/voice", json={"enabled": False}).status_code == 403
        response = client.get("/api/v1/voice", headers={"Authorization": "Bearer test-local"})
        assert response.status_code == 200
        assert response.json()["max_seconds"] == 120
