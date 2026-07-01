# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Scheduler-specific exceptions."""


class SchedulerError(Exception):
    """Base class for scheduler failures."""


class ScheduleParseError(SchedulerError, ValueError):
    """Raised when a schedule expression cannot be parsed."""
