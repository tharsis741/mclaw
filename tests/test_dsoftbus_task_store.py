# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest

from mclaw.dsoftbus.task_store import DsoftbusTaskStore, TaskStoreError


PEER = "urn:mclaw:device:oh:" + "a" * 64
TASK_ID = "0e4ba172-c081-48e9-a9d9-7b5714c98a42"
CONTEXT_ID = "b07eca54-7c35-4c27-833c-c578395cc13e"
ARTIFACT_ID = "99cda057-9032-4d98-8a57-20694bcaccd6"


def _task(state: str = "TASK_STATE_SUBMITTED") -> dict[str, object]:
    return {
        "id": TASK_ID,
        "contextId": CONTEXT_ID,
        "status": {
            "state": state,
            "timestamp": "2026-08-14T00:00:00.000Z",
        },
        "history": [],
        "artifacts": [],
        "metadata": {"mclaw.requestMessageId": TASK_ID},
    }


def test_task_store_persists_tasks_across_reopen(tmp_path: Path) -> None:
    state_root = tmp_path / "dsoftbus"
    first = DsoftbusTaskStore(state_root)
    first.put_task("owned", PEER, _task())
    first.put_task("owned", PEER, _task("TASK_STATE_WORKING"))
    assert first.database_path == state_root / "tasks.db"
    first.close()

    second = DsoftbusTaskStore(state_root)
    task = second.get_task("owned", PEER, TASK_ID)
    assert task is not None
    assert task["status"]["state"] == "TASK_STATE_WORKING"
    assert second.list_tasks("owned", PEER) == (task,)
    second.close()


def test_task_state_cannot_regress_or_change_context(tmp_path: Path) -> None:
    store = DsoftbusTaskStore(tmp_path / "dsoftbus")
    store.put_task("received", PEER, _task("TASK_STATE_WORKING"))
    with pytest.raises(TaskStoreError, match="TASK_STATE_REGRESSION"):
        store.put_task("received", PEER, _task("TASK_STATE_SUBMITTED"))

    completed = _task("TASK_STATE_COMPLETED")
    store.put_task("received", PEER, completed)
    with pytest.raises(TaskStoreError, match="TASK_STATE_REGRESSION"):
        store.put_task("received", PEER, _task("TASK_STATE_WORKING"))

    changed_context = {
        **completed,
        "contextId": "10a423fd-471a-4c56-aef9-204023fc0f11",
    }
    with pytest.raises(TaskStoreError, match="TASK_CONTEXT_CONFLICT"):
        store.put_task("received", PEER, changed_context)


def test_received_artifact_is_hash_verified_and_path_is_local(
    tmp_path: Path,
) -> None:
    store = DsoftbusTaskStore(tmp_path / "dsoftbus")
    store.put_task("received", PEER, _task("TASK_STATE_WORKING"))
    raw = b"\x00remote bytes\xff"
    receipt = store.persist_artifact(
        "received",
        PEER,
        TASK_ID,
        {
            "artifactId": ARTIFACT_ID,
            "name": "../report.bin",
            "parts": [
                {
                    "raw": base64.b64encode(raw).decode("ascii"),
                    "filename": "../../peer-output.bin",
                    "mediaType": "application/octet-stream",
                }
            ],
        },
    )
    part = receipt["parts"][0]
    path = Path(part["localPath"])
    expected_peer_digest = hashlib.sha256(PEER.encode("utf-8")).hexdigest()
    assert path.read_bytes() == raw
    assert path.name == "01-peer-output.bin"
    assert expected_peer_digest in path.parts
    assert part["byteLength"] == len(raw)
    assert part["sha256"] == hashlib.sha256(raw).hexdigest()
    assert not list(path.parent.glob("*.part"))
    assert store.get_artifact_receipt(
        "received", PEER, TASK_ID, ARTIFACT_ID
    ) == receipt


def test_owned_text_and_data_artifacts_are_materialized(tmp_path: Path) -> None:
    store = DsoftbusTaskStore(tmp_path / "dsoftbus")
    store.put_task("owned", PEER, _task("TASK_STATE_WORKING"))
    receipt = store.persist_artifact(
        "owned",
        PEER,
        TASK_ID,
        {
            "artifactId": ARTIFACT_ID,
            "name": "result",
            "parts": [
                {"text": "完成", "mediaType": "text/plain"},
                {"data": {"ok": True}, "mediaType": "application/json"},
            ],
        },
    )
    text_path = Path(receipt["parts"][0]["localPath"])
    data_path = Path(receipt["parts"][1]["localPath"])
    assert text_path.read_text(encoding="utf-8") == "完成"
    assert json.loads(data_path.read_text(encoding="utf-8")) == {"ok": True}


def test_artifact_replay_is_idempotent_but_same_id_cannot_change(
    tmp_path: Path,
) -> None:
    store = DsoftbusTaskStore(tmp_path / "dsoftbus")
    store.put_task("received", PEER, _task("TASK_STATE_WORKING"))
    artifact = {
        "artifactId": ARTIFACT_ID,
        "name": "answer.txt",
        "parts": [{"text": "first", "mediaType": "text/plain"}],
    }
    first = store.persist_artifact(
        "received", PEER, TASK_ID, artifact
    )
    second = store.persist_artifact(
        "received", PEER, TASK_ID, artifact
    )
    assert second == first
    assert Path(second["parts"][0]["localPath"]).read_text(
        encoding="utf-8"
    ) == "first"

    changed = {
        **artifact,
        "parts": [{"text": "replacement", "mediaType": "text/plain"}],
    }
    with pytest.raises(TaskStoreError, match="ARTIFACT_CONFLICT"):
        store.persist_artifact("received", PEER, TASK_ID, changed)
    assert Path(first["parts"][0]["localPath"]).read_text(
        encoding="utf-8"
    ) == "first"


def test_artifact_requires_an_existing_direction_scoped_task(
    tmp_path: Path,
) -> None:
    store = DsoftbusTaskStore(tmp_path / "dsoftbus")
    with pytest.raises(TaskStoreError, match="TASK_NOT_FOUND"):
        store.persist_artifact(
            "received",
            PEER,
            TASK_ID,
            {
                "artifactId": ARTIFACT_ID,
                "parts": [{"text": "x"}],
            },
        )


def test_runtime_restart_fails_only_owned_active_tasks(tmp_path: Path) -> None:
    store = DsoftbusTaskStore(tmp_path / "dsoftbus")
    store.put_task("owned", PEER, _task("TASK_STATE_WORKING"))
    store.put_task("received", PEER, _task("TASK_STATE_WORKING"))
    assert store.recover_interrupted_owned_tasks() == 1
    owned = store.get_task("owned", PEER, TASK_ID)
    received = store.get_task("received", PEER, TASK_ID)
    assert owned is not None and received is not None
    assert owned["status"]["state"] == "TASK_STATE_FAILED"
    assert received["status"]["state"] == "TASK_STATE_WORKING"
