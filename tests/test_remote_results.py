# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import json
from types import SimpleNamespace

import pytest

from mclaw.agent.remote_results import project_history, project_reply, read_reply
from mclaw.dsoftbus import protocol
from mclaw.dsoftbus.tools import _encode, read_task_result_handler
from mclaw.providers.base import RuntimeProviderProfile
from mclaw.providers.runtime import ProviderRuntimeContext
from mclaw.state import SessionDB
from mclaw.tools.registry import registry


def parent(database=None, session_id='one'):
    return SimpleNamespace(
        _session_db=database, session_id=session_id, tools=[],
        valid_tool_names={'dsoftbus_run_agent_task', 'dsoftbus_read_task_result'},
        provider_runtime=ProviderRuntimeContext(
            profile=RuntimeProviderProfile(name='test', display_name='Test'), model='test',
            api_key='test-only', base_url='https://provider.test',
        ),
    )


@pytest.mark.parametrize('unit', ['文🙂', 'a', '\u0000'], ids=['utf8', 'ascii', 'escaped'])
def test_complete_task_tool_json_survives_its_registry_output_limit(unit):
    text = unit * (protocol.REMOTE_TEXT_RESULT_BYTES_MAX // len(unit.encode()))
    result = _encode({'success': True, 'text': text}, maximum=protocol.REMOTE_TASK_TOOL_RESULT_CHARS_MAX)
    assert json.loads(result)['text'] == text
    for name in ('dsoftbus_run_agent_task', 'dsoftbus_continue_agent_task'):
        assert len(result) <= registry._tools[name].max_result_size_chars


def test_full_reply_reaches_large_context_while_small_context_has_explicit_paging(monkeypatch):
    original = {'success': True, 'text': '正文🙂' * 25000 + '最后一段', '_untrustedRemoteData': True}
    raw = json.dumps(original, ensure_ascii=False)
    agent = parent()
    monkeypatch.setattr('mclaw.agent.context_metadata.resolve_context_length', lambda _: 1_000_000)
    assert project_reply(agent, raw, 'call', []) == raw
    monkeypatch.setattr('mclaw.agent.context_metadata.resolve_context_length', lambda _: 16384)
    value = json.loads(project_reply(agent, raw, 'call', []))
    assert value['text_truncated'] and value['read_more']['tool_call_id'] == 'call'
    text, offset = value['text'], value['read_more']['offset']
    while offset < len(original['text']):
        page = read_reply(agent, 'call', offset, 8192)
        assert page['_untrustedRemoteData'] is True
        text += page['text']
        offset = page['next_offset']
    assert page['eof'] and text == original['text']
    assert json.loads(raw) == original


def test_reply_paging_survives_restart_and_is_scoped_to_active_session_history(tmp_path, monkeypatch):
    database_path = tmp_path / 'history.db'
    raw = json.dumps({'text': 'abc🙂' * 40000 + 'END', '_untrustedRemoteData': True})
    database = SessionDB(database_path)
    for sid in ('one', 'two'):
        database.create_session(sid, source='web')
    database.append_message('one', 'tool', content=raw, tool_name='dsoftbus_run_agent_task', tool_call_id='remote')
    database.append_message('one', 'tool', content=raw, tool_name='read_file', tool_call_id='ordinary')
    database.close()
    database = SessionDB(database_path)
    try:
        agent = parent(database)
        assert read_reply(agent, 'remote', 160000, 8192)['text'] == 'END'
        assert json.loads(read_task_result_handler({'tool_call_id': 'remote', 'offset': 160000}, parent_agent=agent))['text'] == 'END'
        with pytest.raises(LookupError):
            read_reply(parent(database, 'two'), 'remote', 0, 4096)
        with pytest.raises(LookupError):
            read_reply(agent, 'ordinary', 0, 4096)
        monkeypatch.setattr('mclaw.agent.context_metadata.resolve_context_length', lambda _: 16384)
        messages = [
            {'role': 'assistant', 'tool_calls': [{'id': 'remote', 'function': {'name': 'dsoftbus_run_agent_task'}}]},
            {'role': 'tool', 'tool_call_id': 'remote', 'content': raw},
        ]
        projected = project_history(agent, messages)
        assert json.loads(projected[1]['content'])['text_truncated']
        assert messages[1]['content'] == database.get_tool_result('one', 'remote')['content'] == raw
        database._conn.execute("UPDATE messages SET invalidated_at=1 WHERE tool_call_id='remote'")
        with pytest.raises(LookupError):
            read_reply(agent, 'remote', 0, 4096)
    finally:
        database.close()


@pytest.mark.parametrize('offset,limit', [(-1, 4096), (0, 0), (0, 8193), (True, 4096), (0, True)])
def test_reply_paging_rejects_invalid_ranges(offset, limit):
    with pytest.raises(ValueError):
        read_reply(parent(), 'call', offset, limit)


def test_local_agent_analyzes_full_remote_text_and_persists_original(tmp_path, monkeypatch):
    from mclaw.agent.core import MClaw
    from mclaw.agent.transports.base import ModelCallResult

    monkeypatch.setenv('MCLAW_HOME', str(tmp_path / 'home'))
    monkeypatch.setattr('mclaw.agent.context_metadata.resolve_context_length', lambda _: 1_000_000)
    body = '完整远程消息🙂' * 10000 + 'TAIL'
    raw = json.dumps({'success': True, 'text': body, '_untrustedRemoteData': True}, ensure_ascii=False)
    calls = []

    class Transport:
        def call(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return ModelCallResult(content='', tool_calls=[{
                    'id': 'remote-result', 'type': 'function',
                    'function': {'name': 'dsoftbus_run_agent_task', 'arguments': '{}'},
                }], finish_reason='tool_calls', reasoning=None, usage=None, was_streamed=False,
                    provider='test', model='test')
            assert json.loads(kwargs['messages'][-1]['content'])['text'] == body
            return ModelCallResult(content='已基于完整正文完成分析', tool_calls=None, finish_reason='stop',
                                   reasoning=None, usage=None, was_streamed=False, provider='test', model='test')

        def close(self):
            return True

    monkeypatch.setattr('mclaw.agent.core.create_transport', lambda _: Transport())
    monkeypatch.setattr('mclaw.tools.dispatch.handle_function_calls', lambda **_: [raw])
    database = SessionDB(tmp_path / 'history.db')
    agent = MClaw(provider_runtime=parent().provider_runtime, session_db=database, session_id='analysis',
                  system_prompt='Test local analysis.', skip_memory=True, enabled_toolsets=[],
                  workspace=str(tmp_path), config={'checkpoints': {'enabled': False},
                                                    'compression': {'enabled': False}})
    agent.valid_tool_names = {'dsoftbus_run_agent_task', 'dsoftbus_read_task_result'}
    agent.tools = [registry._tools[name].schema for name in agent.valid_tool_names]
    try:
        result = agent.run_conversation('分析对端结果', advance_background_review=False)
        assert result['completed'] and result['final_response'] == '已基于完整正文完成分析'
        assert len(calls) == 2
        assert database.get_tool_result('analysis', 'remote-result')['content'] == raw
    finally:
        agent.close()
        database.close()
