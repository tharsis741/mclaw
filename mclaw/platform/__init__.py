# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Platform facts for prompts, doctor diagnostics, and optional providers."""

from mclaw.platform.detect import PlatformInfo, audio_input_available, get_platform_info, gui_available, is_wsl

__all__ = [
    "PlatformInfo",
    "audio_input_available",
    "get_platform_info",
    "gui_available",
    "is_wsl",
]
