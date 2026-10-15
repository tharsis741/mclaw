# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""F10 regressions with real conversation dispatch and temporary Skill storage."""

from __future__ import annotations

import copy
import json
import sys
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from mclaw.agent import background_review
from mclaw.agent.core import MClaw
from mclaw.agent.transports.base import ModelCallError, ModelCallResult
from mclaw.providers.registry import PROVIDER_REGISTRY
from mclaw.providers.runtime import ProviderRuntimeContext
from mclaw.skills_hub import skill_store
from mclaw.tools.skill_tools.manage_tool import skill_manage


def skill_text(name="review-demo"):
    return f"---\nname: {name}\ndescription: Reusable verification workflow.\n---\n\n# Verify\n\nRead the input, check the result, and report evidence.\n"


def skill_call(action, **kwargs):
    return {
        "id": f"skill-{action}", "type": "function",
        "function": {"name": "skill_manage", "arguments": json.dumps({
            "action": action, "name": "review-demo", **kwargs,
        })},
    }


class Transport:
    def __init__(self, context, responses=()):
        self.context = context
        self.responses = deque(responses)
        self.calls = []
        self.closed = False

    def call(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs["messages"]))
        response = self.responses.popleft() if self.responses else None
        if callable(response):
            response = response()
        if isinstance(response, Exception):
            raise response
        return ModelCallResult(
            content="" if response else "done", tool_calls=response,
            finish_reason="tool_calls" if response else "stop", reasoning=None,
            usage=None, was_streamed=False, provider=self.context.provider, model=self.context.model,
        )

    def close(self):
        self.closed = True


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.setenv("MCLAW_HOME", str(tmp_path / "home"))
    context = ProviderRuntimeContext(
        profile=PROVIDER_REGISTRY["openai"], model="synthetic-f10",
        api_key="synthetic-test-key", base_url="https://example.test/v1", auth_source="explicit",
    )
    pending = deque()
    scripts = deque()
    transports = []
    agents = []

    class DeferredThread:
        def __init__(self, *, target, **kwargs):
            self.target = target

        def start(self):
            pending.append(self.target)

    def create_transport(runtime):
        transport = Transport(runtime, scripts.popleft() if scripts else ())
        transports.append(transport)
        return transport

    monkeypatch.setattr("mclaw.agent.core.create_transport", create_transport)
    # Replace only the review module's thread factory, not tool worker threads.
    monkeypatch.setattr(background_review, "threading", SimpleNamespace(Thread=DeferredThread))

    def create(interval=10, **config):
        agent = MClaw(
            provider_runtime=context, platform="web", workspace=str(tmp_path),
            config={
                "compression": {"enabled": False}, "checkpoints": {"enabled": False},
                "memory": {"memory_review_round": 0},
                "skills": {"evolution_review_round": interval}, **config,
            },
        )
        agents.append(agent)
        return agent, transports[-1]

    def run_review(*responses):
        scripts.append(responses)
        pending.popleft()()
        return transports[-1]

    yield SimpleNamespace(
        create=create, pending=pending, scripts=scripts, transports=transports,
        run_review=run_review, thread_factory=DeferredThread,
    )
    for agent in agents:
        agent.close()


def turn(agent, **kwargs):
    return agent.run_conversation("continue verification", conversation_history=agent.messages, **kwargs)


def seed_skill():
    return skill_store.create_skill(
        name="review-demo", skill_md=skill_text(), short_description="可复用的验证流程",
    )


def test_main_prompt_allows_grounded_automatic_creation_without_scheduler_details(harness):
    agent, transport = harness.create()
    turn(agent)
    prompt = transport.calls[0][0]["content"]
    creation = prompt.split("## 创建 Skill", 1)[1].split("## ", 1)[0]
    maintenance = prompt.split("## Skill 沉淀", 1)[1].split("## ", 1)[0]
    assert "从对话中明确发现" in creation
    assert "自动沉淀时" in creation and "暂缓创建" in creation
    assert "用户明确要求创建时" in creation
    assert "自动创建" in maintenance
    assert "后台" not in creation + maintenance
    assert "计数" not in creation + maintenance


@pytest.mark.parametrize("interval,rounds,expected", [(0, 12, []), (1, 4, [1, 2, 3, 4]), (2, 5, [2, 4]), (10, 21, [10, 20])])
def test_periodic_review_repeats_every_n_completed_turns_when_nothing_saved(harness, interval, rounds, expected):
    agent, _ = harness.create(interval)
    reviewed = []
    for index in range(1, rounds + 1):
        assert turn(agent)["completed"]
        if harness.pending:
            reviewed.append(index)
            harness.run_review()
    assert reviewed == expected


