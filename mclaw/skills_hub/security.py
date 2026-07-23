# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Security scan wrapper for Skill packages."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from mclaw.skills_hub.security_scan import format_scan_report, risk_label, scan_skill, should_allow_install
from mclaw.tools.cancellation import cancellation_checkpoint
from mclaw.tools.interrupt import get_interrupt_event


def review_skill_package(
    skill_dir: Path,
    *,
    source: str = "community",
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Run the guard scanner and serialize the install policy decision."""
    cancel_event = cancel_event or get_interrupt_event()
    result = scan_skill(skill_dir, source=source, cancel_event=cancel_event)
    cancellation_checkpoint(cancel_event)
    allowed, reason = should_allow_install(result)
    risk_level = risk_label(result.verdict)
    summary = result.summary
    findings: list[dict[str, Any]] = []
    for finding in result.findings:
        cancellation_checkpoint(cancel_event)
        findings.append(
            {
                "pattern_id": finding.pattern_id,
                "severity": finding.severity,
                "category": finding.category,
                "file": finding.file,
                "line": finding.line,
                "description": finding.description,
            }
        )
    cancellation_checkpoint(cancel_event)
    return {
        "risk_level": risk_level,
        "verdict": result.verdict,
        "install_allowed": allowed,
        "install_policy_reason": reason,
        "summary": summary,
        "scan_report": format_scan_report(result),
        "findings": findings,
    }


def write_security_review(drafting_dir: Path, review: dict[str, Any]) -> Path:
    """Persist the scanner result as the drafting transaction audit artifact."""
    path = drafting_dir / "security_review.json"
    path.write_text(json.dumps(review, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path
