# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Microphone recorder used by M-Claw voice input flows."""

from __future__ import annotations


class AudioRecorder:
    def __init__(self, config: dict):
        self.config = config or {}
        self.running = False
        self._stream = None

    def start(self, on_audio_chunk):
        try:
            import sounddevice as sd
        except ImportError as exc:
            raise RuntimeError("Install voice dependencies with: pip install -e .[voice]") from exc
        if self.running:
            return

        sample_rate = int(self.config.get("sample_rate") or 16000)
        channels = int(self.config.get("channels") or 1)
        block_ms = int(self.config.get("chunk_ms") or 100)
        blocksize = max(1, int(sample_rate * block_ms / 1000))

        def _callback(indata, frames, time_info, status):
            if status:
                # Non-fatal under load; the ASR backend can tolerate dropped chunks.
                pass
            on_audio_chunk(indata.tobytes())

        self._stream = sd.InputStream(
            samplerate=sample_rate,
            channels=channels,
            dtype="int16",
            blocksize=blocksize,
            callback=_callback,
        )
        self._stream.start()
        self.running = True

    def stop(self):
        stream = self._stream
        self._stream = None
        if stream is not None:
            try:
                stream.stop()
            finally:
                stream.close()
        self.running = False
