# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Render prompt_toolkit status fragments for live turn and context state."""

from __future__ import annotations

from datetime import datetime

from mclaw.cli.runtime.events import RuntimeStatus

_CTX_BAR_WIDTH = 8
_CTX_FILLED = "■"
_CTX_EMPTY = "□"
_CTX_THRESHOLD = "▣"

_CTX_COLOR_LOW = "#7dd3fc"
_CTX_COLOR_NORM = "#7ee787"
_CTX_COLOR_WARN = "#fcd34d"
_CTX_COLOR_HIGH = "#fb923c"
_CTX_COLOR_CRIT = "#f87171"

_STATUS_STYLES = {
    RuntimeStatus.IDLE: ("Idle", "#8ab4d6"),
    RuntimeStatus.REQUESTING: ("Requesting", "#6CB4EE"),
    RuntimeStatus.STREAMING: ("Streaming", "#f0c040"),
    RuntimeStatus.TOOLS: ("Tools", "#4A90D9"),
    RuntimeStatus.DELEGATING: ("Delegating", "#c084fc"),
    RuntimeStatus.AGGREGATING: ("Aggregating", "#4ecdc4"),
    RuntimeStatus.WAITING_FOR_USER: ("Waiting", "#fcd34d"),
    RuntimeStatus.DONE: ("Done", "#50c878"),
    RuntimeStatus.INTERRUPTED: ("Interrupted", "#fcd34d"),
    RuntimeStatus.ERROR: ("Error", "#f87171"),
}

_STATUS_ANIM_FRAMES = [
    "🌕", "🌖", "🌗", "🌘", "🌑", "🌑", "🌒", "🌓", "🌔", "🌕",
]
STATUS_ANIM_FRAME_COUNT = len(_STATUS_ANIM_FRAMES)


