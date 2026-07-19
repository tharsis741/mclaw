# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import signal

from mclaw.pet import controller
from mclaw.pet import runtime_qt
from mclaw.pet.events import PetEvent, PetState


def test_sidecar_ignores_sigint_before_starting_qt(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(controller.signal, "signal", lambda *args: calls.append(("signal", args)))
    monkeypatch.setattr(runtime_qt, "run_pet", lambda *args: calls.append(("run_pet", args)))

    controller._run_sidecar("events", "commands", {"asset": "robot-dark"})

    assert calls == [
        ("signal", (signal.SIGINT, signal.SIG_IGN)),
        ("run_pet", ("events", "commands", {"asset": "robot-dark"})),
    ]


def test_sidecar_uses_explicit_state_instead_of_inferring_from_event_type() -> None:
    window = runtime_qt._PetWindow.__new__(runtime_qt._PetWindow)
    states = []
    window._mark_activity = lambda: None
    window.set_state = states.append
    window._set_bubble = lambda _event: None

    window.handle_event(PetEvent(type="tool_started"))
    window.handle_event(PetEvent(type="status_changed", state=PetState.READING.value))

    assert states == [PetState.READING.value]
