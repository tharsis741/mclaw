# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from mclaw.cli.runtime.results import RuntimeTurnResultCoordinator, RuntimeTurnResultHooks


def test_interrupted_turn_renders_nothing_in_the_transcript() -> None:
    events: list[tuple[str, str]] = []
    coordinator = RuntimeTurnResultCoordinator(
        RuntimeTurnResultHooks(
            stream_text=lambda: "partial model output",
            stream_started=lambda: True,
            render_response=lambda text: events.append(("response", text)),
            emit_waiting_for_skill_confirmation=lambda: None,
            remember_pending_skill_confirmation=lambda _data: None,
            render_skill_confirmation=lambda _data: None,
            handle_pending_delegate=lambda _result: False,
            log_skill_confirmation_pending=lambda _data: None,
        )
    )

    coordinator.handle_result({"interrupted": True, "completed": False})

    assert events == []


def test_tool_abort_renders_its_reason_instead_of_user_interrupt() -> None:
    events: list[tuple[str, str]] = []
    coordinator = RuntimeTurnResultCoordinator(
        RuntimeTurnResultHooks(
            stream_text=lambda: "partial model output",
            stream_started=lambda: True,
            render_response=lambda text: events.append(("response", text)),
            emit_waiting_for_skill_confirmation=lambda: None,
            remember_pending_skill_confirmation=lambda _data: None,
            render_skill_confirmation=lambda _data: None,
            handle_pending_delegate=lambda _result: False,
            log_skill_confirmation_pending=lambda _data: None,
            render_abort=lambda message: events.append(("abort", message)),
        )
    )

    coordinator.handle_result(
        {
            "interrupted": True,
            "abort_reason": "tool_completion_unknown",
            "abort_message": "restart required",
            "completed": False,
        }
    )

    assert events == [("abort", "restart required")]
