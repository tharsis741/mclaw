# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Executable cancellation contract for every model-callable M-Claw tool.

The categories describe what callers may rely on:

``atomic-local``
    The dispatcher can stop the tool before it starts.  Once the short local
    operation begins, it is allowed to finish; no mid-operation rollback is
    claimed.
``cooperative``
    Long multi-stage work observes one turn-owned event at stage/loop
    boundaries.  One synchronous network/subprocess call may still run until
    its bounded timeout, and a local atomic copy may finish its current file;
    the dispatcher keeps the next turn fenced in either case.
``async-or-fenced``
    The implementation either has a native cancellation path or is isolated by
    the dispatcher worker fence until its blocking operation really exits.

Keep each tool name explicit.  Tests compare this map with both toolsets and
the live registry so a newly added or removed tool cannot be classified by
accident.
"""

from __future__ import annotations

import threading
from types import MappingProxyType

ATOMIC_LOCAL_TOOLS = frozenset(
    {
        "delete_file",
        "edit_file",
        "list_directory",
        "memory_add",
        "memory_read",
        "memory_remove",
        "memory_replace",
        "patch",
        "read_file",
        "skill_tree",
        "skill_view",
        "skills_list",
        "write_file",
    }
)

COOPERATIVE_TOOLS = frozenset(
    {
        "search_files",
        "session_search",
        "skill_manage",
        "skill_search",
    }
)

ASYNC_OR_FENCED_TOOLS = frozenset(
    {
        "browser_click",
        "browser_download",
        "browser_navigate",
        "browser_press",
        "browser_screenshot",
        "browser_scroll",
        "browser_snapshot",
        "browser_type",
        "delegate_task",
        "dingtalk_send_file",
        "process",
        "secret_request_many",
        "terminal",
        "vision_analyze",
        "web_extract",
        "web_search",
        "weixin_send_file",
    }
)

CANCELLATION_STRATEGY_BY_TOOL = MappingProxyType(
    {
        **{name: "atomic-local" for name in ATOMIC_LOCAL_TOOLS},
        **{name: "cooperative" for name in COOPERATIVE_TOOLS},
        **{name: "async-or-fenced" for name in ASYNC_OR_FENCED_TOOLS},
    }
)

CANCELLATION_STRATEGY_DESCRIPTIONS = MappingProxyType(
    {
        "atomic-local": (
            "Pre-start cancellation only; an already-started short local operation "
            "finishes without claiming mid-operation rollback."
        ),
        "cooperative": (
            "One shared turn Event is checked between stages and loop items; network "
            "and subprocess I/O has bounded timeouts, while a current local file "
            "operation remains fenced until it returns."
        ),
        "async-or-fenced": (
            "Use native/polled cancellation where available and retain the turn fence "
            "until any blocking worker really exits."
        ),
    }
)


def cancellation_strategy(tool_name: str) -> str:
    """Return the declared strategy, failing loudly for an unclassified tool."""

    return CANCELLATION_STRATEGY_BY_TOOL[tool_name]


def cancellation_checkpoint(cancel_event: threading.Event | None) -> None:
    """Raise before the next stage when the shared turn event is cancelled."""

    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError("Tool operation interrupted by cancellation.")
