# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tool groupings and platform presets for M-Claw."""

from __future__ import annotations

from typing import Any

CREDENTIALS_TOOLS: list[str] = ["secret_request_many"]
TERMINAL_TOOLS = ["terminal", "process"]
FILE_TOOLS = ["read_file", "write_file", "patch", "edit_file", "delete_file", "search_files", "list_directory"]
MEMORY_TOOLS = ["memory_read", "memory_add", "memory_replace", "memory_remove"]
SKILLS_TOOLS = ["skills_list", "skill_tree", "skill_view", "skill_search", "skill_manage"]
DELEGATION_SKILL_READ_TOOLS = ["skills_list", "skill_tree", "skill_view"]
SESSION_SEARCH_TOOLS = ["session_search"]
DELEGATION_TOOLS = ["delegate_task"]
WEB_TOOLS = ["web_search", "web_extract"]
VISION_TOOLS = ["vision_analyze"]
BROWSER_TOOLS = [
    "browser_navigate",
    "browser_snapshot",
    "browser_screenshot",
    "browser_click",
    "browser_type",
    "browser_scroll",
    "browser_press",
    "browser_download",
]
WEIXIN_TOOLS = ["weixin_send_file"]
DINGTALK_TOOLS = ["dingtalk_send_file"]
DSOFTBUS_TOOLS = [
    "dsoftbus_list_peers",
    "dsoftbus_get_device_context",
    "dsoftbus_run_agent_task",
]
DSOFTBUS_ARTIFACT_TOOLS = ["return_artifact"]

REQUIRED_TOOLSETS = ["credentials", "terminal", "file", "memory", "skills", "session_search", "delegation"]
OPTIONAL_TOOLSETS = ["web", "vision", "browser", "weixin", "dingtalk"]


def _tools_for_toolsets(toolset_names: list[str], definitions: dict[str, dict[str, Any]]) -> list[str]:
    """Flatten toolsets while preserving first-seen tool order."""
    tools: list[str] = []
    seen: set[str] = set()
    for toolset_name in toolset_names:
        for tool in definitions[toolset_name]["tools"]:
            if tool in seen:
                continue
            tools.append(tool)
            seen.add(tool)
    return tools


