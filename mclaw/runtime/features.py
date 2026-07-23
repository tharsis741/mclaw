# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Declarative runtime feature dependency registry."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from mclaw.providers.registry import get_runtime_profile


class FeatureState(str, Enum):
    """Availability state exposed by runtime capability checks."""
    ENABLED = "enabled"
    DISABLED = "disabled"
    AVAILABLE_WITH_INSTALL = "available_with_install"
    AVAILABLE_WITH_CONFIG = "available_with_config"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RuntimeFeature:
    """Resolved runtime capability with an operator-facing reason."""
    name: str
    state: FeatureState
    reason: str = ""

    @property
    def enabled(self) -> bool:
        return self.state == FeatureState.ENABLED

    def to_dict(self) -> dict[str, str | bool]:
        return {
            "name": self.name,
            "state": self.state.value,
            "enabled": self.enabled,
            "reason": self.reason,
        }


_TOOL_TO_FEATURE = {
    "terminal": "terminal",
    "process": "process",
    "read_file": "file",
    "write_file": "file",
    "patch": "file",
    "edit_file": "file",
    "delete_file": "file",
    "search_files": "file",
    "list_directory": "file",
    "delegate_task": "delegation",
    "browser_navigate": "browser_tool",
    "browser_snapshot": "browser_tool",
    "browser_screenshot": "browser_tool",
    "browser_click": "browser_tool",
    "browser_type": "browser_tool",
    "browser_scroll": "browser_tool",
    "browser_press": "browser_tool",
    "browser_download": "browser_tool",
}

_TOOLSET_TO_FEATURE = {
    "terminal": "terminal",
    "file": "file",
    "delegation": "delegation",
    "browser": "browser_tool",
}


@dataclass(frozen=True)
class RuntimeFeatures:
    """Runtime capability matrix used by setup and tool exposure."""

    features: dict[str, RuntimeFeature]

    def get(self, name: str) -> RuntimeFeature:
        return self.features.get(
            name,
            RuntimeFeature(name=name, state=FeatureState.UNKNOWN, reason="not declared"),
        )

    def is_enabled(self, name: str) -> bool:
        return self.get(name).enabled

    def tool_enabled(self, tool_name: str) -> bool:
        feature = _TOOL_TO_FEATURE.get(tool_name)
        return True if feature is None else self.is_enabled(feature)

    def toolset_enabled(self, toolset: str) -> bool:
        feature = _TOOLSET_TO_FEATURE.get(toolset)
        return True if feature is None else self.is_enabled(feature)

    def filter_tool_names(self, tool_names: set[str]) -> set[str]:
        return {name for name in tool_names if self.tool_enabled(name)}

    def to_dict(self) -> dict[str, dict[str, str | bool]]:
        return {name: feature.to_dict() for name, feature in self.features.items()}


def runtime_features(
    *,
    terminal: FeatureState = FeatureState.ENABLED,
    file: FeatureState = FeatureState.ENABLED,
    process: FeatureState = FeatureState.ENABLED,
    delegation: FeatureState = FeatureState.ENABLED,
    checkpoint: FeatureState = FeatureState.UNKNOWN,
    pet: FeatureState = FeatureState.UNKNOWN,
    browser_tool: FeatureState = FeatureState.UNKNOWN,
    reasons: dict[str, str] | None = None,
) -> RuntimeFeatures:
    """Build the standard runtime capability matrix for one host profile."""
    reasons = reasons or {}
    states = {
        "terminal": terminal,
        "file": file,
        "process": process,
        "delegation": delegation,
        "checkpoint": checkpoint,
        "pet": pet,
        "browser_tool": browser_tool,
    }
    return RuntimeFeatures(
        {
            name: RuntimeFeature(name=name, state=state, reason=reasons.get(name, ""))
            for name, state in states.items()
        }
    )


@dataclass(frozen=True)
class FeatureSpec:
    """Declarative dependency contract for optional tools and runtime features."""
    name: str
    kind: str
    display_name: str = ""
    description: str = ""
    toolset: str = ""
    config_path: str = ""
    requires: tuple[str, ...] = ()
    requires_any: tuple[tuple[str, ...], ...] = field(default_factory=tuple)

    @property
    def required_for(self) -> str:
        return f"{self.kind}:{self.name}"

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "name": self.name,
            "kind": self.kind,
            "display_name": self.display_name or self.name,
            "description": self.description,
        }
        if self.toolset:
            data["toolset"] = self.toolset
        if self.config_path:
            data["config_path"] = self.config_path
        if self.requires:
            data["requires"] = list(self.requires)
        if self.requires_any:
            data["requires_any"] = [list(group) for group in self.requires_any]
        return data


