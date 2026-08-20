# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import base64
from collections import deque
import os
from pathlib import Path
import subprocess
import sys
import threading
from typing import Any, Mapping

import pytest

from mclaw.dsoftbus import protocol
from mclaw.dsoftbus import worker_ipc


EPOCH = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
REPO_ROOT = Path(__file__).resolve().parents[1]


def _id(number: int) -> str:
    return f"{number:08x}-2222-4222-8222-{number:012x}"


def _success(command_id: str, result: Mapping[str, Any]) -> bytes:
    return protocol.encode_ipc_object(
        {"id": command_id, "ok": True, "result": dict(result), "v": 1}
    )


def _error(command_id: str, code: str = "NATIVE_ERROR") -> bytes:
    return protocol.encode_ipc_object(
        {
            "error": {"code": code, "nativeCode": -7},
            "id": command_id,
            "ok": False,
            "v": 1,
        }
    )


def _write_all(state: worker_ipc.WorkerIpcState) -> None:
    writer = worker_ipc.ParentStdinWriter(
        state, write=lambda _descriptor, value: len(value)
    )
    while writer.write_one(99, timeout=0):
        pass


def test_response_routes_have_independent_fixed_size_capacity() -> None:
    state = worker_ipc.WorkerIpcState(EPOCH)
    command_ids = [
        state.admit("hello", {}, command_id=_id(index))
        for index in range(1, 33)
    ]
    health = state.health()
    assert health["parentCommandQueueCount"] == 32
    assert health["parentResponseRouteCount"] == 32
    assert health["parentResponseRouteBytes"] == 32 * protocol.IPC_LINE_MAX
    assert health["parentCommandQueueBytes"] < health["parentResponseRouteBytes"]
    with pytest.raises(worker_ipc.WorkerIpcFailure, match="CAPACITY_BUSY"):
        state.admit("hello", {}, command_id=_id(33))

    assert state.cancel(command_ids[0]) == "canceled"
    health = state.health()
    assert health["parentCommandQueueCount"] == 31
    assert health["parentResponseRouteCount"] == 31
    assert state.admit("hello", {}, command_id=_id(33)) == _id(33)


def test_terminal_records_are_retired_across_a_long_lived_epoch() -> None:
    state = worker_ipc.WorkerIpcState(EPOCH)
    writer = worker_ipc.ParentStdinWriter(
        state, write=lambda _descriptor, value: len(value)
    )
    for index in range(1, 1_001):
        command_id = state.admit("start", {}, command_id=_id(10_000 + index))
        assert writer.write_one(99, timeout=0)
        delivery = state.accept_line(
            _success(command_id, {"nodeEventsStarted": True})
        )
        assert delivery.command_id == command_id
    assert state.health()["parentCommandQueueCount"] == 0
    assert state.health()["parentResponseRouteCount"] == 0
    assert len(state._records) == 0


def test_partial_write_eintr_and_terminal_response_release_once() -> None:
    state = worker_ipc.WorkerIpcState(EPOCH)
    command_id = state.admit("start", {}, command_id=_id(40))
    outcomes: deque[int | BaseException] = deque(
        (InterruptedError(), 1, 2, 4096)
    )

    def write(_descriptor: int, value: bytes | memoryview) -> int:
        outcome = outcomes.popleft()
        if isinstance(outcome, BaseException):
            raise outcome
        return min(outcome, len(value))

    writer = worker_ipc.ParentStdinWriter(state, write=write)
    assert writer.write_one(99, timeout=0)
    record = state.record(command_id)
    assert record["state"] == "WRITTEN"
    assert record["commandReserved"] is False
    assert record["routeReserved"] is True
    assert state.health()["parentCommandQueueCount"] == 0
    assert state.health()["parentResponseRouteCount"] == 1

    delivery = state.accept_line(
        _success(command_id, {"nodeEventsStarted": True})
    )
    assert delivery.kind == "response"
    assert delivery.deliver_to_waiter is True
    assert delivery.outcome_unknown is False
    assert state.health()["parentResponseRouteCount"] == 0
    with pytest.raises(worker_ipc.WorkerIpcFailure, match="WORKER_PROTOCOL_ERROR"):
        state.accept_line(_success(command_id, {"nodeEventsStarted": True}))
    assert state.health()["parentResponseRouteCount"] == 0


