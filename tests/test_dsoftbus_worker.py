# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import base64
from collections import deque
import ctypes
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import struct
import sys
import tempfile
import threading
from typing import Any, Callable, Mapping

import pytest

from mclaw.dsoftbus import protocol, worker


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_observed_api_level_skips_link_alias_and_reads_product_partition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alias = tmp_path / "ohos.para.alias"
    product = tmp_path / "ohos.para"
    alias.write_text('const.ohos.apiversion=99\n', encoding="utf-8")
    product.write_text('const.ohos.apiversion=14\n', encoding="utf-8")
    original_is_symlink = Path.is_symlink

    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda path: path == alias or original_is_symlink(path),
    )
    monkeypatch.setattr(worker, "_OHOS_PARAMETER_FILES", (alias, product))

    assert worker._observed_api_level(object()) == 14  # type: ignore[arg-type]


def test_observed_api_level_falls_back_to_verified_parameter_tool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing = tmp_path / "missing.para"
    profile = object()
    monkeypatch.setattr(worker, "_OHOS_PARAMETER_FILES", (missing,))
    monkeypatch.setattr(
        worker,
        "_read_live_api_level",
        lambda candidate: "14" if candidate is profile else "",
    )

    assert worker._observed_api_level(profile) == 14  # type: ignore[arg-type]


def test_mapped_files_ignores_non_utf8_kernel_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        worker,
        "_read_small_bytes",
        lambda *_args, **_kwargs: b"1000-2000 r--p 0 00:00 1 /invalid-\xee\n",
    )

    assert worker._mapped_files() == {}


def _bridge_string(value: str) -> bytes:
    raw = value.encode("utf-8")
    return struct.pack("<I", len(raw)) + raw


def test_device_manager_bridge_parser_accepts_one_bounded_device() -> None:
    payload = (
        _bridge_string("device-01")
        + _bridge_string("Kaihong BotBook")
        + _bridge_string("network-01")
        + struct.pack("<H", 533)
    )

    device, offset = worker._DeviceManagerBridgeClient._take_device(  # type: ignore[attr-defined]
        payload, 0, trusted=True
    )

    assert offset == len(payload)
    assert device == worker.NativeTrustedDevice(
        "device-01",
        "Kaihong BotBook",
        533,
        "network-01",
    )


@pytest.mark.parametrize(
    "payload",
    [
        b"\x01\x00",
        struct.pack("<I", 2) + b"\xff\xff",
        struct.pack("<I", 1) + b"\x00",
        struct.pack("<I", 97) + b"x" * 97,
    ],
)
def test_device_manager_bridge_parser_rejects_malformed_strings(payload: bytes) -> None:
    with pytest.raises(worker.WorkerFailure, match="DEVICE_MANAGER_DATA_INVALID"):
        worker._DeviceManagerBridgeClient._take_string(  # type: ignore[attr-defined]
            payload, 0, minimum=1, maximum=96
        )


def test_device_manager_bridge_event_queue_accepts_only_unsolicited_events() -> None:
    client = object.__new__(worker._DeviceManagerBridgeClient)  # type: ignore[attr-defined]
    client._condition = threading.Condition()
    client._reader_failure = None
    client._frames = deque(
        [
            (
                worker._DEVICE_MANAGER_BRIDGE_DISCOVERY_FAILED,  # type: ignore[attr-defined]
                0,
                -7,
                b"",
            )
        ]
    )
    client._pending_events = deque()

    assert client.drain_events() == (
        (
            worker._DEVICE_MANAGER_BRIDGE_DISCOVERY_FAILED,  # type: ignore[attr-defined]
            -7,
            b"",
        ),
    )

    client._frames.append(
        (
            worker._DEVICE_MANAGER_BRIDGE_DEVICE_FOUND,  # type: ignore[attr-defined]
            1,
            0,
            b"",
        )
    )
    with pytest.raises(
        worker.WorkerFailure, match="DEVICE_MANAGER_BRIDGE_PROTOCOL_ERROR"
    ):
        client.drain_events()


