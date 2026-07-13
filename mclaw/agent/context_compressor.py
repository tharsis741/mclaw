# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Conversation compaction for long-running M-Claw sessions.

This module owns context-window pressure management for the agent loop. It
prunes stale tool output, protects recent turns by token budget, summarizes
middle turns through the active model provider, and repairs tool-call/result
pairs so compressed histories remain valid API messages.

The compressor keeps a running summary across compactions and uses real API
usage when available. That keeps decisions tied to provider behavior rather
than only to local token estimates.
"""

import logging
import time
from dataclasses import replace
from typing import Any, Callable

from mclaw.agent.transports.base import ModelCallOptions
from mclaw.agent.transports.factory import create_transport
from mclaw.agent.usage import UsageRecord
from mclaw.prompts.compression import build_context_compression_prompt
from mclaw.providers.runtime import ProviderRuntimeContext

logger = logging.getLogger(__name__)

# Prefix attached to generated context summaries.
SUMMARY_PREFIX = (
    "[CONTEXT COMPACTION] Earlier turns in this conversation were compacted "
    "to save context space. The summary below describes work that was "
    "already completed, and the current session state may still reflect "
    "that work. Use the summary and the current state to continue "
    "from where things left off, and avoid repeating work:"
)

# Placeholder used when earlier tool results are pruned.
_PRUNED_TOOL_PLACEHOLDER = "[Earlier tool output cleared to save context space]"

# Summary token budget controls.
_MIN_SUMMARY_TOKENS = 2000
_SUMMARY_RATIO = 0.20
_SUMMARY_TOKENS_CEILING = 12_000
_SUMMARY_FAILURE_COOLDOWN_SECONDS = 600

# Content truncation limits.
_CONTENT_MAX = 6000       # total chars per message body
_CONTENT_HEAD = 4000      # chars kept from the start
_CONTENT_TAIL = 1500      # chars kept from the end
_TOOL_ARGS_MAX = 1500     # max chars kept from tool-call arguments
_TOOL_ARGS_HEAD = 1200    # chars kept from the start of tool-call arguments


def estimate_tokens_rough(text: str) -> int:
    """Estimate tokens cheaply for compression heuristics, not billing."""
    if not text:
        return 0
    # Chinese chars ≈ 1 token each; ASCII ≈ 0.25 tokens each.
    # This is more stable for code-heavy content than len/2 or len/4.
    chinese = sum(1 for c in text if "一" <= c <= "鿿")
    ascii_chars = len(text) - chinese
    return max(1, chinese + int(ascii_chars / 4))


def _content_to_text(content: Any) -> str:
    """Convert provider message content blocks into text for local accounting."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                parts.append(str(block.get("text") or block.get("content") or ""))
        return " ".join(part for part in parts if part)
    return str(content)


def estimate_messages_tokens(messages: list[dict]) -> int:
    """Estimate prompt pressure across message content and tool-call payloads."""
    total = 0
    for msg in messages:
        content = _content_to_text(msg.get("content"))
        total += estimate_tokens_rough(content)
        reasoning = msg.get("reasoning")
        if reasoning is None:
            reasoning = msg.get("reasoning_content")
        total += estimate_tokens_rough(_content_to_text(reasoning))
        if msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                if isinstance(tc, dict):
                    fn = tc.get("function", {})
                    total += estimate_tokens_rough(_content_to_text(fn.get("name")))
                    total += estimate_tokens_rough(_content_to_text(fn.get("arguments")))
    return total