def test_response_can_race_writer_finally_after_last_byte() -> None:
    state = worker_ipc.WorkerIpcState(EPOCH)
    command_id = state.admit("start", {}, command_id=_id(45))
    record = state.dequeue(timeout=0)
    assert record is not None
    state.record_write(command_id, len(record.line))
    delivery = state.accept_line(
        _success(command_id, {"nodeEventsStarted": True})
    )
    assert delivery.deliver_to_waiter is True
    assert state.record(command_id)["state"] == "COMPLETED"
    assert state.health()["parentCommandQueueCount"] == 1
    assert state.health()["parentResponseRouteCount"] == 0
    state.finish_write(command_id, success=True)
    assert state.health()["parentCommandQueueCount"] == 0


def test_response_can_arrive_after_kernel_write_before_writer_accounts_bytes() -> None:
    state = worker_ipc.WorkerIpcState(EPOCH)
    command_id = state.admit("start", {}, command_id=_id(46))
    deliveries: list[worker_ipc.IpcDelivery] = []

    def write(_descriptor: int, value: bytes | memoryview) -> int:
        deliveries.append(
            state.accept_line(_success(command_id, {"nodeEventsStarted": True}))
        )
        return len(value)

    writer = worker_ipc.ParentStdinWriter(state, write=write)
    assert writer.write_one(99, timeout=0)
    assert len(deliveries) == 1
    assert deliveries[0].deliver_to_waiter is True
    assert state.health()["parentCommandQueueCount"] == 0
    assert state.health()["parentResponseRouteCount"] == 0
    with pytest.raises(worker_ipc.WorkerIpcFailure, match="COMMAND_NOT_FOUND"):
        state.record(command_id)


def test_queued_cancel_is_tombstoned_and_never_written() -> None:
    state = worker_ipc.WorkerIpcState(EPOCH)
    first = state.admit("hello", {}, command_id=_id(50))
    second = state.admit("start", {}, command_id=_id(51))
    assert state.cancel(first) == "canceled"
    written: list[bytes] = []
    writer = worker_ipc.ParentStdinWriter(
        state,
        write=lambda _descriptor, value: written.append(bytes(value)) or len(value),
    )
    assert writer.write_one(99, timeout=0)
    assert writer.write_one(99, timeout=0) is False
    assert len(written) == 1
    assert b'"op":"start"' in written[0]
    with pytest.raises(worker_ipc.WorkerIpcFailure, match="COMMAND_NOT_FOUND"):
        state.record(first)
    assert state.record(second)["state"] == "WRITTEN"


def test_written_timeout_detaches_waiter_but_late_response_releases_route() -> None:
    state = worker_ipc.WorkerIpcState(EPOCH)
    command_id = state.admit("start", {}, command_id=_id(60))
    _write_all(state)
    assert state.timeout(command_id) == "outcomeUnknown"
    record = state.record(command_id)
    assert record["routeReserved"] is True
    assert record["waiterAttached"] is False
    assert record["outcomeUnknown"] is True

    delivery = state.accept_line(
        _success(command_id, {"nodeEventsStarted": True})
    )
    assert delivery.deliver_to_waiter is False
    assert delivery.outcome_unknown is True
    assert state.health()["parentResponseRouteCount"] == 0


def test_timeout_racing_writer_ownership_is_outcome_unknown() -> None:
    state = worker_ipc.WorkerIpcState(EPOCH)
    command_id = state.admit("start", {}, command_id=_id(65))
    entered_write = threading.Event()
    allow_write = threading.Event()

    def write(_descriptor: int, value: bytes | memoryview) -> int:
        entered_write.set()
        assert allow_write.wait(5)
        return len(value)

    writer = worker_ipc.ParentStdinWriter(state, write=write)
    thread = threading.Thread(target=lambda: writer.write_one(99, timeout=0))
    thread.start()
    assert entered_write.wait(5)
    assert state.record(command_id)["state"] == "WRITING"
    assert state.timeout(command_id) == "outcomeUnknown"
    allow_write.set()
    thread.join(5)
    assert not thread.is_alive()
    delivery = state.accept_line(
        _success(command_id, {"nodeEventsStarted": True})
    )
    assert delivery.deliver_to_waiter is False
    assert delivery.outcome_unknown is True
    health = state.health()
    assert health["parentCommandQueueCount"] == 0
    assert health["parentResponseRouteCount"] == 0


