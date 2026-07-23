#!/usr/bin/env python3
"""
file-organizer: 按文件名关键词/模式对文件夹内文件进行分类整理。

用法:
    python organizer.py <目标目录>

流程:
    1. 扫描目录内所有顶层文件
    2. 按硬编码规则匹配分类
    3. 展示预览并等待用户确认
    4. 执行移动 + 生成撤销脚本

退出码:
    0 - 成功完成（含用户取消）
    1 - 参数错误或目录无效
"""

import os
import re
import sys
import shutil
from datetime import datetime
from pathlib import Path

# ============================================================
# 硬编码分类规则
# 格式: (分类名称, [关键词列表])
# 顺序优先：排在前面的规则优先匹配
# ============================================================
RULES = [
    ("图片", ["photo", "pic", "img", "截图", "图片", "jpg", "jpeg", "png", "gif", "bmp", "webp", "image"]),
    ("文档", ["doc", "文档", "报告", "report", "简历", "resume", "cv", "合同", "contract",
              "pdf", "word", "excel", "ppt", "pptx", "docx", "xlsx", "txt", "md", "markdown",
              "笔记", "note", "总结", "summary"]),
    ("视频", ["video", "视频", "录像", "录制", "record", "mp4", "avi", "mov", "mkv", "wmv",
              "flv", "webm", "screen", "capture"]),
    ("音频", ["audio", "音乐", "歌曲", "music", "mp3", "wav", "flac", "aac", "ogg", "wma",
              "podcast", "录音"]),
    ("代码", ["code", "src", "源码", "source", "项目", "project", "py", "js", "ts", "html",
              "css", "go", "rs", "java", "cpp", "cxx", "hpp", "h", "c", "swift", "kt",
              "vue", "react", "node", "npm", "yarn", "docker", "config"]),
    ("压缩包", ["zip", "rar", "7z", "tar", "gz", "bz2", "xz", "zst", "压缩", "archive",
               "backup"]),
    ("数据", ["data", "数据", "export", "csv", "json", "xml", "sql", "db", "sqlite",
             "备份", "dump", "log", "日志"]),
    ("安装包", ["setup", "install", "安装", "exe", "msi", "dmg", "appimage", "pkg",
               "deb", "rpm", "snap", "flatpak"]),
    ("设计素材", ["design", "设计", "psd", "ai", "sketch", "figma", "xd", "ae", "pr",
                "template", "模板", "素材", "asset"]),
]

# Archive extensions take precedence over ordinary filename keywords.
ARCHIVE_CATEGORY = '压缩包'
ARCHIVE_SUFFIXES = (".zip", ".rar", ".7z", ".tar", ".tgz", ".gz", ".bz2", ".xz", ".zst")

# ============================================================
# 不移动的文件/目录名
# ============================================================
IGNORE_NAMES = {
    "_整理_图片", "_整理_文档", "_整理_视频", "_整理_音频", "_整理_代码",
    "_整理_压缩包", "_整理_数据", "_整理_安装包", "_整理_设计素材", "_整理_其他",
    "_整理撤销脚本",
}

IGNORE_PREFIXES = ("_整理_", "_整理撤销脚本", "undo_", ".")


def should_ignore(name: str) -> bool:
    """判断是否应跳过此文件/目录"""
    if name in IGNORE_NAMES:
        return True
    for prefix in IGNORE_PREFIXES:
        if name.startswith(prefix):
            return True
    return False


def classify(filename: str) -> str:
    """根据文件名匹配分类规则，返回分类名称"""
    lower_name = filename.lower()
    if lower_name.endswith(ARCHIVE_SUFFIXES):
        return ARCHIVE_CATEGORY
    for category, keywords in RULES:
        for kw in keywords:
            # 如果关键词全是字母数字，按单词边界匹配；否则直接子串匹配
            if kw.isalnum():
                pattern = re.compile(r'(^|[\W_])' + re.escape(kw) + r'($|[\W_])', re.IGNORECASE)
                if pattern.search(lower_name):
                    return category
            else:
                if kw.lower() in lower_name:
                    return category
    return "其他"


def format_table(rows, headers):
    """格式化表格输出"""
    col_widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            col_widths[i] = max(col_widths[i], len(cell))
    sep = "+" + "+".join("-" * (w + 2) for w in col_widths) + "+"
    lines = [sep]
    # header
    hdr_line = "|"
    for i, h in enumerate(headers):
        hdr_line += f" {h:<{col_widths[i]}} |"
    lines.append(hdr_line)
    lines.append(sep)
    for row in rows:
        line = "|"
        for i, cell in enumerate(row):
            line += f" {cell:<{col_widths[i]}} |"
        lines.append(line)
    lines.append(sep)
    return "\n".join(lines)


def summarize(assignments: dict):
    """生成各类别文件数统计"""
    total = sum(len(files) for files in assignments.values())
    lines = []
    for cat in sorted(assignments.keys()):
        count = len(assignments[cat])
        lines.append(f"  {cat}: {count} 个文件")
    lines.append(f"  ---")
    lines.append(f"  总计: {total} 个文件")
    return "\n".join(lines)


