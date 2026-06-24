# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Security scan wrapper for Skill 2.0 packages."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mclaw.skills_hub.security_scan import format_scan_report, risk_label, scan_skill, should_allow_install


def review_skill_package(skill_dir: Path, *, source: str = "community") -> dict[str, Any]:
    result = scan_skill(skill_dir, source=source)
    allowed, reason = should_allow_install(result)
    risk_level = risk_label(result.verdict)
    summary = result.summary
    return {
        "risk_level": risk_level,
        "verdict": result.verdict,
        "install_allowed": allowed,
        "install_policy_reason": reason,
        "summary": summary,
        "scan_report": format_scan_report(result),
        "findings": [
            {
                "pattern_id": finding.pattern_id,
                "severity": finding.severity,
                "category": finding.category,
                "file": finding.file,
                "line": finding.line,
                "description": finding.description,
            }
            for finding in result.findings
        ],
    }


def write_security_review(drafting_dir: Path, review: dict[str, Any]) -> Path:
    path = drafting_dir / "security_review.json"
    path.write_text(json.dumps(review, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path
