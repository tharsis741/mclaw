# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import signal

from mclaw.pet import controller
from mclaw.pet import runtime_qt


def test_sidecar_ignores_sigint_before_starting_qt(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(controller.signal, "signal", lambda *args: calls.append(("signal", args)))
    monkeypatch.setattr(runtime_qt, "run_pet", lambda *args: calls.append(("run_pet", args)))

    controller._run_sidecar("events", "commands", {"asset": "robot-dark"})

    assert calls == [
        ("signal", (signal.SIGINT, signal.SIG_IGN)),
        ("run_pet", ("events", "commands", {"asset": "robot-dark"})),
    ]
