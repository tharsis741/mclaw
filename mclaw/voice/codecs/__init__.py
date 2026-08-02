# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Optional audio codec adapters used by inbound channel transcription."""

from mclaw.voice.codecs.base import AudioDecodeError, DecodedAudio
from mclaw.voice.codecs.silk import SILK_SAMPLE_RATES, SilkDecoder

__all__ = ["AudioDecodeError", "DecodedAudio", "SILK_SAMPLE_RATES", "SilkDecoder"]
