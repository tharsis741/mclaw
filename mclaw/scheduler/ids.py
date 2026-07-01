# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""ID helpers for scheduler rows."""

from __future__ import annotations

import secrets
import uuid


def new_job_id() -> str:
    return f"job_{uuid.uuid4().hex[:12]}"


def new_run_id() -> str:
    return f"run_{uuid.uuid4().hex[:12]}"


def new_target_id(prefix: str = "target") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def new_pairing_code() -> str:
    """Return a short code intended for manual channel-target binding."""
    return f"SC-{secrets.randbelow(900000) + 100000}"


def normalize_pairing_code(value: str) -> str:
    """Canonicalize pairing codes before persistence and lookup."""
    return str(value or "").strip().upper()