def test_device_manager_bridge_drains_final_device_before_closing_scan() -> None:
    payload = (
        _bridge_string("device-final")
        + _bridge_string("Kaihong Final")
        + _bridge_string("")
        + struct.pack("<H", 533)
    )

    class Bridge:
        def __init__(self) -> None:
            self.events: list[tuple[int, int, bytes]] = []

        def stop_discovery(self) -> None:
            self.events.append(
                (
                    worker._DEVICE_MANAGER_BRIDGE_DEVICE_FOUND,  # type: ignore[attr-defined]
                    0,
                    payload,
                )
            )

        def drain_events(self) -> tuple[tuple[int, int, bytes], ...]:
            events = tuple(self.events)
            self.events.clear()
            return events

        _take_device = worker._DeviceManagerBridgeClient._take_device  # type: ignore[attr-defined]

    bridge = Bridge()
    backend = object.__new__(worker.RealNativeBackend)
    backend._device_manager_bridge = bridge
    backend._device_discovery_active = True
    backend._device_discovery_failure = None
    backend._discovered_devices = {}
    backend._require_active = lambda **_kwargs: None
    backend._uses_device_manager_bridge = lambda: True
    backend._get_device_manager_bridge = lambda: bridge

    devices, failure = backend.stop_device_discovery()

    assert failure == 0
    assert devices == (
        worker.NativeTrustedDevice(
            "device-final", "Kaihong Final", 533, "", ""
        ),
    )
    assert backend._device_discovery_active is False


class _FakeBackend:
    maps_sha256 = "a" * 64

    def __init__(self, _: object, epoch: str) -> None:
        self.epoch = epoch
        self.constructed_thread = threading.current_thread().name
        self.started = 0
        self.stopped = 0
        self.snapshots = 0
        self.listened: list[str] = []
        self.connected: list[tuple[str, str, str]] = []
        self.sent: list[tuple[int, bytes]] = []
        self.closed: list[int] = []
        self.poll_timeouts: list[int] = []
        self.nodes = (
            worker.NativeNode("peer-network-a", "peer-a", 17),
        )
        self.udids = {"peer-network-a": "b" * 64}
        self.trusted_devices = (
            worker.NativeTrustedDevice(
                "raw-device-a", "peer-a", 17, "peer-network-a"
            ),
        )
        self.discovered_devices = (
            worker.NativeTrustedDevice(
                "raw-discovered-b", "Kaihong B", 533, "network-b"
            ),
        )
        self.discovery_failure = 0
        self.discovery_active = False
        self.discovery_starts = 0
        self.discovery_stops = 0
        self.bind_starts: list[str] = []
        self.bind_results: dict[str, tuple[str, int]] = {}
        self.unbound: list[str] = []
        self.events: deque[worker.NativeEvent] = deque()
        self.sockets = {10, 11, 12}

    def hello_result(self) -> Mapping[str, Any]:
        return {
            "identity": {
                "capabilitySet": ["CAP_NET_ADMIN"],
                "distributedDataSyncGranted": True,
                "gid": 0,
                "selinuxDomain": "u:r:su:s0",
                "supplementaryGids": [1006, 1007],
                "tokenIdHash": "sha256:" + "c" * 64,
                "uid": 0,
            },
            "localUdid": "a" * 64,
            "nativeAbiVersion": 1,
            "socketCap": 16,
            "workerEpoch": self.epoch,
        }

    def start(self) -> Mapping[str, Any]:
        self.started += 1
        return {"nodeEventsStarted": True}

    def snapshot_nodes(self) -> worker.NativeSnapshot:
        self.snapshots += 1
        return worker.NativeSnapshot(self.nodes, 4, 6)

    def get_node_udid(self, network_id: str) -> str:
        if network_id not in self.udids:
            raise worker.WorkerFailure("NATIVE_ERROR", native_code=-9)
        return self.udids[network_id]

    def start_device_discovery(self) -> None:
        if self.discovery_active:
            raise worker.WorkerFailure("DEVICE_DISCOVERY_BUSY")
        self.discovery_active = True
        self.discovery_starts += 1

    def stop_device_discovery(
        self,
    ) -> tuple[tuple[worker.NativeTrustedDevice, ...], int]:
        if not self.discovery_active:
            raise worker.WorkerFailure("DEVICE_DISCOVERY_INACTIVE")
        self.discovery_active = False
        self.discovery_stops += 1
        return self.discovered_devices, self.discovery_failure

    def begin_device_bind(self, device_id_sha256: str) -> None:
        discovered = {
            hashlib.sha256(device.device_id.encode("utf-8")).hexdigest()
            for device in self.discovered_devices
        }
        if device_id_sha256 not in discovered:
            raise worker.WorkerFailure("DEVICE_NOT_FOUND")
        self.bind_starts.append(device_id_sha256)
        self.bind_results[device_id_sha256] = ("pending", 0)

    def device_bind_status(self, device_id_sha256: str) -> tuple[str, int]:
        try:
            return self.bind_results[device_id_sha256]
        except KeyError as error:
            raise worker.WorkerFailure("DEVICE_BIND_NOT_FOUND") from error

    def list_trusted_devices(self) -> tuple[worker.NativeTrustedDevice, ...]:
        return self.trusted_devices

    def unbind_device(self, network_id: str) -> str:
        matches = [
            device
            for device in self.trusted_devices
            if device.network_id == network_id
        ]
        if len(matches) != 1:
            raise worker.WorkerFailure("DEVICE_NOT_FOUND")
        target = matches[0]
        self.unbound.append(network_id)
        self.trusted_devices = tuple(
            device for device in self.trusted_devices if device is not target
        )
        return hashlib.sha256(target.device_id.encode("utf-8")).hexdigest()

    def listen(self, service_name: str) -> int:
        self.listened.append(service_name)
        return 10

    def connect(
        self,
        local_service_name: str,
        peer_service_name: str,
        network_id: str,
    ) -> tuple[int, int]:
        self.connected.append(
            (local_service_name, peer_service_name, network_id)
        )
        return 11, 32_768

    def send_bytes(self, socket: int, data: bytes) -> int:
        if socket not in self.sockets:
            raise worker.WorkerFailure("SOCKET_NOT_OWNED")
        self.sent.append((socket, data))
        return len(data)

    def close_socket(self, socket: int) -> None:
        if socket not in self.sockets:
            raise worker.WorkerFailure("SOCKET_NOT_OWNED")
        self.closed.append(socket)
        self.sockets.remove(socket)

    def poll(self, timeout_ms: int) -> worker.NativeEvent | None:
        self.poll_timeouts.append(timeout_ms)
        return self.events.popleft() if self.events else None

    def stop(self) -> Mapping[str, Any]:
        self.stopped += 1
        return {"stopped": True}


