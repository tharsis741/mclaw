# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Task-scoped file-read deduplication and loop detection.

Design goals:
  - Deduplicate unchanged reads by path, offset, limit, and mtime.
  - Block repeated identical reads after four consecutive attempts.
  - Reset after context compression because original read output was summarized.
  - Reset after non-read tools so only truly consecutive repeated reads count.
"""

import logging
import os
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

_READ_TRACKER_LOCK = threading.Lock()
_READ_TRACKER: dict[str, dict] = {}


def _get_task_data(task_id: str) -> dict:
    with _READ_TRACKER_LOCK:
        return _READ_TRACKER.setdefault(
            task_id,
            {
                "last_key": None,
                "consecutive": 0,
                "read_history": set(),
                "dedup": {},
                "read_timestamps": {},
            },
        )


def check_dedup(path: str, offset: int, limit: int, task_id: str = "default") -> str | None:
    """Return a reuse hint when an unchanged file region was already read.

    Returns None when the file is new, changed, or not previously read.
    """
    try:
        resolved = str(Path(path).expanduser().resolve())
    except (OSError, ValueError):
        return None

    dedup_key = (resolved, offset, limit)
    task_data = _get_task_data(task_id)

    with _READ_TRACKER_LOCK:
        cached_mtime = task_data.get("dedup", {}).get(dedup_key)

    if cached_mtime is None:
        return None

    try:
        current_mtime = os.path.getmtime(resolved)
        if current_mtime == cached_mtime:
            return (
                "File unchanged since last read. The content from the "
                "earlier read_file result in this conversation is still current — "
                "refer to that instead of re-reading."
            )
    except OSError:
        pass
    return None


def record_read(path: str, offset: int, limit: int, task_id: str = "default") -> tuple[int, bool]:
    """Record a read and return consecutive repeat count plus block decision.

    The count tracks identical consecutive reads. should_block becomes True
    after the fourth identical consecutive read.
    """
    try:
        resolved = str(Path(path).expanduser().resolve())
        current_mtime = os.path.getmtime(resolved)
    except (OSError, ValueError):
        return 1, False

    read_key = ("read", path, offset, limit)
    dedup_key = (resolved, offset, limit)
    task_data = _get_task_data(task_id)

    with _READ_TRACKER_LOCK:
        task_data["read_history"].add((path, offset, limit))
        if "dedup" not in task_data:
            task_data["dedup"] = {}
        if "read_timestamps" not in task_data:
            task_data["read_timestamps"] = {}

        task_data["dedup"][dedup_key] = current_mtime
        task_data["read_timestamps"][resolved] = current_mtime

        if task_data["last_key"] == read_key:
            task_data["consecutive"] += 1
        else:
            task_data["last_key"] = read_key
            task_data["consecutive"] = 1
        count = task_data["consecutive"]

    return count, count >= 4


def notify_other_tool_call(task_id: str = "default") -> None:
    """Reset consecutive read count after a non-read tool executes.

    This keeps warnings and blocking scoped to truly consecutive repeated reads.
    """
    with _READ_TRACKER_LOCK:
        task_data = _READ_TRACKER.get(task_id)
        if task_data:
            task_data["last_key"] = None
            task_data["consecutive"] = 0


def reset_file_dedup(task_id: str | None = None) -> None:
    """Clear read deduplication cache after context compression.

    Original read content has been replaced by summaries, so rereading the same
    file should return complete content again.
    """
    with _READ_TRACKER_LOCK:
        if task_id:
            task_data = _READ_TRACKER.get(task_id)
            if task_data and "dedup" in task_data:
                task_data["dedup"].clear()
        else:
            for task_data in _READ_TRACKER.values():
                if "dedup" in task_data:
                    task_data["dedup"].clear()


def get_read_files_summary(task_id: str = "default") -> list[dict[str, list[str]]]:
    """Return read-file summaries for context retention and diagnostics."""
    with _READ_TRACKER_LOCK:
        task_data = _READ_TRACKER.get(task_id, {})
        read_history = task_data.get("read_history", set())
        seen_paths: dict[str, list[str]] = {}
        for (path, offset, limit) in read_history:
            if path not in seen_paths:
                seen_paths[path] = []
            seen_paths[path].append(f"chars {offset}-{offset + limit - 1}")
        return [
            {"path": p, "regions": regions}
            for p, regions in sorted(seen_paths.items())
        ]


def clear_read_tracker(task_id: str | None = None) -> None:
    """Clear read tracking for one task or for all tasks."""
    with _READ_TRACKER_LOCK:
        if task_id:
            _READ_TRACKER.pop(task_id, None)
        else:
            _READ_TRACKER.clear()
