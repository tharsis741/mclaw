# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Deduplicate inbound DingTalk messages before agent dispatch.

The cache prevents repeated platform deliveries from starting duplicate agent
turns while keeping retention and identity logic local to the channel layer.
"""

from mclaw.channels.weixin.dedup import MessageDeduplicator

__all__ = ["MessageDeduplicator"]