def _command(
    operation: str,
    command_id: str,
    args: Mapping[str, Any] | None = None,
) -> bytes:
    return protocol.encode_worker_command(
        operation, args or {}, command_id=command_id
    )


def _run_worker(
    raw_stdin: bytes,
    *,
    configure: Callable[[_FakeBackend], None] | None = None,
    monotonic: Callable[[], float] | None = None,
):
    instances: list[_FakeBackend] = []

    def factory(profile: object, epoch: str) -> _FakeBackend:
        backend = _FakeBackend(profile, epoch)
        if configure is not None:
            configure(backend)
        instances.append(backend)
        return backend

    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        process = worker.WorkerProcess(
            profile=object(),  # type: ignore[arg-type]
            stdin=io.BytesIO(raw_stdin),
            stdout_fd=stdout.fileno(),
            stderr_fd=stderr.fileno(),
            backend_factory=factory,  # type: ignore[arg-type]
            **({"monotonic": monotonic} if monotonic is not None else {}),
        )
        exit_code = process.run()
        stdout.seek(0)
        stderr.seek(0)
        messages = [json.loads(line) for line in stdout.read().splitlines()]
        responses = [message for message in messages if "id" in message]
        events = [message for message in messages if "event" in message]
        diagnostics = [json.loads(line) for line in stderr.read().splitlines()]
    return exit_code, responses, events, diagnostics, instances


def test_worker_hello_start_stop_are_ordered_and_native_has_one_owner() -> None:
    ids = (
        "11111111-1111-4111-8111-111111111111",
        "22222222-2222-4222-8222-222222222222",
        "33333333-3333-4333-8333-333333333333",
    )
    exit_code, responses, events, diagnostics, instances = _run_worker(
        b"".join(
            (
                _command("hello", ids[0]),
                _command("start", ids[1]),
                _command("stop", ids[2]),
            )
        )
    )
    assert exit_code == 0
    assert events == []
    assert diagnostics == []
    assert [response["id"] for response in responses] == list(ids)
    assert [response["ok"] for response in responses] == [True, True, True]
    assert responses[1]["result"] == {"nodeEventsStarted": True}
    assert responses[2]["result"] == {"stopped": True}
    assert len(instances) == 1
    assert instances[0].constructed_thread == "mclaw-dsoftbus-native-owner"
    assert instances[0].started == 1
    assert instances[0].stopped == 1


def _id(number: int) -> str:
    return f"{number:08x}-1111-4111-8111-{number:012x}"


