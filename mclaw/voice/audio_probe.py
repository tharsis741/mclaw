# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Small, dependency-free audio format probing for inbound channel media."""

from __future__ import annotations

import wave
from dataclasses import dataclass
from pathlib import Path


class AudioProbeError(RuntimeError):
    """Raised when a downloaded attachment cannot be identified as audio."""


@dataclass(frozen=True)
class AudioInfo:
    """Verified audio properties used by decoders and ASR providers."""

    codec: str
    container: str
    mime_type: str
    extension: str
    sample_rate: int | None = None
    channels: int | None = None
    duration_ms: int | None = None
    duration_verified: bool = False


_EXTENSION_INFO: dict[str, tuple[str, str, str]] = {
    ".aac": ("aac", "aac", "audio/aac"),
    ".aif": ("pcm", "aiff", "audio/aiff"),
    ".aiff": ("pcm", "aiff", "audio/aiff"),
    ".amr": ("amr", "amr", "audio/amr"),
    ".flac": ("flac", "flac", "audio/flac"),
    ".m4a": ("aac", "mp4", "audio/mp4"),
    ".mp3": ("mp3", "mp3", "audio/mpeg"),
    ".mp4": ("aac", "mp4", "audio/mp4"),
    ".oga": ("ogg", "ogg", "audio/ogg"),
    ".ogg": ("ogg", "ogg", "audio/ogg"),
    ".opus": ("opus", "ogg", "audio/opus"),
    ".silk": ("silk", "silk", "audio/silk"),
    ".wav": ("pcm", "wav", "audio/wav"),
    ".webm": ("opus", "webm", "audio/webm"),
    ".wma": ("wma", "asf", "audio/x-ms-wma"),
}


def _wav_info(path: Path, *, duration_ms_hint: int | None) -> AudioInfo:
    try:
        with wave.open(str(path), "rb") as wav:
            sample_rate = wav.getframerate() or None
            channels = wav.getnchannels() or None
            frames = wav.getnframes()
    except (wave.Error, OSError) as exc:
        raise AudioProbeError(f"invalid WAV audio: {exc}") from exc
    duration_ms = round(frames * 1000 / sample_rate) if sample_rate else None
    return AudioInfo(
        codec="pcm",
        container="wav",
        mime_type="audio/wav",
        extension=".wav",
        sample_rate=sample_rate,
        channels=channels,
        duration_ms=duration_ms,
        duration_verified=duration_ms is not None,
    )


def _amr_duration_ms(path: Path, *, wideband: bool) -> int:
    """Count fixed-duration AMR frames, rejecting truncated/malformed input."""

    header = b"#!AMR-WB\n" if wideband else b"#!AMR\n"
    frame_sizes = (
        (18, 24, 33, 37, 41, 47, 51, 59, 61, 6)
        if wideband
        else (13, 14, 16, 18, 20, 21, 27, 32, 6)
    )
    frames = 0
    with path.open("rb") as stream:
        if stream.read(len(header)) != header:
            raise AudioProbeError("invalid AMR header")
        while toc := stream.read(1):
            frame_type = (toc[0] >> 3) & 0x0F
            if frame_type >= len(frame_sizes):
                raise AudioProbeError("unsupported AMR frame type")
            remaining = frame_sizes[frame_type] - 1
            if len(stream.read(remaining)) != remaining:
                raise AudioProbeError("truncated AMR frame")
            frames += 1
    if not frames:
        raise AudioProbeError("AMR audio contains no frames")
    return frames * 20


def _ogg_duration_ms(path: Path, *, opus: bool) -> int:
    """Derive Ogg duration from per-stream granule positions."""

    granules: dict[int, int] = {}
    sample_rates: dict[int, int] = {}
    with path.open("rb") as stream:
        while header := stream.read(27):
            if len(header) != 27 or header[:4] != b"OggS":
                raise AudioProbeError("invalid Ogg page header")
            segment_table = stream.read(header[26])
            if len(segment_table) != header[26]:
                raise AudioProbeError("truncated Ogg segment table")
            body_size = sum(segment_table)
            body = stream.read(body_size)
            if len(body) != body_size:
                raise AudioProbeError("truncated Ogg page")
            serial = int.from_bytes(header[14:18], "little")
            granule = int.from_bytes(header[6:14], "little")
            if granule != 0xFFFFFFFFFFFFFFFF:
                granules[serial] = max(granules.get(serial, 0), granule)
            if opus or b"OpusHead" in body:
                sample_rates[serial] = 48000
            else:
                marker = body.find(b"\x01vorbis")
                if marker >= 0 and len(body) >= marker + 16:
                    rate = int.from_bytes(body[marker + 12:marker + 16], "little")
                    if rate > 0:
                        sample_rates[serial] = rate
    durations = [
        granule * 1000 / sample_rates[serial]
        for serial, granule in granules.items()
        if granule > 0 and sample_rates.get(serial, 0) > 0
    ]
    if not durations:
        raise AudioProbeError("Ogg duration could not be verified")
    return round(sum(durations))


