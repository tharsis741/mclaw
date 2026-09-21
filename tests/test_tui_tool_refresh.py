# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from mclaw.application.task_queue import TaskQueue
from mclaw.cli import config as config_io
from mclaw.cli.app import InteractiveChat
from mclaw.cli.runtime.interactive import InteractiveRuntime
from mclaw.cli.runtime.tool_configuration import refresh_interactive_tools
from mclaw.cli.runtime.turns import RuntimeTurnCoordinator, RuntimeTurnHooks


@pytest.fixture
def tui_workspace(tmp_path, monkeypatch):
    home = tmp_path / "state"
    workspace = tmp_path / "含空格 工作区"
    workspace.mkdir()
    monkeypatch.setenv("MCLAW_HOME", str(home))
    config_io.save_config({"model": "configured-default", "toolsets": ["mclaw-required"]})
    return workspace


def make_agent():
    config = config_io.load_config(strict=True)
    config["model"] = "session-selected"
    return SimpleNamespace(
        config=config, enabled_toolsets=config["toolsets"], messages=[],
        _discover_tools=Mock(), mark_prompt_epoch_dirty=Mock(),
        run_conversation=Mock(return_value={"final_response": "你好！", "completed": True}),
    )


@pytest.mark.parametrize("path_type", [str, Path])
def test_refresh_accepts_tui_workspace_and_preserves_project_overrides(tui_workspace, path_type):
    agent = make_agent()
    config_io.save_config({"model": "new-default", "toolsets": ["mclaw-required", "web"]})
    (tui_workspace / ".mclaw.yaml").write_text(
        "tools:\n  disabled: [web_search]\nauxiliary:\n  web_extract:\n    backend: trafilatura\n",
        encoding="utf-8",
    )

    refresh_interactive_tools(agent, path_type(tui_workspace))

    assert agent.enabled_toolsets == ["mclaw-required", "web"]
    assert agent.config["tools"]["disabled"] == ["web_search"]
    assert agent.config["auxiliary"]["web_extract"]["backend"] == "trafilatura"
    assert agent.config["model"] == "session-selected"
    agent._discover_tools.assert_called_once()


@pytest.mark.parametrize("resume_previous_failure", [False, True])
def test_tui_greeting_reaches_agent_and_completes_queue(tui_workspace, resume_previous_failure):
    ui = InteractiveChat.__new__(InteractiveChat)
    ui.workspace_path = str(tui_workspace)
    ui.agent = make_agent()
    ui.config = ui.agent.config
    ui.enabled_toolsets = ui.agent.enabled_toolsets
    ui.runtime = InteractiveRuntime(task_queue=TaskQueue())
    ui._render_response = Mock()
    renderer = Mock()
    ui._get_runtime_renderer = lambda: renderer
    queue = ui.runtime.task_queue
    queue.submit("你好", "session-selected")
    if resume_previous_failure:
        queue.take_next()
        queue.finish({"completed": False, "error": "previous refresh failure"})

    hooks = {field.name: (lambda *_args: None) for field in fields(RuntimeTurnHooks)}
    hooks.update(
        run_turn=ui.chat, get_turn_result=lambda: ui._last_chat_result,
        current_model=lambda: "session-selected", task_input=lambda task: task["content"],
    )
    coordinator = RuntimeTurnCoordinator(ui.runtime, RuntimeTurnHooks(**hooks))
    if resume_previous_failure:
        coordinator.handle_input("/continue")
    coordinator.on_idle()
    coordinator.on_idle()

    ui.agent.run_conversation.assert_called_once()
    assert ui.agent.run_conversation.call_args.args == ("你好",)
    ui._render_response.assert_called_once_with("你好！")
    renderer.error.assert_not_called()
    assert ui._last_chat_result["completed"]
    assert queue.snapshot()["active"] is None
    assert queue.snapshot()["pending"] == []
    assert not queue.snapshot()["paused"]
