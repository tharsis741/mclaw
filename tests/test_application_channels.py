# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import time

import pytest

from mclaw.application.channel_runner import PersistentChannelRouter, SharedChannelRunner
from mclaw.application.channels import ChannelService
from mclaw.application.sessions import ApplicationError, SessionService
from mclaw.application.setup_interaction import current_setup_interaction
from mclaw.channels.base import ChannelMessage, ChannelSource, SendResult
from mclaw.channels.weixin.session_router import WeixinSessionRouter
from mclaw.cli.runtime.session_lock import InteractiveSessionLock


class ControlledRunner:
    def __init__(self, session):
        self.calls = []
        self.results = asyncio.Queue()

    async def run(self, task, emit):
        self.calls.append(task)
        return await self.results.get()

    def send(self, command):
        if command["type"] == "stop":
            self.results.put_nowait({"interrupted": True, "completed": False})

    async def close(self):
        pass


class Runtime:
    def __init__(self, kind, config):
        self.kind = kind
        self.adapter = SimpleNamespace()
        self._running = False
        self.stopped = False
        self.event = asyncio.Event()
        self.runner = SimpleNamespace(restore_deliveries=lambda _adapter: None)
        self._stream_task = None

    async def start(self):
        self._running = True
        if self.kind == "weixin":
            await self.event.wait()
        else:
            self._stream_task = asyncio.create_task(self.event.wait())

    async def stop(self):
        self.stopped = True
        self._running = False
        self.event.set()
        if self._stream_task:
            await self._stream_task


@pytest.fixture
def services(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("MCLAW_HOME", str(home))
    for env in ("WEIXIN_ACCOUNT_ID", "WEIXIN_TOKEN", "WEIXIN_BASE_URL", "DINGTALK_CLIENT_ID", "DINGTALK_CLIENT_SECRET", "DINGTALK_ROBOT_CODE"):
        monkeypatch.setenv(env, "")
    sessions = SessionService(home, runner_factory=ControlledRunner)
    output = tmp_path / "output"
    output.mkdir()
    sessions.store.set_setting("output_root", str(output))
    channels = ChannelService(home, sessions, runtime_factory=Runtime)
    return channels, sessions


async def until(predicate):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(0.01)
    raise AssertionError("channel state did not settle")


async def configure(service, kind="weixin", enabled=True):
    creds = {"account_id": "fake-account", "token": "fake-token"} if kind == "weixin" else {
        "client_id": "fake-client", "client_secret": "fake-secret", "robot_code": "fake-robot"}
    return await service.update(kind, {"settings": {"enabled": enabled}, "credentials": creds})


async def close(channels, sessions):
    await channels.close()
    await sessions.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["weixin", "dingtalk"])
async def test_configuration_reuses_credentials_preserves_other_settings_and_controls_lifecycle(services, kind):
    channels, sessions = services
    try:
        from mclaw.cli.config import save_config, load_raw_user_config
        save_config({"model": "existing", "custom_value": {"keep": True}})
        await configure(channels, kind)
        await until(lambda: channels.get(kind)["status"] == "running")
        first = channels.runtimes[kind]
        public = channels.get(kind)
        assert public["configured"] and all(public["credentials"].values())
        assert "fake-token" not in json.dumps(public)
        assert "fake-secret" not in json.dumps(public)
        await channels.update(kind, {"settings": {"dm_policy": "allowlist", "allowed_users": ["peer"]}, "credentials": {}})
        assert first.stopped
        assert load_raw_user_config()["custom_value"] == {"keep": True}
        assert channels.get(kind)["settings"]["allowed_users"] == ["peer"]
        active = channels.runtimes[kind]
        await channels.disconnect(kind)
        assert active.stopped and channels.get(kind)["status"] == "stopped"
        assert channels.get(kind)["enabled"]  # disconnect is separate from next-start preference
    finally:
        await close(channels, sessions)