@pytest.mark.parametrize("interval", [0, 10])
def test_foreground_save_resets_immediately_even_if_followup_model_call_fails(harness, interval):
    agent, transport = harness.create(interval)
    agent._turns_since_evolution_review = 6
    transport.responses.extend([
        [skill_call("create", skill_md=skill_text(), short_description="可复用的验证流程")],
        ModelCallError("test followup failure", provider="openai", model="synthetic-f10"),
    ])
    result = turn(agent)
    assert not result["completed"]
    assert skill_store.get_skill_record("review-demo")
    assert agent._turns_since_evolution_review == 0


def test_scaffold_refreshes_prompt_but_only_completed_content_resets_count(harness):
    agent, transport = harness.create()
    agent._turns_since_evolution_review = 3
    transport.responses.append([skill_call("create_scaffold", short_description="可复用的验证流程")])
    assert turn(agent)["skills_changed"]
    assert agent._turns_since_evolution_review == 4
    transport.responses.append([skill_call("edit", skill_md=skill_text())])
    assert turn(agent)["completed"]
    assert agent._turns_since_evolution_review == 0
    assert "review-demo" in transport.calls[-1][0]["content"]


@pytest.mark.parametrize("fails_after_save", [False, True])
def test_background_save_resets_owning_session_even_if_review_later_fails(harness, fails_after_save):
    first, _ = harness.create(2)
    second, _ = harness.create(10)
    second._turns_since_evolution_review = 7
    first._turns_since_memory_review = 5
    for _ in range(3):
        turn(first)
    assert first._turns_since_evolution_review == 1
    script = [[skill_call("create", skill_md=skill_text(), short_description="可复用的验证流程")]]
    if fails_after_save:
        script.append(ModelCallError("test review failure", provider="openai", model="synthetic-f10"))
    review_transport = harness.run_review(*script)
    assert skill_store.get_skill_record("review-demo")
    assert first._turns_since_evolution_review == 0
    assert first._turns_since_memory_review == 5
    assert second._turns_since_evolution_review == 7
    assert review_transport.closed
    turn(first)
    assert first._turns_since_evolution_review == 1


@pytest.mark.parametrize("saves", [False, True])
def test_busy_review_coalesces_due_turns_and_save_clears_pending_check(harness, saves):
    agent, _ = harness.create(2)
    for _ in range(7):
        turn(agent)
    assert len(harness.pending) == 1
    assert agent._turns_since_evolution_review == 5
    latest_count = len(agent.messages)
    responses = [[skill_call("create", skill_md=skill_text(), short_description="可复用的验证流程")]] if saves else []
    harness.run_review(*responses)
    assert agent._turns_since_evolution_review == 0
    assert len(harness.pending) == (0 if saves else 1)
    if not saves:
        review_transport = harness.run_review()
        history = json.loads(review_transport.calls[0][1]["content"].split("\n", 1)[1])
        assert len(history) == latest_count - 1
        assert not harness.pending


def test_successful_unchanged_result_still_resets_without_new_dedup_logic(harness):
    agent, _ = harness.create()
    seed_skill()
    args = dict(action="evolution_update", name="review-demo", section="runtime_notes", operation="append", content="Verify outputs before reporting success.")
    first = json.loads(skill_manage(**args, parent_agent=agent))
    assert first["success"]
    agent._turns_since_evolution_review = 6
    second = json.loads(skill_manage(**args, parent_agent=agent))
    assert second["success"] and second["changed"] is False
    assert agent._turns_since_evolution_review == 0


def test_failed_save_and_validation_do_not_reset_and_non_user_turn_does_not_count(harness):
    agent, transport = harness.create()
    seed_skill()
    agent._turns_since_evolution_review = 3
    transport.responses.append([skill_call("validate"), skill_call("edit", skill_md="invalid")])
    turn(agent)
    assert agent._turns_since_evolution_review == 4
    turn(agent, advance_background_review=False)
    assert agent._turns_since_evolution_review == 4


def test_thread_start_failure_preserves_due_count_and_next_turn_retries(harness, monkeypatch):
    agent, _ = harness.create(2)

    def fail_start(**kwargs):
        raise RuntimeError("cannot create review thread")

    monkeypatch.setattr(background_review.threading, "Thread", fail_start)
    turn(agent)
    turn(agent)
    assert agent._turns_since_evolution_review == 2
    monkeypatch.setattr(background_review.threading, "Thread", harness.thread_factory)
    turn(agent)
    assert len(harness.pending) == 1
    assert agent._turns_since_evolution_review == 0
    harness.run_review()


