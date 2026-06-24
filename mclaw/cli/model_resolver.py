# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Conservative model name and provider resolution for ``/model``.

This module separates model identification from model switching.  The model
catalog can identify known model IDs and offer suggestions, but it is not an
API routing authority by itself: a resolved provider still needs credentials
and endpoint configuration before M-Claw can call the model.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from mclaw.agent import models_dev
from mclaw.cli.auth import PROVIDER_REGISTRY


@dataclass
class ModelSuggestion:
    model: str
    provider: str = ""
    score: float = 0.0


@dataclass
class ModelResolution:
    status: str
    model: str
    provider: str = ""
    message: str = ""
    suggestions: list[ModelSuggestion] = field(default_factory=list)
    model_known: bool = False
    provider_known: bool = False

    @property
    def ok(self) -> bool:
        return self.status in {"ok", "unverified"}


def resolve_provider_key(provider_input: str, user_providers: Optional[dict] = None) -> str:
    """Resolve user-entered provider text to M-Claw's canonical provider key."""
    raw = str(provider_input or "").strip()
    if not raw:
        return ""
    user_providers = user_providers or {}

    if raw in user_providers or raw in PROVIDER_REGISTRY:
        return raw

    lowered = raw.lower()
    for name in user_providers:
        if name.lower() == lowered:
            return name
    for name in PROVIDER_REGISTRY:
        if name.lower() == lowered:
            return name

    compact = _compact_provider_name(raw)
    for name, cfg in user_providers.items():
        display = str(cfg.get("display_name") or name)
        if compact in {_compact_provider_name(name), _compact_provider_name(display)}:
            return name
    for name, cfg in PROVIDER_REGISTRY.items():
        aliases = {_compact_provider_name(alias) for alias in getattr(cfg, "aliases", [])}
        provider_names = {
            _compact_provider_name(name),
            _compact_provider_name(cfg.display_name),
            *aliases,
        }
        if compact in provider_names:
            return name

    return raw


