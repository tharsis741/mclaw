# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Cross-session recall tool backed by SessionDB FTS5.

Recent-session browsing returns cheap metadata. Keyword search returns focused
LLM summaries of matched sessions so old transcripts do not flood main context.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from mclaw.tools.dispatch import get_current_session_id, get_session_db
from mclaw.tools.registry import registry, tool_error

logger = logging.getLogger(__name__)

MAX_SESSION_TRANSCRIPT_CHARS = 100_000
MAX_TOOL_RESULT_CHARS = 24_000
MAX_FALLBACK_PREVIEW_CHARS = 1_000
MAX_LOG_SUMMARY_CHARS = 4_000
MAX_SUMMARY_TOKENS = 4_000
DEFAULT_LIMIT = 3
MAX_LIMIT = 5

_HIDDEN_SESSION_SOURCES = frozenset({"tool"})
_ERROR_DETAIL_MAX_CHARS = 500


def _coerce_limit(limit: Any) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        value = DEFAULT_LIMIT
    return max(1, min(value, MAX_LIMIT))


def _resolve_db(parent_agent: Any = None) -> Any:
    """Prefer dispatch-scoped DB, then fall back to the parent agent handle."""
    db = get_session_db()
    if db is not None:
        return db
    return getattr(parent_agent, "_session_db", None) if parent_agent is not None else None


def _resolve_current_session_id(parent_agent: Any = None) -> str:
    """Resolve the active session so recall can exclude the current thread tree."""
    current = get_current_session_id()
    if current:
        return current
    return str(getattr(parent_agent, "session_id", "") or "") if parent_agent is not None else ""


def _resolve_workspace(parent_agent: Any = None) -> str:
    if parent_agent is None:
        return ""
    return str(getattr(parent_agent, "workspace_path", "") or "").strip()


def _resolve_to_parent(session_id: str, db: Any) -> str:
    """Walk a session parent chain to the root session."""
    visited = set()
    sid = session_id
    while sid and sid not in visited:
        visited.add(sid)
        try:
            session = db.get_session(sid)
        except Exception:
            break
        if not session:
            break
        parent = session.get("parent_session_id")
        if not parent:
            break
        sid = parent
    return sid


def _format_timestamp(ts: Any) -> str:
    if ts is None:
        return "unknown"
    try:
        if isinstance(ts, str):
            if not ts.replace(".", "", 1).isdigit():
                return ts
            ts = float(ts)
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))
    except Exception:
        return str(ts)


def _build_conversation_text(messages: list[dict[str, Any]]) -> str:
    """Render stored messages into a compact transcript for summarization."""
    parts: list[str] = []
    for msg in messages:
        role = str(msg.get("role") or "unknown").upper()
        content = msg.get("content") or ""
        if isinstance(content, list):
            content = json.dumps(content, ensure_ascii=False)

        if role == "TOOL":
            tool_name = msg.get("tool_name") or "tool"
            if len(content) > 600:
                content = f"{content[:300]}\n...[truncated]...\n{content[-300:]}"
            parts.append(f"[TOOL:{tool_name}] {content}")
            continue

        if role == "ASSISTANT":
            tool_calls = msg.get("tool_calls")
            if isinstance(tool_calls, list) and tool_calls:
                names = []
                for call in tool_calls:
                    if isinstance(call, dict):
                        fn = call.get("function", {})
                        names.append(call.get("name") or fn.get("name") or "?")
                if names:
                    parts.append(f"[ASSISTANT called tools: {', '.join(names)}]")

        if len(content) > 4_000:
            content = f"{content[:2_500]}\n...[truncated]...\n{content[-1_000:]}"
        parts.append(f"[{role}] {content}")
    return "\n\n".join(parts)