class ContextCompressor:
    """Manages context window pressure by summarizing earlier turns.

    Algorithm:
      1. Prune earlier tool results (cheap, no LLM call)
      2. Protect head messages (system + first exchange)
      3. Find tail boundary by token budget (~20% of context)
      4. Summarize middle turns with structured LLM prompt
      5. Sanitize tool-call/result pairs to avoid broken references
    """

    def __init__(
        self,
        provider_runtime: ProviderRuntimeContext,
        context_window: int,
        threshold_percent: float = 0.50,
        protect_first_n: int = 3,
        protect_last_n: int = 20,
        summary_target_ratio: float = 0.20,
        quiet_mode: bool = False,
        summary_model_override: str | None = None,
        summary_provider_override: str = "",
        summary_base_url_override: str = "",
        summary_api_key_override: str = "",
        summary_timeout: int = 180,
        session_id: str = "",
        config: dict[str, Any] | None = None,
        usage_callback: Callable[[UsageRecord], None] | None = None,
    ):
        if not isinstance(provider_runtime, ProviderRuntimeContext):
            raise TypeError("provider_runtime must be a ProviderRuntimeContext")
        if type(context_window) is not int or context_window <= 0:
            raise ValueError("context_window must be a positive integer")
        self.provider_runtime = provider_runtime
        self.protect_first_n = protect_first_n
        self.protect_last_n = protect_last_n
        self.threshold_percent = threshold_percent
        self.summary_target_ratio = max(0.10, min(summary_target_ratio, 0.80))
        self.quiet_mode = quiet_mode
        self.summary_model = summary_model_override or ""
        self.summary_provider = summary_provider_override or ""
        self.summary_base_url = summary_base_url_override or ""
        self.summary_api_key = summary_api_key_override or ""
        try:
            parsed_summary_timeout = int(summary_timeout or 180)
        except (TypeError, ValueError):
            parsed_summary_timeout = 180
        self.summary_timeout = max(30, min(parsed_summary_timeout, 600))
        self.session_id = session_id
        self.config = config if isinstance(config, dict) else None
        self._usage_callback = usage_callback
        self._summary_runtime: ProviderRuntimeContext | None = None
        self._summary_transport = None

        self._refresh_context_budgets(context_window)
        self.compression_count = 0
        self.last_prompt_tokens = 0
        self.last_completion_tokens = 0
        self.last_total_tokens = 0

        self._summary_failure_cooldown_until: float = 0.0
        self._previous_summary: str | None = None

        # Circuit breaker for a single API iteration. The agent loop clears it
        # before each new provider call.
        self._compressed_this_turn: bool = False

        if not quiet_mode:
            logger.info(
                "Context compressor initialized: model=%s context_length=%d "
                "threshold=%d (%.0f%%) target_ratio=%.0f%% tail_budget=%d "
                "summary_timeout=%ds provider=%s base_url=%s",
                self.provider_runtime.model, self.context_length, self.threshold_tokens,
                threshold_percent * 100, self.summary_target_ratio * 100,
                self.tail_token_budget,
                self.summary_timeout,
                self.provider_runtime.provider or "none",
                self.provider_runtime.base_url or "none",
            )

    def _refresh_context_budgets(self, context_window: int) -> None:
        """Recalculate compression thresholds from the active provider context size."""
        self.context_length = context_window
        self.threshold_tokens = int(self.context_length * self.threshold_percent)
        self.tail_token_budget = int(self.threshold_tokens * self.summary_target_ratio)
        self.max_summary_tokens = min(
            int(self.context_length * 0.05), _SUMMARY_TOKENS_CEILING,
        )

    def reconfigure_model(
        self,
        provider_runtime: ProviderRuntimeContext,
        *,
        context_window: int,
    ) -> None:
        """Apply a resolved runtime switch without performing I/O."""
        previous_model = self.provider_runtime.model
        previous_context_length = self.context_length
        previous_threshold_tokens = self.threshold_tokens

        if not isinstance(provider_runtime, ProviderRuntimeContext):
            raise TypeError("provider_runtime must be a ProviderRuntimeContext")
        if type(context_window) is not int or context_window <= 0:
            raise ValueError("context_window must be a positive integer")
        self.provider_runtime = provider_runtime
        self._refresh_context_budgets(context_window)
        self._summary_runtime = None
        self._summary_transport = None

        logger.info(
            "Context compressor reconfigured: model=%s context_length=%d "
            "threshold=%d provider=%s base_url=%s (previous model=%s "
            "context_length=%d threshold=%d)",
            self.provider_runtime.model,
            self.context_length,
            self.threshold_tokens,
            self.provider_runtime.provider or "none",
            self.provider_runtime.base_url or "none",
            previous_model,
            previous_context_length,
            previous_threshold_tokens,
        )

    def update_from_response(self, usage: dict[str, Any]) -> None:
        """Store real token counts from API response.

        Called by core.py after each API call.
        """
        if not isinstance(usage, dict):
            raise TypeError("usage must be a dictionary")
        self.last_prompt_tokens = int(
            usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0
        )
        self.last_completion_tokens = int(
            usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0
        )
        self.last_total_tokens = int(usage.get("total_tokens", 0) or 0)

    def prune(self, messages: list[dict]) -> tuple[list[dict], int]:
        """Lightweight pre-pass: replace earlier tool results with placeholders.

        Safe to call every turn: it only touches messages outside the
        protected tail window and never drops data silently.

        Returns (pruned_messages, pruned_count).
        """
        return self._prune_earlier_tool_results(
            messages,
            protect_tail_count=self.protect_last_n,
            protect_tail_tokens=self.tail_token_budget,
        )

    def _prune_earlier_tool_results(
        self, messages: list[dict], protect_tail_count: int,
        protect_tail_tokens: int,
    ) -> tuple[list[dict], int]:
        """Replace earlier tool result contents with a short placeholder.

        Walks backward protecting recent messages by token budget.
        Returns (pruned_messages, pruned_count).
        """
        if not messages:
            return messages, 0

        result = [m.copy() for m in messages]
        pruned = 0

        # Find the oldest message that should still be protected.
        accumulated = 0
        boundary = len(result)
        min_protect = min(protect_tail_count, len(result) - 1)
        for i in range(len(result) - 1, -1, -1):
            msg = result[i]
            content = _content_to_text(msg.get("content"))
            msg_tokens = estimate_tokens_rough(content) + 10
            for tc in msg.get("tool_calls") or []:
                if isinstance(tc, dict):
                    args = tc.get("function", {}).get("arguments", "")
                    msg_tokens += estimate_tokens_rough(_content_to_text(args))
            if accumulated + msg_tokens > protect_tail_tokens and (len(result) - i) >= min_protect:
                boundary = i
                break
            accumulated += msg_tokens
            boundary = i
        prune_boundary = max(boundary, len(result) - min_protect)

        for i in range(prune_boundary):
            msg = result[i]
            if msg.get("role") != "tool":
                continue
            content = msg.get("content", "")
            if not content or content == _PRUNED_TOOL_PLACEHOLDER:
                continue
            # Only prune substantial content (>200 chars)
            if len(content) > 200:
                result[i] = {**msg, "content": _PRUNED_TOOL_PLACEHOLDER}
                pruned += 1

        return result, pruned

    # ------------------------------------------------------------------
    # Content serialization for summarizer (with head+tail truncation)
    # ------------------------------------------------------------------

    def _serialize_for_summary(self, turns: list[dict]) -> str:
        """Serialize turns with head+tail truncation.

        Preserves both the beginning and end of long content so that
        file paths, variable names, and results are not lost.
        """
        parts = []
        for msg in turns:
            role = msg.get("role", "unknown")
            content = _content_to_text(msg.get("content"))

            # Tool results: head+tail truncation
            if role == "tool":
                tool_id = _content_to_text(msg.get("tool_call_id"))
                if len(content) > _CONTENT_MAX:
                    content = content[:_CONTENT_HEAD] + "\n...[truncated]...\n" + content[-_CONTENT_TAIL:]
                parts.append(f"[TOOL RESULT {tool_id}]: {content}")
                continue

            # Assistant messages keep tool-call names and arguments.
            if role == "assistant":
                if len(content) > _CONTENT_MAX:
                    content = content[:_CONTENT_HEAD] + "\n...[truncated]...\n" + content[-_CONTENT_TAIL:]
                tool_calls = msg.get("tool_calls", [])
                if tool_calls:
                    tc_parts = []
                    for tc in tool_calls:
                        if isinstance(tc, dict):
                            fn = tc.get("function", {})
                            name = _content_to_text(fn.get("name")) or "?"
                            args = _content_to_text(fn.get("arguments"))
                            if len(args) > _TOOL_ARGS_MAX:
                                args = args[:_TOOL_ARGS_HEAD] + "..."
                            tc_parts.append(f"  {name}({args})")
                        else:
                            fn = getattr(tc, "function", None)
                            name = getattr(fn, "name", "?") if fn else "?"
                            tc_parts.append(f"  {name}(...)")
                    content += "\n[Tool calls:\n" + "\n".join(tc_parts) + "\n]"
                parts.append(f"[ASSISTANT]: {content}")
                continue

            # User and other roles.
            if len(content) > _CONTENT_MAX:
                content = content[:_CONTENT_HEAD] + "\n...[truncated]...\n" + content[-_CONTENT_TAIL:]
            parts.append(f"[{role.upper()}]: {content}")

        return "\n\n".join(parts)

    def _find_tail_cut_by_tokens(
        self, messages: list[dict], head_end: int,
        token_budget: int | None = None,
    ) -> int:
        """Walk backward from the end, accumulating tokens until budget is reached.

        Returns the index where the tail starts. Never cuts inside a
        tool_call/result group.
        """
        if token_budget is None:
            token_budget = self.tail_token_budget
        n = len(messages)

        # Hard minimum: always keep at least 3 messages in the tail
        min_tail = min(3, n - head_end - 1) if n - head_end > 1 else 0
        soft_ceiling = int(token_budget * 1.5)
        accumulated = 0
        cut_idx = n

        for i in range(n - 1, head_end - 1, -1):
            msg = messages[i]
            content = _content_to_text(msg.get("content"))
            msg_tokens = estimate_tokens_rough(content) + 10
            for tc in msg.get("tool_calls") or []:
                if isinstance(tc, dict):
                    args = tc.get("function", {}).get("arguments", "")
                    msg_tokens += estimate_tokens_rough(_content_to_text(args))
            if accumulated + msg_tokens > soft_ceiling and (n - i) >= min_tail:
                break
            accumulated += msg_tokens
            cut_idx = i

        # Always protect at least min_tail recent messages.
        fallback_cut = n - min_tail
        if cut_idx > fallback_cut:
            cut_idx = fallback_cut

        # If the budget would protect everything, force a cut after the head.
        if cut_idx <= head_end:
            cut_idx = max(fallback_cut, head_end + 1)

        # Align boundaries so tool-call groups stay intact.
        cut_idx = self._align_boundary_backward(messages, cut_idx)

        return max(cut_idx, head_end + 1)

    @staticmethod
    def _align_boundary_forward(messages: list[dict], idx: int) -> int:
        """Advance idx past tool result messages to keep each group intact."""
        while idx < len(messages) and messages[idx].get("role") == "tool":
            idx += 1
        return idx

    @staticmethod
    def _align_boundary_backward(messages: list[dict], idx: int) -> int:
        """Pull idx backward to avoid splitting a tool_call / result group.

        If boundary falls in the middle of a tool-result group, walk backward
        to the parent assistant message so the whole group is included in
        the summarized region as a complete group.
        """
        if idx <= 0 or idx >= len(messages):
            return idx
        check = idx - 1
        while check >= 0 and messages[check].get("role") == "tool":
            check -= 1
        if check >= 0 and messages[check].get("role") == "assistant" and messages[check].get("tool_calls"):
            idx = check
        return idx

    def _compute_summary_budget(self, turns_to_summarize: list[dict]) -> int:
        """Scale summary token budget with the amount of content being compressed."""
        content_tokens = estimate_messages_tokens(turns_to_summarize)
        budget = int(content_tokens * _SUMMARY_RATIO)
        return max(_MIN_SUMMARY_TOKENS, min(budget, self.max_summary_tokens))

    def _generate_summary(self, turns_to_summarize: list[dict]) -> str | None:
        """Generate a structured summary of compacted conversation turns."""
        now = time.monotonic()
        if now < self._summary_failure_cooldown_until:
            logger.debug(
                "Skipping context summary during cooldown (%.0fs remaining)",
                self._summary_failure_cooldown_until - now,
            )
            return None

        summary_budget = self._compute_summary_budget(turns_to_summarize)
        content_to_summarize = self._serialize_for_summary(turns_to_summarize)

        prompt = build_context_compression_prompt(
            previous_summary=self._previous_summary or "",
            content_to_summarize=content_to_summarize,
            summary_budget=summary_budget,
        )

        try:
            logger.info(
                "[_generate_summary] calling summarization API (mode=%s)",
                self._summary_context().api_mode,
            )
            _summarize_start = time.monotonic()
            summary = self._summarize(prompt, summary_budget)
            _summarize_elapsed = time.monotonic() - _summarize_start
            logger.info("[_generate_summary] summarization API returned in %.2fs, got_summary=%s", _summarize_elapsed, bool(summary))
            if summary:
                # Store the rolling summary without duplicating the public prefix.
                stored = summary.replace(SUMMARY_PREFIX, "").strip()
                self._previous_summary = stored
            return summary
        except Exception as e:
            exc_name = type(e).__name__.lower()
            exc_msg = str(e).lower()
            is_auth = (
                "authentication" in exc_name or "401" in exc_msg
                or "403" in exc_msg or "api key" in exc_msg
                or "api_key" in exc_msg or "invalid_api_key" in exc_msg
                or "unauthorized" in exc_msg or "permission" in exc_msg
            )
            if is_auth:
                logger.error("Summary generation failed (authentication error): %s", e)
            else:
                logger.warning("Summary generation failed: %s", e)
            self._summary_failure_cooldown_until = time.monotonic() + _SUMMARY_FAILURE_COOLDOWN_SECONDS
            return None

    def _summary_context(self) -> ProviderRuntimeContext:
        if self._summary_runtime is not None:
            return self._summary_runtime

        from mclaw.providers.resolver import (
            default_model_for_provider,
            resolve_provider_runtime_context,
        )

        provider = self.summary_provider.strip()
        model = self.summary_model.strip()
        if not provider or provider == "auto":
            if not self.summary_base_url:
                normalized_model = self.provider_runtime.profile.normalize_model(
                    model or self.provider_runtime.model
                )
                self._summary_runtime = replace(
                    self.provider_runtime,
                    model=normalized_model,
                    api_key=self.summary_api_key or self.provider_runtime.api_key,
                    reasoning_config=None,
                )
                return self._summary_runtime
            provider = (
                "custom_anthropic"
                if self.provider_runtime.api_mode == "anthropic_messages"
                else "custom"
            )
            model = model or self.provider_runtime.model
            api_key = self.summary_api_key or self.provider_runtime.api_key
        else:
            model = model or default_model_for_provider(provider, config=self.config)
            api_key = self.summary_api_key

        self._summary_runtime = resolve_provider_runtime_context(
            provider=provider,
            model=model,
            base_url=self.summary_base_url,
            api_key=api_key,
            config=self.config,
        )
        return self._summary_runtime

    def _summarize(self, prompt: str, budget: int) -> str:
        if self._summary_transport is None:
            self._summary_transport = create_transport(self._summary_context())
        result = self._summary_transport.call(
            messages=[{"role": "user", "content": prompt}],
            tools=[],
            options=ModelCallOptions(
                timeout=float(self.summary_timeout),
                max_output_tokens=budget * 2,
                source="summary",
                cache_plan=None,
            ),
        )
        if result.usage is not None and self._usage_callback is not None:
            self._usage_callback(result.usage)
        summary = result.content.strip()
        return f"{SUMMARY_PREFIX}\n{summary}" if summary else ""

    @staticmethod
    def _sanitize_tool_pairs(messages: list[dict]) -> list[dict]:
        """Remove orphaned tool results and insert stubs for missing results."""
        # Collect call IDs that survived compression.
        surviving_call_ids = set()
        for msg in messages:
            if msg.get("role") == "assistant":
                for tc in msg.get("tool_calls") or []:
                    tc_id = tc.get("id", "") if isinstance(tc, dict) else getattr(tc, "id", "") or ""
                    if tc_id:
                        surviving_call_ids.add(tc_id)

        # Collect result IDs still present in the message list.
        present_result_ids = set()
        for msg in messages:
            if msg.get("role") == "tool":
                tid = msg.get("tool_call_id", "")
                if tid:
                    present_result_ids.add(tid)

        # Remove tool results whose assistant call was summarized away.
        orphaned_results = present_result_ids - surviving_call_ids
        if orphaned_results:
            messages = [
                m for m in messages
                if not (m.get("role") == "tool" and m.get("tool_call_id") in orphaned_results)
            ]
            logger.info("Compression sanitizer: removed %d orphaned tool result(s)", len(orphaned_results))

        # Add stub results for surviving calls whose results were summarized away.
        surviving_call_ids = set()
        for msg in messages:
            if msg.get("role") == "assistant":
                for tc in msg.get("tool_calls") or []:
                    tc_id = tc.get("id", "") if isinstance(tc, dict) else getattr(tc, "id", "") or ""
                    if tc_id:
                        surviving_call_ids.add(tc_id)

        result_call_ids = set()
        for msg in messages:
            if msg.get("role") == "tool":
                tid = msg.get("tool_call_id", "")
                if tid:
                    result_call_ids.add(tid)

        missing_results = surviving_call_ids - result_call_ids
        if missing_results:
            patched: list[dict] = []
            for msg in messages:
                patched.append(msg)
                if msg.get("role") == "assistant" and msg.get("tool_calls"):
                    for tc in msg.get("tool_calls") or []:
                        tc_id = tc.get("id", "") if isinstance(tc, dict) else getattr(tc, "id", "") or ""
                        if tc_id in missing_results:
                            patched.append({
                                "role": "tool",
                                "tool_call_id": tc_id,
                                "content": "[Result from earlier conversation; see context summary above]",
                            })
            messages = patched
            logger.info("Compression sanitizer: added %d stub tool result(s)", len(missing_results))

        return messages

    def compress(self, messages: list[dict]) -> list[dict]:
        """Compress conversation history.

        Returns a new list with middle turns replaced by a structured summary.
        """
        n = len(messages)
        _min_for_compress = self.protect_first_n + 3 + 1
        if n <= _min_for_compress:
            return messages

        logger.info("[COMPRESSION START] messages=%d threshold=%d", n, self.threshold_tokens)

        # Phase 1: prune earlier tool results as a cheap pre-pass.
        logger.info("[COMPRESSION] Phase 1: pruning earlier tool results")
        messages, pruned_count = self._prune_earlier_tool_results(
            messages,
            protect_tail_count=self.protect_last_n,
            protect_tail_tokens=self.tail_token_budget,
        )
        if pruned_count:
            logger.info("Pre-compression: pruned %d earlier tool result(s)", pruned_count)

        # Phase 2: Determine boundaries
        logger.info("[COMPRESSION] Phase 2: determining boundaries")
        compress_start = self._align_boundary_forward(messages, self.protect_first_n)

        # Protect recent tail messages with a token budget.
        compress_end = self._find_tail_cut_by_tokens(messages, compress_start)
        # If the boundary lands on a tool result, move it backward so the
        # tool_call/result pair is summarized together.
        compress_end = self._align_boundary_backward(messages, compress_end)

        if compress_start >= compress_end:
            logger.info("[COMPRESSION] boundaries invalid (start=%d >= end=%d), skipping", compress_start, compress_end)
            return messages

        turns_to_summarize = messages[compress_start:compress_end]
        logger.info(
            "Context compression: summarizing turns %d-%d (%d turns), protecting %d head + %d tail",
            compress_start + 1, compress_end, len(turns_to_summarize),
            compress_start, n - compress_end,
        )

        # Phase 3: Generate structured summary
        logger.info("[COMPRESSION] Phase 3: generating summary")
        summary = self._generate_summary(turns_to_summarize)
        logger.info("[COMPRESSION] summary generated: len=%d", len(summary) if summary else 0)

        if not summary:
            logger.warning(
                "Summary generation unavailable; keeping conversation turns and "
                "skipping summary-based compaction"
            )
            return messages

        # Phase 4: Assemble compressed messages
        logger.info("[COMPRESSION] Phase 4: assembling compressed messages")
        compressed = []

        for i in range(compress_start):
            msg = messages[i].copy()
            if i == 0 and msg.get("role") == "system" and self.compression_count == 0:
                msg["content"] = (
                    (msg.get("content") or "")
                    + "\n\n[Note: Some earlier conversation turns have been compacted into a "
                    "handoff summary to preserve context space. The current session state "
                    "may still reflect earlier work, so build on that summary and state "
                    "rather than re-doing work.]"
                )
            compressed.append(msg)

        # Choose a summary role that avoids adjacent same-role messages.
        last_head_role = messages[compress_start - 1].get("role", "user") if compress_start > 0 else "user"
        first_tail_role = messages[compress_end].get("role", "user") if compress_end < n else "user"

        if last_head_role in ("assistant", "tool"):
            summary_role = "user"
        else:
            summary_role = "assistant"

        if summary_role == first_tail_role:
            flipped = "assistant" if summary_role == "user" else "user"
            if flipped != last_head_role:
                summary_role = flipped

        compressed.append({"role": summary_role, "content": summary})

        for i in range(compress_end, n):
            compressed.append(messages[i].copy())

        self.compression_count += 1
        self._compressed_this_turn = True

        # Phase 5: repair tool-call/result pairs.
        logger.info("[COMPRESSION] Phase 5: sanitizing tool pairs")
        compressed = self._sanitize_tool_pairs(compressed)

        before = estimate_messages_tokens(messages)
        after = estimate_messages_tokens(compressed)
        logger.info(
            "Compressed context: %d→%d messages, ~%d→~%d tokens (compression #%d)",
            n, len(compressed), before, after, self.compression_count,
        )
        logger.info("[COMPRESSION END] compression #%d complete", self.compression_count)

        # Reset file-read dedup after compaction: original read contents may
        # have been summarized away, so a later read should be allowed.
        try:
            from mclaw.tools.read_tracker import reset_file_dedup
            reset_file_dedup(task_id=self.session_id or None)
        except Exception as exc:
            logger.debug("File-read dedup reset after compression failed: %s", exc)

        return compressed
