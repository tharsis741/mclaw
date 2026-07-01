# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Qwen realtime ASR backend."""

from __future__ import annotations

import base64
import os
import socket
from typing import Any


_ORIGINAL_GETADDRINFO = None
_FORCED_IPV4_GETADDRINFO = False


def _truthy(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "on", "y"}:
            return True
        if text in {"0", "false", "no", "off", "n"}:
            return False
        if text == "auto":
            return default
    return default


def _is_kaihong_runtime() -> bool:
    try:
        from mclaw.runtime.manager import RuntimeManager

        return RuntimeManager.detect() == "kaihong"
    except Exception:
        return False


def _configure_ca_bundle(config: dict) -> None:
    """Install a CA bundle path for SDKs that honor common SSL env vars."""
    bundle = str(config.get("ca_bundle") or "").strip()
    if bundle.lower() == "auto":
        bundle = ""
    if not bundle:
        try:
            import certifi

            bundle = certifi.where()
        except Exception:
            bundle = ""
    if not bundle or not os.path.exists(bundle):
        return
    os.environ.setdefault("SSL_CERT_FILE", bundle)
    os.environ.setdefault("REQUESTS_CA_BUNDLE", bundle)


def _ensure_dashscope_address_family(config: dict) -> None:
    """Force DashScope DNS resolution to IPv4 on runtimes with weak IPv6 paths."""
    global _FORCED_IPV4_GETADDRINFO, _ORIGINAL_GETADDRINFO

    force_ipv4 = _truthy(config.get("force_ipv4"), default=_is_kaihong_runtime())
    if not force_ipv4 or _FORCED_IPV4_GETADDRINFO:
        return

    _ORIGINAL_GETADDRINFO = socket.getaddrinfo

    def getaddrinfo_ipv4(host, port, family=0, type=0, proto=0, flags=0):  # noqa: A002 - socket API name
        """Mirror socket.getaddrinfo while constraining the address family."""
        infos = _ORIGINAL_GETADDRINFO(host, port, socket.AF_INET, type, proto, flags)
        ipv4_infos = [info for info in infos if info[0] == socket.AF_INET]
        return ipv4_infos or infos

    socket.getaddrinfo = getaddrinfo_ipv4
    _FORCED_IPV4_GETADDRINFO = True


class QwenRealtimeASRBackend:
    """Adapter from raw PCM chunks to DashScope/Qwen realtime transcription events."""

    def __init__(self, config: dict, on_transcript, on_error=None, on_status=None):
        self.config = config or {}
        self.on_transcript = on_transcript
        self.on_error = on_error
        self.on_status = on_status
        self.connected = False
        self._conversation = None

    def connect(self):
        """Open the websocket session and configure server-side VAD/transcription."""
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
                "Install or upgrade M-Claw dependencies from the source directory: pip install -e ."
            ) from exc

        api_key = self.config.get("api_key") or ""
        if not api_key:
            raise RuntimeError("DashScope/Qwen API key is not configured.")
        dashscope.api_key = api_key
        _configure_ca_bundle(self.config)
        _ensure_dashscope_address_family(self.config)

        backend = self

        class Callback(OmniRealtimeCallback):
            """Forward SDK callbacks into the service-level callback contract."""

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
        """Append one PCM chunk after converting to the SDK's base64 payload."""
        if not self.connected:
            raise RuntimeError("Qwen ASR backend is not connected")
        if not pcm_bytes:
            return
        audio_b64 = base64.b64encode(pcm_bytes).decode("ascii")
        self._conversation.append_audio(audio_b64)

    def finish(self, commit: bool = False, timeout: int = 10):
        """Optionally commit buffered audio before closing a manual recording turn."""
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
        """Best-effort shutdown used for cancellation and service teardown paths."""
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