@pytest.mark.parametrize(
    ("first_write", "terminal", "unknown"),
    [(0, "WORKER_DIED", False), (3, "outcomeUnknown", True)],
)
def test_write_failure_releases_all_reservations_and_classifies_outcome(
    first_write: int, terminal: str, unknown: bool
) -> None:
    state = worker_ipc.WorkerIpcState(EPOCH)
    command_id = state.admit("start", {}, command_id=_id(70 + first_write))
    calls = 0

    def write(_descriptor: int, value: bytes | memoryview) -> int:
        nonlocal calls
        calls += 1
        if calls == 1 and first_write:
            return min(first_write, len(value))
        raise OSError("closed")

    writer = worker_ipc.ParentStdinWriter(state, write=write)
    with pytest.raises(worker_ipc.WorkerIpcFailure, match="IPC_WRITE_FAILED"):
        writer.write_one(99, timeout=0)
    record = state.record(command_id)
    assert record["state"] == "EPOCH_FAILED"
    assert record["terminalCode"] == terminal
    assert record["outcomeUnknown"] is unknown
    assert record["commandReserved"] is False
    assert record["routeReserved"] is False
    health = state.health()
    assert health["alive"] is False
    assert health["parentCommandQueueCount"] == 0
    assert health["parentResponseRouteCount"] == 0
    assert health["parentEventCount"] == 0
    assert health["parentEventBytes"] == 0


def test_stop_control_slot_survives_full_business_routes() -> None:
    state = worker_ipc.WorkerIpcState(EPOCH)
    for index in range(1, 33):
        state.admit("hello", {}, command_id=_id(100 + index))
    state.close_business_admission()
    with pytest.raises(worker_ipc.WorkerIpcFailure, match="RUNTIME_STOPPING"):
        state.admit("hello", {}, command_id=_id(200))
    stop_id = state.admit_stop(command_id=_id(201))
    assert state.health()["controlSlotInUse"] is True
    with pytest.raises(worker_ipc.WorkerIpcFailure, match="CONTROL_SLOT_BUSY"):
        state.admit_stop(command_id=_id(202))

    canceled = state.cancel_queued()
    assert len(canceled) == 32
    assert state.health()["parentCommandQueueCount"] == 0
    assert state.health()["parentResponseRouteCount"] == 0

    writer = worker_ipc.ParentStdinWriter(
        state, write=lambda _descriptor, value: len(value)
    )
    assert writer.write_one(99, timeout=0)
    assert writer.write_one(99, timeout=0) is False
    delivery = state.accept_line(_success(stop_id, {"stopped": True}))
    assert delivery.deliver_to_waiter is True
    assert state.health()["controlSlotInUse"] is False


def test_response_error_shape_is_validated_and_releases_route() -> None:
    state = worker_ipc.WorkerIpcState(EPOCH)
    command_id = state.admit("connect", {
        "networkId": "peer-network-a",
        "peerServiceName": protocol.SERVICE_NAME,
        "serviceName": protocol.CLIENT_SERVICE_NAME,
    }, command_id=_id(210))
    _write_all(state)
    delivery = state.accept_line(_error(command_id, "CAPACITY_BUSY"))
    assert delivery.value == {
        "error": {"code": "CAPACITY_BUSY", "nativeCode": -7},
        "id": command_id,
        "ok": False,
        "v": 1,
    }
    assert state.health()["parentResponseRouteCount"] == 0


def test_snapshot_response_exact_page_is_accepted() -> None:
    state = worker_ipc.WorkerIpcState(EPOCH)
    command_id = state.admit("snapshot_nodes", {}, command_id=_id(220))
    _write_all(state)
    result = {
        "nextCursor": "",
        "nodes": [
            {
                "deviceName": "peer-a",
                "deviceTypeId": 17,
                "networkId": "peer-network-a",
            }
        ],
        "replayAfterSeq": 1,
        "replayThroughSeq": 2,
        "snapshotId": _id(221),
    }
    delivery = state.accept_line(_success(command_id, result))
    assert delivery.value is not None
    assert delivery.value["result"] == result


