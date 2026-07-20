# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from mclaw.cli.runtime.results import RuntimeTurnResultCoordinator, RuntimeTurnResultHooks


def test_interrupted_turn_does_not_render_partial_stream_as_a_reply() -> None:
    events: list[tuple[str, str]] = []
    coordinator = RuntimeTurnResultCoordinator(
        RuntimeTurnResultHooks(
            stream_text=lambda: "partial model output",
            stream_started=lambda: True,
            render_response=lambda text: events.append(("response", text)),
            render_interrupted=lambda: events.append(("interrupted", "")),
            emit_waiting_for_skill_confirmation=lambda: None,
            remember_pending_skill_confirmation=lambda _data: None,
            render_skill_confirmation=lambda _data: None,
            handle_pending_delegate=lambda _result: False,
            log_skill_confirmation_pending=lambda _data: None,
        )
    )

    coordinator.handle_result({"interrupted": True, "completed": False})

    assert events == [("interrupted", "")]
