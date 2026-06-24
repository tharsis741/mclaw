"""DingTalk markdown formatting and splitting."""

from __future__ import annotations

import re
import textwrap


def normalize_markdown_for_dingtalk(content: str | None) -> str:
    text = str(content or "")
    lines = text.split("\n")
    out: list[str] = []
    for idx, line in enumerate(lines):
        stripped = line.strip()
        is_numbered = re.match(r"^\d+\.\s", stripped)
        if is_numbered and idx > 0:
            prev = lines[idx - 1]
            if prev.strip() and not re.match(r"^\d+\.\s", prev.strip()):
                out.append("")
        if stripped.startswith("```") and line != line.lstrip():
            line = line.lstrip()
        out.append(line)
    return "\n".join(out).strip()


def split_text_for_dingtalk(content: str, max_length: int = 20000) -> list[str]:
    text = normalize_markdown_for_dingtalk(content)
    if not text:
        return []
    if len(text) <= max_length:
        return [text]

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    in_fence = False
    fence_lang = ""

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            if in_fence:
                in_fence = False
                fence_lang = ""
            else:
                in_fence = True
                fence_lang = stripped[3:].strip().split(" ", 1)[0]

        add_len = len(line) + (1 if current else 0)
        if current and current_len + add_len > max_length - 16:
            chunk = "\n".join(current)
            if in_fence and not chunk.rstrip().endswith("```"):
                chunk += "\n```"
            chunks.append(chunk)
            current = []
            current_len = 0
            if in_fence:
                current.append(f"```{fence_lang}".rstrip())
                current_len = len(current[0])

        if len(line) > max_length:
            wrapped = textwrap.wrap(line, width=max_length - 16, break_long_words=True)
            for part in wrapped:
                if current:
                    chunks.append("\n".join(current))
                    current = []
                    current_len = 0
                chunks.append(part)
            continue

        current.append(line)
        current_len += add_len

    if current:
        chunks.append("\n".join(current))
    if len(chunks) <= 1:
        return chunks
    total = len(chunks)
    return [f"{chunk}\n\n({idx + 1}/{total})" for idx, chunk in enumerate(chunks)]
