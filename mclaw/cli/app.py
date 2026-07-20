# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Interactive prompt_toolkit chat application for M-Claw.

This module owns the terminal user interface around agent turns: slash commands,
streaming renderers, session locking and resume, input history, pet
notifications, ASR controls, and subagent progress buffering.
"""

import logging
import os
import re
import shutil
import threading
import time
import asyncio
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from prompt_toolkit import Application
from prompt_toolkit.application import run_in_terminal
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory, ConditionalAutoSuggest
from prompt_toolkit.completion import ConditionalCompleter
from prompt_toolkit.filters import Condition
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import Dimension, Layout
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.shortcuts import clear_title, set_title
from prompt_toolkit.widgets import TextArea
from prompt_toolkit.styles import Style

from mclaw.cli.skill_registry import get_skill_registry
from mclaw.cli.runtime.commands import CommandRouter, ParsedSlashCommand, SlashInputDispatcher
from mclaw.cli.runtime.delegation import RuntimeDelegationCoordinator, RuntimeDelegationHooks
from mclaw.cli.runtime.events import EventType, RuntimeStatus
from mclaw.cli.runtime.interactive import InteractiveRuntime
from mclaw.cli.runtime.results import RuntimeTurnResultCoordinator, RuntimeTurnResultHooks
from mclaw.cli.runtime.session import RuntimeSessionState
from mclaw.cli.runtime.session_commands import RESUME_LATEST_SESSION
from mclaw.cli.runtime.session_lock import InteractiveSessionLock, InteractiveSessionLockError
from mclaw.cli.runtime.skill_commands import (
    RuntimeSkillCommandCoordinator,
    RuntimeSkillCommandHooks,
    RuntimeSkillImportConfirmationCoordinator,
    RuntimeSkillImportConfirmationHooks,
)
from mclaw.cli.runtime.workspace_trust import ensure_workspace_trusted, normalize_workspace_path
from mclaw.cli.slash_completer import SlashCompleter, slash_token_before_cursor
from mclaw.pet.config import ensure_pet_config
from mclaw.pet.controller import PetController
from mclaw.pet.events import PetEventType, PetState, pet_state_for_runtime_status
from mclaw.prompts.skills import build_skill_import_confirmation_context
from mclaw.prompts.slash_intents import (
    build_pet_file_drop_intent,
    build_skill_creation_intent,
    build_skill_install_intent,
)
from mclaw.providers.runtime import ProviderRuntimeContext

from mclaw.cli.colors import Colors
from mclaw.cli.tui.assets import (
    MCLAW_LOGO,
)
from mclaw.cli.tui.console import MClawConsole as ChatConsole, cprint as _cprint
from mclaw.cli.tui.composer import (
    COMPLETION_TRAY_HEIGHT,
    FoldedPasteStore,
    HistoryNavigationState,
    clear_composer_buffer,
    move_cursor_or_history_down,
    move_cursor_or_history_up,
    normalize_paste_text,
)
from mclaw.cli.tui.frontends.classic import (
    build_classic_root_container,
    formatted_text_height,
)
from mclaw.cli.tui.renderers.banner import BannerRenderer
from mclaw.cli.tui.renderers.commands import CommandsRenderer
from mclaw.cli.tui.renderers.confirm import ConfirmRenderer
from mclaw.cli.tui.renderers.delegation import DelegationRenderer
from mclaw.cli.tui.renderers.response import (
    INTERMEDIATE_ASSISTANT_TITLE,
    INTERMEDIATE_ASSISTANT_TITLE_STYLE,
    ResponseRenderer,
    THINKING_ASSISTANT_TITLE,
    THINKING_ASSISTANT_TITLE_STYLE,
)
from mclaw.cli.tui.renderers.runtime import RuntimeRenderer
from mclaw.cli.tui.renderers.safety import SafetyRenderer
from mclaw.cli.tui.renderers.skills import SkillsRenderer
from mclaw.cli.tui.renderers.status import STATUS_ANIM_FRAME_COUNT, StatusRenderer, format_duration, fmt_tokens
from mclaw.cli.tui.selection_prompt import prompt_workspace_risk_confirmation
from mclaw.cli.tui.theme import ACCENT_COLOR, select_box
from mclaw.constants import display_mclaw_path, get_mclaw_home
from mclaw.tools.delegate_tool import SubtaskEvent, get_pending_result_for_task
from mclaw.tools.terminal_tool import set_current_session
from mclaw.utils import is_truthy_value

logger = logging.getLogger(__name__)

_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")
_SHIFT_ENTER_SEQUENCE = "\x1b[27;2;13~"


def _sanitize_text_for_utf8(text: str) -> str:
    """Replace invalid surrogate code points before writing UTF-8 text sinks."""
    if not text:
        return text
    try:
        text.encode("utf-8")
        return text
    except UnicodeEncodeError:
        return _SURROGATE_RE.sub("\uFFFD", text)


def _append_input_history_safely(history, text: str) -> None:
    if not text:
        return
    safe_text = _sanitize_text_for_utf8(text)
    try:
        history.append_string(safe_text)
    except Exception:
        logger.debug("prompt history append skipped for unwriteable input", exc_info=True)


# ── Color palette (blue theme) ──

_RST = Colors.RESET
_DIM = Colors.DIM

def _set_terminal_title(title: str) -> None:
    try:
        set_title(title)
    except Exception:
        pass


def _clear_terminal_title() -> None:
    try:
        clear_title()
    except Exception:
        pass


def _handle_right_key(buffer) -> None:
    """Move right inside text; accept auto-suggestion only at the end."""
    if buffer.suggestion and buffer.document.is_cursor_at_the_end:
        buffer.insert_text(buffer.suggestion.text)
    else:
        buffer.cursor_right()


def _windows_shift_pressed() -> bool:
    if os.name != "nt":
        return False
    # ponytail: remove OS polling when prompt_toolkit preserves modified Enter.
    import ctypes

    return bool(ctypes.windll.user32.GetAsyncKeyState(0x10) & 0x8000)


def _is_shift_enter(event) -> bool:
    return bool(event.key_sequence and event.key_sequence[-1].data == _SHIFT_ENTER_SEQUENCE) or _windows_shift_pressed()


def _strip_thinking(text: str) -> str:
    """Remove MiniMax internal thinking/reasoning tags from displayed text.

    MiniMax models embed their reasoning process inside <think>...</think> or
    <reasoning>...</reasoning> tags in the content field. These are useful
    for API context but must not be shown to users.
    """
    import re
    if not text:
        return ""
    # Remove <think>...</think> blocks
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    # Remove <reasoning>...</reasoning> blocks
    text = re.sub(r"<reasoning>.*?</reasoning>", "", text, flags=re.DOTALL)
    return text


def _compact_status_detail(message: str, *, has_active_tools: bool = False) -> str:
    """Convert agent status callbacks into compact status-bar details."""
    text = " ".join(str(message or "").replace("…", "...").split()).strip()
    if not text:
        return ""

    lower = text.lower().rstrip(".")
    if "running" in lower and "tool" in lower:
        if has_active_tools:
            return ""
        match = re.search(r"running\s+(\d+)\s+tool", lower)
        if match:
            count = int(match.group(1))
            return f"Running {count} tool" if count == 1 else f"Running {count} tools"
        return "Running tools"

    return text.rstrip(".")


def _format_subagent_status(total: int, done: int, running: int) -> str:
    noun = "subagent" if running == 1 else "subagents"
    return f"{running} {noun} running · {done}/{total} done"


def _strip_markdown(text: str) -> str:
    """Strip common Markdown syntax for compact one-line display."""
    import re
    if not text:
        return ""
    # Remove headers (# ## ###)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    # Remove bold/italic
    text = re.sub(r"\*\*|__", "", text)
    text = re.sub(r"\*|_", "", text)
    # Remove inline code backticks
    text = re.sub(r"`", "", text)
    # Remove list bullets
    text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.MULTILINE)
    # Remove table pipes
    text = re.sub(r"\|", " ", text)
    # Remove links [text](url) -> text
    text = re.sub(r"\[(.*?)\]\(.*?\)", r"\1", text)
    # Collapse whitespace
    return " ".join(text.split())


# ── Main TUI class ──

class InteractiveChat:
    """Interactive terminal shell around one M-Claw agent session.

    The class owns UI state, session locks, pending input handshakes, and runtime
    sidecars while delegating model/tool execution to ``MClaw`` and rendering to
    dedicated TUI renderer classes.
    """

    def _runtime(self) -> InteractiveRuntime:
        """Return the shared runtime container, preserving injected test state."""
        runtime = getattr(self, "runtime", None)
        if not isinstance(runtime, InteractiveRuntime):
            kwargs = {
                "session_state": getattr(self, "runtime_state", None) or RuntimeSessionState(),
            }
            event_bus = getattr(self, "event_bus", None)
            if event_bus is not None:
                kwargs["event_bus"] = event_bus
            runtime = InteractiveRuntime(
                **kwargs,
            )
            self.runtime = runtime
            self.event_bus = runtime.event_bus
            self.runtime_state = runtime.session_state
        return runtime

    @property
    def _pending_input(self):
        return self._runtime().pending_input

    @_pending_input.setter
    def _pending_input(self, value):
        self._runtime().pending_input = value

    @property
    def _should_exit(self) -> bool:
        return self._runtime().should_exit

    @_should_exit.setter
    def _should_exit(self, value: bool) -> None:
        self._runtime().should_exit = bool(value)

    @property
    def _agent_running(self) -> bool:
        return self._runtime().agent_running

    @_agent_running.setter
    def _agent_running(self, value: bool) -> None:
        self._runtime().agent_running = bool(value)

    @property
    def _last_interrupt_at(self) -> float:
        return self._runtime().last_interrupt_at

    @_last_interrupt_at.setter
    def _last_interrupt_at(self, value: float) -> None:
        self._runtime().last_interrupt_at = float(value or 0.0)

    @property
    def _force_exit_no_flush(self) -> bool:
        return self._runtime().force_exit_no_flush

    @_force_exit_no_flush.setter
    def _force_exit_no_flush(self, value: bool) -> None:
        self._runtime().force_exit_no_flush = bool(value)

    @property
    def _turn_start_at(self):
        return self._runtime_state().turn_started_at

    @_turn_start_at.setter
    def _turn_start_at(self, value) -> None:
        self._runtime_state().turn_started_at = value

    @property
    def _last_turn_duration(self) -> float:
        return self._runtime_state().last_turn_duration

    @_last_turn_duration.setter
    def _last_turn_duration(self, value: float) -> None:
        self._runtime_state().last_turn_duration = float(value or 0.0)

    @property
    def _app(self):
        return self.__dict__.get("_prompt_app")

    @_app.setter
    def _app(self, value) -> None:
        self.__dict__["_prompt_app"] = value

    @property
    def provider_runtime(self) -> ProviderRuntimeContext:
        agent_runtime = getattr(getattr(self, "agent", None), "provider_runtime", None)
        return agent_runtime or self.pending_provider_runtime

    @property
    def model(self) -> str:
        return self.provider_runtime.model

    @property
    def provider(self) -> str:
        return self.provider_runtime.provider

    @property
    def api_key(self) -> str:
        return self.provider_runtime.api_key

    @property
    def base_url(self) -> str:
        return self.provider_runtime.base_url

    @property
    def api_mode(self) -> str:
        return self.provider_runtime.api_mode

    def __init__(
        self,
        provider_runtime: ProviderRuntimeContext,
        session_id: str = None,
        resume_session_id: str = None,
        enabled_toolsets: list = None,
        config: dict | None = None,
    ):
        """Initialize a session, bind renderers, and create the agent runtime."""
        self.enabled_toolsets = enabled_toolsets
        self.config = config or {}
        self.pending_provider_runtime = provider_runtime
        display_cfg = self.config.get("display", {}) if isinstance(self.config, dict) else {}
        self._live_status_animation = is_truthy_value(display_cfg.get("live_status_animation"), default=True)
        self._project_name = ""
        if self.config.get("_project_config_dir"):
            self._project_name = Path(self.config["_project_config_dir"]).resolve().name
        self.workspace_path = normalize_workspace_path(
            self.config.get("_launch_cwd")
            or os.environ.get("TERMINAL_CWD")
            or os.getcwd()
        )

        # Snapshot project-level terminal.env overrides so shutdown can restore
        # the process environment after the interactive session exits.
        self._project_env_snapshot: dict[str, str | None] = {}
        terminal_cfg = self.config.get("terminal", {}) if isinstance(self.config.get("terminal", {}), dict) else {}
        terminal_env = terminal_cfg.get("env", {})
        if terminal_env:
            for key, value in terminal_env.items():
                self._project_env_snapshot[key] = os.environ.get(key)
                os.environ[key] = str(value)

        self.session_id = session_id or f"session_{uuid.uuid4().hex[:12]}"
        self.session_start = datetime.now()

        self.runtime = InteractiveRuntime()

        # Turn timing is owned by RuntimeSessionState and surfaced as properties
        # for renderers.
        self.event_bus = self.runtime.event_bus
        self.runtime_state = self.runtime.session_state
        self._status_bar_visible = True
        self._pending_key_setup: Optional[dict] = None
        self._pending_skill_import_confirmation: Optional[dict] = None
        self._pending_secret_request: Optional[dict] = None
        self._input_mode = "keyboard"
        self._asr_service = None
        self._asr_status_text = "off"
        self._asr_ptt_lock = threading.Lock()
        self._asr_ptt_inflight = False
        self._ptt_key = self._resolve_push_to_talk_key()
        self.pet = PetController.from_config(self.config, session_id=self.session_id)
        self._last_chat_result: dict | None = None
        self._safety_renderer = SafetyRenderer(
            printer=_cprint,
            run_external_output=self._run_external_output,
            box_factory=self._box,
        )
        self._response_renderer = ResponseRenderer(
            run_external_output=self._run_external_output,
            box_factory=self._box,
        )
        self._skills_renderer = SkillsRenderer(
            run_external_output=self._run_external_output,
            box_factory=self._box,
        )
        self._delegation_renderer = DelegationRenderer(
            run_external_output=self._run_external_output,
            box_factory=self._box,
        )
        self._confirm_renderer = ConfirmRenderer(
            printer=_cprint,
            box_factory=self._box,
            console_factory=ChatConsole,
        )
        self._banner_renderer = BannerRenderer(
            box_factory=self._box,
            logo=MCLAW_LOGO,
        )
        self._status_renderer = StatusRenderer()
        self._runtime_renderer = RuntimeRenderer(printer=_cprint)
        self._commands_renderer = CommandsRenderer(
            printer=_cprint,
            run_external_output=self._run_external_output,
            box_factory=self._box,
        )

        # Animation state
        self._spinner_idx: int = 0           # 0..3 spinner chars
        self._anim_tick: int = 0             # global tick counter

        # Subagent state
        self.subtask_manager = None
        # Event buffer for progress events that arrive before subtask_manager exists.
        self._pending_subagent_events: list = []

        # Session DB
        from mclaw.state import SessionDB
        self._session_db = SessionDB()
        self._session_lock: InteractiveSessionLock | None = None

        # Resume previous session if requested
        self._resume_history = []
        resume_resolved = False
        if resume_session_id:
            requested_resume = str(resume_session_id)
            if requested_resume == RESUME_LATEST_SESSION:
                resolved = self._session_db.latest_session_id(source="cli", workspace=self.workspace_path)
            else:
                resolved = self._session_db.resolve_session_id(requested_resume, workspace=self.workspace_path)
            if resolved:
                self.session_id = resolved
                self._resume_history = self._session_db.get_messages_as_conversation(resolved)
                resume_resolved = True
            else:
                if requested_resume == RESUME_LATEST_SESSION:
                    self._get_runtime_renderer().warning("没有可恢复的历史会话，已开始新会话。")
                else:
                    self._get_runtime_renderer().warning(f"未找到会话: {requested_resume}，已开始新会话。")

        try:
            self._acquire_session_lock(self.session_id)
        except Exception:
            self._release_session_lock()
            self._session_db.close()
            raise

        # History file for prompt_toolkit
        history_dir = get_mclaw_home() / "history"
        history_dir.mkdir(parents=True, exist_ok=True)
        self._history_file = history_dir / "chat_history"

        # Skill registry used for slash-command discovery.
        self.skill_registry = get_skill_registry()
        self.skill_registry.refresh()

        # Agent (lazy init so config errors show before TUI)
        self.agent = None
        try:
            self._init_agent()
            if resume_resolved:
                self._session_db.reopen_session(self.session_id)
            self.pet.start_if_enabled()
        except Exception:
            self._release_session_lock()
            self._session_db.close()
            raise

        # Recover detached background processes from last checkpoint
        try:
            from mclaw.tools.process_registry import process_registry
            recovered = process_registry.recover_from_checkpoint()
            if recovered:
                self._get_runtime_renderer().dim(f"已从上次会话恢复 {recovered} 个后台进程。")
        except Exception:
            pass

    # ── SubtaskManager ──

    def _make_subtask_manager(self, num_tasks: int, goals: list) -> "SubtaskManager":
        """Factory for SubtaskManager — kept as method for easy subclassing."""
        return self.SubtaskManager(num_tasks, goals)

    def _box(self):
        return select_box()

    def _sym(self, text: str) -> str:
        return text

    def _emit_runtime_event(self, event_type, **payload):
        """Publish normalized UI events through the current runtime event bus."""
        runtime = self._runtime()
        event_bus = getattr(self, "event_bus", None)
        if event_bus is not None and event_bus is not runtime.event_bus:
            runtime.event_bus = event_bus
        bus = runtime.event_bus
        self.event_bus = bus
        return bus.emit(event_type, **payload)

    def _runtime_state(self) -> RuntimeSessionState:
        """Return the canonical mutable session state used by renderers/workers."""
        runtime = self._runtime()
        runtime_state = getattr(self, "runtime_state", None)
        if runtime_state is not None and runtime_state is not runtime.session_state:
            runtime.session_state = runtime_state
        state = runtime.session_state
        self.runtime_state = state
        return state

    @staticmethod
    def _count_user_messages(messages: list[dict]) -> int:
        return sum(1 for msg in messages if isinstance(msg, dict) and msg.get("role") == "user")

    def _set_agent_messages(self, messages: list[dict]) -> None:
        if not self.agent:
            return
        self.agent.messages = messages
        self.agent.session_user_messages = self._count_user_messages(messages)

    def _acquire_session_lock(self, session_id: str) -> None:
        lock = InteractiveSessionLock(session_id)
        lock.acquire()
        self._session_lock = lock

    def _switch_session_lock(self, session_id: str) -> None:
        old_lock = getattr(self, "_session_lock", None)
        if old_lock and old_lock.session_id == session_id:
            return
        new_lock = InteractiveSessionLock(session_id)
        new_lock.acquire()
        self._session_lock = new_lock
        if old_lock:
            old_lock.release()

    def _release_session_lock(self) -> None:
        lock = getattr(self, "_session_lock", None)
        self._session_lock = None
        if lock:
            lock.release()

    def _pet_emit(self, event_type, **kwargs) -> bool:
        """Forward a runtime event to the desktop pet when notifications allow it."""
        pet = getattr(self, "pet", None)
        if pet is None:
            return False
        if not self._pet_should_emit(event_type):
            return False
        try:
            return pet.emit(event_type, **kwargs)
        except Exception:
            return False

    def _pet_emit_for_runtime_status(self, event_type, **kwargs) -> bool:
        """Emit a pet event whose animation is derived from canonical runtime state."""
        kwargs["state"] = pet_state_for_runtime_status(self._runtime_state().status)
        return self._pet_emit(event_type, **kwargs)

    def _drain_pet_commands(self) -> None:
        """Translate pet-side commands into normal pending user input."""
        pet = getattr(self, "pet", None)
        if pet is None:
            return
        while True:
            command = pet.get_command_nowait()
            if not command:
                return
            if command.get("type") == "file_drop":
                prompt = self._build_pet_file_drop_prompt(command.get("paths") or [])
                if prompt:
                    self._pending_input.put(prompt)

    @staticmethod
    def _build_pet_file_drop_prompt(paths) -> str:
        return build_pet_file_drop_intent(paths)
    def _pet_should_emit(self, event_type) -> bool:
        """Apply per-event pet notification preferences before emitting."""
        event_name = event_type.value if hasattr(event_type, "value") else str(event_type)
        config = getattr(self, "config", {})
        pet_cfg = ensure_pet_config(config if isinstance(config, dict) else {})
        notify = pet_cfg.get("notify", {}) if isinstance(pet_cfg.get("notify"), dict) else {}
        if event_name == PetEventType.TURN_COMPLETED.value:
            return bool(notify.get("turn_completed", True))
        if event_name == PetEventType.BACKGROUND_PROCESS_COMPLETED.value:
            return bool(notify.get("background_completed", True))
        if event_name == PetEventType.DELEGATION_COMPLETED.value:
            return bool(notify.get("delegation_completed", True))
        if event_name == PetEventType.TOOL_FINISHED.value:
            return bool(notify.get("tool_finished", False))
        return True

    def _delegate_progress_callback(self, event: SubtaskEvent):
        """Relay subagent progress events to the SubtaskManager.

        If subtask_manager is not ready yet, buffer events and replay them
        later. This covers the race where the daemon thread starts emitting
        progress before chat() has created the manager.
        """
        if not hasattr(self, "subtask_manager") or self.subtask_manager is None:
            # Keep only the most recent progress events to bound memory growth.
            self._pending_subagent_events.append(event)
            if len(self._pending_subagent_events) > 50:
                self._pending_subagent_events.pop(0)
            return

        if event.event_type == "started":
            self._pet_emit(PetEventType.DELEGATION_TASK_STARTED, state=PetState.CARRYING, payload={"task_index": event.task_index, "data": event.data})
        elif event.event_type == "tool_call":
            self._pet_emit(PetEventType.DELEGATION_TASK_TOOL, state=PetState.REVIEW, payload={"task_index": event.task_index, "data": event.data})
        elif event.event_type == "completed":
            self._pet_emit(PetEventType.DELEGATION_TASK_COMPLETED, state=PetState.CARRYING, payload={"task_index": event.task_index, "data": event.data})
        elif event.event_type == "error":
            self._pet_emit(PetEventType.DELEGATION_TASK_FAILED, state=PetState.FAILED, payload={"task_index": event.task_index, "data": event.data})

        self.subtask_manager.on_progress(event)
        self._update_subagent_status_detail()

    def _update_subagent_status_detail(self):
        """Refresh canonical delegation detail from current subtask state."""
        sm = self.subtask_manager
        if not sm:
            return

        terminal_statuses = {"completed", "timed_out", "interrupted", "failed", "error"}
        done = sum(1 for t in sm.tasks if t["status"] in terminal_statuses)
        running = sum(1 for t in sm.tasks if t["status"] == "running")
        state = self._runtime_state()
        state.detail = _format_subagent_status(sm.num_tasks, done, running)
        if self._app:
            self._app.invalidate()

    def _build_subagent_compact_progress(self) -> list:
        return self._get_status_renderer().build_subagent_compact_progress(self)

    def _format_subagent_goal(self, goal: str, max_len: int = 72) -> str:
        return self._get_status_renderer().format_subagent_goal(goal, max_len=max_len)

    def _create_agent(self, provider_runtime: ProviderRuntimeContext, session_id: str):
        """Construct one agent without mutating the active interactive state."""
        from mclaw.agent.core import MClaw

        agent = MClaw(
            provider_runtime=provider_runtime,
            session_db=self._session_db,
            session_id=session_id,
            enabled_toolsets=self.enabled_toolsets,
            stream_callback=self._on_stream_delta,
            tool_callback=self._on_tool_start,
            tool_end_callback=self._on_tool_end,
            status_callback=self._on_status,
            event_callback=self._on_agent_event,
            print_fn=_cprint,
            workspace=self.workspace_path,
            config=self.config,
        )

        agent._delegate_progress_callback = self._delegate_progress_callback
        agent.secret_request_callback = self._secret_request_many_prompt
        return agent

    def _init_agent(self):
        """Create the core agent and attach UI callbacks for one session."""
        self.agent = self._create_agent(self.pending_provider_runtime, self.session_id)

        if self._resume_history:
            self._set_agent_messages(self._resume_history)
            self._resume_history = []

    def _secret_request_many_prompt(self, required_for: str, needs: list[dict]) -> dict:
        """Normalize tool secret requests before handing them to the TUI prompt."""
        result = {"values": {}, "authorized": [], "skipped": []}
        request_needs = []
        for item in needs:
            if not isinstance(item, dict):
                continue
            env_var = str(item.get("env_var") or "").strip().upper()
            if env_var:
                normalized = dict(item)
                normalized["env_var"] = env_var
                request_needs.append(normalized)
        if not request_needs:
            return result

        response = self._prompt_secret_request(required_for, request_needs)
        values = response.get("values") if isinstance(response.get("values"), dict) else {}
        authorized = response.get("authorized", [])
        skipped = response.get("skipped", [])
        if isinstance(authorized, str):
            authorized = [authorized]
        if isinstance(skipped, str):
            skipped = [skipped]
        result["values"].update({str(k).strip().upper(): str(v) for k, v in values.items() if str(k).strip()})
        result["authorized"].extend(str(item).strip().upper() for item in authorized if str(item).strip())
        result["skipped"].extend(str(item).strip().upper() for item in skipped if str(item).strip())
        return result

    def _prompt_secret_request(self, required_for: str, needs: list[dict]) -> dict:
        """Render one TUI batch secret request and wait for the input loop to resolve it."""
        if getattr(self, "_app", None) is None:
            return {"values": {}, "authorized": [], "skipped": [str(item.get("env_var") or "").strip().upper() for item in needs]}

        state = self._runtime_state()
        event = threading.Event()
        pending = {
            "required_for": required_for,
            "needs": [dict(item) for item in needs],
            "event": event,
            "response": None,
            "resume_status": state.status,
            "resume_detail": state.detail,
        }
        self._pending_secret_request = pending
        state.set_status(RuntimeStatus.WAITING_FOR_USER, f"Waiting for {required_for} credentials")
        self._emit_runtime_event(EventType.STATUS_CHANGED, status=state.status, message=state.detail)
        self._render_secret_request(pending)
        self._pet_emit_for_runtime_status(
            PetEventType.WAITING_FOR_USER,
            text="凭据请求",
        )
        if self._app:
            self._app.invalidate()
        if not event.wait(timeout=115):
            if getattr(self, "_pending_secret_request", None) is pending:
                self._pending_secret_request = None
                self._last_rendered_secret_request_signature = None
                state.set_status(pending["resume_status"], pending["resume_detail"])
                self._emit_runtime_event(EventType.STATUS_CHANGED, status=state.status, message=state.detail)
                self._pet_emit_for_runtime_status(PetEventType.STATUS_CHANGED, text=state.detail)
                if self._app:
                    self._app.invalidate()
            skipped = [
                str(item.get("env_var") or "").strip().upper()
                for item in needs
                if str(item.get("env_var") or "").strip()
            ]
            return {"values": {}, "authorized": [], "skipped": skipped, "timed_out": True}
        response = pending.get("response")
        return response if isinstance(response, dict) else {"action": "skip"}

    def _on_stream_delta(self, text: str):
        """Receive streamed assistant text and update UI/runtime state."""
        # Strip MiniMax internal thinking tags before displaying
        clean = _strip_thinking(text)
        state = self._runtime_state()
        char_count = state.append_stream_delta(clean)
        self._pet_emit_for_runtime_status(PetEventType.MODEL_STREAMING)
        self._emit_runtime_event(
            EventType.ASSISTANT_DELTA,
            text=clean,
            char_count=char_count,
            status=state.status,
        )
        if self._app:
            self._app.invalidate()

    def _on_tool_start(self, name: str, args: dict):
        """Reflect agent tool execution in status state, events, and pet signals."""
        state = self._runtime_state()
        state.begin_tools(name)
        self._emit_runtime_event(EventType.TOOL_STARTED, name=name, args=args, status=state.status)
        self._pet_emit_for_runtime_status(PetEventType.TOOL_STARTED, text=name, payload={"tool": name})
        if self._app:
            self._app.invalidate()

    def _on_tool_end(self):
        logger.info("[TUI] _on_tool_end")
        state = self._runtime_state()
        state.finish_tools()
        self._emit_runtime_event(EventType.TOOL_FINISHED, status=state.status, message=state.detail)
        self._pet_emit_for_runtime_status(PetEventType.TOOL_FINISHED)
        if self._app:
            logger.info("[TUI] _on_tool_end calling invalidate")
            self._app.invalidate()
            logger.info("[TUI] _on_tool_end invalidate returned")
        logger.info("[TUI] _on_tool_end done")

    def _on_status(self, msg: str):
        lower = msg.lower()
        is_tool_status = "running" in lower and "tool" in lower
        state = self._runtime_state()
        detail = _compact_status_detail(msg, has_active_tools=bool(state.active_tools))
        if is_tool_status:
            state.set_status(RuntimeStatus.TOOLS, detail)
        else:
            state.detail = detail
        self._emit_runtime_event(EventType.STATUS_CHANGED, status=state.status, message=msg, detail=state.detail)
        self._pet_emit_for_runtime_status(PetEventType.STATUS_CHANGED, text=msg)
        if self._app:
            self._app.invalidate()

    def _reset_stream_accumulator(self) -> None:
        self._runtime_state().reset_stream()

    def _on_agent_event(self, event: dict):
        """Render non-final assistant rounds that would otherwise be hidden by tools."""
        event_type = event.get("type")
        if event_type != EventType.ASSISTANT_MESSAGE and event_type != "assistant.message":
            return

        text = _strip_thinking(event.get("content") or "").strip()
        if not text:
            return

        is_final = bool(event.get("is_final"))
        self._emit_runtime_event(
            EventType.ASSISTANT_MESSAGE,
            text=text,
            api_call_index=event.get("api_call_index"),
            is_final=is_final,
            tool_names=list(event.get("tool_names") or []),
        )

        if is_final:
            return

        try:
            if event.get("content_source") == "reasoning_content":
                self._render_response(
                    text,
                    title=THINKING_ASSISTANT_TITLE,
                    title_style=THINKING_ASSISTANT_TITLE_STYLE,
                )
            else:
                self._render_response(
                    text,
                    title=INTERMEDIATE_ASSISTANT_TITLE,
                    title_style=INTERMEDIATE_ASSISTANT_TITLE_STYLE,
                )
        except Exception:
            logger.warning("assistant message render failed", exc_info=True)
            return
        self._reset_stream_accumulator()

    # ── Chat ──

    def _render_response(self, text: str, **kwargs):
        self._get_response_renderer().render_response(text, **kwargs)

    def _render_skills_output(self, text: str):
        self._get_skills_renderer().render_skills_output(text)

    def _get_response_renderer(self) -> ResponseRenderer:
        renderer = getattr(self, "_response_renderer", None)
        if renderer is None:
            renderer = ResponseRenderer(
                run_external_output=self._run_external_output,
                box_factory=self._box,
            )
            self._response_renderer = renderer
        return renderer

    def _get_skills_renderer(self) -> SkillsRenderer:
        renderer = getattr(self, "_skills_renderer", None)
        if renderer is None:
            renderer = SkillsRenderer(
                run_external_output=self._run_external_output,
                box_factory=self._box,
            )
            self._skills_renderer = renderer
        return renderer

    def _get_safety_renderer(self) -> SafetyRenderer:
        renderer = getattr(self, "_safety_renderer", None)
        if renderer is None:
            renderer = SafetyRenderer(
                printer=_cprint,
                run_external_output=self._run_external_output,
                box_factory=self._box,
            )
            self._safety_renderer = renderer
        return renderer

    def _render_safety_panel(self, title: str, *items, border_style: str = ACCENT_COLOR) -> None:
        self._get_safety_renderer().render_panel(title, *items, border_style=border_style)

    def _emit_status_snapshot(self) -> None:
        agent = getattr(self, "agent", None)
        total_tokens = 0
        api_calls = 0
        if agent is not None:
            total_tokens = getattr(agent, "session_input_tokens", 0) + getattr(agent, "session_output_tokens", 0)
            api_calls = getattr(agent, "session_api_calls", 0)
        state = self._runtime_state()
        if self._agent_running and state.turn_started_at:
            elapsed = (datetime.now() - state.turn_started_at).total_seconds()
            duration_label = format_duration(elapsed)
        elif state.last_turn_duration > 0:
            duration_label = f"last {format_duration(state.last_turn_duration)}"
        else:
            duration_label = format_duration((datetime.now() - self.session_start).total_seconds())
        self._emit_runtime_event(
            EventType.STATUS_SNAPSHOT,
            model=self.model,
            provider=self.provider,
            status=str(state.status),
            running=self._agent_running,
            spinner_index=self._spinner_idx,
            detail=state.detail,
            active_tools=sorted(state.active_tools),
            tokens=total_tokens,
            tokens_label=fmt_tokens(total_tokens),
            api_calls=api_calls,
            duration_label=duration_label,
            asr_status=self._compact_asr_status(
                getattr(self, "_asr_status_text", "") or getattr(self, "_input_mode", "keyboard")
            ),
        )

    def _render_rollback_groups(self, groups: list, workspace: str) -> None:
        self._get_safety_renderer().render_rollback_groups(groups, workspace)

    def _render_rollback_operations(self, operations: list, workspace: str) -> None:
        self._get_safety_renderer().render_rollback_operations(operations, workspace)

    def _render_project_checkpoints(self, checkpoints: list, workspace: str) -> None:
        self._get_safety_renderer().render_project_checkpoints(checkpoints, workspace)

    def _render_project_restore_prompt(self, checkpoint_ref: str, file_path: str = "") -> None:
        self._get_safety_renderer().render_project_restore_prompt(checkpoint_ref, file_path)

    def _render_checkpoints_status(self, status: dict) -> None:
        self._get_safety_renderer().render_checkpoints_status(status)

    def _render_checkpoints_prune(self, result: dict) -> None:
        self._get_safety_renderer().render_checkpoints_prune(result)

    def _render_checkpoints_clear_prompt(self) -> None:
        self._get_safety_renderer().render_checkpoints_clear_prompt()

    def _render_checkpoints_clear_result(self, result: dict) -> None:
        self._get_safety_renderer().render_checkpoints_clear_result(result)

    def _run_external_output(self, print_fn):
        """Print scrollback content without leaving the live status bar behind."""
        self._prepare_for_external_output()
        app = getattr(self, "_app", None)
        old_status_visible = getattr(self, "_status_bar_visible", False)
        self._status_bar_visible = False

        def _print_after_erase():
            self._erase_tui_before_external_output()
            print_fn()

        try:
            if app and getattr(app, "is_running", False):
                app.invalidate()
                loop = getattr(app, "loop", None)
                if loop and loop.is_running():
                    async def _run_in_ui_thread():
                        await run_in_terminal(_print_after_erase, in_executor=False)

                    future = asyncio.run_coroutine_threadsafe(_run_in_ui_thread(), loop)
                    future.result()
                else:
                    _print_after_erase()
            else:
                _print_after_erase()
        finally:
            self._status_bar_visible = old_status_visible
            if app and getattr(app, "is_running", False):
                app.invalidate()

    def _erase_tui_before_external_output(self):
        """Clear prompt_toolkit's live layout before Rich writes scrollback output.

        Terminal resize can leave the status bar rendered in the scrollback just
        before a Rich panel. Erasing the live layout first keeps final answers
        from being glued to a stale runtime status line.
        """
        app = getattr(self, "_app", None)
        if not app or not getattr(app, "is_running", False):
            return
        try:
            app.renderer.erase(leave_alternate_screen=False)
        except Exception:
            pass

    def _prepare_for_external_output(self):
        """Shorten live status before printing a final block to scrollback."""
        if getattr(self, "_asr_status_text", "") and self._asr_status_text != "off":
            self._asr_status_text = self._compact_asr_status(self._asr_status_text)
        if getattr(self, "_app", None):
            try:
                self._app.invalidate()
            except Exception:
                pass

    @staticmethod
    def _compact_asr_status(status: str) -> str:
        text = (status or "").lower()
        if text in ("off", ""):
            return "off"
        if "record" in text:
            return "REC"
        if "push_to_talk" in text:
            return "PTT"
        if "wake" in text:
            return "WAKE"
        if "once" in text:
            return "ONCE"
        if "error" in text:
            return "ERR"
        if "ready" in text:
            return "READY"
        if "closed" in text:
            return "IDLE"
        if "speech" in text or "hearing" in text:
            return "REC"
        return "ON"

    # ── SubtaskManager (nested inside CLI for access to CLI helpers) ──

    class SubtaskManager:
        """Tracks subagent progress and aggregates results."""

        def __init__(self, num_tasks: int, goals: list):
            self.num_tasks = num_tasks
            self.goals = goals
            self.tasks: List[dict] = [
                {
                    "index": i,
                    "status": "pending",
                    "goal": goals[i] if i < len(goals) else "",
                    "tool_calls": [],
                    "summary": "",
                    "duration": 0.0,
                    "error": None,
                }
                for i in range(num_tasks)
            ]
            self._lock = threading.Lock()
            self.completion_event = threading.Event()

        def on_progress(self, event: SubtaskEvent):
            with self._lock:
                if event.task_index >= len(self.tasks):
                    return
                task = self.tasks[event.task_index]
                if event.event_type == "started":
                    task["status"] = "running"
                    task["started_at"] = time.time()
                elif event.event_type == "tool_call":
                    task["tool_calls"].append((
                        event.data.get("tool", "unknown"),
                        event.data.get("args_bytes", 0),
                    ))
                elif event.event_type == "finalizing":
                    task["status"] = "finalizing"
                    task["finalizing_reason"] = event.data.get("reason", "")
                elif event.event_type == "completed":
                    result_status = event.data.get("status", "completed")
                    task["status"] = result_status
                    task["summary"] = event.data.get("summary", "")
                    task["duration"] = event.data.get("duration", 0.0)
                    task["api_calls"] = event.data.get("api_calls", 0)
                elif event.event_type == "error":
                    task["status"] = "error"
                    task["error"] = event.data.get("error", "")
                if all(
                    t["status"] in (
                        "completed", "timed_out", "interrupted", "failed", "error"
                    )
                    for t in self.tasks
                ):
                    self.completion_event.set()

        def render_overview(self) -> str:
            lines = ["", "─── Subagent Overview ───"]
            icons = {"pending": "⏳", "running": "🔄", "completed": "✅", "error": "❌"}
            for t in self.tasks:
                icon = icons.get(t["status"], "?")
                if t["status"] in ("completed", "error"):
                    status_str = f'{t["status"]} ({t["duration"]:.1f}s)'
                else:
                    status_str = t["status"]
                lines.append(f"  {icon} Task {t['index']}: {status_str}")
                if t["status"] == "running" and t["tool_calls"]:
                    last_tool = t["tool_calls"][-1][0]
                    lines.append(f"       └─ 🔧 {last_tool}")
            return "\n".join(lines)

    # ── Aggregation ──

    def _render_aggregation(self, results: dict) -> str:
        return self._get_delegation_renderer().render_aggregation(results)

    def _get_delegation_renderer(self) -> DelegationRenderer:
        renderer = getattr(self, "_delegation_renderer", None)
        if renderer is None:
            renderer = DelegationRenderer(
                run_external_output=self._run_external_output,
                box_factory=self._box,
            )
            self._delegation_renderer = renderer
        return renderer

    def chat(self, user_input: str):
        """Send user message to agent and display response."""
        state = self._runtime_state()
        state.reset_stream()
        self._last_chat_result = None
        if state.turn_started_at is None:
            state.begin_turn()

        try:
            result = self.agent.run_conversation(
                user_input,
                conversation_history=self.agent.messages,
            )
            self._last_chat_result = result
            state.last_result = result
        except Exception as exc:
            logger.exception("run_conversation failed: %s", exc)
            self._last_chat_result = {"error": str(exc), "completed": False}
            state.fail_turn(str(exc))
            self._get_runtime_renderer().error(f"处理请求时出错: {exc}", leading_newline=True)
            return

        self._get_turn_result_coordinator().handle_result(result)

    def _echo_user_message(self, user_input: str) -> None:
        self._emit_runtime_event(EventType.USER_MESSAGE, text=user_input)
        self._get_runtime_renderer().user_message("❯", user_input)

    def _get_turn_result_coordinator(self) -> RuntimeTurnResultCoordinator:
        def _emit_waiting_for_skill_confirmation() -> None:
            state = self._runtime_state()
            state.set_status(RuntimeStatus.WAITING_FOR_USER, "Waiting for skill confirmation")
            self._emit_runtime_event(EventType.STATUS_CHANGED, status=state.status, message=state.detail)
            self._pet_emit_for_runtime_status(
                PetEventType.WAITING_FOR_USER,
                text="Skill import confirmation",
            )

        return RuntimeTurnResultCoordinator(
            RuntimeTurnResultHooks(
                stream_text=lambda: self._runtime_state().stream_text,
                stream_started=lambda: self._runtime_state().stream_started,
                render_response=self._render_response,
                render_interrupted=lambda: self._get_runtime_renderer().interrupted(
                    self._sym("⚡")
                ),
                emit_waiting_for_skill_confirmation=_emit_waiting_for_skill_confirmation,
                remember_pending_skill_confirmation=lambda confirmation: setattr(
                    self,
                    "_pending_skill_import_confirmation",
                    confirmation,
                ),
                render_skill_confirmation=self._render_skill_import_confirmation,
                handle_pending_delegate=self._handle_pending_delegate,
                log_skill_confirmation_pending=lambda confirmation: logger.info(
                    "[SKILL ENABLE CONFIRMATION] pending in TUI drafting_id=%s risk=%s skill=%s",
                    confirmation.get("drafting_id"),
                    confirmation.get("risk_level"),
                    confirmation.get("skill_name"),
                ),
                invalidate_skill_registry=lambda: self.skill_registry.invalidate(),
            )
        )

    def _handle_pending_delegate(self, result: dict) -> bool:
        def _invalidate() -> None:
            if self._app:
                self._app.invalidate()

        def _replay_pending_events(manager) -> None:
            for ev in self._pending_subagent_events:
                manager.on_progress(ev)
            self._pending_subagent_events.clear()

        def _set_subtask_manager(manager) -> None:
            self.subtask_manager = manager

        def _set_delegating_status(num_tasks: int) -> None:
            state = self._runtime_state()
            state.set_status(
                RuntimeStatus.DELEGATING,
                _format_subagent_status(num_tasks, 0, num_tasks),
            )
            self._emit_runtime_event(EventType.STATUS_CHANGED, status=state.status, message=state.detail)

        def _set_aggregating_status() -> None:
            state = self._runtime_state()
            state.set_status(RuntimeStatus.AGGREGATING, "Collecting results")
            self._emit_runtime_event(EventType.STATUS_CHANGED, status=state.status, message=state.detail)
            self._pet_emit_for_runtime_status(PetEventType.STATUS_CHANGED, text=state.detail)

        def _set_synthesis_status() -> None:
            state = self._runtime_state()
            state.set_status(RuntimeStatus.AGGREGATING, "Summarizing results")
            self._emit_runtime_event(EventType.STATUS_CHANGED, status=state.status, message=state.detail)
            self._pet_emit_for_runtime_status(PetEventType.STATUS_CHANGED, text=state.detail)

        def _clear_stream_state() -> None:
            self._reset_stream_accumulator()

        def _clear_subagent_state() -> None:
            self.subtask_manager = None
            self._pending_subagent_events.clear()

        def _run_synthesis(synthesis_prompt: str, extra_system: str) -> dict:
            return self.agent.run_conversation(
                synthesis_prompt,
                conversation_history=self.agent.messages,
                disable_tools=False,
                advance_background_review=False,
                extra_system=extra_system,
            )

        def _render_synthesis_response(synth_result: dict) -> None:
            self._get_turn_result_coordinator().handle_result(synth_result)

        return RuntimeDelegationCoordinator(
            RuntimeDelegationHooks(
                emit_delegation_started=lambda payload: self._pet_emit_for_runtime_status(
                    PetEventType.DELEGATION_STARTED,
                    payload=payload,
                ),
                make_subtask_manager=self._make_subtask_manager,
                set_subtask_manager=_set_subtask_manager,
                replay_pending_subagent_events=_replay_pending_events,
                update_subagent_status=self._update_subagent_status_detail,
                set_delegating_status=_set_delegating_status,
                set_aggregating_status=_set_aggregating_status,
                set_synthesis_status=_set_synthesis_status,
                clear_stream_state=_clear_stream_state,
                clear_subagent_state=_clear_subagent_state,
                emit_delegation_completed=lambda payload: self._pet_emit_for_runtime_status(
                    PetEventType.DELEGATION_COMPLETED,
                    payload=payload,
                ),
                invalidate=_invalidate,
                sleep=time.sleep,
                get_pending_result=get_pending_result_for_task,
                render_aggregation=self._render_aggregation,
                render_result_timeout=lambda: self._get_runtime_renderer().warning(
                    f"{self._sym('⚠️')} 子代理结果获取超时",
                    leading_newline=True,
                ),
                render_display_error=lambda message: self._get_runtime_renderer().error(
                    f"显示结果时出错: {message}",
                    leading_newline=True,
                ),
                run_synthesis=_run_synthesis,
                render_synthesis_response=_render_synthesis_response,
                render_synthesis_error=lambda message: self._get_runtime_renderer().error(
                    f"综合结果时出错: {message}",
                    leading_newline=True,
                ),
                render_synthesis_timeout=lambda: self._get_runtime_renderer().warning(
                    f"{self._sym('⚠')} 综合结果超时或未完成",
                    leading_newline=True,
                ),
                log_info=lambda message, args: logger.info(message, *args),
                log_warning=lambda message, args: logger.warning(message, *args),
            )
        ).handle_pending_delegate(result)

    def _render_skill_import_confirmation(self, confirmation: dict):
        drafting_id = confirmation.get("drafting_id") or ""
        if drafting_id and getattr(self, "_last_rendered_skill_confirmation_id", None) == drafting_id:
            return
        if drafting_id:
            self._last_rendered_skill_confirmation_id = drafting_id
        self._emit_runtime_event(
            EventType.CONFIRMATION_REQUESTED,
            title="Skill 安装确认",
            message=confirmation.get("message") or "M-Claw 准备安装一个 Skill，请确认是否允许。",
            skill_name=confirmation.get("skill_name") or confirmation.get("name") or "",
            risk_level=confirmation.get("risk_level") or "存疑",
            drafting_id=confirmation.get("drafting_id") or "",
        )
        self._get_confirm_renderer().render_skill_import_confirmation(confirmation)

    def _render_secret_request(self, request: dict):
        required_for = str(request.get("required_for") or "")
        needs = self._secret_needs_from_request(request)
        signature = (
            required_for,
            tuple((str(item.get("env_var") or "").strip().upper(), str(item.get("state") or "").strip()) for item in needs),
        )
        if getattr(self, "_last_rendered_secret_request_signature", None) == signature:
            return
        self._last_rendered_secret_request_signature = signature
        env_vars = [str(item.get("env_var") or "").strip().upper() for item in needs]
        self._emit_runtime_event(
            EventType.CONFIRMATION_REQUESTED,
            title="凭据请求",
            message=f"M-Claw 需要用于 {required_for} 的作用域凭据，明文不会返回给模型。",
            required_for=required_for,
            env_vars=env_vars,
        )
        self._get_confirm_renderer().render_secret_request(request)

    def _secret_input_is_password(self) -> bool:
        request = getattr(self, "_pending_secret_request", None)
        if not isinstance(request, dict):
            return False
        return any(str(item.get("state") or "").strip() != "authorize" for item in self._secret_needs_from_request(request))

    @staticmethod
    def _secret_needs_from_request(request: dict) -> list[dict]:
        needs = request.get("needs") if isinstance(request, dict) else None
        if isinstance(needs, list):
            return [item for item in needs if isinstance(item, dict)]
        need = request.get("need") if isinstance(request, dict) else None
        return [need] if isinstance(need, dict) else []

    def _resolve_secret_request(self, response: dict) -> None:
        pending = getattr(self, "_pending_secret_request", None)
        if not isinstance(pending, dict):
            return
        self._pending_secret_request = None
        self._last_rendered_secret_request_signature = None
        state = self._runtime_state()
        state.set_status(pending["resume_status"], pending["resume_detail"])
        self._emit_runtime_event(EventType.STATUS_CHANGED, status=state.status, message=state.detail)
        self._pet_emit_for_runtime_status(PetEventType.STATUS_CHANGED, text=state.detail)
        pending["response"] = response
        event = pending.get("event")
        if hasattr(event, "set"):
            event.set()
        if self._app:
            self._app.invalidate()

    def _handle_secret_request_input(self, user_input: str) -> bool:
        pending = getattr(self, "_pending_secret_request", None)
        if not isinstance(pending, dict):
            return False
        needs = self._secret_needs_from_request(pending)
        if not needs:
            self._resolve_secret_request({"values": {}, "authorized": [], "skipped": []})
            return True

        env_vars = [str(item.get("env_var") or "").strip().upper() for item in needs if str(item.get("env_var") or "").strip()]
        authorize_envs = [
            str(item.get("env_var") or "").strip().upper()
            for item in needs
            if str(item.get("env_var") or "").strip() and str(item.get("state") or "").strip() == "authorize"
        ]
        missing_envs = [
            str(item.get("env_var") or "").strip().upper()
            for item in needs
            if str(item.get("env_var") or "").strip() and str(item.get("state") or "").strip() != "authorize"
        ]
        text = str(user_input or "").strip()
        lowered = text.lower()
        approve_values = {"", "y", "yes", "allow", "approve", "confirm", "授权", "允许", "确认", "是"}
        reject_values = {
            "__mclaw_confirm_esc__",
            "/skip",
            "skip",
            "n",
            "no",
            "cancel",
            "reject",
            "取消",
            "否",
            "跳过",
            "停止",
        }

        if not missing_envs:
            if lowered in approve_values:
                self._resolve_secret_request({"values": {}, "authorized": env_vars, "skipped": []})
                return True
            if lowered in reject_values:
                self._resolve_secret_request({"values": {}, "authorized": [], "skipped": env_vars})
                return True
            self._get_commands_renderer().line(
                f"  {Colors.YELLOW}请输入 Y/Enter 授权，或 N/Esc 跳过。{Colors.RESET}"
            )
            return True

        if lowered in reject_values or not text:
            self._resolve_secret_request({"values": {}, "authorized": [], "skipped": env_vars})
            return True
        values, error = self._parse_secret_batch_values(text, missing_envs)
        if error:
            self._get_commands_renderer().line(f"  {Colors.YELLOW}{error}{Colors.RESET}")
            return True
        self._resolve_secret_request({"values": values, "authorized": authorize_envs, "skipped": []})
        return True

    @staticmethod
    def _parse_secret_batch_values(text: str, env_vars: list[str]) -> tuple[dict[str, str], str]:
        import json as _json

        env_vars = [str(item or "").strip().upper() for item in env_vars if str(item or "").strip()]
        if not env_vars:
            return {}, ""
        raw = str(text or "").strip()
        if not raw:
            return {}, "请输入密钥，或使用 /skip 跳过。"

        if len(env_vars) == 1 and "=" not in raw and not raw.startswith("{"):
            return {env_vars[0]: raw}, ""

        values: dict[str, str] = {}
        if raw.startswith("{"):
            try:
                parsed = _json.loads(raw)
            except Exception:
                return {}, "批量密钥格式错误：请输入 JSON 对象，或使用 KEY=value; KEY2=value。"
            if not isinstance(parsed, dict):
                return {}, "批量密钥格式错误：JSON 顶层必须是对象。"
            values = {str(k).strip().upper(): str(v).strip() for k, v in parsed.items() if str(k).strip()}
        else:
            parts = [part.strip() for part in re.split(r"[\n;]+", raw) if part.strip()]
            if all("=" in part for part in parts):
                for part in parts:
                    key, _, value = part.partition("=")
                    values[key.strip().upper()] = value.strip().strip('"\'')
            elif len(parts) == len(env_vars):
                values = {env_var: value for env_var, value in zip(env_vars, parts)}
            else:
                return {}, "批量密钥格式错误：多个 key 请使用 KEY=value; KEY2=value，或按顺序用分号/换行分隔。"

        unknown = sorted(set(values) - set(env_vars))
        if unknown:
            return {}, f"批量密钥包含未请求的变量：{', '.join(unknown)}。"
        missing = [env_var for env_var in env_vars if not values.get(env_var)]
        if missing:
            return {}, f"还缺少：{', '.join(missing)}。请一次性补齐，或使用 /skip 跳过。"
        return {env_var: values[env_var] for env_var in env_vars}, ""

    def _get_confirm_renderer(self) -> ConfirmRenderer:
        renderer = getattr(self, "_confirm_renderer", None)
        if renderer is None:
            renderer = ConfirmRenderer(
                printer=_cprint,
                box_factory=self._box,
                console_factory=ChatConsole,
            )
            self._confirm_renderer = renderer
        return renderer

    def _get_commands_renderer(self) -> CommandsRenderer:
        renderer = getattr(self, "_commands_renderer", None)
        if renderer is None:
            renderer = CommandsRenderer(
                printer=_cprint,
                run_external_output=self._run_external_output,
                box_factory=self._box,
            )
            self._commands_renderer = renderer
        return renderer

    def _get_runtime_renderer(self) -> RuntimeRenderer:
        renderer = getattr(self, "_runtime_renderer", None)
        if renderer is None:
            renderer = RuntimeRenderer(printer=_cprint)
            self._runtime_renderer = renderer
        return renderer

    def _record_skill_import_confirmation_result(
        self,
        action: str,
        confirmation: dict,
        result: dict,
    ) -> None:
        content = build_skill_import_confirmation_context(action, confirmation, result)
        if not content:
            return

        message = {
            "role": "assistant",
            "content": content,
            "_mclaw_runtime_event": "skill_import_confirmation",
        }

        agent = getattr(self, "agent", None)
        messages = getattr(agent, "messages", None)
        if isinstance(messages, list):
            messages.append(message)

        session_db = getattr(self, "_session_db", None)
        session_id = getattr(self, "session_id", None)
        if session_db is not None and session_id:
            try:
                session_db.append_message(session_id, "assistant", content=content)
            except Exception:
                pass

    def _handle_skill_import_confirmation_input(self, user_input: str) -> bool:
        """Handle Y/N input while a skill import confirmation is pending."""
        def _run_skill_import_action(action: str, drafting_id: str) -> str:
            from mclaw.tools.skill_tools.manage_tool import skill_manage

            return skill_manage(action=action, name="", drafting_id=drafting_id)

        def _set_busy(approve: bool) -> None:
            self._agent_running = True
            state = self._runtime_state()
            state.set_status(
                RuntimeStatus.TOOLS,
                "Confirming Skill install" if approve else "Cancelling Skill import",
            )
            self._emit_runtime_event(EventType.STATUS_CHANGED, status=state.status, message=state.detail)
            self._pet_emit_for_runtime_status(PetEventType.STATUS_CHANGED, text=state.detail)

        def _clear_busy() -> None:
            self._agent_running = False
            state = self._runtime_state()
            state.set_status(RuntimeStatus.DONE)
            self._emit_runtime_event(EventType.STATUS_CHANGED, status=state.status, message="done")
            self._pet_emit_for_runtime_status(PetEventType.STATUS_CHANGED)

        def _invalidate_skill_registry() -> None:
            try:
                self.skill_registry.invalidate()
            except Exception:
                pass
            agent = getattr(self, "agent", None)
            mark_prompt_epoch_dirty = getattr(agent, "mark_prompt_epoch_dirty", None)
            if callable(mark_prompt_epoch_dirty):
                mark_prompt_epoch_dirty()

        return RuntimeSkillImportConfirmationCoordinator(
            RuntimeSkillImportConfirmationHooks(
                get_pending_confirmation=lambda: self._pending_skill_import_confirmation,
                clear_pending_confirmation=lambda: setattr(self, "_pending_skill_import_confirmation", None),
                run_skill_import_action=_run_skill_import_action,
                invalidate_skill_registry=_invalidate_skill_registry,
                render_invalid_input=lambda: self._get_commands_renderer().line(
                    f"  {Colors.YELLOW}请输入 Y 允许安装，或 N/Esc 停止安装。{_RST}"
                ),
                render_installed=lambda name, path: self._get_commands_renderer().render_notice(
                    "M-Claw Skill",
                    f"Skill 已安装：{name}",
                    detail=path or None,
                    kind="success",
                ),
                render_cancelled=lambda: self._get_commands_renderer().render_notice(
                    "M-Claw Skill",
                    "已停止安装并清理暂存 Skill。",
                    kind="warning",
                ),
                render_failed=lambda message: self._get_commands_renderer().render_notice(
                    "M-Claw Skill",
                    f"Skill 导入确认处理失败：{message}",
                    kind="danger",
                ),
                set_busy=_set_busy,
                clear_busy=_clear_busy,
                invalidate=lambda: self._app.invalidate() if self._app else None,
                record_confirmation_result=self._record_skill_import_confirmation_result,
            )
        ).handle_input(user_input)

    # ── Slash commands ──

    def _get_command_router(self) -> CommandRouter:
        router = getattr(self, "_command_router", None)
        if router is None:
            router = CommandRouter(
                {
                    "quit": lambda parsed: False,
                    "help": lambda parsed: self._show_help(),
                    "clear": lambda parsed: self._new_session(clear_screen=True),
                    "model": lambda parsed: self._handle_model_switch(parsed.args.strip()),
                    "model-update": lambda parsed: self._handle_model_update(parsed.args.strip()),
                    "search-backend": self._handle_search_backend_command,
                    "asr-mode": lambda parsed: self._handle_asr_mode(parsed.args.strip()),
                    "keyboard-mode": lambda parsed: self._handle_keyboard_mode(),
                    "asr-status": lambda parsed: self._show_asr_status(),
                    "asr-once": lambda parsed: self._handle_asr_once(),
                    "pet": lambda parsed: self._handle_pet_command(parsed.args.strip()),
                    "usage": lambda parsed: self._show_usage(),
                    "doctor": self._handle_doctor_command,
                    "history": lambda parsed: self._show_history(),
                    "resume": self._handle_resume_command,
                    "rollback": lambda parsed: self._handle_rollback_command(parsed.args.strip()),
                    "checkpoints": lambda parsed: self._handle_checkpoints_command(parsed.args.strip()),
                    "title": self._handle_title_command,
                    "provider": lambda parsed: self._show_providers(parsed.args.strip()),
                    "save": self._handle_save_command,
                    "schedule": self._handle_schedule_command,
                    "skills": self._handle_skills_command,
                    "skill": self._handle_skill_command,
                },
                unknown_handler=self._handle_unknown_command,
            )
            self._command_router = router
        return router

    def process_command(self, command: str) -> bool:
        """Process a slash command. Returns True to continue, False to exit."""
        return self._get_command_router().dispatch(command).continue_running

    def _handle_unknown_command(self, parsed: ParsedSlashCommand) -> bool:
        command = f"/{parsed.name}" + (f" {parsed.args}" if parsed.args else "")
        self._get_commands_renderer().render_notice("M-Claw 命令", f"未知命令: {command}", detail="输入 /help 查看可用命令", kind="danger")
        return True

    def _handle_search_backend_command(self, parsed: ParsedSlashCommand) -> bool:
        from mclaw.cli.runtime.search_commands import RuntimeSearchCommandCoordinator, RuntimeSearchCommandHooks
        from mclaw.cli.search_backend_switch import get_search_backend_status, switch_search_backend

        def _render_status(status) -> None:
            self._get_commands_renderer().render_notice(
                "M-Claw 搜索后端",
                "当前搜索后端配置",
                detail=(
                    f"当前后端: {getattr(status, 'current', 'dashscope')}\n"
                    f"DashScope: {'可用' if getattr(status, 'dashscope_available', False) else '未配置'}\n"
                    f"Tavily: {'可用' if getattr(status, 'tavily_available', False) else '未配置'}\n"
                    "用法: /search-backend dashscope|tavily|auto"
                ),
                kind="info",
            )

        RuntimeSearchCommandCoordinator(
            RuntimeSearchCommandHooks(
                get_status=get_search_backend_status,
                switch_search_backend=switch_search_backend,
                render_backend_status=_render_status,
                render_key_prompt=lambda: self._get_commands_renderer().render_notice(
                    "M-Claw 搜索后端",
                    "Tavily API 密钥尚未配置。请输入密钥（或输入 /cancel 取消）：",
                    kind="warning",
                ),
                render_success=lambda message: self._get_commands_renderer().render_notice(
                    "M-Claw 搜索后端",
                    message,
                    kind="success",
                ),
                render_error=lambda message: self._get_commands_renderer().render_notice(
                    "M-Claw 搜索后端",
                    message,
                    kind="danger",
                ),
                remember_pending_key_setup=lambda setup: setattr(self, "_pending_key_setup", setup),
                sync_search_backend=self._sync_search_backend_config,
                print_fn=_cprint,
            )
        ).handle_search_backend(parsed.args.strip())
        return True

    def _handle_doctor_command(self, parsed: ParsedSlashCommand) -> bool:
        self._get_info_command_coordinator().handle_doctor(parsed.args)
        return True

    def _handle_resume_command(self, parsed: ParsedSlashCommand) -> bool:
        self._get_session_command_coordinator().handle_resume(parsed.args)
        return True

    def _handle_title_command(self, parsed: ParsedSlashCommand) -> bool:
        self._get_session_command_coordinator().handle_title(parsed.args)
        return True

    def _handle_save_command(self, parsed: ParsedSlashCommand) -> bool:
        self._get_session_command_coordinator().handle_save(parsed.args)
        return True

    def _handle_schedule_command(self, parsed: ParsedSlashCommand) -> bool:
        self._get_scheduler_coordinator().handle_command(parsed.args)
        return True

    def _get_scheduler_coordinator(self):
        coordinator = getattr(self, "_scheduler_coordinator", None)
        if coordinator is not None:
            self._refresh_scheduler_provider_runtime()
            return coordinator
        from mclaw.cli.runtime.scheduler import RuntimeSchedulerCoordinator
        from mclaw.scheduler.delivery import DeliveryService
        from mclaw.scheduler.engine import SchedulerEngine
        from mclaw.scheduler.runner import SchedulerRunner
        from mclaw.scheduler.store import SchedulerStore
        from mclaw.tools.toolsets import get_optional_toolset_names

        scheduler_cfg = self.config.get("scheduler", {}) if isinstance(self.config, dict) else {}
        store = SchedulerStore(self._session_db)
        dingtalk_client = self._build_scheduler_dingtalk_client()
        weixin_client, weixin_token_store = self._build_scheduler_weixin_client()
        delivery = DeliveryService(
            store=store,
            dingtalk_client=dingtalk_client,
            weixin_client=weixin_client,
            weixin_token_store=weixin_token_store,
        )
        runner = SchedulerRunner(
            provider_runtime=self.provider_runtime,
            config=self.config,
            session_db=self._session_db,
            store=store,
            output_dir=str(scheduler_cfg.get("output_dir") or (get_mclaw_home() / "scheduler" / "output")),
            print_fn=_cprint,
        )
        engine = SchedulerEngine(
            store=store,
            runner=runner,
            delivery=delivery,
            worker_id=f"cli-{self.session_id[:8]}",
            max_due_per_tick=int(scheduler_cfg.get("max_due_per_tick", 5) or 5),
            max_workers=int(scheduler_cfg.get("max_workers", 2) or 2),
            runtime_config=scheduler_cfg,
        )
        coordinator = RuntimeSchedulerCoordinator(
            store=store,
            engine=engine,
            delivery=delivery,
            config=self.config,
            workspace=self.workspace_path,
            render_notice=lambda title, message, detail=None, kind="info": self._get_commands_renderer().render_text_panel(
                title,
                message,
                detail=detail,
                kind=kind,
                namespace="scheduler",
            ),
            available_toolsets=get_optional_toolset_names,
        )
        self._scheduler_coordinator = coordinator
        return coordinator

    def _refresh_scheduler_provider_runtime(self) -> None:
        """Keep the cached scheduler runner aligned with the active live context."""
        coordinator = getattr(self, "_scheduler_coordinator", None)
        runner = getattr(getattr(coordinator, "engine", None), "runner", None)
        if runner is not None:
            runner.startup_provider_runtime = self.provider_runtime

    def _build_scheduler_dingtalk_client(self):
        try:
            from mclaw.channels.dingtalk.config import DingTalkConfig
            from mclaw.channels.dingtalk.stream_client import DingTalkClient

            cfg = DingTalkConfig.from_config(self.config)
            if not cfg.enabled:
                return None
            return DingTalkClient(cfg)
        except Exception:
            return None

    def _build_scheduler_weixin_client(self):
        try:
            from mclaw.channels.weixin.config import WeixinConfig
            from mclaw.channels.weixin.context_token_store import ContextTokenStore
            from mclaw.channels.weixin.ilink_client import ILinkClient

            cfg = WeixinConfig.from_config(self.config)
            if not cfg.enabled:
                return None, None
            token_store = ContextTokenStore()
            if cfg.account_id:
                token_store.restore(cfg.account_id)
            return ILinkClient(base_url=cfg.base_url, token=cfg.token, timeout_ms=cfg.api_timeout_ms), token_store
        except Exception:
            return None, None

    def _apply_resume(
        self,
        target_session_id: str,
        provider_runtime: ProviderRuntimeContext,
        history: list[dict],
    ) -> None:
        """Atomically replace the active session after target setup succeeds."""
        current_session_id = self.session_id
        current_lock = self._session_lock
        target_lock = current_lock
        if target_session_id != current_session_id:
            target_lock = InteractiveSessionLock(target_session_id)
            target_lock.acquire()

        try:
            target_agent = self._create_agent(provider_runtime, target_session_id)
            target_agent.messages = history
            target_agent.session_user_messages = self._count_user_messages(history)
            self._session_db.switch_active_session(
                current_session_id,
                target_session_id,
                end_reason="user_resume",
            )
        except Exception:
            if target_lock is not current_lock and target_lock:
                target_lock.release()
            raise

        self.session_id = target_session_id
        self.agent = target_agent
        self.pending_provider_runtime = target_agent.provider_runtime
        self._session_lock = target_lock
        self._resume_history = []
        self._refresh_scheduler_provider_runtime()
        if current_lock is not target_lock and current_lock:
            current_lock.release()

    def _get_session_command_coordinator(self):
        from mclaw.cli.runtime.session_commands import RuntimeSessionCommandCoordinator, RuntimeSessionCommandHooks

        def _render_notice(title: str, message: str, detail: str, kind: str) -> None:
            kwargs = {"kind": kind}
            if detail:
                kwargs["detail"] = detail
            self._get_commands_renderer().render_notice(title, message, **kwargs)

        def _save_session_export(session_id: str, export: dict) -> str:

            save_path = get_mclaw_home() / "exports"
            save_path.mkdir(parents=True, exist_ok=True)
            out_file = save_path / f"{session_id}.json"
            out_file.write_text(json.dumps(export, indent=2, default=str), encoding="utf-8")
            return str(out_file)

        def _restore_runtime(snapshot: dict, row_model: str) -> ProviderRuntimeContext:
            from mclaw.providers.resolver import restore_session_runtime_context

            return restore_session_runtime_context(
                snapshot,
                config=self.config,
                row_model=row_model,
                fallback_context=self.provider_runtime,
            )
        return RuntimeSessionCommandCoordinator(
            RuntimeSessionCommandHooks(
                current_session_id=lambda: self.session_id,
                resolve_session_id=lambda session_ref: self._session_db.resolve_session_id(
                    session_ref,
                    workspace=self.workspace_path,
                ),
                get_messages_as_conversation=self._session_db.get_messages_as_conversation,
                get_model_config=self._session_db.get_model_config,
                restore_runtime_context=_restore_runtime,
                apply_resume=self._apply_resume,
                get_session=self._session_db.get_session,
                set_session_title=self._session_db.set_session_title,
                export_session=self._session_db.export_session,
                save_session_export=_save_session_export,
                render_notice=_render_notice,
            )
        )

    def _handle_skills_command(self, parsed: ParsedSlashCommand) -> bool:
        self._get_info_command_coordinator().handle_skills(parsed.args)
        return True

    def _handle_skill_command(self, parsed: ParsedSlashCommand) -> bool:
        raw = (parsed.args or "").strip()
        subcommand, _, rest = raw.partition(" ")
        subcommand = subcommand.lower()
        rest = rest.strip()
        if subcommand == "install":
            if self._pending_skill_import_confirmation:
                self._get_commands_renderer().render_notice(
                    "M-Claw Skill",
                    "已有一个 Skill 启用确认正在等待处理。",
                    detail="请先确认或取消当前 pending Skill，再开始新的安装。",
                    kind="warning",
                )
                return True
            if not rest:
                self._get_commands_renderer().render_notice(
                    "M-Claw Skill",
                    "Usage: /skill install <github-url|clawhub-url|local-path>",
                    kind="warning",
                )
                return True
            self._echo_user_message(f"/skill {raw}")
            self._pending_input.put(build_skill_install_intent(rest))
            self._skip_next_prompt = True
            return True

        if subcommand == "creation":
            if not rest:
                self._get_commands_renderer().render_notice(
                    "M-Claw Skill",
                    "Usage: /skill creation <what the Skill should do>",
                    kind="warning",
                )
                return True
            self._echo_user_message(f"/skill {raw}")
            self._pending_input.put(build_skill_creation_intent(rest))
            self._skip_next_prompt = True
            return True

        self._get_commands_renderer().render_notice(
            "M-Claw Skill",
            "Usage: /skill install <source> or /skill creation <brief>",
            kind="warning",
        )
        return True

    def _dispatch_slash_input(self, text: str) -> bool:
        """Route slash input to built-in command or skill invocation."""
        dispatcher = getattr(self, "_slash_input_dispatcher", None)
        if dispatcher is None:
            dispatcher = SlashInputDispatcher(
                builtin_commands=lambda: self.skill_registry.builtin_commands,
                dispatch_builtin=self.process_command,
                get_skill=self.skill_registry.get_skill,
                invoke_skill=self._invoke_skill,
                unknown_handler=self._render_unknown_slash_input,
            )
            self._slash_input_dispatcher = dispatcher
        return dispatcher.dispatch(text)

    def _render_unknown_slash_input(self, text: str) -> bool:
        self._get_commands_renderer().render_notice(
            "M-Claw 命令",
            f"未知命令: {text}",
            detail="输入 /help 查看可用命令",
            kind="danger",
        )
        return True

    def _invoke_skill(self, skill, user_intent: str, raw_text: str = "") -> bool:
        """Load skill content into context and send user intent."""
        if str(user_intent or "").strip():
            command_text = str(raw_text or "").strip()
            if not command_text:
                command_text = f"/{getattr(skill, 'name', '')} {user_intent}".strip()
            self._echo_user_message(command_text)
        return RuntimeSkillCommandCoordinator(
            RuntimeSkillCommandHooks(
                get_agent_messages=lambda: self.agent.messages,
                enqueue_user_intent=self._pending_input.put,
                set_skip_next_prompt=lambda: setattr(self, "_skip_next_prompt", True),
                build_base_system_prompt=lambda: self.agent._build_system_prompt(),
                render_read_error=lambda message: self._get_commands_renderer().render_notice(
                    "M-Claw Skill",
                    message,
                    kind="danger",
                ),
                render_processing=lambda name: self._get_commands_renderer().line(
                    f"  {_DIM}{self._sym('↳')} 使用 skill '{name}' 处理请求...{_RST}"
                ),
                render_loaded=lambda name: self._get_commands_renderer().line(
                    f"  {_DIM}{self._sym('✓')} 已加载 skill '{name}'，请继续输入你的问题。{_RST}"
                ),
            )
        ).invoke_skill(skill, user_intent)

    def _inject_skill_context(self, skill_text: str) -> None:
        """Append skill guidance to the existing system message."""
        RuntimeSkillCommandCoordinator.inject_skill_context(self.agent.messages, skill_text)

    # ── Model switching ──

    def _handle_model_update(self, raw_args: str = ""):
        """Refresh the models.dev cache used by setup and /model resolution."""
        self._get_model_library_command_coordinator().handle_model_update(raw_args)

    def _handle_model_switch(self, raw_args: str):
        from mclaw.cli.model_switch import parse_model_flags, switch_model
        from mclaw.cli.runtime.model_commands import RuntimeModelCommandCoordinator, RuntimeModelCommandHooks

        RuntimeModelCommandCoordinator(
            RuntimeModelCommandHooks(
                current_runtime=lambda: self.provider_runtime,
                config=lambda: self.config,
                switch_model=switch_model,
                parse_model_flags=parse_model_flags,
                render_model_status=lambda model, provider: self._get_commands_renderer().render_model_status(
                    model=model,
                    provider=provider,
                ),
                render_model_key_prompt=lambda provider_display_name, key_url: self._get_commands_renderer().render_model_key_prompt(
                    provider_display_name=provider_display_name,
                    key_url=key_url,
                ),
                render_model_error=lambda message: self._get_commands_renderer().render_notice(
                    "M-Claw 模型",
                    message,
                    kind="danger",
                ),
                remember_pending_key_setup=lambda setup: setattr(self, "_pending_key_setup", setup),
                apply_model_switch=self._apply_model_switch,
            )
        ).handle_model_switch(raw_args)

    def _apply_model_switch(self, result, is_global: bool = False):
        """Apply a successful ModelSwitchResult to the TUI and agent."""
        from mclaw.cli.config import ConfigError
        from mclaw.cli.model_switch import persist_model_choice

        context = result.runtime_context
        if context is None:
            raise ValueError("Successful model switch is missing a runtime context")
        self.agent.switch_model(context)
        self.pending_provider_runtime = self.agent.provider_runtime
        self._refresh_scheduler_provider_runtime()

        if is_global:
            try:
                persist_model_choice(context.model, context.provider)
            except ConfigError as exc:
                self._get_commands_renderer().render_notice(
                    "M-Claw 模型",
                    f"配置错误: {exc}",
                    detail="本次模型切换仅在当前会话生效，未写入配置文件。",
                    kind="danger",
                )
                return
            self._get_commands_renderer().render_notice("M-Claw 模型", f"{result.info_message}（已保存至配置）", kind="success")
        else:
            self._get_commands_renderer().render_notice("M-Claw 模型", result.info_message, kind="success")

    def _complete_key_setup(self, api_key: str):
        """Save the collected API key and retry the pending operation."""
        from mclaw.cli.config import save_env_value
        from mclaw.cli.runtime.key_setup import RuntimeKeySetupCoordinator, RuntimeKeySetupHooks

        setup = self._pending_key_setup
        self._pending_key_setup = None

        def _retry_search_backend():
            from mclaw.cli.search_backend_switch import switch_search_backend

            return switch_search_backend(
                raw_input="tavily",
                print_fn=_cprint,
            )

        def _retry_model_switch(pending_setup: dict):
            from mclaw.cli.model_switch import switch_model

            return switch_model(
                model_input=pending_setup["model"],
                current_runtime=self.provider_runtime,
                explicit_provider=pending_setup["explicit_provider"],
                explicit_profile=pending_setup.get("explicit_profile", ""),
                config=self.config,
            )

        RuntimeKeySetupCoordinator(
            RuntimeKeySetupHooks(
                save_env_value=save_env_value,
                render_missing_key=lambda: self._get_commands_renderer().render_notice(
                    "M-Claw 密钥配置",
                    "未提供密钥，切换已取消。",
                    kind="danger",
                ),
                render_key_saved=lambda env_var, masked: self._get_commands_renderer().render_notice(
                    "M-Claw 密钥配置",
                    f"密钥已保存至 {display_mclaw_path('.env')} ({env_var}={masked})",
                    kind="success",
                ),
                retry_search_backend=_retry_search_backend,
                render_search_error=lambda message: self._get_commands_renderer().render_notice(
                    "M-Claw 搜索后端",
                    message,
                    kind="danger",
                ),
                sync_search_backend=self._sync_search_backend_config,
                render_search_success=lambda message: self._get_commands_renderer().render_notice(
                    "M-Claw 搜索后端",
                    message,
                    kind="success",
                ),
                retry_model_switch=_retry_model_switch,
                render_model_error=lambda message: self._get_commands_renderer().render_notice(
                    "M-Claw 模型",
                    message,
                    kind="danger",
                ),
                apply_model_switch=self._apply_model_switch,
            )
        ).complete(setup, api_key)

    def _show_providers(self, raw_args: str = ""):
        self._get_model_library_command_coordinator().handle_provider(raw_args)

    def _get_model_library_command_coordinator(self):
        from mclaw.agent import models_dev
        from mclaw.cli.auth import PROVIDER_REGISTRY, list_configured_providers
        from mclaw.cli.model_resolver import resolve_provider_key
        from mclaw.cli.provider_profiles import (
            get_provider_profiles,
            search_models_dev_provider_ids,
        )
        from mclaw.cli.runtime.model_library_commands import RuntimeModelLibraryCommandCoordinator, RuntimeModelLibraryHooks

        def _render_notice(title: str, message: str, detail: str, kind: str) -> None:
            kwargs = {"kind": kind}
            if detail:
                kwargs["detail"] = detail
            self._get_commands_renderer().render_notice(title, message, **kwargs)

        return RuntimeModelLibraryCommandCoordinator(
            RuntimeModelLibraryHooks(
                user_providers=lambda: self.config.get("providers", {}) if isinstance(self.config, dict) else {},
                current_model=lambda: self.model,
                current_provider=lambda: self.provider,
                current_base_url=lambda: self.base_url,
                current_api_mode=lambda: self.api_mode,
                provider_registry=lambda: PROVIDER_REGISTRY,
                list_configured_providers=list_configured_providers,
                fetch_registry=lambda: models_dev.fetch_models_dev(force_refresh=False),
                registry_stats=models_dev._registry_stats,
                cache_path=models_dev._get_cache_path,
                refresh_cache=models_dev.refresh_models_dev_cache,
                resolve_provider_key=resolve_provider_key,
                get_provider_profiles=get_provider_profiles,
                list_provider_models=models_dev.list_provider_models,
                list_models_dev_provider_ids=models_dev.list_models_dev_provider_ids,
                search_models_dev_provider_ids=lambda query, provider_ids: search_models_dev_provider_ids(query, provider_ids, limit=20),
                render_notice=_render_notice,
                render_providers=self._get_commands_renderer().render_providers,
            )
        )

    def _sync_search_backend_config(self, backend: str):
        """Keep the live agent config in sync after /search-backend changes."""
        if not backend:
            return
        if not isinstance(self.config, dict):
            self.config = {}
        auxiliary = self.config.setdefault("auxiliary", {})
        if not isinstance(auxiliary, dict):
            auxiliary = {}
            self.config["auxiliary"] = auxiliary
        web_search = auxiliary.setdefault("web_search", {})
        if not isinstance(web_search, dict):
            web_search = {}
            auxiliary["web_search"] = web_search
        web_search["backend"] = backend

        agent = getattr(self, "agent", None)
        if agent is not None:
            setattr(agent, "config", self.config)

    def _sync_asr_config(self, updates: dict):
        """Keep the live ASR config in sync after ASR mode changes."""
        if not isinstance(self.config, dict):
            self.config = {}
        auxiliary = self.config.setdefault("auxiliary", {})
        if not isinstance(auxiliary, dict):
            auxiliary = {}
            self.config["auxiliary"] = auxiliary
        asr = auxiliary.setdefault("asr", {})
        if not isinstance(asr, dict):
            asr = {}
            auxiliary["asr"] = asr
        asr.update(updates or {})

        agent = getattr(self, "agent", None)
        if agent is not None:
            setattr(agent, "config", self.config)
        service = getattr(self, "_asr_service", None)
        if service is not None:
            if hasattr(service, "update_config"):
                service.update_config(asr)
            else:
                service.config.update(asr)

    def _resolve_push_to_talk_key(self) -> str:
        """Return the configured prompt_toolkit key name for push-to-talk."""
        try:
            auxiliary = self.config.get("auxiliary", {}) if isinstance(self.config, dict) else {}
            asr = auxiliary.get("asr", {}) if isinstance(auxiliary, dict) else {}
            key = str(asr.get("push_to_talk_key") or "f8").strip().lower()
        except Exception:
            key = "f8"
        allowed = {
            "f1", "f2", "f3", "f4", "f5", "f6", "f7", "f8", "f9", "f10", "f11", "f12",
            "c-space", "c-f8", "c-p",
        }
        if key not in allowed:
            logger.warning("Unsupported ASR push_to_talk_key=%s; falling back to f8", key)
            return "f8"
        return key

    def _push_to_talk_label(self) -> str:
        return self._ptt_key.upper().replace("C-", "Ctrl+")

    def _set_asr_status(self, status: str):
        self._asr_status_text = status or ""
        if self._app:
            self._app.invalidate()

    def _ensure_asr_service(self):
        """Create or refresh the voice input service from the live config."""
        from mclaw.voice.config import resolve_asr_config
        from mclaw.voice.service import VoiceInputService

        asr_config = resolve_asr_config(config=self.config)
        if not asr_config.get("enabled", False):
            mode = asr_config.get("enabled_mode") or "off"
            if mode == "auto":
                raise RuntimeError("ASR 自动禁用：当前系统未检测到可用音频输入设备。")
            raise RuntimeError("ASR 已在配置中禁用。")
        if self._asr_service is None:
            self._asr_service = VoiceInputService(
                config=asr_config,
                on_text=lambda text: self._pending_input.put(text),
                on_interrupt=lambda: self.agent.interrupt() if self.agent else None,
                on_status=self._set_asr_status,
            )
        else:
            self._asr_service.update_config(asr_config)
        return self._asr_service

    def _handle_asr_mode(self, raw_args: str = ""):
        self._get_asr_command_coordinator().handle_asr_mode(raw_args)

    def _handle_keyboard_mode(self):
        self._get_asr_command_coordinator().handle_keyboard_mode()

    def _handle_asr_once(self):
        self._get_asr_command_coordinator().handle_asr_once()

    def _handle_push_to_talk_key(self):
        """Start one ASR push-to-talk worker while coalescing repeated key presses."""
        with self._asr_ptt_lock:
            if self._asr_ptt_inflight:
                self._set_asr_status("starting")
                return
            self._asr_ptt_inflight = True
        self._set_asr_status("starting")

        def _run_push_to_talk():
            try:
                self._get_asr_command_coordinator().handle_push_to_talk_key()
            except Exception as exc:
                logger.exception("ASR push-to-talk failed")
                self._asr_status_text = f"error {exc}"
                if self._app:
                    self._app.invalidate()
            finally:
                with self._asr_ptt_lock:
                    self._asr_ptt_inflight = False

        threading.Thread(target=_run_push_to_talk, name="mclaw-asr-ptt", daemon=True).start()

    def _show_asr_status(self):
        self._get_asr_command_coordinator().show_status()

    def _get_asr_command_coordinator(self):
        from mclaw.cli.runtime.asr_commands import RuntimeAsrCommandCoordinator, RuntimeAsrCommandHooks
        from mclaw.voice.config import mask_asr_status, resolve_asr_config

        def _stop_asr_service() -> None:
            if self._asr_service is not None:
                try:
                    self._asr_service.stop()
                finally:
                    self._asr_service = None

        return RuntimeAsrCommandCoordinator(
            RuntimeAsrCommandHooks(
                sync_asr_config=self._sync_asr_config,
                ensure_asr_service=self._ensure_asr_service,
                stop_asr_service=_stop_asr_service,
                set_input_mode=lambda mode: setattr(self, "_input_mode", mode),
                set_asr_status_text=self._set_asr_status,
                push_to_talk_label=self._push_to_talk_label,
                resolve_status_config=lambda: mask_asr_status(resolve_asr_config(config=self.config)),
                get_service_status=lambda: self._asr_service.status() if self._asr_service is not None else {},
                render_notice=lambda message, kind: self._get_commands_renderer().render_notice(
                    "M-Claw ASR",
                    message,
                    kind=kind,
                ),
                render_success=lambda message: self._get_commands_renderer().render_notice(
                    "M-Claw ASR",
                    message,
                    kind="success",
                ),
                render_error=lambda message: self._get_commands_renderer().render_notice(
                    "M-Claw ASR",
                    message,
                    kind="danger",
                ),
                render_asr_status=lambda cfg, service_status: self._get_commands_renderer().render_asr_status(
                    cfg=cfg,
                    service_status=service_status,
                ),
            )
        )

    # ── Help ──

    def _sync_pet_config(self, updates: dict):
        """Keep live pet config in sync after /pet changes."""
        old_pet = getattr(self, "pet", None)
        was_running = bool(old_pet and old_pet.running)
        if old_pet is not None:
            try:
                old_pet.stop()
            except Exception:
                pass
        if not isinstance(self.config, dict):
            self.config = {}
        pet_cfg = ensure_pet_config(self.config)
        pet_cfg.update(updates or {})
        self.pet = PetController.from_config(self.config, session_id=self.session_id)
        if was_running or self.pet.config.enabled:
            self.pet.start_if_enabled()
        agent = getattr(self, "agent", None)
        if agent is not None:
            setattr(agent, "config", self.config)

    def _save_pet_config(self) -> bool:
        """Persist the current pet config to the user config file."""
        try:
            from mclaw.cli.config import load_config, save_config

            user_config = load_config(strict=True)
            user_pet = ensure_pet_config(user_config)
            runtime_pet = ensure_pet_config(self.config)
            user_pet.clear()
            user_pet.update(runtime_pet)
            save_config(user_config)
            return True
        except Exception as exc:
            self._get_commands_renderer().render_pet_notice(f"桌面宠物配置保存失败: {exc}")
            return False

    def _handle_pet_command(self, raw_args: str = ""):
        self._get_pet_command_coordinator().handle_pet_command(raw_args)

    def _get_pet_command_coordinator(self):
        from mclaw.cli.runtime.pet_commands import (
            RuntimePetCommandCoordinator,
            RuntimePetCommandHooks,
            RuntimePetStartResult,
            RuntimePetStatus,
        )

        renderer = self._get_commands_renderer()

        def _start_pet_from_config() -> RuntimePetStartResult:
            self.pet = PetController.from_config(self.config, session_id=self.session_id)
            ok = self.pet.start()
            return RuntimePetStartResult(ok=bool(ok), last_error=self.pet.last_error or "")

        def _stop_pet() -> None:
            if getattr(self, "pet", None) is not None:
                self.pet.stop()

        def _restart_pet_if_enabled() -> RuntimePetStartResult | None:
            was_enabled = bool(ensure_pet_config(self.config).get("enabled"))
            _stop_pet()
            if not was_enabled:
                return None
            self.pet = PetController.from_config(self.config, session_id=self.session_id)
            ok = self.pet.start()
            return RuntimePetStartResult(ok=bool(ok), last_error=self.pet.last_error or "")

        def _get_pet_status() -> RuntimePetStatus:
            pet_cfg = ensure_pet_config(self.config)
            pet = getattr(self, "pet", None)
            return RuntimePetStatus(
                pet_cfg=pet_cfg,
                running=bool(pet and pet.running),
                last_error=pet.last_error if pet and pet.last_error else None,
            )

        def _reset_pet_position() -> None:
            state_path = get_mclaw_home() / "pet_state.json"
            state_path.unlink(missing_ok=True)

        def _emit_test_event(name: str) -> bool:
            mapping = {
                "completed": (PetEventType.TURN_COMPLETED, PetState.WAVING),
                "failed": (PetEventType.TURN_FAILED, PetState.FAILED),
                "waiting": (PetEventType.WAITING_FOR_USER, PetState.WAITING),
                "running": (PetEventType.TURN_STARTED, PetState.RUNNING),
                "review": (PetEventType.TOOL_STARTED, PetState.REVIEW),
                "idle": (PetEventType.STATUS_CHANGED, PetState.IDLE),
                "jumping": (PetEventType.STATUS_CHANGED, PetState.JUMPING),
            }
            event_type, state = mapping.get(name, mapping["completed"])
            return self._pet_emit(event_type, state=state, text=f"test:{name}")

        return RuntimePetCommandCoordinator(
            RuntimePetCommandHooks(
                get_pet_config=lambda: ensure_pet_config(self.config),
                start_pet_from_config=_start_pet_from_config,
                stop_pet=_stop_pet,
                sync_pet_config=self._sync_pet_config,
                save_pet_config=self._save_pet_config,
                restart_pet_if_enabled=_restart_pet_if_enabled,
                reset_pet_position=_reset_pet_position,
                get_pet_status=_get_pet_status,
                emit_test_event=_emit_test_event,
                render_pet_notice=renderer.render_pet_notice,
                render_pet_status=lambda pet_cfg, running, last_error: renderer.render_pet_status(
                    pet_cfg=pet_cfg,
                    running=running,
                    last_error=last_error,
                ),
                render_pet_usage=renderer.render_pet_usage,
            )
        )

    def _show_help(self):
        self._get_info_command_coordinator().handle_help()

    def _show_usage(self):
        self._get_info_command_coordinator().handle_usage()

    def _show_history(self):
        self._get_info_command_coordinator().handle_history()

    def _get_info_command_coordinator(self):
        from mclaw.agent.skill_commands import handle_skills_command
        from mclaw.cli.runtime.info_commands import RuntimeInfoCommandCoordinator, RuntimeInfoCommandHooks
        from mclaw.doctor import format_doctor, run_doctor

        return RuntimeInfoCommandCoordinator(
            RuntimeInfoCommandHooks(
                push_to_talk_label=self._push_to_talk_label,
                run_doctor=run_doctor,
                format_doctor=format_doctor,
                current_agent=lambda: self.agent,
                current_model=lambda: self.model,
                session_start=lambda: self.session_start,
                list_history_sessions=lambda: self._session_db.list_sessions_rich(
                    source="cli",
                    limit=15,
                    workspace=self.workspace_path,
                ),
                handle_skills_command=lambda args: handle_skills_command(args, format_output=True),
                render_help=lambda push_to_talk_label: self._get_commands_renderer().render_help(
                    push_to_talk_label=push_to_talk_label,
                ),
                render_doctor=self._get_commands_renderer().render_doctor,
                render_usage=lambda agent, model, session_start: self._get_commands_renderer().render_usage(
                    agent=agent,
                    model=model,
                    session_start=session_start,
                ),
                render_history=self._get_commands_renderer().render_history,
                render_skills_output=self._render_skills_output,
            )
        )

    def _checkpoint_cwd(self) -> str:
        """Resolve the workspace used by checkpoint and rollback commands."""
        recent = getattr(self.agent, "_last_checkpoint_work_dir", None) if self.agent else None
        if recent:
            return str(recent)
        terminal_cfg = self.config.get("terminal", {}) if isinstance(self.config, dict) else {}
        if isinstance(terminal_cfg, dict) and terminal_cfg.get("cwd") and str(terminal_cfg.get("cwd")) != ".":
            return str(terminal_cfg.get("cwd"))
        launch_cfg = str(self.config.get("_launch_cwd") or "").strip() if isinstance(self.config, dict) else ""
        if launch_cfg and not self._is_mclaw_runtime_path(launch_cfg):
            return launch_cfg
        try:
            from mclaw.tools import terminal_tool

            sid = getattr(terminal_tool, "_current_session_id", None)
            env = getattr(terminal_tool, "_env_registry", {}).get(sid) if sid else None
            cwd = getattr(env, "cwd", None)
            if cwd and not self._is_mclaw_runtime_path(cwd):
                return str(cwd)
        except Exception:
            pass
        launch_cwd = os.environ.get("TERMINAL_CWD") or os.getcwd()
        if launch_cwd and not self._is_mclaw_runtime_path(launch_cwd):
            return str(launch_cwd)
        return launch_cwd

    @staticmethod
    def _is_mclaw_runtime_path(path_value) -> bool:
        try:
            from mclaw.runtime.manager import RuntimeManager

            return RuntimeManager.current().paths.is_runtime_internal_path(path_value)
        except Exception:
            return False

    def _resolve_checkpoint_ref(self, ref: str, checkpoints: list) -> str | None:
        try:
            idx = int(ref) - 1
            if 0 <= idx < len(checkpoints):
                return checkpoints[idx]["hash"]
            self._get_safety_renderer().render_notice("Checkpoint", f"无效 checkpoint 编号。请使用 1-{len(checkpoints)}。", kind="warning")
            return None
        except ValueError:
            return ref

    @staticmethod
    def _split_checkpoint_ref_and_rest(raw_args: str) -> tuple[str, str]:
        parts = (raw_args or "").strip().split(maxsplit=1)
        if not parts:
            return "", ""
        rest = parts[1].strip().strip("'\"") if len(parts) > 1 else ""
        return parts[0].strip("'\""), rest

    def _rollback_cwd_from_options(self, options: dict) -> tuple[str, str | None]:
        if options.get("dir"):
            return str(Path(options["dir"]).expanduser().resolve()), None
        if options.get("path"):
            path = Path(options["path"]).expanduser().resolve()
            return str(path.parent if not path.is_dir() else path), path.name if not path.is_dir() else None
        return self._checkpoint_cwd(), None

    def _restore_chat_context_after_rollback(self, metadata: dict, mode: str = "soft") -> None:
        """Apply checkpoint metadata to the chat history after filesystem rollback."""
        cp_cfg = self.config.get("checkpoints", {}) if isinstance(self.config, dict) else {}
        if isinstance(cp_cfg, dict) and not cp_cfg.get("restore_chat_context", True):
            return
        mode = (mode or "soft").lower()
        if mode in {"off", "fs-only"}:
            self._get_safety_renderer().render_notice("上下文回滚", "已跳过聊天上下文回滚。")
            return
        if mode not in {"soft", "strict"}:
            self._get_safety_renderer().render_notice("上下文回滚", f"未知上下文回滚模式：{mode}，已跳过聊天上下文回滚。", kind="warning")
            return
        if not metadata:
            return
        session_id = metadata.get("session_id")
        if not session_id or session_id != self.session_id:
            return
        marker = metadata.get("message_id_before_turn")
        try:
            marker_id = int(marker) if marker is not None else None
        except (TypeError, ValueError):
            marker_id = None
        if marker_id is None:
            self._get_safety_renderer().render_notice("上下文回滚", "未找到上下文边界，已跳过聊天上下文回滚。", kind="warning")
            return
        checkpoint_hash = metadata.get("commit") or metadata.get("rollback_target")
        operation_id = metadata.get("operation_id")
        from mclaw.safety.context_rollback import ContextRollbackManager

        result = ContextRollbackManager(session_db=self._session_db, agent=self.agent).apply(
            session_id=self.session_id,
            marker_message_id=marker_id,
            mode=mode,
            checkpoint_hash=checkpoint_hash,
            operation_id=operation_id,
            metadata=metadata,
            scope="tail",
        )
        deleted = int(result.get("invalidated", 0) or 0)
        rollback_id = result.get("rollback_id")
        if rollback_id:
            self._get_safety_renderer().render_notice("上下文回滚", f"已同步回退聊天上下文：软失效 {deleted} 条消息（{rollback_id}）。", kind="success")
        else:
            self._get_safety_renderer().render_notice("上下文回滚", f"已同步回退聊天上下文：软失效 {deleted} 条消息。", kind="success")

    def _handle_rollback_command(self, raw_args: str = "") -> None:
        self._get_file_safety_command_coordinator().handle_rollback(raw_args)

    def _handle_checkpoints_command(self, raw_args: str = "") -> None:
        self._get_file_safety_command_coordinator().handle_checkpoints(raw_args)

    def _get_file_safety_command_coordinator(self):
        from mclaw.cli.runtime.file_safety_commands import RuntimeFileSafetyCommandCoordinator, RuntimeFileSafetyCommandHooks
        from mclaw.safety.rollback_coordinator import RollbackCoordinator
        from mclaw.tools.checkpoint_manager import clear_all, maybe_auto_prune_checkpoints, store_status

        def _render_notice(title: str, message: str, detail: str, kind: str) -> None:
            kwargs = {"kind": kind}
            if detail:
                kwargs["detail"] = detail
            self._get_safety_renderer().render_notice(title, message, **kwargs)

        return RuntimeFileSafetyCommandCoordinator(
            RuntimeFileSafetyCommandHooks(
                get_checkpoint_manager=lambda: getattr(self.agent, "_checkpoint_mgr", None) if self.agent else None,
                create_rollback_coordinator=lambda mgr: RollbackCoordinator(
                    checkpoint_manager=mgr,
                    session_db=self._session_db,
                    agent=self.agent,
                    config=self.config if isinstance(self.config, dict) else {},
                ),
                resolve_rollback_workspace=self._rollback_cwd_from_options,
                resolve_checkpoint_ref=self._resolve_checkpoint_ref,
                checkpoint_config=lambda: self.config.get("checkpoints", {}) if isinstance(self.config, dict) else {},
                store_status=store_status,
                prune_checkpoints=lambda retention_days, delete_orphans: maybe_auto_prune_checkpoints(
                    retention_days=retention_days,
                    delete_orphans=delete_orphans,
                    min_interval_hours=0,
                ),
                clear_checkpoints=clear_all,
                restore_chat_context=self._restore_chat_context_after_rollback,
                render_notice=_render_notice,
                render_diff_result=self._get_safety_renderer().render_diff_result,
                render_rollback_result=self._get_safety_renderer().render_rollback_result,
                render_rollback_groups=self._render_rollback_groups,
                render_rollback_operations=self._render_rollback_operations,
                render_project_checkpoints=self._render_project_checkpoints,
                render_project_restore_prompt=self._render_project_restore_prompt,
                render_project_restore_result=lambda result, file_path: self._get_safety_renderer().render_project_restore_result(
                    result,
                    file_path=file_path,
                ),
                render_checkpoints_status=self._render_checkpoints_status,
                render_checkpoints_prune=self._render_checkpoints_prune,
                render_checkpoints_clear_prompt=self._render_checkpoints_clear_prompt,
                render_checkpoints_clear_result=self._render_checkpoints_clear_result,
            )
        )

    def _restore_project_env(self) -> None:
        """Restore original environment variables overwritten by project config."""
        for key, original in getattr(self, "_project_env_snapshot", {}).items():
            if original is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = original

    def _end_current_session(self, reason: str, flush: bool = True):
        """Flush memories and end the current session."""
        if flush and self.agent and not self._force_exit_no_flush:
            try:
                self.agent.flush_memories(timeout=7.0)
            except Exception:
                pass

        # Kill all background processes started in this session
        try:
            from mclaw.tools.process_registry import process_registry
            killed = process_registry.kill_all(task_id=self.session_id)
            if killed > 0:
                self._get_runtime_renderer().background_processes_stopped(killed)
        except Exception:
            pass

        self._session_db.end_session(self.session_id, reason)

    def _new_session(self, clear_screen: bool = False):
        self._end_current_session("user_new_session", flush=False)
        new_session_id = f"session_{uuid.uuid4().hex[:12]}"
        try:
            self._switch_session_lock(new_session_id)
        except InteractiveSessionLockError as exc:
            self._get_commands_renderer().render_notice("M-Claw 会话", str(exc), kind="danger")
            return
        self.session_id = new_session_id
        self.session_start = datetime.now()
        self._init_agent()
        if clear_screen and self._app:
            out = self._app.output
            out.erase_screen()
            out.cursor_goto(0, 0)
            out.flush()
        self._get_commands_renderer().render_notice("M-Claw 会话", f"新会话已开启: {self.session_id[:16]}", kind="success")

    # ── Banner ──

    def _show_banner(self):
        self._get_banner_renderer().render(
            model=self.model,
            provider=self.provider,
            session_id=self.session_id,
            agent=self.agent,
        )

    def _get_banner_renderer(self) -> BannerRenderer:
        renderer = getattr(self, "_banner_renderer", None)
        if renderer is None:
            renderer = BannerRenderer(
                box_factory=self._box,
                logo=MCLAW_LOGO,
            )
            self._banner_renderer = renderer
        return renderer

    # ── Status bar ──

    def _get_status_fragments(self):
        return self._get_status_renderer().build_status_fragments(self)

    def _get_status_renderer(self) -> StatusRenderer:
        renderer = getattr(self, "_status_renderer", None)
        if renderer is None:
            renderer = StatusRenderer()
            self._status_renderer = renderer
        return renderer

    # ── TUI layout and main loop ──

    def _input_area_height(self, status_height=None):
        """Let the composer grow while reserving the slash completion tray."""
        status_rows = 1
        if status_height is not None:
            try:
                status_rows = max(0, int(status_height()))
            except Exception:
                status_rows = 1
        terminal_rows = max(1, shutil.get_terminal_size(fallback=(80, 24)).lines)
        max_rows = max(1, terminal_rows - status_rows - COMPLETION_TRAY_HEIGHT)
        return Dimension(min=1, max=max_rows)

    def build_runtime_worker_hooks(self, *, invalidate, exit_ui, is_ui_running):
        """Build worker hooks that isolate prompt_toolkit from turn execution."""
        from mclaw.cli.runtime.background import RuntimeBackgroundCoordinator, RuntimeBackgroundHooks
        from mclaw.cli.runtime.turns import RuntimeTurnCoordinator, RuntimeTurnHooks
        from mclaw.cli.runtime.workers import RuntimeWorkerHooks

        def _safe_invalidate() -> None:
            try:
                invalidate()
            except Exception:
                pass

        def _pump_watchers() -> None:
            """Run watcher scheduler and enqueue watcher events."""
            try:
                from mclaw.tools.process_registry import process_registry
                process_registry.pump_due_watchers()
            except Exception:
                pass

        def _pop_completion_event():
            try:
                from mclaw.tools.process_registry import process_registry
            except Exception:
                return None
            try:
                return process_registry.completion_queue.get_nowait()
            except Exception:
                return None

        def _begin_background_turn(_notice: str) -> None:
            self._agent_running = True
            state = self._runtime_state()
            state.begin_turn()
            self._emit_runtime_event(EventType.STATUS_CHANGED, status=state.status, message="requesting")
            self._pet_emit_for_runtime_status(PetEventType.TURN_STARTED, text="background process event")
            _safe_invalidate()

        def _run_background_turn(notice: str) -> None:
            set_current_session(self.session_id)
            self.chat(notice)

        def _finish_agent_turn(*, emit_done_status: bool) -> None:
            result = getattr(self, "_last_chat_result", {}) or {}
            state = self._runtime_state()
            duration = state.finish_turn(result)
            self._agent_running = False
            if emit_done_status:
                self._emit_runtime_event(
                    EventType.STATUS_CHANGED,
                    status=state.status,
                    message=state.detail or str(state.status),
                )
            if state.status == RuntimeStatus.INTERRUPTED:
                self._pet_emit_for_runtime_status(PetEventType.TURN_INTERRUPTED)
            elif state.status == RuntimeStatus.ERROR:
                self._pet_emit_for_runtime_status(PetEventType.TURN_FAILED, text=state.detail)
            elif state.status == RuntimeStatus.DONE and result.get("completed"):
                self._pet_emit_for_runtime_status(PetEventType.TURN_COMPLETED, payload={
                    "duration": duration,
                    "api_calls": result.get("api_calls", 0),
                })
            elif state.status == RuntimeStatus.DONE:
                self._pet_emit_for_runtime_status(PetEventType.STATUS_CHANGED)
            _safe_invalidate()

        background_coordinator = RuntimeBackgroundCoordinator(
            RuntimeBackgroundHooks(
                pop_completion_event=_pop_completion_event,
                emit_process_completed=lambda cmd, event: self._pet_emit(
                    PetEventType.BACKGROUND_PROCESS_COMPLETED,
                    state=PetState.WAVING,
                    text=cmd,
                    payload=event,
                ),
                emit_process_updated=lambda cmd, event: self._pet_emit(
                    PetEventType.BACKGROUND_PROCESS_UPDATED,
                    state=PetState.REVIEW,
                    text=cmd,
                    payload=event,
                ),
                render_watcher_notice=lambda notice: self._get_runtime_renderer().watcher_notice(
                    self._sym("🔔"),
                    notice,
                ),
                begin_background_turn=_begin_background_turn,
                run_background_turn=_run_background_turn,
                finish_background_turn=lambda: _finish_agent_turn(emit_done_status=False),
            )
        )

        def _drain_completion_queue() -> None:
            background_coordinator.drain_completion_events()

        def _echo_command(user_input: str) -> None:
            self._emit_runtime_event(EventType.COMMAND_ECHO, text=user_input)
            self._get_runtime_renderer().command_echo(self._sym("⚙️"), user_input)

        def _begin_user_turn(user_input: str) -> None:
            self._agent_running = True
            state = self._runtime_state()
            state.begin_turn()
            self._emit_runtime_event(EventType.STATUS_CHANGED, status=state.status, message="requesting")
            self._pet_emit_for_runtime_status(PetEventType.TURN_STARTED, text=user_input[:80])
            _safe_invalidate()

        def _run_user_turn(user_input: str) -> None:
            set_current_session(self.session_id)
            self.chat(user_input)

        def _finish_user_turn() -> None:
            _finish_agent_turn(emit_done_status=True)

        turn_coordinator = RuntimeTurnCoordinator(
            self._runtime(),
            RuntimeTurnHooks(
                drain_pet_commands=self._drain_pet_commands,
                pump_background_watchers=_pump_watchers,
                drain_background_completions=_drain_completion_queue,
                pump_scheduler=lambda: self._get_scheduler_coordinator().pump(),
                has_pending_scheduler_input=lambda: self._get_scheduler_coordinator().has_pending_input(),
                handle_scheduler_input=lambda text: self._get_scheduler_coordinator().handle_input(text),
                has_pending_secret_request=lambda: bool(self._pending_secret_request),
                handle_secret_request=self._handle_secret_request_input,
                has_pending_skill_import_confirmation=lambda: bool(self._pending_skill_import_confirmation),
                handle_skill_import_confirmation=self._handle_skill_import_confirmation_input,
                has_pending_key_setup=lambda: bool(self._pending_key_setup),
                cancel_key_setup=lambda: setattr(self, "_pending_key_setup", None),
                complete_key_setup=self._complete_key_setup,
                render_key_input_cancelled=self._get_runtime_renderer().key_input_cancelled,
                should_skip_next_prompt=lambda: bool(getattr(self, "_skip_next_prompt", False)),
                consume_skip_next_prompt=lambda: setattr(self, "_skip_next_prompt", False),
                echo_user_message=self._echo_user_message,
                echo_command=_echo_command,
                dispatch_slash=self._dispatch_slash_input,
                is_ui_running=is_ui_running,
                exit_ui=exit_ui,
                begin_turn=_begin_user_turn,
                run_turn=_run_user_turn,
                finish_turn=_finish_user_turn,
            ),
        )

        def _on_animation_tick() -> None:
            self._anim_tick += 1
            self._spinner_idx = self._anim_tick % STATUS_ANIM_FRAME_COUNT
            self._emit_status_snapshot()

        def _on_error(exc: Exception) -> None:
            self._get_runtime_renderer().error(f"错误: {exc}")

        return RuntimeWorkerHooks(
            handle_input=turn_coordinator.handle_input,
            on_idle=turn_coordinator.on_idle,
            on_animation_tick=_on_animation_tick,
            on_error=_on_error,
            invalidate=_safe_invalidate,
            animation_enabled=lambda: bool(self._live_status_animation),
            animation_interval=lambda running: 0.24 if running else 0.56,
        )

    def _shutdown_runtime_threads(self, process_thread, anim_thread) -> None:
        """Stop shared runtime workers and close session resources."""
        from mclaw.cli.runtime.lifecycle import RuntimeShutdownCoordinator, RuntimeShutdownHooks

        def _stop_asr_service() -> None:
            if self._asr_service is not None:
                self._asr_service.stop()

        def _interrupt_agent() -> None:
            if self.agent is not None:
                self.agent.interrupt()

        def _stop_pet() -> None:
            if getattr(self, "pet", None) is not None:
                self.pet.stop()

        RuntimeShutdownCoordinator(
            self._runtime(),
            RuntimeShutdownHooks(
                stop_asr_service=_stop_asr_service,
                interrupt_agent=_interrupt_agent,
                restore_project_env=self._restore_project_env,
                stop_pet=_stop_pet,
                end_session=lambda flush: self._end_current_session("session_end", flush=flush),
                close_session_db=self._session_db.close,
                clear_terminal_title=_clear_terminal_title,
            ),
        ).shutdown(process_thread, anim_thread)

    def run(self):
        """Build and run the prompt_toolkit application."""
        cli_ref = self

        _set_terminal_title("M-Claw")
        self._show_banner()

        def get_prompt():
            if cli_ref._pending_secret_request:
                if cli_ref._secret_input_is_password():
                    return [("class:prompt-key", " Secret ▸ ")]
                return [("class:prompt-key", " Secret Y/N ▸ ")]
            if cli_ref._pending_skill_import_confirmation:
                return [("class:prompt-key", " 确认 Y/N ▸ ")]
            if cli_ref._pending_key_setup:
                return [("class:prompt-key", " API 密钥 ▸ ")]
            if self._agent_running:
                return [("class:prompt-dim", " 等待中 ▸ ")]
            return [("class:prompt", " 输入 ▸ ")]

        def _status_bar_height():
            columns = shutil.get_terminal_size(fallback=(80, 24)).columns
            return formatted_text_height(cli_ref._get_status_fragments(), columns)

        paste_store = FoldedPasteStore()
        history_state = HistoryNavigationState()

        input_area = TextArea(
            height=lambda: self._input_area_height(_status_bar_height),
            prompt=get_prompt,
            style="class:input-area",
            multiline=True,
            dont_extend_height=True,
            password=Condition(lambda: bool(cli_ref._pending_key_setup) or cli_ref._secret_input_is_password()),
            wrap_lines=True,
            read_only=Condition(lambda: bool(cli_ref._agent_running and not cli_ref._pending_secret_request)),
            history=FileHistory(str(self._history_file)),
            auto_suggest=ConditionalAutoSuggest(
                AutoSuggestFromHistory(),
                Condition(lambda: not bool(cli_ref._pending_key_setup or cli_ref._pending_secret_request)),
            ),
            completer=ConditionalCompleter(
                SlashCompleter(self.skill_registry),
                Condition(lambda: not bool(cli_ref._pending_key_setup or cli_ref._pending_secret_request)),
            ),
            complete_while_typing=Condition(lambda: not bool(cli_ref._pending_key_setup or cli_ref._pending_secret_request)),
        )
        input_area.buffer.on_text_changed += history_state.on_text_changed

        kb = KeyBindings()
        slash_completion = {"index": 0, "key": ""}

        def _sync_slash_completion_index() -> None:
            buffer = input_area.buffer
            key = f"{buffer.cursor_position}:{buffer.text}"
            if key != slash_completion["key"]:
                slash_completion["key"] = key
                slash_completion["index"] = 0

        def _slash_completion_active() -> bool:
            state = input_area.buffer.complete_state
            active = bool(state and state.completions and slash_token_before_cursor(input_area.buffer.document))
            if active:
                state.complete_index = None
                _sync_slash_completion_index()
            return active

        def _move_slash_completion(delta: int) -> None:
            buffer = input_area.buffer
            state = buffer.complete_state
            if not state or not state.completions:
                return
            state.complete_index = None
            _sync_slash_completion_index()
            slash_completion["index"] = (slash_completion["index"] + delta) % len(state.completions)

        def _accept_slash_completion() -> bool:
            buffer = input_area.buffer
            state = buffer.complete_state
            if not state or not state.completions or slash_token_before_cursor(buffer.document) is None:
                return False
            _sync_slash_completion_index()
            index = max(0, min(slash_completion["index"], len(state.completions) - 1))
            buffer.go_to_completion(index)
            buffer.complete_state = None
            slash_completion["key"] = f"{buffer.cursor_position}:{buffer.text}"
            slash_completion["index"] = 0
            return True

        @kb.add(Keys.BracketedPaste, eager=True)
        def _(event):
            text = normalize_paste_text(event.data)
            if self._pending_key_setup or self._pending_secret_request:
                event.current_buffer.insert_text(text)
                return
            event.current_buffer.insert_text(paste_store.fold_for_display(text))

        @kb.add("enter")
        def _(event):
            buf = event.app.current_buffer
            if _is_shift_enter(event):
                buf.insert_text("\n")
                return
            if not self._pending_key_setup and not self._pending_secret_request:
                _accept_slash_completion()
            display_text = buf.text.strip()
            if not self._pending_key_setup and not self._pending_secret_request:
                display_text = paste_store.expand(display_text)
            text = _sanitize_text_for_utf8(display_text.strip())
            if not text and not self._pending_secret_request:
                paste_store.clear()
                history_state.deactivate()
                return
            if self._pending_secret_request:
                buf.reset()
                history_state.deactivate()
                self._handle_secret_request_input(text)
                return
            if not self._pending_key_setup and not self._pending_secret_request:
                _append_input_history_safely(buf.history, text)
            buf.reset()
            paste_store.clear()
            history_state.deactivate()
            self._pending_input.put(text)

        @kb.add("escape", filter=Condition(lambda: bool(self._pending_skill_import_confirmation or self._pending_secret_request)))
        def _(event):
            event.app.current_buffer.reset()
            paste_store.clear()
            history_state.deactivate()
            if self._pending_secret_request:
                self._handle_secret_request_input("__mclaw_confirm_esc__")
                return
            self._pending_input.put("__mclaw_confirm_esc__")

        @kb.add("c-c")
        def _(event):
            now = time.time()
            double_tap = (now - self._last_interrupt_at) < 2.0

            if self._pending_secret_request:
                event.app.current_buffer.reset()
                self._handle_secret_request_input("__mclaw_confirm_esc__")
                return

            if self._agent_running:
                if double_tap:
                    # Force exit: skip flush.
                    self._force_exit_no_flush = True
                    self._should_exit = True
                    event.app.exit()
                else:
                    # First tap: interrupt only the current task and keep the loop alive.
                    self._last_interrupt_at = now
                    if self.agent:
                        self.agent.interrupt()
            else:
                # Ctrl+C while idle exits gracefully.
                self._should_exit = True
                event.app.exit()

        @kb.add("c-d")
        def _(event):
            self._should_exit = True
            event.app.exit()

        @kb.add("right")
        def _(event):
            """→: accept auto-suggestion if present, else cursor right."""
            if _accept_slash_completion():
                return
            history_state.deactivate()
            _handle_right_key(event.app.current_buffer)

        @kb.add("left", filter=Condition(lambda: history_state.active), eager=True)
        def _(event):
            """Leave history browsing when the user starts positioning the cursor."""
            history_state.deactivate()
            event.app.current_buffer.cursor_left(count=max(1, int(event.arg or 1)))

        @kb.add("home", filter=Condition(lambda: history_state.active), eager=True)
        def _(event):
            """Leave history browsing and move to the beginning of the input."""
            history_state.deactivate()
            event.app.current_buffer.cursor_position = 0

        @kb.add("end", filter=Condition(lambda: history_state.active), eager=True)
        def _(event):
            """Leave history browsing and move to the end of the input."""
            history_state.deactivate()
            event.app.current_buffer.cursor_position = len(event.app.current_buffer.text)

        @kb.add("c-e")
        def _(event):
            """Ctrl+E: accept auto-suggestion."""
            if _accept_slash_completion():
                return
            buffer = event.app.current_buffer
            if buffer.suggestion:
                history_state.deactivate()
                buffer.insert_text(buffer.suggestion.text)

        @kb.add("c-u")
        def _(event):
            """Ctrl+U: clear the entire composer buffer."""
            clear_composer_buffer(event.app.current_buffer, paste_store, history_state)

        @kb.add("tab", filter=Condition(_slash_completion_active), eager=True)
        def _(event):
            """Tab: accept the highlighted slash completion without submitting."""
            _accept_slash_completion()

        @kb.add("down", filter=Condition(_slash_completion_active), eager=True)
        def _(event):
            """Browse slash completions without previewing them in the input."""
            _move_slash_completion(1)
            event.app.invalidate()

        @kb.add("up", filter=Condition(_slash_completion_active), eager=True)
        def _(event):
            """Browse slash completions without previewing them in the input."""
            _move_slash_completion(-1)
            event.app.invalidate()

        @kb.add("down", filter=Condition(lambda: not _slash_completion_active()), eager=True)
        def _(event):
            """Move inside edited input; keep browsing history previews."""
            move_cursor_or_history_down(
                event.app.current_buffer,
                count=max(1, int(event.arg or 1)),
                history_state=history_state,
            )

        @kb.add("up", filter=Condition(lambda: not _slash_completion_active()), eager=True)
        def _(event):
            """Move inside edited input; keep browsing history previews."""
            move_cursor_or_history_up(
                event.app.current_buffer,
                count=max(1, int(event.arg or 1)),
                history_state=history_state,
            )

        @kb.add(self._ptt_key)
        def _(event):
            """Configured push-to-talk key for ASR push_to_talk mode."""
            self._handle_push_to_talk_key()

        layout = Layout(
            build_classic_root_container(
                status_fragments=lambda: cli_ref._get_status_fragments(),
                status_height=_status_bar_height,
                input_area=input_area,
                completion_selected_index=lambda: slash_completion["index"],
            )
        )

        style = Style.from_dict({
            "input-area": "#e0e0e0",
            "prompt": "bold #4A90D9",
            "prompt-dim": "#6688aa",
            "prompt-key": "bold #e0a030",
            "status-bar": "#8ab4d6",
            "status-bar-model": "bold #6CB4EE",
            "status-bar-active": "bold #90caf9",
            "status-bar-done": "bold #50c878",
            "status-bar-warning": "bold #fcd34d",
            "status-bar-error": "bold #f87171",
            "status-bar-ctx": "#ffcc00",
            "status-bar-subagent-running": "bold #90caf9",
            "status-bar-subagent-done": "bold #50c878",
            "status-bar-subagent-error": "bold #d95a5a",
            "status-bar-subagent-pending": "#5a6b7d",
            "status-bar-subagent-goal": "#e0e0e0",
            "status-bar-subagent-badge": "bold #c084fc",
            "completion-menu": "bg:#1e293b #e2e8f0",
            "completion-menu.completion": "",
            "completion-menu.completion.current": "bg:#334155 #ffffff",
            "completion-menu.meta": "#94a3b8",
            "completion-menu.meta.current": "#94a3b8",
        })

        app = Application(
            layout=layout,
            key_bindings=kb,
            style=style,
            full_screen=False,
            mouse_support=False,
        )
        self._app = app

        from mclaw.cli.runtime.controller import get_runtime_controller

        controller = get_runtime_controller(self)
        process_thread, anim_thread = controller.start_runtime_threads(
            invalidate=lambda: getattr(app, "invalidate", lambda: None)(),
            exit_ui=lambda: app.exit() if getattr(app, "is_running", False) else None,
            is_ui_running=lambda: bool(getattr(app, "is_running", False)),
        )

        set_current_session(self.session_id)

        try:
            with patch_stdout():
                app.run()
        except (EOFError, KeyboardInterrupt, BrokenPipeError):
            pass
        finally:
            try:
                controller.shutdown_runtime_threads(process_thread, anim_thread)
            finally:
                self._release_session_lock()


def run_interactive(
    provider_runtime: ProviderRuntimeContext,
    resume_session_id: str = None,
    enabled_toolsets: list = None,
    config: dict | None = None,
):
    """Entry point to launch the interactive TUI."""
    from mclaw.cli.config import ConfigError

    workspace = os.environ.get("TERMINAL_CWD") or os.getcwd()
    try:
        trusted = ensure_workspace_trusted(
            workspace,
            config=config,
            prompt=prompt_workspace_risk_confirmation,
        )
    except ConfigError as exc:
        RuntimeRenderer(printer=_cprint).warning(f"配置错误: {exc}", leading_newline=True)
        return
    if not trusted:
        return

    try:
        chat = InteractiveChat(
            provider_runtime=provider_runtime,
            resume_session_id=resume_session_id,
            enabled_toolsets=enabled_toolsets,
            config=config,
        )
    except InteractiveSessionLockError as exc:
        RuntimeRenderer(printer=_cprint).warning(str(exc), leading_newline=True)
        return
    chat.run()
