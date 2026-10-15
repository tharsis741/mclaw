# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""F07 regressions through real Agent initialization, requests, and dispatch."""

from __future__ import annotations

import copy
import json
from collections import deque

import pytest

from mclaw.agent.core import MClaw
from mclaw.agent.transports.base import ModelCallResult
from mclaw.providers.registry import PROVIDER_REGISTRY
from mclaw.providers.runtime import ProviderRuntimeContext
from mclaw.tools.memory_tool import MEMORY_TOOL_NAMES, MEMORY_TOOL_SCHEMAS, MemoryStore
from mclaw.tools.toolsets import SKILLS_TOOLS


class RecordingTransport:
    def __init__(self, context):
        self.context = context
        self.calls = []
        self.responses = deque()

    def call(self, **kwargs):
        self.calls.append(copy.deepcopy({
            "messages": kwargs["messages"], "tools": kwargs["tools"],
            "dynamic_system_context": kwargs["options"].dynamic_system_context,
        }))
        tool_calls = self.responses.popleft() if self.responses else None
        return ModelCallResult(
            content="" if tool_calls else "done", tool_calls=tool_calls,
            finish_reason="tool_calls" if tool_calls else "stop", reasoning=None,
            usage=None, was_streamed=False, provider=self.context.provider, model=self.context.model,
        )


@pytest.fixture
def agents(tmp_path, monkeypatch):
    monkeypatch.setenv("MCLAW_HOME", str(tmp_path / "home"))
    context = ProviderRuntimeContext(
        profile=PROVIDER_REGISTRY["openai"], model="synthetic-f07",
        api_key="synthetic-test-key", base_url="https://example.test/v1", auth_source="explicit",
    )
    created = []

    def create(**overrides):
        config = {
            "compression": {"enabled": False}, "checkpoints": {"enabled": False},
            "memory": {"memory_review_round": 0}, "skills": {"evolution_review_round": 0},
            **overrides,
        }
        transport = RecordingTransport(context)
        monkeypatch.setattr("mclaw.agent.core.create_transport", lambda _context: transport)
        agent = MClaw(provider_runtime=context, config=config, platform="web", workspace=str(tmp_path))
        created.append(agent)
        return agent, transport

    yield create
    for agent in created:
        agent.close()


def request(agent, transport):
    agent.run_conversation("inspect capabilities", advance_background_review=False)
    return transport.calls[-1]


def memory_schemas(call):
    return {
        tool["function"]["name"]: tool["function"]
        for tool in call["tools"] if tool["function"]["name"] in MEMORY_TOOL_NAMES
    }


def tool_call(name, target, content):
    return {
        "id": f"call-{name}-{target}", "type": "function",
        "function": {"name": name, "arguments": json.dumps({"target": target, "content": content})},
    }


@pytest.mark.parametrize("targets", [{"memory", "user"}, {"memory"}, {"user"}, set()])
def test_memory_targets_match_model_request_before_and_after_tool_refresh(agents, targets):
    original_schemas = copy.deepcopy(MEMORY_TOOL_SCHEMAS)
    agent, transport = agents(memory={
        "memory_enabled": "memory" in targets, "user_profile_enabled": "user" in targets,
    })
    original_config = copy.deepcopy(agent.config)
    for refreshed in (False, True):
        if refreshed:
            agent._discover_tools()
            agent.mark_prompt_epoch_dirty()
        call = request(agent, transport)
        schemas = memory_schemas(call)
        assert set(schemas) == (set(MEMORY_TOOL_NAMES) if targets else set())
        prompt = call["messages"][0]["content"]
        for target in ("memory", "user"):
            assert (f"target='{target}'" in prompt) == (target in targets)
        for schema in schemas.values():
            assert set(schema["parameters"]["properties"]["target"]["enum"]) == targets
            if targets == {"user"}:
                assert "target" in schema["parameters"].get("required", [])
                assert "'memory'" not in schema["description"]
        assert set(SKILLS_TOOLS) <= agent.valid_tool_names
    assert agent.config == original_config
    assert MEMORY_TOOL_SCHEMAS == original_schemas