def test_worker_device_discovery_and_bind_operations_redact_raw_device_id() -> None:
    digest = hashlib.sha256(b"raw-discovered-b").hexdigest()
    commands = (
        _command("hello", _id(90)),
        _command("start", _id(91)),
        _command("start_device_discovery", _id(92)),
        _command("stop_device_discovery", _id(93)),
        _command(
            "begin_device_bind", _id(94), {"deviceIdSha256": digest}
        ),
        _command(
            "get_device_bind_status", _id(95), {"deviceIdSha256": digest}
        ),
        _command("stop", _id(96)),
    )
    exit_code, responses, events, diagnostics, instances = _run_worker(
        b"".join(commands)
    )

    assert exit_code == 0, (responses, diagnostics)
    assert diagnostics == []
    assert events == []
    assert all(response["ok"] for response in responses)
    assert responses[2]["result"] == {"started": True}
    assert responses[3]["result"] == {
        "devices": [
            {
                "deviceIdSha256": digest,
                "deviceName": "Kaihong B",
                "deviceTypeId": 533,
                "networkIdSha256": hashlib.sha256(b"network-b").hexdigest(),
                "publicDeviceId": "",
            }
        ],
        "failureNativeCode": None,
        "stopped": True,
    }
    assert responses[4]["result"] == {
        "binding": True,
        "deviceIdSha256": digest,
    }
    assert responses[5]["result"] == {
        "deviceIdSha256": digest,
        "nativeCode": 0,
        "status": "pending",
    }
    assert "raw-discovered-b" not in json.dumps(responses, sort_keys=True)
    backend = instances[0]
    assert backend.discovery_starts == 1
    assert backend.discovery_stops == 1
    assert backend.bind_starts == [digest]


def test_worker_all_operations_and_all_nonterminal_events_are_exact() -> None:
    payload = b'{"jsonrpc":"2.0"}'

    def configure(backend: _FakeBackend) -> None:
        backend.events.extend(
            (
                worker.NativeEvent(
                    "node-online",
                    node_event_seq=1,
                    network_id="peer-network-a",
                    device_name="peer-a",
                    device_type_id=17,
                ),
                worker.NativeEvent(
                    "node-offline",
                    node_event_seq=2,
                    network_id="peer-network-a",
                    device_name="peer-a",
                    device_type_id=17,
                ),
                worker.NativeEvent(
                    "bound",
                    socket=12,
                    mtu=32_768,
                    network_id="peer-network-a",
                ),
                worker.NativeEvent("bytes", socket=11, data=payload),
                worker.NativeEvent(
                    "closed", socket=11, native_code=3
                ),
            )
        )

    commands = (
        _command("hello", _id(1)),
        _command("start", _id(2)),
        _command("snapshot_nodes", _id(3)),
        _command(
            "get_node_udid", _id(4), {"networkId": "peer-network-a"}
        ),
        _command("list_trusted_devices", _id(5)),
        _command(
            "unbind_device", _id(6), {"networkId": "peer-network-a"}
        ),
        _command("listen", _id(7), {"serviceName": protocol.SERVICE_NAME}),
        _command(
            "connect",
            _id(8),
            {
                "networkId": "peer-network-a",
                "peerServiceName": protocol.SERVICE_NAME,
                "serviceName": protocol.CLIENT_SERVICE_NAME,
            },
        ),
        _command(
            "send_bytes",
            _id(9),
            {
                "data": base64.b64encode(payload).decode("ascii"),
                "socket": 11,
            },
        ),
        _command("close_socket", _id(10), {"socket": 11}),
        _command("stop", _id(11)),
    )
    exit_code, responses, events, diagnostics, instances = _run_worker(
        b"".join(commands), configure=configure
    )

    assert exit_code == 0, (responses, diagnostics)
    assert diagnostics == []
    assert [response["id"] for response in responses] == [
        _id(index) for index in range(1, 12)
    ]
    assert all(response["ok"] for response in responses)
    hello = responses[0]["result"]
    assert frozenset(hello) == frozenset(
        {"identity", "localUdid", "nativeAbiVersion", "socketCap", "workerEpoch"}
    )
    assert responses[1]["result"] == {"nodeEventsStarted": True}
    assert responses[2]["result"]["nodes"] == [
        {
            "deviceName": "peer-a",
            "deviceTypeId": 17,
            "networkId": "peer-network-a",
        }
    ]
    assert responses[2]["result"]["nextCursor"] == ""
    assert responses[2]["result"]["replayAfterSeq"] == 4
    assert responses[2]["result"]["replayThroughSeq"] == 6
    assert responses[3]["result"] == {"udid": "b" * 64}
    digest = hashlib.sha256(b"raw-device-a").hexdigest()
    assert responses[4]["result"] == {
        "devices": [
            {
                "deviceIdSha256": digest,
                "deviceName": "peer-a",
                "deviceTypeId": 17,
                "networkId": "peer-network-a",
            }
        ]
    }
    assert responses[5]["result"] == {
        "deviceIdSha256": digest,
        "unbound": True,
    }
    assert "raw-device-a" not in json.dumps(responses, sort_keys=True)
    assert responses[6]["result"] == {"socket": 10}
    assert responses[7]["result"] == {"mtu": 32_768, "socket": 11}
    assert responses[8]["result"] == {"sentBytes": len(payload)}
    assert responses[9]["result"] == {"closed": True}
    assert responses[10]["result"] == {"stopped": True}
    assert [event["event"] for event in events] == [
        "node-online",
        "node-offline",
        "bound",
        "bytes",
        "closed",
    ]
    assert events[0]["data"] == {
        "deviceName": "peer-a",
        "deviceTypeId": 17,
        "networkId": "peer-network-a",
        "nodeEventSeq": 1,
    }
    assert events[2]["data"] == {
        "mtu": 32_768,
        "networkId": "peer-network-a",
        "socket": 12,
    }
    assert events[3]["data"] == {
        "data": base64.b64encode(payload).decode("ascii"),
        "socket": 11,
    }
    assert events[4]["data"] == {
        "code": "SOCKET_CLOSED",
        "nativeCode": 3,
        "scope": "socket",
        "socket": 11,
    }
    assert all(
        frozenset(event) == frozenset({"data", "event", "v", "workerEpoch"})
        for event in events
    )
    backend = instances[0]
    assert backend.snapshots == 1
    assert backend.unbound == ["peer-network-a"]
    assert backend.listened == [protocol.SERVICE_NAME]
    assert backend.connected == [
        (
            protocol.CLIENT_SERVICE_NAME,
            protocol.SERVICE_NAME,
            "peer-network-a",
        )
    ]
    assert backend.sent == [(11, payload)]
    assert backend.closed == [11]
    assert backend.stopped == 1
    assert backend.poll_timeouts
    assert all(0 <= timeout <= 50 for timeout in backend.poll_timeouts)


