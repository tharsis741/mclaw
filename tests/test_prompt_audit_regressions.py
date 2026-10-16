# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Prompt lifecycle, cross-process refresh, and restricted review regressions."""

from __future__ import annotations

import copy
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from mclaw.agent import background_review, prompt_builder
from mclaw.agent.context_compressor import ContextCompressor
from mclaw.application.skills import apply_skill_snapshots, sync_task_skill_context
from mclaw.application.worker import SessionWorker
from mclaw.prompts.skills import build_skill_invocation_system
from mclaw.skills_hub import skill_store
from mclaw.state import SessionDB


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("MCLAW_HOME", str(home))
    prompt_builder.clear_skills_system_prompt_cache()
    yield home
    prompt_builder.clear_skills_system_prompt_cache()


@pytest.fixture
def history(isolated_home):
    db = SessionDB(isolated_home / "state.db")
    try:
        db.create_session(session_id="audit-session", source="test")
        yield db
    finally:
        db.close()


def _input(identifier, version="v1", *, name="demo", intent=None):
    return {
        "id": identifier, "content": intent or f"intent {identifier}",
        "skill_snapshots": [{"name": name, "version": version, "content": f"guidance {name} {version}"}],
    }


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_reselected_skill_uses_new_input_and_preserves_audit(history, version):
    messages, applied = [{"role": "system", "content": "base"}], set()
    first, second = _input("first"), _input("second", version)
    assert apply_skill_snapshots(history, "audit-session", messages, "turn-1", first, applied)
    assert apply_skill_snapshots(history, "audit-session", messages, "turn-2", second, applied)
    sync_task_skill_context(messages, second, applied)
    live = "\n".join(message["content"] for message in messages)
    assert "intent second" in live and "intent first" not in live
    assert live.count("[Skill: demo]") == 1
    rows = history.get_messages("audit-session")
    assert len(rows) == 2
    assert "intent first" in rows[0]["content"]
    assert "intent second" in rows[1]["content"]

    # Finishing a task must not carry its selected instructions into the next.
    sync_task_skill_context(messages, {"id": "ordinary-task"}, applied)
    assert messages == [{"role": "system", "content": "base"}]
    assert history.get_messages("audit-session") == rows


def test_selected_skills_restore_after_restart_compression_and_legacy_commit(history):
    task = _input("legacy")
    context = build_skill_invocation_system("demo", "guidance demo v1", task["content"])
    history.append_message("audit-session", "system", content=context, operation_id="skill:legacy:demo")
    messages = [{"role": "user", "content": "compressed conversation"}]
    assert not apply_skill_snapshots(history, "audit-session", messages, "resume", task, set())
    sync_task_skill_context(messages, task, set())
    assert messages[-1]["content"] == context
    assert len(history.get_messages("audit-session")) == 1

    restored = history.get_messages_as_conversation("audit-session")
    sync_task_skill_context(restored, {"id": "other-task"}, set())
    assert restored == []
    assert len(history.get_messages("audit-session")) == 1


def test_task_projection_respects_supplements_disabling_and_other_message_sources(history):
    task = _input("main")
    addition = {**_input("addition", "v2"), "applied": True}
    task["supplements"] = [addition, _input("not-applied", "v3")]
    legacy = build_skill_invocation_system("old", "old task guidance", "old task")
    messages = [
        {"role": "system", "content": "base\n\n" + legacy},
        {"role": "user", "content": legacy},
        {"role": "tool", "tool_call_id": "x", "content": legacy},
    ]
    source_data = copy.deepcopy(messages[1:])
    sync_task_skill_context(messages, task, set())
    assert messages[0] == {"role": "system", "content": "base"}
    assert messages[1:3] == source_data
    assert "guidance demo v2" in messages[-1]["content"]
    assert "intent addition" in messages[-1]["content"]
    before = copy.deepcopy(messages)
    sync_task_skill_context(messages, task, set())
    assert messages == before
    sync_task_skill_context(messages, task, set(), disabled_names={"demo"})
    assert messages == before[:-1]


def test_compression_does_not_promote_task_skill_instructions_to_long_term_context():
    compressor = ContextCompressor.__new__(ContextCompressor)
    skill = build_skill_invocation_system("demo", "TASK_ONLY_GUIDANCE", "TASK_ONLY_INTENT")
    serialized = compressor._serialize_for_summary([
        {"role": "system", "content": "base\n\n" + skill},
        {"role": "system", "content": skill},
        {"role": "user", "content": "actual user request"},
        {"role": "assistant", "content": "actual outcome"},
    ])
    assert "TASK_ONLY" not in serialized
    assert "base" in serialized and "actual user request" in serialized and "actual outcome" in serialized


