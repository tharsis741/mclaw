# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Skills Guard — minimal security scanner for skills content."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Tuple

TRUST_BUNDLED = "bundled"
TRUST_COMMUNITY = "community"
TRUST_AGENT_CREATED = "agent_created"

INSTALL_POLICY = {
    TRUST_BUNDLED: ("allow", "allow", "allow"),
    TRUST_COMMUNITY: ("allow", "ask", "block"),
    TRUST_AGENT_CREATED: ("allow", "allow", "ask"),
}

VERDICT_INDEX = {"safe": 0, "caution": 1, "dangerous": 2}
RISK_LABELS = {
    "safe": "安全",
    "caution": "存疑",
    "dangerous": "危险",
}


@dataclass
class Finding:
    """One matched guard pattern in a Skill package."""

    pattern_id: str
    severity: str
    category: str
    file: str
    line: int
    match: str
    description: str


@dataclass
class ScanResult:
    """Aggregate guard verdict plus findings for install policy checks."""

    skill_name: str
    source: str
    trust_level: str
    verdict: str
    findings: List[Finding] = field(default_factory=list)
    summary: str = ""


THREAT_PATTERNS: List[Tuple[str, str, str, str, str]] = [
    (r"ignore\s+(?:\w+\s+)*(previous|all|above|prior)\s+instructions", "prompt_injection_ignore", "critical", "injection", "prompt injection: ignore previous instructions"),
    (r"you\s+are\s+(?:\w+\s+)*now\s+", "role_hijack", "high", "injection", "attempts to override the agent's role"),
    (r"(?:你是一个|你现在是|i am a professional|i am now a|as a professional)", "role_play_injection", "high", "injection", "attempts to override agent persona via role-play"),
    (r"\[\s*INST\s*\]|\[\s*#\s*INSTRUCTION\s*\]", "instruction_marker", "high", "injection", "instruction override marker bracket notation"),
    (r"<<\s*EOF|<<\s*EOT|<<\s*EOS|<<\s*END", "heredoc_injection", "high", "injection", "heredoc injection technique"),
    (r"(?:base64|d3vLnw|b3BlbnNo|cmFuZG9t)", "encoded_injection", "high", "injection", "possible encoded/obfuscated instruction"),
    (r"(?:system|prompt)\s*:\s*override|override\s*(?:system|prompt)", "prompt_override", "high", "injection", "attempts to override system prompt"),
    (r"<xml[^>]*>.*?(?:instruction|system|prompt).*?</xml>", "xml_injection", "medium", "injection", "XML-style prompt injection"),
    (r"curl\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)", "env_exfil_curl", "critical", "exfiltration", "curl command interpolating secret env var"),
    (r"wget\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)", "env_exfil_wget", "critical", "exfiltration", "wget command interpolating secret env var"),
    (r"cat\s+[^\n]*(\.env|credentials|\.netrc|\.pgpass|\.npmrc|\.pypirc)", "read_secrets_file", "critical", "exfiltration", "reads known secrets file"),
    (r"rm\s+-rf\s+/", "destructive_root_rm", "critical", "destructive", "recursive delete from root"),
    (r"authorized_keys", "ssh_persistence", "critical", "persistence", "modifies SSH authorized keys"),
]


def _severity_to_verdict(severity: str) -> str:
    if severity == "critical":
        return "dangerous"
    if severity in ("high", "medium"):
        return "caution"
    return "safe"


def _max_verdict(v1: str, v2: str) -> str:
    return v1 if VERDICT_INDEX[v1] >= VERDICT_INDEX[v2] else v2


def risk_label(verdict: str) -> str:
    """Return the user-facing Chinese risk label for a guard verdict."""
    return RISK_LABELS.get(str(verdict or "").lower(), "存疑")


def _detect_trust_level(source: str) -> str:
    """Map source metadata into the trust tiers used by install policy."""
    if source == TRUST_BUNDLED:
        return TRUST_BUNDLED
    if source == TRUST_AGENT_CREATED:
        return TRUST_AGENT_CREATED
    return TRUST_COMMUNITY


def scan_skill(skill_dir: Path, source: str = "community") -> ScanResult:
    """Scan text files in a Skill package for known injection/exfiltration patterns."""
    trust_level = _detect_trust_level(source)
    verdict = "safe"
    findings: List[Finding] = []

    skill_dir = Path(skill_dir)
    for fpath in skill_dir.rglob("*"):
        if not fpath.is_file():
            continue
        if fpath.stat().st_size > 2 * 1024 * 1024:
            continue
        try:
            text = fpath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        for lineno, line in enumerate(text.splitlines(), 1):
            for pattern, pid, severity, category, description in THREAT_PATTERNS:
                m = re.search(pattern, line, re.IGNORECASE)
                if not m:
                    continue
                findings.append(
                    Finding(
                        pattern_id=pid,
                        severity=severity,
                        category=category,
                        file=str(fpath.relative_to(skill_dir)),
                        line=lineno,
                        match=m.group(0)[:120],
                        description=description,
                    )
                )
                verdict = _max_verdict(verdict, _severity_to_verdict(severity))

    summary = f"{len(findings)} findings; verdict={verdict}"
    return ScanResult(
        skill_name=skill_dir.name,
        source=source,
        trust_level=trust_level,
        verdict=verdict,
        findings=findings,
        summary=summary,
    )


def should_allow_install(result: ScanResult) -> tuple[bool | None, str]:
    """Apply source-trust policy to a scanner verdict.

    The tri-state return lets callers distinguish automatic allow, automatic
    block, and user-confirmation-required outcomes.
    """
    policy = INSTALL_POLICY.get(result.trust_level, INSTALL_POLICY[TRUST_COMMUNITY])
    action = policy[VERDICT_INDEX[result.verdict]]
    if action == "allow":
        return True, "policy_allow"
    if action == "block":
        return False, "policy_block"
    return None, "policy_ask"


def format_scan_report(result: ScanResult) -> str:
    """Render a compact human-readable scanner report for audit context."""
    lines = [
        f"Skill: {result.skill_name}",
        f"Source: {result.source}",
        f"Trust: {result.trust_level}",
        f"Verdict: {result.verdict}",
        f"Summary: {result.summary}",
    ]
    for f in result.findings[:30]:
        lines.append(
            f"- [{f.severity}] {f.file}:{f.line} {f.pattern_id} — {f.description}"
        )
    if len(result.findings) > 30:
        lines.append(f"... and {len(result.findings) - 30} more findings")
    return "\n".join(lines)