def test_snapshot_pager_freezes_watermarks_and_pages_32_nodes() -> None:
    now = [10.0]
    pager = worker._SnapshotPager(  # type: ignore[attr-defined]
        worker_epoch=_id(100), monotonic=lambda: now[0]
    )
    snapshot = worker.NativeSnapshot(
        tuple(
            worker.NativeNode(f"network-{index:03d}", f"device-{index:03d}", index)
            for index in range(65)
        ),
        12,
        14,
    )
    first = pager.begin(snapshot)
    second = pager.continue_page(first["snapshotId"], first["nextCursor"])
    third = pager.continue_page(second["snapshotId"], second["nextCursor"])

    assert [len(page["nodes"]) for page in (first, second, third)] == [32, 32, 1]
    assert first["snapshotId"] == second["snapshotId"] == third["snapshotId"]
    assert [page["replayAfterSeq"] for page in (first, second, third)] == [12] * 3
    assert [page["replayThroughSeq"] for page in (first, second, third)] == [14] * 3
    assert third["nextCursor"] == ""
    with pytest.raises(worker.WorkerFailure, match="SNAPSHOT_NOT_FOUND"):
        pager.continue_page(third["snapshotId"], "stale")


def test_snapshot_pager_ttl_cursor_and_new_snapshot_are_fail_closed() -> None:
    now = [1.0]
    pager = worker._SnapshotPager(  # type: ignore[attr-defined]
        worker_epoch=_id(101), monotonic=lambda: now[0]
    )
    snapshot = worker.NativeSnapshot(
        tuple(
            worker.NativeNode(f"network-{index:03d}", "device", 1)
            for index in range(40)
        ),
        0,
        0,
    )
    first = pager.begin(snapshot)
    with pytest.raises(worker.WorkerFailure, match="SNAPSHOT_CURSOR_INVALID"):
        pager.continue_page(first["snapshotId"], "wrong")
    now[0] += protocol.NODE_SNAPSHOT_TTL_S
    with pytest.raises(worker.WorkerFailure, match="SNAPSHOT_EXPIRED"):
        pager.continue_page(first["snapshotId"], first["nextCursor"])

    now[0] = 20.0
    old = pager.begin(snapshot)
    replacement = pager.begin(snapshot)
    assert old["snapshotId"] != replacement["snapshotId"]
    with pytest.raises(worker.WorkerFailure, match="SNAPSHOT_CURSOR_INVALID"):
        pager.continue_page(old["snapshotId"], old["nextCursor"])

    regressing = [5.0]
    pager = worker._SnapshotPager(  # type: ignore[attr-defined]
        worker_epoch=_id(103), monotonic=lambda: regressing[0]
    )
    page = pager.begin(snapshot)
    regressing[0] = 4.0
    with pytest.raises(worker.WorkerFailure, match="SNAPSHOT_CLOCK_INVALID"):
        pager.continue_page(page["snapshotId"], page["nextCursor"])