@pytest.mark.parametrize("enabled", [
    set(MEMORY_TOOL_NAMES), {"memory_read"}, {"memory_add"}, set(),
])
def test_memory_initialization_and_refresh_preserve_individual_tool_disables(agents, enabled):
    disabled = sorted(set(MEMORY_TOOL_NAMES) - enabled)
    agent, transport = agents(tools={"disabled": disabled})
    for refreshed in (False, True):
        if refreshed:
            agent._discover_tools()
            agent.mark_prompt_epoch_dirty()
        call = request(agent, transport)
        assert set(memory_schemas(call)) == enabled
        assert set(agent.valid_tool_names) & set(MEMORY_TOOL_NAMES) == enabled
        prompt = call["messages"][0]["content"]
        for name in disabled:
            assert name not in prompt
        assert set(SKILLS_TOOLS) <= agent.valid_tool_names
    assert agent.config["tools"]["disabled"] == disabled


def test_target_settings_keep_session_semantics_after_configuration_refresh(agents):
    agent, transport = agents(memory={"memory_enabled": False, "user_profile_enabled": True})
    agent.config["memory"]["memory_enabled"] = True
    agent._discover_tools()
    agent.mark_prompt_epoch_dirty()
    existing = request(agent, transport)
    assert "target='memory'" not in existing["messages"][0]["content"]
    assert all(schema["parameters"]["properties"]["target"]["enum"] == ["user"]
               for schema in memory_schemas(existing).values())
    new_agent, new_transport = agents(memory=copy.deepcopy(agent.config["memory"]))
    assert "target='memory'" in request(new_agent, new_transport)["messages"][0]["content"]


@pytest.mark.parametrize("target", ["user", "memory"])
def test_real_dispatch_allows_enabled_target_and_rejects_disabled_target(agents, target):
    other = "memory" if target == "user" else "user"
    agent, transport = agents(memory={
        "memory_enabled": target == "memory", "user_profile_enabled": target == "user",
    })
    transport.responses.append([
        tool_call("memory_add", target, "Allowed durable preference"),
        tool_call("memory_add", other, "Rejected durable preference"),
    ])
    request(agent, transport)
    results = {message["tool_call_id"]: json.loads(message["content"])
               for message in agent.messages if message["role"] == "tool"}
    assert results[f"call-memory_add-{target}"]["success"] is True
    assert results[f"call-memory_add-{other}"]["success"] is False
    store = MemoryStore()
    store.load_from_disk()
    assert "Allowed durable preference" in store._success_response(target)["entries"]
    assert store._success_response(other)["entries"] == []


def test_disabled_write_is_not_exposed_or_executed_in_main_turn_or_flush(agents):
    agent, transport = agents(tools={"disabled": ["memory_add"]})
    rejected = tool_call("memory_add", "user", "This disabled write must never persist")
    transport.responses.append([rejected])
    request(agent, transport)
    results = [json.loads(message["content"]) for message in agent.messages if message["role"] == "tool"]
    assert any("disabled" in result.get("error", "") for result in results)

    transport.responses.append([rejected])
    agent.flush_memories([
        {"role": "system", "content": "synthetic"},
        {"role": "user", "content": "remember a preference"},
        {"role": "assistant", "content": "review it"},
    ])
    assert len(transport.calls) == 3
    for call in transport.calls:
        assert "memory_add" not in memory_schemas(call)
        assert "memory_add" not in call["messages"][0]["content"]
    store = MemoryStore()
    store.load_from_disk()
    assert store._success_response("user")["entries"] == []


def test_flush_skips_model_call_when_all_memory_writes_are_disabled(agents):
    agent, transport = agents(tools={"disabled": ["memory_add", "memory_replace", "memory_remove"]})
    agent.flush_memories([
        {"role": "system", "content": "synthetic"},
        {"role": "user", "content": "remember a preference"},
        {"role": "assistant", "content": "review it"},
    ])
    assert transport.calls == []


def test_provider_schema_failure_does_not_restore_unscoped_memory_tools(agents, monkeypatch):
    agent, transport = agents(memory={"memory_enabled": False, "user_profile_enabled": True})
    def fail():
        raise RuntimeError("synthetic provider failure")
    with monkeypatch.context() as scoped:
        scoped.setattr(agent._memory_manager, "get_all_tool_schemas", fail)
        agent._discover_tools()
        agent.mark_prompt_epoch_dirty()
        call = request(agent, transport)
        assert not memory_schemas(call)
        assert "memory_add" not in call["messages"][0]["content"]
    agent._discover_tools()
    agent.mark_prompt_epoch_dirty()
    assert all(schema["parameters"]["properties"]["target"]["enum"] == ["user"]
               for schema in memory_schemas(request(agent, transport)).values())


