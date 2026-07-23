# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""End-to-end Ctrl+C coverage for the interactive credential prompt."""

import json
import threading
from types import SimpleNamespace

from mclaw.cli.app import InteractiveChat
from mclaw.cli.runtime.interactive import InteractiveRuntime
from mclaw.runtime import secrets as secret_service
from mclaw.tools import secret_tool
from mclaw.tools.interrupt import reset_interrupt_event, set_interrupt_event


def test_ctrl_c_during_secret_prompt_cancels_same_turn_and_cannot_persist(
    monkeypatch,
) -> None:
    turn_event = threading.Event()
    prompt_rendered = threading.Event()
    saved = []
    authorized = []
    results = []
    errors = []
    turn_continued = threading.Event()

    class Agent:
        def __init__(self) -> None:
            self.interrupted_event = None
            self.secret_request_callback = None

        def current_turn_cancel_event(self):
            return turn_event

        def interrupt(self) -> None:
            self.interrupted_event = self.current_turn_cancel_event()
            self.interrupted_event.set()

    class Buffer:
        def __init__(self) -> None:
            self.reset_count = 0

        def reset(self) -> None:
            self.reset_count += 1

    buffer = Buffer()
    app = SimpleNamespace(
        current_buffer=buffer,
        invalidate=lambda: None,
        exit=lambda: None,
    )
    agent = Agent()
    chat = InteractiveChat.__new__(InteractiveChat)
    chat.runtime = InteractiveRuntime()
    chat.runtime_state = chat.runtime.session_state
    chat.event_bus = chat.runtime.event_bus
    chat.agent = agent
    chat.pet = None
    chat._app = app
    chat._pending_secret_request = None
    chat._last_rendered_secret_request_signature = None
    chat._render_secret_request = lambda _pending: prompt_rendered.set()
    chat._agent_running = True
    agent.secret_request_callback = chat._secret_request_many_prompt

    monkeypatch.setattr(secret_service, "get_env_value", lambda _name: None)
    monkeypatch.setattr(
        secret_service,
        "save_env_value",
        lambda name, value: saved.append((name, value)),
    )
    monkeypatch.setattr(
        secret_service,
        "authorize",
        lambda scope, names: authorized.append((scope, names)) or list(names),
    )

    def run_turn() -> None:
        token = set_interrupt_event(turn_event)
        try:
            result = json.loads(
                secret_tool._handle_secret_request_many(
                    {
                        "required_for": "tool:demo",
                        "secrets": ["DEMO_API_KEY"],
                    },
                    parent_agent=agent,
                )
            )
            results.append(result)
            if not turn_event.is_set():
                turn_continued.set()
        except BaseException as exc:
            errors.append(exc)
        finally:
            reset_interrupt_event(token)

    worker = threading.Thread(target=run_turn)
    worker.start()
    assert prompt_rendered.wait(1)
    pending = chat._pending_secret_request
    assert pending is not None

    chat._handle_interrupt_key(SimpleNamespace(app=app))
    worker.join(1)

    assert not worker.is_alive()
    assert errors == []
    assert agent.interrupted_event is turn_event
    assert turn_event.is_set()
    assert chat._pending_secret_request is None
    assert buffer.reset_count == 1
    assert results[0]["status"] == "cancelled"
    assert results[0]["interrupted"] is True
    assert saved == []
    assert authorized == []
    assert not turn_continued.is_set()
