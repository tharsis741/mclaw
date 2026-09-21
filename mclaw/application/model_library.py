# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Offline model-library browsing and explicit shared-cache refresh."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from mclaw.agent import models_dev
from mclaw.application.sessions import ApplicationError
from mclaw.cli.provider_profiles import get_provider_profiles, get_setup_profile_label, get_setup_provider_label
from mclaw.providers.registry import PROVIDER_REGISTRY


class ModelLibraryService:
    def __init__(self, home: Path) -> None:
        self.home = Path(home).resolve()
        self._refresh = threading.Lock()
        self._last_refresh: dict | None = None

    def _check_home(self) -> None:
        if models_dev._get_cache_path().parent.resolve() != self.home:
            raise ApplicationError("CONFIG_ROOT_MISMATCH", "模型库目录与本机服务不一致", 409)

    def status(self) -> dict:
        self._check_home()
        return {**models_dev.cached_models_dev_status(), "refreshing": self._refresh.locked(),
                "last_refresh": self._last_refresh}

    def refresh(self) -> dict:
        self._check_home()
        if not self._refresh.acquire(blocking=False):
            raise ApplicationError("MODEL_LIBRARY_BUSY", "模型库正在刷新，请稍后查看结果", 409)
        try:
            result = models_dev.refresh_models_dev_cache()
            self._last_refresh = {"ok": bool(result.get("ok")), "finished_at": time.time()}
        except Exception:
            self._last_refresh = {"ok": False, "finished_at": time.time()}
        finally:
            self._refresh.release()
        return self.status()

    def browse(self, query: str = "", provider: str = "", offset: int = 0) -> dict:
        self._check_home()
        if len(query) > 300 or len(provider) > 200 or not 0 <= offset <= 100000:
            raise ApplicationError("INVALID_MODEL_QUERY", "搜索条件不正确", 422)
        registry = models_dev._providers_data(models_dev.cached_models_dev())
        mappings: dict[str, list[dict]] = {}
        for name in PROVIDER_REGISTRY:
            for profile in get_provider_profiles(name):
                entry = {"provider": name, "label": get_setup_provider_label(name),
                         "profile": profile.id, "region": get_setup_profile_label(profile),
                         "callable": profile.callable}
                group = mappings.setdefault(profile.models_dev_provider, [])
                if not any(item["label"] == entry["label"] and item["region"] == entry["region"] for item in group):
                    group.append(entry)
        providers, matches = [], []
        needle = query.strip().casefold()
        for provider_id, entry in sorted(registry.items()):
            label = str(entry.get("name") or provider_id)
            profiles = mappings.get(provider_id, [])
            providers.append({"id": provider_id, "label": label, "profiles": profiles})
            if provider and provider_id != provider:
                continue
            provider_text = " ".join([provider_id, label, *(item["label"] for item in profiles)]).casefold()
            for model_id in models_dev.list_models_dev_provider(provider_id, limit=None, registry=registry):
                raw = entry["models"].get(model_id, {})
                title = str(raw.get("name") or model_id) if isinstance(raw, dict) else model_id
                if needle and needle not in f"{provider_text} {model_id} {title}".casefold():
                    continue
                context = models_dev._extract_context_length(raw) if isinstance(raw, dict) else None
                matches.append({"id": model_id, "name": title, "provider": provider_id,
                                "provider_label": label, "context": context})
        offset = min(offset, max(0, (len(matches) - 1) // 100 * 100))
        return {"status": self.status(), "providers": providers, "models": matches[offset:offset + 100],
                "total": len(matches), "offset": offset, "limit": 100}