def _truncate_around_matches(
    full_text: str,
    query: str,
    max_chars: int = MAX_SESSION_TRANSCRIPT_CHARS,
) -> str:
    """Keep summarization input centered on the first likely query match."""
    if not full_text or len(full_text) <= max_chars:
        return full_text

    text_lower = full_text.lower()
    terms = [t for t in re.split(r"\s+", query.lower().strip()) if t and t.upper() not in {"AND", "OR", "NOT"}]
    positions: list[int] = []

    phrase = query.lower().strip().strip('"')
    if phrase:
        positions.extend(m.start() for m in re.finditer(re.escape(phrase), text_lower))

    if not positions:
        for term in terms:
            clean = term.strip('"*')
            if clean:
                positions.extend(m.start() for m in re.finditer(re.escape(clean), text_lower))

    if not positions:
        start = 0
    else:
        positions.sort()
        start = max(0, positions[0] - max_chars // 3)
        if start + max_chars > len(full_text):
            start = max(0, len(full_text) - max_chars)

    end = min(len(full_text), start + max_chars)
    prefix = "...[earlier conversation truncated]...\n\n" if start > 0 else ""
    suffix = "\n\n...[later conversation truncated]..." if end < len(full_text) else ""
    return prefix + full_text[start:end] + suffix


def _first_user_title(messages: list[dict[str, Any]]) -> str | None:
    for msg in messages:
        if msg.get("role") == "user":
            title = (msg.get("content") or "").strip()
            if title:
                return title[:120]
    return None


def _fallback_summary(conversation_text: str) -> str:
    preview = (conversation_text or "No preview available.").strip()
    if len(preview) > MAX_FALLBACK_PREVIEW_CHARS:
        preview = preview[:MAX_FALLBACK_PREVIEW_CHARS] + "\n...[truncated preview]"
    return "[Raw preview: session summarization unavailable]\n" + preview


def _safe_error_detail(exc: BaseException | str) -> str:
    """Format exception text for tool output while redacting likely secrets."""
    detail = str(exc)
    if not isinstance(exc, str):
        detail = f"{type(exc).__name__}: {detail or type(exc).__name__}"
    detail = re.sub(
        r"(?i)\b(api[_-]?key|token|secret|password)\s*[:=]\s*['\"]?[^'\"\s,;]+",
        r"\1=<redacted>",
        detail,
    )
    detail = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "<redacted>", detail)
    if len(detail) > _ERROR_DETAIL_MAX_CHARS:
        detail = detail[:_ERROR_DETAIL_MAX_CHARS] + "...[truncated]"
    return detail