def test_memory_initialization_failure_stays_unavailable_after_tool_refresh(agents, monkeypatch):
    def fail(*_args, **_kwargs):
        raise RuntimeError("synthetic memory initialization failure")
    monkeypatch.setattr("mclaw.agent.builtin_memory_provider.BuiltinMemoryProvider", fail)
    agent, transport = agents()
    for refreshed in (False, True):
        if refreshed:
            agent._discover_tools()
            agent.mark_prompt_epoch_dirty()
        assert not memory_schemas(request(agent, transport))
        assert set(SKILLS_TOOLS) <= agent.valid_tool_names


@pytest.mark.parametrize("disabled", [["skill_manage", "skill_tree"], ["skill_manage"], ["skill_tree"]])
def test_skill_read_guidance_does_not_require_disabled_management_or_tree(agents, disabled):
    agent, transport = agents(tools={"disabled": disabled})
    prompt = request(agent, transport)["messages"][0]["content"]
    assert "skill_view" in prompt
    for name in disabled:
        assert name not in prompt


@pytest.mark.parametrize("disable_all", [False, True])
def test_one_turn_restrictions_do_not_change_next_turn_default_capabilities(agents, disable_all):
    agent, transport = agents()
    original_tools = copy.deepcopy(agent.tools)
    disabled = {"memory_add", "memory_replace", "memory_remove", "skill_manage"}
    agent.run_conversation(
        "limited turn", disable_tools=disable_all, disabled_tool_names=disabled,
        advance_background_review=False,
    )
    call = transport.calls[-1]
    effective = {tool["function"]["name"] for tool in call["tools"]}
    assert not effective & disabled
    for name in (set(MEMORY_TOOL_NAMES) | set(SKILLS_TOOLS) if disable_all else disabled):
        assert name not in call["messages"][0]["content"]
    assert agent.tools == original_tools

    agent.run_conversation("normal turn", conversation_history=agent.messages, advance_background_review=False)
    normal = transport.calls[-1]
    assert set(memory_schemas(normal)) == set(MEMORY_TOOL_NAMES)
    assert set(SKILLS_TOOLS) <= {tool["function"]["name"] for tool in normal["tools"]}
    assert "memory_add" in normal["messages"][0]["content"]
    assert "skill_manage" in normal["messages"][0]["content"]


def test_flush_uses_enabled_target_and_requires_explicit_user_target(agents):
    agent, transport = agents(memory={"memory_enabled": False, "user_profile_enabled": True})
    transport.responses.append([tool_call("memory_add", "user", "Durable user preference")])
    agent.flush_memories([
        {"role": "system", "content": "synthetic"},
        {"role": "user", "content": "remember a preference"},
        {"role": "assistant", "content": "review it"},
    ])
    call = transport.calls[-1]
    assert "target='memory'" not in call["messages"][0]["content"]
    assert "target='user'" in call["messages"][0]["content"]
    assert all(schema["parameters"]["properties"]["target"]["enum"] == ["user"]
               for schema in memory_schemas(call).values())
    result = json.loads(agent._memory_manager.handle_tool_call("memory_add", {"content": "Missing target"}))
    assert result["success"] is False
    store = MemoryStore()
    store.load_from_disk()
    assert store._success_response("user")["entries"] == ["Durable user preference"]
    assert store._success_response("memory")["entries"] == []


@pytest.mark.parametrize("channel", ["weixin", "dingtalk"])
@pytest.mark.parametrize("enabled", [False, True])
def test_channel_delivery_guidance_agrees_with_send_tool_availability(agents, channel, enabled):
    from mclaw.prompts.channels import build_channel_context

    name = f"{channel}_send_file"
    agent, transport = agents(tools={"disabled": [] if enabled else [name]})
    agent.platform = channel
    agent.enabled_toolsets = ["mclaw-required", channel]
    agent._discover_tools()
    agent.mark_prompt_epoch_dirty()
    agent.run_conversation(
        "inspect channel capabilities", advance_background_review=False,
        extra_system=build_channel_context(channel, user_id="synthetic-user", chat_type="dm"),
    )
    call = transport.calls[-1]
    assert (name in {tool["function"]["name"] for tool in call["tools"]}) == enabled
    prompt = call["messages"][0]["content"] + (call["dynamic_system_context"] or "")
    assert (name in prompt) == enabled
