"""M-Claw Agent Core — conversation loop with tool calling.

Handles multi-turn chat with LLM providers, tool call dispatch,
retry with jittered backoff, and session persistence.
"""

import json
import logging
import re
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Set

import anthropic
import openai

from mclaw.agent.background_review import spawn_background_review
from mclaw.agent.context_compressor import ContextCompressor
from mclaw.agent.memory_manager import MemoryManager
from mclaw.agent.prompt_builder import build_system_prompt
from mclaw.prompts.background import build_memory_flush_system_prompt
from mclaw.agent.retry_utils import (
    get_retry_after,
    is_retryable_error,
    jittered_backoff,
)
from mclaw.state import SessionDB
from mclaw.tools.interrupt import is_interrupted, set_interrupt

logger = logging.getLogger(__name__)

MAX_RETRIES = 5

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
    """Core agent that manages the conversation loop with an LLM."""

    def __init__(
        self,
        model: str = "",
        api_key: str = "",
        base_url: str = "",
        api_mode: str = "chat_completions",
        provider: str = "",
        system_prompt: str = "",
        session_db: SessionDB = None,
        session_id: str = None,
        parent_session_id: str = None,
        max_iterations: int = 50,
        platform: str = "cli",
        enabled_toolsets: List[str] = None,
        stream_callback: Callable = None,
        tool_callback: Callable = None,
        tool_end_callback: Callable = None,
        status_callback: Callable = None,
        event_callback: Callable = None,
        print_fn: Callable = None,
        workspace: str = None,
        # 子代理隔离标记。
        skip_memory: bool = False,
        skip_context_files: bool = False,
        skip_skills: bool = False,
        config: dict | None = None,
    ):
        self.model = model
        self.api_key = api_key
        self.base_url = base_url
        self.api_mode = api_mode
        self.provider = provider
        self.system_prompt = system_prompt
        self.max_iterations = max_iterations
        self.platform = platform
        self.enabled_toolsets = enabled_toolsets

        self.config = config or {}
        self.session_id = session_id or f"session_{uuid.uuid4().hex[:12]}"
        self.workspace_path = str(workspace or "").strip()
        self._session_db = session_db
        self._print_fn = print_fn or print
        self._stream_callback = stream_callback
        self._tool_callback = tool_callback
        self._tool_end_callback = tool_end_callback
        self._status_callback = status_callback
        self._event_callback = event_callback

        # 子代理隔离标记。
        self._skip_memory = skip_memory
        self._skip_context_files = skip_context_files
        self._skip_skills = skip_skills
        self._delegate_depth: int = 0

        self.messages: List[Dict[str, Any]] = []
        self._interrupted = False
        self._is_anthropic = api_mode == "anthropic_messages"

        # 记忆子系统。
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

        # 主动记忆提示计数器。

        # 主动 Skill 创建提示计数器，按工具调用迭代统计。

        # token 使用量跟踪。
        self.session_input_tokens = 0
        self.session_output_tokens = 0
        self.session_api_calls = 0
        self.session_user_messages = 0

        # 文件系统 checkpoint。它是透明基础设施，不暴露给模型。
        self._checkpoint_mgr = self._build_checkpoint_manager()
        self._checkpoint_turn_id: Optional[str] = None
        self._checkpoint_message_id_before_turn: Optional[int] = None
        self._checkpoint_messages_len_before_turn: int = 0
        self._last_checkpoint_work_dir: Optional[str] = None
        self._last_checkpoint_targets: List[str] = []
        self._last_checkpoint_attempt: Dict[str, Any] = {}

        # 上下文压缩器。
        compression_cfg = self.config.get("compression", {}) if isinstance(self.config, dict) else {}
        auxiliary_cfg = self.config.get("auxiliary", {}) if isinstance(self.config, dict) else {}
        auxiliary_compression_cfg = auxiliary_cfg.get("compression", {}) if isinstance(auxiliary_cfg, dict) else {}
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
                compression_protect_last_n = int(compression_cfg.get("protect_last_n", 20))
            except (TypeError, ValueError):
                compression_protect_last_n = 20
            try:
                compression_summary_timeout = int(auxiliary_compression_cfg.get("timeout", 180))
            except (TypeError, ValueError):
                compression_summary_timeout = 180
            compression_summary_timeout = max(30, min(compression_summary_timeout, 600))

            summary_model = (
                compression_cfg.get("summary_model")
                or auxiliary_compression_cfg.get("model")
                or ""
            )
            summary_provider = (
                compression_cfg.get("summary_provider")
                or auxiliary_compression_cfg.get("provider")
                or ""
            )
            summary_base_url = (
                compression_cfg.get("summary_base_url")
                or auxiliary_compression_cfg.get("base_url")
                or ""
            )
            summary_api_key = ""

            self.context_compressor = ContextCompressor(
                model=self.model,
                threshold_percent=compression_threshold,
                protect_last_n=compression_protect_last_n,
                summary_target_ratio=compression_target_ratio,
                base_url=self.base_url,
                api_key=self.api_key,
                api_mode=self.api_mode,
                provider=self.provider,
                session_id=self.session_id,
                summary_model_override=summary_model,
                summary_provider_override=summary_provider,
                summary_base_url_override=summary_base_url,
                summary_api_key_override=summary_api_key,
                summary_timeout=compression_summary_timeout,
            )
        else:
            self.context_compressor = None

        # 工具定义。
        self.tools: List[dict] = []
        self.valid_tool_names: Set[str] = set()
        self._discover_tools()

        # 初始化记忆子系统；provider 配置决定暴露哪些 target 和工具。
        if not self._skip_memory:
            self._init_memory()
            self._sync_memory_tool_schema()

        # 构建客户端：空 base_url 必须转成 None/NOT_GIVEN。
        # 60s HTTP timeout: long enough for most APIs, short enough that a hung
        # request releases quickly when the user presses Ctrl+C.
        _HTTP_TIMEOUT = 60.0
        if self._is_anthropic:
            self.anthropic_client = anthropic.Anthropic(
                api_key=self.api_key,
                base_url=self.base_url or anthropic.NOT_GIVEN,
                max_retries=0,
                timeout=_HTTP_TIMEOUT,
            )
            self.client = None
        else:
            self.client = openai.OpenAI(
                api_key=self.api_key,
                base_url=self.base_url or openai.NOT_GIVEN,
                max_retries=0,
                timeout=_HTTP_TIMEOUT,
            )
            self.anthropic_client = None

        # 在数据库中创建会话。
        if self._session_db:
            self._session_db.create_session(
                session_id=self.session_id,
                source=self.platform,
                model=self.model,
                system_prompt=self.system_prompt,
                parent_session_id=parent_session_id,
                workspace=self.workspace_path or None,
            )
            if hasattr(self._session_db, "count_user_messages"):
                try:
                    self.session_user_messages = self._session_db.count_user_messages(self.session_id)
                except Exception:
                    self.session_user_messages = 0

    def _discover_tools(self):
        from mclaw.tools.dispatch import get_tool_definitions
        definitions, valid_names = get_tool_definitions(
            enabled_toolsets=self.enabled_toolsets,
            config=self.config,
        )
        self.tools = definitions
        self.valid_tool_names = valid_names

    def _build_checkpoint_manager(self):
        cp_cfg = self.config.get("checkpoints", {}) if isinstance(self.config, dict) else {}
        if isinstance(cp_cfg, bool):
            cp_cfg = {"enabled": cp_cfg}
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
            # 加载配置中的记忆上限。优先使用 CLI 传入的合并运行时配置，
            # 以确保项目级 .mclaw.yaml 生效。
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

        flush_system = build_memory_flush_system_prompt()

        # 从最近 N 轮用户/assistant 交互中构造刷新消息。
        flush_msgs = []
        for msg in msgs[-8:]:
            if msg.get("role") in ("user", "assistant"):
                flush_msgs.append(msg)

        # 保存原始工具状态。
        original_tools = self.tools
        original_valid = self.valid_tool_names

        def _run_flush():
            try:
                # 仅保留 memory 工具。
                memory_schema = self._memory_manager.get_all_tool_schemas()
                self.tools = memory_schema
                self.valid_tool_names = self._memory_manager.get_all_tool_names()

                # 使用刷新提示词执行一次 API 调用。
                if self._is_anthropic:
                    api_model = self._normalize_anthropic_model(self.model)
                    max_output = self._get_anthropic_max_output(api_model)
                    sys_part, conv = self._split_anthropic_messages(flush_msgs)
                    combined_sys = (sys_part + "\n\n" + flush_system) if sys_part else flush_system
                    response = self.anthropic_client.messages.create(
                        model=api_model,
                        max_tokens=max_output,
                        system=combined_sys,
                        messages=conv,
                        tools=self._convert_tools_to_anthropic(),
                    )
                    _, tool_calls, _ = self._parse_anthropic(response)
                else:
                    api_msgs = [{"role": "system", "content": flush_system}] + flush_msgs
                    response = self.client.chat.completions.create(
                        model=self.model,
                        messages=api_msgs,
                        tools=self.tools,
                    )
                    _, tool_calls, _, _ = self._parse_openai(response)

                # 直接解析 tool_calls，直接调用 memory handlers.
                # 不经过 handle_function_calls()，不依赖 LLM 的第二次决策
                if tool_calls:
                    from mclaw.tools.memory_tool import MEMORY_TOOL_NAMES, handle_memory_tool_call

                    for tc in tool_calls:
                        fn = tc.get("function", {})
                        tool_name = fn.get("name")
                        if tool_name not in MEMORY_TOOL_NAMES:
                            continue
                        try:
                            args = json.loads(fn.get("arguments", "{}"))
                        except json.JSONDecodeError:
                            continue

                        # Direct memory call without dispatch.
                        result = handle_memory_tool_call(tool_name, args, store=self._memory_store)
                        try:
                            result_data = json.loads(result)
                            if result_data.get("success"):
                                logger.info(
                                    "Memory flush saved: %s", args.get("target", "memory")
                                )
                        except Exception:
                            pass
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
                logger.debug("flush_memories timed out after %.1fs", timeout)
        else:
            _run_flush()

        # 无论成功或超时都恢复工具状态。
        self.tools = original_tools
        self.valid_tool_names = original_valid

    # ── 后台记忆审查 ───────────────────────────────

    def _spawn_background_review(
        self,
        messages_snapshot: List[Dict],
        review_memory: bool = False,
        review_skills: bool = False,
    ) -> None:
        """启动后台线程，审查当前会话是否需要沉淀记忆或技能。

        主回复返回后，后台线程会创建一个独立的 MClaw 审查实例，
        与父实例共享记忆存储，并独立判断是否需要写入记忆或创建技能。
        审查过程不会污染主会话历史。
        """
        spawn_background_review(
            self,
            messages_snapshot=messages_snapshot,
            review_memory=review_memory,
            review_skills=review_skills,
        )
        return

    # ── 刷新记忆快照 ───────────────────────────────────────────────

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
        if not self._event_callback:
            return
        try:
            self._event_callback(event)
        except Exception:
            logger.warning("agent event callback failed", exc_info=True)

    def _strip_event_visible_content(self, text: str) -> str:
        import re

        text = str(text or "")
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<reasoning>.*?</reasoning>", "", text, flags=re.DOTALL | re.IGNORECASE)
        return text.strip()

    def _assistant_round_visible_content(
        self,
        content: str,
        reasoning_content: str | None = None,
    ) -> tuple[str, str]:
        visible_content = self._strip_event_visible_content(content)
        if visible_content:
            return visible_content, "content"

        reasoning_visible = self._strip_event_visible_content(reasoning_content or "")
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
        calls = tool_calls or []
        return bool(calls) and all(self._is_cleanup_tool_call(tc) for tc in calls)

    @staticmethod
    def _is_cleanup_tool_call(tool_call: dict) -> bool:
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

    def switch_model(self, new_model: str, new_provider: str = "",
                     api_key: str = "", base_url: str = "", api_mode: str = ""):
        """Switch model/provider in-place, rebuilding the API client only when needed."""
        old_api_mode = self.api_mode
        old_key = self.api_key
        old_url = self.base_url

        self.model = new_model
        if new_provider:
            self.provider = new_provider
        if api_key:
            self.api_key = api_key
        if base_url:
            self.base_url = base_url
        if api_mode:
            self.api_mode = api_mode
            self._is_anthropic = api_mode == "anthropic_messages"

        needs_rebuild = (
            api_mode and api_mode != old_api_mode
            or self.api_key != old_key
            or self.base_url != old_url
        )

        if needs_rebuild:
            _HTTP_TIMEOUT = 60.0
            if self._is_anthropic:
                self.anthropic_client = anthropic.Anthropic(
                    api_key=self.api_key,
                    base_url=self.base_url or anthropic.NOT_GIVEN,
                    max_retries=0,
                    timeout=_HTTP_TIMEOUT,
                )
                self.client = None
            else:
                self.client = openai.OpenAI(
                    api_key=self.api_key,
                    base_url=self.base_url or openai.NOT_GIVEN,
                    max_retries=0,
                    timeout=_HTTP_TIMEOUT,
                )
                self.anthropic_client = None
            logger.info(
                "API client rebuilt: model=%s provider=%s base_url=%s",
                self.model, self.provider, self.base_url,
            )

        # 使用新模型名称重建系统提示词并更新对话。
        new_sys = self._build_system_prompt()
        if self.messages and self.messages[0].get("role") == "system":
            self.messages[0]["content"] = new_sys

        if self.context_compressor:
            self.context_compressor.reconfigure_model(
                self.model,
                base_url=self.base_url,
                api_key=self.api_key,
                api_mode=self.api_mode,
                provider=self.provider,
            )

    def _build_system_prompt(self) -> str:
        if self.system_prompt:
            return self.system_prompt
        memory_block = None
        if self._memory_manager:
            try:
                memory_block = self._memory_manager.build_system_prompt() or None
            except Exception as e:
                logger.debug("Memory build_system_prompt failed: %s", e)

        # 根据 valid_tool_names 构建 available_toolsets。
        from mclaw.tools.dispatch import get_toolset_for_tool
        avail_toolsets: set = set()
        for t in self.valid_tool_names:
            ts = get_toolset_for_tool(t)
            if ts:
                avail_toolsets.add(ts)

        return build_system_prompt(
            agent_platform=self.platform,
            model=self.model,
            memory_block=memory_block,
            tool_names=self.valid_tool_names,
            available_toolsets=avail_toolsets,
            available_tool_names=sorted(self.valid_tool_names),
            config=self.config,
        )

    # ── Main conversation loop ──

    def run_conversation(
        self,
        user_message: str,
        conversation_history: List[Dict] = None,
        skip_delegation_analysis: bool = False,
        disable_tools: bool = False,
        extra_system: str = "",
        advance_background_review: bool = True,
    ) -> Dict[str, Any]:
        """Run one conversation turn with full tool-calling loop.

        Returns dict with: final_response, messages, model, session_id, api_calls, assistant_rounds
        """
        self.clear_interrupt()
        self._memory_changed_in_turn = False
        self._skills_changed_in_turn = False

        # 子代理传入 None 时，不继承父代理完整历史。
        messages = list(conversation_history if conversation_history is not None else [])

        # 新会话开始时重置上一轮 token 计数，避免重任务后的残留统计
        # 触发不必要的预防性压缩。
        if self.context_compressor and conversation_history is None:
            self.context_compressor.last_prompt_tokens = 0
            self.context_compressor.last_completion_tokens = 0

        if not messages or messages[0].get("role") != "system":
            system_prompt_text = self._build_system_prompt()
            if extra_system:
                system_prompt_text += "\n\n" + extra_system
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

        # 为当前轮预取相关记忆，仅在 API 调用时注入。
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
        final_response = ""
        final_response_recorded = False
        interrupted = False
        assistant_rounds = []

        # Temporarily disable tools when disable_tools=True (e.g. synthesis turn)
        original_tools = self.tools
        if disable_tools:
            self.tools = []

        while api_call_count < self.max_iterations:
            logger.info("[LOOP] starting iteration %d", api_call_count + 1)
            if self._interrupted:
                interrupted = True
                break

            api_call_count += 1

            # 子代理诊断：记录每轮 API 迭代，方便定位卡住的位置。
            if getattr(self, "_delegate_depth", 0) > 0:
                logger.info(
                    "[subagent-%s] API iteration %d/%d",
                    getattr(self, "session_id", "?")[-6:], api_call_count, self.max_iterations
                )

            # 下一次 API 调用前清理本轮压缩断路标记。
            if self.context_compressor:
                self.context_compressor._compressed_this_turn = False

            # ── 预防性压缩：估算上下文过大时，在发送 API 前先压缩。 ──
            # last_prompt_tokens 只反映上一轮 API 调用；历史消息会持续增长，
            # 因此这里必须估算当前 messages。
            if self.context_compressor:
                cc = self.context_compressor
                if not cc._compressed_this_turn:
                    from mclaw.agent.context_compressor import estimate_messages_tokens
                    logger.info("[LOOP] estimating tokens for preventive compression")
                    estimated = estimate_messages_tokens(messages)
                    logger.info("[LOOP] estimated=%d last_prompt=%d last_completion=%d", estimated, cc.last_prompt_tokens, cc.last_completion_tokens)
                    # 检查时使用估算值和上次真实用量中的较大值。
                    real_tokens = cc.last_prompt_tokens + cc.last_completion_tokens
                    check_tokens = max(estimated, real_tokens)
                    if check_tokens >= cc.threshold_tokens:
                        logger.info("[LOOP] preventive compression triggered (check=%d >= threshold=%d)", check_tokens, cc.threshold_tokens)
                        self._emit_status("Compressing context...")
                        self.flush_memories()
                        messages = cc.compress(messages)
                        logger.info("[LOOP] compression done")
                        self._refresh_memory_snapshot()
                        logger.info("[LOOP] memory snapshot refreshed")
                        new_sys = self._build_system_prompt()
                        if messages and messages[0].get("role") == "system":
                            messages[0]["content"] = new_sys
                        cc._compressed_this_turn = True
                    else:
                        logger.info("[LOOP] no preventive compression needed (check=%d < threshold=%d)", check_tokens, cc.threshold_tokens)

            # ── API call with retry ──
            response = None
            retry_count = 0

            while retry_count < MAX_RETRIES:
                if self._interrupted:
                    interrupted = True
                    break
                try:
                    # API 调用前记录完整消息列表，便于排查上下文增长问题。
                    try:
                        import json as _json
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
                        logger.info("[MESSAGES BEFORE API CALL] total_messages=%d total_chars=%d details=%s", len(messages), _total_chars, _json.dumps(_redact_log_secrets(_msgs_log), ensure_ascii=False, default=str))
                    except Exception:
                        pass

                    if getattr(self, "_delegate_depth", 0) > 0:
                        logger.info("[subagent-%s] 开始 API 调用", getattr(self, "session_id", "?")[-6:])
                    if self._is_anthropic:
                        response = self._call_anthropic(messages)
                    else:
                        response = self._call_openai(messages)
                    if getattr(self, "_delegate_depth", 0) > 0:
                        logger.info("[subagent-%s] API 调用完成", getattr(self, "session_id", "?")[-6:])
                    break
                except Exception as e:
                    err_str = str(e).lower()
                    # 上下文溢出：先压缩再重试，每轮最多一次。
                    is_context_limit = (
                        ("context" in err_str and "limit" in err_str)
                        or "2013" in str(e)
                        or "too large" in err_str
                        or "entity too large" in err_str
                    )
                    if is_context_limit and self.context_compressor and retry_count == 0:
                        # 压缩后重试一次，不计入普通重试预算。
                        self._emit_status("Context overflow — compressing and retrying...")
                        self.flush_memories()
                        messages = self.context_compressor.compress(messages)
                        self._refresh_memory_snapshot()
                        new_sys = self._build_system_prompt()
                        if messages and messages[0].get("role") == "system":
                            messages[0]["content"] = new_sys
                        self.context_compressor._compressed_this_turn = True
                        continue  # retry with compressed messages
                    if is_retryable_error(e) and retry_count < MAX_RETRIES - 1:
                        retry_count += 1
                        retry_after = get_retry_after(e)
                        wait = retry_after or jittered_backoff(retry_count)
                        logger.warning(
                            "API error (attempt %d/%d), retrying in %.1fs: %s",
                            retry_count, MAX_RETRIES, wait, e,
                        )
                        self._emit_status(f"重试中，等待 {wait:.0f}s...")
                        deadline = time.time() + wait
                        while time.time() < deadline:
                            if self._interrupted:
                                break
                            time.sleep(0.2)
                        continue
                    logger.error("API error (non-retryable): %s", e)
                    final_response = f"API Error: {e}"
                    self.messages = messages
                    return {
                        "final_response": final_response,
                        "messages": messages,
                        "model": self.model,
                        "session_id": self.session_id,
                        "api_calls": api_call_count,
                        "assistant_rounds": assistant_rounds,
                    }

            if self._interrupted:
                interrupted = True

            if interrupted or response is None:
                break

            # ── Track token usage ──
            self._track_usage(response)
            self.session_api_calls += 1

            # 将真实 token 用量反馈给压缩器，使后续决策基于 API 实测值，
            # 而不是可能不准确的估算值。
            usage = getattr(response, "usage", None)
            if usage and self.context_compressor:
                self.context_compressor.update_from_response({
                    "prompt_tokens": getattr(usage, "prompt_tokens", 0) or getattr(usage, "input_tokens", 0),
                    "completion_tokens": getattr(usage, "completion_tokens", 0),
                })

            # ── Parse response ──
            if self._is_anthropic:
                assistant_content, tool_calls, finish = self._parse_anthropic(response)
                reasoning_content = None
            else:
                assistant_content, tool_calls, finish, reasoning_content = self._parse_openai(response)

            finish_after_cleanup = (
                bool(assistant_content and assistant_content.strip())
                and self._is_cleanup_only_tool_batch(tool_calls)
            )
            round_event = self._build_assistant_round_event(
                api_call_index=api_call_count,
                content=assistant_content,
                reasoning_content=reasoning_content,
                tool_calls=tool_calls,
                finish_reason=finish,
                was_streamed=bool(self._stream_callback),
                is_final_override=True if finish_after_cleanup else None,
            )
            assistant_rounds.append(round_event)
            self._emit_event(round_event)

            # ── Tool calls present → dispatch and continue ──
            if tool_calls:
                # 写入会话数据库；这与 API messages 列表相互独立。
                if self._session_db:
                    self._session_db.append_message(
                        self.session_id, "assistant",
                        content=assistant_content,
                        tool_calls=[tc for tc in tool_calls],
                        turn_id=self._checkpoint_turn_id,
                    )

                # _execute_tool_calls appends the assistant message internally,
                # 因此不要在这里再次追加，避免 messages 列表重复。
                # delegate_task 进入非阻塞模式时会返回 pending_delegate=True。
                # was started in non-blocking mode — check it to avoid unnecessary work.
                pending_result = self._execute_tool_calls(
                    tool_calls,
                    messages,
                    assistant_content=assistant_content,
                    reasoning_content=reasoning_content,
                )
                logger.info("[POST-TOOL] _execute_tool_calls returned, pending=%s", pending_result is not None)
                if pending_result is not None:
                    # delegate_task 已以非阻塞模式启动，立即返回给 TUI 轮询。
                    # 提前返回前恢复工具，避免工具被永久禁用。
                    if disable_tools:
                        self.tools = original_tools
                    pending_result["assistant_rounds"] = assistant_rounds
                    return pending_result

                if finish_after_cleanup:
                    logger.info(
                        "[POST-TOOL] cleanup-only tool call after assistant content; "
                        "finishing without another API call"
                    )
                    final_response = assistant_content or ""
                    final_response_recorded = True
                    break

                # 每轮工具调用后裁剪旧工具结果，避免 read_file/terminal 等大输出
                # 在消息历史中无限累积。
                if self.context_compressor:
                    logger.info("[POST-TOOL] pruning context before next iteration")
                    messages, _pruned = self.context_compressor.prune(messages)
                    logger.info("[POST-TOOL] pruning done, pruned=%s", _pruned)

                logger.info("[POST-TOOL] continuing to next API iteration")
                continue

            # ── 无工具调用，进入最终回复 ──
            final_response = assistant_content or ""
            if self._stream_callback and not final_response:
                final_response = ""

            assistant_msg = {"role": "assistant", "content": final_response}
            if reasoning_content:
                assistant_msg["reasoning_content"] = reasoning_content
            messages.append(assistant_msg)
            if getattr(self, "_delegate_depth", 0) > 0:
                logger.info("[subagent-%s] no tool_calls, breaking loop", self.session_id[-6:])
            break

        if getattr(self, "_delegate_depth", 0) > 0:
            logger.info("[subagent-%s] exited loop, api_calls=%d", self.session_id[-6:], api_call_count)

        if self._session_db and final_response and not final_response_recorded:
            self._session_db.append_message(
                self.session_id,
                "assistant",
                content=final_response,
                turn_id=self._checkpoint_turn_id,
            )

        self.messages = messages

        _should_review_skills = False
        completed_user_turn = bool(advance_background_review and not interrupted and final_response)
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

        # 后台记忆/技能审查在当前回复完成后触发，不与用户任务竞争。
        # 子代理是临时实例，不能再启动后台线程，避免与父代理竞争或形成嵌套代理。
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
            except Exception:
                pass  # background review is best-effort

        # 如果工具被禁用过，则恢复工具。
        if disable_tools:
            self.tools = original_tools

        return {
            "final_response": final_response,
            "messages": messages,
            "model": self.model,
            "session_id": self.session_id,
            "api_calls": api_call_count,
            "interrupted": interrupted,
            "completed": bool(not interrupted and final_response),
            "assistant_rounds": assistant_rounds,
            "skills_changed": bool(getattr(self, "_skills_changed_in_turn", False)),
        }

    # ── OpenAI API calls ──

    def _call_openai(self, messages: List[Dict]):
        logger.info("[_call_openai] start model=%s stream=%s", self.model, bool(self._stream_callback))
        api_messages = self._prepare_openai_messages(messages)
        kwargs = {"model": self.model, "messages": api_messages, "timeout": 30}
        if self.tools:
            kwargs["tools"] = self.tools

        # MiniMax 扩展思考字段：将 reasoning 与用户可见内容分离。
        if self.provider in ("minimax", "minimax-cn"):
            kwargs["extra_body"] = {"reasoning_split": True}

        if self._stream_callback:
            logger.info("[_call_openai] entering streaming path")
            result = self._openai_streaming(kwargs)
            logger.info("[_call_openai] streaming returned")
            return result

        # 非流式路径：通过 watchdog 线程实现硬超时。
        # httpx/openai timeout 在半开 TCP、负载均衡 keep-alive 等边界情况可能失效。
        logger.info("[_call_openai] entering non-streaming path")
        result_container: list = [None]
        def _run():
            try:
                result_container[0] = self.client.chat.completions.create(**kwargs)
            except Exception as exc:
                result_container[0] = exc

        import threading
        t = threading.Thread(target=_run, daemon=True)
        t.start()
        logger.info("[_call_openai] watchdog thread started, waiting max 90s")
        deadline = time.monotonic() + 90
        while t.is_alive():
            if self._interrupted:
                logger.info("[_call_openai] interrupted while waiting for non-streaming response")
                return None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            t.join(timeout=min(0.2, remaining))
        logger.info("[_call_openai] watchdog join returned, result_type=%s", type(result_container[0]).__name__ if result_container[0] is not None else "None")
        if self._interrupted and result_container[0] is None:
            return None
        if isinstance(result_container[0], Exception):
            raise result_container[0]
        if result_container[0] is None:
            raise openai.APITimeoutError(request=None)
        return result_container[0]

    def _prepare_openai_messages(self, messages: List[Dict]) -> List[Dict]:
        """Clean messages for the OpenAI API.

        Entries are new dicts (from dict comprehension), so mutating them
        here does NOT alter the canonical ``messages`` list.
        """
        api_msgs = []
        for msg in messages:
            clean = {k: v for k, v in msg.items() if not k.startswith("_")}
            clean.pop("finish_reason", None)
            api_msgs.append(clean)
        # 将召回记忆注入 system message，仅 API 调用时生效。
        if self._recalled_memory and api_msgs and api_msgs[0].get("role") == "system":
            base = api_msgs[0].get("content", "")
            api_msgs[0]["content"] = f"{base}\n\n{self._recalled_memory}" if base else self._recalled_memory
        return api_msgs

    def _openai_streaming(self, kwargs) -> Any:
        """Stream with callbacks, return assembled response.

        Uses a background producer thread so the main loop can enforce a
        per-chunk stall timeout.  SSE connections can hang indefinitely when
        the server stops sending data but keeps the TCP socket open; the
        standard ``for chunk in stream`` would block forever.  By pulling
        chunks through a Queue we can bail out after N seconds of inactivity.
        """
        import queue as _queue
        import threading as _threading

        kwargs["stream"] = True
        _create_start = time.monotonic()
        logger.info("[_openai_streaming] starting create() watchdog")

        # create() watchdog，与 _call_openai 非流式路径使用相同保护原因。
        stream = None
        create_exc: list = [None]
        def _run_create():
            nonlocal stream
            for attempt in range(MAX_RETRIES):
                try:
                    kwargs["stream_options"] = {"include_usage": True}
                    stream = self.client.chat.completions.create(**kwargs)
                    return
                except openai.APITimeoutError as exc:
                    # 超时不应重试：服务端未在限定时间内响应。
                    create_exc[0] = exc
                    return
                except openai.RateLimitError as exc:
                    if attempt < MAX_RETRIES - 1:
                        retry_after = get_retry_after(exc)
                        wait = retry_after or jittered_backoff(attempt)
                        logger.warning(
                            "Rate limit (attempt %d/%d), retrying in %.1fs: %s",
                            attempt + 1, MAX_RETRIES, wait, exc,
                        )
                        time.sleep(wait)
                        continue
                    create_exc[0] = exc
                    return
                except (openai.BadRequestError, openai.APIError):
                    # 降级方案：部分供应商不支持 stream_options。
                    try:
                        kwargs.pop("stream_options", None)
                        stream = self.client.chat.completions.create(**kwargs)
                        return
                    except openai.APITimeoutError as exc:
                        create_exc[0] = exc
                        return
                    except Exception as exc:
                        if is_retryable_error(exc) and attempt < MAX_RETRIES - 1:
                            retry_after = get_retry_after(exc)
                            wait = retry_after or jittered_backoff(attempt)
                            logger.warning(
                                "API fallback retry (attempt %d/%d), waiting %.1fs: %s",
                                attempt + 1, MAX_RETRIES, wait, exc,
                            )
                            time.sleep(wait)
                            continue
                        create_exc[0] = exc
                        return
                except Exception as exc:
                    if is_retryable_error(exc) and attempt < MAX_RETRIES - 1:
                        retry_after = get_retry_after(exc)
                        wait = retry_after or jittered_backoff(attempt)
                        logger.warning(
                            "API create retry (attempt %d/%d), waiting %.1fs: %s",
                            attempt + 1, MAX_RETRIES, wait, exc,
                        )
                        time.sleep(wait)
                        continue
                    create_exc[0] = exc
                    return

        _create_thread = _threading.Thread(target=_run_create, daemon=True)
        _create_thread.start()
        CREATE_TIMEOUT = 90
        POLL_INTERVAL = 0.2
        _create_deadline = time.monotonic() + CREATE_TIMEOUT
        while _create_thread.is_alive():
            if self._interrupted:
                logger.info("[_openai_streaming] interrupted during create() watchdog")
                return None
            _remaining = _create_deadline - time.monotonic()
            if _remaining <= 0:
                break
            _create_thread.join(timeout=min(POLL_INTERVAL, _remaining))
        _create_elapsed = time.monotonic() - _create_start
        if create_exc[0] is not None:
            logger.error("[_openai_streaming] create() raised exception after %.1fs: %s", _create_elapsed, create_exc[0])
            raise create_exc[0]
        if stream is None:
            logger.error("[_openai_streaming] create() timed out after %.1fs", _create_elapsed)
            raise openai.APITimeoutError(request=None)
        logger.info("[_openai_streaming] create() done in %.2fs", _create_elapsed)
        getattr(self, "_emit_status", lambda msg: None)("Waiting for response...")

        chunk_q: "_queue.Queue[Any | None]" = _queue.Queue()
        producer_exc: list = [None]
        producer_done = _threading.Event()

        def _producer():
            try:
                for chunk in stream:
                    if self._interrupted:
                        break
                    chunk_q.put(chunk)
            except Exception as exc:
                producer_exc[0] = exc
            finally:
                producer_done.set()

        _threading.Thread(target=_producer, daemon=True).start()
        logger.info("[_openai_streaming] producer thread started")

        content_chunks = []
        reasoning_chunks = []
        tool_calls_map: Dict[int, Dict] = {}
        usage = None
        finish_reason = None
        stream_start = time.monotonic()
        SAFETY_TIMEOUT = 300       # 5 min hard ceiling for entire stream
        STALL_TIMEOUT = 60         # 60 s without meaningful content -> bail
        GRACE_AFTER_FINISH = 3     # fast exit once finish_reason seen + queue drained
        chunk_count = 0
        _first_chunk_at: float | None = None
        _last_meaningful_at = stream_start
        _finish_reason_at: float | None = None

        logger.info("[_openai_streaming] entering consumer loop")
        while not producer_done.is_set() or not chunk_q.empty():
            if self._interrupted:
                logger.info("[_openai_streaming] interrupted; closing stream")
                break

            # 全局流式生命周期保护。
            now = time.monotonic()
            elapsed = now - stream_start
            if elapsed > SAFETY_TIMEOUT:
                logger.warning("[STREAM TIMEOUT] Breaking SSE stream after %ds", SAFETY_TIMEOUT)
                break

            if finish_reason and chunk_q.empty() and _finish_reason_at is not None:
                if now - _finish_reason_at >= GRACE_AFTER_FINISH:
                    logger.info("[STREAM] Fast exit after finish_reason (grace=%ds)", GRACE_AFTER_FINISH)
                    break

            if now - _last_meaningful_at > STALL_TIMEOUT:
                logger.warning("[STREAM STALL] No meaningful content for %ds, breaking", STALL_TIMEOUT)
                break

            # Poll in short intervals so Ctrl+C is observed quickly; do not block
            # here for the full stall timeout.
            try:
                chunk = chunk_q.get(timeout=POLL_INTERVAL)
            except _queue.Empty:
                continue

            # 生产者已结束并发送哨兵值。
            if chunk is None:
                break

            chunk_count += 1
            if _first_chunk_at is None:
                _first_chunk_at = time.monotonic() - stream_start
                logger.info("[STREAM] first chunk after %.2fs", _first_chunk_at)

            if self._interrupted:
                break
            if not chunk.choices and hasattr(chunk, "usage") and chunk.usage:
                usage = chunk.usage
                continue
            if not chunk.choices:
                # SSE keep-alive empty chunk — check meaningful-content stall
                if time.monotonic() - _last_meaningful_at > STALL_TIMEOUT:
                    logger.warning("[STREAM STALL] No meaningful content for %ds, breaking", STALL_TIMEOUT)
                    break
                continue

            delta = chunk.choices[0].delta
            if chunk.choices[0].finish_reason:
                finish_reason = chunk.choices[0].finish_reason
                if _finish_reason_at is None:
                    _finish_reason_at = time.monotonic()

            # 记录当前 chunk 是否携带有效 payload。
            _had_meaningful = False

            if delta and delta.content:
                _had_meaningful = True
                content_chunks.append(delta.content)
                if self._stream_callback:
                    self._stream_callback(delta.content)

            # MiniMax reasoning: collect but don't stream to UI (already separated by reasoning_split=True)
            if delta and getattr(delta, "reasoning_content", None):
                _had_meaningful = True
                reasoning_chunks.append(delta.reasoning_content)
            elif delta and hasattr(delta, "reasoning_details") and delta.reasoning_details:
                _had_meaningful = True
                # 部分供应商会以 detail 对象列表发送 reasoning。
                for detail in delta.reasoning_details:
                    if hasattr(detail, "text") and detail.text:
                        reasoning_chunks.append(detail.text)
                    elif isinstance(detail, str):
                        reasoning_chunks.append(detail)

            if delta and delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    idx = getattr(tc_delta, "index", None)
                    if idx is None:
                        continue
                    if idx not in tool_calls_map:
                        tool_calls_map[idx] = {
                            "id": tc_delta.id or "",
                            "type": "function",
                            "function": {"name": "", "arguments": ""},
                        }
                    entry = tool_calls_map[idx]
                    if tc_delta.id:
                        entry["id"] = tc_delta.id
                    if tc_delta.function:
                        if tc_delta.function.name:
                            _had_meaningful = True
                            entry["function"]["name"] += tc_delta.function.name
                        if tc_delta.function.arguments:
                            _had_meaningful = True
                            entry["function"]["arguments"] += tc_delta.function.arguments

            if _had_meaningful:
                _last_meaningful_at = time.monotonic()
            else:
                # Empty choices.delta or no payload — still subject to stall timeout
                if time.monotonic() - _last_meaningful_at > STALL_TIMEOUT:
                    logger.warning("[STREAM STALL] No meaningful content for %ds, breaking", STALL_TIMEOUT)
                    break

        logger.info("[_openai_streaming] consumer loop exited")

        # 关闭底层 HTTP 响应以释放连接。
        # 生产者线程可能仍阻塞在 ``for chunk in stream``；
        # 关闭 stream 可解除阻塞，让线程退出。
        logger.info("[_openai_streaming] closing stream")
        try:
            stream.close()
        except Exception:
            pass

        _total_stream_time = time.monotonic() - stream_start
        logger.info(
            "[STREAM] finished: total=%.2fs chunks=%d first_chunk=%s finish_reason=%s content_len=%d",
            _total_stream_time,
            chunk_count,
            f"{_first_chunk_at:.2f}s" if _first_chunk_at else "N/A",
            finish_reason or "none",
            len("".join(content_chunks)),
        )

        # 如果流在收到任何 payload 前停滞，则按超时处理，
        # 让调用方明确知道服务端未正常响应。
        if (
            not self._interrupted
            and not content_chunks
            and not reasoning_chunks
            and not tool_calls_map
            and not finish_reason
        ):
            logger.error("[STREAM] Stall timeout with zero content — treating as timeout")
            raise openai.APITimeoutError(request=None)

        # 构造统一 response 对象。
        class _Msg:
            pass
        msg = _Msg()
        msg.content = "".join(content_chunks) or None
        msg.reasoning_content = "".join(reasoning_chunks) or None
        msg.tool_calls = None
        if tool_calls_map:
            tcs = []
            for idx in sorted(tool_calls_map):
                tc_data = tool_calls_map[idx]
                tc = _Msg()
                tc.id = tc_data["id"]
                tc.type = "function"
                fn = _Msg()
                fn.name = tc_data["function"]["name"]
                fn.arguments = tc_data["function"]["arguments"]
                tc.function = fn
                tcs.append(tc)
            msg.tool_calls = tcs

        class _Choice:
            pass
        choice = _Choice()
        choice.message = msg
        choice.finish_reason = finish_reason

        class _Response:
            pass
        resp = _Response()
        resp.choices = [choice]
        # 如果供应商未在流式响应中返回 usage，则进行本地估算。
        # (skip estimation when interrupted — partial output gives bad estimates)
        if usage is None and not self._interrupted:
            usage = self._estimate_streaming_usage(
                kwargs.get("messages", []), msg
            )
        resp.usage = usage
        logger.info("[_openai_streaming] returning assembled response")
        return resp

    def _parse_openai(self, response) -> tuple:
        msg = response.choices[0].message
        content = msg.content or ""
        reasoning = getattr(msg, "reasoning_content", None) or getattr(msg, "reasoning", None)
        tool_calls = None
        if msg.tool_calls:
            tool_calls = []
            for tc in msg.tool_calls:
                tool_calls.append({
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                })
        finish = getattr(response.choices[0], "finish_reason", None)
        return content, tool_calls, finish, reasoning

    # ── Anthropic API calls ──

    @staticmethod
    def _normalize_anthropic_model(model: str) -> str:
        """Normalize model name for the Anthropic API.

        - Strip 'anthropic/' prefix (OpenRouter format)
        - Convert dots to hyphens (claude-sonnet-4.6 → claude-sonnet-4-6)
        """
        if model.lower().startswith("anthropic/"):
            model = model[len("anthropic/"):]
        model = model.replace(".", "-")
        return model

    @staticmethod
    def _get_anthropic_max_output(model: str) -> int:
        """Look up max output tokens for an Anthropic model."""
        limits = {
            "claude-opus-4-6": 128_000,
            "claude-sonnet-4-6": 64_000,
            "claude-opus-4-5": 32_000,
            "claude-sonnet-4-5": 16_384,
            "claude-sonnet-4-0": 16_384,
            "claude-sonnet-4": 16_384,
            "claude-3-5-sonnet": 8_192,
            "claude-3-5-haiku": 8_192,
            "claude-3-opus": 4_096,
            "claude-3-haiku": 4_096,
        }
        m = model.lower()
        best_key, best_val = "", 8_192
        for key, val in limits.items():
            if key in m and len(key) > len(best_key):
                best_key, best_val = key, val
        return best_val

    def _call_anthropic(self, messages: List[Dict]):
        system, conv = self._split_anthropic_messages(messages)
        api_model = self._normalize_anthropic_model(self.model)
        max_output = self._get_anthropic_max_output(api_model)
        kwargs = {"model": api_model, "max_tokens": max_output, "messages": conv}
        if system:
            kwargs["system"] = system
        if self.tools:
            kwargs["tools"] = self._convert_tools_to_anthropic()

        if self._stream_callback:
            return self._anthropic_streaming(kwargs)
        return self.anthropic_client.messages.create(**kwargs)

    def _convert_tools_to_anthropic(self) -> List[dict]:
        """Convert OpenAI-format tool definitions to Anthropic format."""
        result = []
        for tool_def in self.tools:
            fn = tool_def.get("function", {})
            result.append({
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
            })
        return result

    def _split_anthropic_messages(self, messages: List[Dict]):
        system = ""
        conv = []
        for msg in messages:
            if msg["role"] == "system":
                system = msg.get("content", "")
            elif msg["role"] == "tool":
                conv.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": msg.get("tool_call_id", ""),
                        "content": msg.get("content", ""),
                    }],
                })
            elif msg["role"] == "assistant" and msg.get("tool_calls"):
                content_blocks = []
                if msg.get("content"):
                    content_blocks.append({"type": "text", "text": msg["content"]})
                for tc in msg["tool_calls"]:
                    fn = tc.get("function", {})
                    try:
                        input_data = json.loads(fn.get("arguments", "{}"))
                    except json.JSONDecodeError:
                        input_data = {}
                    content_blocks.append({
                        "type": "tool_use",
                        "id": tc.get("id", ""),
                        "name": fn.get("name", ""),
                        "input": input_data,
                    })
                conv.append({"role": "assistant", "content": content_blocks})
            else:
                conv.append({"role": msg["role"], "content": msg.get("content", "")})
        # 将召回记忆注入 system，仅 API 调用时生效。
        if self._recalled_memory:
            system = f"{system}\n\n{self._recalled_memory}" if system else self._recalled_memory
        return system, conv

    def _anthropic_streaming(self, kwargs):
        """Stream Anthropic-compatible responses without blocking interrupt.

        Anthropic SDK stream iteration and ``get_final_message()`` can block
        inside the HTTP read when a provider keeps an SSE connection half-open.
        Run those calls in a daemon producer so the conversation loop can honor
        user interrupts and recover from stalls.
        """
        import queue as _queue
        import threading as _threading

        event_q: "_queue.Queue[tuple[str, Any]]" = _queue.Queue()
        producer_done = _threading.Event()
        stream_holder: list[Any] = [None]
        content_chunks: list[str] = []

        def _producer():
            try:
                with self.anthropic_client.messages.stream(**kwargs) as stream:
                    stream_holder[0] = stream
                    event_q.put(("started", None))
                    for text in stream.text_stream:
                        if self._interrupted:
                            break
                        event_q.put(("text", text))
                    if not self._interrupted:
                        event_q.put(("final", stream.get_final_message()))
            except Exception as exc:
                event_q.put(("error", exc))
            finally:
                producer_done.set()

        getattr(self, "_emit_status", lambda msg: None)("Waiting for response...")
        _threading.Thread(target=_producer, daemon=True).start()
        logger.info("[_anthropic_streaming] producer thread started")

        stream_start = time.monotonic()
        last_event_at = stream_start
        response = None
        producer_exc = None
        timed_out = False
        SAFETY_TIMEOUT = 300
        STALL_TIMEOUT = 60
        POLL_INTERVAL = 0.2

        while not producer_done.is_set() or not event_q.empty():
            if self._interrupted:
                logger.info("[_anthropic_streaming] interrupted; closing stream")
                break

            now = time.monotonic()
            if now - stream_start > SAFETY_TIMEOUT:
                logger.warning("[ANTHROPIC STREAM TIMEOUT] Breaking stream after %ds", SAFETY_TIMEOUT)
                timed_out = True
                break
            if now - last_event_at > STALL_TIMEOUT:
                logger.warning("[ANTHROPIC STREAM STALL] No event for %ds, breaking", STALL_TIMEOUT)
                timed_out = True
                break

            try:
                event_type, payload = event_q.get(timeout=POLL_INTERVAL)
            except _queue.Empty:
                continue

            last_event_at = time.monotonic()
            if event_type == "started":
                continue
            if event_type == "text":
                content_chunks.append(payload)
                if self._stream_callback:
                    self._stream_callback(payload)
                continue
            if event_type == "final":
                response = payload
                break
            if event_type == "error":
                producer_exc = payload
                break

        stream = stream_holder[0]
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass

        if response is not None:
            return response
        if producer_exc is not None and not self._interrupted:
            raise producer_exc
        if timed_out and not content_chunks:
            raise TimeoutError("Anthropic stream stalled before returning content")

        class _Block:
            type = "text"

            def __init__(self, text: str):
                self.text = text

        class _Response:
            pass

        partial = _Response()
        partial.content = [_Block("".join(content_chunks))]
        partial.stop_reason = "interrupted" if self._interrupted else "stream_stalled"
        partial.usage = None
        logger.info("[_anthropic_streaming] returning partial response stop_reason=%s", partial.stop_reason)
        return partial

    def _parse_anthropic(self, response) -> tuple:
        content_parts = []
        tool_calls = []
        for block in response.content:
            if block.type == "text":
                content_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append({
                    "id": block.id,
                    "type": "function",
                    "function": {
                        "name": block.name,
                        "arguments": json.dumps(block.input),
                    },
                })
        content = "".join(content_parts)
        finish = getattr(response, "stop_reason", None)
        return content, tool_calls if tool_calls else None, finish

    # ── Tool execution ──

    def _build_assistant_msg(self, content: str, tool_calls: List[Dict], reasoning_content: str = None) -> Dict:
        msg = {"role": "assistant", "tool_calls": tool_calls}
        if content:
            msg["content"] = content
        if reasoning_content:
            msg["reasoning_content"] = reasoning_content
        return msg

    def _execute_tool_calls(
        self,
        tool_calls: List[Dict],
        messages: List[Dict],
        assistant_content: str = "",
        reasoning_content: str = None,
    ):
        """Dispatch tool calls (batch, serial or concurrent) and append results to messages."""
        from mclaw.tools.dispatch import handle_function_calls, set_tool_context

        if not tool_calls:
            return

        is_subagent = getattr(self, "_delegate_depth", 0) > 0
        _sid_tail = self.session_id[-6:] if self.session_id else "?"

        # 设置本次调用上下文，让 session_search 等工具可访问 SessionDB。
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
            reasoning_content=reasoning_content,
        )
        messages.append(assistant_msg)

        checkpoint_mgr = self._get_checkpoint_manager()
        checkpoint_mgr.new_turn()

        # 批量调度：写工具走带 CheckpointManager 的串行路径。
        # 只读批处理会在 handle_function_calls 内自动选择并发路径。
        if self._tool_callback:
            for tc in tool_calls:
                fn = tc.get("function", {})
                try:
                    args = json.loads(fn.get("arguments", "{}"))
                except (json.JSONDecodeError, TypeError):
                    args = {}
                self._tool_callback(fn.get("name", ""), args)

        # 过滤被禁用的工具。
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

        # 诊断日志：子代理调度前记录每个工具名称，避免污染父代理日志。
        if is_subagent:
            logger.info("[subagent-%s] dispatching tools: %s", _sid_tail, tool_names)

        results = handle_function_calls(
            calls=tool_calls,
            tool_names=set(self.valid_tool_names),
            available_toolsets=None,
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

        if getattr(self, "_memory_review_round", 0) > 0:
            try:
                from mclaw.tools.memory_tool import MEMORY_WRITE_TOOL_NAMES
            except Exception:
                MEMORY_WRITE_TOOL_NAMES = set()
            for tc, result in zip(tool_calls, results):
                fn = tc.get("function", {})
                if fn.get("name") not in MEMORY_WRITE_TOOL_NAMES:
                    continue
                try:
                    result_data = json.loads(result)
                    if result_data.get("success"):
                        self._memory_changed_in_turn = True
                        self._turns_since_memory_review = 0
                        break
                except Exception:
                    pass

        # 只有 skill_manage 真正成功后才重置技能审查计数。
        # 计数可能在执行前已预重置，这里按执行结果做最终校正。
        if getattr(self, "_evolution_review_round", 0) > 0 and "skill_manage" in self.valid_tool_names:
            for tc, result in zip(tool_calls, results):
                fn = tc.get("function", {})
                if fn.get("name") != "skill_manage":
                    continue
                try:
                    result_data = json.loads(result)
                    action = str(result_data.get("action") or "").strip()
                    if result_data.get("success") and action in {
                        "create",
                        "edit",
                        "patch",
                        "delete",
                        "write_file",
                        "remove_file",
                        "enable_drafting",
                        "evolution_update",
                    }:
                        self._turns_since_evolution_review = 0
                        break  # only one skill_manage per batch
                except Exception:
                    pass

        pending_delegate_data = None
        pending_skill_import_confirmation = None
        skill_write_actions = {
            "create",
            "edit",
            "patch",
            "delete",
            "write_file",
            "remove_file",
            "enable_drafting",
            "evolution_update",
        }

        # 按调用顺序追加工具结果。
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
                    if result_data.get("success") and action in skill_write_actions:
                        self._skills_changed_in_turn = True
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

        # 工具执行后更新 prompt token 估算，
        # 让压缩器和状态栏反映当前真实消息规模。
        if self.context_compressor:
            from mclaw.agent.context_compressor import estimate_messages_tokens
            estimated = estimate_messages_tokens(messages)
            self.context_compressor.last_prompt_tokens = estimated
            logger.info("[TOKEN ESTIMATE POST-TOOLS] estimated=%d", estimated)

        # 检查是否有子代理正在后台运行（pending 模式）。
        # 先写入本轮父代理的 assistant tool call 和所有 tool results，再交给 TUI 轮询；
        # 这里不会注入子代理内部运行历史，只保存 delegate_task 返回的 pending 元数据。
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

    @staticmethod
    def _estimate_streaming_usage(messages: List[Dict], response_msg) -> Any:
        """Estimate token usage when the streaming API doesn't return usage data.

        Returns a usage-like object with prompt_tokens, completion_tokens,
        total_tokens, and _estimated=True.
        """
        from mclaw.agent.context_compressor import estimate_messages_tokens, estimate_tokens_rough

        class _EstimatedUsage:
            pass

        prompt_t = max(1, estimate_messages_tokens(messages))

        # 完成态：响应中包含 content 和 tool calls。
        comp_text = ""
        if response_msg is not None:
            content = getattr(response_msg, "content", None) or ""
            comp_text += str(content)
            tool_calls = getattr(response_msg, "tool_calls", None) or []
            for tc in tool_calls:
                fn = getattr(tc, "function", None)
                if fn:
                    comp_text += getattr(fn, "name", "") or ""
                    comp_text += getattr(fn, "arguments", "") or ""

        comp_t = max(1, estimate_tokens_rough(comp_text))

        usage = _EstimatedUsage()
        usage.prompt_tokens = prompt_t
        usage.completion_tokens = comp_t
        usage.total_tokens = prompt_t + comp_t
        usage._estimated = True
        return usage

    @staticmethod
    def _obj_to_dict(obj) -> Any:
        """Serialize a response object (SDK or bare) to a plain dict/list."""
        if isinstance(obj, list):
            return [MClaw._obj_to_dict(v) for v in obj]
        if isinstance(obj, dict):
            return {k: MClaw._obj_to_dict(v) for k, v in obj.items()}
        if hasattr(obj, "model_dump"):
            return obj.model_dump()
        if hasattr(obj, "to_dict"):
            return obj.to_dict()
        if hasattr(obj, "dict"):
            return obj.dict()
        if hasattr(obj, "__dict__"):
            return {k: MClaw._obj_to_dict(v) for k, v in vars(obj).items()}
        return obj

    def _track_usage(self, response):
        # 记录完整响应对象，便于检查 usage、model、choices 等字段。
        try:
            resp_dict = self._obj_to_dict(response)
            logger.info("[API RESPONSE] %s", json.dumps(_redact_log_secrets(resp_dict), ensure_ascii=False, default=str))
        except Exception as e:
            logger.debug("Failed to serialize response for logging: %s", e)

        usage = getattr(response, "usage", None)
        if not usage:
            return
        input_t = getattr(usage, "prompt_tokens", 0) or getattr(usage, "input_tokens", 0) or 0
        output_t = getattr(usage, "completion_tokens", 0) or getattr(usage, "output_tokens", 0) or 0
        self.session_input_tokens += input_t
        self.session_output_tokens += output_t

        if self._session_db:
            try:
                self._session_db.update_token_counts(
                    self.session_id,
                    input_tokens=input_t,
                    output_tokens=output_t,
                    model=self.model,
                )
            except Exception:
                pass
