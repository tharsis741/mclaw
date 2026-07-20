# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from datetime import datetime
from types import SimpleNamespace

from mclaw.cli.app import InteractiveChat, _compact_status_detail
from mclaw.cli.runtime.events import RuntimeStatus
from mclaw.cli.runtime.interactive import InteractiveRuntime
from mclaw.cli.runtime.session import RuntimeSessionState
from mclaw.cli.tui.renderers.status import StatusRenderer, _STATUS_STYLES
from mclaw.pet.events import (
    PET_STATE_BY_RUNTIME_STATUS,
    PetState,
    pet_state_for_runtime_status,
)


class RecordingPet:
    def __init__(self) -> None:
        self.events = []

    def emit(self, event_type, **kwargs) -> bool:
        self.events.append((event_type, kwargs))
        return True


def make_chat_with_pet() -> tuple[InteractiveChat, RecordingPet]:
    chat = InteractiveChat.__new__(InteractiveChat)
    chat.runtime = InteractiveRuntime()
    chat.runtime_state = chat.runtime.session_state
    chat.event_bus = chat.runtime.event_bus
    chat.config = {"display": {"pet": {"notify": {"tool_finished": True}}}}
    chat._app = None
    pet = RecordingPet()
    chat.pet = pet
    return chat, pet


def test_runtime_status_follows_stream_and_tool_round_trip() -> None:
    state = RuntimeSessionState()

    state.begin_turn()
    assert state.status == RuntimeStatus.REQUESTING
    assert state.detail == "Preparing request"

    assert state.append_stream_delta("hello") == 5
    assert state.status == RuntimeStatus.STREAMING
    assert state.detail == "Streaming 5 chars"

    state.begin_tools("web_search")
    assert state.status == RuntimeStatus.TOOLS
    assert state.active_tools == {"web_search"}

    state.finish_tools()
    assert state.status == RuntimeStatus.REQUESTING
    assert state.detail == "Waiting for response"
    assert not state.active_tools


def test_finish_turn_preserves_terminal_outcomes() -> None:
    state = RuntimeSessionState()
    state.begin_turn()
    state.fail_turn("provider failed")
    state.finish_turn(state.last_result)

    assert state.status == RuntimeStatus.ERROR
    assert state.detail == "provider failed"

    state.begin_turn()
    state.finish_turn({"interrupted": True, "completed": False})
    assert state.status == RuntimeStatus.INTERRUPTED

    state.begin_turn()
    state.finish_turn({"pending_skill_import_confirmation": True})
    assert state.status == RuntimeStatus.WAITING_FOR_USER


def test_every_runtime_status_has_one_pet_mapping() -> None:
    assert set(_STATUS_STYLES) == set(RuntimeStatus)
    assert set(PET_STATE_BY_RUNTIME_STATUS) == {status.value for status in RuntimeStatus}
    assert pet_state_for_runtime_status(RuntimeStatus.REQUESTING) == PetState.RUNNING
    assert pet_state_for_runtime_status(RuntimeStatus.STREAMING) == PetState.TYPING
    assert pet_state_for_runtime_status(RuntimeStatus.TOOLS) == PetState.READING
    assert pet_state_for_runtime_status(RuntimeStatus.DELEGATING) == PetState.CARRYING
    assert pet_state_for_runtime_status(RuntimeStatus.AGGREGATING) == PetState.REVIEW
    assert pet_state_for_runtime_status(RuntimeStatus.WAITING_FOR_USER) == PetState.WAITING
    assert pet_state_for_runtime_status(RuntimeStatus.DONE) == PetState.WAVING
    assert pet_state_for_runtime_status(RuntimeStatus.INTERRUPTED) == PetState.FAILED
    assert pet_state_for_runtime_status(RuntimeStatus.ERROR) == PetState.FAILED


def test_tui_callbacks_map_the_canonical_status_to_pet() -> None:
    chat, pet = make_chat_with_pet()
    chat.runtime_state.begin_turn()

    chat._on_stream_delta("hello")
    chat._on_tool_start("web_search", {})
    chat._on_tool_end()

    assert chat.runtime_state.status == RuntimeStatus.REQUESTING
    assert [event[1]["state"] for event in pet.events] == [
        PetState.TYPING,
        PetState.READING,
        PetState.RUNNING,
    ]


def test_status_detail_does_not_replace_the_canonical_lifecycle() -> None:
    chat, pet = make_chat_with_pet()
    chat.runtime_state.set_status(RuntimeStatus.AGGREGATING)

    chat._on_status("Compressing context...")

    assert chat.runtime_state.status == RuntimeStatus.AGGREGATING
    assert chat.runtime_state.detail == "Compressing context"
    assert pet.events[-1][1]["state"] == PetState.REVIEW


def test_all_agent_status_messages_are_self_contained() -> None:
    cases = {
        "Refreshing context...": "Refreshing context",
        "Pruning tool results...": "Pruning tool results",
        "Compressing context...": "Compressing context",
        "Waiting for model...": "Waiting for model",
        "Retrying in 5s...": "Retrying in 5s",
        "Reviewing memory...": "Reviewing memory",
        "Reviewing skills...": "Reviewing skills",
        "Reviewing memory and skills...": "Reviewing memory and skills",
        "Running 1 tool(s)...": "Running 1 tool",
        "Running 3 tool(s)...": "Running 3 tools",
    }

    for message, expected in cases.items():
        assert _compact_status_detail(message) == expected

    assert _compact_status_detail("未知状态...") == "未知状态"


def test_status_bar_shows_only_the_most_specific_live_status() -> None:
    state = RuntimeSessionState(
        status=RuntimeStatus.REQUESTING,
        detail="Waiting for model",
    )
    owner = SimpleNamespace(
        _status_bar_visible=True,
        agent=None,
        model="test-model",
        _agent_running=True,
        _turn_start_at=datetime.now(),
        session_start=datetime.now(),
        _last_turn_duration=0,
        _project_name="",
        _input_mode="",
        _asr_status_text="",
        _spinner_idx=0,
        runtime_state=state,
        subtask_manager=None,
    )

    rendered = "".join(
        text for _style, text in StatusRenderer().build_status_fragments(owner)
    )
    assert "Waiting for model" in rendered
    assert "Requesting" not in rendered

    state.set_status(RuntimeStatus.TOOLS, "Running 3 tools")
    state.active_tools.add("web_search")
    rendered = "".join(
        text for _style, text in StatusRenderer().build_status_fragments(owner)
    )
    assert "web_search" in rendered
    assert "Tools" not in rendered
    assert "Running 3 tools" not in rendered
