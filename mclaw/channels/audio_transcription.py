# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Channel-independent inbound voice-message transcription capability."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

from mclaw.channels.base import (
    AttachmentKind,
    AttachmentOrigin,
    ChannelAttachment,
    ChannelMessage,
)
from mclaw.channels.inbound_pipeline import InboundCapabilityError, InboundCapabilityPipeline
from mclaw.voice.audio_probe import AudioProbeError, probe_audio
from mclaw.voice.codecs import AudioDecodeError, SilkDecoder
from mclaw.voice.config import resolve_inbound_audio_config
from mclaw.voice.providers import QwenFileTranscriber
from mclaw.voice.transcription import (
    AudioTranscriber,
    AudioTranscriptionError,
    TranscriptionRequest,
    TranscriptionResult,
)

logger = logging.getLogger(__name__)


class AudioTranscriptionCapability:
    """Turn downloaded voice-message attachments into ordinary user text."""

    name = "audio_transcription"

    def __init__(
        self,
        config: dict[str, Any],
        *,
        transcriber: AudioTranscriber | None = None,
        silk_decoder: SilkDecoder | None = None,
    ) -> None:
        self.config = dict(config)
        self._transcriber = transcriber
        self.silk_decoder = silk_decoder or SilkDecoder()
        self._semaphore = asyncio.Semaphore(max(1, int(self.config.get("max_concurrency") or 1)))
        self._cleanup_tasks: set[asyncio.Task[None]] = set()

    async def process(self, message: ChannelMessage) -> ChannelMessage:
        voice_messages = [
            attachment
            for attachment in message.attachments
            if self._is_voice_message(attachment)
        ]
        selected = [attachment for attachment in message.attachments if self._should_transcribe(attachment)]
        if not selected:
            if voice_messages:
                for attachment in voice_messages:
                    self._cleanup_managed_source(attachment)
                self._raise_failure(
                    "ASR_VOICE_DISABLED",
                    "automatic voice-message transcription is disabled",
                    "语音消息自动识别已关闭，请联系管理员或改用文字输入。",
                )
            return message
        try:
            if not self.config.get("enabled"):
                self._raise_failure(
                    "ASR_NOT_CONFIGURED",
                    str(self.config.get("configuration_error") or (
                        "inbound audio transcription is disabled or has no credential"
                    )),
                    "语音识别尚未配置，请联系管理员或改用文字输入。",
                )

            transcripts: list[tuple[ChannelAttachment, TranscriptionResult]] = []
            failures: list[AudioTranscriptionError] = []
            for attachment in selected:
                try:
                    result = await self._transcribe_attachment(attachment)
                    transcripts.append((attachment, result))
                except AudioTranscriptionError as exc:
                    failures.append(exc)
                    logger.warning(
                        "inbound audio transcription failed channel=%s code=%s",
                        message.source.channel,
                        exc.code,
                    )

            if not transcripts:
                failure = failures[0] if failures else AudioTranscriptionError(
                    "ASR_EMPTY_TRANSCRIPT",
                    "no voice attachment produced a transcript",
                )
                self._raise_failure(failure.code, str(failure), failure.safe_message)

            return self._compose(message, transcripts, failures=failures)
        finally:
            # Channel caches are M-Claw-owned temporary files.  Clean them on
            # every terminal path, including disabled ASR, so a rejected voice
            # message never leaks on disk.
            for attachment in selected:
                self._cleanup_managed_source(attachment)

    def _should_transcribe(self, attachment: ChannelAttachment) -> bool:
        kind = attachment.kind.value if isinstance(attachment.kind, AttachmentKind) else str(attachment.kind)
        if kind not in {AttachmentKind.AUDIO.value, AttachmentKind.VOICE.value}:
            return False
        origin = (
            attachment.origin.value
            if isinstance(attachment.origin, AttachmentOrigin)
            else str(attachment.origin)
        )
        if origin == AttachmentOrigin.VOICE_MESSAGE.value:
            return bool(self.config.get("auto_transcribe_voice_messages", True))
        # File uploads intentionally remain ordinary attachments.  Only the
        # channel-native voice-message affordance is automatic and consented.
        return False

    @staticmethod
    def _is_voice_message(attachment: ChannelAttachment) -> bool:
        kind = attachment.kind.value if isinstance(attachment.kind, AttachmentKind) else str(attachment.kind)
        origin = (
            attachment.origin.value
            if isinstance(attachment.origin, AttachmentOrigin)
            else str(attachment.origin)
        )
        return (
            kind in {AttachmentKind.AUDIO.value, AttachmentKind.VOICE.value}
            and origin == AttachmentOrigin.VOICE_MESSAGE.value
        )

    async def _transcribe_attachment(self, attachment: ChannelAttachment) -> TranscriptionResult:
        if attachment.error:
            raise AudioTranscriptionError(
                "AUDIO_DOWNLOAD_FAILED",
                f"channel media download failed: {attachment.error}",
                safe_message="语音下载失败，请重发或改用文字输入。",
            )
        path = Path(attachment.path)
        if not path.is_file():
            raise AudioTranscriptionError(
                "AUDIO_MISSING",
                "downloaded voice attachment is missing",
                safe_message="语音下载失败，请重发或改用文字输入。",
            )
        size = path.stat().st_size
        max_bytes = int(self.config.get("max_audio_bytes") or 7 * 1024 * 1024)
        if size <= 0:
            raise AudioTranscriptionError("AUDIO_EMPTY", "downloaded voice attachment is empty")
        if size > max_bytes:
            raise AudioTranscriptionError(
                "AUDIO_TOO_LARGE",
                f"voice attachment exceeds limit ({size} > {max_bytes})",
                safe_message="这段语音文件过大，请缩短后重发。",
            )

        metadata = attachment.metadata if isinstance(attachment.metadata, dict) else {}
        sample_rate_hint = _positive_int(metadata.get("sample_rate"))
        duration_ms = attachment.duration_ms or _positive_int(metadata.get("playtime"))
        max_duration_ms = int(self.config.get("max_duration_seconds") or 120) * 1000
        if duration_ms and duration_ms > max_duration_ms:
            raise AudioTranscriptionError(
                "AUDIO_TOO_LONG",
                f"voice attachment exceeds duration limit ({duration_ms} > {max_duration_ms})",
                safe_message="这段语音过长，请缩短后分段发送。",
            )

        try:
            info = probe_audio(
                path,
                mime_hint=attachment.mime_type,
                codec_hint=attachment.codec,
                sample_rate_hint=sample_rate_hint,
                duration_ms_hint=duration_ms or None,
            )
        except AudioProbeError as exc:
            raise AudioTranscriptionError(
                "AUDIO_CODEC_UNSUPPORTED",
                str(exc),
                safe_message="暂不支持这段语音的音频格式，请改用文字输入。",
            ) from exc

        if info.duration_ms and info.duration_ms > max_duration_ms:
            raise AudioTranscriptionError(
                "AUDIO_TOO_LONG",
                f"probed audio exceeds duration limit ({info.duration_ms} > {max_duration_ms})",
                safe_message="这段语音过长，请缩短后分段发送。",
            )
        if info.codec != "silk" and not info.duration_verified:
            raise AudioTranscriptionError(
                "AUDIO_DURATION_UNVERIFIED",
                f"audio duration could not be verified for {info.container}",
                safe_message="无法验证这段语音的实际时长，请缩短后重发或改用文字输入。",
            )

        asr_path = path
        asr_info = info
        decoded_path: Path | None = None
        try:
            if info.codec == "silk":
                sample_rate = info.sample_rate or int(self.config.get("silk_sample_rate") or 24000)
                try:
                    decoded = await asyncio.wait_for(
                        self.silk_decoder.decode(
                            path,
                            sample_rate=sample_rate,
                            max_output_bytes=max(1, max_duration_ms * sample_rate * 2 // 1000),
                        ),
                        timeout=float(self.config.get("decode_timeout_seconds") or 15.0),
                    )
                except asyncio.TimeoutError as exc:
                    raise AudioTranscriptionError(
                        "AUDIO_DECODE_TIMEOUT",
                        "SILK decoding exceeded its time limit",
                        safe_message="微信语音解码超时，请缩短后重发或改用文字输入。",
                    ) from exc
                except AudioDecodeError as exc:
                    raise AudioTranscriptionError(
                        "AUDIO_DECODE_FAILED",
                        str(exc),
                        safe_message="微信语音解码失败，请重发或改用文字输入。",
                    ) from exc
                asr_path = Path(decoded.path)
                asr_info = decoded.info
                decoded_path = asr_path if decoded.temporary else None
                if asr_info.duration_ms and asr_info.duration_ms > max_duration_ms:
                    raise AudioTranscriptionError(
                        "AUDIO_TOO_LONG",
                        "decoded SILK audio exceeds the duration limit",
                        safe_message="这段语音过长，请缩短后分段发送。",
                    )

            if not asr_info.duration_verified:
                raise AudioTranscriptionError(
                    "AUDIO_DURATION_UNVERIFIED",
                    f"decoded audio duration could not be verified for {asr_info.container}",
                    safe_message="无法验证这段语音的实际时长，请缩短后重发或改用文字输入。",
                )

            request = TranscriptionRequest(
                path=str(asr_path),
                mime_type=asr_info.mime_type,
                codec=asr_info.codec,
                language=str(self.config.get("language") or "auto"),
                duration_ms=asr_info.duration_ms,
            )
            result = await self._with_retry(request)
            return result
        finally:
            if decoded_path is not None:
                self._schedule_cleanup(
                    decoded_path,
                    int(self.config.get("retain_decoded_seconds") or 0),
                )

    async def _with_retry(self, request: TranscriptionRequest) -> TranscriptionResult:
        retries = max(0, int(self.config.get("max_retries") or 0))
        backoff = max(0.0, float(self.config.get("retry_backoff_seconds") or 0.0))
        for attempt in range(retries + 1):
            try:
                async with self._semaphore:
                    return await self._get_transcriber().transcribe(request)
            except AudioTranscriptionError as exc:
                if not exc.retryable or attempt >= retries:
                    raise
                await asyncio.sleep(backoff * (2**attempt))
        raise AssertionError("unreachable transcription retry state")

    def _get_transcriber(self) -> AudioTranscriber:
        if self._transcriber is None:
            provider = str(self.config.get("provider") or "qwen").casefold()
            if provider not in {"qwen", "qwen-intl"}:
                raise AudioTranscriptionError(
                    "ASR_PROVIDER_UNAVAILABLE",
                    f"unsupported inbound ASR provider: {provider}",
                )
            self._transcriber = QwenFileTranscriber(
                api_key=str(self.config.get("api_key") or ""),
                base_url=str(self.config.get("base_url") or ""),
                provider=provider,
                model=str(self.config.get("model") or "qwen3-asr-flash"),
                timeout_seconds=float(self.config.get("transcription_timeout_seconds") or 60.0),
                max_audio_bytes=int(self.config.get("max_audio_bytes") or 7 * 1024 * 1024),
                enable_itn=bool(self.config.get("enable_itn", False)),
            )
        return self._transcriber

    def _compose(
        self,
        message: ChannelMessage,
        transcripts: list[tuple[ChannelAttachment, TranscriptionResult]],
        *,
        failures: list[AudioTranscriptionError] | None = None,
    ) -> ChannelMessage:
        ordered = list(transcripts)
        blocks: list[str] = []
        for index, (_attachment, result) in enumerate(ordered, 1):
            if not message.text.strip() and len(ordered) == 1:
                blocks.append(result.text.strip())
            else:
                label = "[语音转写]" if len(ordered) == 1 else f"[语音转写 {index}]"
                blocks.append(f"{label}\n{result.text.strip()}")
        final_text = "\n\n".join(part for part in [message.text.strip(), *blocks] if part).strip()

        capability_results = dict(message.capability_results)
        capability_results[self.name] = [
            {
                "status": "ok",
                "provider": result.provider,
                "model": result.model,
                "language": result.language,
                "duration_ms": result.duration_ms,
                "source_hash": result.source_hash,
                "warnings": list(result.warnings),
            }
            for _attachment, result in ordered
        ]
        if failures:
            capability_results[f"{self.name}_failures"] = [failure.code for failure in failures]
        return replace(message, text=final_text, capability_results=capability_results)

    def _schedule_cleanup(self, path: Path, delay_seconds: int) -> None:
        if delay_seconds <= 0:
            _safe_unlink(path)
            return
        task = asyncio.create_task(_unlink_after(path, delay_seconds))
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)

    def _cleanup_managed_source(self, attachment: ChannelAttachment) -> None:
        metadata = attachment.metadata if isinstance(attachment.metadata, dict) else {}
        if metadata.get("managed_cache") is not True or not attachment.path:
            return
        self._schedule_cleanup(
            Path(attachment.path),
            int(self.config.get("retain_source_seconds") or 0),
        )

    def _raise_failure(self, code: str, detail: str, safe_message: str) -> None:
        raise InboundCapabilityError(
            self.name,
            code=code,
            detail=detail,
            safe_message=safe_message,
        )


def build_inbound_pipeline(config: dict[str, Any]) -> InboundCapabilityPipeline:
    """Build the default channel pipeline, including disabled-state handling."""

    resolved = resolve_inbound_audio_config(config=config)
    return InboundCapabilityPipeline([AudioTranscriptionCapability(resolved)])


def _positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.debug("temporary inbound audio cleanup failed path=%s", path.name)


async def _unlink_after(path: Path, delay_seconds: int) -> None:
    try:
        await asyncio.sleep(delay_seconds)
        _safe_unlink(path)
    except asyncio.CancelledError:
        raise
