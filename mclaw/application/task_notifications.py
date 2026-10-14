# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Presentation-only completion records for interactive, top-level tasks."""

from __future__ import annotations

import html
import re
from collections.abc import Collection

from mclaw.application.process_events import public_detail


def first_reply_line(content: str) -> str:
    """Use the first nonempty final-reply line, with inline formatting removed."""
    line = next((part.strip() for part in content.splitlines() if part.strip()), "")
    line = re.sub(r"^(?:(?:#{1,6}|>)\s+|[-+*]\s+|\d+[.)]\s+)+", "", line)
    line = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", line)
    line = re.sub(r"\*\*|__|~~|`+", "", line)
    line = re.sub(r"(?<!\w)([*_])(.+?)\1(?!\w)", r"\2", line)
    line = " ".join(html.unescape(line).split())
    return line if len(line) <= 500 else line[:499] + "…"


def completion_notice(session: dict, task: dict, response: dict,
                      secrets: Collection[str] = ()) -> dict | None:
    """Called only at finalization, never when loading history or saving a Skill."""
    if task.get("source", "web") not in {"web", "cli"} or task.get("status") != "completed":
        return None
    result = task.get("result") or {}
    if (not result.get("completed") or result.get("cancelled") or result.get("interrupted")
            or result.get("completion_unknown") or result.get("error") or result.get("abort_reason")):
        return None
    raw_line = next((line.strip() for line in (response.get("content") or "").splitlines() if line.strip()), "")
    # Redacting JSON may pretty-print it; keep all of that original first line,
    # rather than accidentally displaying only the newly inserted opening brace.
    preview = first_reply_line(" ".join(public_detail(raw_line, secrets).split()))
    return {
        "id": "completion_" + response["id"],
        "session_id": session["id"],
        "task_id": task["id"],
        "response_id": response["id"],
        "title": public_detail(session["title"], secrets),
        "preview": preview,
        "created_at": task["attempts"][-1]["ended_at"],
    }