def _flac_duration_ms(path: Path) -> tuple[int, int]:
    """Read sample rate and total samples from the mandatory STREAMINFO block."""

    with path.open("rb") as stream:
        if stream.read(4) != b"fLaC":
            raise AudioProbeError("invalid FLAC header")
        block_header = stream.read(4)
        if len(block_header) != 4 or block_header[0] & 0x7F != 0:
            raise AudioProbeError("FLAC STREAMINFO block is missing")
        block_length = int.from_bytes(block_header[1:4], "big")
        stream_info = stream.read(block_length)
    if len(stream_info) < 18:
        raise AudioProbeError("truncated FLAC STREAMINFO block")
    packed = int.from_bytes(stream_info[10:18], "big")
    sample_rate = (packed >> 44) & 0xFFFFF
    total_samples = packed & 0xFFFFFFFFF
    if sample_rate <= 0 or total_samples <= 0:
        raise AudioProbeError("FLAC duration could not be verified")
    return round(total_samples * 1000 / sample_rate), sample_rate


def _aac_adts_duration_ms(path: Path) -> tuple[int, int]:
    """Walk ADTS frames and derive duration from their sample counts."""

    rates = (96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050, 16000, 12000, 11025, 8000, 7350)
    total_samples = 0
    sample_rate = 0
    with path.open("rb") as stream:
        while header := stream.read(7):
            if len(header) != 7 or header[0] != 0xFF or header[1] & 0xF6 != 0xF0:
                raise AudioProbeError("invalid AAC ADTS frame")
            rate_index = (header[2] >> 2) & 0x0F
            if rate_index >= len(rates):
                raise AudioProbeError("unsupported AAC sample rate")
            current_rate = rates[rate_index]
            if sample_rate and sample_rate != current_rate:
                raise AudioProbeError("AAC sample rate changed between frames")
            sample_rate = current_rate
            frame_length = ((header[3] & 0x03) << 11) | (header[4] << 3) | (header[5] >> 5)
            if frame_length < 7:
                raise AudioProbeError("invalid AAC frame length")
            if len(stream.read(frame_length - 7)) != frame_length - 7:
                raise AudioProbeError("truncated AAC frame")
            total_samples += 1024 * ((header[6] & 0x03) + 1)
    if not total_samples or not sample_rate:
        raise AudioProbeError("AAC audio contains no frames")
    return round(total_samples * 1000 / sample_rate), sample_rate


def _mp3_duration_ms(path: Path) -> tuple[int, int]:
    """Walk MPEG Layer III frames, including variable-bitrate files."""

    data = path.read_bytes()
    offset = 0
    if data.startswith(b"ID3"):
        if len(data) < 10:
            raise AudioProbeError("truncated MP3 ID3 header")
        tag_size = sum((data[6 + index] & 0x7F) << (21 - index * 7) for index in range(4))
        offset = 10 + tag_size
    bitrate_mpeg1 = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)
    bitrate_mpeg2 = (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160)
    base_rates = (44100, 48000, 32000)
    total_seconds = 0.0
    frames = 0
    first_rate = 0
    while offset + 4 <= len(data):
        header = int.from_bytes(data[offset:offset + 4], "big")
        if header >> 21 != 0x7FF:
            if frames and (data[offset:offset + 3] == b"TAG" or not data[offset:].strip(b"\0")):
                break
            raise AudioProbeError("invalid MP3 frame sync")
        version = (header >> 19) & 0x03
        layer = (header >> 17) & 0x03
        bitrate_index = (header >> 12) & 0x0F
        rate_index = (header >> 10) & 0x03
        padding = (header >> 9) & 0x01
        if version == 1 or layer != 1 or bitrate_index in {0, 15} or rate_index == 3:
            raise AudioProbeError("unsupported MP3 frame header")
        divisor = 1 if version == 3 else 2 if version == 2 else 4
        sample_rate = base_rates[rate_index] // divisor
        bitrate = (bitrate_mpeg1 if version == 3 else bitrate_mpeg2)[bitrate_index]
        samples = 1152 if version == 3 else 576
        frame_length = (144000 if version == 3 else 72000) * bitrate // sample_rate + padding
        if frame_length < 4 or offset + frame_length > len(data):
            raise AudioProbeError("truncated MP3 frame")
        first_rate = first_rate or sample_rate
        total_seconds += samples / sample_rate
        frames += 1
        offset += frame_length
    if not frames:
        raise AudioProbeError("MP3 audio contains no frames")
    return round(total_seconds * 1000), first_rate


