"""Low-level host OS primitives used outside runtime execution policy."""

from mclaw.system.lock import file_lock

__all__ = ["file_lock"]