def make_undo_dir(target_dir: Path) -> Path:
    """创建撤销脚本存放目录"""
    undo_dir = target_dir / "_整理撤销脚本"
    undo_dir.mkdir(parents=True, exist_ok=True)
    return undo_dir


def generate_undo_script(undo_dir: Path, moves: list, timestamp: str):
    """生成撤销脚本（bat + sh）"""
    is_windows = sys.platform == "win32"

    # BAT (Windows)
    bat_path = undo_dir / f"undo_{timestamp}.bat"
    with open(bat_path, "w", encoding="utf-8") as f:
        f.write("@echo off\n")
        f.write("chcp 65001 >nul\n")
        f.write(f"echo 正在撤销文件整理 ({timestamp})...\n")
        f.write("echo.\n")
        for src, dst in moves:
            f.write(f'move "{dst}" "{src}" >nul 2>&1\n')
        f.write("echo 撤销完成！\n")
        f.write("pause\n")
    os.chmod(bat_path, 0o755)

    # SH (Unix/macOS)
    sh_path = undo_dir / f"undo_{timestamp}.sh"
    with open(sh_path, "w", encoding="utf-8") as f:
        f.write("#!/bin/bash\n")
        f.write(f"# 撤销文件整理 ({timestamp})\n")
        f.write("set -e\n")
        f.write("cd \"$(dirname \"$0\")\"\n")
        f.write("echo '正在撤销文件整理...'\n")
        for src, dst in moves:
            escaped_src = str(src).replace("'", "'\\''")
            escaped_dst = str(dst).replace("'", "'\\''")
            f.write(f'mv -n "{escaped_dst}" "{escaped_src}"\n')
        f.write("echo '撤销完成！'\n")
    os.chmod(sh_path, 0o755)

    # 日志
    log_path = undo_dir / f"undo_{timestamp}.log"
    with open(log_path, "w", encoding="utf-8") as f:
        f.write(f"# 整理操作日志 - {timestamp}\n")
        f.write(f"# 来源目录: {undo_dir.parent}\n")
        f.write(f"# 共 {len(moves)} 个文件\n\n")
        for src, dst in moves:
            f.write(f"{src}\t->\t{dst}\n")

    return bat_path, sh_path


def main():
    if len(sys.argv) != 2:
        print("用法: python organizer.py <目标目录>")
        sys.exit(1)

    target = Path(sys.argv[1]).resolve()
    if not target.is_dir():
        print(f"错误: 目录不存在或不是文件夹: {target}")
        sys.exit(1)

    # === 扫描文件 ===
    print(f"\n正在扫描目录: {target}\n")
    files = []
    for entry in target.iterdir():
        if entry.is_file() and not should_ignore(entry.name):
            files.append(entry)

    if not files:
        print("没有找到需要整理的文件。")
        sys.exit(0)

    # === 分类 ===
    assignments = {}   # category -> [(src_path, filename)]
    unclassified = []  # files that will go to "其他"
    for f in files:
        cat = classify(f.name)
        assignments.setdefault(cat, []).append((f, f.name))

    # === 预览 ===
    print("=" * 60)
    print("  文件分类预览")
    print("=" * 60)

    rows = []
    for cat in sorted(assignments.keys()):
        for fpath, fname in assignments[cat]:
            rows.append((fname, f"→  _整理_{cat}/", fname))

    if rows:
        print(format_table(
            [(r[0], r[1]) for r in rows],
            ["文件名", "移动至"]
        ))
    else:
        print("没有文件需要移动。")
        sys.exit(0)

    print()
    print("分类统计:")
    print(summarize(assignments))
    print()

    # === 确认 ===
    try:
        confirm = input("确认执行以上整理操作？(y/N): ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\n已取消。")
        sys.exit(0)

    if confirm != "y":
        print("已取消操作，未做任何修改。")
        sys.exit(0)

    # === 执行移动 ===
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    undo_dir = make_undo_dir(target)
    moves = []  # (src_path, dst_path)

    moved_count = 0
    error_count = 0

    for cat in sorted(assignments.keys()):
        category_dir = target / f"_整理_{cat}"
        category_dir.mkdir(exist_ok=True)

        for src_path, fname in assignments[cat]:
            dst_path = category_dir / fname
            # 避免覆盖
            if dst_path.exists():
                stem = dst_path.stem
                suffix = dst_path.suffix
                counter = 1
                while dst_path.exists():
                    dst_path = category_dir / f"{stem}_{counter}{suffix}"
                    counter += 1
            try:
                shutil.move(str(src_path), str(dst_path))
                moves.append((str(src_path), str(dst_path)))
                moved_count += 1
            except OSError as e:
                print(f"  移动失败: {src_path.name} → {e}")
                error_count += 1

    # === 生成撤销脚本 ===
    if moves:
        bat, sh = generate_undo_script(undo_dir, moves, timestamp)
        print()
        print(f"已移动 {moved_count} 个文件")
        print(f"撤销脚本: {bat}")
        print(f"         {sh}")
        if error_count:
            print(f"注意: {error_count} 个文件移动失败")
        print("\n如需撤销，请运行对应的撤销脚本。")
    else:
        print("没有文件被移动。")

    print("整理完成。")


if __name__ == "__main__":
    main()