def format_duration(seconds: float) -> str:
    """Format short live durations for the compact status bar."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    m, s = divmod(int(seconds), 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


def fmt_tokens(n: int) -> str:
    """Format token counts with compact suffixes for fixed-width status fragments."""
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        s = f"{n / 1000:.1f}"
        return s.rstrip("0").rstrip(".") + "k"
    s = f"{n / 1_000_000:.1f}"
    return s.rstrip("0").rstrip(".") + "M"


def user_message_count(agent) -> int:
    """Read the authoritative user message count with a message-list fallback."""
    if not agent:
        return 0
    value = getattr(agent, "session_user_messages", None)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return max(0, int(value))
    messages = getattr(agent, "messages", None)
    if isinstance(messages, list):
        return sum(1 for msg in messages if isinstance(msg, dict) and msg.get("role") == "user")
    return 0


def format_context_bar(compressor) -> tuple[str, str] | tuple[None, None]:
    """Return a threshold-aware context usage bar and prompt_toolkit style."""
    if not compressor or compressor.context_length <= 0:
        return None, None

    if hasattr(compressor, "display_context_tokens"):
        context_tokens = compressor.display_context_tokens
    else:
        context_tokens = compressor.last_prompt_tokens + compressor.last_completion_tokens
    estimated = bool(getattr(compressor, "display_context_estimated", False))
    context_length = compressor.context_length
    threshold_tokens = compressor.threshold_tokens

    usage = (context_tokens or 0) / context_length
    filled = min(int(usage * _CTX_BAR_WIDTH), _CTX_BAR_WIDTH)

    threshold_pos = -1
    if threshold_tokens > 0 and context_length > 0:
        threshold_pos = min(
            int((threshold_tokens / context_length) * _CTX_BAR_WIDTH),
            _CTX_BAR_WIDTH - 1,
        )

    chars = []
    for i in range(_CTX_BAR_WIDTH):
        if i == threshold_pos:
            chars.append(_CTX_THRESHOLD)
        elif i < filled:
            chars.append(_CTX_FILLED)
        else:
            chars.append(_CTX_EMPTY)
    bar = "".join(chars)
    if context_tokens is None:
        label = f"—/{fmt_tokens(context_length)}"
    else:
        estimate_prefix = "~" if estimated else ""
        label = f"{estimate_prefix}{fmt_tokens(context_tokens)}/{fmt_tokens(context_length)}"

    if usage >= 0.90:
        color = _CTX_COLOR_CRIT
    elif usage >= 0.75:
        color = _CTX_COLOR_HIGH
    elif usage >= 0.50:
        color = _CTX_COLOR_WARN
    elif usage >= 0.30:
        color = _CTX_COLOR_NORM
    else:
        color = _CTX_COLOR_LOW

    return f"{bar} {label}", f"fg:{color}"


class StatusRenderer:
    """Build prompt_toolkit fragments for the live status area."""

    def build_status_fragments(self, owner) -> list:
        """Build the primary status line and optional subagent progress rows."""
        if not owner._status_bar_visible:
            return []

        agent = owner.agent
        state = owner.runtime_state
        status = state.status
        model_short = owner.model.split("/")[-1] if "/" in owner.model else owner.model
        if len(model_short) > 24:
            model_short = model_short[:21] + "..."

        total_tokens = (agent.session_input_tokens + agent.session_output_tokens) if agent else 0

        ctx_bar_text = None
        ctx_bar_style = None
        if agent and agent.context_compressor:
            ctx_bar_text, ctx_bar_style = format_context_bar(agent.context_compressor)

        if owner._agent_running and owner._turn_start_at:
            turn_elapsed = format_duration((datetime.now() - owner._turn_start_at).total_seconds())
            time_label = f" {turn_elapsed} "
        else:
            session_elapsed = format_duration((datetime.now() - owner.session_start).total_seconds())
            if owner._last_turn_duration > 0:
                last_turn = format_duration(owner._last_turn_duration)
                time_label = f" last {last_turn} · {session_elapsed} "
            else:
                time_label = f" {session_elapsed} "

        if owner._agent_running:
            moon = _STATUS_ANIM_FRAMES[owner._spinner_idx % len(_STATUS_ANIM_FRAMES)]
        else:
            moon = "🌕"

        fragments = [
            ("class:status-bar-active", f" {moon}"),
            ("class:status-bar-model", f" {model_short} "),
            ("class:status-bar", time_label),
        ]

        if owner._project_name:
            fragments.append(("class:status-bar", f" 📁 {owner._project_name} "))

        if ctx_bar_text and ctx_bar_style:
            fragments.append((ctx_bar_style, f" {ctx_bar_text} "))

        fragments.append(("class:status-bar", f" {fmt_tokens(total_tokens)}tk "))
        fragments.append(("class:status-bar", f" #{user_message_count(agent)} "))
        if owner._input_mode == "asr" or owner._asr_status_text not in ("", "off"):
            asr_label = owner._compact_asr_status(owner._asr_status_text or owner._input_mode)
            fragments.append(("class:status-bar-active", f" ASR:{asr_label} "))

        if owner._agent_running or status == RuntimeStatus.WAITING_FOR_USER:
            status_label, status_color = _STATUS_STYLES[status]
            fragments.append((f"fg:{status_color} bold", f" {status_label} "))
            if state.active_tools:
                tools_str = " | ".join(sorted(state.active_tools))
                if len(tools_str) > 30:
                    tools_str = tools_str[:27] + "..."
                fragments.append(("class:status-bar", f" · {tools_str}"))
            if state.detail:
                fragments.append(("class:status-bar", f" · {state.detail}"))
        elif status == RuntimeStatus.INTERRUPTED:
            fragments.append(("class:status-bar-warning", " ⚡ Interrupted "))
        elif status == RuntimeStatus.ERROR:
            result = state.last_result or {}
            if result.get("stop_reason") == "max_iterations":
                fragments.append(("class:status-bar-warning", " ⚠️ Iteration limit reached "))
            elif result.get("stop_reason") == "timeout":
                fragments.append(("class:status-bar-warning", " ⚠ Timed out "))
            else:
                fragments.append(("class:status-bar-error", " ✗ Error "))
        elif status == RuntimeStatus.DONE:
            fragments.append(("class:status-bar-done", " ✓ Done "))

        if owner.subtask_manager:
            sm = owner.subtask_manager
            running = sum(1 for t in sm.tasks if t["status"] == "running")
            finalizing = sum(1 for t in sm.tasks if t["status"] == "finalizing")
            done = sum(1 for t in sm.tasks if t["status"] == "completed")
            errs = sum(1 for t in sm.tasks if t["status"] in {"error", "failed"})
            timed_out = sum(1 for t in sm.tasks if t["status"] == "timed_out")
            interrupted = sum(1 for t in sm.tasks if t["status"] == "interrupted")
            total = len(sm.tasks)
            if running:
                badge_text = f"🔀 {running}/{total} running"
                if finalizing:
                    badge_text += f" · {finalizing} finalizing"
            elif finalizing:
                badge_text = f"🔀 {finalizing}/{total} finalizing"
            elif errs or timed_out or interrupted:
                badge_text = f"🔀 {done}/{total} done · {errs} failed"
                if timed_out:
                    badge_text += f" · {timed_out} timed out"
                if interrupted:
                    badge_text += f" · {interrupted} interrupted"
            else:
                badge_text = f"🔀 {done}/{total} done"
            fragments.append(("class:status-bar-subagent-badge", f" {badge_text}"))

        if owner.subtask_manager:
            fragments.append(("", "\n"))
            fragments.extend(self.build_subagent_compact_progress(owner))

        return fragments

    def build_subagent_compact_progress(self, owner) -> list:
        """Show every delegated task in the compact progress area."""
        sm = getattr(owner, "subtask_manager", None)
        if not sm:
            return []

        frags: list = []
        for task in sm.tasks:
            idx = task["index"] + 1
            status = task["status"]
            goal = self.format_subagent_goal(task["goal"], max_len=100)
            if status in {"error", "failed"}:
                marker = "✗"
                style = "class:status-bar-subagent-error"
            elif status in {"timed_out", "interrupted"}:
                marker = "⚠"
                style = "class:status-bar-subagent-error"
            elif status == "finalizing":
                marker = "◐"
                style = "class:status-bar-subagent-running"
            elif status == "running":
                marker = "◼"
                style = "class:status-bar-subagent-running"
            elif status == "completed":
                marker = "✓"
                style = "class:status-bar-subagent-done"
            else:
                marker = "◻"
                style = "class:status-bar-subagent-pending"
            phase = "（收尾中）" if status == "finalizing" else ""
            frags.append((style, f"{marker} Task {idx}{phase}: "))
            goal_style = (
                style
                if status in {"completed", "error", "failed", "timed_out", "interrupted"}
                else "class:status-bar-subagent-goal"
            )
            frags.append((goal_style, goal))
            frags.append(("", "\n"))
        return frags

    @staticmethod
    def format_subagent_goal(goal: str, max_len: int = 100) -> str:
        """Normalize and trim subagent goals for one-line status rows."""
        text = " ".join(str(goal or "").split())
        if len(text) <= max_len:
            return text
        return text[: max_len - 1] + "…"
