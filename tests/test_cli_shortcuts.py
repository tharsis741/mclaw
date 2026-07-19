# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from types import SimpleNamespace

import mclaw.cli.app as cli_app


def test_shift_enter_falls_back_to_windows_key_state(monkeypatch) -> None:
    event = SimpleNamespace(key_sequence=[SimpleNamespace(data="\r")])

    monkeypatch.setattr(cli_app, "_windows_shift_pressed", lambda: True)
    assert cli_app._is_shift_enter(event)

    monkeypatch.setattr(cli_app, "_windows_shift_pressed", lambda: False)
    assert not cli_app._is_shift_enter(event)
