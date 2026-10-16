# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Storage/format contracts; Agent execution is covered by real-provider tests."""

import json
import sqlite3

import pytest

from mclaw.application.store import ApplicationStore
from mclaw.application.task_notifications import completion_notice, first_reply_line


@pytest.mark.parametrize(("content", "expected"), [
    ("\n  第一行结果。\n第二行不应进入摘要", "第一行结果。"),
    ("## **完成**：`007`\n后续说明", "完成：007"),
    ("- [查看报告](https://example.org/report) &amp; **结果**", "查看报告 & 结果"),
    ("保留 sample_id、a_b 和 007", "保留 sample_id、a_b 和 007"),
    ("   \n", ""),
    ("a" * 501, "a" * 499 + "…"),
])
def test_final_reply_first_line(content, expected):
    assert first_reply_line(content) == expected


def test_json_first_line_and_credentials_are_preserved_and_redacted_respectively():
    session = {"id": "s", "title": "result"}
    task = {"id": "t", "source": "web", "status": "completed", "result": {"completed": True},
            "attempts": [{"ended_at": 1}]}
    response = {"id": "r", "content": '{"count":3,"api_key":"test-secret"}\nDo not include this line'}
    notice = completion_notice(session, task, response, {"test-secret"})
    assert '"count": 3' in notice["preview"]
    assert "test-secret" not in notice["preview"] and "Do not include" not in notice["preview"]
    assert "\n" not in notice["preview"]


@pytest.mark.parametrize("source,result,status", [
    ("scheduler", {"completed": True}, "completed"),
    ("delegate", {"completed": True}, "completed"),
    ("web", {"completed": True, "cancelled": True}, "completed"),
    ("web", {"completed": True, "completion_unknown": True}, "completed"),
    ("web", {"completed": False}, "failed"),
    ("web", {"completed": True}, "waiting"),
])
def test_non_main_and_non_successful_results_do_not_create_completion_records(source, result, status):
    assert completion_notice({}, {"source": source, "status": status, "result": result}, {}) is None


def test_additive_store_upgrade_ack_and_atomic_save(tmp_path):
    path = tmp_path / "application.sqlite3"
    legacy = {"id": "old", "seq": 3, "status": "completed", "updated_at": 1, "messages": []}
    with sqlite3.connect(path) as db:
        db.executescript("CREATE TABLE app_sessions (id TEXT PRIMARY KEY, document TEXT NOT NULL, updated_at REAL NOT NULL); PRAGMA user_version=1;")
        db.execute("INSERT INTO app_sessions VALUES (?, ?, ?)", ("old", json.dumps(legacy), 1))
    store = ApplicationStore(path)
    try:
        assert store.get("old") == legacy
        assert store.task_notifications()["pending_count"] == 0  # No historical backfill.
        notice = {"id": "completion_a", "session_id": "old", "created_at": 2, "response_id": "a", "title": "old", "preview": "text"}
        store.save({**legacy, "seq": 4}, notification=notice)
        store.save({**legacy, "seq": 4}, notification=notice)
        assert store.task_notifications()["notifications"] == [notice]
        with pytest.raises(ValueError):
            store.save({**legacy, "seq": 99}, notification={**notice, "session_id": "other"})
        assert store.get("old")["seq"] == 4
        store.acknowledge_task_notifications([notice["id"]], read=False)
    finally:
        store.close()
    restored = ApplicationStore(path)
    try:
        assert restored.task_notifications() == {"notifications": [], "pending_count": 0, "unread_session_ids": ["old"]}
        restored.save({**legacy, "seq": 5}, notification=notice)
        assert restored.task_notifications()["pending_count"] == 0  # Replay cannot undo dismissal.
        restored.acknowledge_task_notifications([notice["id"], "missing"], read=True)
        assert not restored.task_notifications()["unread_session_ids"]
        restored.save(legacy, notification={**notice, "id": "completion_b"})
        restored.delete("old")
        assert restored.task_notifications()["pending_count"] == 0
    finally:
        restored.close()


def test_notification_feed_drains_more_than_one_page(tmp_path):
    store = ApplicationStore(tmp_path / "application.sqlite3")
    try:
        session = {"id": "s", "updated_at": 1}
        for index in range(105):
            store.save(session, notification={"id": str(index), "session_id": "s", "created_at": index})
        feed = store.task_notifications()
        assert len(feed["notifications"]) == 100 and feed["pending_count"] == 105
        store.acknowledge_task_notifications([item["id"] for item in feed["notifications"]], read=True)
        assert [item["id"] for item in store.task_notifications()["notifications"]] == [str(i) for i in range(100, 105)]
    finally:
        store.close()
