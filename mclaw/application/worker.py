# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Private NDJSON worker entrypoint; one process owns one Session's tools."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

_CONTINUE_MESSAGE = (
    "继续处理本会话中尚未完成的原任务。依据已保存的对话、工具结果与当前实际状态接续，"
    "先核对中断时结果未知的操作；已确认完成的操作不要重复执行。"
)


def resolve_task_provider(task: dict, config: dict):
    """Resolve a UI provider/model selector or an existing history snapshot."""
    from mclaw.providers.resolver import resolve_provider_runtime_context, restore_provider_runtime_context

    temporary = task.get("_runtime_context")
    if temporary:
        return restore_provider_runtime_context(temporary["snapshot"], config=config,
                                                api_key=temporary.get("api_key", ""), base_url=temporary.get("base_url", ""))
    model = str(task.get("model") or "")
    if "::" in model:
        provider, model = model.split("::", 1)
        return resolve_provider_runtime_context(provider=provider, model=model, config=config)
    snapshot = task.get("provider_snapshot") or {}
    if snapshot:
        return restore_provider_runtime_context(snapshot, config=config, model=model)
    return resolve_provider_runtime_context(model=model, config=config)


class SessionWorker:
    """Bridge Agent callbacks and blocking authorization to an owning service."""

    def __init__(self, home: Path, session: dict, emit: Callable[[dict], None]) -> None:
        self.home = Path(home).resolve()
        self.session = dict(session)
        self.workspace = Path(session["workspace"]).resolve()
        self.emit_raw = emit
        self.agent = None
        self.db = None
        self.run_id = ""
        self.cancel = threading.Event()
        self.shutdown = threading.Event()
        self.commands: queue.Queue = queue.Queue()
        self._condition = threading.Condition()
        self._supplements: dict[str, dict] = {}
        self._applied: set[str] = set()
        self._waiting: dict[str, object] = {}
        from mclaw.application.process_events import secret_values
        self._secret_values: set[str] = secret_values(self.home)
        self._base_environment = os.environ.copy()
        self._loaded_env: set[str] = set()
        self._processes = None
        self._current_task: dict = {}
        self._execution_signature = None
        self._tool_names: list[str] = []
        self._changes = None

    def emit(self, event: dict) -> None:
        from mclaw.runtime.secrets import redact_secret_values

        event = redact_secret_values(event, self._secret_values)
        self.emit_raw({**event, "run_id": self.run_id})

    def control(self, command: dict) -> None:
        """Handle controls off the Agent thread, including blocked prompts."""
        kind = command.get("type")
        with self._condition:
            if kind == "run":
                cancel = threading.Event()
                self.cancel = cancel
                self.run_id = str(command["run_id"])
                task = command.get("task") or {}
                self._supplements = {
                    item["id"]: dict(item) for item in task.get("supplements", [])
                    if item.get("id") and not item.get("applied")
                }
                self.commands.put((task, cancel))
            elif kind == "maintenance":
                self.cancel = threading.Event()
                self.run_id = str(command["run_id"])
                self.commands.put(({"_maintenance": command.get("request") or {}}, self.cancel))
            elif kind == "steer":
                identifier = str(command.get("id") or "")
                if identifier and "supplement:" + identifier not in self._applied:
                    self._supplements.setdefault(identifier, dict(command))
            elif kind == "reply":
                identifier = str(command.get("request_id") or "")
                if identifier in self._waiting:
                    self._waiting[identifier] = command.get("value")
            elif kind == "device_event":
                self._agent_event({**(command.get("event") or {}), "origin": "dsoftbus"})
            elif kind in {"stop", "shutdown"}:
                self.cancel.set()
                if self.agent is not None:
                    self.agent.interrupt()
                if kind == "shutdown":
                    self.shutdown.set()
                    self.commands.put(None)
            self._condition.notify_all()

    def request(self, kind: str, *, _cancel_event=None, **fields):
        from mclaw.tools.cancellation import cancellation_checkpoint

        identifier = "request_" + uuid.uuid4().hex
        pending = object()
        with self._condition:
            cancellation_checkpoint(self.cancel)
            self._waiting[identifier] = pending
            self.emit({"type": "request", "request": {"id": identifier, "kind": kind, **fields}})
            try:
                sent_cancel = False
                while self._waiting[identifier] is pending:
                    if kind == "device_rpc":
                        cancelled = self.cancel.is_set() or (_cancel_event is not None and _cancel_event.is_set())
                        if cancelled and not sent_cancel:
                            self.emit({"type": "device_cancel", "request_id": identifier})
                            sent_cancel = True
                        # Native cancellation must finish before this tool returns.
                    else:
                        cancellation_checkpoint(self.cancel)
                    self._condition.wait(timeout=0.1)
                if kind != "device_rpc":
                    cancellation_checkpoint(self.cancel)
                return self._waiting[identifier]
            finally:
                self._waiting.pop(identifier, None)
                self.emit({"type": "request_resolved", "request_id": identifier})

    def secret_request(self, required_for: str, needs: list[dict]) -> dict:
        response = self.request("secret", title="凭据授权", required_for=required_for, needs=needs)
        if not isinstance(response, dict):
            return {"values": {}, "authorized": [], "skipped": [item["env_var"] for item in needs]}
        allowed = {item["env_var"] for item in needs}
        values = response.get("values") if isinstance(response.get("values"), dict) else {}
        authorized = response.get("authorized") or []
        skipped = response.get("skipped") or []
        authorized = [authorized] if isinstance(authorized, str) else authorized
        skipped = [skipped] if isinstance(skipped, str) else skipped
        result = {
            "values": {key: str(value) for key, value in values.items() if key in allowed},
            "authorized": [key for key in authorized if key in allowed] if isinstance(authorized, list) else [],
            "skipped": [key for key in skipped if key in allowed] if isinstance(skipped, list) else [],
        }
        self._secret_values.update(result["values"].values())
        return result

    def _ensure_workspace_trusted(self, config: dict, platform: str = "web") -> None:
        from mclaw.application.workspaces import is_managed_background_workspace
        from mclaw.cli.config import load_config
        from mclaw.cli.runtime.workspace_trust import ensure_workspace_trusted, is_workspace_trusted
        from mclaw.system.lock import file_lock
        from mclaw.tools.cancellation import cancellation_checkpoint

        if is_workspace_trusted(load_config(strict=True), self.workspace):
            return
        allowed = is_managed_background_workspace(self.session, platform) or self.request(
            "workspace", title="信任此工作目录", workspace=str(self.workspace),
            description="M-Claw 将在此目录执行任务，并可按任务需要读取、修改文件和运行命令。",
        )
        cancellation_checkpoint(self.cancel)
        if allowed is not True:
            raise PermissionError("工作目录尚未获得信任，任务已保留")
        # Reload after the user answers, preserving trust entries saved by other
        # sessions while this request was pending.
        with file_lock(self.home / "config.lock"):
            cancellation_checkpoint(self.cancel)
            ensure_workspace_trusted(self.workspace, config=config, prompt=lambda _path: True)

    def _load_configuration(self) -> dict:
        from mclaw.cli.config import load_merged_config
        from mclaw.cli.env_loader import load_mclaw_dotenv

        for key in self._loaded_env:
            if key in self._base_environment:
                os.environ[key] = self._base_environment[key]
            else:
                os.environ.pop(key, None)
        before = os.environ.copy()
        load_mclaw_dotenv(mclaw_home=self.home)
        self._loaded_env = {key for key, value in os.environ.items() if before.get(key) != value}
        config = load_merged_config(cwd=self.workspace)
        config["_launch_cwd"] = str(self.workspace)
        return config

    def _prepare_agent(self, task: dict):
        from mclaw.agent.core import MClaw
        from mclaw.state import SessionDB
        from mclaw.tools import process_registry as processes, terminal_tool

        config = self._load_configuration()
        from mclaw.application.process_events import secret_values
        self._secret_values.update(secret_values(self.home))
        execution = task.get("execution") or {}
        toolsets = execution.get("enabled_toolsets") or config.get("toolsets", ["mclaw-required"])
        from mclaw.dsoftbus.active import get_active_runtime, install_active_runtime, clear_active_runtime
        from mclaw.application.device_bridge import WorkerDeviceClient
        toolsets = [name for name in toolsets if name not in {"dsoftbus", "dsoftbus-remote"}]
        if task.get("_device_service"):
            if not isinstance(get_active_runtime(), WorkerDeviceClient):
                install_active_runtime(WorkerDeviceClient(self.request))
            toolsets.append("dsoftbus")
        elif isinstance(get_active_runtime(), WorkerDeviceClient):
            clear_active_runtime(get_active_runtime())
        platform = execution.get("platform") or "web"
        if platform in {"weixin", "dingtalk"}:
            if execution.get("channel_toolsets"):
                toolsets = config.get("channels", {}).get(platform, {}).get("toolsets") or toolsets
            from mclaw.application.channel_outbound import install_channel_outbound
            install_channel_outbound(self)
        maximum = execution.get("max_iterations")
        signature = (tuple(toolsets), platform, maximum)
        if self.agent is not None and self._execution_signature != signature:
            self._drain_tools()
            if not self.agent.close(deadline=time.monotonic() + 5):
                raise RuntimeError("Previous task resources have not finished closing")
            self.agent = None
        context = resolve_task_provider(task, config)
        self._secret_values.add(context.api_key)
        self._ensure_workspace_trusted(config, platform)
        if self.db is None:
            self.db = SessionDB(self.home / "state.db")
        if self._processes is None:
            namespace = hashlib.sha256(self.session["id"].encode("utf-8")).hexdigest()
            processes.CHECKPOINT_PATH = self.home / "processes" / f"{namespace}.json"
            processes.CHECKPOINT_PATH.parent.mkdir(parents=True, exist_ok=True)
            self._processes = processes.process_registry
            self._processes.recover_from_checkpoint()
        terminal_tool.set_current_session(self.session["id"])
        if self.agent is None:
            self.agent = MClaw(
                provider_runtime=context, session_db=self.db, session_id=self.session["id"],
                workspace=str(self.workspace), platform=platform, config=config,
                enabled_toolsets=toolsets, max_iterations=maximum,
                stream_callback=lambda text: self.emit({"type": "delta", "text": text}),
                tool_callback=self._tool_started,
                status_callback=self._status,
                event_callback=self._agent_event,
                print_fn=lambda *_args, **_kwargs: None,
            )
            self.agent.secret_request_callback = self.secret_request
            self.agent._delegate_live_callback = self._delegation_event
            self._execution_signature = signature
            self.db.update_model_config(self.session["id"], model=context.model, model_config=context.snapshot())
        else:
            self.agent.config = config
            self.agent._discover_tools()
            if self.agent.provider_runtime.fingerprint() != context.fingerprint():
                self.agent.switch_model(context)
            self.agent.mark_prompt_epoch_dirty()
        self.agent.messages = self.db.get_messages_as_conversation(self.session["id"])
        self._applied = {
            str(row.get("operation_id"))
            for row in self.db.get_messages(self.session["id"], include_invalidated=True)
            if str(row.get("operation_id") or "").startswith(("supplement:", "skill:"))
        }
        self.emit({"type": "runtime_view", "view": self._runtime_view()})
        return self.agent

    def _runtime_view(self) -> dict:
        compressor = getattr(self.agent, "context_compressor", None)
        return {"tools": sorted(getattr(self.agent, "valid_tool_names", ())),
                "context": {name: getattr(compressor, name, None) for name in (
                    "context_length", "threshold_tokens", "display_context_tokens", "display_context_estimated")}
                if compressor is not None else None}

    def _agent_event(self, event: dict) -> None:
        from mclaw.application.process_events import agent_events

        for record in agent_events(event, self.run_id, self._secret_values):
            self.emit(record)

    def _delegation_event(self, event) -> None:
        from mclaw.application.process_events import agent_events, public_detail

        base = {"type": "delegation", "delegation_id": event.delegation_id,
                "task_index": event.task_index, "timestamp": event.timestamp}
        if event.event_type == "activity":
            scope = f"{self.run_id}_{event.delegation_id}_{event.task_index}"
            for record in agent_events(event.data, scope, self._secret_values):
                self.emit({**base, "event_type": "activity", "data": record})
        else:
            self.emit({**base, "event_type": event.event_type,
                       "data": public_detail(event.data, self._secret_values)})

    def _status(self, text: str) -> None:
        from mclaw.application.process_events import status_text

        self.emit({"type": "status", "text": status_text(str(text))})

    def _tool_started(self, name: str, _arguments) -> None:
        self._tool_names.append(str(name))

    def _apply_supplements(self, messages: list[dict], turn_id: str) -> None:
        """Persist once before acknowledging, at the Agent's model boundary."""
        from mclaw.application.task_queue import apply_pending_supplement
        from mclaw.application.skills import apply_skill_snapshots
        from mclaw.tools.cancellation import cancellation_checkpoint

        cancellation_checkpoint(self.cancel)
        apply_skill_snapshots(self.db, self.session["id"], messages, turn_id, self._current_task, self._applied)
        with self._condition:
            pending = list(self._supplements.values())
        for item in pending:
            cancellation_checkpoint(self.cancel)
            identifier = item["id"]
            apply_pending_supplement(
                self.db, self.session["id"], messages, turn_id, item, self._applied,
                parent_agent=self.agent, cancel_event=self.cancel,
            )
            with self._condition:
                self._supplements.pop(identifier, None)
            self.emit({"type": "supplement_applied", "id": identifier})

    def _drain_tools(self) -> None:
        if self.agent is None:
            return
        workers = getattr(self.agent, "_turn_workers_drained", None)
        if workers is not None and not workers.is_set():
            self.emit({"type": "progress", "title": "正在停止", "text": "等待工具完成收尾"})
            workers.wait()

    def _terminate_processes(self, *, task_id: str | None = None) -> None:
        if self._processes is None:
            return
        while True:
            report = self._processes.terminate_scope(task_id=task_id, session_key=self.session["id"])
            if report.get("termination_confirmed"):
                return
            self.emit({"type": "progress", "title": "正在停止", "text": "等待所属进程确认退出"})
            time.sleep(0.5)

    def _resolve_skill_install(self, confirmation: dict) -> bool:
        """Resolve one displayed draft, then persist the decision for continuation."""
        from mclaw.application.skills import SkillService
        from mclaw.prompts.skills import build_skill_import_confirmation_context

        skills = SkillService(self.home)
        drafting_id = str(confirmation["drafting_id"])
        root = skills._draft_root(drafting_id)
        approved = True
        if not root.exists():
            # A crash after promotion but before recording the runtime decision
            # can only replay the already-committed result for this exact token.
            result = skills.confirm(drafting_id, str(confirmation.get("confirmation_token") or ""))
            approved = result.get("action") == "enable_drafting"
        else:
            detail = skills.draft_detail(drafting_id)
            value = self.request("skill_install", title="确认安装技能", **detail)
            approved = isinstance(value, dict) and value.get("approved") is True
            if approved:
                result = skills.confirm(drafting_id, str(value.get("confirmation_token") or ""))
            else:
                result = skills.cancel(drafting_id)
        action = "enable_drafting" if approved else "cancel_drafting"
        context = build_skill_import_confirmation_context(action, confirmation, result)
        operation = "skill-install:" + drafting_id
        if not any(row.get("operation_id") == operation for row in self.db.get_messages(self.session["id"], include_invalidated=True)):
            self.db.append_message(self.session["id"], "system", content=context,
                                   turn_id=getattr(self.agent, "_checkpoint_turn_id", None), operation_id=operation)
        self.emit({"type": "skill_install_resolved", "drafting_id": drafting_id})
        self.agent.messages = self.db.get_messages_as_conversation(self.session["id"])
        self.agent.mark_prompt_epoch_dirty()
        return approved

    def execute(self, task: dict, cancel: threading.Event) -> dict:
        from mclaw.application.files import FileService, attachment_context
        from mclaw.application.process_events import public_detail
        from mclaw.application.sessions import ApplicationError
        from mclaw.cli.config import ConfigError
        from mclaw.providers.resolver import ProviderResolutionError
        from mclaw.tools.cancellation import cancellation_checkpoint
        from mclaw.tools.dispatch import reset_current_task_id, set_current_task_id
        from mclaw.tools.vision.types import VisionInputError

        token = set_current_task_id(str(task.get("id") or ""))
        self._current_task = task
        self._tool_names = []
        try:
            cancellation_checkpoint(cancel)
            agent = self._prepare_agent(task)
            cancellation_checkpoint(cancel)
            if task.get("pending_skill_install"):
                if not self._resolve_skill_install(task["pending_skill_install"]):
                    return {"completed": False, "interrupted": True}
            input_operation_id = "task:" + str(task["id"])
            continuing = any(
                row.get("operation_id") == input_operation_id
                for row in self.db.get_messages(self.session["id"], include_invalidated=True)
            )
            attachments = FileService(self.home).task_inputs(self.session["id"], task["attachments"]) if (
                not continuing and task.get("attachments")) else []
            message = _CONTINUE_MESSAGE if continuing else str(task.get("content") or "") + attachment_context(attachments)
            result = agent.run_conversation(
                message, conversation_history=agent.messages, cancel_event=cancel,
                before_model_callback=self._apply_supplements,
                input_operation_id=None if continuing else input_operation_id,
                extra_system=str((task.get("execution") or {}).get("extra_system") or ""),
                **({"image_paths": [item["path"] for item in attachments if str(item.get("mime_type", "")).startswith("image/")]}
                   if any(str(item.get("mime_type", "")).startswith("image/") for item in attachments) else {}),
            )
            while result.get("pending_skill_import_confirmation"):
                self._drain_tools()
                cancellation_checkpoint(cancel)
                if not self._resolve_skill_install(result["confirmation_data"]):
                    return {"completed": False, "interrupted": True}
                cancellation_checkpoint(cancel)
                result = agent.run_conversation(
                    "依据已记录的技能安装确认结果继续处理原任务。",
                    conversation_history=agent.messages, cancel_event=cancel,
                    before_model_callback=self._apply_supplements,
                    extra_system=str((task.get("execution") or {}).get("extra_system") or ""),
                )
            allowed = {"final_response", "model", "session_id", "api_calls", "interrupted",
                       "abort_reason", "abort_message", "stop_reason", "completed", "token_usage",
                       "skills_changed", "pending_skill_import_confirmation"}
            return {**{key: value for key, value in result.items() if key in allowed},
                    "runtime_view": self._runtime_view(),
                    "session_usage": {name: getattr(agent, "session_" + name, 0) for name in (
                        "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens", "api_calls")},
                    "turn_id": getattr(agent, "_checkpoint_turn_id", None),
                    "tool_calls": [{"name": name} for name in self._tool_names]}
        except InterruptedError:
            return {"completed": False, "interrupted": True}
        except ProviderResolutionError as exc:
            messages = {
                "missing_credential": "模型凭据不可用，请在设置中配置有效凭据",
                "missing_model": "未配置可用模型，请在设置中选择模型",
                "provider_required": "未配置可用供应商，请先完成设置",
                "invalid_snapshot": "会话模型记录无法恢复，请重新选择模型",
                "unknown_provider": "此会话的模型供应商已不可用，请重新选择模型",
            }
            return {"completed": False, "error": messages.get(exc.code, "模型配置不可用，请检查设置"),
                    "error_code": exc.code, "settings_required": True}
        except ConfigError:
            return {"completed": False, "error": "配置文件无法加载，请检查设置", "settings_required": True}
        except PermissionError:
            return {"completed": False, "error": "工作目录尚未获得信任，任务已保留"}
        except (ApplicationError, VisionInputError) as exc:
            return {"completed": False, "error": public_detail(str(exc), self._secret_values),
                    "error_code": exc.code}
        except Exception:
            logging.getLogger(__name__).exception("[WORKER] task failed")
            return {"completed": False, "error": "任务执行失败，请检查执行过程并核对已知结果"}
        finally:
            self._drain_tools()
            if cancel.is_set():
                self._terminate_processes(task_id=str(task.get("id") or ""))
            reset_current_task_id(token)

    def maintain(self, request: dict) -> dict:
        from mclaw.application.changes import ChangeService
        from mclaw.application.sessions import ApplicationError
        from mclaw.cli.config import load_config
        from mclaw.cli.runtime.workspace_trust import is_workspace_trusted
        from mclaw.safety.operation_journal import OperationJournal
        from mclaw.safety.rollback_coordinator import RollbackCoordinator
        from mclaw.state import SessionDB
        from mclaw.tools.checkpoint_manager import CheckpointManager

        try:
            action = request.get("operation", "list")
            if action not in {"list", "status", "preview", "apply", "trust", "terminal_files"}:
                raise ApplicationError("INVALID_RECOVERY", "恢复操作无效")
            config = self._load_configuration()
            if action == "apply" and request.get("action") not in {"prune", "clear"} and not is_workspace_trusted(load_config(strict=True), self.workspace):
                raise ApplicationError("WORKSPACE_UNTRUSTED", "请在恢复预览中确认信任此工作目录", 409)
            if self.db is None:
                self.db = SessionDB(self.home / "state.db")
            settings = config.get("checkpoints") or {}
            manager = CheckpointManager(
                enabled=bool(settings.get("enabled", False)), max_snapshots=int(settings.get("max_snapshots", 50)),
                max_total_size_mb=int(settings.get("max_total_size_mb", 500)), max_file_size_mb=int(settings.get("max_file_size_mb", 10)))
            if action == "apply" and request.get("action") not in {"prune", "clear"} and not manager.enabled:
                raise ApplicationError("CHECKPOINTS_DISABLED", "请先在设置中启用检查点恢复", 409)
            coordinator = RollbackCoordinator(checkpoint_manager=manager, session_db=self.db, agent=self.agent,
                                              journal=OperationJournal(self.home / "operations"), config=config)
            if action == "terminal_files":
                from mclaw.application.terminal_files import execute
                return {"value": execute(self, request, config, manager, coordinator)}
            if self._changes is None:
                self._changes = ChangeService(self.home, self.session, coordinator)
            else:
                self._changes.coordinator = coordinator
            result = getattr(self._changes, action)() if action in {"list", "status"} else getattr(self._changes, action)(request)
            if action == "preview":
                result["workspace_trust_required"] = request.get("action") not in {"prune", "clear"} and not is_workspace_trusted(load_config(strict=True), self.workspace)
            return {"value": result}
        except ApplicationError as exc:
            return {"error": str(exc), "code": exc.code, "status": exc.status}
        except Exception:
            return {"error": "恢复信息无法读取或操作未完成，请检查文件及恢复记录", "code": "RECOVERY_FAILED", "status": 409}

    def serve(self) -> None:
        try:
            while True:
                try:
                    entry = self.commands.get(timeout=0.25)
                except queue.Empty:
                    self._pump_background()
                    continue
                if entry is None:
                    break
                task, cancel = entry
                if "_maintenance" in task:
                    result = self.maintain(task["_maintenance"])
                    self.emit({"type": "maintenance_result", "result": result})
                else:
                    result = self.execute(task, cancel)
                    self.emit({"type": "result", "result": result})
                if self.shutdown.is_set():
                    break
        finally:
            self._drain_tools()
            self._terminate_processes()
            if self.agent is not None:
                self.agent.close(deadline=time.monotonic() + 5)
            if self.db is not None:
                self.db.close()

    def _pump_background(self):
        if self._processes is None or self.cancel.is_set() or self.shutdown.is_set():
            return
        from mclaw.cli.runtime.background import RuntimeBackgroundCoordinator

        try:
            self._processes.pump_due_watchers()
        except Exception:
            # A watcher failure must not discard this session's live worker.
            logging.getLogger(__name__).warning("Background watcher scan failed")
        while True:
            try:
                event = self._processes.completion_queue.get_nowait()
            except queue.Empty:
                break
            if isinstance(event, dict):
                kind = str(event.get("event_type") or "process_complete")
                notice = RuntimeBackgroundCoordinator._notice_for_event(event, kind, str(event.get("command") or "")[:60])
                self.emit({"type": "background", "id": uuid.uuid4().hex, "notice": notice})