def test_device_manager_results_are_cross_bound_and_raw_ids_are_rejected() -> None:
    state = worker_ipc.WorkerIpcState(EPOCH)
    list_id = state.admit("list_trusted_devices", {}, command_id=_id(223))
    _write_all(state)
    listed = {
        "devices": [
            {
                "deviceIdSha256": "d" * 64,
                "deviceName": "Kaihong B",
                "deviceTypeId": 533,
                "networkId": "peer-network-a",
            }
        ]
    }
    assert state.accept_line(_success(list_id, listed)).value["result"] == listed

    unbind_id = state.admit(
        "unbind_device",
        {"networkId": "peer-network-a"},
        command_id=_id(224),
    )
    _write_all(state)
    result = {"deviceIdSha256": "d" * 64, "unbound": True}
    assert state.accept_line(_success(unbind_id, result)).value["result"] == result

    invalid = worker_ipc.WorkerIpcState(EPOCH)
    invalid_id = invalid.admit("list_trusted_devices", {}, command_id=_id(226))
    _write_all(invalid)
    with pytest.raises(worker_ipc.WorkerIpcFailure, match="WORKER_PROTOCOL_ERROR"):
        invalid.accept_line(
            _success(
                invalid_id,
                {
                    "devices": [
                        {
                            **listed["devices"][0],
                            "deviceId": "raw-system-device-id",
                        }
                    ]
                },
            )
        )


def test_device_discovery_and_bind_results_are_exact_and_cross_bound() -> None:
    state = worker_ipc.WorkerIpcState(EPOCH)
    digest = "e" * 64

    start_id = state.admit(
        "start_device_discovery", {}, command_id=_id(227)
    )
    _write_all(state)
    assert state.accept_line(
        _success(start_id, {"started": True})
    ).value["result"] == {"started": True}

    stop_id = state.admit(
        "stop_device_discovery", {}, command_id=_id(228)
    )
    _write_all(state)
    stopped = {
        "devices": [
            {
                "deviceIdSha256": digest,
                "deviceName": "Kaihong B",
                "deviceTypeId": 533,
            }
        ],
        "failureNativeCode": None,
        "stopped": True,
    }
    assert state.accept_line(_success(stop_id, stopped)).value["result"] == stopped

    begin_id = state.admit(
        "begin_device_bind",
        {"deviceIdSha256": digest},
        command_id=_id(229),
    )
    _write_all(state)
    begun = {"binding": True, "deviceIdSha256": digest}
    assert state.accept_line(_success(begin_id, begun)).value["result"] == begun

    status_id = state.admit(
        "get_device_bind_status",
        {"deviceIdSha256": digest},
        command_id=_id(230),
    )
    _write_all(state)
    status = {"deviceIdSha256": digest, "nativeCode": 0, "status": "pending"}
    assert state.accept_line(_success(status_id, status)).value["result"] == status

    mismatch = worker_ipc.WorkerIpcState(EPOCH)
    mismatch_id = mismatch.admit(
        "get_device_bind_status",
        {"deviceIdSha256": digest},
        command_id=_id(231),
    )
    _write_all(mismatch)
    with pytest.raises(worker_ipc.WorkerIpcFailure, match="WORKER_PROTOCOL_ERROR"):
        mismatch.accept_line(
            _success(
                mismatch_id,
                {
                    "deviceIdSha256": "f" * 64,
                    "nativeCode": 0,
                    "status": "bound",
                },
            )
        )


@pytest.mark.parametrize(
    "result",
    [
        {
            "devices": [
                {
                    "deviceId": "raw-device-id",
                    "deviceIdSha256": "e" * 64,
                    "deviceName": "Kaihong B",
                    "deviceTypeId": 533,
                }
            ],
            "failureNativeCode": None,
            "stopped": True,
        },
        {
            "devices": [],
            "failureNativeCode": 0,
            "stopped": True,
        },
    ],
)
def test_device_discovery_result_rejects_raw_ids_and_ambiguous_zero_failure(
    result: dict[str, object],
) -> None:
    state = worker_ipc.WorkerIpcState(EPOCH)
    command_id = state.admit(
        "stop_device_discovery", {}, command_id=_id(232)
    )
    _write_all(state)
    with pytest.raises(worker_ipc.WorkerIpcFailure, match="WORKER_PROTOCOL_ERROR"):
        state.accept_line(_success(command_id, result))


def test_response_result_must_cross_bind_original_command() -> None:
    state = worker_ipc.WorkerIpcState(EPOCH)
    command_id = state.admit(
        "send_bytes",
        {"data": base64.b64encode(b"abc").decode("ascii"), "socket": 7},
        command_id=_id(225),
    )
    _write_all(state)
    with pytest.raises(worker_ipc.WorkerIpcFailure, match="WORKER_PROTOCOL_ERROR"):
        state.accept_line(_success(command_id, {"sentBytes": 2}))
    assert state.health()["alive"] is False
    assert state.health()["parentResponseRouteCount"] == 0