def resolve_model_input(
    model_input: str,
    *,
    provider_input: str = "",
    current_provider: str = "",
    user_providers: Optional[dict] = None,
    max_suggestions: int = 5,
) -> ModelResolution:
    """Resolve a model name without guessing unsafe substitutions.

    Rules:
    - Explicit provider wins and is matched case-insensitively.
    - Exact/normalized model catalog matches may canonicalize the model ID.
    - Fuzzy matches are suggestions only; they never auto-switch the model.
    - Without a provider, unknown models require the user to specify one.
    """
    model = str(model_input or "").strip()
    if not model:
        return ModelResolution(
            status="missing_model",
            model="",
            message="未指定模型。用法: /model <模型名> [--provider <接入方>]",
        )

    user_providers = user_providers or {}
    provider = resolve_provider_key(provider_input, user_providers)
    provider_known = bool(provider and (provider in PROVIDER_REGISTRY or provider in user_providers))
    if provider_input and not provider_known:
        return ModelResolution(
            status="unknown_provider",
            model=model,
            provider=provider,
            message=_unknown_provider_message(provider),
        )

    catalog = _load_catalog()

    if provider_known:
        match = _find_model_match(model, catalog, provider=provider)
        if match:
            return ModelResolution(
                status="ok",
                model=match["model"],
                provider=provider,
                message=f"已识别为 {match['model']}。",
                model_known=True,
                provider_known=True,
            )

        suggestions = _suggest_models(model, catalog, provider=provider, limit=max_suggestions)
        if suggestions:
            return ModelResolution(
                status="unknown_model",
                model=model,
                provider=provider,
                message=_unknown_model_message(model, suggestions, provider=provider),
                suggestions=suggestions,
                provider_known=True,
            )

        return ModelResolution(
            status="unverified",
            model=model,
            provider=provider,
            message=(
                f"模型库暂未识别 {model}。将按你指定的接入方调用，"
                "请确认这是官方模型 ID。"
            ),
            provider_known=True,
        )

    # No explicit provider: prefer exact catalog match, then current provider,
    # then prefix detection.  Do not silently keep current provider for totally
    # unknown model names.
    current = resolve_provider_key(current_provider, user_providers)
    if current and (current in PROVIDER_REGISTRY or current in user_providers):
        match = _find_model_match(model, catalog, provider=current)
        if match:
            return ModelResolution(
                status="ok",
                model=match["model"],
                provider=current,
                message=f"已在当前接入方识别为 {match['model']}。",
                model_known=True,
                provider_known=True,
            )

    matches = _find_model_matches(model, catalog)
    routable = [m for m in matches if _is_routable_provider(m["provider"], user_providers)]
    unique_providers = sorted({m["provider"] for m in routable})
    if len(unique_providers) == 1:
        chosen = routable[0]
        return ModelResolution(
            status="ok",
            model=chosen["model"],
            provider=chosen["provider"],
            message=f"已识别为 {chosen['model']}（{chosen['provider']}）。",
            model_known=True,
            provider_known=True,
        )
    if len(unique_providers) > 1:
        suggestions = [
            ModelSuggestion(model=m["model"], provider=m["provider"], score=1.0)
            for m in routable[:max_suggestions]
        ]
        return ModelResolution(
            status="needs_provider",
            model=model,
            message=_needs_provider_message(model, suggestions),
            suggestions=suggestions,
            model_known=True,
        )

    detected = _detect_provider_for_model(model)
    if detected:
        return ModelResolution(
            status="ok",
            model=model,
            provider=detected,
            message=f"已根据模型名前缀识别接入方：{detected}。",
            provider_known=True,
        )

    suggestions = _suggest_models(model, catalog, limit=max_suggestions)
    if suggestions:
        return ModelResolution(
            status="unknown_model",
            model=model,
            message=_unknown_model_message(model, suggestions),
            suggestions=suggestions,
        )

    return ModelResolution(
        status="needs_provider",
        model=model,
        message=(
            "M-Claw 无法判断这个模型应该通过哪个接入方调用。\n"
            "请指定接入方，例如：/model <模型名> --provider openai\n"
            "如果不确定模型 ID，请到对应大模型平台官网查看官方模型 ID。"
        ),
    )


def _unknown_provider_message(provider: str) -> str:
    available = ", ".join(sorted(PROVIDER_REGISTRY.keys()))
    return (
        f"未知接入方 '{provider}'。\n"
        f"可用接入方: {available}\n"
        "使用 /provider 查看大模型接入状态。"
    )


def _unknown_model_message(model: str, suggestions: list[ModelSuggestion], provider: str = "") -> str:
    lines = [
        f"没有找到完全匹配的模型名：{model}",
        "",
        "相似模型：",
    ]
    for idx, item in enumerate(suggestions, 1):
        provider_text = f" --provider {item.provider}" if item.provider and not provider else ""
        lines.append(f"{idx}. {item.model}{provider_text}")
    lines.extend([
        "",
        "请使用完整模型名重新切换，例如：",
        f"/model {suggestions[0].model} --provider {provider or suggestions[0].provider or '<接入方>'}",
        "如果不确定模型 ID，请到对应大模型平台官网查看官方模型 ID。",
    ])
    return "\n".join(lines)


def _needs_provider_message(model: str, suggestions: list[ModelSuggestion]) -> str:
    lines = [
        f"M-Claw 已识别模型 {model}，但无法唯一判断应该使用哪个接入方。",
        "",
        "可选接入方：",
    ]
    for idx, item in enumerate(suggestions, 1):
        lines.append(f"{idx}. /model {item.model} --provider {item.provider}")
    lines.append("请指定接入方后重试。")
    return "\n".join(lines)


