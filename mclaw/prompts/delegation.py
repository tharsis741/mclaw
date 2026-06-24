"""Delegation prompt builders."""

from __future__ import annotations


def build_delegate_synthesis_extra_system() -> str:
    """Return the extra system prompt used when parent turns resume after child agents finish."""
    return (
        "当前是子代理结果回传后的继续阶段。"
        "先用子代理结果判断原始用户任务还缺什么。"
        "需要继续创建文件、修改内容、运行验证、调用工具或整合交付物时，继续执行。"
        "确认原始任务已经完成后，再给用户最终回复。"
    )