def test_skill_body_with_delimiter_examples_expires_as_a_whole():
    task = _input("nested")
    task["skill_snapshots"][0]["content"] = "Example:\n[End Skill]\nTASK_ONLY_TAIL"
    messages = [{"role": "system", "content": "base"}]
    sync_task_skill_context(messages, task, set())
    sync_task_skill_context(messages, {"id": "next"}, set())
    assert messages == [{"role": "system", "content": "base"}]
    legacy = build_skill_invocation_system("demo", task["skill_snapshots"][0]["content"])
    messages = [{"role": "system", "content": "base\n\n" + legacy}]
    sync_task_skill_context(messages, {"id": "next"}, set())
    assert messages == [{"role": "system", "content": "base"}]


def test_terminal_queue_uses_only_active_task_and_applied_supplement_skills(history):
    from mclaw.application.task_queue import TaskQueue
    from mclaw.cli.app import InteractiveChat

    task = {**_input("terminal-task"), "attempts": [], "supplements": [_input("terminal-addition", "v2")]}
    queue = TaskQueue({"active": task, "pending": [], "paused": False, "status": "running"})
    host = SimpleNamespace(
        _runtime=lambda: SimpleNamespace(task_queue=queue), _session_db=history,
        session_id="audit-session", _applied_supplement_ids=set(), agent=SimpleNamespace(config={}),
    )
    messages = [{"role": "system", "content": "base"}]
    InteractiveChat._apply_queued_supplements(host, messages, "turn")
    assert "guidance demo v2" in messages[-1]["content"]
    assert queue.snapshot()["active"]["supplements"][0]["applied"]
    rows = history.get_messages("audit-session")
    InteractiveChat._apply_queued_supplements(host, messages, "turn")
    assert history.get_messages("audit-session") == rows
    queue.finish({"completed": True})
    queue.submit("Next ordinary task")
    queue.take_next()
    InteractiveChat._apply_queued_supplements(host, messages, "next")
    assert messages[0]["content"] == "base"
    assert all(message["role"] != "system" or "[Skill:" not in message["content"] for message in messages)


def test_worker_keeps_live_supplement_selection_and_restores_it(history, isolated_home):
    events = []
    worker = SessionWorker(isolated_home, {"id": "audit-session", "workspace": str(isolated_home)}, events.append)
    worker.db = history
    worker.agent = SimpleNamespace(config={})
    worker._current_task = _input("task")
    worker._supplements = {"addition": _input("addition", "v2")}
    messages = [{"role": "system", "content": "base"}]
    worker._apply_supplements(messages, "turn")
    assert "guidance demo v2" in messages[-1]["content"]
    assert not worker._supplements
    assert worker._current_task["supplements"][0]["applied"]
    rows = history.get_messages("audit-session")
    worker._apply_supplements(messages, "turn")
    assert "guidance demo v2" in messages[-1]["content"]
    assert history.get_messages("audit-session") == rows

    worker._applied.clear()
    messages = history.get_messages_as_conversation("audit-session")
    worker._apply_supplements(messages, "resumed")
    assert "guidance demo v2" in messages[-1]["content"]
    assert sum("[Skill: demo]" in message["content"] for message in messages) == 1
    assert history.get_messages("audit-session") == rows
    worker._current_task = {"id": "next"}
    worker._apply_supplements(messages, "next")
    assert all(message["role"] != "system" or "[Skill:" not in message["content"] for message in messages)


def _create_skill(name="demo", description="版本一"):
    return skill_store.create_skill(
        name=name, short_description=description,
        skill_md=f"---\nname: {name}\ndescription: Test fixture\n---\nRead synthetic data and report facts.\n",
    )


