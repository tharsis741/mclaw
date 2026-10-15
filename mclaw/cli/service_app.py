# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Existing terminal UI connected to the device-local application service."""

from __future__ import annotations

from datetime import datetime
import threading
import time

from mclaw.application.client import ServiceError
from mclaw.cli.app import InteractiveChat
from mclaw.cli.runtime.events import RuntimeStatus
from mclaw.cli.service_state import ServiceAgentView, ServiceHistory, ServiceTaskQueue


class ServiceInteractiveChat(InteractiveChat):
    """Own terminal presentation only: execution survives frontend detachment."""

    def __init__(self, *, service_client, provider_runtime, resume_session_id=None,
                 workspace_path=None, **kwargs):
        self.service_client = service_client
        self._view_guard = threading.RLock()
        self._monitor_stop = threading.Event()
        self._monitor_thread = None
        self._rendered = set()
        self._request_id = None
        self._view_attempt = None
        self._attached_id = None
        model = f"{provider_runtime.provider}::{provider_runtime.model}" if provider_runtime else ""
        state = service_client.attach(str(workspace_path or ""), model, resume_session_id or "")
        self._service_snapshot = state
        self._attached_id = state["id"]
        try:
            super().__init__(provider_runtime=provider_runtime, session_id=state["id"],
                             workspace_path=state["workspace"], **kwargs)
            # A view also exists in providerless device-management mode.
            if self.agent is None:
                self.agent = ServiceAgentView(self, provider_runtime)
            self._bind_task_queue()
            if provider_runtime and (state.get("model") != model or provider_runtime.auth_source == "explicit" or provider_runtime.base_url_source == "explicit"):
                state = self.service_client.runtime(state["id"], provider_runtime)
                self._service_snapshot = state
            self._seed_history(state)
        except BaseException:
            self._release_session_lock()
            raise

    def _open_session_database(self):
        return ServiceHistory(self.service_client)

    def _acquire_session_lock(self, identifier):
        self.service_client.session(identifier)
        self._attached_id = identifier

    def _release_session_lock(self):
        identifier, self._attached_id = self._attached_id, None
        if identifier:
            try:
                self.service_client.release(identifier)
            except ServiceError:
                pass  # The service expires the presentation lease independently.

    def _create_agent(self, context, session_id, **_kwargs):
        return ServiceAgentView(self, context)

    def _bind_task_queue(self):
        self.runtime.task_queue = ServiceTaskQueue(self)

    def _seed_history(self, state):
        self._rendered = {message["id"] for message in state.get("messages", [])
                          if message["role"] == "user" or message.get("result")
                          or str(message["id"]).startswith("history_")}
        self._rendered.update(entry["id"] for message in state.get("messages", [])
                              for entry in message.get("process", []) if message["id"] in self._rendered)
        self.agent.messages = self._session_db.get_messages_as_conversation(state["id"])
        self._resume_history = self.agent.messages
        self._update_usage(state)

    def _accept_snapshot(self, state):
        with self._view_guard:
            if state.get("id") != self.session_id:
                return
            if state.get("seq", 0) < self._service_snapshot.get("seq", 0):
                return
            self._service_snapshot = state

    def _update_usage(self, state):
        from types import SimpleNamespace

        view = state.get("runtime_view") or {}
        self.agent.context_compressor = SimpleNamespace(**view["context"]) if view.get("context") else None
        self.agent.valid_tool_names = set(view.get("tools", []))
        usage = state.get("session_usage") or state.get("usage") or {}
        for field, key in (("session_input_tokens", "input_tokens"), ("session_output_tokens", "output_tokens"),
                           ("session_cache_read_tokens", "cache_read_tokens"),
                           ("session_cache_write_tokens", "cache_write_tokens"),
                           ("session_reasoning_tokens", "reasoning_tokens")):
            setattr(self.agent, field, int(usage.get(key) or 0))
        self.agent.session_user_messages = sum(message["role"] == "user" and not message.get("invalidated")
                                               for message in state.get("messages", []))
        self.agent.session_api_calls = usage.get("api_calls") or sum((message.get("result") or {}).get("api_calls", 0)
                                                                    for message in state.get("messages", []))

    def _render_snapshot(self, state):
        """Translate shared state into the existing terminal renderers once."""
        running = state.get("status") in {"running", "waiting", "stopping"}
        active = state.get("active_task") or {}
        attempts = active.get("attempts") or []
        attempt = attempts[-1]["id"] if running and attempts else None
        if attempt and attempt != self._view_attempt:
            self.runtime_state.begin_turn()
            self._view_attempt = attempt
        self._agent_running = running
        for message in state.get("messages", []):
            if message.get("invalidated"):
                continue
            identifier = message["id"]
            if message["role"] == "user":
                if identifier not in self._rendered:
                    self._echo_user_message(message["content"])
                    self._rendered.add(identifier)
                continue
            for entry in message.get("process", []):
                if entry.get("kind") == "remote":
                    for progress in entry.get("progress", []):
                        key = f"{entry['id']}:progress:{progress['id']}"
                        if key not in self._rendered:
                            self._on_agent_event({"type": "assistant.message", "is_final": False,
                                                  "content": progress["content"], "content_source": "content",
                                                  "origin": "dsoftbus", "remote_device_id": entry.get("device_id"),
                                                  "remote_task_id": entry.get("task_id")})
                            self._rendered.add(key)
                if entry["id"] in self._rendered or entry.get("status") == "running":
                    continue
                if entry.get("kind") in {"reasoning", "progress"} and entry.get("content"):
                    self._on_agent_event({"type": "assistant.message", "is_final": False,
                                          "content": entry["content"],
                                          "content_source": "reasoning_content" if entry["kind"] == "reasoning" else "content"})
                self._rendered.add(entry["id"])
            result = message.get("result")
            self._render_delegations(message)
            if result is not None and identifier not in self._rendered:
                if message.get("content"):
                    self._render_response(message["content"])
                if result.get("error") or result.get("abort_message"):
                    self._get_runtime_renderer().error(result.get("error") or result["abort_message"])
                self._last_chat_result = result
                self.runtime_state.finish_turn(result)
                if not result.get("completed"):
                    self._get_runtime_renderer().warning("当前任务与队列已暂停；/continue 优先继续原任务，Alt+Enter 可补充并继续。")
                self._rendered.add(identifier)
            if identifier == attempt:
                self.runtime_state.stream_text = message.get("content", "")
                self.runtime_state.stream_started = bool(message.get("content"))
                self.runtime_state.active_tools = {entry["title"] for entry in message.get("process", [])
                                                    if entry.get("kind") == "tool" and entry.get("status") == "running"}
        if running:
            status = RuntimeStatus.WAITING_FOR_USER if state.get("pending_request") else (
                RuntimeStatus.TOOLS if self.runtime_state.active_tools else (
                    RuntimeStatus.STREAMING if self.runtime_state.stream_started else RuntimeStatus.REQUESTING))
            self.runtime_state.set_status(status, state.get("status_text") or "")
        self._update_usage(state)
        self._present_request(state.get("pending_request"))

    def _render_delegations(self, message):
        from mclaw.tools.delegate_tool import SubtaskEvent
        for identifier, group in message.get("delegations", {}).items():
            if identifier in self._rendered or message["id"] in self._rendered:
                continue
            if not self.subtask_manager or getattr(self.subtask_manager, "delegation_id", None) != identifier:
                self.subtask_manager = self.SubtaskManager(group.get("num_tasks", 0), group.get("goals", []))
                self.subtask_manager.delegation_id = identifier
            for event in group.get("events", []):
                key = f"{identifier}:{event['task_index']}:{event['timestamp']}:{event['event_type']}"
                if key not in self._rendered:
                    self._delegate_progress_callback(SubtaskEvent(event["task_index"], event["event_type"], event.get("data"),
                                                                 event["timestamp"], identifier))
                    self._rendered.add(key)
            if group.get("result"):
                self._render_aggregation(group["result"])
                self._rendered.add(identifier)
                self.subtask_manager = None

    def _present_request(self, request):
        identifier = (request or {}).get("id")
        if self._request_id == identifier:
            return
        self._request_id = identifier
        self._pending_secret_request = None
        self._pending_skill_import_confirmation = None
        if not request:
            return
        if request["kind"] == "secret":
            pending = {**request, "remote_request": identifier, "event": threading.Event(),
                       "resume_status": RuntimeStatus.REQUESTING, "resume_detail": ""}
            self._pending_secret_request = pending
            self._render_secret_request(pending)
        elif request["kind"] in {"workspace", "skill_install"}:
            self._pending_skill_import_confirmation = {"remote_request": request}
            details = request.get("description") or request.get("content") or request.get("summary") or ""
            if request["kind"] == "workspace":
                details = f"{request.get('workspace', '')}\n{details}"
            else:
                details = f"{request.get('skill_name', '')}\n{details}"
                if request.get("review"):
                    import json
                    details += "\n" + json.dumps(request["review"], ensure_ascii=False, indent=2)
            self._get_commands_renderer().render_notice(request.get("title", "需要确认"), details,
                                                        detail="输入 Y 确认，N 取消", kind="warning")
        else:
            self._get_runtime_renderer().warning(f"需要处理：{request.get('title', request['kind'])}")

    def _resolve_secret_request(self, response):
        pending = self._pending_secret_request or {}
        if pending.get("remote_request"):
            try:
                state = self.service_client.action(self.session_id, "reply", {"request_id": pending["remote_request"], "value": response})
                self._accept_snapshot(state)
            except ServiceError as exc:
                if exc.code != "REQUEST_EXPIRED":
                    raise
        super()._resolve_secret_request(response)

    def _handle_skill_import_confirmation_input(self, text):
        pending = self._pending_skill_import_confirmation or {}
        request = pending.get("remote_request")
        if not request:
            return super()._handle_skill_import_confirmation_input(text)
        answer = text.strip().lower()
        if answer not in {"y", "yes", "是", "确认", "n", "no", "否", "取消", "__mclaw_confirm_esc__"}:
            self._get_runtime_renderer().warning("请输入 Y 确认，或 N 取消。")
            return
        approved = answer in {"y", "yes", "是", "确认"}
        value = approved if request["kind"] == "workspace" else {
            "approved": approved, "confirmation_token": request.get("confirmation_token", "")}
        self._accept_snapshot(self.service_client.action(self.session_id, "reply", {"request_id": request["id"], "value": value}))
        self._pending_skill_import_confirmation = None

    def _invoke_skill(self, skill, user_intent, raw_text=""):
        name = str(skill.name)
        if user_intent.strip():
            self.runtime.task_queue.submit(user_intent, skills=[name])
        else:
            names = list(dict.fromkeys([*self._service_snapshot.get("draft_skills", []), name]))
            self._accept_snapshot(self.service_client.patch(self.session_id, {"draft_skills": names}))
            self._get_runtime_renderer().dim(f"已选择技能：{name}")
        return True

    def _apply_resume(self, identifier, context, history):
        with self._view_guard:
            state = self.service_client.session(identifier)  # Claim first; failure keeps current view intact.
            previous = self._attached_id
            try:
                if context and (context.auth_source == "explicit" or context.base_url_source == "explicit"):
                    state = self.service_client.runtime(identifier, context)
            except BaseException:
                if previous != identifier:
                    self.service_client.release(identifier)
                raise
            self.session_id = self._attached_id = identifier
            self.workspace_path = state["workspace"]
            self._service_snapshot = state
            self.agent = ServiceAgentView(self, context)
            self.pending_provider_runtime = context
            self._request_id = self._view_attempt = None
            self._pending_secret_request = self._pending_skill_import_confirmation = None
            self._agent_running = state.get("status") in {"running", "waiting", "stopping"}
            self.subtask_manager = None
            self._bind_task_queue()
            self._seed_history(state)
            if previous and previous != identifier:
                self.service_client.release(previous)

    def _new_session(self, clear_screen=True):
        context = self.provider_runtime
        model = f"{context.provider}::{context.model}" if context else ""
        state = self.service_client.attach(self.workspace_path, model)
        self._apply_resume(state["id"], context, [])
        self.session_start = datetime.now()
        self.runtime_state = self.runtime.session_state.__class__()
        self.runtime.session_state = self.runtime_state
        if clear_screen:
            from prompt_toolkit.shortcuts import clear
            clear()
            self._show_banner()

    def _refresh_dsoftbus_provider_runtime(self):
        pass  # Session model changes never overwrite the device-wide inbound provider.

    def _refresh_scheduler_provider_runtime(self):
        pass  # Scheduled jobs resolve their own provider in the service.

    def _get_scheduler_coordinator(self):
        from mclaw.cli.service_scheduler import coordinator
        if getattr(self, "_scheduler_coordinator", None) is None:
            self._scheduler_coordinator = coordinator(self)
        self._scheduler_coordinator.workspace = self.workspace_path
        return self._scheduler_coordinator

    def _file_command(self, command, arguments):
        from mclaw.cli.runtime.file_safety_commands import RuntimeFileSafetyCommandHooks
        result = self.service_client.request("POST", f"/sessions/{self.session_id}/changes/terminal_files",
                                             {"command": command, "arguments": arguments}, timeout=120)
        allowed = {name for name in RuntimeFileSafetyCommandHooks.__dataclass_fields__ if name.startswith("render_")}
        renderer = self._get_safety_renderer()
        for record in result.get("records", []):
            if record.get("method") in allowed:
                getattr(renderer, record["method"])(*record.get("args", []), **record.get("kwargs", {}))
        if result.get("session"):
            self._accept_snapshot(result["session"])

    def _handle_rollback_command(self, raw_args=""):
        self._file_command("rollback", raw_args)

    def _handle_checkpoints_command(self, raw_args=""):
        self._file_command("checkpoints", raw_args)

    def build_runtime_worker_hooks(self, *, invalidate, exit_ui, is_ui_running):
        from mclaw.cli.runtime.turns import RuntimeTurnCoordinator, RuntimeTurnHooks
        from mclaw.cli.runtime.workers import RuntimeWorkerHooks

        noop = lambda *_args: None
        coordinator = RuntimeTurnCoordinator(self.runtime, RuntimeTurnHooks(
            drain_pet_commands=self._drain_pet_commands, pump_background_watchers=noop,
            drain_background_completions=noop,
            has_pending_secret_request=lambda: bool(self._pending_secret_request),
            handle_secret_request=self._handle_secret_request_input,
            has_pending_skill_import_confirmation=lambda: bool(self._pending_skill_import_confirmation),
            handle_skill_import_confirmation=self._handle_skill_import_confirmation_input,
            has_pending_key_setup=lambda: bool(self._pending_key_setup),
            cancel_key_setup=lambda: setattr(self, "_pending_key_setup", None), complete_key_setup=self._complete_key_setup,
            render_key_input_cancelled=self._get_runtime_renderer().key_input_cancelled,
            should_skip_next_prompt=lambda: False, consume_skip_next_prompt=noop,
            echo_user_message=self._echo_user_message, echo_command=noop,
            dispatch_slash=self._dispatch_slash_input, is_ui_running=is_ui_running, exit_ui=exit_ui,
            begin_turn=noop, run_turn=noop, finish_turn=noop,
            queue_notice=self._get_runtime_renderer().warning, current_model=lambda: self.model,
            pump_scheduler=lambda: self._get_scheduler_coordinator().pump(),
            has_pending_scheduler_input=lambda: self._get_scheduler_coordinator().has_pending_input(),
            handle_scheduler_input=lambda text: self._get_scheduler_coordinator().handle_input(text),
        ))
        self._turn_coordinator = coordinator

        def monitor():
            last_error = None
            while not self._monitor_stop.is_set():
                try:
                    identifier = self.session_id
                    state = self.service_client.session(identifier)
                    with self._view_guard:
                        if identifier == self.session_id:
                            self._accept_snapshot(state)
                            self._render_snapshot(self._service_snapshot)
                    last_error = None
                    invalidate()
                except ServiceError as exc:
                    if str(exc) != last_error:
                        self._get_runtime_renderer().warning(str(exc))
                        last_error = str(exc)
                    if exc.code in {"SESSION_OCCUPIED", "SESSION_NOT_FOUND"}:
                        self.runtime.request_exit()
                        if is_ui_running():
                            exit_ui()
                        break
                except Exception:
                    self._get_runtime_renderer().warning("终端状态刷新失败，请重新连接查看任务进展。")
                    break
                self._monitor_stop.wait(0.25 if self._agent_running else 1.0)

        self._monitor_thread = threading.Thread(target=monitor, name="mclaw-tui-view", daemon=True)
        self._monitor_thread.start()

        def tick():
            self._anim_tick += 1
            self._spinner_idx = self._anim_tick % 10
            self._emit_status_snapshot()

        return RuntimeWorkerHooks(handle_input=coordinator.handle_input, on_idle=coordinator.on_idle,
                                  on_animation_tick=tick, on_error=lambda exc: self._get_runtime_renderer().error(str(exc)),
                                  invalidate=invalidate, animation_enabled=lambda: bool(self._live_status_animation),
                                  animation_interval=lambda running: 0.24 if running else 0.56)

    def _shutdown_runtime_threads(self, process_thread, anim_thread):
        from mclaw.cli.app import _clear_terminal_title

        self.runtime.request_exit()
        self._monitor_stop.set()
        for thread in (process_thread, anim_thread, self._monitor_thread):
            if thread and thread is not threading.current_thread():
                thread.join(timeout=2)
        if self._asr_service:
            self._cleanup_call(self._asr_service.stop)
        if self.pet:
            self._cleanup_call(self.pet.stop)
        self._restore_project_env()
        _clear_terminal_title()

    def _end_current_session(self, *_args, **_kwargs):
        return True  # Detachment is not task cancellation or history closure.
