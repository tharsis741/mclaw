# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Voice input service lifecycle.

Recorder and backend instances are created when capture starts. Transcripts
pass through the voice policy before entering the normal CLI input queue.
"""

from __future__ import annotations

import queue
import threading
from typing import Callable

from mclaw.voice.filters import TranscriptFilter


def _pcm16_rms(pcm_bytes: bytes) -> int:
    """Compute an inexpensive RMS gate for signed 16-bit mono/stereo PCM."""
    usable = len(pcm_bytes) - (len(pcm_bytes) % 2)
    if usable <= 0:
        return 0
    samples = memoryview(pcm_bytes[:usable]).cast("h")
    if not samples:
        return 0
    total = 0
    for sample in samples:
        value = int(sample)
        total += value * value
    return int((total / len(samples)) ** 0.5)


class VoiceInputService:
    """Coordinate recorder, realtime ASR backend, transcript policy, and CLI hooks."""

    def __init__(
        self,
        config: dict,
        on_text: Callable[[str], None],
        on_interrupt: Callable[[], None] | None = None,
        on_status: Callable[[str], None] | None = None,
    ):
        self.config = config or {}
        self.on_text = on_text
        self.on_interrupt = on_interrupt
        self.on_status = on_status
        self.running = False
        self.recording = False
        self.listen_mode = str(self.config.get("listen_mode") or "wake_word")
        self.last_error = ""
        self._filter = TranscriptFilter(self.config)
        self._recorder = None
        self._backend = None
        self._finishing_manual = False
        self._audio_queue = None
        self._audio_worker = None
        self._audio_stop = None
        self._sent_audio_chunks = 0

    def update_config(self, config: dict):
        """Replace runtime ASR settings without changing the current service state."""
        self.config = config or {}
        self.listen_mode = str(self.config.get("listen_mode") or self.listen_mode or "wake_word")
        self._filter = TranscriptFilter(self.config)

    def start(self, listen_mode: str | None = None):
        """Enable voice input, starting continuous capture only for wake-word mode."""
        listen_mode = str(listen_mode or self.listen_mode or "wake_word")
        if listen_mode not in {"wake_word", "push_to_talk"}:
            self.last_error = f"Unsupported ASR listen mode: {listen_mode}"
            self._status("error")
            return False
        self.listen_mode = listen_mode
        self.config["listen_mode"] = listen_mode
        if not self.config.get("api_key"):
            self.last_error = "DashScope/Qwen API key is not configured."
            self._status("error")
            return False
        self._stop_audio_session()
        self.running = True
        self.last_error = ""
        if self.listen_mode == "wake_word":
            if not self._start_audio_session():
                return False
        else:
            self.recording = False
            self._status(f"{self.listen_mode} ready")
        return True

    def stop(self):
        self._stop_audio_session()
        self.running = False
        self.recording = False
        self._status("off")

    def toggle_push_to_talk(self):
        """Toggle manual recording, committing buffered audio when recording stops."""
        if self.listen_mode != "push_to_talk":
            return False
        if not self.running:
            if not self.start("push_to_talk"):
                return False
        if self.recording:
            self._finish_audio_session(commit=True)
            self._status("push_to_talk ready")
            return True
        return self._start_audio_session()

    def handle_transcript(self, text: str):
        """Route an accepted transcript to prompt submission or interrupt handling."""
        result = self._filter.process(text, mode=self.listen_mode)
        if result.action == "submit" and result.text:
            self.on_text(result.text)
            if self.listen_mode == "push_to_talk":
                if not self._finishing_manual:
                    self._stop_audio_session()
                self._status("push_to_talk ready")
        elif result.action == "interrupt":
            if self.on_interrupt:
                self.on_interrupt()
        return result

    def status(self) -> dict:
        return {
            "running": self.running,
            "recording": self.recording,
            "listen_mode": self.listen_mode,
            "backend": self.config.get("backend", ""),
            "provider": self.config.get("provider", ""),
            "model": self.config.get("model", ""),
            "key_source": self.config.get("key_source", ""),
            "last_error": self.last_error,
        }

    def _status(self, text: str):
        if self.on_status:
            self.on_status(text)

    def _start_audio_session(self) -> bool:
        """Create backend and recorder in order, then bridge audio through a queue."""
        try:
            self._stop_audio_session()
            self._sent_audio_chunks = 0
            from mclaw.voice.qwen_realtime import QwenRealtimeASRBackend
            from mclaw.voice.recorder import AudioRecorder

            if not AudioRecorder.input_available(self.config):
                raise RuntimeError("No audio input device detected by configured recorder backends.")

            backend_config = dict(self.config)
            backend_config["enable_server_vad"] = self.listen_mode == "wake_word"
            self._backend = QwenRealtimeASRBackend(
                backend_config,
                on_transcript=self.handle_transcript,
                on_error=self._handle_backend_error,
                on_status=self._status,
            )
            self._backend.connect()
            self._start_audio_worker()
            self._recorder = AudioRecorder(self.config)
            self._recorder.start(self._send_audio)
            self.recording = True
            self.running = True
            self.last_error = ""
            self._status(f"{self.listen_mode} recording")
            return True
        except Exception as exc:
            self.last_error = str(exc)
            self.recording = False
            self.running = False
            self._stop_audio_session()
            self._status("error")
            return False

    def _stop_audio_session(self):
        """Tear down recorder, worker, and backend for cancellation/off states."""
        recorder = self._recorder
        backend = self._backend
        self._recorder = None
        self._backend = None
        self._stop_audio_worker()
        if recorder is not None:
            try:
                recorder.stop()
            except Exception:
                pass
        if backend is not None:
            try:
                backend.close()
            except Exception:
                pass
        self.recording = False

    def _finish_audio_session(self, commit: bool = False):
        """Stop capture and optionally commit queued audio for manual ASR turns."""
        recorder = self._recorder
        backend = self._backend
        audio_queue = self._audio_queue
        self._recorder = None
        if recorder is not None:
            try:
                recorder.stop()
            except Exception:
                pass
        self._stop_audio_worker(wait=True)
        self._backend = None
        if backend is not None:
            self._drain_audio_queue(audio_queue, backend)
            self._finishing_manual = bool(commit)
            try:
                min_chunks = int(self.config.get("min_voice_chunks") or 0)
                should_commit = bool(commit) and self._sent_audio_chunks >= min_chunks
                if hasattr(backend, "finish"):
                    backend.finish(commit=should_commit, timeout=int(self.config.get("manual_commit_timeout") or 10))
                else:
                    backend.close()
                if commit and not should_commit:
                    self._status("push_to_talk no speech")
            except Exception as exc:
                self._handle_backend_error(str(exc))
            finally:
                self._finishing_manual = False
        self.recording = False

    def _drain_audio_queue(self, audio_queue, backend):
        """Flush already accepted audio before committing a manual recording."""
        if audio_queue is None or backend is None:
            return
        while True:
            try:
                chunk = audio_queue.get_nowait()
            except queue.Empty:
                break
            try:
                backend.send_audio(chunk)
            except Exception as exc:
                self._handle_backend_error(str(exc))
                break

    def _send_audio(self, pcm_bytes: bytes):
        """Accept audio from recorder callbacks without blocking capture threads."""
        audio_queue = self._audio_queue
        if audio_queue is None:
            return
        if not self._is_voice_audio(pcm_bytes):
            return
        try:
            audio_queue.put_nowait(pcm_bytes)
            self._sent_audio_chunks += 1
        except queue.Full:
            # Dropping a chunk is preferable to blocking PortAudio's callback.
            pass

    def _is_voice_audio(self, pcm_bytes: bytes) -> bool:
        threshold = int(self.config.get("min_audio_rms") or 0)
        if threshold <= 0:
            return True
        return _pcm16_rms(pcm_bytes) >= threshold

    def _handle_backend_error(self, message: str):
        self.last_error = message
        self._status("error")

    def _start_audio_worker(self):
        """Start the worker that serializes recorder callbacks into backend sends."""
        self._audio_queue = queue.Queue(maxsize=int(self.config.get("audio_queue_size") or 50))
        self._audio_stop = threading.Event()
        audio_queue = self._audio_queue
        stop = self._audio_stop

        def _worker():
            while not stop.is_set():
                try:
                    chunk = audio_queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                backend = self._backend
                if backend is None:
                    continue
                try:
                    backend.send_audio(chunk)
                except Exception as exc:
                    self._handle_backend_error(str(exc))

        self._audio_worker = threading.Thread(target=_worker, daemon=True)
        self._audio_worker.start()

    def _stop_audio_worker(self, wait: bool = False):
        """Signal the audio worker and release queue references during teardown."""
        stop = self._audio_stop
        worker = self._audio_worker
        self._audio_stop = None
        self._audio_worker = None
        self._audio_queue = None
        if stop is not None:
            stop.set()
        if worker is not None:
            worker.join(timeout=None if wait else 1.0)
