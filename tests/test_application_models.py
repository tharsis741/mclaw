# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from types import SimpleNamespace

from mclaw.application.models import configured_models


def test_model_choices_include_configured_provider_labels_without_credentials(monkeypatch):
    from mclaw.cli import config
    from mclaw.providers import registry, resolver

    monkeypatch.setattr(config, "load_config", lambda **kwargs: {
        "providers": {"company": {"model": "shared-model"}},
    })
    monkeypatch.setattr(registry, "PROVIDER_REGISTRY", {})
    def resolve(*, provider, model, config):
        return SimpleNamespace(provider=provider, model=model,
                               profile=SimpleNamespace(display_name="公司模型服务" if provider == "company" else "DeepSeek"),
                               api_key="private-test-credential", base_url="https://private.test")
    monkeypatch.setattr(resolver, "resolve_provider_runtime_context", resolve)

    choices = configured_models({"provider": "deepseek", "model": "shared-model"})
    assert choices == [
        {"id": "deepseek::shared-model", "label": "shared-model", "provider": "deepseek", "provider_label": "DeepSeek"},
        {"id": "company::shared-model", "label": "shared-model", "provider": "company", "provider_label": "公司模型服务"},
    ]
