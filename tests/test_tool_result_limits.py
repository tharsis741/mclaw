# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
from copy import deepcopy

import pytest

from mclaw.tools import dispatch, web_search_tool
from mclaw.tools.search import router


def _search_payload(answer: str, snippet: str) -> dict:
    return {
        "success": True,
        "answer": answer,
        "sources": [
            {"index": i + 1, "title": f"Source {i + 1}",
             "url": f"https://example.test/source/{i + 1}?q=" + "x" * 200,
             "snippet": snippet}
            for i in range(10)
        ],
        "_backend": "tavily",
    }


def _dispatch_search(monkeypatch, payload: dict) -> str:
    monkeypatch.setattr(router, "execute_search", lambda **kwargs: payload)
    return dispatch._dispatch_single(
        {"id": "search-limit-test", "function": {"name": "web_search",
         "arguments": json.dumps({"query": "test", "limit": 10})}},
        {"web_search"}, None,
    )


@pytest.mark.parametrize("answer,snippet", [
    ("A" * 1000, "B" * 2000),
    ("A" * 9000, "B" * 1200),
    ("", "B" * 3000),
    ("中文回答" * 8000, "引用\n\"摘要\"\\" * 1200),
], ids=["long_snippets", "long_answer", "sources_only", "unicode_and_escapes"])
def test_long_search_results_keep_answer_sources_and_complete_urls(monkeypatch, answer, snippet):
    payload = _search_payload(answer, snippet)
    original = deepcopy(payload)
    raw = _dispatch_search(monkeypatch, payload)
    result = json.loads(raw)

    assert len(raw) <= 15_000
    assert result["success"] is True
    assert not result.get("error")
    assert bool(result["answer"]) == bool(answer)
    assert len(result["sources"]) == 10
    assert [(s["index"], s["url"]) for s in result["sources"]] == [
        (s["index"], s["url"]) for s in original["sources"]
    ]
    assert result["_truncated"] is True
    assert result["_original_size"] == len(json.dumps(original, ensure_ascii=False))
    assert result["_limit"] == 15_000
    assert payload == original


def test_search_omits_unfit_sources_instead_of_corrupting_links_or_losing_answer(monkeypatch):
    payload = _search_payload("Useful answer [2]. " * 100, "Brief source. " * 20)
    payload["sources"][0]["url"] += "x" * 20_000
    original = deepcopy(payload)
    raw = _dispatch_search(monkeypatch, payload)
    result = json.loads(raw)

    assert len(raw) <= 15_000
    assert result["success"] is True
    assert result["answer"] == original["answer"]
    assert result["sources"] == original["sources"][1:]
    assert result["_sources_omitted"] == 1
    assert payload == original


def test_search_marks_omissions_when_complete_links_together_exceed_budget(monkeypatch):
    payload = _search_payload("Answer with references.", "Brief source.")
    for source in payload["sources"]:
        source["url"] += "x" * 2500
    original = deepcopy(payload)
    raw = _dispatch_search(monkeypatch, payload)
    result = json.loads(raw)

    assert len(raw) <= 15_000
    assert result["success"] is True
    assert result["answer"] == original["answer"]
    assert 0 < len(result["sources"]) < 10
    assert result["_sources_omitted"] == 10 - len(result["sources"])
    for source in result["sources"]:
        assert source == original["sources"][source["index"] - 1]
    assert payload == original


@pytest.mark.parametrize("size", [14_999, 15_000])
def test_search_at_or_below_limit_is_unchanged(monkeypatch, size):
    payload = {"success": True, "answer": "", "sources": []}
    payload["answer"] = "A" * (size - len(json.dumps(payload)))
    raw = _dispatch_search(monkeypatch, payload)
    assert len(raw) == size
    assert json.loads(raw) == payload


def test_long_search_error_stays_a_failure(monkeypatch):
    result = json.loads(_dispatch_search(monkeypatch, {
        "success": False, "error": "provider unavailable " * 2000,
    }))
    assert result["success"] is False
    assert result["error"].startswith("provider unavailable")


def test_shared_guard_keeps_nested_results_and_original_size(monkeypatch):
    payload = {"success": True, "summary": "A" * 1000,
               "results": [{"content": "B" * 2000} for _ in range(10)]}
    original = deepcopy(payload)
    raw_result = json.dumps(payload)
    monkeypatch.setattr(dispatch.registry, "dispatch", lambda *args, **kwargs: raw_result)
    monkeypatch.setattr(dispatch.registry, "get_max_result_size", lambda *args: 15_000)
    result = json.loads(dispatch._dispatch_single(
        {"function": {"name": "test_read_tool", "arguments": "{}"}},
        {"test_read_tool"}, None,
    ))
    assert result["success"] is True
    assert len(result["results"]) == 10
    assert len(json.dumps(result, ensure_ascii=False)) <= 15_000
    assert result["_original_size"] == len(raw_result)
    assert payload == original


def test_repeated_string_trimming_always_makes_progress():
    value = {"answer": "A" * 1000, "sources": [{"snippet": "B" * 2000}]}
    for _ in range(10):
        updated, trimmed = dispatch._truncate_json_value(value, 10_000)
        if not trimmed:
            break
        assert len(json.dumps(updated)) < len(json.dumps(value))
        value = updated
    assert len(json.dumps(value)) < 400