def main() -> int:
    # Keep every dependency print and inherited subprocess stdout away from the
    # private protocol descriptor, including libraries writing directly to fd 1.
    output = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8", buffering=1)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
    lock = threading.Lock()

    def emit(value: dict) -> None:
        with lock:
            output.write(json.dumps(value, ensure_ascii=False) + "\n")
            output.flush()

    initial = json.loads(sys.stdin.readline())
    if initial.get("type") != "init":
        return 2
    home, session = Path(initial["home"]).resolve(), initial["session"]
    os.environ["MCLAW_HOME"] = str(home)
    os.environ["TERMINAL_CWD"] = str(Path(session["workspace"]).resolve())
    os.chdir(session["workspace"])
    worker = SessionWorker(home, session, emit)
    from mclaw.application.diagnostics import configure_worker_logging

    log_handler = configure_worker_logging(home, session["id"],
                                           secrets=lambda: worker._secret_values.copy(),
                                           run_id=lambda: worker.run_id)
    logger = logging.getLogger("mclaw.application.worker")
    logger.info("[WORKER] started")

    def controls() -> None:
        try:
            for line in sys.stdin:
                value = json.loads(line)
                if isinstance(value, dict):
                    worker.control(value)
        except (OSError, ValueError):
            pass
        finally:
            worker.control({"type": "shutdown"})

    threading.Thread(target=controls, name="mclaw-worker-controls", daemon=True).start()
    try:
        worker.serve()
    except Exception:
        logger.exception("[WORKER] failed")
        raise
    finally:
        logger.info("[WORKER] stopped")
        if log_handler is not None:
            logging.getLogger().removeHandler(log_handler)
            log_handler.close()
        output.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
