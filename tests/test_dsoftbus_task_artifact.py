# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

import pytest

from mclaw.dsoftbus import protocol
from mclaw.dsoftbus.a2a import A2AError, CoreMethodCall, validate_core_method
from mclaw.dsoftbus.a2a_media import TASK_ARTIFACT_REFERENCE_PREFIX
from mclaw.dsoftbus.task_artifact import (
    TASK_ARTIFACT_DESCRIPTOR_MEDIA_TYPE,
    InboundArtifactStore,
    TaskArtifactCollector,
    TaskArtifactError,
    artifact_part_local_filename,
    artifact_transfer_parts,
)
from mclaw.dsoftbus.workspace import DsoftbusWorkspace


PEER = "urn:mclaw:device:oh:" + "b" * 64
TASK_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
CONTEXT_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


@pytest.mark.parametrize(
    ("reason", "message"),
    (
        ("ARTIFACT_NOT_FOUND", "Task artifact was not found"),
        ("ARTIFACT_IO_ERROR", "Task artifact storage failed"),
        ("ARTIFACT_HASH_MISMATCH", "Task artifact hash does not match"),
        ("ARTIFACT_TOO_LARGE", "Task artifact is too large"),
        ("ARTIFACT_CHANGED", "Task artifact changed while it was read"),
    ),
)
def test_artifact_error_reasons_are_wire_stable(reason: str, message: str) -> None:
    error = A2AError(reason)

    assert error.code == protocol.RPC_ERROR_CODES[reason]
    assert error.message == message


def _file_artifact(
    root: Path,
    content: bytes,
) -> tuple[DsoftbusWorkspace, dict]:
    workspace = DsoftbusWorkspace(root / "producer")
    source = root / "source.bin"
    source.write_bytes(content)
    collector = TaskArtifactCollector(
        task_id=TASK_ID,
        context_id=CONTEXT_ID,
        peer_device_id=PEER,
        workspace=workspace,
    )
    artifact = collector.add_file(
        name="result.bin",
        path=source,
        media_type="application/octet-stream",
        description="Result file.",
    )
    return workspace, dict(artifact)


def test_artifact_transfer_resumes_and_atomically_publishes(tmp_path: Path) -> None:
    content = bytes(range(251)) * 240
    producer, artifact = _file_artifact(tmp_path, content)
    transfers = artifact_transfer_parts(artifact)
    assert len(transfers) == 1
    index, _part, descriptor = transfers[0]
    part = artifact["parts"][0]
    assert part["url"] == (
        f"{TASK_ARTIFACT_REFERENCE_PREFIX}{descriptor['transferId']}"
    )
    assert part["mediaType"] == "application/octet-stream"
    assert "data" not in part
    artifact_id = artifact["artifactId"]
    produced = (
        producer.ensure_artifact_directory(
            "produced", PEER, TASK_ID, artifact_id
        )
        / artifact_part_local_filename(artifact, index)
    )
    assert produced.read_bytes() == content

    receiver_workspace = DsoftbusWorkspace(tmp_path / "receiver")
    receiver = InboundArtifactStore(
        receiver_workspace, PEER, TASK_ID, artifact
    )
    transfer_id = str(descriptor["transferId"])
    first = content[: protocol.TASK_TRANSFER_CHUNK_BYTES_MAX]
    assert receiver.begin(transfer_id) == 0
    assert receiver.append(transfer_id, 0, first) == len(first)

    resumed = InboundArtifactStore(
        receiver_workspace, PEER, TASK_ID, artifact
    )
    offset = resumed.begin(transfer_id)
    assert offset == len(first)
    while offset < len(content):
        chunk = content[
            offset : offset + protocol.TASK_TRANSFER_CHUNK_BYTES_MAX
        ]
        offset = resumed.append(transfer_id, offset, chunk)
    receipt = resumed.commit(transfer_id)

    local_path = Path(receipt["localPath"])
    assert local_path.is_absolute()
    assert local_path.read_bytes() == content
    assert receipt["byteLength"] == len(content)
    assert receipt["sha256"] == descriptor["sha256"]


def test_persisted_data_descriptor_remains_readable() -> None:
    transfer_id = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    artifact = {
        "artifactId": CONTEXT_ID,
        "parts": [
            {
                "data": {
                    "transferId": transfer_id,
                    "contentMediaType": "image/png",
                    "byteLength": 0,
                    "sha256": "0" * 64,
                },
                "filename": "old-result.png",
                "mediaType": TASK_ARTIFACT_DESCRIPTOR_MEDIA_TYPE,
            }
        ],
        "extensions": [protocol.TASK_FILES_EXTENSION_URI],
    }

    descriptor = artifact_transfer_parts(artifact)[0][2]
    assert descriptor["transferId"] == transfer_id
    assert descriptor["contentMediaType"] == "image/png"


def test_artifact_hash_mismatch_discards_partial_for_clean_retry(
    tmp_path: Path,
) -> None:
    content = b"verified artifact bytes"
    _producer, artifact = _file_artifact(tmp_path, content)
    descriptor = artifact_transfer_parts(artifact)[0][2]
    transfer_id = str(descriptor["transferId"])
    receiver = InboundArtifactStore(
        DsoftbusWorkspace(tmp_path / "receiver"),
        PEER,
        TASK_ID,
        artifact,
    )
    receiver.begin(transfer_id)
    receiver.append(transfer_id, 0, b"x" * len(content))

    with pytest.raises(TaskArtifactError, match="ARTIFACT_HASH_MISMATCH"):
        receiver.commit(transfer_id)
    assert receiver.begin(transfer_id) == 0


def test_artifact_file_limit_is_product_limit_not_frame_limit(
    tmp_path: Path,
) -> None:
    workspace = DsoftbusWorkspace(tmp_path / "producer")
    source = tmp_path / "too-large.bin"
    with source.open("wb") as stream:
        stream.truncate(protocol.TASK_ARTIFACT_BYTES_MAX + 1)
    collector = TaskArtifactCollector(
        task_id=TASK_ID,
        context_id=CONTEXT_ID,
        peer_device_id=PEER,
        workspace=workspace,
    )

    with pytest.raises(TaskArtifactError, match="ARTIFACT_TOO_LARGE"):
        collector.add_file(
            name="too-large.bin",
            path=source,
            media_type="application/octet-stream",
            description="Too large.",
        )
    assert collector.snapshot() == ()


@pytest.mark.parametrize(
    ("method", "params"),
    (
        (
            "mclaw.taskArtifact.open",
            {
                "taskId": TASK_ID,
                "artifactId": CONTEXT_ID,
                "transferId": "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
            },
        ),
        (
            "mclaw.taskArtifact.read",
            {
                "taskId": TASK_ID,
                "transferId": "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
                "offset": 0,
            },
        ),
    ),
)
def test_artifact_transfer_methods_are_strictly_validated(
    method: str,
    params: dict,
) -> None:
    call = validate_core_method(method, params)
    assert isinstance(call, CoreMethodCall)
    assert dict(call.params) == params

    with pytest.raises(A2AError, match="INVALID_PARAMS"):
        validate_core_method(method, {**params, "unexpected": True})
