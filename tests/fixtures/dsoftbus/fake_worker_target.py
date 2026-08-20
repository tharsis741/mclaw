# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Pipe-level fake Worker used only for Runtime supervision validation."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import time
from typing import Any


def _canonical(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _write(stream: Any, value: Any) -> None:
    stream.write(_canonical(value))
    stream.flush()


def _hello_template(encoded: str) -> dict[str, Any]:
    try:
        raw = base64.b64decode(encoded, validate=True)
        value = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("invalid hello template") from error
    expected = {"identity", "localUdid", "nativeAbiVersion", "socketCap"}
    if not isinstance(value, dict) or set(value) != expected:
        raise RuntimeError("invalid hello template")
    return value


def _audit(path: str, operation: str) -> None:
    if not path:
        return
    with open(path, "ab", buffering=0) as stream:
        stream.write(_canonical({"op": operation}))


def run(args: argparse.Namespace) -> int:
    hello = _hello_template(args.hello_json_base64)
    maps_sha256 = hashlib.sha256(
        b"mclaw-fake-worker-maps\0" + args.epoch.encode("ascii")
    ).hexdigest()
    readiness = {
        "code": "WORKER_READY",
        "kind": "worker-diagnostic",
        "mapsSha256": maps_sha256,
        "pid": os.getpid(),
        "workerEpoch": args.epoch,
    }
    if args.mode == "startup-error":
        _write(sys.stderr.buffer, {"code": "NATIVE_LOAD_FAILED", "kind": "worker-diagnostic"})
        return 78
    if args.mode == "early-stdout":
        _write(sys.stdout.buffer, {"early": True})
    if args.mode == "malformed-readiness":
        sys.stderr.buffer.write(b"not-json\n")
        sys.stderr.buffer.flush()
        return 78
    if args.mode == "bad-readiness":
        readiness["unknown"] = True
    if args.mode == "wrong-ready-pid":
        readiness["pid"] = os.getpid() + 1
    _write(sys.stderr.buffer, readiness)
    if args.mode in {"bad-readiness", "wrong-ready-pid"}:
        return 78

    native_started = False
    trusted_device_present = True
    trusted_device_digest = "d" * 64
    discovery_active = False
    bind_status: dict[str, tuple[str, int]] = {}
    listener_socket = 10
    peer_socket = 11
    for raw in sys.stdin.buffer:
        try:
            command = json.loads(raw.decode("utf-8"))
            command_id = command["id"]
            operation = command["op"]
        except (KeyError, UnicodeDecodeError, json.JSONDecodeError, TypeError):
            return 70
        _audit(args.audit_path, operation)
        if operation == "hello":
            result = dict(hello)
            result["workerEpoch"] = args.epoch
            if args.mode == "malformed-hello":
                del result["localUdid"]
            if args.mode == "wrong-hello-epoch":
                result["workerEpoch"] = "00000000-0000-4000-8000-000000000001"
            _write(
                sys.stdout.buffer,
                {"id": command_id, "ok": True, "result": result, "v": 1},
            )
            if args.mode == "event-after-hello":
                _write(
                    sys.stdout.buffer,
                    {
                        "data": {
                            "deviceName": "unexpected",
                            "deviceTypeId": 0,
                            "networkId": "unexpected-network",
                            "nodeEventSeq": 1,
                        },
                        "event": "node-online",
                        "v": 1,
                        "workerEpoch": args.epoch,
                    },
                )
            if args.mode == "exit-after-hello":
                time.sleep(args.exit_delay)
                return 70
            continue
        if operation == "start" and args.mode.startswith("phase-b"):
            native_started = True
            _write(
                sys.stdout.buffer,
                {
                    "id": command_id,
                    "ok": True,
                    "result": {"nodeEventsStarted": True},
                    "v": 1,
                },
            )
            if args.mode == "phase-b-event":
                _write(
                    sys.stdout.buffer,
                    {
                        "data": {
                            "deviceName": "Peer device",
                            "deviceTypeId": 1,
                            "networkId": "peer-network",
                            "nodeEventSeq": 1,
                        },
                        "event": "node-online",
                        "v": 1,
                        "workerEpoch": args.epoch,
                    },
                )
            if args.mode == "phase-b-overflow":
                _write(
                    sys.stdout.buffer,
                    {
                        "data": {"droppedBytes": 1, "droppedCount": 1},
                        "event": "overflow",
                        "v": 1,
                        "workerEpoch": args.epoch,
                    },
                )
            continue
        if operation == "snapshot_nodes" and native_started and args.mode.startswith("phase-b"):
            continuation = bool(command.get("args"))
            _write(
                sys.stdout.buffer,
                {
                    "id": command_id,
                    "ok": True,
                    "result": {
                        "nextCursor": "",
                        "nodes": (
                            []
                            if continuation
                            else [
                                {
                                    "deviceName": "Peer device",
                                    "deviceTypeId": 1,
                                    "networkId": "peer-network",
                                }
                            ]
                        ),
                        "replayAfterSeq": 1 if args.mode == "phase-b-event" else 0,
                        "replayThroughSeq": 1 if args.mode == "phase-b-event" else 0,
                        "snapshotId": "00000000-0000-4000-8000-000000000002",
                    },
                    "v": 1,
                },
            )
            continue
        if operation == "get_node_udid" and native_started and args.mode.startswith("phase-b"):
            _write(
                sys.stdout.buffer,
                {
                    "id": command_id,
                    "ok": True,
                    "result": {"udid": "peer-b"},
                    "v": 1,
                },
            )
            continue
        if operation == "start_device_discovery" and native_started and args.mode.startswith("phase-b"):
            if discovery_active:
                return 64
            discovery_active = True
            _write(
                sys.stdout.buffer,
                {
                    "id": command_id,
                    "ok": True,
                    "result": {"started": True},
                    "v": 1,
                },
            )
            continue
        if operation == "stop_device_discovery" and native_started and args.mode.startswith("phase-b"):
            if not discovery_active:
                return 64
            discovery_active = False
            _write(
                sys.stdout.buffer,
                {
                    "id": command_id,
                    "ok": True,
                    "result": {
                        "devices": [
                            {
                                "deviceIdSha256": "e" * 64,
                                "deviceName": "Candidate device",
                                "deviceTypeId": 533,
                            }
                        ],
                        "failureNativeCode": None,
                        "stopped": True,
                    },
                    "v": 1,
                },
            )
            continue
        if operation == "begin_device_bind" and native_started and args.mode.startswith("phase-b"):
            digest = command["args"]["deviceIdSha256"]
            if digest != "e" * 64:
                return 64
            bind_status[digest] = ("bound", 0)
            trusted_device_present = True
            trusted_device_digest = digest
            _write(
                sys.stdout.buffer,
                {
                    "id": command_id,
                    "ok": True,
                    "result": {"binding": True, "deviceIdSha256": digest},
                    "v": 1,
                },
            )
            continue
        if operation == "get_device_bind_status" and native_started and args.mode.startswith("phase-b"):
            digest = command["args"]["deviceIdSha256"]
            if digest not in bind_status:
                return 64
            status, native_code = bind_status[digest]
            _write(
                sys.stdout.buffer,
                {
                    "id": command_id,
                    "ok": True,
                    "result": {
                        "deviceIdSha256": digest,
                        "nativeCode": native_code,
                        "status": status,
                    },
                    "v": 1,
                },
            )
            continue
        if operation == "list_trusted_devices" and native_started and args.mode.startswith("phase-b"):
            devices = []
            if trusted_device_present:
                devices.append(
                    {
                        "deviceIdSha256": trusted_device_digest,
                        "deviceName": "Peer device",
                        "deviceTypeId": 1,
                        "networkId": "peer-network",
                    }
                )
            _write(
                sys.stdout.buffer,
                {
                    "id": command_id,
                    "ok": True,
                    "result": {"devices": devices},
                    "v": 1,
                },
            )
            continue
        if operation == "unbind_device" and native_started and args.mode.startswith("phase-b"):
            if command["args"]["networkId"] != "peer-network" or not trusted_device_present:
                return 64
            trusted_device_present = False
            _write(
                sys.stdout.buffer,
                {
                    "id": command_id,
                    "ok": True,
                    "result": {
                        "deviceIdSha256": trusted_device_digest,
                        "unbound": True,
                    },
                    "v": 1,
                },
            )
            continue
        if operation == "listen" and native_started and args.mode.startswith("phase-b"):
            _write(
                sys.stdout.buffer,
                {
                    "id": command_id,
                    "ok": True,
                    "result": {"socket": listener_socket},
                    "v": 1,
                },
            )
            continue
        if operation == "connect" and native_started and args.mode.startswith("phase-b"):
            _write(
                sys.stdout.buffer,
                {
                    "id": command_id,
                    "ok": True,
                    "result": {"mtu": 32768, "socket": peer_socket},
                    "v": 1,
                },
            )
            continue
        if operation == "send_bytes" and native_started and args.mode.startswith("phase-b"):
            sent = len(base64.b64decode(command["args"]["data"], validate=True))
            _write(
                sys.stdout.buffer,
                {
                    "id": command_id,
                    "ok": True,
                    "result": {"sentBytes": sent},
                    "v": 1,
                },
            )
            continue
        if operation == "close_socket" and native_started and args.mode.startswith("phase-b"):
            _write(
                sys.stdout.buffer,
                {
                    "id": command_id,
                    "ok": True,
                    "result": {"closed": True},
                    "v": 1,
                },
            )
            continue
        if operation == "stop":
            if args.mode == "hang-stop":
                while True:
                    time.sleep(1)
            if args.mode == "stop-error":
                _write(
                    sys.stdout.buffer,
                    {
                        "error": {"code": "NATIVE_STOP_FAILED", "nativeCode": -1},
                        "id": command_id,
                        "ok": False,
                        "v": 1,
                    },
                )
                continue
            if args.stop_delay:
                time.sleep(args.stop_delay)
            _write(
                sys.stdout.buffer,
                {
                    "id": command_id,
                    "ok": True,
                    "result": {"stopped": True},
                    "v": 1,
                },
            )
            return 0
        _write(
            sys.stdout.buffer,
            {
                "error": {"code": "INVALID_WORKER_STATE", "nativeCode": 0},
                "id": command_id,
                "ok": False,
                "v": 1,
            },
        )
    return 70


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--epoch", required=True)
    parser.add_argument("--hello-json-base64", required=True)
    parser.add_argument("--audit-path", default="")
    parser.add_argument("--mode", default="normal")
    parser.add_argument("--exit-delay", type=float, default=0.10)
    parser.add_argument("--stop-delay", type=float, default=0.0)
    args = parser.parse_args()
    try:
        return run(args)
    except Exception:
        return 70


if __name__ == "__main__":
    raise SystemExit(main())
