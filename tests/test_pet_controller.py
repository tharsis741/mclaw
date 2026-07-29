# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import signal
from types import SimpleNamespace

from mclaw.pet import controller
from mclaw.pet import runtime_qt
from mclaw.pet.config import PetConfig
from mclaw.pet.events import PetEvent, PetState
from mclaw.runtime.features import FeatureState, runtime_features
from mclaw.runtime.manager import RuntimeManager


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


def test_controller_honors_runtime_pet_capability(monkeypatch) -> None:
    features = runtime_features(
        pet=FeatureState.AVAILABLE_WITH_CONFIG,
        reasons={"pet": "No display session"},
    )
    monkeypatch.setattr(
        RuntimeManager,
        "current",
        classmethod(lambda _cls: SimpleNamespace(features=features)),
    )
    pet = controller.PetController(PetConfig(enabled=True))

    assert not pet.start()
    assert pet.last_error == "No display session"
