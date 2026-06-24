"""Delegate Tool — 子代理架构

在独立线程中 spawn 子 MClaw 实例，拥有：
  - 隔离的对话历史（无父代理历史继承）
  - 受限的工具集（DEFAULT_DELEGATE_TOOLSETS - DELEGATE_BLOCKED_TOOLS）
  - 轻量的系统提示词（直接注入，不走9层构建）
  - 深度限制（MAX_DELEGATE_DEPTH=2）

父代理的上下文只看到 delegation call 和最终 summary，
不暴露子代理的中间 tool calls 或推理过程。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Empty, Queue
from typing import Any, Callable, Dict, List, Optional

from mclaw.tools.path_extract import extract_absolute_paths
from mclaw.tools.registry import registry, tool_error

logger = logging.getLogger(__name__)

# ── 常量 ──────────────────────────────────────────────────────────────────

DELEGATE_BLOCKED_TOOLS = frozenset([
    "delegate_task",    # 禁止递归代理
    "memory",           # 旧记忆工具名，保留为防御项
    "memory_read",
    "memory_add",
    "memory_replace",
    "memory_remove",
    "session_search",    # 禁止跨 session 搜索（破坏隔离）
    "skill_manage",     # 禁止修改共享技能库
    "skill_search",     # 禁止子代理发现/安装外部 Skill
])

_BLOCKED_TOOLSET_NAMES = frozenset(["delegation", "memory"])

DEFAULT_DELEGATE_TOOLSETS = ["terminal", "file"]
ALLOWED_DELEGATE_TOOLSETS = frozenset([
    "terminal",
    "file",
    "vision",
    "web",
    "browser",
    "delegation_skill_read",
])

MAX_CONCURRENT_CHILDREN = 5
MAX_DELEGATE_DEPTH = 2
DEFAULT_MAX_ITERATIONS = 10  # 子代理迭代上限。预注入目录树后 3-5 轮即可完成分析，10 轮是硬上限防止无限探索。
MAX_SUMMARY_CHARS = 800  # 子代理摘要上限，保留有效信息
MAX_WORKSPACE_COPY_FILES = 300
MAX_WORKSPACE_COPY_BYTES = 30 * 1024 * 1024
DELEGATION_COPY_EXCLUDES = frozenset({
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    "dist",
    "build",
    ".next",
    ".turbo",
})

# ── 进度回调 ───────────────────────────────────────────────────────────

class SubtaskEvent:
    """子代理进度事件，线程安全传递"""
    def __init__(
        self,
        task_index: int,
        event_type: str,
        data: Any = None,
        timestamp: float = None,
    ):
        self.task_index = task_index
        self.event_type = event_type  # "started" | "tool_call" | "completed" | "error"
        self.data = data
        self.timestamp = timestamp or time.time()

    def __repr__(self):
        return f"SubtaskEvent(task={self.task_index}, type={self.event_type})"


SUBAGENT_STARTED = "started"
SUBAGENT_TOOL_CALL = "tool_call"
SUBAGENT_COMPLETED = "completed"
SUBAGENT_ERROR = "error"

# 进度回调类型
ProgressCallback = Callable[[SubtaskEvent], None]

# ── 模块级队列（供 TUI 轮询）────────────────────────────────────────────

_subagent_results: Queue = Queue()
_subagent_result_stash: dict[str, Dict[str, Any]] = {}
_subagent_result_stash_lock = threading.Lock()

# ── Prompt 构建 ──────────────────────────────────────────────────────────

def _build_child_system_prompt(
    goal: str,
    context: Optional[str] = None,
    *,
    workspace_path: Optional[str] = None,
    max_iterations: int = 10,
) -> str:
    """构建子代理的轻量系统提示词（不继承父代理提示词）"""
    parts = [
        "你是一个子代理，负责完成父代理委托的独立任务。",
        "",
        f"任务目标：\n{goal}",
    ]
    if context and context.strip():
        parts.append(f"\n上下文信息：\n{context.strip()}")
    if workspace_path and str(workspace_path).strip():
        parts.append(
            f"\n工作区路径：\n{workspace_path.strip()}"
        )
    parts.append(
        "\n执行方式：\n"
        "- 以任务目标为准，结合上下文完成可独立处理的部分。\n"
        "- 本地文件读写以工作区为默认范围；优先使用工作区内已有文件和上下文提供的路径。\n"
        "- 只能通过 terminal 访问 delegation workspace 内的文件，不要声称拥有更高权限。\n"
        "- 不要用 terminal 直接访问 delegation workspace 之外的路径，除非任务上下文明确要求。\n"
        "- 路径或文件不明确时，先做最小范围探索，再读取关键文件。\n"
        "- 信息收集类任务直接返回结论；需要交付文件时再创建或修改文件。\n"
        "- 遇到缺失文件、权限限制、工具不可用或上下文不足时，说明影响和已完成部分。\n"
    )
    parts.append(
        "\n完成要求：\n"
        "完成后返回简洁摘要：\n"
        "- 你做了什么\n"
        "- 完成了什么\n"
        "- 关键发现或结果\n"
        "- 创建或修改了哪些文件\n"
        "- 未完成项或阻塞点\n\n"
        "你的回复会交给父代理继续处理，保持事实化、简洁；保持简洁。"
    )
    return "\n".join(parts)


def _resolve_workspace_hint(parent_agent) -> Optional[str]:
    """从父代理获取最佳本地工作区路径提示"""
    candidates = [
        os.getenv("TERMINAL_CWD"),
        getattr(parent_agent, "terminal_cwd", None),
        getattr(parent_agent, "cwd", None),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            text = os.path.abspath(os.path.expanduser(str(candidate)))
        except Exception:
            continue
        if os.path.isabs(text) and os.path.isdir(text):
            return text
    return None


def _extract_paths_from_text(text: str) -> List[str]:
    """从文本中提取绝对文件路径（支持 Windows 和 Unix）。"""
    return [p for p in extract_absolute_paths(text) if os.path.isabs(os.path.normpath(p))]


def _workspace_preparation_for_child(child: Any) -> Dict[str, Any]:
    preparation = getattr(child, "_workspace_preparation", None)
    return preparation if isinstance(preparation, dict) else {}


def _workspace_preparation_summary(child: Any) -> Dict[str, Any]:
    preparation = _workspace_preparation_for_child(child)
    return {
        "detected_count": len(preparation.get("detected_paths", [])),
        "copied_count": len(preparation.get("copied_paths", [])),
        "failed_count": len(preparation.get("failed_paths", [])),
        "skipped_count": len(preparation.get("skipped_paths", [])),
        "workspace_path": preparation.get("workspace_path"),
    }


def _generate_dir_tree(path: str, max_depth: int = 4, max_files_per_dir: int = 30) -> str:
    """为子代理生成精简目录树，避免子代理反复 list_directory 探索。"""
    root = Path(path)
    if not root.exists() or not root.is_dir():
        return ""

    lines: list[str] = []

    def _walk(p: Path, prefix: str, depth: int):
        if depth > max_depth:
            return
        try:
            entries = sorted(p.iterdir(), key=lambda e: (e.is_file(), e.name.lower()))
        except OSError:
            return
        # 限制每级目录显示数量，防止大目录刷屏
        shown = entries[:max_files_per_dir]
        for i, entry in enumerate(shown):
            is_last = (i == len(shown) - 1)
            connector = "└── " if is_last else "├── "
            suffix = "/" if entry.is_dir() else ""
            lines.append(f"{prefix}{connector}{entry.name}{suffix}")
            if entry.is_dir() and depth < max_depth:
                ext = "    " if is_last else "│   "
                _walk(entry, prefix + ext, depth + 1)
        if len(entries) > max_files_per_dir:
            lines.append(f"{prefix}... ({len(entries) - max_files_per_dir} more items)")

    lines.append(f"{root.name}/")
    _walk(root, "", 1)
    return "\n".join(lines)


def _copy_path_limited(src: Path, dest: Path) -> tuple[int, int, list[str]]:
    copied_files = 0
    copied_bytes = 0
    skipped: list[str] = []

    def _copy_file(file_src: Path, file_dest: Path) -> None:
        nonlocal copied_files, copied_bytes
        try:
            size = file_src.stat().st_size
        except OSError:
            skipped.append(str(file_src))
            return
        if copied_files + 1 > MAX_WORKSPACE_COPY_FILES:
            skipped.append(str(file_src))
            return
        if copied_bytes + size > MAX_WORKSPACE_COPY_BYTES:
            skipped.append(str(file_src))
            return
        file_dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(file_src, file_dest)
        copied_files += 1
        copied_bytes += size

    if src.is_file():
        _copy_file(src, dest)
        return copied_files, copied_bytes, skipped

    for item in src.rglob("*"):
        rel = item.relative_to(src)
        if any(part in DELEGATION_COPY_EXCLUDES for part in rel.parts):
            if item.is_dir():
                skipped.append(str(item))
            continue
        if item.is_dir():
            continue
        _copy_file(item, dest / rel)

    return copied_files, copied_bytes, skipped


def _prepare_delegation_workspace(
    goal: str,
    context: Optional[str],
    child_delegation_dir: Path,
) -> tuple[str, Optional[str], str, Dict[str, Any]]:
    """将 goal/context 中引用的外部文件/目录复制到子代理 workspace，并改写路径。

    Returns:
        (new_goal, new_context, workspace_path, preparation_report)
    """
    workspace = child_delegation_dir / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    text = f"{goal or ''} {context or ''}"
    paths = _extract_paths_from_text(text)
    # 最长优先，避免部分替换
    paths = sorted(paths, key=len, reverse=True)

    preparation: Dict[str, Any] = {
        "workspace_path": str(workspace),
        "detected_paths": paths,
        "copied_paths": [],
        "failed_paths": [],
        "skipped_paths": [],
    }

    copied: Dict[str, str] = {}
    covered_norms: set = set()

    for original in paths:
        norm = os.path.normpath(original)
        if not os.path.exists(norm):
            preparation["failed_paths"].append({
                "path": original,
                "reason": "not_found",
            })
            logger.warning("[delegate] external path not found for subagent copy: %s", original)
            continue

        # 若已被父目录拷贝覆盖则跳过
        is_covered = False
        for covered in covered_norms:
            if norm == covered or norm.startswith(covered + os.sep):
                is_covered = True
                break
        if is_covered:
            preparation["skipped_paths"].append({
                "path": original,
                "reason": "covered_by_parent_copy",
            })
            continue

        # 跳过 mclaw 系统目录
        lower = norm.lower()
        if ".mclaw" in lower:
            skip = False
            for marker in ("\\workspace\\", "/workspace/", "\\delegations\\", "/delegations/"):
                if marker in lower:
                    skip = True
                    break
            if skip:
                preparation["skipped_paths"].append({
                    "path": original,
                    "reason": "mclaw_internal_path",
                })
                continue

        basename = os.path.basename(norm) or "item"
        dest = workspace / basename
        counter = 1
        while dest.exists() and os.path.normpath(dest) != norm:
            dest = workspace / f"{basename}_{counter}"
            counter += 1

        try:
            if os.path.isdir(norm):
                copied_files, copied_bytes, skipped = _copy_path_limited(Path(norm), dest)
                logger.info(
                    "[delegate] copied directory for subagent: %s -> %s files=%d bytes=%d skipped=%d",
                    norm,
                    dest,
                    copied_files,
                    copied_bytes,
                    len(skipped),
                )
                if copied_files <= 0:
                    preparation["skipped_paths"].append({
                        "path": original,
                        "reason": "copy_limit_or_empty_directory",
                        "skipped": skipped[:20],
                    })
                    continue
            else:
                copied_files, copied_bytes, skipped = _copy_path_limited(Path(norm), dest)
                if copied_files <= 0:
                    preparation["skipped_paths"].append({
                        "path": original,
                        "reason": "copy_limit_or_empty_file",
                        "skipped": skipped[:20],
                    })
                    continue
            copied[original] = str(dest)
            covered_norms.add(norm)
            preparation["copied_paths"].append({
                "source": original,
                "destination": str(dest),
                "files": copied_files,
                "bytes": copied_bytes,
            })
            if skipped:
                preparation["skipped_paths"].append({
                    "path": original,
                    "reason": "partial_copy_skipped_items",
                    "skipped": skipped[:20],
                    "skipped_count": len(skipped),
                })
            logger.info("[delegate] copied for subagent: %s -> %s", norm, dest)
        except Exception as exc:
            preparation["failed_paths"].append({
                "path": original,
                "reason": "copy_error",
                "error": str(exc),
            })
            logger.warning("[delegate] failed to copy %s: %s", norm, exc)

    new_goal = goal or ""
    new_context = context or ""
    for original in sorted(copied.keys(), key=len, reverse=True):
        replacement = copied[original]
        new_goal = new_goal.replace(original, replacement)
        if new_context:
            new_context = new_context.replace(original, replacement)
        # 同时处理正斜杠变体（JSON 转义常见）
        if "\\" in original:
            forward = original.replace("\\", "/")
            new_goal = new_goal.replace(forward, replacement)
            if new_context:
                new_context = new_context.replace(forward, replacement)

    if paths:
        prep_lines = [
            "\n\n【Delegation workspace 准备情况】",
            f"- workspace: {workspace}",
            f"- 检测到外部路径: {len(preparation['detected_paths'])}",
            f"- 已复制: {len(preparation['copied_paths'])}",
            f"- 失败: {len(preparation['failed_paths'])}",
            f"- 跳过: {len(preparation['skipped_paths'])}",
        ]
        for item in preparation["copied_paths"][:10]:
            prep_lines.append(f"- 可用副本: {item['destination']}")
        for item in preparation["failed_paths"][:10]:
            prep_lines.append(f"- 未复制: {item['path']} ({item.get('reason', 'unknown')})")
        if preparation["failed_paths"]:
            prep_lines.append(
                "处理文件时优先使用 workspace 内可用副本；缺少必要文件时，在总结中说明缺失路径和影响。"
            )
        new_context = (new_context or "") + "\n".join(prep_lines)

    # ── 预注入目录树，消除子代理盲目探索 ──
    dir_tree = _generate_dir_tree(str(workspace))
    if dir_tree:
        tree_block = (
            f"\n\n【项目目录结构】\n"
            f"```\n{dir_tree}\n```\n"
            f"以上已包含 workspace 内目录树；请直接读取关键文件进行分析。"
        )
        new_context = (new_context or "") + tree_block

    return new_goal, new_context or None, str(workspace), preparation


# ── 工具集过滤 ────────────────────────────────────────────────────────────

def _strip_blocked_toolsets(toolsets: List[str]) -> List[str]:
    """移除被屏蔽的工具集名称（工具集级别过滤）"""
    return [
        t for t in toolsets
        if t in ALLOWED_DELEGATE_TOOLSETS and t not in _BLOCKED_TOOLSET_NAMES
    ]


def _filter_blocked_tools(tool_names: List[str]) -> List[str]:
    """直接按工具名称过滤被禁止的工具（最终安全防线）"""
    return [t for t in tool_names if t not in DELEGATE_BLOCKED_TOOLS]


def _resolve_child_toolsets(
    requested_toolsets: Optional[List[str]],
    parent_enabled: Optional[List[str]],
    parent_available_toolsets: Optional[List[str]],
) -> List[str]:
    """计算子代理的工具集

    优先级：
      1. 显式指定的 toolsets（父代理调用时传入），但只允许 ALLOWED_DELEGATE_TOOLSETS
      2. 未显式指定时固定使用 DEFAULT_DELEGATE_TOOLSETS
    """
    desired = list(DEFAULT_DELEGATE_TOOLSETS)
    if requested_toolsets:
        desired.extend(str(t) for t in requested_toolsets if str(t).strip())

    parent_allowed = set(parent_available_toolsets) if parent_available_toolsets is not None else None
    effective: list[str] = []
    seen: set[str] = set()
    for toolset in desired:
        if toolset in seen:
            continue
        seen.add(toolset)
        if toolset not in ALLOWED_DELEGATE_TOOLSETS or toolset in _BLOCKED_TOOLSET_NAMES:
            continue
        if parent_allowed is not None and toolset not in parent_allowed:
            continue
        effective.append(toolset)

    return effective


# ── 子代理构建 ────────────────────────────────────────────────────────────

def _build_child_agent(
    task_index: int,
    goal: str,
    context: Optional[str],
    toolsets: Optional[List[str]],
    max_iterations: int,
    parent_agent,
) -> "MClaw":
    """在主线程上构建子 MClaw 实例（线程安全的构造）"""
    from mclaw.agent.core import MClaw
    from mclaw.tools.dispatch import get_tool_definitions, get_toolset_for_tool

    logger.info("[subagent-%d] 构建中, depth=%d", task_index, getattr(parent_agent, "_delegate_depth", 0) + 1)

    # ── 1. 解析父代理工具集信息 ──
    parent_enabled = getattr(parent_agent, "enabled_toolsets", None)

    # 从 valid_tool_names 反推 parent_available_toolsets。子代理不能通过 delegation 获取父代理未暴露的能力。
    parent_available_toolsets: Optional[List[str]] = None
    try:
        parent_available = set()
        for t in getattr(parent_agent, "valid_tool_names", []):
            ts = get_toolset_for_tool(t)
            if ts:
                parent_available.add(ts)
        parent_available_toolsets = list(parent_available) if parent_available else None
    except Exception:
        parent_available_toolsets = None

    # ── 2. 计算子代理工具集 ──
    child_toolsets = _resolve_child_toolsets(
        requested_toolsets=toolsets,
        parent_enabled=parent_enabled,
        parent_available_toolsets=parent_available_toolsets,
    )
    if not child_toolsets:
        raise ValueError("No delegate toolsets are available under the parent agent's current tool permissions.")

    # ── 3. 获取工具定义并过滤被禁止的工具 ──
    parent_config = getattr(parent_agent, "config", {}) if parent_agent is not None else {}
    all_tool_defs, all_tool_names = get_tool_definitions(
        enabled_toolsets=child_toolsets,
        config=parent_config if isinstance(parent_config, dict) else None,
    )

    # 工具名称级别过滤（最终安全防线）
    safe_tool_names = _filter_blocked_tools(list(all_tool_names))
    safe_tool_defs = [t for t in all_tool_defs if t["function"]["name"] in safe_tool_names]

    # ── 4. 预创建 session_id 与 runtime 工作目录（prompt 构建前需要 workspace）──
    child_session_id = f"delegate_{getattr(parent_agent, 'session_id', 'unknown')}_{task_index}_{uuid.uuid4().hex[:6]}"
    from mclaw.runtime.manager import RuntimeManager

    runtime = RuntimeManager.current(parent_config if isinstance(parent_config, dict) else None)
    child_runtime_workspace = runtime.paths.session_root(child_session_id)
    child_runtime_workspace.mkdir(parents=True, exist_ok=True)
    delegation_root = runtime.paths.delegation_root()
    child_delegation_dir = delegation_root / child_session_id
    child_delegation_dir.mkdir(parents=True, exist_ok=True)

    # ── 4.5 复制外部文件到 workspace 并改写 goal/context ──
    prepared_goal, prepared_context, workspace_path, workspace_preparation = _prepare_delegation_workspace(
        goal, context, child_delegation_dir
    )

    # ── 5. 构建子代理提示词 ──
    child_prompt = _build_child_system_prompt(
        prepared_goal, prepared_context, workspace_path=workspace_path, max_iterations=max_iterations
    )

    # ── 6. 获取父代理认证信息 ──
    parent_api_key = getattr(parent_agent, "api_key", None) or ""
    parent_base_url = getattr(parent_agent, "base_url", None) or ""
    parent_model = getattr(parent_agent, "model", None) or ""
    parent_provider = getattr(parent_agent, "provider", None) or ""
    parent_api_mode = getattr(parent_agent, "api_mode", None) or "chat_completions"
    delegation_cfg = parent_config.get("delegation", {}) if isinstance(parent_config, dict) else {}

    child_model = delegation_cfg.get("model") or parent_model
    child_api_key = parent_api_key
    child_base_url = parent_base_url
    child_provider = parent_provider
    child_api_mode = parent_api_mode

    has_delegation_auth_override = any(
        delegation_cfg.get(k) for k in ("provider", "base_url")
    )
    if has_delegation_auth_override:
        try:
            from mclaw.cli.auth import resolve_provider
            resolved = resolve_provider(
                model=child_model,
                provider=delegation_cfg.get("provider") or "",
                base_url=delegation_cfg.get("base_url") or "",
                api_key="",
                config=parent_config if isinstance(parent_config, dict) else None,
            )
            child_model = resolved.get("model") or child_model
            child_api_key = resolved.get("api_key") or child_api_key
            child_base_url = resolved.get("base_url") or child_base_url
            child_provider = resolved.get("provider") or child_provider
            child_api_mode = resolved.get("api_mode") or child_api_mode
        except Exception as exc:
            logger.warning(
                "[subagent-%d] delegation provider override failed, using parent credentials: %s",
                task_index,
                exc,
            )

    # ── 7. 创建子代理实例 ──
    child = MClaw(
        model=child_model,
        api_key=child_api_key,
        base_url=child_base_url,
        api_mode=child_api_mode,
        provider=child_provider,
        system_prompt=child_prompt,          # 直接注入轻量提示词
        skip_memory=True,                     # 禁用记忆系统
        skip_context_files=True,              # 不加载 SOUL.md
        skip_skills=True,                     # 不注册 skills 工具
        enabled_toolsets=child_toolsets,      # 受限工具集
        max_iterations=max_iterations,
        session_db=getattr(parent_agent, "_session_db", None),
        session_id=child_session_id,
        parent_session_id=getattr(parent_agent, "session_id", None),
        workspace=getattr(parent_agent, "workspace_path", None),
        config=parent_config,
    )

    # ── 7.5 复用父代理的 context compressor 配置，避免子代理重复查询网络（models.dev 等）
    # 子代理使用与父代理相同的模型，因此 context_length 完全一致。
    parent_compressor = getattr(parent_agent, "context_compressor", None)
    if parent_compressor and child.context_compressor:
        child.context_compressor.context_length = parent_compressor.context_length
        child.context_compressor.threshold_tokens = parent_compressor.threshold_tokens
        child.context_compressor.last_prompt_tokens = parent_compressor.last_prompt_tokens
        child.context_compressor.last_completion_tokens = parent_compressor.last_completion_tokens

    # ── 8. 手动设置过滤后的工具定义（不走 _discover_tools） ──
    child.tools = safe_tool_defs
    child.valid_tool_names = set(safe_tool_names)

    # ── 9. 设置代理深度 ──
    parent_depth = getattr(parent_agent, "_delegate_depth", 0)
    child._delegate_depth = parent_depth + 1

    # ── 10. 挂载 runtime 工作目录与 delegation 目录 ──
    child._runtime_workspace_dir = child_runtime_workspace
    child._delegation_dir = child_delegation_dir
    child._prepared_goal = prepared_goal
    child._workspace_preparation = workspace_preparation

    # ── 11. 继承父代理的回调函数 ──
    child._print_fn = getattr(parent_agent, "_print_fn", print)
    child._stream_callback = getattr(parent_agent, "_stream_callback", None)
    child._tool_callback = getattr(parent_agent, "_tool_callback", None)
    child._status_callback = getattr(parent_agent, "_status_callback", None)

    logger.info(
        "[subagent-%d] 已构建, runtime_workspace=%s, delegation=%s, tools=%s, depth=%d",
        task_index, str(child_runtime_workspace), str(child_delegation_dir),
        list(safe_tool_names), child._delegate_depth
    )

    return child


# ── 子代理运行 ────────────────────────────────────────────────────────────

def _run_single_child(
    task_index: int,
    goal: str,
    child: "MClaw",
    parent_agent,
    progress_callback: ProgressCallback = None,
) -> Dict[str, Any]:
    """在线程中运行子代理并收集结果，支持进度回调"""
    child_start = time.monotonic()

    logger.info(
        "[subagent-%d] 启动, depth=%d, goal=%.50s",
        task_index, child._delegate_depth, goal
    )

    # 设置进度回调：relay 子代理的工具调用到 progress_callback
    # 注意：不调用父代理的 _tool_callback，避免子代理的工具调用显示在父 TUI
    if progress_callback:
        def _relay_tool(tool_name: str, args: dict):
            progress_callback(SubtaskEvent(
                task_index, SUBAGENT_TOOL_CALL,
                {"tool": tool_name, "args_bytes": len(str(args))}
            ))

        child._tool_callback = _relay_tool

        # 发送 started 事件
        progress_callback(SubtaskEvent(
            task_index, SUBAGENT_STARTED,
            {"goal": goal[:100], "depth": child._delegate_depth}
        ))

    try:
        # 运行子代理（无父对话历史，conversation_history=None）
        logger.info("[subagent-%d] entering run_conversation", task_index)
        effective_goal = getattr(child, "_prepared_goal", None) or goal
        result = child.run_conversation(user_message=effective_goal)
        logger.info("[subagent-%d] run_conversation returned", task_index)

        duration = round(time.monotonic() - child_start, 2)

        summary = result.get("final_response") or ""
        completed = result.get("completed", False)
        interrupted = result.get("interrupted", False)
        api_calls = result.get("api_calls", 0)

        if interrupted:
            status = "interrupted"
            exit_reason = "interrupted"
        elif summary:
            status = "completed"
            exit_reason = "completed" if completed else "max_iterations"
        else:
            status = "failed"
            exit_reason = "max_iterations"

        logger.info(
            "[subagent-%d] 完成, duration=%.2fs, status=%s, exit=%s",
            task_index, duration, status, exit_reason
        )

        # 获取 token 统计
        input_tokens = getattr(child, "session_input_tokens", 0) or 0
        output_tokens = getattr(child, "session_output_tokens", 0) or 0

        # 截断过长的 summary
        if len(summary) > MAX_SUMMARY_CHARS:
            summary = summary[:MAX_SUMMARY_CHARS] + "……[内容已截断]"

        entry: Dict[str, Any] = {
            "task_index": task_index,
            "goal": goal,
            "status": status,
            "summary": summary,
            "api_calls": api_calls,
            "duration_seconds": duration,
            "model": child.model if isinstance(child.model, str) else None,
            "exit_reason": exit_reason,
            "tokens": {
                "input": input_tokens if isinstance(input_tokens, (int, float)) else 0,
                "output": output_tokens if isinstance(output_tokens, (int, float)) else 0,
            },
            "tool_trace": [],
        }
        workspace_preparation = _workspace_preparation_for_child(child)
        if workspace_preparation:
            entry["workspace_preparation"] = workspace_preparation

        if status == "failed":
            entry["error"] = result.get("error", "子代理未产生响应")

        # 发送 completed 事件
        if progress_callback:
            progress_callback(SubtaskEvent(
                task_index, SUBAGENT_COMPLETED,
                {"status": status, "duration": duration, "summary": summary, "api_calls": api_calls}
            ))

        return entry

    except Exception as exc:
        duration = round(time.monotonic() - child_start, 2)
        logger.error("[subagent-%d] 异常: %s", task_index, exc)

        # 发送 error 事件
        if progress_callback:
            progress_callback(SubtaskEvent(
                task_index, SUBAGENT_ERROR,
                {"error": str(exc)}
            ))

        return {
            "task_index": task_index,
            "goal": goal,
            "status": "error",
            "summary": None,
            "error": str(exc),
            "api_calls": 0,
            "duration_seconds": duration,
        }


# ── 后台运行 ──────────────────────────────────────────────────────────────

# 子代理硬性墙钟超时，单位秒；即使 HTTP timeout 为 120s，也要防止
# 单个卡死任务无限阻塞父代理。单个子任务可能多次调用 API。
# 浏览器重任务需要更长时间：导航、快照、滚动和 API 延迟都会累积。
_SUBAGENT_MAX_WALL_TIME = 300  # 5 minutes


def _run_all_children_background(
    task_list: list,
    children: list,
    parent_agent,
    progress_callback: ProgressCallback,
    task_id: str,
    start_time: float,
) -> None:
    """在 daemon 线程中运行所有子代理（并行 + 单个超时），完成后放入队列"""
    results: List[Dict[str, Any]] = []
    goal_map = {i: task["goal"] for i, task, _child in children}

    # 注意：不使用 `with ThreadPoolExecutor(...)`，因为它会在 __exit__ 调用
    # shutdown(wait=True)。当 worker 线程卡在 API 调用时，这会阻塞 daemon
    # 线程 indefinitely，导致 _subagent_results.put() 永远不会执行。
    executor = ThreadPoolExecutor(max_workers=min(len(children), MAX_CONCURRENT_CHILDREN))
    try:
        futures = {}
        for i, task, child in children:
            fut = executor.submit(
                _run_single_child,
                task_index=i,
                goal=task["goal"],
                child=child,
                parent_agent=parent_agent,
                progress_callback=progress_callback,
            )
            futures[fut] = i

        # 并行等待所有子代理，全局硬超时。用 wait(FIRST_COMPLETED) 循环
        # 避免顺序遍历 futures 导致的总超时 = num_children * timeout。
        from concurrent.futures import wait, FIRST_COMPLETED

        pending = set(futures.keys())
        deadline = time.time() + _SUBAGENT_MAX_WALL_TIME

        while pending:
            remaining = max(0.0, deadline - time.time())
            if remaining <= 0:
                break
            done, pending = wait(pending, timeout=remaining, return_when=FIRST_COMPLETED)
            for future in done:
                idx = futures[future]
                try:
                    entry = future.result()
                except Exception as exc:
                    logger.error("[subagent-%d] 执行异常或超时: %s", idx, exc)
                    entry = {
                        "task_index": idx,
                        "goal": goal_map.get(idx, ""),
                        "status": "error",
                        "summary": None,
                        "error": str(exc),
                        "api_calls": 0,
                        "duration_seconds": _SUBAGENT_MAX_WALL_TIME,
                    }
                    if progress_callback:
                        progress_callback(SubtaskEvent(
                            idx, SUBAGENT_ERROR,
                            {"error": str(exc)}
                        ))
                results.append(entry)

        # 标记剩余未完成的为超时
        for future in pending:
            idx = futures[future]
            future.cancel()
            logger.error("[subagent-%d] 执行异常或超时: %s", idx, "wall-clock timeout")
            entry = {
                "task_index": idx,
                "goal": goal_map.get(idx, ""),
                "status": "error",
                "summary": None,
                "error": "子代理执行超时（5分钟）",
                "api_calls": 0,
                "duration_seconds": _SUBAGENT_MAX_WALL_TIME,
            }
            child = next((c for i, _task, c in children if i == idx), None)
            workspace_preparation = _workspace_preparation_for_child(child)
            if workspace_preparation:
                entry["workspace_preparation"] = workspace_preparation
            if progress_callback:
                progress_callback(SubtaskEvent(
                    idx, SUBAGENT_ERROR,
                    {"error": "子代理执行超时（5分钟）"}
                ))
            results.append(entry)
    finally:
        # shutdown(wait=False) 立即返回，不阻塞 daemon 线程等待 worker 完成。
        # 已经卡住的工作线程会继续在后台运行直到 API 超时或完成，但不会影响
        # 主流程。
        executor.shutdown(wait=False)

    results.sort(key=lambda r: r["task_index"])
    _subagent_results.put({
        "task_id": task_id,
        "results": results,
        "total_duration_seconds": round(time.time() - start_time, 2),
    })
    logger.info(
        "delegate_task result queued task_id=%s results=%d duration=%.2fs",
        task_id,
        len(results),
        time.time() - start_time,
    )


def get_pending_results(timeout: float = 0.05) -> Optional[Dict]:
    """从队列中非阻塞获取子代理结果（供 TUI 调用）"""
    try:
        return _subagent_results.get(timeout=timeout)
    except Empty:
        return None


def get_pending_result_for_task(task_id: str, timeout: float = 0.05) -> Optional[Dict]:
    """获取指定 task_id 的子代理结果，不匹配的结果会暂存而不是丢弃。"""
    if not task_id:
        return get_pending_results(timeout=timeout)

    with _subagent_result_stash_lock:
        stashed = _subagent_result_stash.pop(task_id, None)
        if stashed is not None:
            logger.info("delegate_task result loaded from stash task_id=%s", task_id)
            return stashed

    deadline = time.monotonic() + max(0.0, timeout)
    unmatched: list[Dict[str, Any]] = []
    result: Optional[Dict[str, Any]] = None

    while True:
        remaining = max(0.0, deadline - time.monotonic())
        try:
            item = _subagent_results.get(timeout=remaining)
        except Empty:
            break

        item_task_id = item.get("task_id") if isinstance(item, dict) else None
        if item_task_id == task_id:
            result = item
            break
        if isinstance(item, dict) and item_task_id:
            unmatched.append(item)
            logger.info(
                "delegate_task result stashed while waiting for task_id=%s got=%s",
                task_id,
                item_task_id,
            )
        if time.monotonic() >= deadline:
            break

    if unmatched:
        with _subagent_result_stash_lock:
            for item in unmatched:
                item_task_id = item.get("task_id")
                if item_task_id:
                    _subagent_result_stash[item_task_id] = item

    return result


# ── 主入口函数 ────────────────────────────────────────────────────────────

def delegate_task(
    tasks: Optional[List[Dict[str, Any]]] = None,
    parent_agent=None,
) -> str:
    """Spawn 一个或多个子代理处理委托任务

    模型侧只支持 tasks 数组；单任务也使用 tasks=[{...}]。
    tasks 最多 5 个并行。

    返回 JSON 格式结果数组。
    """
    # ── 1. 验证 parent_agent ──
    if parent_agent is None:
        return tool_error("delegate_task 需要 parent_agent 上下文")

    # ── 2. 深度检查 ──
    parent_depth = getattr(parent_agent, "_delegate_depth", 0)
    if parent_depth >= MAX_DELEGATE_DEPTH:
        logger.warning(
            "delegate_task 深度超限: %d >= %d",
            parent_depth, MAX_DELEGATE_DEPTH
        )
        return json.dumps({
            "error": (
                f"代理深度已达上限（{MAX_DELEGATE_DEPTH}）。"
                "子代理不能创建进一步的子代理。"
            ),
            "success": False,
        }, ensure_ascii=False)

    # ── 3. 规范化参数 ──
    parent_config = getattr(parent_agent, "config", {}) if parent_agent is not None else {}
    delegation_cfg = parent_config.get("delegation", {}) if isinstance(parent_config, dict) else {}
    configured_max_iter = delegation_cfg.get("max_iterations", DEFAULT_MAX_ITERATIONS)
    try:
        configured_max_iter = int(configured_max_iter)
    except (TypeError, ValueError):
        configured_max_iter = DEFAULT_MAX_ITERATIONS
    effective_max_iter = configured_max_iter

    # ── 4. 解析任务列表 ──
    if not isinstance(tasks, list):
        return tool_error("delegate_task 需要 tasks 数组；单任务也使用 tasks=[{goal, context?, toolsets?}]")
    if len(tasks) > MAX_CONCURRENT_CHILDREN:
        return tool_error(
            f"tasks 最多支持 {MAX_CONCURRENT_CHILDREN} 个任务，当前收到 {len(tasks)} 个"
        )
    task_list = tasks

    if not task_list:
        return tool_error("任务列表为空")

    # ── 5. 验证每个任务都有 goal ──
    for i, task in enumerate(task_list):
        if not isinstance(task, dict):
            return tool_error(f"任务 {i} 必须是对象")
        goal_text = str(task.get("goal") or "").strip()
        if not goal_text:
            return tool_error(f"任务 {i} 缺少 goal")
        task["goal"] = goal_text
        if "toolsets" in task and task.get("toolsets") is not None and not isinstance(task.get("toolsets"), list):
            return tool_error(f"任务 {i} 的 toolsets 必须是字符串数组")

    logger.info("delegate_task 处理 %d 个任务", len(task_list))

    # ── 6. 构建所有子代理（在主线程上，线程安全） ──
    children = []
    try:
        for i, task in enumerate(task_list):
            child = _build_child_agent(
                task_index=i,
                goal=task["goal"],
                context=task.get("context"),
                toolsets=task.get("toolsets"),
                max_iterations=effective_max_iter,
                parent_agent=parent_agent,
            )
            children.append((i, task, child))
    except Exception as exc:
        logger.exception("构建子代理失败")
        return json.dumps({
            "error": f"构建子代理失败: {exc}",
            "success": False,
        }, ensure_ascii=False)

    # ── 7. 获取进度回调（非阻塞模式标志）──
    progress_callback: Optional[ProgressCallback] = getattr(
        parent_agent, "_delegate_progress_callback", None
    )

    # ── 8. 运行子代理 ──
    start_time = time.time()
    task_id = str(uuid.uuid4())[:8]

    if progress_callback is not None:
        # 非阻塞模式：启动 daemon 线程，立即返回
        thread = threading.Thread(
            target=_run_all_children_background,
            args=(task_list, children, parent_agent, progress_callback, task_id, start_time),
            daemon=True,
        )
        thread.start()

        task_info = {
            "task_id": task_id,
            "num_tasks": len(task_list),
            "goals": [t["goal"][:80] for t in task_list],
            "workspace_preparation": [
                _workspace_preparation_summary(child)
                for _i, _task, child in children
            ],
        }

        return json.dumps({
            "pending": True,
            "task_id": task_id,
            "num_tasks": len(task_list),
            "task_info": task_info,
            "success": True,
        }, ensure_ascii=False)

    # Synchronous mode when the caller did not request progress callbacks.
    overall_start = time.monotonic()
    results = []
    goal_map = {i: task["goal"] for i, task, _child in children}

    if len(children) == 1:
        # 单任务：直接运行（无线程池开销）
        _, _, child = children[0]
        result = _run_single_child(0, children[0][1]["goal"], child, parent_agent)
        results.append(result)
    else:
        # 批量：并发运行（带 wall-clock 超时）
        with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_CHILDREN) as executor:
            futures = {}
            for i, task, child in children:
                future = executor.submit(
                    _run_single_child,
                    task_index=i,
                    goal=task["goal"],
                    child=child,
                    parent_agent=parent_agent,
                )
                futures[future] = i

            # 直接用 future.result(timeout) 等待，不用 as_completed，
            # 避免 worker 线程永远挂住时 as_completed 无限阻塞。
            for future, idx in futures.items():
                try:
                    entry = future.result(timeout=_SUBAGENT_MAX_WALL_TIME)
                except Exception as exc:
                    logger.error("[subagent-%d] 执行异常或超时: %s", idx, exc)
                    entry = {
                        "task_index": idx,
                        "goal": goal_map.get(idx, ""),
                        "status": "error",
                        "summary": None,
                        "error": str(exc),
                        "api_calls": 0,
                        "duration_seconds": _SUBAGENT_MAX_WALL_TIME,
                    }
                results.append(entry)

        # 按 task_index 排序，保证结果顺序与输入一致
        results.sort(key=lambda r: r["task_index"])

    total_duration = round(time.monotonic() - overall_start, 2)

    logger.info(
        "delegate_task 完成, %d 个任务, 总耗时=%.2fs",
        len(results), total_duration
    )

    return json.dumps({
        "results": results,
        "total_duration_seconds": total_duration,
        "success": True,
        "workspace_preparation": [
            _workspace_preparation_for_child(child)
            for _i, _task, child in children
        ],
    }, ensure_ascii=False)


# ── Schema ────────────────────────────────────────────────────────────────

DELEGATE_TASK_SCHEMA = {
    "type": "function",
    "function": {
        "name": "delegate_task",
        "description": (
            "Spawn up to 5 isolated subagents for independent subtasks. "
            "Always pass a tasks array; use one task object for a single subtask. "
            "Each task must be self-contained because subagents do not inherit conversation history. "
            "Subagents default to terminal+file and may only add allowed extra toolsets."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "tasks": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_CONCURRENT_CHILDREN,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "goal": {
                                "type": "string",
                                "description": (
                                    "Self-contained objective and completion criteria for one subagent."
                                ),
                            },
                            "context": {
                                "type": "string",
                                "description": (
                                    "Only necessary background: absolute file paths, errors, constraints, "
                                    "expected output, and relevant facts. The subagent sees no parent history."
                                ),
                            },
                            "toolsets": {
                                "type": "array",
                                "uniqueItems": True,
                                "items": {
                                    "type": "string",
                                    "enum": [
                                        "terminal",
                                        "file",
                                        "web",
                                        "vision",
                                        "browser",
                                        "delegation_skill_read",
                                    ],
                                },
                                "description": (
                                    "Omit by default. Include only extra capabilities this task needs. "
                                    "Default terminal+file is added automatically. Do not pass memory, "
                                    "session_search, delegate_task, skill_manage, skill_search, all, or unknown toolsets."
                                ),
                            },
                        },
                        "required": ["goal"],
                    },
                    "description": (
                        f"One to {MAX_CONCURRENT_CHILDREN} independent tasks. "
                        "Use length 1 for a single delegated task."
                    ),
                },
            },
            "required": ["tasks"],
        },
    },
}


# ── 注册 ────────────────────────────────────────────────────────────────

registry.register(
    name="delegate_task",
    toolset="delegation",
    schema=DELEGATE_TASK_SCHEMA,
    handler=lambda args, **kw: delegate_task(
        tasks=args.get("tasks"),
        parent_agent=kw.get("parent_agent"),
    ),
    description="委托子代理处理任务（隔离上下文）",
    emoji="🔀",
)
