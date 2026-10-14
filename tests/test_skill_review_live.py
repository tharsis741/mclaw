# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Opt-in regressions using real custom-provider Agent requests and Skill writes.

Set MCLAW_RUN_LIVE_AGENT_TESTS=1. MCLAW_LIVE_REPORT_DIR optionally retains
redacted evidence. Event gates control interleaving, never model responses.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path

import pytest

from mclaw.agent.core import MClaw
from mclaw.agent.skill_review_state import get_skill_review_state
from mclaw.application.diagnostics import configure_worker_logging
from mclaw.cli.config import load_config, load_env
from mclaw.prompts.skills import build_skill_runtime_content
from mclaw.providers.resolver import resolve_provider_runtime_context
from mclaw.skills_hub import skill_store
from mclaw.tools.skill_tools.manage_tool import skill_manage

pytestmark = pytest.mark.skipif(
    os.environ.get("MCLAW_RUN_LIVE_AGENT_TESTS") != "1",
    reason="Explicit opt-in required for real configured-provider requests",
)

WORKFLOW = """我们团队长期采用“云杉会议决议整理”流程，已连续三次验证有效。
用途：把会议原文整理为决议清单。输入：会议原文，可能包含决议、负责人、截止日期和未解决问题。
步骤：只提取原文明示的决议；保留原文负责人和日期，缺失统一标为待确认；
按原文顺序排列；单独列出原文明示的未解决问题。
输出：决议表（决议、负责人、截止日期）、待确认事项列表。
验证样例：小林负责修复登录，周五完成；需要补充接口文档，负责人和日期未定。
预期：两项决议，第一项小林/周五，第二项负责人和日期均为待确认。
边界：只整理文本，不执行决议、不联网、不发送消息、不创建日程。
本条消息只回复“收到”，不调用任何工具；此限制仅适用于本条回复。"""
ARITHMETIC = "一次性算术问答，本条回复只输出 8+1 的结果，不调用工具。"
LEGACY_BODY = """---
name: ledger-review
description: 收到账目样本时，按顺序核对编号和状态并列出缺失项。
---
# 账目样本核对
逐行读取编号和状态；原文缺失的值标为待确认；保留原文顺序。
输出原文编号、状态、缺失项；仅整理文本，不修改源数据。
"""
COPIED_SIDECARS = """
## M-Claw Skill Metadata

~~~yaml
created_by: main_agent
updated_by: main_agent
~~~

## M-Claw Skill Memory

~~~json
{"user_preferences": ["缺失值统一标为待确认"]}
~~~
"""


class Trace(logging.Handler):
    def __init__(self, context):
        super().__init__(logging.INFO)
        self.context = context
        self.records = []
        self.calls = []
        self.results = []
        self.agents = []
        self.lock_data = threading.RLock()
        self.gate_mode = None
        self.gated = False
        self.ready = threading.Event()
        self.gate_release = threading.Event()
        self.backgrounds = 0
        self.active = 0
        self.max_active = 0
        self.snapshots = []
        self.events = []
        self.fail_after_save = False
        self.fail_before_save = False
        self.worker_requests = None

    def emit(self, record):
        if record.name != "mclaw.agent.skill_review_state":
            return
        text = record.getMessage()
        if text.startswith("[SKILL REVIEW] "):
            data = json.loads(text.removeprefix("[SKILL REVIEW] "))
            with self.lock_data:
                self.records.append(data)
            if data["event"] in {"saved", "review_reserved", "review_finished"}:
                print(json.dumps({"event": data["event"], "source": data.get("source"),
                                  "before": data["before"], "after": data["after"]}), flush=True)


class RealTransport:
    def __init__(self, delegate, agent):
        self.delegate, self.agent = delegate, agent

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    def call(self, **kwargs):
        trace = self.agent.trace
        actor = getattr(self.agent, "_execution_actor", "main_agent")
        started = time.monotonic()
        row = {"actor": actor, "stream": bool(kwargs.get("stream_callback")),
               "started": started, "tool_count": len(kwargs.get("tools") or [])}
        with trace.lock_data:
            trace.calls.append(row)
        try:
            response = self.delegate.call(**kwargs)
        except Exception as exc:
            row.update(error_type=type(exc).__name__, error_code=getattr(exc, "code", None))
            raise
        finally:
            row["seconds"] = round(time.monotonic() - started, 3)
        calls = []
        for call in response.tool_calls or []:
            function = call.get("function") or {}
            arguments = json.loads(function.get("arguments") or "{}")
            calls.append({"tool": function.get("name"), "action": arguments.get("action")})
        row.update(finish_reason=response.finish_reason, calls=calls)
        print(json.dumps({"model_response": actor, "seconds": row["seconds"], "calls": calls}), flush=True)
        if trace.fail_before_save and actor == "background_review":
            raise RuntimeError("Controlled failure after a real response, before any Skill write")
        if trace.fail_after_save and actor == "background_review" and any(
            item["actor"] == "background_review" and item["success"] for item in trace.results
        ):
            raise RuntimeError("Controlled failure after a real response and committed Skill save")
        writes = any(item["tool"] == "skill_manage" and item["action"] in {
            "create", "edit", "patch", "write_file", "evolution_update",
        } for item in calls)
        if actor == "background_review" and not trace.gated and (
            trace.gate_mode == "first_response" or trace.gate_mode == "save" and writes
        ):
            trace.gated = True
            trace.ready.set()
            if not trace.gate_release.wait(240):
                raise TimeoutError("Live test interleaving gate expired")
        return response