def _load_catalog() -> list[dict[str, str]]:
    registry = models_dev.fetch_models_dev()
    providers_data = models_dev._providers_data(registry)
    reverse = _models_dev_to_mclaw_provider()
    catalog: list[dict[str, str]] = []
    seen = set()
    for provider_id, provider_data in providers_data.items():
        mclaw_provider = reverse.get(provider_id, provider_id)
        for model in models_dev._iter_models(provider_data):
            model_id = str(model.get("id") or model.get("model_id") or "").strip()
            if not model_id:
                continue
            key = (mclaw_provider, model_id.lower())
            if key in seen:
                continue
            seen.add(key)
            catalog.append({
                "provider": mclaw_provider,
                "model": model_id,
                "key": _model_key(model_id),
            })
    return catalog


def _models_dev_to_mclaw_provider() -> dict[str, str]:
    reverse: dict[str, str] = {}
    try:
        from mclaw.cli.provider_profiles import get_default_provider_profile
    except Exception:
        get_default_provider_profile = None

    if get_default_provider_profile:
        for mclaw_provider in PROVIDER_REGISTRY:
            try:
                dev_provider = get_default_provider_profile(mclaw_provider).models_dev_provider
            except Exception:
                continue
            if dev_provider and dev_provider not in reverse:
                reverse[dev_provider] = mclaw_provider

    for mclaw_provider, dev_provider in models_dev.PROVIDER_TO_MODELS_DEV.items():
        if mclaw_provider in PROVIDER_REGISTRY and dev_provider not in reverse:
            reverse[dev_provider] = mclaw_provider
    return reverse


def _find_model_match(model: str, catalog: list[dict[str, str]], *, provider: str = "") -> Optional[dict[str, str]]:
    matches = _find_model_matches(model, catalog, provider=provider)
    return matches[0] if matches else None


def _find_model_matches(model: str, catalog: list[dict[str, str]], *, provider: str = "") -> list[dict[str, str]]:
    key = _model_key(model)
    lower = str(model or "").lower()
    scoped = [item for item in catalog if not provider or item["provider"] == provider]
    exact = [item for item in scoped if item["model"].lower() == lower]
    if exact:
        return exact
    return [item for item in scoped if item["key"] == key]


def _suggest_models(
    model: str,
    catalog: list[dict[str, str]],
    *,
    provider: str = "",
    limit: int = 5,
) -> list[ModelSuggestion]:
    scoped = [item for item in catalog if not provider or item["provider"] == provider]
    query = _model_key(model)
    scored = []
    for item in scoped:
        score = difflib.SequenceMatcher(None, query, item["key"]).ratio()
        if query and (query in item["key"] or item["key"] in query):
            score = max(score, 0.72)
        if score >= 0.62:
            scored.append((score, item))
    scored.sort(key=lambda pair: (-pair[0], pair[1]["provider"], pair[1]["model"]))
    suggestions: list[ModelSuggestion] = []
    seen = set()
    for score, item in scored:
        key = (item["provider"], item["model"].lower())
        if key in seen:
            continue
        seen.add(key)
        suggestions.append(ModelSuggestion(model=item["model"], provider=item["provider"], score=score))
        if len(suggestions) >= limit:
            break
    return suggestions


def _detect_provider_for_model(model: str) -> str:
    lower = str(model or "").lower()
    for pname, pcfg in PROVIDER_REGISTRY.items():
        for prefix in pcfg.model_prefixes:
            if lower.startswith(prefix.lower()):
                return pname
    if "/" in str(model or ""):
        prefix = lower.split("/")[0]
        known_orgs = {
            "anthropic", "openai", "google", "meta-llama", "mistralai",
            "microsoft", "nousresearch", "qwen", "deepseek",
        }
        if prefix in known_orgs or prefix in PROVIDER_REGISTRY:
            return "openrouter"
    return ""


def _is_routable_provider(provider: str, user_providers: dict) -> bool:
    return provider in PROVIDER_REGISTRY or provider in user_providers


def _model_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _compact_provider_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())
