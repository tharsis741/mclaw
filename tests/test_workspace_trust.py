# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from mclaw.cli.runtime import workspace_trust


def test_workspace_trust_prompts_once_and_persists(monkeypatch, tmp_path) -> None:
    saved = {}
    prompts = []
    monkeypatch.setattr(workspace_trust, "load_config", lambda **_kwargs: saved)
    monkeypatch.setattr(workspace_trust, "save_config", lambda config: saved.update(config))

    assert workspace_trust.ensure_workspace_trusted(
        tmp_path,
        prompt=lambda path: prompts.append(path) or True,
    )
    assert workspace_trust.ensure_workspace_trusted(
        tmp_path,
        prompt=lambda _path: False,
    )
    assert prompts == [workspace_trust.normalize_workspace_path(tmp_path)]
    assert saved["security"]["trusted_workspaces"] == prompts