@pytest.mark.parametrize("source", ["foreground", "background"])
@pytest.mark.parametrize("action,kwargs", [
    ("edit", {"skill_md": skill_text() + "\nRecord the evidence.\n"}),
    ("patch", {"old_text": "Read the input", "new_text": "Inspect the input"}),
    ("write_file", {"file_path": "references/checks.md", "content": "Check the expected output."}),
    ("remove_file", {"file_path": "references/checks.md"}),
    ("evolution_update", {"section": "runtime_notes", "operation": "append", "content": "Check outputs before reporting."}),
    ("delete", {}),
])
def test_committed_skill_actions_reset_in_both_execution_paths(harness, source, action, kwargs):
    agent, transport = harness.create(2 if source == "background" else 10)
    seed_skill()
    if action == "remove_file":
        skill_store.write_skill_file(name="review-demo", file_path="references/checks.md", content="Check the output.")
    call = skill_call(action, **kwargs)
    if source == "background":
        for _ in range(3):
            turn(agent)
        result_transport = harness.run_review([call])
    else:
        agent._turns_since_evolution_review = 6
        transport.responses.append([call])
        turn(agent)
        result_transport = transport
    result = next(json.loads(row["content"]) for row in result_transport.calls[-1]
                  if row.get("tool_call_id") == call["id"])
    assert result["success"], result
    assert agent._turns_since_evolution_review == 0


def test_foreground_save_clears_pending_review_without_waiting_for_active_review(harness):
    agent, transport = harness.create(2)
    for _ in range(4):
        turn(agent)
    transport.responses.append([skill_call("create", skill_md=skill_text(), short_description="可复用的验证流程")])
    turn(agent)
    assert agent._turns_since_evolution_review == 0
    assert len(harness.pending) == 1
    harness.run_review()
    assert not harness.pending
    turn(agent)
    assert agent._turns_since_evolution_review == 1
    turn(agent)
    assert len(harness.pending) == 1


def test_background_save_during_inflight_main_turn_is_not_counted_again(harness):
    agent, transport = harness.create(2)
    turn(agent)
    turn(agent)
    in_model = threading.Event()
    release_model = threading.Event()

    def blocked_response():
        in_model.set()
        assert release_model.wait(10)

    transport.responses.append(blocked_response)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(turn, agent)
        try:
            assert in_model.wait(5)
            harness.run_review([skill_call("create", skill_md=skill_text(), short_description="可复用的验证流程")])
            assert agent._turns_since_evolution_review == 0
        finally:
            release_model.set()
        assert future.result(timeout=5)["completed"]
    assert agent._turns_since_evolution_review == 0
    turn(agent)
    assert not harness.pending
    turn(agent)
    assert len(harness.pending) == 1


@pytest.mark.parametrize("save", [False, True])
def test_review_exception_releases_slot_and_committed_save_survives(harness, monkeypatch, save):
    agent, _ = harness.create(2)
    for _ in range(4):
        turn(agent)
    original_run = MClaw.run_conversation

    def fail_review(self, *args, **kwargs):
        if getattr(self, "_execution_actor", "") == "background_review":
            original_run(self, *args, **kwargs)
            raise RuntimeError("failure after running review")
        return original_run(self, *args, **kwargs)

    monkeypatch.setattr(MClaw, "run_conversation", fail_review)
    script = [[skill_call("create", skill_md=skill_text(), short_description="可复用的验证流程")]] if save else []
    harness.run_review(*script)
    assert len(harness.pending) == (0 if save else 1)
    monkeypatch.setattr(MClaw, "run_conversation", original_run)
    if harness.pending:
        harness.run_review()
    turn(agent)
    turn(agent)
    assert len(harness.pending) == 1


def test_save_racing_thread_start_failure_is_not_undone(harness, monkeypatch):
    agent, _ = harness.create(2)

    def fail_after_save(**kwargs):
        result = json.loads(skill_manage(
            "create", name="review-demo", skill_md=skill_text(),
            short_description="可复用的验证流程", parent_agent=agent,
        ))
        assert result["success"]
        raise RuntimeError("thread start failed after foreground save")

    monkeypatch.setattr(background_review.threading, "Thread", fail_after_save)
    turn(agent)
    turn(agent)
    assert agent._turns_since_evolution_review == 0
    monkeypatch.setattr(background_review.threading, "Thread", harness.thread_factory)
    turn(agent)
    assert not harness.pending
    turn(agent)
    assert len(harness.pending) == 1


def test_parallel_requests_share_one_review_slot_per_session(harness):
    first, _ = harness.create()
    second, _ = harness.create()
    barrier = threading.Barrier(6)

    def request_review(agent):
        barrier.wait(timeout=5)
        background_review.spawn_background_review(agent, messages_snapshot=[], review_skills=True)

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = [executor.submit(request_review, first if index < 3 else second) for index in range(6)]
        for future in futures:
            future.result(timeout=5)
    assert len(harness.pending) == 2


