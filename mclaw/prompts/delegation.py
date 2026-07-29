# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Prompt builders for delegated subagent planning and execution."""

from __future__ import annotations


def build_delegate_synthesis_extra_system() -> str:
    """Return the recovery prompt appended after delegated child agents finish.

    The text keeps the parent turn responsible for unfinished work instead of
    treating child-agent output as an automatic final answer.
    """
    return (
        "当前是子代理结果回传后的继续阶段。"
        "先用子代理结果判断原始用户任务还缺什么。"
        "需要继续创建文件、修改内容、运行验证、调用工具或整合交付物时，继续执行。"
        "本阶段不可再次调用 delegate_task；需要补充工作时使用现有普通工具完成。"
        "确认原始任务已经完成后，再给用户最终回复。"
    )