def test_snapshot_pager_rejects_count_watermark_and_field_overflow() -> None:
    pager = worker._SnapshotPager(  # type: ignore[attr-defined]
        worker_epoch=_id(102), monotonic=lambda: 1.0
    )
    with pytest.raises(worker.WorkerFailure, match="NODE_SNAPSHOT_OVERFLOW"):
        pager.begin(
            worker.NativeSnapshot(
                tuple(
                    worker.NativeNode(f"n-{index}", "d", 1)
                    for index in range(protocol.NODE_SNAPSHOT_MAX + 1)
                ),
                0,
                0,
            )
        )
    with pytest.raises(worker.WorkerFailure, match="NATIVE_DATA_INVALID"):
        pager.begin(
            worker.NativeSnapshot(
                (worker.NativeNode("x" * 65, "d", 1),),
                0,
                0,
            )
        )
    with pytest.raises(worker.WorkerFailure, match="NATIVE_DATA_INVALID"):
        pager.begin(worker.NativeSnapshot((), 2, 1))


def test_worker_rejects_any_first_operation_other_than_hello() -> None:
    command_id = "11111111-1111-4111-8111-111111111111"
    exit_code, responses, events, diagnostics, instances = _run_worker(
        _command("start", command_id)
    )
    assert exit_code == 70
    assert events == []
    assert diagnostics == []
    assert responses == [
        {
            "error": {"code": "HELLO_REQUIRED", "nativeCode": 0},
            "id": command_id,
            "ok": False,
            "v": 1,
        }
    ]
    assert instances[0].stopped == 1


def test_worker_rejects_business_before_start_but_can_stop_cleanly() -> None:
    exit_code, responses, events, diagnostics, instances = _run_worker(
        b"".join(
            (
                _command("hello", _id(20)),
                _command("snapshot_nodes", _id(21)),
                _command("stop", _id(22)),
            )
        )
    )
    assert exit_code == 0
    assert events == []
    assert diagnostics == []
    assert responses[1] == {
        "error": {"code": "INVALID_WORKER_STATE", "nativeCode": 0},
        "id": _id(21),
        "ok": False,
        "v": 1,
    }
    assert responses[2]["result"] == {"stopped": True}
    assert instances[0].snapshots == 0
    assert instances[0].stopped == 1


def test_worker_start_is_idempotent_and_backend_error_is_bounded() -> None:
    exit_code, responses, events, diagnostics, instances = _run_worker(
        b"".join(
            (
                _command("hello", _id(30)),
                _command("start", _id(31)),
                _command("start", _id(32)),
                _command(
                    "get_node_udid", _id(33), {"networkId": "missing-peer"}
                ),
                _command("stop", _id(34)),
            )
        )
    )
    assert exit_code == 0
    assert events == []
    assert diagnostics == []
    assert responses[3] == {
        "error": {"code": "NATIVE_ERROR", "nativeCode": -9},
        "id": _id(33),
        "ok": False,
        "v": 1,
    }
    assert responses[4]["result"] == {"stopped": True}
    assert instances[0].started == 2
    assert instances[0].stopped == 1


