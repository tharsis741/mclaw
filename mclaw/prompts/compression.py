# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Context compression prompt builders.

The prompt shape is kept separate from the compressor so tests and future
providers can reuse the same handoff-summary contract without importing runtime
state.
"""

from __future__ import annotations


def build_context_compression_prompt(
    *,
    previous_summary: str = "",
    content_to_summarize: str,
    summary_budget: int,
) -> str:
    """Build the handoff-summary prompt used by context compression."""
    previous_instruction = ""
    if previous_summary:
        previous_instruction = f"""已有上一版摘要。请基于下面的新会话更新它，保留仍然有效的事实，不要从零重写。

上一版摘要：
{previous_summary}

"""

    return f"""为后续继续本会话的 assistant 生成结构化交接摘要。

{previous_instruction}需要摘要的会话：
{content_to_summarize}

使用这个结构：

## Goal
[用户真正要完成的目标]

## Constraints & Preferences
[用户偏好、约束、代码风格、关键要求]

## Progress
### Done
[已完成的工作：具体文件、命令、结果]
### In Progress
[正在进行的工作]
### Blocked
[阻塞点或问题]

## Key Decisions
[重要技术决策和原因]

## Relevant Files
[读过、修改过、创建过的文件]

## Next Steps
[下一步要做什么]

## Critical Context
[关键值、报错、配置细节]

## Tools & Patterns
[使用过的工具和模式]

目标约 {summary_budget} tokens。写具体事实，只输出摘要正文。"""
