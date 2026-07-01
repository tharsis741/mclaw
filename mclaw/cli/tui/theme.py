# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared M-Claw TUI theme tokens."""

from rich import box as rich_box

ACCENT_COLOR = "#4A90D9"
ACCENT_LIGHT = "#6CB4EE"
ACCENT_DIM = "#2C5F8A"
BANNER_TEXT_COLOR = "#E8F0FE"
COMMAND_COLOR = "#7DD3FC"
COMMAND_ARG_COLOR = "#FCD34D"
COMMAND_EXAMPLE_COLOR = "#94A3B8"
VALUE_COLOR = "#E8F0FE"

SUCCESS = "#7ee787"
WARNING = "#fcd34d"
DANGER = "#f87171"
MUTED = "#94a3b8"
INFO = "#7dd3fc"

TUI_BRAND_TITLE = "M-Claw Powered By M-Robots"


def select_box():
    """Return the standard M-Claw border style."""
    return rich_box.ROUNDED