def test_duplicate_node_sequence_emits_epoch_fatal_and_reaps_backend() -> None:
    def configure(backend: _FakeBackend) -> None:
        backend.events.extend(
            (
                worker.NativeEvent(
                    "node-online",
                    node_event_seq=1,
                    network_id="peer-network-a",
                    device_name="peer-a",
                    device_type_id=1,
                ),
                worker.NativeEvent(
                    "node-offline",
                    node_event_seq=1,
                    network_id="peer-network-a",
                    device_name="peer-a",
                    device_type_id=1,
                ),
            )
        )

    exit_code, responses, events, diagnostics, instances = _run_worker(
        _command("hello", _id(40)) + _command("start", _id(41)),
        configure=configure,
    )
    assert exit_code == 70
    assert diagnostics == []
    assert [response["ok"] for response in responses] == [True, True]
    assert [event["event"] for event in events] == ["node-online", "fatal"]
    assert events[1]["data"] == {
        "code": "NATIVE_EVENT_SEQUENCE_INVALID",
        "nativeCode": 0,
        "scope": "epoch",
    }
    assert instances[0].stopped == 1


@pytest.mark.parametrize(
    ("native_event", "expected_data"),
    [
        (
            worker.NativeEvent(
                "overflow",
                status=-5,
                dropped_count=3,
                dropped_bytes=4096,
            ),
            {"droppedBytes": 4096, "droppedCount": 3},
        ),
        (
            worker.NativeEvent(
                "fatal", socket=12, status=-7, native_code=-44
            ),
            {
                "code": "BINDING_INCOMPATIBLE",
                "nativeCode": -44,
                "scope": "socket",
                "socket": 12,
            },
        ),
    ],
)
def test_terminal_native_events_have_exact_shape_and_poison_epoch(
    native_event: worker.NativeEvent, expected_data: Mapping[str, Any]
) -> None:
    def configure(backend: _FakeBackend) -> None:
        backend.events.append(native_event)

    exit_code, responses, events, diagnostics, instances = _run_worker(
        _command("hello", _id(50)) + _command("start", _id(51)),
        configure=configure,
    )
    assert exit_code == 70
    assert diagnostics == []
    assert [response["ok"] for response in responses] == [True, True]
    assert len(events) == 1
    assert events[0]["event"] == native_event.event_type
    assert events[0]["data"] == expected_data
    assert instances[0].stopped == 1


def test_malformed_backend_hello_is_terminal_and_not_reflected() -> None:
    def configure(backend: _FakeBackend) -> None:
        backend.hello_result = lambda: {  # type: ignore[method-assign]
            "workerEpoch": backend.epoch,
            "secret": "must-not-be-reflected",
        }

    exit_code, responses, events, diagnostics, instances = _run_worker(
        _command("hello", _id(60)), configure=configure
    )
    assert exit_code == 70
    assert events == []
    assert diagnostics == []
    assert responses == [
        {
            "error": {"code": "NATIVE_DATA_INVALID", "nativeCode": 0},
            "id": _id(60),
            "ok": False,
            "v": 1,
        }
    ]
    assert "secret" not in json.dumps(responses)
    assert instances[0].stopped == 1


def test_native_status_mapping_is_stable() -> None:
    cases = {
        -1: "NATIVE_ERROR",
        -2: "NATIVE_ERROR",
        -3: "NATIVE_TIMEOUT",
        -4: "SOCKET_CLOSED",
        -5: "NATIVE_OVERFLOW",
        -6: "FRAME_TOO_LARGE",
        -7: "BINDING_INCOMPATIBLE",
        -8: "CAPACITY_BUSY",
    }
    for status, code in cases.items():
        with pytest.raises(worker.WorkerFailure) as captured:
            worker._raise_native_status(status, -123, "send_bytes")  # type: ignore[attr-defined]
        assert captured.value.code == code
        assert captured.value.native_code == -123
    with pytest.raises(worker.WorkerFailure) as snapshot:
        worker._raise_native_status(-5, 0, "snapshot_nodes")  # type: ignore[attr-defined]
    assert snapshot.value.code == "NODE_SNAPSHOT_OVERFLOW"


def test_native_event_text_reads_only_through_the_bounded_terminator() -> None:
    backend = object.__new__(worker.RealNativeBackend)
    backend._ctypes = ctypes
    value = ctypes.create_string_buffer(b"peer-a", 7)
    assert (
        backend._decode_event_text(
            ctypes.addressof(value), capacity=7, minimum=1, maximum=6
        )
        == "peer-a"
    )
    assert backend._decode_event_text(None, capacity=1, minimum=0, maximum=0) == ""
    unterminated = (ctypes.c_ubyte * 4)(*b"peer")
    with pytest.raises(worker.WorkerFailure, match="NATIVE_EVENT_INVALID"):
        backend._decode_event_text(
            ctypes.addressof(unterminated), capacity=4, minimum=1, maximum=4
        )