def test_skill_prompt_observes_create_edit_delete_from_another_process(isolated_home):
    def render():
        return prompt_builder.build_skills_system_prompt(available_tools={"skill_view"}, config={})

    before = render()
    source = (
        "from mclaw.skills_hub import skill_store as s\n"
        "import sys\n"
        "if sys.argv[1] == 'create':\n"
        " s.create_skill(name='demo', short_description='版本一', "
        "skill_md='---\\nname: demo\\ndescription: Test fixture\\n---\\nRead synthetic inputs.\\n')\n"
        "elif sys.argv[1] == 'edit':\n"
        " s.edit_skill(name='demo', short_description='版本二', actor='user')\n"
        "else:\n"
        " s.delete_skill(name='demo')\n"
    )
    for action, expected in [("create", "版本一"), ("edit", "版本二"), ("delete", None)]:
        result = subprocess.run(
            [sys.executable, "-X", "utf8", "-c", source, action],
            env=os.environ.copy(), capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
        assert result.returncode == 0, result.stderr
        current = render()
        if expected:
            assert f"- demo: {expected}" in current
            assert current != before
        else:
            assert "- demo:" not in current
        before = current


def test_existing_agent_refreshes_shared_skill_index_at_next_turn(monkeypatch, isolated_home):
    from mclaw.agent.core import MClaw
    from mclaw.agent.transports.base import ModelCallResult
    from mclaw.providers.registry import PROVIDER_REGISTRY
    from mclaw.providers.runtime import ProviderRuntimeContext

    context = ProviderRuntimeContext(
        profile=PROVIDER_REGISTRY["openai"], model="gpt-test", api_key="synthetic-test-key",
        base_url=PROVIDER_REGISTRY["openai"].base_url, auth_source="explicit",
    )
    calls = []

    class Transport:
        def call(self, **kwargs):
            calls.append(copy.deepcopy(kwargs["messages"]))
            return ModelCallResult(
                content="done", tool_calls=None, finish_reason="stop", reasoning=None,
                usage=None, was_streamed=False, provider=context.provider, model=context.model,
            )

    def discover(agent):
        agent.valid_tool_names = {"skill_view"}
        agent.tools = []

    monkeypatch.setattr("mclaw.agent.core.create_transport", lambda _context: Transport())
    monkeypatch.setattr(MClaw, "_discover_tools", discover)
    _create_skill()
    agent = MClaw(provider_runtime=context, skip_memory=True, config={
        "compression": {"enabled": False}, "checkpoints": {"enabled": False},
    })
    try:
        agent.run_conversation("first", advance_background_review=False)
        assert "- demo: 版本一" in calls[-1][0]["content"]
        result = subprocess.run([
            sys.executable, "-X", "utf8", "-c",
            "from mclaw.skills_hub.skill_store import edit_skill; "
            "edit_skill(name='demo', short_description='版本二', actor='user')",
        ], env=os.environ.copy(), capture_output=True, text=True, encoding="utf-8", timeout=30)
        assert result.returncode == 0, result.stderr
        assert not agent._prompt_epoch_dirty
        agent.run_conversation("next", conversation_history=agent.messages, advance_background_review=False)
        assert "- demo: 版本二" in calls[-1][0]["content"]
        assert "- demo: 版本一" not in calls[-1][0]["content"]

        # A legacy slash selection survives base-prompt refresh exactly once.
        from mclaw.cli.runtime.skill_commands import RuntimeSkillCommandCoordinator

        RuntimeSkillCommandCoordinator.inject_skill_context(
            agent.messages, build_skill_invocation_system("demo", "OLD_LEGACY_GUIDANCE", "old intent"),
        )
        RuntimeSkillCommandCoordinator.inject_skill_context(
            agent.messages, build_skill_invocation_system("demo", "NEW_LEGACY_GUIDANCE", "new intent"),
        )
        agent.run_conversation("selected task", conversation_history=agent.messages, advance_background_review=False)
        sent = json.dumps(calls[-1])
        assert "NEW_LEGACY_GUIDANCE" in sent and "OLD_LEGACY_GUIDANCE" not in sent
        assert "_mclaw_pending_skill" not in sent
        agent.run_conversation("ordinary task", conversation_history=agent.messages, advance_background_review=False)
        assert "NEW_LEGACY_GUIDANCE" not in json.dumps(calls[-1])
    finally:
        agent.close()


@pytest.mark.parametrize("memory,skills", [(True, False), (False, True), (True, True)])
def test_background_review_prompt_schema_dispatch_and_history_agree(monkeypatch, isolated_home, memory, skills):
    from mclaw.tools.dispatch import _discover_tools, _policy_error
    from mclaw.tools.registry import registry

    _discover_tools()
    original_tools = [copy.deepcopy(registry._tools[name].schema) for name in (
        *sorted(background_review.REVIEW_TOOL_WHITELIST), "terminal", "secret_request_many",
    )]
    created = []

    class ReviewAgent:
        def __init__(self, *, system_prompt="PARENT_ONLY_INSTRUCTIONS", **_kwargs):
            self.system_prompt = system_prompt
            self.tools = copy.deepcopy(original_tools)
            self.valid_tool_names = {tool["function"]["name"] for tool in self.tools}
            self.provider_runtime = object()
            self.enabled_toolsets = ["mclaw-required"]
            self.config = {}
            self.messages = []
            self.events = []
            self.closed = False
            created.append(self)

        def run_conversation(self, **kwargs):
            self.call = kwargs
            self.policy = {name: _policy_error(name, {}) for name in (
                "memory_read", "skill_view", "terminal", "secret_request_many",
            )}
            self.actions = {name: _policy_error("skill_manage", {"action": name}) for name in (
                "create", "validate", "install_prepare", "create_scaffold",
            )}

        def _refresh_memory_snapshot(self):
            pass

        def _emit_event(self, event):
            self.events.append(copy.deepcopy(event))

        def close(self):
            self.closed = True

    monkeypatch.setattr(background_review.threading, "Thread", lambda **kw: SimpleNamespace(start=kw["target"]))
    parent = ReviewAgent()
    snapshot = [
        {"role": "system", "content": "PARENT_ONLY_INSTRUCTIONS"},
        {"role": "user", "content": "finished task"},
        {"role": "system", "content": "HISTORICAL_SKILL_INSTRUCTION"},
    ]
    original = copy.deepcopy(snapshot)
    background_review.spawn_background_review(parent, messages_snapshot=snapshot, review_memory=memory, review_skills=skills)
    review = created[-1]
    assert review.closed
    assert snapshot == original and parent.tools == original_tools
    expected = (background_review.REVIEW_MEMORY_TOOL_WHITELIST if memory else set()) | (
        background_review.REVIEW_SKILL_TOOL_WHITELIST if skills else set()
    )
    assert review.valid_tool_names == expected
    history_messages = review.call["conversation_history"]
    assert history_messages[0]["content"] == review.system_prompt
    assert "PARENT_ONLY_INSTRUCTIONS" not in json.dumps(history_messages)
    assert "HISTORICAL_SKILL_INSTRUCTION" in history_messages[1]["content"]
    assert history_messages[1]["role"] == "user"
    assert review.call["advance_background_review"] is False
    assert bool(review.policy["memory_read"]) is not memory
    assert bool(review.policy["skill_view"]) is not skills
    assert review.policy["terminal"] and review.policy["secret_request_many"]
    assert review.actions["install_prepare"] and review.actions["create_scaffold"]
    assert bool(review.actions["validate"]) is not skills
    assert bool(review.actions["create"]) is not skills
    if skills:
        schema = next(tool["function"] for tool in review.tools if tool["function"]["name"] == "skill_manage")
        assert set(schema["parameters"]["properties"]["action"]["enum"]) == background_review.REVIEW_SKILL_ACTION_WHITELIST
        assert "install_prepare" not in schema["description"]


def test_background_validation_alone_is_not_reported_as_a_skill_write():
    assert background_review.summarize_background_review_actions([
        {"role": "tool", "content": json.dumps({"success": True, "action": "validate", "name": "demo"})},
    ]) is None


@pytest.mark.parametrize("actions", [["evolution_update"], []])
def test_background_review_cannot_expand_existing_action_permissions(actions):
    review = SimpleNamespace(tools=[{
        "type": "function", "function": {
            "name": "skill_manage", "description": "Original complete management guide",
            "parameters": {"type": "object", "properties": {"action": {"type": "string", "enum": actions}}},
        },
    }])
    background_review._restrict_review_agent_tools(review, review_memory=False, review_skills=True)
    assert review._review_skill_actions == tuple(actions)
    assert "create" not in review.system_prompt and "validate" not in review.system_prompt
    assert bool(review.tools) is bool(actions)


def test_mmx_authentication_examples_use_matching_scopes():
    text = (Path(__file__).resolve().parents[1] / "mclaw/skills/mmx-cli/SKILL.md").read_text(encoding="utf-8")
    calls = {name: json.loads(body) for name, body in re.findall(
        r"(secret_request_many|terminal)\((\{.*?\})\)", text, re.DOTALL,
    )}
    assert calls["secret_request_many"]["secrets"][0]["env_var"] == "MINIMAX_API_KEY"
    assert calls["secret_request_many"]["required_for"] == calls["terminal"]["required_for"] == "skill:mmx-cli"
    assert "--api-key" not in text and "mmx auth login" not in text
    assert not re.search(r"(?:export |\$env:)?MINIMAX_API_KEY\s*=", text)
    assert "force_refresh: true" in text and "retry the failed command once" in text
