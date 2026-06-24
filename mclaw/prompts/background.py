# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Prompt builders for background memory and Skill review tasks."""

from __future__ import annotations

MEMORY_REVIEW_PROMPT = (
    "审查上面的会话，提取值得长期保存的记忆。\n\n"
    "重点保存用户偏好、稳定个人背景、环境事实、工具习惯和明确的长期要求。"
    "有可复用事实时使用 memory_add。没有值得保存的内容时，回复 Nothing to save."
)

EVOLUTION_REVIEW_PROMPT = (
    "审查已完成会话，提取可复用的 Skill 经验。\n\n"
    "会话中出现稳定流程、明确纠正、可复用教训或弃用信号时，"
    "用 skill_manage(action='evolution_update', ...) 更新对应 Skill。"
    "需要维护 Skill 文件时，使用允许的 skill_manage 文件动作。"
    "没有值得保存的内容时，回复 Nothing to save."
)

COMBINED_REVIEW_PROMPT = (
    f"{MEMORY_REVIEW_PROMPT}\n\n"
    f"{EVOLUTION_REVIEW_PROMPT}\n\n"
    "只保存稳定、可复用、未来会影响行为的信息。"
)

MEMORY_FLUSH_SYSTEM_PROMPT = (
    "当前是 memory flush 模式。审查最近会话，只处理值得长期保存的信息。\n"
    "需要保存时调用 memory_add / memory_replace / memory_remove。\n"
    "没有可保存内容时直接结束，不调用工具。"
)


def build_background_review_prompt(*, review_memory: bool, review_skills: bool) -> str:
    """Return the user prompt for an isolated background review pass."""
    if review_memory and review_skills:
        return COMBINED_REVIEW_PROMPT
    if review_memory:
        return MEMORY_REVIEW_PROMPT
    return EVOLUTION_REVIEW_PROMPT


def build_memory_flush_system_prompt() -> str:
    """Return the system prompt for explicit memory flush turns."""
    return MEMORY_FLUSH_SYSTEM_PROMPT
