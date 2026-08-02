# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Provider-neutral contracts for transcribing complete inbound audio files."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlparse


class AudioTranscriptionError(RuntimeError):
    """A classified transcription failure with a user-safe fallback message."""

    def __init__(
        self,
        code: str,
        detail: str,
        *,
        safe_message: str = "语音识别失败，请重发或改用文字输入。",
        retryable: bool = False,
    ) -> None:
        super().__init__(detail)
        self.code = code
        self.safe_message = safe_message
        self.retryable = retryable


@dataclass(frozen=True)
class TranscriptionRequest:
    """One local audio artifact submitted to an ASR provider."""

    path: str
    mime_type: str
    codec: str
    language: str = "auto"
    duration_ms: int | None = None


@dataclass(frozen=True)
class TranscriptionResult:
    """Normalized ASR output without provider response objects or secrets."""

    text: str
    provider: str
    model: str
    language: str = ""
    duration_ms: int | None = None
    confidence: float | None = None
    source_hash: str = ""
    warnings: tuple[str, ...] = ()


class AudioTranscriber(Protocol):
    """Interface implemented by remote and local complete-file ASR providers."""

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        ...


def validate_audio_service_url(value: str) -> str:
    """Return a normalized ASR endpoint, rejecting credential-leaking URLs.

    Remote services must use TLS.  Plain HTTP is accepted only for an explicit
    loopback endpoint so local development proxies remain possible without
    allowing an API key or audio payload to cross the network in clear text.
    """

    normalized = str(value or "").strip().rstrip("/")
    parsed = urlparse(normalized)
    if not normalized or not parsed.scheme or not parsed.hostname:
        raise ValueError("ASR base URL must be an absolute URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("ASR base URL must not contain user credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("ASR base URL must not contain a query or fragment")

    scheme = parsed.scheme.casefold()
    if scheme == "https":
        return normalized
    if scheme != "http":
        raise ValueError("ASR base URL must use HTTPS")

    hostname = parsed.hostname.casefold()
    is_loopback = hostname == "localhost"
    if not is_loopback:
        try:
            is_loopback = ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            is_loopback = False
    if not is_loopback:
        raise ValueError("plain HTTP ASR endpoints are allowed only on loopback")
    return normalized