TOOLSETS: dict[str, dict[str, Any]] = {
    "credentials": {
        "description": "Scoped credential requests and authorization",
        "display": {"emoji": "🔐", "summary_zh": "请求密钥授权"},
        "tools": CREDENTIALS_TOOLS,
        "kind": "required",
    },
    "terminal": {
        "description": "Terminal execution and background process management",
        "display": {"emoji": "💻", "summary_zh": "终端命令与后台进程"},
        "tools": TERMINAL_TOOLS,
        "kind": "required",
    },
    "file": {
        "description": "Workspace file read, write, patch, edit, delete, search, and directory listing",
        "display": {"emoji": "📄", "summary_zh": "工作区文件读写"},
        "tools": FILE_TOOLS,
        "kind": "required",
    },
    "memory": {
        "description": "Persistent cross-session memory",
        "display": {"emoji": "🧠", "summary_zh": "管理长期记忆"},
        "tools": MEMORY_TOOLS,
        "kind": "required",
    },
    "skills": {
        "description": "Skills discovery, search, view, and management",
        "display": {"emoji": "🧩", "summary_zh": "技能发现, 下载与管理"},
        "tools": SKILLS_TOOLS,
        "kind": "required",
    },
    "delegation_skill_read": {
        "description": "Read-only local skill discovery and viewing for delegated agents",
        "tools": DELEGATION_SKILL_READ_TOOLS,
        "kind": "scoped",
    },
    "session_search": {
        "description": "Cross-session conversation search and recall",
        "display": {"emoji": "🔎", "summary_zh": "历史会话检索"},
        "tools": SESSION_SEARCH_TOOLS,
        "kind": "required",
    },
    "delegation": {
        "description": "Subagent delegation",
        "display": {"emoji": "🔀", "summary_zh": "委派子代理执行复杂任务"},
        "tools": DELEGATION_TOOLS,
        "kind": "required",
    },
    "web": {
        "description": "Web search and readable page extraction",
        "display": {"emoji": "🌐", "summary_zh": "联网搜索与网页提取"},
        "tools": WEB_TOOLS,
        "kind": "optional",
    },
    "vision": {
        "description": "Vision analysis for images via URL or local path",
        "display": {"emoji": "👓", "summary_zh": "图片理解与识别"},
        "tools": VISION_TOOLS,
        "kind": "optional",
    },
    "browser": {
        "description": "Headless browser automation (navigate, click, type, scroll, screenshot, download)",
        "display": {"emoji": "🤖", "summary_zh": "浏览器自动化"},
        "tools": BROWSER_TOOLS,
        "kind": "optional",
    },
    "weixin": {
        "description": "Weixin current-chat outbound helpers",
        "display": {"emoji": "💬", "summary_zh": "微信工具集"},
        "tools": WEIXIN_TOOLS,
        "kind": "optional",
    },
    "dingtalk": {
        "description": "DingTalk current-chat outbound helpers",
        "display": {"emoji": "📨", "summary_zh": "钉钉工具集"},
        "tools": DINGTALK_TOOLS,
        "kind": "optional",
    },
    "dsoftbus": {
        "description": "Trusted OpenHarmony device discovery and M-Claw messaging",
        "display": {"emoji": "✉", "summary_zh": "可信设备协作"},
        "tools": DSOFTBUS_TOOLS,
        "kind": "platform",
    },
    "dsoftbus-remote": {
        "description": "Internal scoped marker for inbound DSoftBus turns",
        "tools": [],
        "kind": "scoped",
    },
    "dsoftbus-artifact": {
        "description": "Internal Task-output adapter for inbound DSoftBus turns",
        "tools": DSOFTBUS_ARTIFACT_TOOLS,
        "kind": "scoped",
    },
    "minimal": {
        "description": "Minimal toolset for simple tasks",
        "tools": ["terminal", "read_file", "write_file"],
        "kind": "preset",
    },
}

TOOLSETS["mclaw-required"] = {
    "description": "Required M-Claw baseline for first-run setup",
    "tools": _tools_for_toolsets(REQUIRED_TOOLSETS, TOOLSETS),
    "kind": "preset",
}


def resolve_toolset(name: str, *, include_platform: bool = False) -> list[str]:
    """Resolve a toolset name to a flat list of tool names."""
    if name == "all":
        tools = set()
        for ts in TOOLSETS.values():
            kind = ts.get("kind")
            if kind == "scoped":
                continue
            if kind == "platform" and not include_platform:
                continue
            tools.update(ts.get("tools", []))
        return sorted(tools)
    ts = TOOLSETS.get(name)
    if not ts:
        return []
    return list(ts.get("tools", []))


def resolve_multiple_toolsets(
    names: list[str],
    *,
    include_platform: bool = False,
) -> set[str]:
    """Resolve multiple toolset names into a deduplicated tool-name set."""
    tools = set()
    for name in names:
        tools.update(resolve_toolset(name, include_platform=include_platform))
    return tools


def get_optional_toolset_names() -> list[str]:
    """Return optional toolset names exposed in setup and diagnostics."""
    return list(OPTIONAL_TOOLSETS)


def validate_toolset(
    name: str,
    *,
    allow_platform: bool = False,
    allow_scoped: bool = False,
) -> bool:
    """Return whether a toolset name or the special `all` preset is valid."""
    if name == "all":
        return True
    definition = TOOLSETS.get(name)
    if definition is None:
        return False
    kind = definition.get("kind")
    if kind == "platform" and not allow_platform:
        return False
    if kind == "scoped" and not allow_scoped:
        return False
    return True
