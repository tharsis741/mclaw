# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared decoder result types."""

from __future__ import annotations

from dataclasses import dataclass

from mclaw.voice.audio_probe import AudioInfo


class AudioDecodeError(RuntimeError):
    """Raised when a supported source codec cannot be decoded safely."""


@dataclass(frozen=True)
class DecodedAudio:
    """A temporary normalized audio artifact and its verified properties."""

    path: str
    info: AudioInfo
    temporary: bool = True
