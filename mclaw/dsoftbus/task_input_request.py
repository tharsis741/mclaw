# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Task-local requests for additional caller input."""

from __future__ import annotations

import copy
import threading
from collections.abc import Mapping, Sequence
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any


INPUT_KINDS = frozenset({"text", "file", "directory"})


class TaskInputRequestError(RuntimeError):
    """Stable failure raised before an input request changes Task state."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def normalize_input_request(value: Any) -> Mapping[str, Any]:
    """Validate the bounded public request carried in Task status metadata."""

    if not isinstance(value, Mapping) or frozenset(value) != frozenset(
        {"message", "accepts"}
    ):
        raise TaskInputRequestError("INVALID_PARAMS")
    message = value["message"]
    accepts = value["accepts"]
    try:
        message_size = len(message.encode("utf-8")) if isinstance(message, str) else 0
    except UnicodeEncodeError as error:
        raise TaskInputRequestError("INVALID_PARAMS") from error
    if not 1 <= message_size <= 4_096:
        raise TaskInputRequestError("INVALID_PARAMS")
    if (
        not isinstance(accepts, (tuple, list))
        or not 1 <= len(accepts) <= len(INPUT_KINDS)
        or any(not isinstance(item, str) or item not in INPUT_KINDS for item in accepts)
        or len(set(accepts)) != len(accepts)
    ):
        raise TaskInputRequestError("INVALID_PARAMS")
    return MappingProxyType(
        {
            "message": message,
            "accepts": tuple(accepts),
        }
    )


@dataclass(slots=True)
class TaskInputRequestCollector:
    """Thread-safe single-request collector for one inbound Agent turn."""

    _request: dict[str, Any] | None = field(default=None, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def request(self, *, message: str, accepts: Sequence[str]) -> Mapping[str, Any]:
        normalized = normalize_input_request(
            {"message": message, "accepts": list(accepts)}
        )
        plain = {
            "message": str(normalized["message"]),
            "accepts": list(normalized["accepts"]),
        }
        with self._lock:
            if self._request is not None:
                raise TaskInputRequestError("CAPACITY_BUSY")
            self._request = plain
        return MappingProxyType(copy.deepcopy(plain))

    def snapshot(self) -> Mapping[str, Any] | None:
        with self._lock:
            if self._request is None:
                return None
            return MappingProxyType(copy.deepcopy(self._request))


_CURRENT_COLLECTOR: ContextVar[TaskInputRequestCollector | None] = ContextVar(
    "mclaw_dsoftbus_task_input_request_collector",
    default=None,
)


def bind_task_input_request_collector(
    collector: TaskInputRequestCollector,
) -> Token[TaskInputRequestCollector | None]:
    return _CURRENT_COLLECTOR.set(collector)


def reset_task_input_request_collector(
    token: Token[TaskInputRequestCollector | None],
) -> None:
    _CURRENT_COLLECTOR.reset(token)


def get_task_input_request_collector() -> TaskInputRequestCollector | None:
    return _CURRENT_COLLECTOR.get()


__all__ = [
    "INPUT_KINDS",
    "TaskInputRequestCollector",
    "TaskInputRequestError",
    "bind_task_input_request_collector",
    "get_task_input_request_collector",
    "normalize_input_request",
    "reset_task_input_request_collector",
]
