# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Size reduction for structured tool results without mutating provider data."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

_TRUNCATION_MARKER = " [...truncated]"
_MIN_TEXT_LENGTH = 100


def truncate_json_value(
    value: Any,
    excess: int,
    *,
    protected_keys: frozenset[str] = frozenset(),
) -> tuple[Any, int]:
    """Shorten the longest eligible string, including the marker in its budget."""
    if excess <= 0:
        return value, 0

    def strings(node: Any, path: tuple = ()) -> Iterator[tuple[tuple, str]]:
        if isinstance(node, str) and len(node) > _MIN_TEXT_LENGTH:
            yield path, node
        elif isinstance(node, dict):
            for key, child in node.items():
                if key not in protected_keys:
                    yield from strings(child, (*path, key))
        elif isinstance(node, list):
            for index, child in enumerate(node):
                yield from strings(child, (*path, index))

    candidate = max(strings(value), key=lambda item: len(item[1]), default=None)
    if candidate is None:
        return value, 0
    path, text = candidate
    target_length = max(_MIN_TEXT_LENGTH, len(text) - max(1, int(excess)))
    shortened = text[:target_length - len(_TRUNCATION_MARKER)] + _TRUNCATION_MARKER
    trimmed = len(text) - len(shortened)
    if not path:
        return shortened, trimmed

    result = value.copy()
    parent = result
    for key in path[:-1]:
        parent[key] = parent[key].copy()
        parent = parent[key]
    parent[path[-1]] = shortened
    return result, trimmed