@pytest.mark.asyncio
async def test_missing_credentials_and_invalid_settings_fail_before_writes(services):
    channels, sessions = services
    try:
        with pytest.raises(ApplicationError) as failure:
            await channels.connect("weixin")
        assert failure.value.code == "CHANNEL_NOT_CONFIGURED"
        assert channels.get("weixin")["status"] == "error"
        original = (channels.home / "config.yaml").read_bytes() if (channels.home / "config.yaml").exists() else None
        for settings in ({"enabled": "true"}, {"enabled": True}, {"dm_policy": "wrong"}, {"poll_timeout_ms": -1}):
            with pytest.raises(ApplicationError):
                await channels.update("weixin", {"settings": settings})
        assert ((channels.home / "config.yaml").read_bytes() if (channels.home / "config.yaml").exists() else None) == original
        assert not channels.diagnose("weixin")["ready"]
    finally:
        await close(channels, sessions)


@pytest.mark.asyncio
async def test_auto_start_and_failure_do_not_claim_success_or_cancel_other_owner(services):
    channels, sessions = services
    from mclaw.channels.weixin.runtime_lock import WeixinRuntimeLockError
    await configure(channels, enabled=True)
    await channels.disconnect("weixin")
    class Occupied(Runtime):
        async def start(self):
            raise WeixinRuntimeLockError("sensitive diagnostic should not leak")
    channels.runtime_factory = Occupied
    try:
        await channels.start()
        await until(lambda: channels.get("weixin")["status"] == "error")
        assert "其他入口" in channels.get("weixin")["error"]
        assert "sensitive" not in str(channels.list())
        assert not channels.runtimes["weixin"]._running
    finally:
        await close(channels, sessions)


@pytest.mark.asyncio
async def test_qr_cancel_prevents_credential_save_and_does_not_replace_existing_credentials(services):
    channels, sessions = services
    from mclaw.cli.config import get_env_value, save_env_value
    save_env_value("WEIXIN_TOKEN", "old-login")
    def login():
        interaction = current_setup_interaction.get()
        interaction.show_qr("https://example.invalid/qr")
        while True:
            interaction.check_cancelled()
            time.sleep(0.005)
    channels.login_function = login
    try:
        first = channels.login()
        await until(lambda: channels.login_status(first["id"])["qr_code"])
        assert channels.login()["id"] == first["id"]
        channels.login_status(first["id"], cancel=True)
        await asyncio.to_thread(channels._login.thread.join, 1)
        assert channels.login_status(first["id"])["status"] == "cancelled"
        assert get_env_value("WEIXIN_TOKEN") == "old-login"
    finally:
        await close(channels, sessions)


def new_channel_session(sessions, identifier="channel-test"):
    sessions.history.create_session(identifier, source="weixin")
    return identifier


def message(text, identifier, *, token=""):
    return ChannelMessage(text, ChannelSource("weixin", "peer", message_id=identifier, account_id="account"),
                          raw_message={"context_token": token} if token else {})


def shared(sessions):
    return SharedChannelRunner(sessions, platform="weixin", account_id="account", config={},
                                toolsets=["mclaw-required"], inbound_pipeline=lambda item: item)


@pytest.mark.asyncio
async def test_shared_ingress_persists_fifo_and_keeps_route_credentials_out_of_public_session(services):
    channels, sessions = services
    runner = shared(sessions)
    sid = new_channel_session(sessions)
    replies = []
    async def reply(msg, result):
        replies.append((msg.text, result.final_response))
        return SendResult(True)
    first = asyncio.create_task(runner.handle_message(message=message("一", "one", token="PRIVATE-ROUTE"), session_id=sid, reply_callback=reply))
    try:
        await until(lambda: sid in sessions.runners)
        worker = sessions.runners[sid]
        second = await runner.handle_message(message=message("二", "two"), session_id=sid, reply_callback=reply)
        assert second.queued
        assert [task["content"] for task in sessions.snapshot(sid)["queue"]] == ["二"]
        assert "PRIVATE-ROUTE" not in str(sessions.snapshot(sid))
        worker.results.put_nowait({"completed": True, "final_response": "第一条完成"})
        await first
        await until(lambda: len(worker.calls) == 2)
        worker.results.put_nowait({"completed": True, "final_response": "第二条完成"})
        await until(lambda: len(replies) == 2)
        assert [entry[0] for entry in replies] == ["一", "二"]
        await runner.handle_message(message=message("二", "two"), session_id=sid, reply_callback=reply)
        assert len(worker.calls) == 2
    finally:
        await runner.dispose_all()
        await close(channels, sessions)


