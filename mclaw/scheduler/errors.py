"""Scheduler-specific exceptions."""


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