def _for_log(text: str, max_chars: int = MAX_LOG_SUMMARY_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n...[log truncated: {len(text) - max_chars} chars omitted]"


def _strip_reasoning_blocks(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.IGNORECASE | re.DOTALL)
    return text.strip()


def _summarize_conversation(
    conversation_text: str,
    query: str,
    session_meta: dict[str, Any],
    parent_agent: Any = None,
) -> tuple[str, bool, str | None]:
    """Summarize a matched session, returning a preview fallback on LLM failure."""
    system_prompt = (
        "你在为当前 Agent 召回历史会话。围绕检索主题输出中文事实摘要。"
        "保留用户目标、已做操作、关键决定、命令、文件路径、错误、结果和未解决事项。"
        "英文命令、路径和报错原样保留。"
    )
    user_prompt = (
        f"检索主题：{query}\n"
        f"会话来源：{session_meta.get('source', 'unknown')}\n"
        f"会话时间：{_format_timestamp(session_meta.get('started_at'))}\n\n"
        f"会话记录：\n{conversation_text}\n\n"
        f"请围绕这个主题总结该会话：{query}"
    )
    try:
        from mclaw.agent.auxiliary_client import call_auxiliary_llm

        summary = _strip_reasoning_blocks(call_auxiliary_llm(
            task="session_search",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            parent_agent=parent_agent,
            temperature=0.1,
            max_tokens=MAX_SUMMARY_TOKENS,
        ))
        if summary:
            logger.info(
                "session_search summarizer returned summary: query=%r source=%s summary_len=%d",
                query,
                session_meta.get("source", "unknown"),
                len(summary),
            )
            logger.debug(
                "session_search summarizer summary preview: query=%r source=%s summary=%r",
                query,
                session_meta.get("source", "unknown"),
                _for_log(summary),
            )
            return summary, False, None
        summary_error = "Auxiliary model returned an empty summary."
    except Exception as exc:
        summary_error = _safe_error_detail(exc)
        logger.warning("Session summarization unavailable: %s", summary_error)
    fallback = _fallback_summary(conversation_text)
    logger.info(
        "session_search using fallback preview: query=%r source=%s preview_len=%d",
        query,
        session_meta.get("source", "unknown"),
        len(fallback),
    )
    logger.debug(
        "session_search fallback preview body: query=%r source=%s preview=%r",
        query,
        session_meta.get("source", "unknown"),
        _for_log(fallback),
    )
    return fallback, True, summary_error


def _build_session_summary(
    db: Any,
    session_id: str,
    query: str,
    match_info: dict[str, Any],
    parent_agent: Any = None,
) -> dict[str, Any] | None:
    """Build one search result by loading, trimming, and summarizing a session."""
    session_meta = db.get_session(session_id) or {}
    messages = db.get_messages_as_conversation(session_id)
    if not messages:
        return None

    conversation_text = _build_conversation_text(messages)
    conversation_text = _truncate_around_matches(conversation_text, query)
    summary, fallback, summary_error = _summarize_conversation(conversation_text, query, session_meta, parent_agent)

    entry = {
        "session_id": session_id,
        "when": _format_timestamp(session_meta.get("started_at") or match_info.get("session_started")),
        "source": session_meta.get("source") or match_info.get("source", "unknown"),
        "model": session_meta.get("model") or match_info.get("model", ""),
        "title": session_meta.get("title") or _first_user_title(messages),
        "message_count": session_meta.get("message_count", len(messages)),
        "summary": summary,
    }
    if fallback:
        entry["summary_unavailable"] = True
        if summary_error:
            entry["summary_unavailable_reason"] = summary_error
    logger.info(
        "session_search built result: session_id=%s has_model_summary=%s summary_len=%d",
        session_id,
        not fallback,
        len(summary),
    )
    logger.debug(
        "session_search built result summary preview: session_id=%s summary=%r",
        session_id,
        _for_log(summary),
    )
    return entry


def _list_recent_sessions(
    db: Any,
    limit: int,
    current_session_id: str,
    workspace: str = "",
) -> list[dict[str, Any]]:
    """List recent session metadata while hiding tool-only and current-root sessions."""
    rows = db.list_sessions_rich(
        exclude_sources=list(_HIDDEN_SESSION_SOURCES),
        limit=limit + 5,
        workspace=workspace or None,
    )
    current_root = _resolve_to_parent(current_session_id, db) if current_session_id else None
    results = []
    for row in rows:
        sid = row.get("id", "")
        resolved = _resolve_to_parent(sid, db) if sid else sid
        if current_root and resolved == current_root:
            continue
        results.append({
            "session_id": sid,
            "when": _format_timestamp(row.get("started_at")),
            "source": row.get("source", "unknown"),
            "model": row.get("model", ""),
            "title": row.get("title") or None,
            "preview": row.get("preview", ""),
            "message_count": row.get("message_count", 0),
        })
        if len(results) >= limit:
            break
    return results


def session_search(
    query: str = "",
    role_filter: str | None = None,
    limit: int = DEFAULT_LIMIT,
    parent_agent: Any = None,
) -> str:
    """Search past sessions by keyword or browse recent session metadata."""
    db = _resolve_db(parent_agent)
    if db is None:
        return tool_error("Session database not available.", success=False)

    limit = _coerce_limit(limit)
    current_session_id = _resolve_current_session_id(parent_agent)
    workspace = _resolve_workspace(parent_agent)

    if not query or not str(query).strip():
        recent = _list_recent_sessions(db, limit, current_session_id, workspace=workspace)
        return json.dumps({
            "success": True,
            "mode": "recent",
            "results": recent,
            "count": len(recent),
        }, ensure_ascii=False)

    query = str(query).strip()
    role_list = [r.strip() for r in role_filter.split(",") if r.strip()] if role_filter else None

    try:
        raw_results = db.search_messages(
            query=query,
            role_filter=role_list,
            exclude_sources=list(_HIDDEN_SESSION_SOURCES),
            limit=50,
            offset=0,
            workspace=workspace or None,
        )
    except Exception as exc:
        detail = _safe_error_detail(exc)
        logger.warning("Session search query failed: %s", detail, exc_info=True)
        return tool_error(
            "Session search failed while querying the session index.",
            success=False,
            mode="search",
            query=query,
            error_type=type(exc).__name__,
            detail=detail,
        )

    if not raw_results:
        return json.dumps({
            "success": True,
            "mode": "search",
            "query": query,
            "results": [],
            "count": 0,
            "sessions_searched": 0,
            "message": "No matching sessions found.",
        }, ensure_ascii=False)

    current_root = _resolve_to_parent(current_session_id, db) if current_session_id else None
    seen_sessions: dict[str, dict[str, Any]] = {}
    for result in raw_results:
        raw_sid = result.get("session_id", "")
        if not raw_sid:
            continue
        resolved_sid = _resolve_to_parent(raw_sid, db)
        if current_root and resolved_sid == current_root:
            continue
        if resolved_sid not in seen_sessions:
            seen_sessions[resolved_sid] = result
        if len(seen_sessions) >= limit:
            break

    summaries: list[dict[str, Any]] = []
    partial_errors: list[dict[str, str]] = []
    for sid, match_info in seen_sessions.items():
        try:
            summary = _build_session_summary(db, sid, query, match_info, parent_agent)
            if summary:
                summaries.append(summary)
        except Exception as exc:
            detail = _safe_error_detail(exc)
            logger.warning("Failed to summarize session %s: %s", sid, detail, exc_info=True)
            partial_errors.append({"session_id": sid, "error": detail})

    payload: dict[str, Any] = {
        "success": True,
        "mode": "search",
        "query": query,
        "results": summaries,
        "count": len(summaries),
        "sessions_searched": len(seen_sessions),
    }
    if partial_errors:
        payload["partial_errors"] = partial_errors
    return json.dumps(payload, ensure_ascii=False)


SESSION_SEARCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "session_search",
        "description": (
            "Search past conversations, or browse recent sessions. This is cross-session "
            "recall for the agent.\n\n"
            "TWO MODES:\n"
            "1. Recent sessions (no query): call with no arguments to see recent session "
            "metadata, titles, previews, and timestamps. This mode does not call an LLM.\n"
            "2. Keyword search (with query): search specific topics across past sessions. "
            "Returns focused summaries of matching sessions, not raw transcripts.\n\n"
            "Use proactively when the user references previous work, earlier decisions, "
            "or asks to continue something not present in current context. Use OR between "
            "keywords for broad recall; use quotes for exact phrases."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Search query. Omit to browse recent sessions. Supports FTS5 syntax: "
                        "keywords, OR, NOT, quoted phrases, and prefix wildcard such as deploy*."
                    ),
                },
                "role_filter": {
                    "type": "string",
                    "description": (
                        "Optional comma-separated roles to search, e.g. 'user,assistant' "
                        "to skip tool outputs."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": f"Max sessions to summarize (default {DEFAULT_LIMIT}, max {MAX_LIMIT}).",
                    "default": DEFAULT_LIMIT,
                },
            },
            "required": [],
        },
    },
}


registry.register(
    name="session_search",
    toolset="session_search",
    schema=SESSION_SEARCH_SCHEMA,
    handler=lambda args, **kw: session_search(
        query=args.get("query", ""),
        role_filter=args.get("role_filter"),
        limit=args.get("limit", DEFAULT_LIMIT),
        parent_agent=kw.get("parent_agent"),
    ),
    description="Search past conversations",
    emoji="🕘",
    max_result_size_chars=MAX_TOOL_RESULT_CHARS,
)