@pytest.mark.asyncio
async def test_channel_control_respects_browser_lease_and_stop_retains_durable_work(services):
    channels, sessions = services
    runner = shared(sessions)
    sid = new_channel_session(sessions)
    async def reply(*args):
        return SendResult(True)
    first = asyncio.create_task(runner.handle_message(message=message("原任务", "one"), session_id=sid, reply_callback=reply))
    try:
        await until(lambda: sid in sessions.runners)
        sessions.claim(sid, "browser")
        await runner.handle_message(message=message("排队", "two"), session_id=sid, reply_callback=reply)
        assert not runner.interrupt(sid)
        assert "另一个" in runner.control_error(sid)
        assert not sessions.snapshot(sid)["paused"]
        sessions.release(sid, "browser")
        assert runner.interrupt(sid)
        await first
        await until(lambda: sessions.snapshot(sid)["status"] == "interrupted")
        snapshot = sessions.snapshot(sid)
        assert snapshot["active_task"]["content"] == "原任务" and snapshot["paused"]
        assert snapshot["queue"][0]["content"] == "排队"
    finally:
        await runner.dispose_all()
        await close(channels, sessions)


@pytest.mark.asyncio
async def test_new_channel_binding_is_safe_but_old_workspace_and_terminal_lock_are_respected(services):
    channels, sessions = services
    try:
        sid = new_channel_session(sessions)
        snapshot = sessions.prepare_background_session(sid)
        assert Path(snapshot["workspace"]).is_dir()
        assert sid not in sessions.owners
        assert sessions.history.get_session(sid)["workspace"] == snapshot["workspace"]
        old = new_channel_session(sessions, "old")
        sessions.history.append_message(old, "user", "已存在的历史")
        with pytest.raises(ApplicationError) as failure:
            sessions.prepare_background_session(old)
        assert failure.value.code == "WORKSPACE_UNKNOWN"
        locked = new_channel_session(sessions, "locked")
        lock = InteractiveSessionLock(locked, sessions.home / "sessions" / "locks")
        lock.acquire()
        try:
            with pytest.raises(ApplicationError) as failure:
                sessions.prepare_background_session(locked)
            assert failure.value.code == "SESSION_OCCUPIED"
        finally:
            lock.release()
    finally:
        await close(channels, sessions)


@pytest.mark.asyncio
async def test_new_session_route_persists_and_other_account_receipts_stay_isolated(services):
    channels, sessions = services
    try:
        source = ChannelSource("weixin", "peer", account_id="account")
        router = PersistentChannelRouter(WeixinSessionRouter(account_id="account", session_db=sessions.history), sessions.store, "weixin")
        original = router.route(source).session_id
        fresh = router.new_session(source).session_id
        restored = PersistentChannelRouter(WeixinSessionRouter(account_id="account", session_db=sessions.history), sessions.store, "weixin")
        assert fresh != original and restored.route(source).session_id == fresh
        sessions.store.set_setting("channel_pending:weixin", {"foreign-task": {"session_id": "foreign", "source": {"account_id": "other"}}})
        runner = shared(sessions)
        assert runner._receipts() == {}
        assert runner.interrupt_all() == 0
        assert "foreign-task" in sessions.store.setting("channel_pending:weixin")
    finally:
        await close(channels, sessions)