def _event(epoch: str, sequence: int = 1) -> bytes:
    return protocol.encode_ipc_object(
        {
            "data": {
                "deviceName": "peer-a",
                "deviceTypeId": 17,
                "networkId": "peer-network-a",
                "nodeEventSeq": sequence,
            },
            "event": "node-online",
            "v": 1,
            "workerEpoch": epoch,
        }
    )


def test_old_epoch_event_is_quarantined_without_capacity_use() -> None:
    state = worker_ipc.WorkerIpcState(EPOCH)
    delivery = state.accept_line(
        _event("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
    )
    assert delivery.kind == "quarantined"
    assert state.health()["parentEventCount"] == 0


def test_event_queue_is_bounded_and_overflow_fails_entire_epoch() -> None:
    state = worker_ipc.WorkerIpcState(EPOCH)
    command_id = state.admit("hello", {}, command_id=_id(230))
    for sequence in range(1, protocol.PARENT_EVENT_CAP + 1):
        delivery = state.accept_line(_event(EPOCH, sequence))
        assert delivery.kind == "event"
    assert state.health()["parentEventCount"] == protocol.PARENT_EVENT_CAP
    with pytest.raises(
        worker_ipc.WorkerIpcFailure, match="PARENT_EVENT_CAPACITY_FATAL"
    ):
        state.accept_line(_event(EPOCH, protocol.PARENT_EVENT_CAP + 1))
    health = state.health()
    assert health["alive"] is False
    assert health["parentCommandQueueCount"] == 0
    assert health["parentResponseRouteCount"] == 0
    assert health["parentEventCount"] == 0
    assert health["parentEventBytes"] == 0
    assert state.record(command_id)["state"] == "EPOCH_FAILED"


def test_event_pop_releases_actual_bytes() -> None:
    state = worker_ipc.WorkerIpcState(EPOCH)
    raw = protocol.encode_ipc_object(
        {
            "data": {
                "data": base64.b64encode(b"payload").decode("ascii"),
                "socket": 7,
            },
            "event": "bytes",
            "v": 1,
            "workerEpoch": EPOCH,
        }
    )
    state.accept_line(raw)
    assert state.health()["parentEventBytes"] == len(raw)
    event = state.pop_event()
    assert event is not None and event["event"] == "bytes"
    assert state.health()["parentEventBytes"] == 0
    assert state.pop_event() is None


def test_terminal_event_is_delivered_once_and_atomically_fails_epoch() -> None:
    state = worker_ipc.WorkerIpcState(EPOCH)
    command_id = state.admit("start", {}, command_id=_id(240))
    _write_all(state)
    raw = protocol.encode_ipc_object(
        {
            "data": {"droppedBytes": 1024, "droppedCount": 1},
            "event": "overflow",
            "v": 1,
            "workerEpoch": EPOCH,
        }
    )
    delivery = state.accept_line(raw)
    assert delivery.kind == "event"
    assert delivery.value is not None
    assert delivery.value["event"] == "overflow"
    health = state.health()
    assert health["alive"] is False
    assert health["parentResponseRouteCount"] == 0
    assert health["parentEventCount"] == 0
    assert state.record(command_id)["outcomeUnknown"] is True


@pytest.mark.parametrize(
    "raw",
    [
        b'{"event":"bytes"}\n',
        protocol.encode_ipc_object(
            {
                "data": {"data": "***", "socket": 1},
                "event": "bytes",
                "v": 1,
                "workerEpoch": EPOCH,
            }
        ),
    ],
)
def test_malformed_worker_line_is_rejected(raw: bytes) -> None:
    state = worker_ipc.WorkerIpcState(EPOCH)
    with pytest.raises(worker_ipc.WorkerIpcFailure, match="WORKER_PROTOCOL_ERROR"):
        state.accept_line(raw)
    assert state.health()["alive"] is False


def test_importing_parent_ipc_does_not_load_ctypes_or_native_modules() -> None:
    script = (
        f"import sys;sys.path.insert(0,{str(REPO_ROOT)!r});"
        "import mclaw.dsoftbus.worker_ipc;"
        "assert 'ctypes' not in sys.modules;"
        "assert not any(n.startswith('mclaw.dsoftbus.native') for n in sys.modules)"
    )
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [sys.executable, "-I", "-S", "-c", script],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        check=False,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")
