# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Bounded, redacted execution diagnostics for isolated session workers."""

from __future__ import annotations

import copy
import hashlib
import logging
import os
import re
from collections.abc import Callable, Collection
from logging.handlers import RotatingFileHandler
from pathlib import Path

from mclaw.runtime.secrets import redact_secret_values

_STAGES = (
    "[WORKER]", "[MODEL REQUEST]", "[MODEL RESPONSE]", "[VISION INPUT]",
    "[TOOL DISPATCH START]", "[TOOL DISPATCH END]", "[TOOL CONCURRENT START]",
    "[TOOL CONCURRENT END]", "[TOOL DONE]", "[TOOL FINISHED]",
    "[SKILL REVIEW]", "[BACKGROUND REVIEW]",
)
_SECRET_NAME = re.compile(r"key|token|secret|password|credential", re.I)
_ASSIGNMENT = re.compile(
    r"(?i)(\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|passwd|secret|client[_-]?secret)"
    r"[\"']?\s*[:=]\s*[\"']?)[^\"'\s,;}]+"
)
_BEARER = re.compile(r"(?i)\bBearer\s+[^\s\"',;}]+")
_HEADER_SECRET = re.compile(
    r"(?i)(\b(?:authorization|proxy-authorization|cookie|set-cookie)[\"']?\s*[:=]\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|[^\r\n]*)"
)
_URL_USERINFO = re.compile(r"(https?://)[^/\s]+@", re.I)
_IMAGE = re.compile(r"data:image/[^;,\s]+;base64,[A-Za-z0-9+/=\s]+", re.I)
_API_KEY = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}")


class _Stages(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # Existing verbose INFO records include prompts and message bodies.
        return record.levelno >= logging.WARNING or (
            record.name.startswith("mclaw.") and isinstance(record.msg, str)
            and record.msg.startswith(_STAGES)
        )


class _WorkerFormatter(logging.Formatter):
    def __init__(self, session_id: str, secrets: Callable[[], Collection[str]], run_id: Callable[[], str]):
        super().__init__("%(asctime)s %(levelname)s session=%(session_id)s run=%(run_id)s %(name)s %(message)s")
        self.session_id, self.secrets, self.run_id = session_id, secrets, run_id
        self.environment_secrets = {value for name, value in os.environ.items() if _SECRET_NAME.search(name) and value}

    def format(self, record: logging.LogRecord) -> str:
        record = copy.copy(record)
        record.session_id, record.run_id = self.session_id, self.run_id() or "-"
        text = super().format(record)
        text = redact_secret_values(text, self.environment_secrets | set(self.secrets()))
        text = _IMAGE.sub("<image data>", text)
        text = _ASSIGNMENT.sub(r"\1<redacted>", text)
        text = _BEARER.sub("Bearer <redacted>", text)
        text = _HEADER_SECRET.sub(r"\1<redacted>", text)
        text = _URL_USERINFO.sub(r"\1<redacted>@", text)
        text = _API_KEY.sub("<redacted>", text)
        return text[:16000] + (" [truncated]" if len(text) > 16000 else "")


def configure_worker_logging(home: Path, session_id: str, *,
                             secrets: Callable[[], Collection[str]], run_id: Callable[[], str]) -> RotatingFileHandler | None:
    """Each worker owns its file, keeping stdout reserved for the event protocol."""
    root = Path(home) / "logs" / "sessions"
    name = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    try:
        root.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(root / f"{name}.log", maxBytes=4 * 1024 * 1024,
                                      backupCount=2, encoding="utf-8")
    except OSError as exc:
        logging.getLogger(__name__).warning("[WORKER] session log unavailable (%s)", type(exc).__name__)
        return None
    handler.setLevel(logging.INFO)
    handler.addFilter(_Stages())
    handler.setFormatter(_WorkerFormatter(session_id, secrets, run_id))
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)
    return handler