def test_skill_review_does_not_redirect_other_sessions_output(harness):
    agent, _ = harness.create(1)
    turn(agent)
    stdout, stderr = sys.stdout, sys.stderr
    observed = []

    def observe_output():
        observed.append((sys.stdout is stdout, sys.stderr is stderr))

    harness.run_review(observe_output)
    assert observed == [(True, True)]


@pytest.mark.parametrize("mode", ["failed", "cancelled", "disabled", "delegated"])
def test_only_completed_eligible_user_turns_advance_periodic_review(harness, mode):
    config = {"tools": {"disabled": ["skill_manage"]}} if mode == "disabled" else {}
    agent, transport = harness.create(2, **config)
    agent._turns_since_evolution_review = 1
    if mode == "failed":
        transport.responses.append(ModelCallError("test failure", provider="openai", model="synthetic-f10"))
    elif mode == "cancelled":
        transport.responses.append(agent.interrupt)
    elif mode == "delegated":
        agent._delegate_depth = 1
    turn(agent)
    assert agent._turns_since_evolution_review == 1
    assert not harness.pending


def test_busy_skill_review_does_not_block_memory_review(harness):
    agent, _ = harness.create(2)
    turn(agent)
    turn(agent)
    turn(agent)
    agent._turns_since_memory_review = 4
    background_review.spawn_background_review(
        agent, messages_snapshot=agent.messages, review_memory=True, review_skills=True,
        pending_skill_review=True,
    )
    assert len(harness.pending) == 2
    harness.run_review()
    memory = harness.run_review()
    assert "memory_add" in memory.calls[0][0]["content"]
    assert "skill_manage" not in memory.calls[0][0]["content"]
    assert agent._turns_since_evolution_review == 1
    assert agent._turns_since_memory_review == 4
    assert not harness.pending


def test_failed_clone_construction_releases_review_slot(harness, monkeypatch):
    agent, _ = harness.create(2)
    turn(agent)
    turn(agent)
    original_init = MClaw.__init__

    def fail_init(self, **kwargs):
        raise RuntimeError("clone construction failed")

    monkeypatch.setattr(MClaw, "__init__", fail_init)
    harness.pending.popleft()()
    monkeypatch.setattr(MClaw, "__init__", original_init)
    turn(agent)
    turn(agent)
    assert len(harness.pending) == 1
    harness.run_review()


def test_pending_review_does_not_launch_after_session_closes(harness):
    agent, _ = harness.create(2)
    for _ in range(4):
        turn(agent)
    assert len(harness.pending) == 1
    assert agent.close()
    harness.run_review()
    assert not harness.pending


def test_background_prompt_and_setting_describe_automatic_creation_and_interval(harness):
    from mclaw.application.configuration import ConfigurationService
    from mclaw.constants import get_mclaw_home

    agent, _ = harness.create(1)
    turn(agent)
    review = harness.run_review()
    prompt = review.calls[0][-1]["content"]
    assert "自动创建新 Skill" in prompt
    assert "本轮工具和动作允许的范围内" in prompt
    schema = json.dumps(ConfigurationService(get_mclaw_home()).read(), ensure_ascii=False)
    assert "0 表示关闭定期检查，对话中的主动沉淀仍可执行" in schema


def test_two_sessions_can_review_and_save_concurrently_without_losing_updates(harness):
    first, _ = harness.create(2)
    second, _ = harness.create(2)
    record = seed_skill()
    for agent in (first, second):
        for _ in range(3):
            turn(agent)
    barrier = threading.Barrier(2)

    def response(content):
        def synchronized_call():
            barrier.wait(timeout=5)
            return [skill_call("evolution_update", section="runtime_notes", operation="append", content=content)]
        return (synchronized_call,)

    harness.scripts.extend([response("Verify the input."), response("Verify the output.")])
    targets = [harness.pending.popleft(), harness.pending.popleft()]
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(target) for target in targets]
        for future in futures:
            future.result(timeout=10)
    assert first._turns_since_evolution_review == second._turns_since_evolution_review == 0
    data = json.loads((Path(record["path"]) / "skill_evolution.json").read_text(encoding="utf-8"))
    assert set(data["runtime_notes"]) == {"Verify the input.", "Verify the output."}
    assert all(transport.closed for transport in harness.transports[2:])
    assert not harness.pending


def test_background_success_with_changed_false_still_resets(harness):
    agent, _ = harness.create(2)
    seed_skill()
    kwargs = dict(section="runtime_notes", operation="append", content="Verify the output.")
    skill_store.evolution_update(name="review-demo", **kwargs)
    for _ in range(3):
        turn(agent)
    transport = harness.run_review([skill_call("evolution_update", **kwargs)])
    result = next(json.loads(row["content"]) for row in transport.calls[-1]
                  if row.get("tool_call_id") == "skill-evolution_update")
    assert result["success"] and result["changed"] is False
    assert agent._turns_since_evolution_review == 0