def test_output_queue_response_event_fairness_and_reserved_overflow_slot() -> None:
    outputs = worker._OutputQueues()  # type: ignore[attr-defined]
    responses = [f"response-{index}\n".encode() for index in range(9)]
    for raw in responses:
        outputs.enqueue_response(raw)
    event = b"event\n"
    assert outputs.enqueue_event(event)
    observed = [outputs.next() for _ in range(10)]
    assert observed[:8] == responses[:8]
    assert observed[8] == event
    assert observed[9] == responses[8]

    saturated = worker._OutputQueues()  # type: ignore[attr-defined]
    normal = b"x"
    for _ in range(protocol.WORKER_EVENT_CAP - 1):
        assert saturated.enqueue_event(normal)
    assert saturated.enqueue_event(normal) is False
    terminal = b"overflow\n"
    assert saturated.enqueue_event(terminal, terminal=True)
    assert saturated.enqueue_event(terminal, terminal=True)
    saturated.finish()
    drained = []
    while True:
        item = saturated.next()
        if item is worker._STOP:  # type: ignore[attr-defined]
            break
        drained.append(item)
    assert len(drained) == protocol.WORKER_EVENT_CAP
    assert drained[-1] == terminal

    response_full = worker._OutputQueues()  # type: ignore[attr-defined]
    for _ in range(protocol.WORKER_RESPONSE_CAP):
        response_full.enqueue_response(b"r")
    with pytest.raises(
        worker.WorkerFailure, match="WORKER_RESPONSE_CAPACITY_FATAL"
    ):
        response_full.enqueue_response(b"r")

    byte_full = worker._OutputQueues()  # type: ignore[attr-defined]
    normal_budget = (
        protocol.WORKER_EVENT_BYTES_MAX - protocol.IPC_LINE_MAX
    )
    assert byte_full.enqueue_event(b"x" * normal_budget)
    assert byte_full.enqueue_event(b"x") is False
    assert byte_full.enqueue_event(b"overflow\n", terminal=True)


def test_bounded_queue_count_and_byte_capacity_fail_without_blocking() -> None:
    bounded = worker._BoundedQueue(count_cap=1, byte_cap=3)  # type: ignore[attr-defined]
    bounded.put("one", 3)
    with pytest.raises(worker.WorkerFailure, match="WORKER_QUEUE_CAPACITY"):
        bounded.put("two", 1)
    assert bounded.get_nowait() == ("one", 3)
    with pytest.raises(worker.WorkerFailure, match="WORKER_QUEUE_CAPACITY"):
        bounded.put("large", 4)


def test_write_all_retries_eintr_and_partial_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[bytes] = []
    outcomes: deque[int | BaseException] = deque(
        (InterruptedError(), 2, 1, 100)
    )

    def fake_write(_: int, value: memoryview) -> int:
        calls.append(bytes(value))
        outcome = outcomes.popleft()
        if isinstance(outcome, BaseException):
            raise outcome
        return min(outcome, len(value))

    monkeypatch.setattr(worker.os, "write", fake_write)
    worker._write_all(99, b"abcdef")  # type: ignore[attr-defined]
    assert calls == [b"abcdef", b"abcdef", b"cdef", b"def"]


def test_worker_protocol_error_is_terminal_and_does_not_echo_input() -> None:
    raw = b'{"not":"a-command"}\n'
    exit_code, responses, events, diagnostics, instances = _run_worker(raw)
    assert exit_code == 76
    assert events == []
    assert responses == []
    assert diagnostics == [
        {"code": "IPC_PROTOCOL_FATAL", "kind": "worker-diagnostic"}
    ]
    assert instances[0].stopped == 1


def test_importing_worker_does_not_load_ctypes_or_native_modules() -> None:
    script = (
        "import sys; import mclaw.dsoftbus.worker; "
        "assert 'ctypes' not in sys.modules; "
        "assert not any(n.startswith('mclaw.dsoftbus.native') for n in sys.modules)"
    )
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(REPO_ROOT)
    completed = subprocess.run(
        [sys.executable, "-I", "-S", "-c", f"sys_path={str(REPO_ROOT)!r};import sys;sys.path.insert(0,sys_path);{script}"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        check=False,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")
