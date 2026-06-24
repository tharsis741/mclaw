# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Low-level host OS primitives used outside runtime execution policy."""

from mclaw.system.lock import file_lock

__all__ = ["file_lock"]