@pytest.mark.asyncio
@pytest.mark.parametrize("web_active", [False, True])
async def test_gateway_shutdown_retains_channel_queue_and_browser_lease(services, web_active):
    channels, sessions = services
    runner = shared(sessions)
    sid = new_channel_session(sessions)
    async def reply(*args):
        return SendResult(True)
    try:
        sessions.prepare_background_session(sid)
        sessions.claim(sid, "browser")
        if web_active:
            sessions.submit(sid, "browser", "浏览器任务", "web-first")
            await until(lambda: sid in sessions.runners)
        first = asyncio.create_task(runner.handle_message(message=message("渠道原任务", "one"), session_id=sid, reply_callback=reply))
        await until(lambda: sid in sessions.runners and runner._receipts())
        await runner.handle_message(message=message("渠道下一条", "two"), session_id=sid, reply_callback=reply)
        original_owner = sessions.owners[sid]
        assert runner.interrupt_all() == 2
        await runner.dispose_all()
        await first
        if not web_active:
            await until(lambda: sessions.snapshot(sid)["status"] == "interrupted")
        snapshot = sessions.snapshot(sid)
        assert snapshot["paused"]
        assert sessions.owners[sid] == original_owner
        if web_active:
            assert snapshot["status"] == "running"
            assert snapshot["active_task"]["content"] == "浏览器任务"
            assert [item["content"] for item in snapshot["queue"]] == ["渠道原任务", "渠道下一条"]
        else:
            assert snapshot["active_task"]["content"] == "渠道原任务"
            assert [item["content"] for item in snapshot["queue"]] == ["渠道下一条"]
    finally:
        await runner.dispose_all()
        await close(channels, sessions)


@pytest.mark.asyncio
async def test_standalone_channel_obeys_web_lock_and_refreshes_history(services, monkeypatch):
    from mclaw.channels.runner import AgentRunner
    channels, sessions = services
    sid = new_channel_session(sessions)
    runner = AgentRunner(provider_runtime=SimpleNamespace(model="fake"), session_db=sessions.history, platform="weixin")
    calls = []
    class Agent:
        def run_conversation(self, **kwargs):
            calls.append(kwargs)
            return {"completed": True, "final_response": "完成"}
    monkeypatch.setattr(runner, "_get_or_create_agent", lambda **kwargs: Agent())
    try:
        sessions.prepare_background_session(sid)
        occupied = await runner._run_single_turn(message=message("请求", "first"), session_id=sid, conversation_history=[])
        assert "其他 M-Claw" in occupied.error and not calls
        await sessions.close()
        sessions.history = __import__("mclaw.state", fromlist=["SessionDB"]).SessionDB(channels.home / "state.db")
        runner.session_db = sessions.history
        sessions.history.append_message(sid, "user", "其他入口保存的新历史")
        result = await runner._run_single_turn(message=message("请求", "next"), session_id=sid, conversation_history=[{"role": "user", "content": "陈旧历史"}])
        assert result.final_response == "完成"
        assert calls[0]["conversation_history"][-1]["content"] == "其他入口保存的新历史"
        lock = InteractiveSessionLock(sid)
        lock.acquire()
        lock.release()
    finally:
        sessions.history.close()
        await channels.close()


@pytest.mark.asyncio
async def test_standalone_channel_lock_outlives_outstanding_tools(services, monkeypatch):
    import threading
    from mclaw.channels.runner import AgentRunner
    from mclaw.cli.runtime.session_lock import InteractiveSessionLockError
    channels, sessions = services
    sid = new_channel_session(sessions)
    runner = AgentRunner(provider_runtime=SimpleNamespace(model="fake"), session_db=sessions.history, platform="weixin")
    drained = threading.Event()
    class Agent:
        _turn_workers_drained = drained
        def _has_outstanding_turn_workers(self):
            return not drained.is_set()
        def run_conversation(self, **kwargs):
            return {"completed": False, "interrupted": True}
    monkeypatch.setattr(runner, "_get_or_create_agent", lambda **kwargs: Agent())
    lock = InteractiveSessionLock(sid)
    try:
        await runner._run_single_turn(message=message("请求", "first"), session_id=sid, conversation_history=[])
        with pytest.raises(InteractiveSessionLockError):
            lock.acquire()
        drained.set()
        await until(lambda: sid not in runner._closing_sessions)
        lock.acquire()
        lock.release()
    finally:
        drained.set()
        await close(channels, sessions)
