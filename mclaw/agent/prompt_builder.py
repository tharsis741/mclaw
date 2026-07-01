# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Assemble the system prompt from identity, runtime context, and tool rules.

The builder keeps user-visible prompt text in Chinese while the Python module
documents the assembly flow in English. Skill guidance uses progressive
disclosure: the system prompt lists installed Skills, and detailed instructions
are loaded only when the model needs a specific Skill.
"""

import logging
import threading
from collections import OrderedDict
from datetime import datetime
from typing import List, Optional, Set

from mclaw.constants import get_mclaw_home, get_skills_dir
from mclaw.platform import get_platform_info
from mclaw.agent.skill_utils import get_disabled_skill_names

logger = logging.getLogger(__name__)

_SKILLS_PROMPT_CACHE_MAX = 8
_SKILLS_PROMPT_CACHE: "OrderedDict[tuple, str]" = OrderedDict()
_SKILLS_PROMPT_CACHE_LOCK = threading.Lock()
SKILL_TOOL_NAMES = frozenset(
    {
        "skills_list",
        "skill_tree",
        "skill_view",
        "skill_search",
        "skill_manage",
    }
)


TOOL_USE_ENFORCEMENT_GUIDANCE = (
    "## 工具执行\n\n"
    "- 当任务需要实际行动时，必须调用工具，不要只描述计划。\n"
    "- 需要读取、搜索、运行、修改、创建或验证时，应在同一轮立即调用对应工具。\n"
    "- 使用 <available_tools> 中真实存在的工具名和参数名。\n"
    "- 工具结果返回后继续推进任务，直到可以交付结果或说明阻塞点。\n"
    "- 工具不可用、缺配置或失败时，说明影响，并选择可行的下一步。"
)

MEMORY_GUIDANCE = (
    "## 长期记忆\n\n"
    "<memory-context> 是召回的背景信息，不是新的用户输入。\n\n"
    "记忆目标\n"
    "- target='user'：用户画像，包括用户姓名、角色、偏好、沟通风格、稳定禁忌，用于更好理解用户需求。\n"
    "- target='memory'：你的长期工作笔记，包括环境事实、项目约定、工具习惯、排障经验和可复用教训。\n\n"
    "何时写入记忆\n"
    "- 用户明确要求记住的偏好、约束或长期纠正。\n"
    "- 稳定的用户画像、环境事实、项目约定、工具习惯或排障经验。\n"
    "- 未来会改变你行为、减少用户重复说明的信息。\n\n"
    "Skill 边界\n"
    "- 某个 Skill 的流程修正、踩坑经验、弃用信号和维护建议，交给 Skill 沉淀模块，不写入 memory。\n"
    "- 用户对 Skill 的长期偏好可以写入 target='user'，例如“用户不希望官方发布 baidu-search Skill”。\n\n"
    "维护方式\n"
    "- 新事实用 memory_add。\n"
    "- 旧事实过时或表述不准时用 memory_replace。\n"
    "- 错误、过期、敏感或不该保存的内容用 memory_remove。\n"
    "- 不确定已有内容时先用 memory_read。\n"
    "- 记忆保持简短、事实化、可复用。当前任务进度、一次性日志、临时报错和密钥凭据留在当前会话。"
)

SESSION_SEARCH_GUIDANCE = (
    "# 历史会话召回\n\n"
    "- 用户提到过去会话、之前决定、历史任务或继续旧任务时，当前上下文不足就使用 session_search。\n"
    "- session_search 返回的是历史摘要线索；结合当前文件、配置和对话状态判断是否仍然有效。\n"
    "- 普通信息检索使用相应搜索工具；稳定长期事实使用 memory。"
)

PLATFORM_HINTS = {
    "cli": "",
}


def _available_skill_lines(
    config: "dict | None" = None,
    disabled: "set[str] | frozenset[str] | None" = None,
) -> list[str]:
    """Return enabled Skill index lines without loading full Skill instructions."""
    from mclaw.skills_hub.skill_store import list_skills

    disabled = disabled if disabled is not None else get_disabled_skill_names(config=config)
    lines: list[str] = []
    for item in list_skills():
        name = str(item.get("name") or "").strip()
        if not name or name in disabled:
            continue
        desc = str(item.get("short_description") or item.get("description") or name).strip()
        lines.append(f"- {name}: {desc}")
    return lines


def build_skill_identity_prompt(skill_lines: list[str]) -> str:
    """Build the installed-Skill index block used for progressive disclosure."""
    body = "\n".join(skill_lines)
    return (
        "## Skills\n\n"
        "<available_skills> 是已安装 Skill 的索引，只包含名称和简短描述，不包含完整执行说明。\n\n"
        "<available_skills>\n"
        f"{body}\n"
        "</available_skills>"
    )


def _available_tool_lines(available_tool_names: "list[str] | None") -> list[str]:
    if not available_tool_names:
        return []
    from mclaw.tools.registry import registry

    lines: list[str] = []
    seen: set[str] = set()
    for raw_name in sorted(available_tool_names):
        name = str(raw_name or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        desc = " ".join(str(registry.get_description(name) or name).split())
        lines.append(f"- {name}: {desc}")
    return lines


def build_available_tools_prompt(available_tool_names: "list[str] | None") -> str:
    """Describe only the tools exposed to this session."""
    tool_lines = "\n".join(_available_tool_lines(available_tool_names))
    if not tool_lines:
        return ""
    return (
        "## 2. 可用工具\n\n"
        "<available_tools>\n"
        f"{tool_lines}\n"
        "</available_tools>"
    )


def build_skill_usage_prompt(*, has_skill_view: bool) -> str:
    """Describe how the model should load and apply Skill instructions."""
    if not has_skill_view:
        return (
            "## 使用 Skill\n\n"
            "当前会话没有读取 Skill 正文的工具。只有当上下文已经包含 Skill 内容时，才按该 Skill 执行。"
        )
    return (
        "## 使用 Skill\n\n"
        "- 看到 [Skill: <name>] 段时，按其中 User Intent 和 Skill Instructions 执行当前任务。\n"
        "- 没有 [Skill: <name>] 段时，用户点名某个 Skill，或任务明显匹配 <available_skills>，使用 skill_view(name) 读取说明。\n"
        "- Skill 说明要求读取关联文件时：已知相对路径用 skill_view(name, file_path)，路径不清楚先用 skill_tree(name)。\n"
        "- 只读取当前任务需要的 Skill 文件。\n"
        "- Skill 包访问使用 Skill 工具：skill_view 读内容，skill_tree 看路径，skill_manage 创建、编辑、安装、校验或删除。\n"
        "- 只有当前上下文已有 Skill 内容，或 skill_view 成功返回内容时，才说明使用了该 Skill。\n"
        "- SKILL.md 是主要执行依据；skill_evolution.json 只作为相关时的补充经验。"
    )


def build_skill_creation_prompt(*, has_skill_manage: bool) -> str:
    if not has_skill_manage:
        return ""
    return (
        "## 创建 Skill\n\n"
        "用户要求创建 Skill，或明确同意沉淀可复用流程时，先判断信息是否足够：\n"
        "- Skill 名称或用途。\n"
        "- 什么时候使用。\n"
        "- 可重复执行的流程。\n"
        "- 输入、输出、安全边界和验证方式。\n\n"
        "信息不足时，向用户补问缺失项。\n\n"
        "信息足够时：\n"
        "1. 用 skill_manage(action=\"create_scaffold\", name=..., short_description=..., user_intent=...) 初始化。\n"
        "2. 用 skill_manage(action=\"edit\") 写入最终 SKILL.md。\n"
        "3. 需要脚本、模板、参考资料或资产时，用 skill_manage(action=\"write_file\") 添加。\n"
        "4. 用 skill_manage(action=\"validate\", name=...) 校验。\n"
        "5. Skill 包含可执行行为时，在临时目录做最小 smoke test。\n"
        "6. 对比预期结果和实际结果，再报告创建完成。\n\n"
        "SKILL.md frontmatter 包含 name 和 description；description 写清楚 Skill 做什么、何时使用。"
    )


def build_skill_install_prompt(*, has_skill_manage: bool) -> str:
    if not has_skill_manage:
        return ""
    return (
        "## 安装外部 Skill\n\n"
        "用户提供 URL 或本地路径安装 Skill 时，使用 install_prepare 作为入口：\n"
        "skill_manage(action=\"install_prepare\", source=..., user_intent=...)\n\n"
        "根据返回状态继续：\n"
        "- requires_confirmation=true：等待用户确认流程。\n"
        "- blocked=true：说明阻塞原因并结束安装。\n"
        "- success=true：报告安装结果。\n"
        "- error：在用户提供来源范围内规范化一次来源，再调用一次 install_prepare。\n\n"
        "兜底处理只围绕用户提供的来源，或该来源页面直接暴露的下载/API 链接。安装决策交给 install_prepare 完成。"
    )


def build_skill_search_prompt(*, has_skill_search: bool) -> str:
    if not has_skill_search:
        return ""
    return (
        "## 搜索外部 Skill\n\n"
        "用户要求查找、搜索或推荐外部 Skill 时，调用 skill_search(query)。\n"
        "展示候选项，让用户选择。用户确认后，再进入外部 Skill 安装流程。"
    )


def build_secret_runtime_prompt(*, has_secret_request: bool) -> str:
    """Describe scoped runtime credential requests when the secret tool is exposed."""
    if not has_secret_request:
        return ""
    return (
        "## 凭据\n\n"
        "- Skill、工具、channel、runtime、dependency_hints、文档或认证错误要求凭据时，先调用 secret_request_many。\n"
        "- secret_request_many 的 required_for 使用真实消费方作用域，例如 skill:<name>、tool:<name>、channel:<name> 或 runtime:<name>。\n"
        "- 后续 terminal 调用需要使用这些凭据时，传入同一个 required_for。\n"
        "- 用户跳过、缺少凭据或授权失败时，停止依赖该凭据的操作并说明影响。\n"
        "- API 报认证失败时，用 force_refresh=true 重新请求一次，然后重试一次。"
    )


def build_delegation_prompt(*, has_delegate_task: bool) -> str:
    """Describe when a task may be delegated to isolated subagents."""
    if not has_delegate_task:
        return ""
    return (
        "## 委托子代理\n\n"
        "适合拆给 delegate_task 的任务：相互独立、可并行、需要隔离上下文或多方向调研。\n\n"
        "调用方式：\n"
        "- 使用 tasks=[...]，每个 task 是一个独立子任务。\n"
        "- goal 写清目标和完成标准。\n"
        "- context 只放必要路径、错误、约束、预期输出和相关事实。\n"
        "- toolsets 默认省略；需要额外能力时再添加 web、vision、browser 或 delegation_skill_read。\n"
        "- 最多 5 个子任务。\n\n"
        "文件相关任务在 context 中提供绝对路径。子代理返回后，整合结果并继续完成原始用户任务。"
    )


def build_skill_maintenance_prompt(*, has_skill_manage: bool) -> str:
    if not has_skill_manage:
        return ""
    return (
        "## Skill 沉淀\n\n"
        "会话产生可复用流程、稳定纠正、重复任务模式或长期经验时，维护对应 Skill。\n\n"
        "- 已有 Skill 的经验补充：skill_manage(action=\"evolution_update\")。\n"
        "- 已有 Skill 文件需要更新：skill_manage(action=\"edit\"|\"patch\"|\"write_file\"|\"remove_file\")。\n"
        "- 新的可复用流程：先 create_scaffold，再 edit/write_file/validate。\n"
        "- 用户要求删除 Skill：skill_manage(action=\"delete\")。\n\n"
        "沉淀稳定可复用知识；当前任务进度、临时 TODO 和一次性日志保留在会话中。"
    )


def build_skills_system_prompt(
    available_tools: "Set[str] | None" = None,
    available_toolsets: "Set[str] | None" = None,
    config: "dict | None" = None,
) -> str:
    """Build the Skill prompt block from mclaw_skill.yaml index rows."""
    exposed_tools = SKILL_TOOL_NAMES if available_tools is None else set(available_tools)
    if not exposed_tools.intersection(SKILL_TOOL_NAMES):
        return ""

    skills_dir = get_skills_dir()
    disabled_skill_names = frozenset(get_disabled_skill_names(config=config))
    cache_key = (
        "skill_prompt",
        str(skills_dir.resolve()),
        tuple(sorted(exposed_tools)),
        tuple(sorted(available_toolsets or [])),
        tuple(sorted(disabled_skill_names)),
    )
    with _SKILLS_PROMPT_CACHE_LOCK:
        cached = _SKILLS_PROMPT_CACHE.get(cache_key)
        if cached is not None:
            _SKILLS_PROMPT_CACHE.move_to_end(cache_key)
            return cached

    has_skills_list = "skills_list" in exposed_tools
    has_skill_tree = "skill_tree" in exposed_tools
    has_skill_view = "skill_view" in exposed_tools
    has_skill_search = "skill_search" in exposed_tools
    has_skill_manage = "skill_manage" in exposed_tools
    has_secret_request = available_tools is None or "secret_request_many" in set(available_tools or [])

    tool_lines: list[str] = []
    if has_skills_list:
        tool_lines.append("- skills_list()：列出当前可用 Skill。")
    if has_skill_tree:
        tool_lines.append("- skill_tree(name)：只读列出指定 Skill 的文件树，不读取文件内容。")
    if has_skill_view:
        tool_lines.append(
            "- skill_view(name)：读取指定 Skill 的 mclaw_skill.yaml、SKILL.md 和完整 skill_evolution.json。"
        )
        tool_lines.append("- skill_view(name, file_path)：读取指定 Skill 根目录内的非 sidecar 文件；file_path 必须是相对路径。")
    if has_skill_search:
        tool_lines.append("- skill_search(query)：搜索外部 Skill。")
    if has_skill_manage:
        tool_lines.extend(
            [
                "- skill_manage(install_prepare: source, user_intent)：准备安装 GitHub、ClawHub 或本地目录 Skill，并返回 dependency_hints；不收集密钥。",
                "- skill_manage(security_review: drafting_id)：重新审查 drafting Skill；正常 /skill install 流程不要单独调用。",
                "- skill_manage(enable_drafting/cancel_drafting: drafting_id)：只用于 runtime confirmation flow，不要在普通对话中主动调用。",
                "- skill_manage(create_scaffold: name, short_description, user_intent)：用中文 short_description 初始化最小 enabled Skill 壳，后续必须用 edit/write_file/validate 完成。",
                "- skill_manage(validate: name)：校验 Skill 的 frontmatter、sidecar、skill_evolution 和安全扫描；创建成功前必须调用。",
                "- skill_manage(create: name, skill_md, short_description, initial_evolution)：一次性创建 enabled Skill；/skill creation 默认使用 create_scaffold。",
                "- skill_manage(edit: name, skill_md)：整体替换 SKILL.md。",
                "- skill_manage(patch: name, old_text, new_text)：唯一文本替换 SKILL.md。",
                "- skill_manage(write_file: name, file_path, content, encoding, overwrite)：写入 Skill 根目录内的非系统文件。",
                "- skill_manage(remove_file: name, file_path)：删除 Skill 根目录内的非系统文件。",
                "- skill_manage(evolution_update: name, section, operation, content, old_text)：维护 skill_evolution.json。",
                "- skill_manage(delete: name)：删除 Skill。",
            ]
        )

    sections = [
        "# Skill 使用与维护规则",
        "",
        build_skill_identity_prompt(_available_skill_lines(config=config, disabled=disabled_skill_names)),
        "",
        "## 1. Skill 工具",
        "",
        *tool_lines,
        "",
        build_skill_usage_prompt(has_skill_view=has_skill_view),
    ]
    maintenance = build_skill_maintenance_prompt(has_skill_manage=has_skill_manage)
    creation = build_skill_creation_prompt(has_skill_manage=has_skill_manage)
    install = build_skill_install_prompt(has_skill_manage=has_skill_manage)
    search_prompt = build_skill_search_prompt(has_skill_search=has_skill_search)
    secret_prompt = build_secret_runtime_prompt(has_secret_request=has_secret_request)
    for section in (maintenance, creation, install, search_prompt, secret_prompt):
        if section:
            sections.extend(["", section])

    result = "\n".join(sections)
    with _SKILLS_PROMPT_CACHE_LOCK:
        _SKILLS_PROMPT_CACHE[cache_key] = result
        _SKILLS_PROMPT_CACHE.move_to_end(cache_key)
        while len(_SKILLS_PROMPT_CACHE) > _SKILLS_PROMPT_CACHE_MAX:
            _SKILLS_PROMPT_CACHE.popitem(last=False)
    return result


def clear_skills_system_prompt_cache() -> None:
    """Clear the in-process skills prompt cache."""
    with _SKILLS_PROMPT_CACHE_LOCK:
        _SKILLS_PROMPT_CACHE.clear()


def load_soul_md() -> Optional[str]:
    """Load SOUL.md from M-Claw home and return its content, or None."""
    soul_path = get_mclaw_home() / "SOUL.md"
    if not soul_path.exists():
        return None
    try:
        content = soul_path.read_text(encoding="utf-8").strip()
        return content if content else None
    except (OSError, UnicodeError):
        return None


def _build_platform_block(model: str = "", config: "dict | None" = None) -> str:
    """Build platform/environment context for the system prompt."""
    from mclaw.runtime.manager import RuntimeManager

    runtime = RuntimeManager.current(config)
    info = get_platform_info(config=config)
    lines = []
    if model:
        lines.append(f"Model: {model}")
    now = datetime.now()
    lines.append(f"Current date: {now.strftime('%Y-%m-%d %H:%M %Z').strip()}")
    os_text = runtime.prompt_os_label(info.os_name, info.os_release)
    if info.is_wsl:
        os_text += " (WSL)"
    lines.append(f"OS: {os_text}")
    lines.append(f"Python: {info.python_version}")
    lines.append(f"Working directory: {info.cwd}")
    lines.append(f"Shell: {info.shell_name}")
    if info.command_hint:
        lines.append(f"Command execution: {info.command_hint}")
    if info.is_linux and info.runtime_mode != "kaihong":
        lines.append(
            "Linux note: desktop GUI, audio input, and browser automation depend on the local display/audio/browser runtime; "
            "use /doctor to verify optional capabilities before relying on them."
        )
    return "\n".join(lines)


def build_system_prompt(
    agent_platform: str = "cli",
    model: str = "",
    soul_md: str = None,
    memory_block: str = None,
    tool_names: "set | None" = None,
    available_toolsets: "set | None" = None,
    available_tool_names: "list[str] | None" = None,
    config: "dict | None" = None,
) -> str:
    """Assemble the full system prompt from identity + platform + guidance.

    When tool_names is provided and includes skills tools, the skills index block
    (progressive disclosure) is appended automatically.
    """
    sections = []

    # 1. Identity: SOUL.md, stored default, or built-in default.
    if soul_md:
        sections.append(soul_md)
    else:
        loaded = load_soul_md()
        if loaded:
            sections.append(loaded)
        else:
            from mclaw.cli.default_soul import DEFAULT_SOUL_MD
            sections.append(DEFAULT_SOUL_MD)

    # 1b. Language enforcement
    sections.append(
        "语言规定 / Language Rule:\n"
        "你必须全程使用简体中文（Simplified Chinese）回复所有用户可见的内容，"
        "包括：所有文字输出、工具描述、错误信息、状态提示。\n"
        "不要用英文回复，除非用户明确用英文提问。"
    )

    # 2. Platform hint
    hint = PLATFORM_HINTS.get(agent_platform, "")
    if hint:
        sections.append(hint)

    # 3. Runtime environment context.
    sections.append("# Environment\n" + _build_platform_block(model=model, config=config))

    # 4. Tool-use rules and available tool names.
    tool_guidance_parts: List[str] = []
    if tool_names or available_tool_names:
        tool_guidance_parts.append(TOOL_USE_ENFORCEMENT_GUIDANCE)
    if available_tool_names:
        tool_guidance_parts.append(build_available_tools_prompt(available_tool_names))
    if tool_guidance_parts:
        sections.append("# 工具使用规则\n" + "\n\n".join(tool_guidance_parts))

    if tool_names and "delegate_task" in tool_names:
        delegation_prompt = build_delegation_prompt(has_delegate_task=True)
        if delegation_prompt:
            sections.append(delegation_prompt)

    # 5. Memory rules and recalled memory block.
    memory_tool_names = {"memory_read", "memory_add", "memory_replace", "memory_remove"}
    if tool_names is None or bool(memory_tool_names & set(tool_names)) or memory_block:
        memory_parts = [MEMORY_GUIDANCE]
        if memory_block:
            memory_parts.append(memory_block)
        sections.append("\n\n".join(memory_parts))

    # 5b. Cross-session recall guidance
    if tool_names and "session_search" in tool_names:
        sections.append(SESSION_SEARCH_GUIDANCE)

    # 6. Skills index — progressive disclosure (only when skills tools are available)
    if tool_names:
        has_skills = bool(SKILL_TOOL_NAMES.intersection(tool_names))
        if has_skills:
            skills_block = build_skills_system_prompt(
                available_tools=tool_names,
                available_toolsets=available_toolsets,
                config=config,
            )
            if skills_block:
                sections.append(skills_block)

    return "\n\n".join(sections)
