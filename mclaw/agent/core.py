# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Core conversation loop for M-Claw agent sessions.

This module coordinates provider calls, tool dispatch, memory refresh, context
compression, session persistence, checkpoint integration, and streaming
response handling. The turn lifecycle stays centralized so API calls, tools,
persistence, and recovery state remain ordered across CLI, channel, and
scheduler runtimes.
"""

import json
import logging
import re
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Set

from mclaw.agent.background_review import spawn_background_review
from mclaw.agent.context_metadata import resolve_context_length
from mclaw.agent.context_compressor import ContextCompressor
from mclaw.agent.memory_manager import MemoryManager
from mclaw.agent.prompt_cache import build_prompt_cache_plan
from mclaw.agent.prompt_builder import build_system_prompt
from mclaw.agent.retry_utils import jittered_backoff
from mclaw.agent.token_budget import estimate_request_budget
from mclaw.agent.transports.base import (
    ModelCallError,
    ModelCallOptions,
    ModelCallResult,
    ReasoningTrace,
    model_response_confirms_visibility,
)
from mclaw.agent.transports.factory import create_transport
from mclaw.agent.usage import UsageRecord
from mclaw.prompts.background import build_memory_flush_system_prompt
from mclaw.providers.runtime import ProviderRuntimeContext
from mclaw.state import SessionDB
from mclaw.tools.interrupt import set_interrupt

logger = logging.getLogger(__name__)

MAX_RETRIES = 5
_COMPRESSION_FALLBACK_TARGET_RATIO = 0.80

SKILL_WRITE_ACTIONS = frozenset({
    "create_scaffold",
    "create",
    "edit",
    "patch",
    "delete",
    "write_file",
    "remove_file",
    "enable_drafting",
    "evolution_update",
})

_SECRET_VALUE_RE = re.compile(
    r"(?i)\b(?:sk-(?:api-)?[A-Za-z0-9_-]{16,}|[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,})"
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_-]?key|token|client[_-]?secret|password|secret)([\"']?\s*[:=]\s*[\"']?)([^\"'\s,}]{8,})"
)


def _redact_log_secrets(value: Any) -> Any:
    """Best-effort redaction for verbose debug logs; runtime secret policy is enforced elsewhere."""
    if isinstance(value, str):
        text = _SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}<redacted>", value)
        return _SECRET_VALUE_RE.sub("<redacted>", text)
    if isinstance(value, list):
        return [_redact_log_secrets(item) for item in value]
    if isinstance(value, dict):
        redacted: dict[Any, Any] = {}
        for key, item in value.items():
            key_text = str(key).lower()
            if any(marker in key_text for marker in ("api_key", "apikey", "token", "secret", "password")):
                redacted[key] = "<redacted>" if item else item
            else:
                redacted[key] = _redact_log_secrets(item)
        return redacted
    return value


class MClaw:
    """Stateful agent facade for one conversation session.

    ``MClaw`` owns the canonical message list, bound transport, tool registry,
    memory/compression helpers, checkpoint metadata, and callback hooks used by
    CLI, channel, and scheduler runtimes.
    """

    def __init__(
        self,
        *,
        provider_runtime: ProviderRuntimeContext,
        usage_sink: Callable[[UsageRecord], None] | None = None,
        system_prompt: str = "",
        session_db: SessionDB = None,
        session_id: str = None,
        parent_session_id: str = None,
        max_iterations: int | None = None,
        platform: str = "cli",
        enabled_toolsets: List[str] = None,
        stream_callback: Callable = None,
        tool_callback: Callable = None,
        tool_end_callback: Callable = None,
        status_callback: Callable = None,
        event_callback: Callable = None,
        print_fn: Callable = None,
        workspace: str = None,
        # Subagent isolation.
        skip_memory: bool = False,
        config: dict | None = None,
    ):
        """Create session-scoped runtime state without starting a model call."""
        if not isinstance(provider_runtime, ProviderRuntimeContext):
            raise TypeError("provider_runtime must be a ProviderRuntimeContext")
        self.provider_runtime = provider_runtime
        self.transport = create_transport(provider_runtime)
        self._usage_sink = usage_sink
        self.system_prompt = system_prompt
        self.config = config or {}
        if max_iterations is None:
            agent_config = self.config.get("agent", {})
            if isinstance(agent_config, dict):
                max_iterations = agent_config.get("max_turns")
        if max_iterations is not None:
            try:
                max_iterations = int(max_iterations)
            except (TypeError, ValueError) as exc:
                raise ValueError("agent.max_turns must be a positive integer") from exc
            if max_iterations <= 0:
                raise ValueError("agent.max_turns must be a positive integer")
        self.max_iterations = max_iterations
        self.platform = platform
        self.enabled_toolsets = enabled_toolsets

        self.session_id = session_id or f"session_{uuid.uuid4().hex[:12]}"
        self.workspace_path = str(workspace or "").strip()
        self._session_db = session_db
        self._print_fn = print_fn or print
        self._stream_callback = stream_callback
        self._tool_callback = tool_callback
        self._tool_end_callback = tool_end_callback
        self._status_callback = status_callback
        self._event_callback = event_callback

        # Subagent isolation.
        self._skip_memory = skip_memory
        self._delegate_depth: int = 0

        self.messages: List[Dict[str, Any]] = []
        self._interrupted = False
        self._prompt_epoch_dirty = False
        self._tool_call_ids_pending_visibility: Set[str] = set()
        self._tool_visibility_state_reliable = False

        # Memory subsystem.
        self._memory_manager: Optional[MemoryManager] = None
        self._memory_store = None  # BuiltinMemoryProvider sets this
        self._recalled_memory: str = ""  # per-turn prefetch, injected at API-call time only
        memory_cfg = self.config.get("memory", {}) if isinstance(self.config, dict) else {}
        skills_cfg = self.config.get("skills", {}) if isinstance(self.config, dict) else {}
        try:
            self._memory_review_round: int = int(memory_cfg.get("memory_review_round", 10))
        except (TypeError, ValueError):
            self._memory_review_round = 10
        try:
            self._evolution_review_round: int = int(skills_cfg.get("evolution_review_round", 10))
        except (TypeError, ValueError):
            self._evolution_review_round = 10
        self._turns_since_memory_review: int = 0
        self._turns_since_evolution_review: int = 0

        # Token usage tracking.
        self._usage_lock = threading.Lock()
        self._turn_usage: dict[str, int] | None = None
        self.session_input_tokens = 0
        self.session_output_tokens = 0
        self.session_cache_read_tokens = 0
        self.session_cache_write_tokens = 0
        self.session_reasoning_tokens = 0
        self.session_api_calls = 0
        self.session_user_messages = 0

        # Filesystem checkpointing is transparent infrastructure, not model context.
        self._checkpoint_mgr = self._build_checkpoint_manager()
        self._checkpoint_turn_id: Optional[str] = None
        self._checkpoint_message_id_before_turn: Optional[int] = None
        self._checkpoint_messages_len_before_turn: int = 0
        self._last_checkpoint_work_dir: Optional[str] = None
        self._last_checkpoint_targets: List[str] = []
        self._last_checkpoint_attempt: Dict[str, Any] = {}

        # Context compressor.
        compression_cfg = self.config.get("compression", {}) if isinstance(self.config, dict) else {}
        if compression_cfg.get("enabled", True):
            try:
                compression_threshold = float(compression_cfg.get("threshold", 0.50))
            except (TypeError, ValueError):
                compression_threshold = 0.50
            try:
                compression_target_ratio = float(compression_cfg.get("target_ratio", 0.20))
            except (TypeError, ValueError):
                compression_target_ratio = 0.20
            try:
                compression_summary_timeout = int(compression_cfg.get("summary_timeout", 180))
            except (TypeError, ValueError):
                compression_summary_timeout = 180
            compression_summary_timeout = max(30, min(compression_summary_timeout, 600))

            summary_model = (
                compression_cfg.get("summary_model")
                or ""
            )
            summary_provider = (
                compression_cfg.get("summary_provider")
                or ""
            )
            summary_base_url = (
                compression_cfg.get("summary_base_url")
                or ""
            )
            summary_api_key = ""

            context_window = resolve_context_length(self.provider_runtime)
            self.context_compressor = ContextCompressor(
                provider_runtime=self.provider_runtime,
                context_window=context_window,
                threshold_percent=compression_threshold,
                summary_target_ratio=compression_target_ratio,
                session_id=self.session_id,
                summary_model_override=summary_model,
                summary_provider_override=summary_provider,
                summary_base_url_override=summary_base_url,
                summary_api_key_override=summary_api_key,
                summary_timeout=compression_summary_timeout,
                config=self.config,
                usage_callback=self._record_usage,
            )
        else:
            self.context_compressor = None

        # Tool definitions.
        self.tools: List[dict] = []
        self.valid_tool_names: Set[str] = set()
        self._discover_tools()

        # Initialize memory subsystem; provider config controls exposed targets and tools.
        if not self._skip_memory:
            self._init_memory()
            self._sync_memory_tool_schema()

        # Create the session record in the database.
        if self._session_db:
            self._session_db.create_session(
                session_id=self.session_id,
                source=self.platform,
                model=self.model,
                model_config=self.provider_runtime.snapshot(),
                system_prompt=self.system_prompt,
                parent_session_id=parent_session_id,
                workspace=self.workspace_path or None,
            )
            row = self._session_db.get_session(self.session_id) or {}
            self.session_input_tokens = int(row.get("input_tokens") or 0)
            self.session_output_tokens = int(row.get("output_tokens") or 0)
            self.session_cache_read_tokens = int(row.get("cache_read_tokens") or 0)
            self.session_cache_write_tokens = int(row.get("cache_write_tokens") or 0)
            self.session_reasoning_tokens = int(row.get("reasoning_tokens") or 0)
            self._session_db.update_model_config(
                self.session_id,
                model=self.model,
                model_config=self.provider_runtime.snapshot(),
            )
            if hasattr(self._session_db, "count_user_messages"):
                try:
                    self.session_user_messages = self._session_db.count_user_messages(self.session_id)
                except Exception:
                    self.session_user_messages = 0
            if hasattr(self._session_db, "get_pending_tool_call_ids"):
                try:
                    self._tool_call_ids_pending_visibility = (
                        self._session_db.get_pending_tool_call_ids(self.session_id)
                    )
                    self._tool_visibility_state_reliable = True
                    if self._tool_call_ids_pending_visibility:
                        logger.info(
                            "[CONTEXT RESTORE] pending tool visibility restored: count=%d ids=%s",
                            len(self._tool_call_ids_pending_visibility),
                            sorted(self._tool_call_ids_pending_visibility),
                        )
                except Exception:
                    logger.warning(
                        "Could not restore pending tool visibility for session %s",
                        self.session_id,
                        exc_info=True,
                    )

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

    def _discover_tools(self):
        """Load the active tool schemas and validation set for this session."""
        from mclaw.tools.dispatch import get_tool_definitions
        definitions, valid_names = get_tool_definitions(
            enabled_toolsets=self.enabled_toolsets,
            config=self.config,
        )
        self.tools = definitions
        self.valid_tool_names = valid_names

    def _build_checkpoint_manager(self):
        """Build the filesystem checkpoint manager from config and runtime flags."""
        cp_cfg = self.config.get("checkpoints", {}) if isinstance(self.config, dict) else {}
        if not isinstance(cp_cfg, dict):
            cp_cfg = {}
        try:
            from mclaw.runtime.manager import RuntimeManager

            runtime = RuntimeManager.current(self.config)
            runtime_checkpoint_enabled = runtime.features.is_enabled("checkpoint")
        except Exception:
            runtime_checkpoint_enabled = True

        def _int_config(key: str, default: int) -> int:
            try:
                return int(cp_cfg.get(key, default))
            except (TypeError, ValueError):
                return default

        from mclaw.tools.checkpoint_manager import CheckpointManager
        return CheckpointManager(
            enabled=bool(cp_cfg.get("enabled", False)) and runtime_checkpoint_enabled,
            max_snapshots=_int_config("max_snapshots", 50),
            max_total_size_mb=_int_config("max_total_size_mb", 500),
            max_file_size_mb=_int_config("max_file_size_mb", 10),
        )

    def _get_checkpoint_manager(self):
        """Return checkpoint state, backfilling fields for resumed/older agents."""
        checkpoint_mgr = getattr(self, "_checkpoint_mgr", None)
        if checkpoint_mgr is None:
            checkpoint_mgr = self._build_checkpoint_manager()
            self._checkpoint_mgr = checkpoint_mgr
        if not hasattr(self, "_checkpoint_turn_id"):
            self._checkpoint_turn_id = None
        if not hasattr(self, "_checkpoint_message_id_before_turn"):
            self._checkpoint_message_id_before_turn = None
        if not hasattr(self, "_checkpoint_messages_len_before_turn"):
            self._checkpoint_messages_len_before_turn = len(getattr(self, "messages", []) or [])
        if not hasattr(self, "_last_checkpoint_work_dir"):
            self._last_checkpoint_work_dir = None
        if not hasattr(self, "_last_checkpoint_targets"):
            self._last_checkpoint_targets = []
        if not hasattr(self, "_last_checkpoint_attempt"):
            self._last_checkpoint_attempt = {}
        return checkpoint_mgr

    def _init_memory(self):
        """Bootstrap the memory subsystem (MemoryManager + BuiltinMemoryProvider)."""
        try:
            from mclaw.agent.builtin_memory_provider import BuiltinMemoryProvider
            from mclaw.tools.memory_tool import MemoryStore, _DEFAULT_MEMORY_LIMIT, _DEFAULT_USER_LIMIT
            # Load configured memory limits. Prefer the merged runtime config
            # passed by the CLI so project-level .mclaw.yaml is honored.
            try:
                cfg = self.config or {}
                if not cfg:
                    from mclaw.cli.config import load_config
                    cfg = load_config()
                mem_cfg = cfg.get("memory", {})
                memory_char_limit = int(mem_cfg.get("memory_char_limit", _DEFAULT_MEMORY_LIMIT))
                user_char_limit = int(mem_cfg.get("user_char_limit", _DEFAULT_USER_LIMIT))
                memory_enabled = bool(mem_cfg.get("memory_enabled", True))
                user_profile_enabled = bool(mem_cfg.get("user_profile_enabled", True))
            except Exception:
                memory_char_limit = _DEFAULT_MEMORY_LIMIT
                user_char_limit = _DEFAULT_USER_LIMIT
                memory_enabled = True
                user_profile_enabled = True
            store = MemoryStore(
                memory_char_limit=memory_char_limit,
                user_char_limit=user_char_limit,
            )
            provider = BuiltinMemoryProvider(
                memory_store=store,
                memory_enabled=memory_enabled,
                user_profile_enabled=user_profile_enabled,
            )
            self._memory_store = store  # direct ref for snapshot refresh
            manager = MemoryManager()
            manager.add_provider(provider)
            manager.initialize(session_id=self.session_id)
            self._memory_manager = manager
            logger.debug("Memory subsystem initialised")
        except Exception as e:
            logger.warning("Memory subsystem init failed (non-fatal): %s", e)
            self._memory_manager = None
            self._memory_store = None

    def _sync_memory_tool_schema(self) -> None:
        """Align exposed memory tool schema with the active memory provider config."""
        from mclaw.tools.memory_tool import MEMORY_TOOL_NAMES

        memory_tool_names = set(MEMORY_TOOL_NAMES)
        if not (memory_tool_names & set(self.valid_tool_names)):
            return

        memory_schemas = []
        if self._memory_manager:
            try:
                memory_schemas = self._memory_manager.get_all_tool_schemas()
            except Exception:
                memory_schemas = []

        if not memory_schemas:
            self.valid_tool_names.difference_update(memory_tool_names)
            self.tools = [
                t for t in self.tools
                if t.get("function", {}).get("name") not in memory_tool_names
            ]
            return

        self.tools = [
            t for t in self.tools
            if t.get("function", {}).get("name") not in memory_tool_names
        ]
        for schema in memory_schemas:
            name = schema.get("function", {}).get("name")
            if not name:
                continue
            self.tools.append({"type": "function", "function": dict(schema.get("function", {}))})
            self.valid_tool_names.add(name)

    # ── Memory flush (pre-eviction) ─────────────────────────────

    def flush_memories(self, messages: list = None, timeout: float = None) -> None:
        """Force-persist memories before context compression.

        Makes ONE API call with only the memory tools available,
        extracts memory tool calls from the response, and calls the memory handlers.
        DIRECTLY (not through tool dispatch).

        Args:
            messages: conversation messages to include in the flush context.
                      Defaults to self.messages if not provided.
            timeout: Maximum seconds to wait for the API call. If None, wait
                     until the provider returns.
        """
        if not self._memory_manager or not self._memory_store:
            return

        msgs = messages if messages is not None else self.messages
        if not msgs or len(msgs) < 3:
            return

        flush_msgs = [
            {"role": "system", "content": build_memory_flush_system_prompt()}
        ]
        for msg in msgs[-8:]:
            if msg.get("role") in ("user", "assistant"):
                flush_msgs.append(msg)
        memory_tools = self._memory_manager.get_all_tool_schemas()
        if not memory_tools:
            return
        transport = self.transport
        provider_runtime = self.provider_runtime
        cache_enabled = bool(
            self.config.get("prompt_cache", {}).get("enabled", True)
        )
        cancelled = threading.Event()

        def _run_flush():
            try:
                self._record_api_attempt(
                    include_in_turn=not cancelled.is_set(),
                )
                result = transport.call(
                    messages=flush_msgs,
                    tools=memory_tools,
                    options=ModelCallOptions(
                        timeout=float(timeout) if timeout and timeout > 0 else 30.0,
                        source="memory_flush",
                        cache_plan=build_prompt_cache_plan(
                            messages=flush_msgs,
                            tools=memory_tools,
                            context=provider_runtime,
                            session_id=self.session_id,
                            enabled=cache_enabled,
                        ),
                    ),
                    interrupted=lambda: cancelled.is_set() or self._interrupted,
                )
                if result.usage is not None:
                    self._record_usage(
                        result.usage,
                        include_in_turn=not cancelled.is_set(),
                    )
                if result.interrupted or cancelled.is_set():
                    return

                # Parse tool calls directly and route them through MemoryManager
                # without asking the model for a second decision.
                if result.tool_calls:
                    from mclaw.tools.memory_tool import MEMORY_TOOL_NAMES

                    for tc in result.tool_calls:
                        fn = tc.get("function", {})
                        tool_name = fn.get("name")
                        if tool_name not in MEMORY_TOOL_NAMES:
                            continue
                        try:
                            args = json.loads(fn.get("arguments", "{}"))
                        except json.JSONDecodeError:
                            continue

                        result = self._memory_manager.handle_tool_call(tool_name, args)
                        try:
                            result_data = json.loads(result)
                            if result_data.get("success"):
                                logger.info(
                                    "Memory flush saved: %s", args.get("target", "memory")
                                )
                        except (json.JSONDecodeError, TypeError) as exc:
                            logger.debug("Memory flush result could not be parsed: %s", exc)
            except Exception as e:
                logger.debug("flush_memories failed: %s", e)

        if timeout and timeout > 0:
            thread = threading.Thread(
                target=_run_flush,
                daemon=True,
                name="mclaw-memory-flush",
            )
            thread.start()
            thread.join(timeout=timeout)
            if thread.is_alive():
                cancelled.set()
                logger.debug("flush_memories timed out after %.1fs", timeout)
        else:
            _run_flush()

    # Background memory and Skill review.

    def _spawn_background_review(
        self,
        messages_snapshot: List[Dict],
        review_memory: bool = False,
        review_skills: bool = False,
    ) -> None:
        """Start a background review thread for memory and Skill evolution.

        After the main reply returns, the worker creates an isolated MClaw
        review instance that shares memory storage with the parent and decides
        independently whether to write memory or create Skills. The review does
        not mutate the main conversation history.
        """
        spawn_background_review(
            self,
            messages_snapshot=messages_snapshot,
            review_memory=review_memory,
            review_skills=review_skills,
        )
        return

    # Memory snapshot refresh.

    def _refresh_memory_snapshot(self) -> None:
        """Reload memory files and re-freeze the snapshot (e.g. after compression)."""
        if self._memory_store is not None:
            try:
                self._memory_store.load_from_disk()
            except Exception as e:
                logger.debug("Memory snapshot refresh failed: %s", e)

    def interrupt(self):
        logger.info("[INTERRUPT] requested session=%s", self.session_id or "?")
        self._interrupted = True
        set_interrupt(True)

    def clear_interrupt(self):
        self._interrupted = False
        set_interrupt(False)

    def _emit_status(self, msg: str):
        if self._status_callback:
            self._status_callback(msg)

    def _emit_event(self, event: dict):
        """Send structured agent events to the hosting runtime without failing turns."""
        if not self._event_callback:
            return
        try:
            self._event_callback(event)
        except Exception:
            logger.warning("agent event callback failed", exc_info=True)

    def _assistant_round_visible_content(
        self,
        content: str,
        reasoning_content: str | None = None,
    ) -> tuple[str, str]:
        """Choose the safest visible payload for an intermediate assistant event."""
        visible_content = str(content or "").strip()
        if visible_content:
            return visible_content, "content"

        reasoning_visible = str(reasoning_content or "").strip()
        if reasoning_visible:
            return reasoning_visible, "reasoning_content"

        return "", "empty"

    def _build_assistant_round_event(
        self,
        *,
        api_call_index: int,
        content: str,
        reasoning_content: str | None = None,
        tool_calls: list | None,
        finish_reason: str | None,
        was_streamed: bool,
        is_final_override: bool | None = None,
    ) -> dict:
        """Build a provider-neutral event describing one assistant API round."""
        calls = tool_calls or []
        visible_content, content_source = self._assistant_round_visible_content(
            content,
            reasoning_content,
        )
        is_final = not bool(calls) if is_final_override is None else bool(is_final_override)
        return {
            "type": "assistant.message",
            "schema_version": 1,
            "session_id": self.session_id,
            "turn_id": getattr(self, "_checkpoint_turn_id", None),
            "api_call_index": api_call_index,
            "content": visible_content,
            "content_source": content_source,
            "is_final": is_final,
            "tool_call_count": len(calls),
            "tool_names": [
                (tc.get("function", {}) or {}).get("name", "?")
                for tc in calls
            ],
            "finish_reason": finish_reason,
            "was_streamed": was_streamed,
        }

    def _is_cleanup_only_tool_batch(self, tool_calls: list | None) -> bool:
        """Detect terminal cleanup batches that should not trigger another model turn."""
        calls = tool_calls or []
        return bool(calls) and all(self._is_cleanup_tool_call(tc) for tc in calls)

    @staticmethod
    def _is_cleanup_tool_call(tool_call: dict) -> bool:
        """Identify low-risk temporary-file cleanup commands from terminal calls."""
        fn = (tool_call or {}).get("function", {}) or {}
        if fn.get("name") != "terminal":
            return False
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except (TypeError, json.JSONDecodeError):
            return False

        command = str(args.get("command") or "").strip()
        if not command:
            return False
        lower = command.lower()

        delete_marker = (
            lower.startswith("rm ")
            or lower.startswith("del ")
            or lower.startswith("erase ")
            or "remove-item" in lower
        )
        if not delete_marker:
            return False
        if any(marker in lower for marker in ("rm -rf /", "rm -fr /", "remove-item -recurse /")):
            return False

        temp_marker = any(
            marker in lower
            for marker in ("temp", "tmp", "sysinfo", ".tmp", ".temp", ".ps1")
        )
        return temp_marker

    def switch_model(self, context: ProviderRuntimeContext) -> None:
        """Atomically activate one already-resolved provider runtime."""
        if not isinstance(context, ProviderRuntimeContext):
            raise TypeError("context must be a ProviderRuntimeContext")
        next_transport = create_transport(context)
        next_context_window = resolve_context_length(context)
        next_system_prompt = self._build_system_prompt(model=context.model)
        if self._session_db:
            self._session_db.update_model_config(
                self.session_id,
                model=context.model,
                model_config=context.snapshot(),
            )

        self.provider_runtime = context
        self.transport = next_transport
        self._replace_system_message(next_system_prompt)
        self._prompt_epoch_dirty = False
        if self.context_compressor:
            self.context_compressor.reconfigure_model(
                context,
                context_window=next_context_window,
            )

    def _replace_system_message(self, content: str) -> None:
        if self.messages and self.messages[0].get("role") == "system":
            self.messages[0]["content"] = content

    def mark_prompt_epoch_dirty(self) -> None:
        """Request a system-prompt rebuild after an out-of-band prompt source change."""
        self._prompt_epoch_dirty = True

    def _refresh_prompt_epoch(self, messages: List[Dict[str, Any]]) -> None:
        content = self._build_system_prompt()
        if messages and messages[0].get("role") == "system":
            messages[0]["content"] = content
        else:
            messages.insert(0, {"role": "system", "content": content})
        self._prompt_epoch_dirty = False

    def _build_system_prompt(self, *, model: str | None = None) -> str:
        """Assemble the current system prompt from platform, tools, and memory."""
        if self.system_prompt:
            return self.system_prompt
        memory_block = None
        if self._memory_manager:
            try:
                memory_block = self._memory_manager.build_system_prompt() or None
            except Exception as e:
                logger.debug("Memory build_system_prompt failed: %s", e)

        # Build available_toolsets from valid_tool_names.
        from mclaw.tools.dispatch import get_toolset_for_tool
        avail_toolsets: set = set()
        for t in self.valid_tool_names:
            ts = get_toolset_for_tool(t)
            if ts:
                avail_toolsets.add(ts)

        return build_system_prompt(
            agent_platform=self.platform,
            model=model or self.model,
            memory_block=memory_block,
            tool_names=self.valid_tool_names,
            available_toolsets=avail_toolsets,
            available_tool_names=sorted(self.valid_tool_names),
            config=self.config,
        )

    def _clear_unconfirmed_context_display(self) -> None:
        """Replace a one-off estimate with unknown when no input usage arrived."""
        compressor = self.context_compressor
        if compressor and compressor.display_context_estimated:
            compressor.display_context_tokens = None
            compressor.display_context_estimated = False
            logger.info("[TUI CONTEXT] source=provider tokens=unknown")

    def _fallback_prune_confirmed_tool_results(
        self,
        messages: List[Dict[str, Any]],
        *,
        dynamic_system_context: str,
        reason: str,
        force_one: bool = False,
    ) -> tuple[List[Dict[str, Any]], int]:
        """Prune confirmed tool results when summary compaction cannot help."""
        compressor = self.context_compressor
        if not compressor:
            return messages, 0
        if not self._tool_visibility_state_reliable:
            logger.warning(
                "[CONTEXT FALLBACK SKIP] reason=%s outcome=visibility_unknown pending=%d",
                reason,
                len(self._tool_call_ids_pending_visibility),
            )
            return messages, 0

        before = estimate_request_budget(
            messages=messages,
            tools=self.tools,
            dynamic_system_context=dynamic_system_context,
            context=self.provider_runtime,
            context_window=compressor.context_length,
        )
        compression_limit = min(
            compressor.threshold_tokens,
            max(1, before.context_window - before.output_budget),
        )
        target = max(1, int(compression_limit * _COMPRESSION_FALLBACK_TARGET_RATIO))
        if not force_one and before.input_tokens <= target:
            logger.warning(
                "[CONTEXT FALLBACK SKIP] reason=%s outcome=target_met "
                "estimated=%d target=%d",
                reason,
                before.input_tokens,
                target,
            )
            return messages, 0

        tokens_to_save = max(0, before.input_tokens - target)
        if force_one:
            tokens_to_save = max(1, tokens_to_save)
        if tokens_to_save <= 0:
            return messages, 0

        logger.warning(
            "[CONTEXT FALLBACK START] reason=%s estimated_before=%d target=%d "
            "pending=%d",
            reason,
            before.input_tokens,
            target,
            len(self._tool_call_ids_pending_visibility),
        )
        self._emit_status(
            "History compression failed — pruning confirmed tool results..."
        )
        pruned_messages, pruned, _saved = compressor.prune_confirmed_tool_results(
            messages,
            tokens_to_save=tokens_to_save,
            protected_tool_call_ids=self._tool_call_ids_pending_visibility,
        )
        after = estimate_request_budget(
            messages=pruned_messages,
            tools=self.tools,
            dynamic_system_context=dynamic_system_context,
            context=self.provider_runtime,
            context_window=compressor.context_length,
        )
        outcome = (
            "target_met"
            if after.input_tokens <= target
            else "partial"
            if pruned
            else "no_candidates"
        )
        logger.warning(
            "[CONTEXT FALLBACK END] reason=%s pruned=%d saved_tokens=%d "
            "estimated_after=%d target=%d pending=%d outcome=%s",
            reason,
            pruned,
            max(0, before.input_tokens - after.input_tokens),
            after.input_tokens,
            target,
            len(self._tool_call_ids_pending_visibility),
            outcome,
        )
        if pruned:
            # The previous provider prompt count described a different payload.
            compressor.last_prompt_tokens = 0
        return pruned_messages, pruned

    # ── Main conversation loop ──

    def run_conversation(
        self,
        user_message: str,
        conversation_history: List[Dict] = None,
        disable_tools: bool = False,
        extra_system: str = "",
        advance_background_review: bool = True,
        *,
        call_source: str = "turn",
        deadline_monotonic: float | None = None,
    ) -> Dict[str, Any]:
        """Run one conversation turn and discard stale estimates on failure."""
        try:
            return self._run_conversation_impl(
                user_message,
                conversation_history=conversation_history,
                disable_tools=disable_tools,
                extra_system=extra_system,
                advance_background_review=advance_background_review,
                call_source=call_source,
                deadline_monotonic=deadline_monotonic,
            )
        except Exception:
            self._clear_unconfirmed_context_display()
            raise

    def _run_conversation_impl(
        self,
        user_message: str,
        conversation_history: List[Dict] = None,
        disable_tools: bool = False,
        extra_system: str = "",
        advance_background_review: bool = True,
        *,
        call_source: str = "turn",
        deadline_monotonic: float | None = None,
    ) -> Dict[str, Any]:
        """Run one conversation turn through API, tools, persistence, and review hooks.

        Returns dict with: final_response, messages, model, session_id,
        api_calls, assistant_rounds, and any pending handoff metadata needed by
        the hosting runtime.
        """
        self.clear_interrupt()
        self._memory_changed_in_turn = False
        self._skills_changed_in_turn = False
        with self._usage_lock:
            self._turn_usage = {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "reasoning_tokens": 0,
                "api_calls": 0,
            }

        # Subagents pass None when they should not inherit the parent history.
        messages = list(conversation_history if conversation_history is not None else [])
        if self._prompt_epoch_dirty:
            self._refresh_prompt_epoch(messages)

        # Reset prior token counts for fresh sessions so heavy previous tasks
        # do not trigger unnecessary preventive compression.
        if self.context_compressor and conversation_history is None:
            self.context_compressor.last_prompt_tokens = 0
            self.context_compressor.last_completion_tokens = 0

        if not messages or messages[0].get("role") != "system":
            system_prompt_text = self._build_system_prompt()
            logger.info("[SYSTEM PROMPT]\n%s", system_prompt_text)
            messages.insert(0, {"role": "system", "content": system_prompt_text})

        self._checkpoint_turn_id = uuid.uuid4().hex[:12]
        self._checkpoint_messages_len_before_turn = len(messages)
        self._checkpoint_message_id_before_turn = None
        self._tool_operation_ids = {}
        if self._session_db and hasattr(self._session_db, "get_last_message_id"):
            try:
                self._checkpoint_message_id_before_turn = self._session_db.get_last_message_id(self.session_id)
            except Exception:
                logger.debug("Could not capture checkpoint message marker", exc_info=True)

        messages.append({"role": "user", "content": user_message})

        # Background review counters are advanced only after this user turn completes.
        _should_review_memory = False

        # Prefetch relevant memory for this turn; inject it only at API-call time.
        if self._memory_manager:
            try:
                self._recalled_memory = self._memory_manager.prefetch_all(
                    user_message, session_id=self.session_id
                ) or ""
            except Exception as e:
                logger.debug("Memory prefetch failed: %s", e)
                self._recalled_memory = ""
        else:
            self._recalled_memory = ""
        dynamic_system_context = "\n\n".join(
            part for part in (extra_system, self._recalled_memory) if part
        )
        cache_enabled = bool(
            self.config.get("prompt_cache", {}).get("enabled", True)
        )

        if self._session_db:
            self._session_db.append_message(
                self.session_id,
                "user",
                content=user_message,
                turn_id=self._checkpoint_turn_id,
            )
            if hasattr(self._session_db, "count_user_messages"):
                try:
                    self.session_user_messages = self._session_db.count_user_messages(self.session_id)
                except Exception:
                    self.session_user_messages += 1
            else:
                self.session_user_messages += 1
        else:
            self.session_user_messages += 1

        api_call_count = 0
        assistant_iteration_count = 0
        api_call_limit = self.max_iterations if getattr(self, "_delegate_depth", 0) > 0 else None
        final_response = ""
        final_response_recorded = False
        interrupted = False
        incomplete_response = False
        stop_reason: str | None = None
        assistant_rounds = []
        final_result: ModelCallResult | None = None

        def _deadline_reached() -> bool:
            return bool(
                deadline_monotonic is not None
                and time.monotonic() >= deadline_monotonic
            )

        # Temporarily disable tools when disable_tools=True (e.g. synthesis turn)
        original_tools = self.tools
        if disable_tools:
            self.tools = []

        while (
            (self.max_iterations is None or assistant_iteration_count < self.max_iterations)
            and (api_call_limit is None or api_call_count < api_call_limit)
        ):
            if _deadline_reached():
                stop_reason = "timeout"
                logger.info("[LOOP] delegation deadline reached before next iteration")
                break
            assistant_iteration_count += 1
            logger.info("[LOOP] starting iteration %d", assistant_iteration_count)
            if self._interrupted:
                interrupted = True
                break

            # Successful write payloads shrink at one boundary for fresh,
            # resumed, and post-tool requests. Pending writes remain raw until
            # a successful main-model response proves visibility once.
            pre_api_pruned = 0
            if self.context_compressor:
                messages, pre_api_pruned = self.context_compressor.prune(
                    messages,
                    protected_tool_call_ids=self._tool_call_ids_pending_visibility,
                )
                logger.info(
                    "[PRE-API WRITE PRUNE] messages=%d pruned=%d pending=%d",
                    len(messages),
                    pre_api_pruned,
                    len(self._tool_call_ids_pending_visibility),
                )

            # Subagent diagnostics: log each API iteration to locate stalls.
            if getattr(self, "_delegate_depth", 0) > 0:
                logger.info(
                    "[subagent-%s] API iteration %d/%s",
                    getattr(self, "session_id", "?")[-6:], assistant_iteration_count, self.max_iterations
                )

            # Clear the per-turn compression circuit breaker before the next API call.
            if self.context_compressor:
                self.context_compressor._compressed_this_turn = False

            # Preventive compression: estimate current context before the API call.
            # last_prompt_tokens only reflects the previous API call, while
            # message history keeps growing.
            if self.context_compressor:
                cc = self.context_compressor
                if not cc._compressed_this_turn:
                    logger.info("[LOOP] estimating tokens for preventive compression")
                    budget = estimate_request_budget(
                        messages=messages,
                        tools=self.tools,
                        dynamic_system_context=dynamic_system_context,
                        context=self.provider_runtime,
                        context_window=cc.context_length,
                    )
                    logger.info(
                        "[LOOP] estimated=%d output_budget=%d last_prompt=%d last_completion=%d",
                        budget.input_tokens,
                        budget.output_budget,
                        cc.last_prompt_tokens,
                        cc.last_completion_tokens,
                    )
                    # A provider prompt count from before pruning describes a
                    # different payload and must not force a stale compression.
                    check_tokens = (
                        budget.input_tokens
                        if pre_api_pruned
                        else max(budget.input_tokens, cc.last_prompt_tokens)
                    )
                    compression_limit = min(
                        cc.threshold_tokens,
                        max(1, budget.context_window - budget.output_budget),
                    )
                    if check_tokens >= compression_limit:
                        logger.info("[LOOP] preventive compression triggered (check=%d >= threshold=%d)", check_tokens, compression_limit)
                        self._emit_status(
                            "Compressing history before the model request — "
                            "the current task will continue..."
                        )
                        self.flush_memories(messages)
                        messages = cc.compress(
                            messages,
                            protected_tool_call_ids=self._tool_call_ids_pending_visibility,
                        )
                        self._refresh_memory_snapshot()
                        logger.info("[LOOP] memory snapshot refreshed")
                        self._refresh_prompt_epoch(messages)
                        if cc._compressed_this_turn:
                            logger.info("[LOOP] compression done")
                            post_summary_budget = estimate_request_budget(
                                messages=messages,
                                tools=self.tools,
                                dynamic_system_context=dynamic_system_context,
                                context=self.provider_runtime,
                                context_window=cc.context_length,
                            )
                            fallback_target = max(
                                1,
                                int(
                                    compression_limit
                                    * _COMPRESSION_FALLBACK_TARGET_RATIO
                                ),
                            )
                            if post_summary_budget.input_tokens > fallback_target:
                                messages, fallback_pruned = (
                                    self._fallback_prune_confirmed_tool_results(
                                        messages,
                                        dynamic_system_context=dynamic_system_context,
                                        reason="preventive_summary_insufficient",
                                    )
                                )
                                if fallback_pruned:
                                    logger.warning(
                                        "[LOOP] summary remained above target; "
                                        "confirmed-tool fallback applied"
                                    )
                        else:
                            outcome = getattr(
                                cc,
                                "last_compression_outcome",
                                "compression_unavailable",
                            )
                            messages, fallback_pruned = (
                                self._fallback_prune_confirmed_tool_results(
                                    messages,
                                    dynamic_system_context=dynamic_system_context,
                                    reason=f"preventive_{outcome}",
                                )
                            )
                            if fallback_pruned:
                                logger.warning(
                                    "[LOOP] summary compression unavailable; "
                                    "confirmed-tool fallback applied"
                                )
                            else:
                                logger.warning(
                                    "[LOOP] compression unavailable; continuing "
                                    "without compaction"
                                )
                    else:
                        logger.info("[LOOP] no preventive compression needed (check=%d < threshold=%d)", check_tokens, compression_limit)

            # ── API call with retry ──
            result: ModelCallResult | None = None
            retry_count = 0
            context_recovery_attempts = 0

            while retry_count < MAX_RETRIES:
                if _deadline_reached():
                    stop_reason = "timeout"
                    break
                if api_call_limit is not None and api_call_count >= api_call_limit:
                    break
                if self._interrupted:
                    interrupted = True
                    break
                try:
                    if self._prompt_epoch_dirty:
                        self._refresh_prompt_epoch(messages)
                    # Emit one display estimate per runtime/model epoch. Backend
                    # request estimates above still run on every iteration for
                    # compression decisions.
                    if (
                        self.context_compressor
                        and not getattr(
                            self.context_compressor,
                            "_display_estimate_emitted",
                            False,
                        )
                    ):
                        display_budget = estimate_request_budget(
                            messages=messages,
                            tools=self.tools,
                            dynamic_system_context=dynamic_system_context,
                            context=self.provider_runtime,
                            context_window=self.context_compressor.context_length,
                        )
                        self.context_compressor.display_context_tokens = (
                            display_budget.input_tokens
                        )
                        self.context_compressor.display_context_estimated = True
                        self.context_compressor._display_estimate_emitted = True
                        logger.info(
                            "[TUI CONTEXT] source=estimate tokens=%d",
                            display_budget.input_tokens,
                        )
                    self._emit_status("Requesting model...")
                    # Log the full message list before API calls to diagnose context growth.
                    try:
                        _msgs_log = []
                        for i, m in enumerate(messages):
                            _entry = {"index": i, "role": m.get("role", "?")}
                            _content = m.get("content", "")
                            if isinstance(_content, str):
                                _entry["content_preview"] = _redact_log_secrets(_content[:200])
                                _entry["content_length"] = len(_content)
                            else:
                                _entry["content_preview"] = _redact_log_secrets(str(_content)[:200])
                                _entry["content_length"] = len(str(_content))
                            if m.get("tool_calls"):
                                _tcs = m["tool_calls"]
                                _entry["tool_calls_count"] = len(_tcs)
                                _entry["tool_calls_preview"] = [
                                    {"name": (tc.get("function", {}) or {}).get("name", "?"),
                                     "args_preview": _redact_log_secrets(((tc.get("function", {}) or {}).get("arguments", "")[:100]))}
                                    for tc in _tcs
                                ]
                            if m.get("tool_call_id"):
                                _entry["tool_call_id"] = m["tool_call_id"]
                            _msgs_log.append(_entry)
                        _total_chars = sum(e.get("content_length", 0) for e in _msgs_log)
                        logger.info("[MESSAGES BEFORE API CALL] total_messages=%d total_chars=%d details=%s", len(messages), _total_chars, json.dumps(_redact_log_secrets(_msgs_log), ensure_ascii=False, default=str))
                    except Exception:
                        pass

                    if getattr(self, "_delegate_depth", 0) > 0:
                        logger.info("[subagent-%s] 开始 API 调用", getattr(self, "session_id", "?")[-6:])
                    api_call_count += 1
                    self._record_api_attempt()
                    result = self.transport.call(
                        messages=messages,
                        tools=self.tools,
                        options=ModelCallOptions(
                            stream=bool(self._stream_callback),
                            timeout=30.0,
                            source=call_source,
                            dynamic_system_context=dynamic_system_context,
                            cache_plan=build_prompt_cache_plan(
                                messages=messages,
                                tools=self.tools,
                                context=self.provider_runtime,
                                session_id=self.session_id,
                                enabled=cache_enabled,
                            ),
                        ),
                        stream_callback=self._stream_callback,
                        interrupted=lambda: self._interrupted,
                    )
                    if getattr(self, "_delegate_depth", 0) > 0:
                        logger.info("[subagent-%s] API 调用完成", getattr(self, "session_id", "?")[-6:])
                    break
                except ModelCallError as error:
                    if (
                        error.context_limit
                        and self.context_compressor
                        and context_recovery_attempts < 2
                    ):
                        if context_recovery_attempts == 0:
                            context_recovery_attempts = 1
                            self._emit_status(
                                "Context overflow — compressing and retrying..."
                            )
                            self.flush_memories(messages)
                            self._refresh_memory_snapshot()
                            self._refresh_prompt_epoch(messages)
                            before_budget = estimate_request_budget(
                                messages=messages,
                                tools=self.tools,
                                dynamic_system_context=dynamic_system_context,
                                context=self.provider_runtime,
                                context_window=self.context_compressor.context_length,
                            )
                            compressed_messages = self.context_compressor.compress(
                                messages,
                                protected_tool_call_ids=(
                                    self._tool_call_ids_pending_visibility
                                ),
                            )
                            after_budget = estimate_request_budget(
                                messages=compressed_messages,
                                tools=self.tools,
                                dynamic_system_context=dynamic_system_context,
                                context=self.provider_runtime,
                                context_window=self.context_compressor.context_length,
                            )
                            summary_reduced = (
                                after_budget.input_tokens < before_budget.input_tokens
                            )
                            candidate_messages = (
                                compressed_messages if summary_reduced else messages
                            )
                            candidate_budget = (
                                after_budget if summary_reduced else before_budget
                            )
                            recovery_limit = min(
                                self.context_compressor.threshold_tokens,
                                max(
                                    1,
                                    candidate_budget.context_window
                                    - candidate_budget.output_budget,
                                ),
                            )
                            recovery_target = max(
                                1,
                                int(
                                    recovery_limit
                                    * _COMPRESSION_FALLBACK_TARGET_RATIO
                                ),
                            )
                            fallback_pruned = 0
                            if (
                                not summary_reduced
                                or candidate_budget.input_tokens > recovery_target
                            ):
                                outcome = getattr(
                                    self.context_compressor,
                                    "last_compression_outcome",
                                    "compression_unavailable",
                                )
                                candidate_messages, fallback_pruned = (
                                    self._fallback_prune_confirmed_tool_results(
                                        candidate_messages,
                                        dynamic_system_context=dynamic_system_context,
                                        reason=f"context_overflow_{outcome}",
                                        force_one=True,
                                    )
                                )
                            if summary_reduced or fallback_pruned:
                                messages = candidate_messages
                                continue
                            logger.warning(
                                "Context overflow recovery did not reduce history "
                                "(messages=%d tokens=%d)",
                                len(messages),
                                before_budget.input_tokens,
                            )
                        else:
                            context_recovery_attempts = 2
                            fallback_messages, fallback_pruned = (
                                self._fallback_prune_confirmed_tool_results(
                                    messages,
                                    dynamic_system_context=dynamic_system_context,
                                    reason="context_overflow_after_compacted_retry",
                                    force_one=True,
                                )
                            )
                            if fallback_pruned:
                                messages = fallback_messages
                                continue
                            logger.warning(
                                "Context overflow fallback has no remaining "
                                "confirmed tool results"
                            )
                    if (
                        error.retryable
                        and not error.context_limit
                        and retry_count < MAX_RETRIES - 1
                        and (api_call_limit is None or api_call_count < api_call_limit)
                    ):
                        retry_count += 1
                        wait = (
                            error.retry_after
                            if error.retry_after is not None
                            else jittered_backoff(retry_count)
                        )
                        logger.warning(
                            "API error (attempt %d/%d), retrying in %.1fs: %s",
                            retry_count, MAX_RETRIES, wait, error,
                        )
                        self._emit_status(f"重试中，等待 {wait:.0f}s...")
                        deadline = time.time() + wait
                        while time.time() < deadline:
                            if self._interrupted:
                                break
                            time.sleep(0.2)
                        continue
                    logger.error("API error (not retried): %s", error)
                    self._clear_unconfirmed_context_display()
                    final_response = f"API Error: {error}"
                    self.messages = messages
                    if disable_tools:
                        self.tools = original_tools
                    return {
                        "final_response": final_response,
                        "messages": messages,
                        "model": self.model,
                        "session_id": self.session_id,
                        "api_calls": api_call_count,
                        "error": str(error),
                        "interrupted": interrupted,
                        "stop_reason": stop_reason,
                        "completed": False,
                        "assistant_rounds": assistant_rounds,
                        "token_usage": self._finish_turn_usage(),
                    }
                except Exception:
                    self._clear_unconfirmed_context_display()
                    raise

            if self._interrupted:
                interrupted = True

            if result is not None and result.usage is not None:
                self._record_usage(result.usage)
            elif result is not None:
                self._clear_unconfirmed_context_display()
            if result is not None and result.interrupted:
                interrupted = True

            response_confirms_visibility = bool(
                result
                and model_response_confirms_visibility(
                    result.finish_reason,
                    interrupted=result.interrupted,
                )
            )
            if result is not None and not result.interrupted and not response_confirms_visibility:
                incomplete_response = True
                stop_reason = result.finish_reason or "incomplete_response"
                logger.warning(
                    "Incomplete model stream retained without releasing pending "
                    "tool visibility: finish_reason=%s pending=%d",
                    result.finish_reason,
                    len(self._tool_call_ids_pending_visibility),
                )

            if interrupted or result is None:
                if result is None:
                    self._clear_unconfirmed_context_display()
                break

            if response_confirms_visibility:
                # A successful model response proves the pending tool calls and
                # paired results were visible once.
                self._tool_visibility_state_reliable = True
                self._tool_call_ids_pending_visibility.clear()

            assistant_content = result.content
            tool_calls = result.tool_calls if response_confirms_visibility else None
            finish = result.finish_reason
            reasoning_text = result.reasoning.text if result.reasoning else None

            finish_after_cleanup = (
                bool(assistant_content and assistant_content.strip())
                and self._is_cleanup_only_tool_batch(tool_calls)
            )
            round_event = self._build_assistant_round_event(
                api_call_index=api_call_count,
                content=assistant_content,
                reasoning_content=reasoning_text,
                tool_calls=tool_calls,
                finish_reason=finish,
                was_streamed=result.was_streamed,
                is_final_override=(
                    False
                    if incomplete_response
                    else True
                    if finish_after_cleanup
                    else None
                ),
            )
            assistant_rounds.append(round_event)
            self._emit_event(round_event)

            # ── Tool calls present → dispatch and continue ──
            if tool_calls:
                # Establish protection as soon as the model creates the batch,
                # before any tool starts or a pause/exception can intervene.
                self._tool_call_ids_pending_visibility = {
                    tc.get("id")
                    for tc in tool_calls
                    if isinstance(tc, dict) and tc.get("id")
                }

                # Persist to session DB independently from the API messages list.
                if self._session_db:
                    self._session_db.append_message(
                        self.session_id, "assistant",
                        content=assistant_content,
                        tool_calls=[tc for tc in tool_calls],
                        finish_reason=finish,
                        **(result.reasoning.to_message_fields() if result.reasoning else {}),
                        turn_id=self._checkpoint_turn_id,
                    )

                # _execute_tool_calls appends the assistant message internally, so
                # do not append it again here. Non-blocking delegate_task returns
                # pending metadata that is checked below to avoid unnecessary work.
                pending_result = self._execute_tool_calls(
                    tool_calls,
                    messages,
                    assistant_content=assistant_content,
                    reasoning=result.reasoning,
                )
                logger.info("[POST-TOOL] _execute_tool_calls returned, pending=%s", pending_result is not None)
                if pending_result is not None:
                    # delegate_task started in non-blocking mode; return for TUI polling.
                    # Restore tools before returning so they are not left disabled.
                    if disable_tools:
                        self.tools = original_tools
                    pending_result["assistant_rounds"] = assistant_rounds
                    pending_result["api_calls"] = api_call_count
                    pending_result["token_usage"] = self._finish_turn_usage()
                    return pending_result

                if finish_after_cleanup:
                    logger.info(
                        "[POST-TOOL] cleanup-only tool call after assistant content; "
                        "finishing without another API call"
                    )
                    final_response = assistant_content or ""
                    final_response_recorded = True
                    break

                logger.info("[POST-TOOL] continuing to next API iteration")
                continue

            # No tool calls: enter final response handling.
            final_response = assistant_content or ""
            if self._stream_callback and not final_response:
                final_response = ""

            assistant_msg = {"role": "assistant", "content": final_response}
            if result.reasoning:
                assistant_msg.update(result.reasoning.to_message_fields())
            messages.append(assistant_msg)
            final_result = result
            if getattr(self, "_delegate_depth", 0) > 0:
                logger.info("[subagent-%s] no tool_calls, breaking loop", self.session_id[-6:])
            break
        else:
            stop_reason = "max_iterations"
            logger.info("[LOOP] iteration limit reached (%s)", self.max_iterations)

        if getattr(self, "_delegate_depth", 0) > 0:
            logger.info("[subagent-%s] exited loop, api_calls=%d", self.session_id[-6:], api_call_count)

        if self._session_db and final_response and not final_response_recorded:
            self._session_db.append_message(
                self.session_id,
                "assistant",
                content=final_response,
                finish_reason=final_result.finish_reason if final_result else None,
                **(
                    final_result.reasoning.to_message_fields()
                    if final_result and final_result.reasoning
                    else {}
                ),
                turn_id=self._checkpoint_turn_id,
            )

        self.messages = messages
        turn_usage = self._finish_turn_usage()

        _should_review_skills = False
        completed_user_turn = bool(
            advance_background_review
            and not interrupted
            and not incomplete_response
            and final_response
        )
        if completed_user_turn:
            if (
                self._memory_manager
                and getattr(self, "_memory_review_round", 0) > 0
            ):
                if getattr(self, "_memory_changed_in_turn", False):
                    self._turns_since_memory_review = 0
                else:
                    self._turns_since_memory_review = getattr(self, "_turns_since_memory_review", 0) + 1
                    if self._turns_since_memory_review >= getattr(self, "_memory_review_round", 0):
                        _should_review_memory = True
                        self._turns_since_memory_review = 0

            if (
                getattr(self, "_evolution_review_round", 0) > 0
                and "skill_manage" in self.valid_tool_names
            ):
                if getattr(self, "_skills_changed_in_turn", False):
                    self._turns_since_evolution_review = 0
                else:
                    self._turns_since_evolution_review = getattr(self, "_turns_since_evolution_review", 0) + 1
                    if self._turns_since_evolution_review >= getattr(self, "_evolution_review_round", 0):
                        _should_review_skills = True
                        self._turns_since_evolution_review = 0

        # Background memory/Skill reviews start after the reply completes so
        # they do not compete with the user task. Subagents are temporary and
        # must not spawn nested background reviews.
        if (_should_review_memory or _should_review_skills) and getattr(self, "_delegate_depth", 0) == 0:
            try:
                if _should_review_memory and _should_review_skills:
                    self._emit_status("触发后台记忆与技能审查...")
                elif _should_review_memory:
                    self._emit_status("触发后台记忆审查...")
                elif _should_review_skills:
                    self._emit_status("触发后台技能审查...")
                self._spawn_background_review(
                    messages_snapshot=list(messages),
                    review_memory=_should_review_memory,
                    review_skills=_should_review_skills,
                )
            except Exception as exc:
                logger.debug("Background review scheduling failed: %s", exc)

        # Restore tools if this turn temporarily disabled them.
        if disable_tools:
            self.tools = original_tools

        return {
            "final_response": final_response,
            "messages": messages,
            "model": self.model,
            "session_id": self.session_id,
            "api_calls": api_call_count,
            "interrupted": interrupted,
            "stop_reason": stop_reason,
            "completed": bool(
                not interrupted and not incomplete_response and final_response
            ),
            "assistant_rounds": assistant_rounds,
            "token_usage": turn_usage,
            "skills_changed": bool(getattr(self, "_skills_changed_in_turn", False)),
        }

    # ── Tool execution ──

    def _build_assistant_msg(
        self,
        content: str,
        tool_calls: List[Dict],
        reasoning: ReasoningTrace | None = None,
    ) -> Dict:
        msg = {"role": "assistant", "content": content, "tool_calls": tool_calls}
        if reasoning:
            msg.update(reasoning.to_message_fields())
        return msg

    def _execute_tool_calls(
        self,
        tool_calls: List[Dict],
        messages: List[Dict],
        assistant_content: str = "",
        reasoning: ReasoningTrace | None = None,
    ):
        """Dispatch tool calls and append normalized results to the conversation.

        The dispatcher may run read-only tools concurrently, but write-capable
        tools share the checkpoint manager so filesystem recovery metadata stays
        aligned with the assistant tool call that produced it.
        """
        from mclaw.tools.dispatch import handle_function_calls, set_tool_context

        if not tool_calls:
            return

        is_subagent = getattr(self, "_delegate_depth", 0) > 0
        _sid_tail = self.session_id[-6:] if self.session_id else "?"

        # Set per-call context so tools such as session_search can access SessionDB.
        set_tool_context(session_db=self._session_db, session_id=self.session_id)

        if self._interrupted:
            for tc in tool_calls:
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": json.dumps({"error": "Interrupted by user"}),
                })
            return

        assistant_msg = self._build_assistant_msg(
            assistant_content or "",
            tool_calls,
            reasoning=reasoning,
        )
        messages.append(assistant_msg)

        checkpoint_mgr = self._get_checkpoint_manager()
        checkpoint_mgr.new_turn()

        # Batch dispatch: write tools take the serialized CheckpointManager path.
        # Read-only batches choose the concurrent path inside handle_function_calls.
        if self._tool_callback:
            for tc in tool_calls:
                fn = tc.get("function", {})
                try:
                    args = json.loads(fn.get("arguments", "{}"))
                except (json.JSONDecodeError, TypeError):
                    args = {}
                self._tool_callback(fn.get("name", ""), args)

        # Filter disabled tools.
        disabled = set(getattr(self, "config", {}).get("tools", {}).get("disabled", []))
        filtered_calls = []
        has_install_prepare = False
        for tc in tool_calls:
            fn = tc.get("function", {})
            fn_name = fn.get("name", "?")
            if fn_name in disabled:
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": json.dumps({"error": f"Tool {fn_name} is disabled by project configuration"}),
                })
                continue
            if fn_name == "skill_manage":
                try:
                    args = json.loads(fn.get("arguments", "{}"))
                except (json.JSONDecodeError, TypeError):
                    args = {}
                if str(args.get("action") or "").strip() == "install_prepare":
                    if has_install_prepare:
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": json.dumps(
                                {
                                    "success": False,
                                    "error": (
                                        "Only one skill_manage(action='install_prepare') "
                                        "call is allowed per tool batch."
                                    ),
                                }
                            ),
                        })
                        continue
                    has_install_prepare = True
            filtered_calls.append(tc)
        tool_calls = filtered_calls

        tool_names = [tc.get("function", {}).get("name", "?") for tc in tool_calls]
        logger.info("[TOOL DISPATCH START] tools=%s count=%d", tool_names, len(tool_calls))
        self._emit_status(f"Running {len(tool_calls)} tool(s)...")

        # Subagent diagnostics: log tool names before dispatch without polluting parent logs.
        if is_subagent:
            logger.info("[subagent-%s] dispatching tools: %s", _sid_tail, tool_names)

        results = handle_function_calls(
            calls=tool_calls,
            tool_names=set(self.valid_tool_names),
            memory_manager=self._memory_manager,
            checkpoint_manager=checkpoint_mgr,
            parent_agent=self,
        )

        logger.info("[TOOL DISPATCH END] tools=%s results=%d", tool_names, len(results))
        if is_subagent:
            logger.info("[subagent-%s] tools returned (%d results)", _sid_tail, len(results))

        if self._tool_end_callback:
            logger.info("[POST-TOOL] calling _tool_end_callback")
            self._tool_end_callback()
            logger.info("[POST-TOOL] _tool_end_callback returned")

        memory_prompt_changed = False
        try:
            from mclaw.tools.memory_tool import MEMORY_WRITE_TOOL_NAMES
        except Exception as exc:
            logger.debug("Memory write metadata unavailable: %s", exc)
            MEMORY_WRITE_TOOL_NAMES = set()
        for tc, result in zip(tool_calls, results):
            fn = tc.get("function", {})
            if fn.get("name") not in MEMORY_WRITE_TOOL_NAMES:
                continue
            try:
                result_data = json.loads(result)
                if result_data.get("success"):
                    self._memory_changed_in_turn = True
                    memory_prompt_changed = True
                    if getattr(self, "_memory_review_round", 0) > 0:
                        self._turns_since_memory_review = 0
                    break
            except (json.JSONDecodeError, TypeError, AttributeError) as exc:
                logger.debug("Memory write result could not be parsed: %s", exc)

        # Reset the Skill review counter only after skill_manage truly succeeds.
        # The counter may have been pre-reset before execution; this is the final correction.
        if getattr(self, "_evolution_review_round", 0) > 0 and "skill_manage" in self.valid_tool_names:
            for tc, result in zip(tool_calls, results):
                fn = tc.get("function", {})
                if fn.get("name") != "skill_manage":
                    continue
                try:
                    result_data = json.loads(result)
                    action = str(result_data.get("action") or "").strip()
                    if result_data.get("success") and action in SKILL_WRITE_ACTIONS:
                        self._turns_since_evolution_review = 0
                        break  # only one skill_manage per batch
                except (json.JSONDecodeError, TypeError) as exc:
                    logger.debug("Memory write result could not be parsed: %s", exc)

        pending_delegate_data = None
        pending_skill_import_confirmation = None
        skill_prompt_changed = False

        # Append tool results in call order.
        for i, tc in enumerate(tool_calls):
            result = results[i] if i < len(results) else json.dumps(
                {"error": "No result returned"}
            )
            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": result,
            })

            fn = tc.get("function", {})
            name = fn.get("name", "")
            if name == "delegate_task":
                try:
                    result_data = json.loads(result)
                    if result_data.get("pending") and result_data.get("success"):
                        pending_delegate_data = result_data
                except Exception:
                    pass
            elif name == "skill_manage":
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                    action = str(args.get("action") or "").strip()
                    result_data = json.loads(result)
                    if result_data.get("success") and action in SKILL_WRITE_ACTIONS:
                        self._skills_changed_in_turn = True
                        skill_prompt_changed = True
                    if (
                        result_data.get("requires_confirmation")
                        and result_data.get("confirmation_type") == "skill_enable_drafting"
                    ):
                        pending_skill_import_confirmation = result_data
                except Exception:
                    pass

            if self._session_db:
                self._session_db.append_message(
                    self.session_id, "tool",
                    content=result,
                    tool_name=name,
                    tool_call_id=tc["id"],
                    turn_id=self._checkpoint_turn_id,
                    operation_id=getattr(self, "_tool_operation_ids", {}).get(tc["id"]),
                )

        if memory_prompt_changed or skill_prompt_changed:
            self._refresh_prompt_epoch(messages)

        # Check whether a subagent is running in pending mode. The parent turn's
        # assistant tool call and tool results are already stored before the TUI
        # starts polling; subagent internals are not injected here.
        if pending_delegate_data:
            self.messages = messages
            return {
                "final_response": None,
                "messages": messages,
                "model": self.model,
                "session_id": self.session_id,
                "api_calls": getattr(self, 'session_api_calls', 0),
                "interrupted": False,
                "pending_delegate": True,
                "pending_data": pending_delegate_data,
                "skills_changed": bool(getattr(self, "_skills_changed_in_turn", False)),
            }

        if pending_skill_import_confirmation:
            self.messages = messages
            logger.info(
                "[SKILL ENABLE] returning pending confirmation to CLI drafting_id=%s risk=%s",
                pending_skill_import_confirmation.get("drafting_id"),
                pending_skill_import_confirmation.get("risk_level"),
            )
            return {
                "final_response": None,
                "messages": messages,
                "model": self.model,
                "session_id": self.session_id,
                "api_calls": getattr(self, 'session_api_calls', 0),
                "interrupted": False,
                "pending_skill_import_confirmation": True,
                "confirmation_data": pending_skill_import_confirmation,
                "skills_changed": bool(getattr(self, "_skills_changed_in_turn", False)),
            }

    # ── Usage tracking ──

    def _record_api_attempt(self, *, include_in_turn: bool = True) -> None:
        with self._usage_lock:
            self.session_api_calls += 1
            if include_in_turn and self._turn_usage is not None:
                self._turn_usage["api_calls"] += 1

    def _record_usage(
        self,
        record: UsageRecord,
        *,
        include_in_turn: bool = True,
    ) -> None:
        if not isinstance(record, UsageRecord):
            raise TypeError("record must be a UsageRecord")
        delta = record.to_counter_delta()
        with self._usage_lock:
            for name, value in delta.items():
                setattr(self, f"session_{name}", getattr(self, f"session_{name}") + value)
                if include_in_turn and self._turn_usage is not None:
                    self._turn_usage[name] += value

        if self._session_db and delta:
            try:
                self._session_db.update_token_counts(self.session_id, **delta)
            except Exception:
                logger.warning("Failed to persist provider usage", exc_info=True)
        if self.context_compressor and record.source == "turn":
            self.context_compressor.update_from_response(record.to_compressor_update())
            if record.input_tokens is not None:
                self.context_compressor.display_context_tokens = record.input_tokens
                self.context_compressor.display_context_estimated = False
                logger.info(
                    "[TUI CONTEXT] source=provider tokens=%d",
                    record.input_tokens,
                )
            else:
                self._clear_unconfirmed_context_display()
        if self._usage_sink is not None:
            try:
                self._usage_sink(record)
            except Exception:
                logger.warning("Usage sink failed", exc_info=True)

    def _finish_turn_usage(self) -> dict[str, int]:
        with self._usage_lock:
            usage = self._turn_usage or {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "reasoning_tokens": 0,
                "api_calls": 0,
            }
            self._turn_usage = None
            return dict(usage)
