# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unified file-safety preflight for mutating tool calls."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mclaw.safety.mutation_detector import MutationIntent, detect_mutation
from mclaw.safety.path_resolver import normalize_path, resolve_workspace
from mclaw.safety.policy import RiskDecision, RiskPolicyEngine


@dataclass
class SafetyPlan:
    """Preflight output shared by checkpointing, journal, and dispatch layers."""

    intent: MutationIntent
    workspace: str
    target_paths: list[str]
    decision: RiskDecision

    @property
    def mutates(self) -> bool:
        return self.intent.mutates


class MClawSafetyLayer:
    """Normalize mutation metadata before checkpoint and operation journal."""

    def __init__(
        self,
        *,
        config: dict | None = None,
        checkpoint_manager: Any = None,
        path_policy: Any = None,
    ):
        self.checkpoint_manager = checkpoint_manager
        self.policy = RiskPolicyEngine()
        self.path_policy = path_policy

    def plan(self, tool_name: str, arguments: dict[str, Any], parent_agent: Any = None) -> SafetyPlan:
        """Build a policy-ready safety plan for one tool invocation."""
        intent = detect_mutation(tool_name, arguments or {})
        terminal_cwd = _terminal_cwd(arguments, parent_agent)
        launch_cwd = _launch_cwd(parent_agent)
        target_base = terminal_cwd if tool_name == "terminal" else launch_cwd
        normalized_targets: list[str] = []
        normalized_actions: list[str] = []
        target_actions = intent.target_actions or []
        for index, target in enumerate(intent.target_paths):
            if not target:
                continue
            normalized_targets.append(normalize_path(target, target_base or launch_cwd))
            normalized_actions.append(
                target_actions[index]
                if index < len(target_actions)
                else intent.action
            )
        workspace = self._workspace_for(normalized_targets, terminal_cwd, launch_cwd, parent_agent)
        decision = self.policy.decide(
            action=intent.action,
            target_paths=normalized_targets,
            raw_command=intent.raw_command,
        ) if intent.mutates else RiskDecision("none", "allow", "read-only")
        if decision.allowed and normalized_targets:
            path_policy = self.path_policy or self._active_path_policy()
            for target_action, target in zip(normalized_actions, normalized_targets):
                path_decision = path_policy.check(target_action, target)
                if not path_decision.allowed:
                    decision = RiskDecision(
                        "blocked",
                        "block",
                        f"{path_decision.reason}: {path_decision.resolved} "
                        f"(scope={path_decision.scope})",
                    )
                    break
        return SafetyPlan(intent=intent, workspace=workspace, target_paths=normalized_targets, decision=decision)

    @staticmethod
    def _active_path_policy():
        from mclaw.runtime.manager import RuntimeManager

        return RuntimeManager.current().paths

    def _workspace_for(
        self,
        target_paths: list[str],
        terminal_cwd: str,
        launch_cwd: str,
        parent_agent: Any = None,
    ) -> str:
        """Resolve the checkpoint workspace, preferring explicit target ownership."""
        target_workspace = _target_workspace(target_paths)
        if target_workspace:
            return target_workspace

        explicit = ""
        if self.checkpoint_manager is not None:
            try:
                seed = terminal_cwd or launch_cwd
                if seed:
                    explicit = self.checkpoint_manager.get_working_dir_for_path(seed)
            except Exception:
                explicit = ""
        return resolve_workspace(
            explicit_workdir=explicit,
            target_paths=None,
            terminal_cwd=terminal_cwd,
            launch_cwd=launch_cwd,
            recent_checkpoint_dir=str(getattr(parent_agent, "_last_checkpoint_work_dir", "") or ""),
            fallback_cwd=launch_cwd,
        )


def _terminal_cwd(arguments: dict[str, Any], parent_agent: Any = None) -> str:
    """Find the shell cwd associated with the current terminal tool session."""
    workdir = str((arguments or {}).get("workdir") or "").strip()
    if workdir:
        return workdir
    try:
        from mclaw.tools.dispatch import get_current_session_id
        from mclaw.tools.terminal_tool import get_session_cwd

        current_id = get_current_session_id()
        cwd = get_session_cwd(current_id)
        if cwd:
            return str(cwd)
    except Exception:
        pass
    return _launch_cwd(parent_agent)


def _launch_cwd(parent_agent: Any = None) -> str:
    """Return the configured process launch cwd when terminal state is unavailable."""
    workspace = str(getattr(parent_agent, "workspace_path", "") or "").strip()
    if workspace:
        return workspace
    cfg = getattr(parent_agent, "config", {}) if parent_agent is not None else {}
    if isinstance(cfg, dict):
        terminal_cfg = cfg.get("terminal", {})
        if isinstance(terminal_cfg, dict):
            configured = str(terminal_cfg.get("cwd") or "").strip()
            if configured and configured != ".":
                return configured
        launch_cwd = str(cfg.get("_launch_cwd") or "").strip()
        if launch_cwd:
            return launch_cwd
    return ""


def _target_workspace(target_paths: list[str]) -> str:
    """Use the first explicit target as the checkpoint workspace anchor."""
    for target in target_paths or []:
        if not target:
            continue
        path = Path(target).expanduser()
        try:
            path = path.resolve()
        except OSError:
            pass
        return str(path if path.is_dir() else path.parent)
    return ""
