"""按任务隔离的文件读取去重与循环检测。

设计目标：
  - 去重：文件未变化时，按 path/offset/limit/mtime 跳过重复读取。
  - 连续读取保护：同一读取连续重复 4 次后阻止，避免模型陷入读取循环。
  - 压缩后重置：上下文压缩会移除原始读取内容，需要允许再次读取。
  - 非读取工具后重置：只统计真正连续的重复读取。
"""

import logging
import os
import threading
from pathlib import Path
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

_READ_TRACKER_LOCK = threading.Lock()
_READ_TRACKER: Dict[str, dict] = {}


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


def check_dedup(path: str, offset: int, limit: int, task_id: str = "default") -> Optional[str]:
    """如果同一区间已读取且文件未变化，返回提示模型复用已有内容的消息。

    文件新增、变化或从未读取时返回 None。
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


def record_read(path: str, offset: int, limit: int, task_id: str = "default") -> Tuple[int, bool]:
    """记录一次读取并返回连续重复次数和是否需要阻止。

    consecutive_count 表示同一读取连续重复次数。
    should_block 在连续重复次数达到 4 次时为 True。
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


def notify_other_tool_call(task_id: str = "default"):
    """非读取工具执行后重置连续读取计数。

    这样只会对真正连续的重复读取告警或阻止。
    """
    with _READ_TRACKER_LOCK:
        task_data = _READ_TRACKER.get(task_id)
        if task_data:
            task_data["last_key"] = None
            task_data["consecutive"] = 0


def reset_file_dedup(task_id: str = None):
    """上下文压缩后清空读取去重缓存。

    原始读取内容已经被摘要替换，模型再次读取同一文件时需要拿到完整内容。
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


def get_read_files_summary(task_id: str = "default") -> list:
    """返回本任务已读取文件摘要，用于上下文保留和诊断。"""
    with _READ_TRACKER_LOCK:
        task_data = _READ_TRACKER.get(task_id, {})
        read_history = task_data.get("read_history", set())
        seen_paths: dict = {}
        for (path, offset, limit) in read_history:
            if path not in seen_paths:
                seen_paths[path] = []
            seen_paths[path].append(f"lines {offset}-{offset + limit - 1}")
        return [
            {"path": p, "regions": regions}
            for p, regions in sorted(seen_paths.items())
        ]


def clear_read_tracker(task_id: str = None):
    with _READ_TRACKER_LOCK:
        if task_id:
            _READ_TRACKER.pop(task_id, None)
        else:
            _READ_TRACKER.clear()
