# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Stable identifier helpers for scheduler job and run records."""

from __future__ import annotations

import random
import uuid


def new_job_id() -> str:
    return f"job_{uuid.uuid4().hex[:12]}"


def new_run_id() -> str:
    return f"run_{uuid.uuid4().hex[:12]}"


def new_target_id(prefix: str = "target") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def new_pairing_code() -> str:
    return f"SC-{random.randint(100000, 999999)}"
