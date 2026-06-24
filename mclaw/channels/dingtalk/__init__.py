# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""DingTalk channel runtime package for M-Claw.

The package adapts DingTalk Stream Mode events into agent turns and exposes
outbound delivery helpers for replies and files.
"""

from mclaw.channels.dingtalk.runtime import DingTalkRuntime

__all__ = ["DingTalkRuntime"]
