# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import threading

from mclaw.voice.config import resolve_asr_config
from mclaw.voice.filters import TranscriptFilter
from mclaw.voice.qwen_realtime import QwenRealtimeASRBackend
from mclaw.voice.service import VoiceInputService


def test_legacy_once_mode_falls_back_to_guarded_wake_word() -> None:
    config = resolve_asr_config(config={
        "auxiliary": {
            "asr": {
                "enabled": False,
                "listen_mode": "once",
                "require_wake_word": False,
            },
        },
    })

    assert config["listen_mode"] == "wake_word"
    assert config["require_wake_word"] is True


def test_voice_service_rejects_removed_once_mode() -> None:
    statuses = []
    service = VoiceInputService(
        {"listen_mode": "wake_word", "api_key": "configured"},
        on_text=lambda text: None,
        on_status=statuses.append,
    )

    assert service.start("once") is False
    assert service.last_error == "Unsupported ASR listen mode: once"
    assert statuses == ["error"]


def test_wake_word_filter_keeps_existing_behavior() -> None:
    transcript_filter = TranscriptFilter({
        "listen_mode": "wake_word",
        "require_wake_word": True,
        "wake_words": ["小爪"],
    })

    wake_only = transcript_filter.process("小爪")
    assert (wake_only.action, wake_only.text) == ("submit", "小爪")

    missing_wake = transcript_filter.process("打开浏览器")
    assert (missing_wake.action, missing_wake.reason) == ("discard", "missing_wake_word")

    inline = transcript_filter.process("小爪，打开终端")
    assert (inline.action, inline.text) == ("submit", "打开终端")


def test_push_to_talk_waits_for_inflight_audio_and_flushes_tail() -> None:
    class Recorder:
        def stop(self):
            pass

    class Backend:
        def __init__(self):
            self.started = threading.Event()
            self.release = threading.Event()
            self.order = []

        def send_audio(self, chunk):
            self.started.set()
            self.release.wait(3)
            self.order.append(("audio", chunk))

        def finish(self, commit=False, timeout=10):
            self.order.append(("finish", commit, timeout))

    service = VoiceInputService(
        {"listen_mode": "push_to_talk", "min_voice_chunks": 1},
        on_text=lambda text: None,
    )
    backend = Backend()
    service._recorder = Recorder()
    service._backend = backend
    service._start_audio_worker()
    service._send_audio(b"inflight")
    service._send_audio(b"tail")
    assert backend.started.wait(1)

    finished = threading.Event()

    def finish():
        service._finish_audio_session(commit=True)
        finished.set()

    worker = threading.Thread(target=finish, daemon=True)
    worker.start()
    try:
        assert not finished.wait(1.1)
    finally:
        backend.release.set()
    assert finished.wait(1)
    worker.join()

    assert backend.order == [
        ("audio", b"inflight"),
        ("audio", b"tail"),
        ("finish", True, 10),
    ]


def test_qwen_partial_preview_and_transcription_failure() -> None:
    statuses = []
    errors = []
    backend = QwenRealtimeASRBackend(
        {},
        on_transcript=lambda text: None,
        on_error=errors.append,
        on_status=statuses.append,
    )

    backend._handle_event({
        "type": "conversation.item.input_audio_transcription.text",
        "text": "今天",
        "stash": "天气",
    })
    backend._handle_event({
        "type": "conversation.item.input_audio_transcription.failed",
        "error": {"message": "bad audio"},
    })

    assert statuses == ["hearing 今天天气"]
    assert errors == ["bad audio"]
