"""Runtime coordination for model-library and provider status commands."""

from __future__ import annotations

import shlex
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RuntimeModelLibraryHooks:
    """Host operations for UI-neutral model library commands."""

    user_providers: Callable[[], dict[str, Any]]
    current_model: Callable[[], str]
    current_provider: Callable[[], str]
    current_base_url: Callable[[], str]
    current_api_mode: Callable[[], str]
    provider_registry: Callable[[], Mapping[str, Any]]
    list_configured_providers: Callable[[], Sequence[Any]]
    fetch_registry: Callable[[], Any]
    registry_stats: Callable[[Any], dict[str, Any]]
    cache_path: Callable[[], Any]
    refresh_cache: Callable[[], dict[str, Any]]
    resolve_provider_key: Callable[[str, dict[str, Any]], str | None]
    get_provider_profile: Callable[[str, str], Any]
    get_provider_profiles: Callable[[str], Sequence[Any]]
    list_provider_models: Callable[..., Sequence[str]]
    list_models_dev_provider_ids: Callable[[], Sequence[str]]
    search_models_dev_provider_ids: Callable[[str, Sequence[str]], Sequence[str]]
    render_notice: Callable[[str, str, str, str], None]
    render_providers: Callable[..., None]


class RuntimeModelLibraryCommandCoordinator:
    """Handles `/model-update` and `/provider` library views."""

    def __init__(self, hooks: RuntimeModelLibraryHooks) -> None:
        self.hooks = hooks

    def handle_model_update(self, raw_args: str = "") -> None:
        parts = self._split_args(raw_args)
        command = parts[0].lower() if parts else ""

        if command == "status":
            registry = self.hooks.fetch_registry()
            stats = self.hooks.registry_stats(registry)
            self.hooks.render_notice(
                "M-Claw 模型库",
                "当前模型库缓存状态",
                self._format_stats_detail(stats),
                "info",
            )
            return

        if command == "provider":
            self._handle_model_update_provider(parts)
            return

        if command == "list":
            self._handle_model_update_list(parts)
            return

        result = self.hooks.refresh_cache()
        if result.get("ok"):
            self.hooks.render_notice(
                "M-Claw 模型库",
                "模型库缓存已更新",
                self._format_result_detail(result),
                "success",
            )
            return

        self.hooks.render_notice(
            "M-Claw 模型库",
            "模型库更新失败，已保留现有缓存",
            self._format_result_detail(result, include_error=True),
            "warning",
        )

    def handle_provider(self, raw_args: str = "") -> None:
        args = str(raw_args or "").strip().split()
        if "--profile" in args:
            provider_input = next((arg for arg in args if arg != "--profile"), "") or self.hooks.current_provider()
            provider_key = self.hooks.resolve_provider_key(provider_input, self.hooks.user_providers())
            registry = self.hooks.provider_registry()
            if not provider_key or provider_key not in registry:
                self.hooks.render_notice(
                    "M-Claw · 大模型接入",
                    f"未识别接入方: {provider_input}",
                    "用法: /provider <供应商> --profile",
                    "warning",
                )
                return

            pcfg = registry[provider_key]
            lines = [f"接入方: {pcfg.display_name} ({provider_key})", ""]
            for profile in self.hooks.get_provider_profiles(provider_key):
                status = "可配置" if getattr(profile, "callable", False) else "仅模型库"
                lines.append(f"{profile.id}  {profile.label}  [{status}]")
                lines.append(f"  models.dev: {profile.models_dev_provider}")
                lines.append(f"  类型: {profile.kind}")
                if getattr(profile, "note", ""):
                    lines.append(f"  说明: {profile.note}")
            self.hooks.render_notice(
                "M-Claw · 接口类型",
                "可用 profile",
                "\n".join(lines),
                "info",
            )
            return

        self.hooks.render_providers(
            model=self.hooks.current_model(),
            provider=self.hooks.current_provider(),
            base_url=self.hooks.current_base_url(),
            api_mode=self.hooks.current_api_mode(),
            configured=self.hooks.list_configured_providers(),
            provider_registry=self.hooks.provider_registry(),
        )

    def _handle_model_update_provider(self, parts: list[str]) -> None:
        provider_query = parts[1] if len(parts) > 1 else ""
        profile_query = parts[2] if len(parts) > 2 else ""
        provider = self.hooks.resolve_provider_key(provider_query, self.hooks.user_providers())
        registry = self.hooks.provider_registry()
        if not provider or provider not in registry:
            self.hooks.render_notice(
                "M-Claw 模型库",
                "未识别接入方。用法: /model-update provider <接入方> [profile]",
                "",
                "warning",
            )
            return

        lines = [f"接入方: {registry[provider].display_name} ({provider})", ""]
        if profile_query:
            profiles = [self.hooks.get_provider_profile(provider, profile_query)]
        else:
            profiles = list(self.hooks.get_provider_profiles(provider))

        for profile in profiles:
            models = self.hooks.list_provider_models(provider, limit=20, profile_id=profile.id)
            lines.append(f"{profile.label} ({profile.id}) [{profile.status_label}]")
            lines.append(f"models.dev: {profile.models_dev_provider}")
            if getattr(profile, "note", ""):
                lines.append(f"说明: {profile.note}")
            if models:
                for idx, model in enumerate(models, 1):
                    lines.append(f"  {idx}. {model}")
            else:
                lines.append("  当前缓存没有这个 profile 的模型列表。")
            lines.append("")

        self.hooks.render_notice(
            "M-Claw 模型库",
            "接入方模型视图",
            "\n".join(lines).rstrip(),
            "info",
        )

    def _handle_model_update_list(self, parts: list[str]) -> None:
        query = " ".join(parts[1:]).strip()
        provider_ids = list(self.hooks.list_models_dev_provider_ids())
        matches = list(self.hooks.search_models_dev_provider_ids(query, provider_ids)) if query else provider_ids[:20]
        detail_lines = [f"查询: {query or '前 20 个 provider id'}", ""]
        if matches:
            detail_lines.extend(f"{idx}. {provider_id}" for idx, provider_id in enumerate(matches, 1))
        else:
            detail_lines.extend([
                "没有匹配的 models.dev provider id。",
                "如果它是私有部署或网关服务，请使用自定义兼容接口。",
                "如果不确定官方名称，请到对应大模型平台官网查看接入说明。",
            ])
        self.hooks.render_notice(
            "M-Claw 模型库",
            "models.dev provider 查询",
            "\n".join(detail_lines),
            "info" if matches else "warning",
        )

    def _format_stats_detail(self, stats: dict[str, Any]) -> str:
        return (
            f"缓存: {self.hooks.cache_path()}\n"
            f"供应商: {stats.get('providers', 0)}\n"
            f"模型: {stats.get('models', 0)}"
        )

    @staticmethod
    def _format_result_detail(result: dict[str, Any], *, include_error: bool = False) -> str:
        lines = []
        if include_error:
            lines.append(f"错误: {result.get('error', 'unknown')}")
        lines.extend([
            f"缓存: {result.get('cache_path')}",
            f"供应商: {result.get('providers', 0)}",
            f"模型: {result.get('models', 0)}",
        ])
        return "\n".join(lines)

    @staticmethod
    def _split_args(raw_args: str) -> list[str]:
        try:
            return shlex.split(str(raw_args or "").strip())
        except ValueError:
            return str(raw_args or "").strip().split()
