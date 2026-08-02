# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Qwen3-ASR-Flash complete-file transcription over the OpenAI-compatible API."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
from pathlib import Path
from typing import Any, Callable

from mclaw.voice.transcription import (
    AudioTranscriptionError,
    TranscriptionRequest,
    TranscriptionResult,
    validate_audio_service_url,
)

_QWEN_DATA_URI_LIMIT_BYTES = 10 * 1024 * 1024
_QWEN3_SUPPORTED_AUDIO_MIME_TYPES = {
    "audio/aac",
    "audio/aiff",
    "audio/amr",
    "audio/amr-wb",
    "audio/flac",
    "audio/mpeg",
    "audio/ogg",
    "audio/opus",
    "audio/wav",
    "audio/webm",
    "audio/x-aiff",
    "audio/x-ms-wma",
}


class QwenFileTranscriber:
    """Submit bounded local audio as a Base64 data URI to Qwen ASR."""

    provider = "qwen"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        provider: str = "qwen",
        model: str = "qwen3-asr-flash",
        timeout_seconds: float = 60.0,
        max_audio_bytes: int = 7 * 1024 * 1024,
        enable_itn: bool = False,
        client_factory: Callable[..., Any] | None = None,
    ) -> None:
        provider_name = str(provider or "qwen").strip().casefold()
        if provider_name not in {"qwen", "qwen-intl"}:
            raise ValueError(f"unsupported Qwen ASR provider: {provider_name}")
        self.provider = provider_name
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_audio_bytes = max_audio_bytes
        self.enable_itn = enable_itn
        self._client_factory = client_factory
        self._client: Any = None

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        """Transcribe a local file with a cancellation-aware async request."""

        try:
            return await asyncio.wait_for(
                self._transcribe_async(request),
                timeout=self.timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise AudioTranscriptionError(
                "ASR_TIMEOUT",
                f"Qwen ASR exceeded {self.timeout_seconds:.1f}s",
                safe_message="语音识别超时，请稍后重试或改用文字输入。",
                retryable=True,
            ) from exc

    def _client_instance(self) -> Any:
        if self._client is not None:
            return self._client
        if not self.api_key:
            raise AudioTranscriptionError(
                "ASR_NOT_CONFIGURED",
                "Qwen ASR API key is missing",
                safe_message="语音识别尚未配置，请联系管理员或改用文字输入。",
            )
        if not self.base_url:
            raise AudioTranscriptionError(
                "ASR_NOT_CONFIGURED",
                "Qwen ASR base URL is missing",
                safe_message="语音识别尚未配置，请联系管理员或改用文字输入。",
            )
        try:
            self.base_url = validate_audio_service_url(self.base_url)
        except ValueError as exc:
            raise AudioTranscriptionError(
                "ASR_NOT_CONFIGURED",
                f"unsafe Qwen ASR base URL: {exc}",
                safe_message="语音识别服务地址配置不安全，请联系管理员或改用文字输入。",
            ) from exc
        factory = self._client_factory
        if factory is None:
            try:
                from openai import AsyncOpenAI
            except Exception as exc:  # pragma: no cover - required project dependency
                raise AudioTranscriptionError(
                    "ASR_PROVIDER_UNAVAILABLE",
                    f"OpenAI-compatible client import failed: {exc}",
                ) from exc
            factory = AsyncOpenAI
        self._client = factory(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.timeout_seconds,
            # Retry policy lives in AudioTranscriptionCapability so request
            # counts, backoff and concurrency stay observable and bounded.
            max_retries=0,
        )
        return self._client

    def _prepare_request(self, request: TranscriptionRequest) -> tuple[str, str, dict[str, Any]]:
        """Read and encode the bounded local file without blocking the event loop."""

        mime_type = str(request.mime_type or "").partition(";")[0].strip().casefold()
        if self.model.casefold().startswith("qwen3-asr-flash") and (
            mime_type not in _QWEN3_SUPPORTED_AUDIO_MIME_TYPES
        ):
            raise AudioTranscriptionError(
                "AUDIO_CODEC_UNSUPPORTED",
                f"Qwen3 ASR does not support input MIME type {mime_type or '<empty>'}",
                safe_message="暂不支持这段语音的音频格式，请改用文字输入。",
            )
        path = Path(request.path)
        if not path.is_file():
            raise AudioTranscriptionError("AUDIO_MISSING", "ASR input file is missing")
        size = path.stat().st_size
        if size <= 0:
            raise AudioTranscriptionError("AUDIO_EMPTY", "ASR input file is empty")
        if size > self.max_audio_bytes:
            raise AudioTranscriptionError(
                "AUDIO_TOO_LARGE",
                f"ASR input exceeds limit ({size} > {self.max_audio_bytes})",
                safe_message="这段语音文件过大，请缩短后重发。",
            )

        data = path.read_bytes()
        source_hash = f"sha256:{hashlib.sha256(data).hexdigest()}"
        encoded = base64.b64encode(data)
        # AMR-WB is part of the documented AMR family; use the conventional
        # AMR media type in the data URI while content magic remains decisive.
        data_uri_mime = "audio/amr" if mime_type == "audio/amr-wb" else mime_type
        data_uri_prefix = f"data:{data_uri_mime};base64,".encode("ascii")
        if len(data_uri_prefix) + len(encoded) > _QWEN_DATA_URI_LIMIT_BYTES:
            raise AudioTranscriptionError(
                "AUDIO_TOO_LARGE",
                "Base64 audio exceeds Qwen's 10 MiB data-URI input limit",
                safe_message="这段语音文件过大，请缩短后重发。",
            )
        data_uri = (data_uri_prefix + encoded).decode("ascii")
        asr_options: dict[str, Any] = {"enable_itn": self.enable_itn}
        if request.language and request.language.casefold() != "auto":
            asr_options["language"] = request.language
        return data_uri, source_hash, asr_options

    async def _transcribe_async(self, request: TranscriptionRequest) -> TranscriptionResult:
        # Preparation is bounded to seven MiB. Keep it synchronous so task
        # cancellation cannot leave a background reader holding the managed
        # source file open while Windows cleanup runs.
        data_uri, source_hash, asr_options = self._prepare_request(request)

        try:
            outcome = self._client_instance().chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_audio",
                                "input_audio": {"data": data_uri},
                            }
                        ],
                    }
                ],
                stream=False,
                extra_body={"asr_options": asr_options},
            )
            completion = await outcome if inspect.isawaitable(outcome) else outcome
        except asyncio.CancelledError:
            # AsyncOpenAI/httpx observes cancellation and closes the in-flight
            # request, preventing timeout retries from overlapping paid calls.
            raise
        except AudioTranscriptionError:
            raise
        except Exception as exc:
            status_code = getattr(exc, "status_code", None)
            sdk_connection_error = False
            sdk_timeout_error = False
            try:
                from openai import APIConnectionError, APITimeoutError

                sdk_connection_error = isinstance(exc, APIConnectionError)
                sdk_timeout_error = isinstance(exc, APITimeoutError)
            except Exception:  # pragma: no cover - OpenAI is a project dependency
                pass
            retryable = sdk_connection_error or status_code in {408, 409, 429} or (
                isinstance(status_code, int) and status_code >= 500
            )
            if sdk_timeout_error or status_code == 408:
                code = "ASR_TIMEOUT"
            elif status_code == 429:
                code = "ASR_RATE_LIMITED"
            else:
                code = "ASR_PROVIDER_ERROR"
            safe = (
                "语音识别超时，请稍后重试或改用文字输入。"
                if code == "ASR_TIMEOUT"
                else "语音识别服务繁忙，请稍后重试。"
                if retryable
                else "语音识别失败，请重发或改用文字输入。"
            )
            raise AudioTranscriptionError(
                code,
                f"Qwen ASR request failed ({type(exc).__name__}): {exc}",
                safe_message=safe,
                retryable=retryable,
            ) from exc

        return self._normalize_completion(
            completion,
            request=request,
            source_hash=source_hash,
        )

    def _normalize_completion(
        self,
        completion: Any,
        *,
        request: TranscriptionRequest,
        source_hash: str,
    ) -> TranscriptionResult:
        """Normalize the OpenAI-compatible response into the provider contract."""

        choices = getattr(completion, "choices", None) or []
        if not choices:
            raise AudioTranscriptionError("ASR_EMPTY_TRANSCRIPT", "Qwen ASR returned no choices")
        message = getattr(choices[0], "message", None)
        text = str(getattr(message, "content", "") or "").strip()
        if not text:
            raise AudioTranscriptionError(
                "ASR_EMPTY_TRANSCRIPT",
                "Qwen ASR returned an empty transcript",
                safe_message="没有识别到清晰语音，请重新说一次。",
            )

        language = ""
        annotations = getattr(message, "annotations", None)
        if not annotations:
            model_extra = getattr(message, "model_extra", None)
            if isinstance(model_extra, dict):
                annotations = model_extra.get("annotations")
        for annotation in annotations or []:
            if isinstance(annotation, dict):
                language = str(annotation.get("language") or "")
            else:
                language = str(getattr(annotation, "language", "") or "")
            if language:
                break

        duration_ms = request.duration_ms
        usage = getattr(completion, "usage", None)
        seconds = getattr(usage, "seconds", None) if usage is not None else None
        if seconds is None and usage is not None:
            model_extra = getattr(usage, "model_extra", None)
            if isinstance(model_extra, dict):
                seconds = model_extra.get("seconds")
        if duration_ms is None and isinstance(seconds, (int, float)):
            duration_ms = round(float(seconds) * 1000)

        return TranscriptionResult(
            text=text,
            provider=self.provider,
            model=self.model,
            language=language,
            duration_ms=duration_ms,
            source_hash=source_hash,
        )
