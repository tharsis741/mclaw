# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Per-task device choices and presentation of native collaboration results."""

from __future__ import annotations

import hashlib
import re
import uuid
from pathlib import Path


DEVICE_ID = re.compile(r"^urn:mclaw:device:oh:[0-9a-f]{64}$")


def device_ids(value) -> list[str]:
    from mclaw.application.sessions import ApplicationError

    if (not isinstance(value, list) or len(value) > 20
            or any(not isinstance(item, str) or not DEVICE_ID.fullmatch(item) for item in value)):
        raise ApplicationError("INVALID_DEVICES", "请选择有效的协作设备，最多 20 台")
    return list(dict.fromkeys(value))


def peer_name(service, identifier: str) -> str:
    report = getattr(service, "report", {}) or {}
    for peer in report.get("devices", []) + report.get("trustedDevices", []):
        if peer.get("deviceId", peer.get("publicDeviceId")) == identifier:
            return str(peer.get("deviceName") or "协作设备")
    runtime = getattr(service, "runtime", None)
    read = getattr(runtime, "list_peers", None)
    try:
        for peer in read() if read else []:
            if peer.get("device_id", peer.get("deviceId")) == identifier:
                return str(peer.get("device_name", peer.get("deviceName")) or "协作设备")
    except Exception:
        pass  # A display name must not prevent the native call from returning its own error.
    return "设备 " + identifier.rsplit(":", 1)[-1][:8]


def selection_snapshot(value, service=None) -> dict:
    selected = device_ids(value)
    return {"mode": "selected" if selected else "auto",
            "devices": [{"id": identifier, "name": peer_name(service, identifier)} for identifier in selected]}


def selected_scope(task: dict) -> frozenset[str] | None:
    selection = task.get("device_selection") or {}
    if selection.get("mode", "auto") == "auto":
        return None
    return frozenset(device_ids([item["id"] for item in selection.get("devices", [])]))


def import_receipts(home: Path, session_id: str, device_id: str, name: str, result: dict) -> tuple[list[dict], list[str]]:
    """Copy only hashed receipts from the exact peer/task artifact directory."""
    from mclaw.application.files import FileService, MAX_UPLOAD_BYTES
    from mclaw.application.sessions import ApplicationError
    from mclaw.dsoftbus.workspace import default_workspace_root, peer_directory

    files, errors = [], []
    task_id = str(uuid.UUID(result["task_id"]))
    if not DEVICE_ID.fullmatch(device_id):
        raise ValueError("Invalid peer identity")
    root = default_workspace_root(home / "dsoftbus")
    store = FileService(home)
    for receipt in result.get("artifacts", []):
        try:
            artifact_id = str(uuid.UUID(receipt["artifactId"]))
            directory = root / "artifacts" / "received" / peer_directory(device_id) / task_id / artifact_id
            for part in receipt.get("parts", []):
                path = Path(part["localPath"])
                # Compare unresolved and resolved paths so symlinks cannot widen access.
                if (path.parent != directory or path.is_symlink() or not path.is_file()
                        or path.resolve().parent != directory.absolute()):
                    raise ValueError("Invalid artifact path")
                length = part["byteLength"]
                if type(length) is not int or not 0 <= length <= MAX_UPLOAD_BYTES:
                    raise ValueError("Artifact too large")
                with path.open("rb") as stream:
                    content = stream.read(MAX_UPLOAD_BYTES + 1)
                if len(content) != length or hashlib.sha256(content).hexdigest() != part["sha256"]:
                    raise ValueError("Artifact contents changed")
                identifier = "remote_" + hashlib.sha256(
                    f"{session_id}:{device_id}:{task_id}:{artifact_id}:{part['index']}:{part['sha256']}".encode()
                ).hexdigest()[:32]
                files.append(store.receive(session_id, identifier, path.name, content,
                                           device=name, device_id=device_id, task_id=task_id))
        except (ApplicationError, KeyError, TypeError, ValueError, OSError):
            errors.append("有文件未能加入会话，请在调用详情中核对回传记录。")
    return files, errors
