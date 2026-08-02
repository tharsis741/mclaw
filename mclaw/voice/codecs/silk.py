# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tencent SILK to WAV decoding through the BSD-licensed pysilk binding."""

from __future__ import annotations

import asyncio
import importlib
import io
import os
import tempfile
import threading
import uuid
import wave
from pathlib import Path

from mclaw.voice.audio_probe import AudioInfo
from mclaw.voice.codecs.base import AudioDecodeError, DecodedAudio


SILK_SAMPLE_RATES = frozenset({8000, 12000, 16000, 24000})
_DEFAULT_MAX_OUTPUT_BYTES = 16 * 1024 * 1024


class _BoundedPCMWriter:
    """Stop native decoding before it can grow an unbounded PCM artifact."""

    def __init__(self, stream, *, maximum: int, cancelled: threading.Event) -> None:
        self._stream = stream
        self._maximum = maximum
        self._cancelled = cancelled
        self._written = 0

    def write(self, data: bytes) -> int:
        if self._cancelled.is_set():
            raise AudioDecodeError("SILK decoding was cancelled")
        if self._written + len(data) > self._maximum:
            raise AudioDecodeError("decoded SILK audio exceeds the output limit")
        written = self._stream.write(data)
        self._written += written
        return written

    def __getattr__(self, name: str):
        return getattr(self._stream, name)


class SilkDecoder:
    """Decode Weixin SILK into mono signed 16-bit PCM WAV."""

    def __init__(self, *, output_dir: str | Path | None = None) -> None:
        self.output_dir = Path(output_dir).expanduser() if output_dir else None

    @staticmethod
    def available() -> bool:
        """Return whether the native binding can actually be imported."""

        try:
            importlib.import_module("pysilk")
            return True
        except Exception:
            return False

    async def decode(
        self,
        path: str | Path,
        *,
        sample_rate: int = 24000,
        max_output_bytes: int = _DEFAULT_MAX_OUTPUT_BYTES,
    ) -> DecodedAudio:
        """Decode one Tencent SILK file without blocking the event loop."""

        source = Path(path)
        if not source.is_file():
            raise AudioDecodeError("SILK source file is missing")
        try:
            # Copy the already size-bounded source before starting the native
            # worker. The worker then never holds the channel cache file open,
            # making cancellation cleanup deterministic on Windows.
            source_bytes = source.read_bytes()
        except OSError as exc:
            raise AudioDecodeError(f"SILK source file cannot be read: {exc}") from exc
        cancelled = threading.Event()
        worker = asyncio.create_task(asyncio.to_thread(
            self._decode_sync,
            source_bytes,
            sample_rate,
            max(1, int(max_output_bytes)),
            cancelled,
        ))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            cancelled.set()
            worker.add_done_callback(_discard_cancelled_decode)
            raise

    def _decode_sync(
        self,
        source_bytes: bytes,
        sample_rate: int,
        max_output_bytes: int,
        cancelled: threading.Event,
    ) -> DecodedAudio:
        if not source_bytes:
            raise AudioDecodeError("SILK source file is empty")
        if sample_rate not in SILK_SAMPLE_RATES:
            raise AudioDecodeError(f"unsupported SILK sample rate: {sample_rate}")
        if not self.available():
            raise AudioDecodeError(
                "SILK decoder is unavailable; install the silk-python package"
            )

        try:
            import pysilk
        except Exception as exc:  # pragma: no cover - import loader edge case
            raise AudioDecodeError(f"SILK decoder import failed: {exc}") from exc

        output_dir = self.output_dir or Path(tempfile.gettempdir()) / "mclaw-inbound-audio"
        output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "posix":
            os.chmod(output_dir, 0o700)
        token = uuid.uuid4().hex
        pcm_path = output_dir / f"{token}.pcm"
        wav_path = output_dir / f"{token}.wav"

        try:
            _create_private_empty(pcm_path)
            _create_private_empty(wav_path)
            with io.BytesIO(source_bytes) as silk_stream, pcm_path.open("wb") as raw_pcm_stream:
                pcm_stream = _BoundedPCMWriter(
                    raw_pcm_stream,
                    maximum=max_output_bytes,
                    cancelled=cancelled,
                )
                pysilk.decode(silk_stream, pcm_stream, sample_rate)

            pcm_bytes = pcm_path.stat().st_size
            if pcm_bytes <= 0:
                raise AudioDecodeError("SILK decoder returned empty audio")
            if pcm_bytes > max_output_bytes:
                raise AudioDecodeError("decoded SILK audio exceeds the output limit")

            with pcm_path.open("rb") as pcm_stream, wave.open(str(wav_path), "wb") as wav_stream:
                wav_stream.setnchannels(1)
                wav_stream.setsampwidth(2)
                wav_stream.setframerate(sample_rate)
                while chunk := pcm_stream.read(1024 * 1024):
                    wav_stream.writeframesraw(chunk)

            duration_ms = round(pcm_bytes * 1000 / (sample_rate * 2))
            if cancelled.is_set():
                raise AudioDecodeError("SILK decoding was cancelled")
            return DecodedAudio(
                path=str(wav_path),
                info=AudioInfo(
                    codec="pcm",
                    container="wav",
                    mime_type="audio/wav",
                    extension=".wav",
                    sample_rate=sample_rate,
                    channels=1,
                    duration_ms=duration_ms,
                    duration_verified=True,
                ),
            )
        except AudioDecodeError:
            try:
                wav_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        except Exception as exc:
            try:
                wav_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise AudioDecodeError(f"SILK decoding failed: {exc}") from exc
        finally:
            try:
                pcm_path.unlink(missing_ok=True)
            except OSError:
                pass
            # Best effort: do not leave a world-writable decoded file mode on
            # POSIX systems when the temporary directory has permissive umask.
            if wav_path.exists():
                try:
                    os.chmod(wav_path, 0o600)
                except OSError:
                    pass


def _discard_cancelled_decode(task: asyncio.Task[DecodedAudio]) -> None:
    """Observe a shielded worker and delete output produced after cancellation."""
    try:
        decoded = task.result()
    except (asyncio.CancelledError, Exception):
        return
    try:
        Path(decoded.path).unlink(missing_ok=True)
    except OSError:
        pass


def _create_private_empty(path: Path) -> None:
    """Reserve one decoder artifact with mode 0600 before any data is written."""

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags, 0o600)
    os.close(descriptor)
    if os.name == "posix":
        os.chmod(path, 0o600)
