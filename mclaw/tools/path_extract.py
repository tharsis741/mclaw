# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Extract local absolute paths from model and tool text.

The extractor handles quoted paths, extension-aware unquoted file paths, and a
fallback path scan for simple directory references. It favors avoiding false
"file not found" reports over greedily consuming surrounding prose.
"""

from __future__ import annotations

import os
import re
from typing import Iterable, List


PATH_EXTENSIONS = (
    "pptx", "ppt", "pdf", "docx", "doc", "xlsx", "xls", "csv",
    "txt", "md", "markdown", "py", "json", "yaml", "yml",
    "html", "htm", "css", "js", "ts", "tsx", "jsx",
    "png", "jpg", "jpeg", "gif", "webp", "bmp", "svg",
    "zip", "tar", "gz", "7z", "mp3", "wav", "mp4", "mov",
)

_TRAILING_CHARS = " \t\r\n\"'”’`。；;，,、.!?)]}"
_UNIX_PATH_START_CHARS = set(" \t\r\n\"'“”‘’`")
_PATH_SCAN_STOP_CHARS = set("\r\n\"'“”‘’`<>|?*")
_MAX_EXISTENCE_SCAN_CHARS = 2048


def _clean_path(path: str) -> str:
    cleaned = path.strip().strip("\"'“”‘’`")
    while cleaned and cleaned[-1] in _TRAILING_CHARS:
        cleaned = cleaned[:-1]
    return cleaned.strip()


def _dedupe(paths: Iterable[str]) -> List[str]:
    seen: set[str] = set()
    result: list[str] = []
    for path in paths:
        cleaned = _clean_path(path)
        if not cleaned:
            continue
        norm = os.path.normcase(os.path.normpath(cleaned))
        if norm in seen:
            continue
        seen.add(norm)
        result.append(cleaned)
    return result


def _extend_to_existing_path(text: str, start: int, initial: str) -> str:
    """Extend an unquoted simple path to the longest existing path on disk.

    Regex can only safely capture an unquoted path up to the first whitespace.
    For paths such as `D:/work/M-Robots OS 3.0 Materials`, continue scanning the
    current text segment and keep the longest prefix that actually exists.
    This is intentionally existence-based so normal prose after the path is not
    swallowed merely because it looks path-like.
    """
    best = _clean_path(initial)
    stop = start
    limit = min(len(text), start + _MAX_EXISTENCE_SCAN_CHARS)
    while stop < limit and text[stop] not in _PATH_SCAN_STOP_CHARS:
        stop += 1

    segment = text[start:stop]
    for end in range(len(best), len(segment) + 1):
        candidate = _clean_path(segment[:end])
        if len(candidate) <= len(best):
            continue
        try:
            if os.path.exists(os.path.normpath(candidate)):
                best = candidate
        except (OSError, ValueError):
            continue
    return best


def extract_absolute_paths(text: str) -> List[str]:
    """Extract Windows/Unix absolute paths, including paths with spaces.

    The extractor is intentionally extension-aware for unquoted paths. This
    avoids the old failure mode where `D:/foo/M-Robots OS 3.0.pptx` was cut at
    the first space while also preventing greedy matches from eating following
    prose.
    """
    if not text:
        return []

    paths: list[str] = []
    primary_paths: list[str] = []

    # Quoted paths may be files or directories; capture until the closing quote.
    # This handles command snippets such as cd "D:/path with spaces".
    quote_pattern = r'["“”\']((?:[A-Za-z]:[\\/]|/)[^"“”\']+)["“”\']'
    for match in re.finditer(quote_pattern, text):
        path = match.group(1)
        paths.append(path)
        primary_paths.append(path)

    ext_alt = "|".join(re.escape(ext) for ext in PATH_EXTENSIONS)

    # Unquoted file paths may contain spaces; stop at known file extensions.
    win_file = rf'[A-Za-z]:[\\/][^\r\n<>|?*]*?\.(?:{ext_alt})(?=$|[\s"”’\'`,，。；;!?)\]])'
    unix_file = rf'/(?:[^\r\n<>|?*]*?)\.(?:{ext_alt})(?=$|[\s"”’\'`,，。；;!?)\]])'
    for match in re.finditer(win_file, text, flags=re.IGNORECASE):
        path = match.group(0)
        paths.append(path)
        primary_paths.append(path)
    for match in re.finditer(unix_file, text, flags=re.IGNORECASE):
        if match.start() > 0 and text[match.start() - 1] not in _UNIX_PATH_START_CHARS:
            continue
        path = match.group(0)
        paths.append(path)
        primary_paths.append(path)

    # Fallback for unquoted directories or simple paths without spaces.
    win_simple_path = r'[A-Za-z]:[\\/][^\s"\'`<>|?*]+'
    unix_simple_path = r'/[^\s"\'`<>|?*]+'
    for match in re.finditer(win_simple_path, text):
        simple = _clean_path(match.group(0))
        simple = _extend_to_existing_path(text, match.start(), simple)
        # Extension-aware matches may already have captured a longer path with
        # spaces. Drop the simple-prefix match to avoid false not-found reports.
        if any(_clean_path(path).startswith(simple + " ") for path in primary_paths):
            continue
        paths.append(simple)
    for match in re.finditer(unix_simple_path, text):
        if match.start() > 0 and text[match.start() - 1] not in _UNIX_PATH_START_CHARS:
            continue
        simple = _clean_path(match.group(0))
        simple = _extend_to_existing_path(text, match.start(), simple)
        if any(_clean_path(path).startswith(simple + " ") for path in primary_paths):
            continue
        paths.append(simple)

    return _dedupe(paths)
