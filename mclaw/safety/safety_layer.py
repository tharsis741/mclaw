"""Unified file-safety preflight for mutating tool calls."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from mclaw.safety.mutation_detector import MutationIntent, detect_mutation
from mclaw.safety.path_resolver import normalize_path, resolve_workspace
from mclaw.safety.policy import RiskDecision, RiskPolicyEngine


@dataclass
class SafetyPlan:
    intent: MutationIntent
    workspace: str
    target_paths: List[str]
    decision: RiskDecision

    @property
    def mutates(self) -> bool:
        return self.intent.mutates


class MClawSafetyLayer:
    """Normalize mutation metadata before checkpoint and operation journal."""

    def __init__(self, *, config: Optional[Dict] = None, checkpoint_manager: Any = None):
        self.config = config or {}
        self.checkpoint_manager = checkpoint_manager
        self.policy = RiskPolicyEngine(self.config)

    def plan(self, tool_name: str, arguments: Dict[str, Any], parent_agent: Any = None) -> SafetyPlan:
        intent = detect_mutation(tool_name, arguments or {})
        terminal_cwd = _terminal_cwd(arguments, parent_agent)
        launch_cwd = _launch_cwd(parent_agent)
        normalized_targets = [
            normalize_path(path, terminal_cwd or launch_cwd)
            for path in intent.target_paths
            if path
        ]
        workspace = self._workspace_for(intent, normalized_targets, terminal_cwd, launch_cwd, parent_agent)
        decision = self.policy.decide(
            action=intent.action,
            target_paths=normalized_targets,
            workspace=workspace,
            raw_command=intent.raw_command,
        ) if intent.mutates else RiskDecision("none", "allow", "read-only")
        return SafetyPlan(intent=intent, workspace=workspace, target_paths=normalized_targets, decision=decision)

    def _workspace_for(
        self,
        intent: MutationIntent,
        target_paths: List[str],
        terminal_cwd: str,
        launch_cwd: str,
        parent_agent: Any = None,
    ) -> str:
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


def _terminal_cwd(arguments: Dict[str, Any], parent_agent: Any = None) -> str:
    workdir = str((arguments or {}).get("workdir") or "").strip()
    if workdir:
        return workdir
    try:
        from mclaw.tools import terminal_tool

        current_id = getattr(terminal_tool, "_current_session_id", None)
        env = getattr(terminal_tool, "_env_registry", {}).get(current_id) if current_id else None
        cwd = getattr(env, "cwd", None)
        if cwd:
            return str(cwd)
    except Exception:
        pass
    return _launch_cwd(parent_agent)


def _launch_cwd(parent_agent: Any = None) -> str:
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


def _target_workspace(target_paths: List[str]) -> str:
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