_QWEN_CREDENTIAL_GROUPS = (
    get_runtime_profile("qwen").env_vars,
    get_runtime_profile("qwen-intl").env_vars,
)

FEATURES: dict[str, FeatureSpec] = {
    "web_search": FeatureSpec(
        name="web_search",
        kind="tool",
        display_name="网页搜索",
        description="优先通过 Tavily 搜索网页，可选 DashScope/Qwen 作为第二搜索源。",
        toolset="web",
        requires_any=(("TAVILY_API_KEY",), *_QWEN_CREDENTIAL_GROUPS),
    ),
    "vision_analyze": FeatureSpec(
        name="vision_analyze",
        kind="tool",
        display_name="视觉分析",
        description="通过 Qwen 视觉模型分析图片。",
        toolset="vision",
        requires_any=_QWEN_CREDENTIAL_GROUPS,
    ),
    "asr": FeatureSpec(
        name="asr",
        kind="runtime",
        display_name="语音输入",
        description="通过 DashScope/Qwen 实时语音识别启用麦克风输入。",
        config_path="auxiliary.asr.enabled",
        requires_any=_QWEN_CREDENTIAL_GROUPS,
    ),
}


def get_feature(name: str) -> FeatureSpec | None:
    return FEATURES.get(name)


def list_features() -> list[dict[str, Any]]:
    return [spec.to_dict() for spec in FEATURES.values()]


def list_feature_specs() -> list[FeatureSpec]:
    return list(FEATURES.values())


def _has_value(get_value, env_var: str) -> bool:
    return bool(get_value(env_var))


def is_feature_configured(spec: FeatureSpec, get_value) -> bool:
    """Return whether config/env values satisfy a feature's dependency contract."""
    if spec.requires and not all(_has_value(get_value, env_var) for env_var in spec.requires):
        return False
    if spec.requires_any:
        return any(any(_has_value(get_value, env_var) for env_var in group) for group in spec.requires_any)
    return True


def configured_env_vars(spec: FeatureSpec, get_value) -> list[str]:
    """Return the concrete env vars that currently satisfy a feature spec."""
    if not is_feature_configured(spec, get_value):
        return []
    values: list[str] = []
    for env_var in spec.requires:
        if _has_value(get_value, env_var) and env_var not in values:
            values.append(env_var)
    for group in spec.requires_any:
        present = [env_var for env_var in group if _has_value(get_value, env_var)]
        if present:
            for env_var in present:
                if env_var not in values:
                    values.append(env_var)
            break
    return values


def _is_authorized(required_for: str, env_var: str) -> bool:
    try:
        from mclaw.runtime.secrets import is_authorized

        return is_authorized(required_for, env_var)
    except Exception:
        return False


def authorized_env_value(required_for: str, env_var: str, get_value, default: str = "") -> str:
    """Return a configured env value only when the feature scope authorized it."""
    value = get_value(env_var) or default
    if not value:
        return ""
    return value if _is_authorized(required_for, env_var) else ""


def authorized_configured_env_vars(spec: FeatureSpec, get_value) -> list[str]:
    """Return configured env vars that satisfy both dependency and allowlist rules."""
    values: list[str] = []

    for env_var in spec.requires:
        if not _has_value(get_value, env_var) or not _is_authorized(spec.required_for, env_var):
            return []
        values.append(env_var)

    if spec.requires_any:
        matched_group: list[str] = []
        for group in spec.requires_any:
            authorized_present = [
                env_var
                for env_var in group
                if _has_value(get_value, env_var) and _is_authorized(spec.required_for, env_var)
            ]
            if authorized_present:
                matched_group = authorized_present
                break
        if not matched_group:
            return []
        for env_var in matched_group:
            if env_var not in values:
                values.append(env_var)

    return values


def is_feature_authorized(spec: FeatureSpec, get_value) -> bool:
    return bool(authorized_configured_env_vars(spec, get_value))


def secret_requests_for_feature(spec: FeatureSpec, get_value) -> list[dict[str, str]]:
    """Build model-safe secret request descriptors for missing feature credentials."""
    if is_feature_configured(spec, get_value):
        return []

    requests: list[dict[str, str]] = []
    for env_var in spec.requires:
        if not _has_value(get_value, env_var):
            requests.append({"env_var": env_var, "provider": spec.display_name or spec.name, "purpose": spec.description})

    if spec.requires_any and not any(any(_has_value(get_value, env_var) for env_var in group) for group in spec.requires_any):
        first_group = spec.requires_any[0]
        if first_group:
            requests.append(
                {
                    "env_var": first_group[0],
                    "provider": spec.display_name or spec.name,
                    "purpose": spec.description,
                }
            )
    return requests
