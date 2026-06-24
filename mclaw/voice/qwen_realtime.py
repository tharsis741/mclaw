"""Qwen realtime ASR backend."""

from __future__ import annotations

import base64


class QwenRealtimeASRBackend:
    def __init__(self, config: dict, on_transcript, on_error=None, on_status=None):
        self.config = config or {}
        self.on_transcript = on_transcript
        self.on_error = on_error
        self.on_status = on_status
        self.connected = False
        self._conversation = None

    def connect(self):
        try:
            import dashscope
            from dashscope.audio.qwen_omni import (
                MultiModality,
                OmniRealtimeCallback,
                OmniRealtimeConversation,
            )
            from dashscope.audio.qwen_omni.omni_realtime import TranscriptionParams
        except ImportError as exc:
            raise RuntimeError(
                f"Qwen realtime ASR SDK import failed: {exc}. "
                "Install or upgrade voice dependencies with: pip install -e .[voice]"
            ) from exc

        api_key = self.config.get("api_key") or ""
        if not api_key:
            raise RuntimeError("DashScope/Qwen API key is not configured.")
        dashscope.api_key = api_key

        backend = self

        class Callback(OmniRealtimeCallback):
            def on_open(self):
                backend._status("connected")

            def on_close(self, close_status_code, close_msg):
                backend.connected = False
                backend._status(f"closed {close_status_code}")

            def on_event(self, response):
                try:
                    event_type = response.get("type", "")
                    if event_type == "conversation.item.input_audio_transcription.completed":
                        transcript = (response.get("transcript") or "").strip()
                        if transcript:
                            backend.on_transcript(transcript)
                    elif event_type == "conversation.item.input_audio_transcription.text":
                        stash = response.get("stash") or ""
                        if stash:
                            backend._status(f"hearing {stash[:24]}")
                    elif event_type == "input_audio_buffer.speech_started":
                        backend._status("speech started")
                    elif event_type == "input_audio_buffer.speech_stopped":
                        backend._status("speech stopped")
                    elif event_type == "error":
                        message = str(response.get("error") or response)
                        backend._error(message)
                except Exception as exc:
                    backend._error(str(exc))

        self._conversation = OmniRealtimeConversation(
            model=self.config.get("model") or "qwen3-asr-flash-realtime",
            url=self.config.get("websocket_url") or "wss://dashscope.aliyuncs.com/api-ws/v1/realtime",
            callback=Callback(),
            api_key=api_key,
        )
        self._conversation.connect()
        transcription_params = TranscriptionParams(
            language=self.config.get("language") or "zh",
            sample_rate=int(self.config.get("sample_rate") or 16000),
            input_audio_format=self.config.get("input_audio_format") or "pcm",
        )
        self._conversation.update_session(
            output_modalities=[MultiModality.TEXT],
            enable_turn_detection=bool(self.config.get("enable_server_vad", True)),
            turn_detection_type="server_vad",
            turn_detection_threshold=float(self.config.get("vad_threshold", 0.0)),
            turn_detection_silence_duration_ms=int(self.config.get("silence_duration_ms") or 400),
            enable_input_audio_transcription=True,
            transcription_params=transcription_params,
        )
        self.connected = True
        self._status("ready")

    def send_audio(self, pcm_bytes: bytes):
        if not self.connected:
            raise RuntimeError("Qwen ASR backend is not connected")
        if not pcm_bytes:
            return
        audio_b64 = base64.b64encode(pcm_bytes).decode("ascii")
        self._conversation.append_audio(audio_b64)

    def finish(self, commit: bool = False, timeout: int = 10):
        conv = self._conversation
        self._conversation = None
        if conv is None:
            self.connected = False
            return
        try:
            if commit:
                conv.commit()
            if self.connected:
                conv.end_session(timeout=timeout)
        finally:
            try:
                conv.close()
            finally:
                self.connected = False

    def close(self):
        conv = self._conversation
        self._conversation = None
        if conv is not None:
            try:
                if self.connected:
                    conv.end_session(timeout=5)
            except Exception:
                pass
            try:
                conv.close()
            except Exception:
                pass
        self.connected = False

    def _status(self, message: str):
        if self.on_status:
            self.on_status(message)

    def _error(self, message: str):
        if self.on_error:
            self.on_error(message)
        else:
            self._status(f"error {message}")