@pytest.fixture
def live(tmp_path, monkeypatch, request):
    config = load_config(strict=True)
    for key, value in load_env().items():
        monkeypatch.setenv(key, value)
    context = resolve_provider_runtime_context(provider="custom", config=config)
    assert context.provider == "custom" and context.api_mode == "chat_completions"
    assert "deepseek" in context.model.casefold()
    trace = Trace(context)
    trace.config = copy.deepcopy(config)
    monkeypatch.setenv("MCLAW_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    logger = logging.getLogger()
    old_level = logger.level
    logger.addHandler(trace)
    handler = configure_worker_logging(tmp_path, request.node.name,
                                       secrets=lambda: [context.api_key], run_id=lambda: "live-test")

    class RealAgent(MClaw):
        def __init__(self, **kwargs):
            self.trace = trace
            callback = kwargs.pop("event_callback", None)
            def record_event(event):
                if event.get("type") == "skill_review":
                    trace.events.append(copy.deepcopy(event))
                if callback:
                    callback(event)
            kwargs["event_callback"] = record_event
            super().__init__(**kwargs)
            self.transport = RealTransport(self.transport, self)
            trace.agents.append(self)

        def _on_skill_write(self, action, result):
            super()._on_skill_write(action, result)
            trace.results.append({"actor": getattr(self, "_execution_actor", "main_agent"),
                                  "action": action, "success": result.get("success"),
                                  "name": result.get("name")})

        def run_conversation(self, *args, **kwargs):
            background = kwargs.get("call_source") == "background_review"
            if background:
                with trace.lock_data:
                    trace.backgrounds += 1
                    trace.active += 1
                    trace.max_active = max(trace.max_active, trace.active)
                    material = kwargs["conversation_history"][-1]["content"].split("\n", 1)[1]
                    trace.snapshots.append(len(json.loads(material)))
            try:
                return super().run_conversation(*args, **kwargs)
            finally:
                if background:
                    with trace.lock_data:
                        trace.active -= 1

    def make_agent(interval, *, session_id=None, event_callback=None):
        agent_config = copy.deepcopy(config.get("agent") or {})
        agent_config["max_turns"] = 12
        return RealAgent(
            provider_runtime=context, platform="web", workspace=str(tmp_path),
            session_id=session_id or request.node.name, print_fn=lambda *a, **kw: None,
            event_callback=event_callback,
            stream_callback=lambda *a, **kw: None,
            config={"agent": agent_config, "compression": {"enabled": False},
                    "checkpoints": {"enabled": False},
                    "memory": {"memory_enabled": False, "user_profile_enabled": False, "memory_review_round": 0},
                    "skills": {"evolution_review_round": interval}},
        )

    yield make_agent, trace, tmp_path
    trace.gate_release.set()
    for agent in trace.agents:
        if getattr(agent, "_execution_actor", "") != "background_review":
            get_skill_review_state(agent).close()
    for agent in trace.agents:
        agent.interrupt()
    deadline = time.monotonic() + 30
    while trace.active and time.monotonic() < deadline:
        time.sleep(0.1)
    for agent in trace.agents:
        agent.close()
    if handler:
        handler.flush()
        logger.removeHandler(handler)
        handler.close()
    logger.removeHandler(trace)
    logger.setLevel(old_level)
    report_dir = os.environ.get("MCLAW_LIVE_REPORT_DIR")
    if report_dir:
        target = Path(report_dir)
        target.mkdir(parents=True, exist_ok=True)
        payload = {"test": request.node.name, "provider": context.provider, "model": context.model,
                   "api_mode": context.api_mode, "calls": trace.calls, "states": trace.records,
                   "tool_results": trace.results, "max_backgrounds_active": trace.max_active,
                   "background_count": trace.backgrounds, "snapshot_message_counts": trace.snapshots,
                   "notifications": trace.events,
                   "worker_model_requests": trace.worker_requests,
                   "calls_scope": "In-process transport; subprocess requests are counted separately from worker logs",
                   "coordination": "Event gate delays a real model response before tool dispatch; no response replacement"}
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        if context.api_key:
            text = text.replace(context.api_key, "<redacted>")
        (target / (request.node.name + ".json")).write_text(text + "\n", encoding="utf-8")


def turn(agent, text):
    result = agent.run_conversation(text, conversation_history=agent.messages)
    assert result.get("completed"), "Real Agent turn did not complete"
    return result


def wait_idle(agent, seconds=600):
    deadline = time.monotonic() + seconds
    while get_skill_review_state(agent).snapshot()["running_review_id"]:
        if time.monotonic() > deadline:
            pytest.fail("Real background review did not finish within the test deadline")
        time.sleep(0.1)


@pytest.mark.parametrize("saves", [True, False], ids=["save_clears_pending", "no_save_runs_pending"])
def test_live_busy_review(live, saves):
    make_agent, trace, root = live
    trace.gate_mode = "save" if saves else "first_response"
    agent = make_agent(2)
    agent._application_task_id = "first-trigger"
    agent._application_response_id = "first-response"
    state = get_skill_review_state(agent)
    turn(agent, WORKFLOW if saves else ARITHMETIC)
    assert state.turns == 1
    turn(agent, ARITHMETIC)
    assert trace.ready.wait(600), "Background did not reach the real response coordination point"
    assert state.turns == 0 and trace.backgrounds == 1
    agent._application_task_id = "queued-trigger"
    agent._application_response_id = "queued-response"
    turn(agent, ARITHMETIC)
    assert state.turns == 1
    turn(agent, ARITHMETIC)
    before = state.snapshot()
    assert before["turns"] == 2 and before["pending"] and before["running_review_id"]
    assert trace.backgrounds == 1
    trace.gate_release.set()
    wait_idle(agent)
    after = state.snapshot()
    assert after["turns"] == 0 and not after["pending"] and after["running_review_id"] is None
    saved = [row for row in trace.records if row["event"] == "saved" and row.get("source") == "background_review"]
    if saves:
        assert saved and trace.backgrounds == 1
        first = saved[0]
        assert first["before"] == before
        assert first["after"]["turns"] == 0 and not first["after"]["pending"]
        assert first["after"]["save_generation"] == first["before"]["save_generation"] + 1
        assert first["review_id"] == before["running_review_id"]
        names = [row["name"] for row in trace.results if row["actor"] == "background_review"
                 and row["success"] and row["action"] == "create"]
        assert names, "The background Agent must actually create a Skill"
        for name in names:
            view = skill_store.view_skill(name)
            assert view["mclaw_skill"]["created_by"] == "background_review"
            assert view["mclaw_skill"]["status"] == "enabled"
            assert "M-Claw Skill Metadata" not in view["content"]
            assert "M-Claw Skill Memory" not in view["content"]
            assert view["content"] == (root / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
    else:
        assert not saved and trace.backgrounds == 2
        assert trace.snapshots == [4, 8], "The queued review must use the newest completed history"
    assert trace.max_active == 1
    turn(agent, ARITHMETIC)
    assert state.turns == 1 and not state.snapshot()["running_review_id"]
    owner_rows = [row for row in trace.records if row["state_id"] == trace.records[0]["state_id"]]
    assert [row["sequence"] for row in owner_rows] == list(range(1, len(owner_rows) + 1))
    log_path = root / "logs" / "sessions" / (hashlib.sha256(agent.session_id.encode()).hexdigest() + ".log")
    log = log_path.read_text(encoding="utf-8")
    assert "[SKILL REVIEW]" in log and "[BACKGROUND REVIEW]" in log
    assert WORKFLOW not in log and "小林" not in log
    assert not trace.context.api_key or trace.context.api_key not in log


def test_live_repair_legacy_metadata(live):
    make_agent, trace, root = live
    skill_store.create_skill(name="ledger-review", skill_md=LEGACY_BODY,
                             short_description="账目样本核对", actor="background_review",
                             initial_evolution={"user_preferences": ["缺失值统一标为待确认"]})
    path = root / "skills" / "ledger-review" / "SKILL.md"
    path.write_text(LEGACY_BODY + COPIED_SIDECARS, encoding="utf-8")
    agent = make_agent(0)
    turn(agent, "请用 skill_view 检查已有 ledger-review。它的 SKILL.md 意外混入 M-Claw Skill Metadata 和 "
         "M-Claw Skill Memory 段。请修复正文，保留原有流程与全部经验；由服务维护元数据，然后校验。")
    view = skill_store.view_skill("ledger-review")
    assert "M-Claw Skill Metadata" not in view["content"] and "M-Claw Skill Memory" not in view["content"]
    assert view["mclaw_skill"]["created_by"] == "background_review"
    assert view["mclaw_skill"]["updated_by"] == "main_agent"
    assert "缺失值统一标为待确认" in view["skill_evolution"]["user_preferences"]
    assert "缺失值统一标为待确认" in build_skill_runtime_content(view)
    assert view["content"] == path.read_text(encoding="utf-8")
    calls = [call for row in trace.calls for call in row.get("calls", [])]
    assert any(call["tool"] == "skill_view" for call in calls)
    assert any(row["success"] and row["action"] in {"edit", "patch"} for row in trace.results)
    # Deterministic malformed-write probes accompany the real Agent repair.
    # They execute the real tool/service and must leave both files and state intact.
    state = get_skill_review_state(agent)
    snapshot = state.snapshot()
    original = path.read_bytes()
    metadata = (path.parent / "mclaw_skill.yaml").read_bytes()
    invalid = view["content"] + COPIED_SIDECARS
    cases = [
        {"action": "create", "name": "bad-copy", "short_description": "拷贝保护验证",
         "skill_md": invalid.replace("name: ledger-review", "name: bad-copy", 1)},
        {"action": "edit", "name": "ledger-review", "skill_md": invalid},
        {"action": "patch", "name": "ledger-review", "old_text": view["content"], "new_text": invalid},
        {"action": "write_file", "name": "ledger-review", "file_path": "SKILL.md", "overwrite": True, "content": invalid},
        {"action": "write_file", "name": "ledger-review", "file_path": "SKILL.md", "overwrite": True,
         "encoding": "base64", "content": base64.b64encode(invalid.encode()).decode()},
    ]
    if os.name == "nt":
        cases.append({"action": "write_file", "name": "ledger-review", "file_path": "skill.md",
                      "overwrite": True, "content": invalid})
    for args in cases:
        result = json.loads(skill_manage(**args, parent_agent=agent))
        expected = "Sidecar files" if args.get("file_path") == "SKILL.md" else "Metadata/Memory"
        assert result["success"] is False and expected in result["error"]
        assert state.snapshot() == snapshot and path.read_bytes() == original
        assert (path.parent / "mclaw_skill.yaml").read_bytes() == metadata
    assert not (root / "skills" / "bad-copy").exists()


def test_live_invocation_preserves_separate_experience(live):
    from mclaw.application.skills import SkillService
    from mclaw.prompts.skills import build_skill_invocation_system

    make_agent, _trace, root = live
    note = "核对摘要最后单独输出固定口径标识 QH-07。"
    skill_store.create_skill(name="ledger-review", skill_md=LEGACY_BODY,
                             short_description="账目样本核对",
                             initial_evolution={"user_preferences": [note]})
    view = skill_store.view_skill("ledger-review")
    snapshot = SkillService(root).snapshots(["ledger-review"])[0]
    assert note not in view["content"]
    assert note in snapshot["content"]
    assert snapshot["content"] == build_skill_runtime_content(view)
    assert "created_by" not in snapshot["content"]
    agent = make_agent(0)
    result = agent.run_conversation(
        "按所选 Skill 核对：编号 A，状态 PASS；编号 B，状态缺失。只输出核对摘要。",
        conversation_history=agent.messages,
        extra_system=build_skill_invocation_system("ledger-review", snapshot["content"]),
    )
    assert result.get("completed")
    assert "QH-07" in result["final_response"] and "待确认" in result["final_response"]



def test_live_foreground_save_clears_busy_review_queue(live):
    make_agent, trace, _root = live
    trace.gate_mode = "first_response"
    agent = make_agent(2)
    state = get_skill_review_state(agent)
    turn(agent, ARITHMETIC)
    turn(agent, ARITHMETIC)
    assert trace.ready.wait(600)
    turn(agent, ARITHMETIC)
    turn(agent, ARITHMETIC)
    before = state.snapshot()
    assert before["turns"] == 2 and before["pending"] and before["running_review_id"]
    prompt = WORKFLOW.rsplit("\n", 1)[0] + "\n请将这套流程创建为可复用 Skill，并校验。"
    turn(agent, prompt)
    saved = [row for row in trace.records if row["event"] == "saved" and row.get("source") == "main_agent"]
    assert saved and saved[0]["before"] == before
    assert saved[0]["after"]["turns"] == 0 and not saved[0]["after"]["pending"]
    assert any(row["event"] == "turn_skipped" and row.get("reason") == "save_during_turn" for row in trace.records)
    assert state.turns == 0 and not state.snapshot()["pending"]
    trace.gate_release.set()
    wait_idle(agent)
    assert state.turns == 0 and not state.snapshot()["pending"] and trace.backgrounds == 1
    assert trace.max_active == 1
    turn(agent, ARITHMETIC)
    assert state.turns == 1 and not state.snapshot()["running_review_id"]
