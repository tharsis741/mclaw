# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import asyncio

import pytest

from mclaw.application.channel_outbound import drain_channel_outbound, handle_channel_outbound
from mclaw.application.files import FileService
from mclaw.application.store import ApplicationStore
from mclaw.channels.base import SendResult
from mclaw.channels.weixin.outbound_registry import register_weixin_outbound_target, unregister_weixin_outbound_targets_for_adapter


@pytest.mark.asyncio
async def test_channel_outbound_exact_session_uploaded_file_and_idempotent_result(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("MCLAW_HOME", str(home))
    calls = []
    class Adapter:
        async def send_file(self, chat_id, **kwargs):
            calls.append((chat_id, kwargs))
            return SendResult(True, message_id="sent-id")
    adapter = Adapter()
    register_weixin_outbound_target(session_id="session", adapter=adapter, chat_id="peer", loop=asyncio.get_running_loop())
    try:
        uploaded = FileService(home).upload("session", "note.txt", b"hello")
        request = {"id": "one", "platform": "weixin", "operation": "send_file", "file_path": uploaded["path"], "as_file": True}
        assert not (await handle_channel_outbound(home, "foreign", request))["success"]
        private = home / "config.yaml"
        private.write_text("secret: private", encoding="utf-8")
        assert not (await handle_channel_outbound(home, "session", request | {"file_path": str(private)}))["success"]
        other = FileService(home).upload("foreign", "note.txt", b"other")
        assert not (await handle_channel_outbound(home, "session", request | {"file_path": other["path"]}))["success"]
        result = await handle_channel_outbound(home, "session", request)
        assert result["success"] and result["message_id"] == "sent-id"
        assert await handle_channel_outbound(home, "session", request) == result
        assert len(calls) == 1 and calls[0][0] == "peer"
        assert calls[0][1]["force_file_attachment"]
    finally:
        unregister_weixin_outbound_targets_for_adapter(adapter)


@pytest.mark.asyncio
async def test_shutdown_records_unknown_send_before_registry_removed(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("MCLAW_HOME", str(home))
    started = asyncio.Event()
    finished = asyncio.Event()
    class Adapter:
        async def send_file(self, *args, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                finished.set()
    adapter = Adapter()
    register_weixin_outbound_target(session_id="session", adapter=adapter, chat_id="peer", loop=asyncio.get_running_loop())
    path = tmp_path / "send.txt"
    path.write_text("test", encoding="utf-8")
    request = {"id": "pending", "platform": "weixin", "operation": "send_file", "file_path": str(path)}
    try:
        task = asyncio.create_task(handle_channel_outbound(home, "session", request))
        await started.wait()
        assert await drain_channel_outbound(adapter)
        assert finished.is_set() and task.cancelled()
        store = ApplicationStore(home / "application.sqlite3")
        assert store.setting("channel_outbound:session:pending")["state"] == "unknown"
        store.close()
        assert (await handle_channel_outbound(home, "session", request))["completion_unknown"]
    finally:
        unregister_weixin_outbound_targets_for_adapter(adapter)
