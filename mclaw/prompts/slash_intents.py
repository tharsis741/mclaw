"""CLI slash-command intent prompt builders."""

from __future__ import annotations

from collections.abc import Iterable


def build_skill_install_intent(source: str) -> str:
    """Build the model task queued by /skill install."""
    source_text = str(source or "").strip()
    return (
        "用户要安装外部 M-Claw Skill。\n\n"
        "来源：\n"
        f"{source_text}\n\n"
        "执行外部 Skill 安装流程：\n"
        f"1. 调用 skill_manage(action=\"install_prepare\", source={source_text!r}, user_intent=\"/skill install {source_text}\")。\n"
        "2. 根据返回状态处理：requires_confirmation 等待确认；blocked 说明原因；success 报告结果。\n"
        "3. 返回 error 时，在用户提供来源范围内规范化一次来源，再调用一次 install_prepare。\n"
        "4. 兜底来源只使用用户提供的地址/路径，或该来源直接暴露的下载/API 链接。"
    )


def build_skill_creation_intent(brief: str) -> str:
    """Build the model task queued by /skill creation."""
    brief_text = str(brief or "").strip()
    return (
        "用户要创建新的 M-Claw Skill。\n\n"
        "用户意图：\n"
        f"{brief_text}\n\n"
        "执行 Skill 创建流程。\n\n"
        "先判断是否已具备：Skill 用途、使用时机、可复用流程、输入输出、安全边界和验证方式。\n"
        "信息不足时，向用户补问缺失项。\n\n"
        "信息足够时：\n"
        f"1. 调用 skill_manage(action=\"create_scaffold\", name=..., short_description=<中文短描述>, user_intent={brief_text!r})。\n"
        "2. 用 skill_manage(action=\"edit\") 写入最终 SKILL.md。\n"
        "3. 需要脚本、模板、参考资料或资产时，用 skill_manage(action=\"write_file\") 添加。\n"
        "4. 调用 skill_manage(action=\"validate\", name=...) 校验。\n"
        "5. 有可执行行为时，在临时目录做最小 smoke test，并对比预期和实际结果。\n"
        "6. 验证通过后报告创建结果。"
    )


def build_pet_file_drop_intent(paths: Iterable[object]) -> str:
    """Build the model task queued when files are dropped onto the desktop pet."""
    clean_paths = []
    for path in paths:
        text = str(path or "").strip()
        if text:
            clean_paths.append(text)
    if not clean_paths:
        return ""
    if len(clean_paths) == 1:
        return f"请分析这个文件：{clean_paths[0]}"
    joined = "\n".join(f"- {path}" for path in clean_paths)
    return f"请分析这些文件：\n{joined}"
