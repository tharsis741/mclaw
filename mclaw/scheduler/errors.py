# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Scheduler-specific exceptions for validation and execution failures."""


class SchedulerError(Exception):
    """Base class for scheduler failures."""


class ScheduleParseError(SchedulerError, ValueError):
    """Raised when a schedule expression cannot be parsed."""


class SchedulerStoreError(SchedulerError):
    """Raised for scheduler persistence errors."""


class SchedulerTargetError(SchedulerError):
    """Raised when a delivery target cannot be used."""


class SchedulerRunError(SchedulerError):
    """Raised when a scheduler run fails before agent execution completes."""