def probe_audio(
    path: str | Path,
    *,
    mime_hint: str = "",
    codec_hint: str = "",
    sample_rate_hint: int | None = None,
    duration_ms_hint: int | None = None,
) -> AudioInfo:
    """Identify common chat-audio formats using content before extensions.

    Platform callbacks frequently omit a filename or report a generic MIME
    type.  Magic bytes therefore take precedence over every supplied hint.
    """

    audio_path = Path(path)
    if not audio_path.is_file():
        raise AudioProbeError("audio attachment is missing")
    try:
        with audio_path.open("rb") as stream:
            header = stream.read(4096)
    except OSError as exc:
        raise AudioProbeError(f"audio attachment cannot be read: {exc}") from exc
    if not header:
        raise AudioProbeError("audio attachment is empty")

    if header.startswith(b"RIFF") and header[8:12] == b"WAVE":
        return _wav_info(audio_path, duration_ms_hint=duration_ms_hint)
    if header.startswith(b"#!AMR-WB\n"):
        duration_ms = _amr_duration_ms(audio_path, wideband=True)
        return AudioInfo("amr-wb", "amr", "audio/amr-wb", ".amr", 16000, 1, duration_ms, True)
    if header.startswith(b"#!AMR\n"):
        duration_ms = _amr_duration_ms(audio_path, wideband=False)
        return AudioInfo("amr-nb", "amr", "audio/amr", ".amr", 8000, 1, duration_ms, True)
    if header.startswith(b"OggS"):
        is_opus = b"OpusHead" in header
        duration_ms = _ogg_duration_ms(audio_path, opus=is_opus)
        return AudioInfo(
            "opus" if is_opus else "ogg",
            "ogg",
            "audio/opus" if is_opus else "audio/ogg",
            ".opus" if is_opus else ".ogg",
            sample_rate_hint,
            None,
            duration_ms,
            True,
        )
    if header.startswith(b"fLaC"):
        duration_ms, sample_rate = _flac_duration_ms(audio_path)
        return AudioInfo("flac", "flac", "audio/flac", ".flac", sample_rate, None, duration_ms, True)
    if header.startswith(b"ID3") or (len(header) >= 2 and header[0] == 0xFF and header[1] & 0xE0 == 0xE0):
        # ADTS AAC also starts with an FF sync word. Prefer an explicit AAC
        # hint; otherwise MP3 is the safer chat-audio default.
        hinted_aac = codec_hint.casefold() == "aac" or mime_hint.casefold() == "audio/aac"
        if hinted_aac:
            duration_ms, sample_rate = _aac_adts_duration_ms(audio_path)
            return AudioInfo("aac", "aac", "audio/aac", ".aac", sample_rate, None, duration_ms, True)
        duration_ms, sample_rate = _mp3_duration_ms(audio_path)
        return AudioInfo("mp3", "mp3", "audio/mpeg", ".mp3", sample_rate, None, duration_ms, True)
    if len(header) >= 12 and header[4:8] == b"ftyp":
        return AudioInfo("aac", "mp4", "audio/mp4", ".m4a", sample_rate_hint, None, duration_ms_hint)
    if header.startswith(b"\x1aE\xdf\xa3"):
        return AudioInfo("opus", "webm", "audio/webm", ".webm", sample_rate_hint, None, duration_ms_hint)
    if header.startswith(b"0&\xb2u\x8ef\xcf\x11"):
        return AudioInfo("wma", "asf", "audio/x-ms-wma", ".wma", sample_rate_hint, None, duration_ms_hint)
    if header.startswith(b"#!SILK_V3") or header.startswith(b"\x02#!SILK_V3"):
        return AudioInfo("silk", "silk", "audio/silk", ".silk", sample_rate_hint or 24000, 1, duration_ms_hint)

    suffix_info = _EXTENSION_INFO.get(audio_path.suffix.casefold())
    if suffix_info:
        codec, container, mime_type = suffix_info
        return AudioInfo(
            codec_hint.casefold() or codec,
            container,
            mime_hint if mime_hint.startswith("audio/") else mime_type,
            audio_path.suffix.casefold(),
            sample_rate_hint,
            None,
            duration_ms_hint,
        )

    raise AudioProbeError("unsupported or unrecognized audio format")
