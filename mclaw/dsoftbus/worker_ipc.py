# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Bounded parent-side IPC state for one isolated DSoftBus Worker epoch.

The module owns no subprocess and opens no endpoint.  It provides the exact
reservation and command lifecycle used by the later Runtime supervisor: one
stdin writer, independent command/response-route accounting, queued
cancellation, outcome-unknown fencing after write ownership transfers, and a
bounded event handoff.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import os
import threading
from types import MappingProxyType
from typing import Any, Callable, Mapping, NoReturn
import uuid

from . import protocol


_QUEUED = "QUEUED"
_WRITING = "WRITING"
_WRITTEN = "WRITTEN"
_CANCELED = "CANCELED"
_COMPLETED = "COMPLETED"
_EPOCH_FAILED = "EPOCH_FAILED"


class WorkerIpcFailure(RuntimeError):
    """Stable parent-side Worker IPC failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _fail(code: str) -> NoReturn:
    raise WorkerIpcFailure(code)


@dataclass(frozen=True, slots=True)
class IpcDelivery:
    kind: str
    value: Mapping[str, Any] | None
    command_id: str | None = None
    deliver_to_waiter: bool = False
    outcome_unknown: bool = False


@dataclass(slots=True)
class _CommandRecord:
    command_id: str
    operation: str
    args: Mapping[str, Any]
    line: bytes
    worker_epoch: str
    control: bool
    state: str = _QUEUED
    bytes_written: int = 0
    command_reserved: bool = True
    route_reserved: bool = True
    waiter_attached: bool = True
    outcome_unknown: bool = False
    terminal_code: str = ""


def _stable_code(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 64
        or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ_" for character in value)
    ):
        raise protocol.ProtocolError("INVALID_REQUEST", f"{label} is invalid")
    return value


def _validate_identity(value: Any) -> None:
    identity = protocol.exact_object(
        value,
        frozenset(
            {
                "capabilitySet",
                "distributedDataSyncGranted",
                "gid",
                "selinuxDomain",
                "supplementaryGids",
                "tokenIdHash",
                "uid",
            }
        ),
        "hello.result.identity",
    )
    protocol.bounded_integer(identity["uid"], "identity.uid", 0, 2**32 - 1)
    protocol.bounded_integer(identity["gid"], "identity.gid", 0, 2**32 - 1)
    if identity["distributedDataSyncGranted"] is not True:
        raise protocol.ProtocolError(
            "INVALID_REQUEST", "identity permission must be true"
        )
    groups = identity["supplementaryGids"]
    if not isinstance(groups, list) or len(groups) > 256:
        raise protocol.ProtocolError("INVALID_REQUEST", "identity groups invalid")
    normalized_groups = [
        protocol.bounded_integer(group, "identity group", 0, 2**32 - 1)
        for group in groups
    ]
    if normalized_groups != sorted(set(normalized_groups)):
        raise protocol.ProtocolError("INVALID_REQUEST", "identity groups invalid")
    capabilities = identity["capabilitySet"]
    if not isinstance(capabilities, list) or len(capabilities) > 256:
        raise protocol.ProtocolError(
            "INVALID_REQUEST", "identity capabilities invalid"
        )
    normalized_capabilities = [
        protocol.bounded_utf8(capability, "identity capability", 1, 64)
        for capability in capabilities
    ]
    if len(normalized_capabilities) != len(set(normalized_capabilities)):
        raise protocol.ProtocolError(
            "INVALID_REQUEST", "identity capabilities invalid"
        )
    token_hash = identity["tokenIdHash"]
    if (
        not isinstance(token_hash, str)
        or len(token_hash) != 71
        or not token_hash.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in token_hash[7:])
    ):
        raise protocol.ProtocolError("INVALID_REQUEST", "identity token hash invalid")
    protocol.bounded_utf8(identity["selinuxDomain"], "identity domain", 1, 256)


def _validate_snapshot_result(value: Any) -> None:
    result = protocol.exact_object(
        value,
        frozenset(
            {
                "nextCursor",
                "nodes",
                "replayAfterSeq",
                "replayThroughSeq",
                "snapshotId",
            }
        ),
        "snapshot_nodes.result",
    )
    protocol.canonical_uuid4(result["snapshotId"], "snapshotId")
    after = protocol.bounded_integer(
        result["replayAfterSeq"], "replayAfterSeq", 0, 2**64 - 1
    )
    through = protocol.bounded_integer(
        result["replayThroughSeq"], "replayThroughSeq", 0, 2**64 - 1
    )
    if after > through:
        raise protocol.ProtocolError("INVALID_REQUEST", "snapshot watermarks invalid")
    cursor = result["nextCursor"]
    if cursor != "":
        protocol.bounded_utf8(cursor, "nextCursor", 1, 1_024)
    nodes = result["nodes"]
    if not isinstance(nodes, list) or len(nodes) > protocol.NODE_SNAPSHOT_PAGE_MAX:
        raise protocol.ProtocolError("INVALID_REQUEST", "snapshot page count invalid")
    for value_node in nodes:
        node = protocol.exact_object(
            value_node,
            frozenset({"deviceName", "deviceTypeId", "networkId"}),
            "snapshot node",
        )
        protocol.bounded_utf8(node["networkId"], "networkId", 1, 64)
        protocol.bounded_utf8(node["deviceName"], "deviceName", 0, 127)
        protocol.bounded_integer(node["deviceTypeId"], "deviceTypeId", 0, 2**16 - 1)
    if len(protocol.canonical_json_bytes(result)) > protocol.NODE_SNAPSHOT_PAGE_BYTES_MAX:
        raise protocol.ProtocolError("INVALID_REQUEST", "snapshot page bytes invalid")


def _validate_trusted_device(value: Any, label: str) -> None:
    device = protocol.exact_object(
        value,
        frozenset(
            {
                "deviceIdSha256",
                "deviceName",
                "deviceTypeId",
                "networkId",
            }
        ),
        label,
    )
    digest = device["deviceIdSha256"]
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise protocol.ProtocolError(
            "INVALID_REQUEST", f"{label}.deviceIdSha256 is invalid"
        )
    protocol.bounded_utf8(device["deviceName"], f"{label}.deviceName", 0, 127)
    protocol.bounded_integer(
        device["deviceTypeId"], f"{label}.deviceTypeId", 0, 2**16 - 1
    )
    protocol.bounded_utf8(device["networkId"], f"{label}.networkId", 1, 96)


def _validate_device_digest(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise protocol.ProtocolError("INVALID_REQUEST", f"{label} is invalid")
    return value


def _validate_discovered_device(value: Any, label: str) -> None:
    device = protocol.exact_object(
        value,
        frozenset(
            {
                "deviceIdSha256",
                "deviceName",
                "deviceTypeId",
                "networkIdSha256",
                "publicDeviceId",
            }
        ),
        label,
    )
    _validate_device_digest(device["deviceIdSha256"], f"{label}.deviceIdSha256")
    network_digest = device["networkIdSha256"]
    if network_digest != "":
        _validate_device_digest(network_digest, f"{label}.networkIdSha256")
    public_device_id = device["publicDeviceId"]
    if public_device_id != "":
        prefix = "urn:mclaw:device:oh:"
        if (
            not isinstance(public_device_id, str)
            or not public_device_id.startswith(prefix)
            or len(public_device_id) != len(prefix) + 64
            or any(
                character not in "0123456789abcdef"
                for character in public_device_id[len(prefix) :]
            )
        ):
            raise protocol.ProtocolError(
                "INVALID_REQUEST", f"{label}.publicDeviceId is invalid"
            )
    protocol.bounded_utf8(device["deviceName"], f"{label}.deviceName", 0, 127)
    protocol.bounded_integer(
        device["deviceTypeId"], f"{label}.deviceTypeId", 0, 2**16 - 1
    )


def _validate_success_result(operation: str, value: Any) -> None:
    if operation == "hello":
        result = protocol.exact_object(
            value,
            frozenset(
                {
                    "identity",
                    "localUdid",
                    "nativeAbiVersion",
                    "socketCap",
                    "workerEpoch",
                }
            ),
            "hello.result",
        )
        _validate_identity(result["identity"])
        protocol.bounded_utf8(result["localUdid"], "localUdid", 1, 64)
        if result["nativeAbiVersion"] != protocol.NATIVE_ABI_VERSION:
            raise protocol.ProtocolError("INVALID_REQUEST", "Native ABI invalid")
        protocol.bounded_integer(result["socketCap"], "socketCap", 1, 2**32 - 1)
        protocol.canonical_uuid4(result["workerEpoch"], "workerEpoch")
        return
    if operation == "start":
        result = protocol.exact_object(
            value, frozenset({"nodeEventsStarted"}), "start.result"
        )
        if result["nodeEventsStarted"] is not True:
            raise protocol.ProtocolError("INVALID_REQUEST", "start result invalid")
        return
    if operation == "snapshot_nodes":
        _validate_snapshot_result(value)
        return
    if operation == "get_node_udid":
        result = protocol.exact_object(
            value, frozenset({"udid"}), "get_node_udid.result"
        )
        protocol.bounded_utf8(result["udid"], "udid", 1, 64)
        return
    if operation == "start_device_discovery":
        result = protocol.exact_object(
            value, frozenset({"started"}), "start_device_discovery.result"
        )
        if result["started"] is not True:
            raise protocol.ProtocolError(
                "INVALID_REQUEST", "start device discovery result invalid"
            )
        return
    if operation == "stop_device_discovery":
        result = protocol.exact_object(
            value,
            frozenset({"devices", "failureNativeCode", "stopped"}),
            "stop_device_discovery.result",
        )
        devices = result["devices"]
        if not isinstance(devices, list) or len(devices) > protocol.NODE_SNAPSHOT_MAX:
            raise protocol.ProtocolError(
                "INVALID_REQUEST", "discovered device count invalid"
            )
        digests: set[str] = set()
        for index, device in enumerate(devices):
            label = f"discovered device {index}"
            _validate_discovered_device(device, label)
            digest = device["deviceIdSha256"]
            if digest in digests:
                raise protocol.ProtocolError(
                    "INVALID_REQUEST", "discovered device digest duplicated"
                )
            digests.add(digest)
        failure_native_code = result["failureNativeCode"]
        if failure_native_code is not None:
            protocol.bounded_integer(
                failure_native_code,
                "failureNativeCode",
                -(2**31),
                2**31 - 1,
            )
            if failure_native_code == 0:
                raise protocol.ProtocolError(
                    "INVALID_REQUEST", "failureNativeCode must be null or nonzero"
                )
        if result["stopped"] is not True:
            raise protocol.ProtocolError(
                "INVALID_REQUEST", "stop device discovery result invalid"
            )
        return
    if operation == "begin_device_bind":
        result = protocol.exact_object(
            value,
            frozenset({"binding", "deviceIdSha256"}),
            "begin_device_bind.result",
        )
        _validate_device_digest(result["deviceIdSha256"], "deviceIdSha256")
        if result["binding"] is not True:
            raise protocol.ProtocolError(
                "INVALID_REQUEST", "begin device bind result invalid"
            )
        return
    if operation == "get_device_bind_status":
        result = protocol.exact_object(
            value,
            frozenset({"deviceIdSha256", "nativeCode", "status"}),
            "get_device_bind_status.result",
        )
        _validate_device_digest(result["deviceIdSha256"], "deviceIdSha256")
        status = result["status"]
        if status not in {"pending", "bound", "failed"}:
            raise protocol.ProtocolError("INVALID_REQUEST", "bind status invalid")
        native_code = protocol.bounded_integer(
            result["nativeCode"], "nativeCode", -(2**31), 2**31 - 1
        )
        if (status in {"pending", "bound"} and native_code != 0) or (
            status == "failed" and native_code == 0
        ):
            raise protocol.ProtocolError(
                "INVALID_REQUEST", "bind status/nativeCode mismatch"
            )
        return
    if operation == "list_trusted_devices":
        result = protocol.exact_object(
            value, frozenset({"devices"}), "list_trusted_devices.result"
        )
        devices = result["devices"]
        if not isinstance(devices, list) or len(devices) > protocol.NODE_SNAPSHOT_MAX:
            raise protocol.ProtocolError(
                "INVALID_REQUEST", "trusted device count invalid"
            )
        for index, device in enumerate(devices):
            _validate_trusted_device(device, f"trusted device {index}")
        return
    if operation == "unbind_device":
        result = protocol.exact_object(
            value,
            frozenset({"deviceIdSha256", "unbound"}),
            "unbind_device.result",
        )
        _validate_device_digest(result["deviceIdSha256"], "deviceIdSha256")
        if result["unbound"] is not True:
            raise protocol.ProtocolError("INVALID_REQUEST", "unbind result invalid")
        return
    if operation == "listen":
        result = protocol.exact_object(
            value, frozenset({"socket"}), "listen.result"
        )
        protocol.bounded_integer(result["socket"], "socket", 0, 2**31 - 1)
        return
    if operation == "connect":
        result = protocol.exact_object(
            value, frozenset({"mtu", "socket"}), "connect.result"
        )
        protocol.bounded_integer(result["socket"], "socket", 0, 2**31 - 1)
        protocol.bounded_integer(result["mtu"], "mtu", 1, 2**32 - 1)
        return
    if operation == "send_bytes":
        result = protocol.exact_object(
            value, frozenset({"sentBytes"}), "send_bytes.result"
        )
        protocol.bounded_integer(
            result["sentBytes"], "sentBytes", 1, protocol.REMOTE_FRAME_MAX
        )
        return
    if operation == "close_socket":
        result = protocol.exact_object(
            value, frozenset({"closed"}), "close_socket.result"
        )
        if result["closed"] is not True:
            raise protocol.ProtocolError("INVALID_REQUEST", "close result invalid")
        return
    if operation == "stop":
        result = protocol.exact_object(
            value, frozenset({"stopped"}), "stop.result"
        )
        if result["stopped"] is not True:
            raise protocol.ProtocolError("INVALID_REQUEST", "stop result invalid")
        return
    raise protocol.ProtocolError("INVALID_REQUEST", "unknown response operation")


def _validate_response(value: Any, operation: str) -> Mapping[str, Any]:
    if not isinstance(value, dict) or type(value.get("ok")) is not bool:
        raise protocol.ProtocolError("INVALID_REQUEST", "response shape invalid")
    if value["ok"] is True:
        response = protocol.exact_object(
            value, frozenset({"id", "ok", "result", "v"}), "response"
        )
        _validate_success_result(operation, response["result"])
    else:
        response = protocol.exact_object(
            value, frozenset({"error", "id", "ok", "v"}), "response"
        )
        error = protocol.exact_object(
            response["error"], frozenset({"code", "nativeCode"}), "response.error"
        )
        _stable_code(error["code"], "response.error.code")
        protocol.bounded_integer(
            error["nativeCode"], "response.error.nativeCode", -(2**31), 2**31 - 1
        )
    if response["v"] != 1:
        raise protocol.ProtocolError("INVALID_REQUEST", "response version invalid")
    protocol.canonical_uuid4(response["id"], "response.id")
    return MappingProxyType(response)


def _validate_event(value: Any) -> Mapping[str, Any]:
    event = protocol.exact_object(
        value, frozenset({"data", "event", "v", "workerEpoch"}), "event"
    )
    if event["v"] != 1:
        raise protocol.ProtocolError("INVALID_REQUEST", "event version invalid")
    protocol.canonical_uuid4(event["workerEpoch"], "event.workerEpoch")
    event_type = event["event"]
    if event_type not in {
        "node-online",
        "node-offline",
        "bound",
        "bytes",
        "closed",
        "overflow",
        "fatal",
    }:
        raise protocol.ProtocolError("INVALID_REQUEST", "event type invalid")
    data = event["data"]
    if event_type in {"node-online", "node-offline"}:
        fields = protocol.exact_object(
            data,
            frozenset({"deviceName", "deviceTypeId", "networkId", "nodeEventSeq"}),
            "node event",
        )
        protocol.bounded_utf8(fields["networkId"], "networkId", 1, 64)
        protocol.bounded_utf8(fields["deviceName"], "deviceName", 0, 127)
        protocol.bounded_integer(fields["deviceTypeId"], "deviceTypeId", 0, 2**16 - 1)
        protocol.bounded_integer(fields["nodeEventSeq"], "nodeEventSeq", 1, 2**64 - 1)
    elif event_type == "bound":
        fields = protocol.exact_object(
            data, frozenset({"mtu", "networkId", "socket"}), "bound event"
        )
        protocol.bounded_integer(fields["socket"], "socket", 0, 2**31 - 1)
        protocol.bounded_integer(fields["mtu"], "mtu", 1, 2**32 - 1)
        protocol.bounded_utf8(fields["networkId"], "networkId", 1, 64)
    elif event_type == "bytes":
        fields = protocol.exact_object(
            data, frozenset({"data", "socket"}), "bytes event"
        )
        protocol.bounded_integer(fields["socket"], "socket", 0, 2**31 - 1)
        protocol.decode_strict_base64(fields["data"], maximum=protocol.REMOTE_FRAME_MAX)
    elif event_type == "overflow":
        fields = protocol.exact_object(
            data, frozenset({"droppedBytes", "droppedCount"}), "overflow event"
        )
        protocol.bounded_integer(fields["droppedCount"], "droppedCount", 1, 2**32 - 1)
        protocol.bounded_integer(fields["droppedBytes"], "droppedBytes", 0, 2**64 - 1)
    else:
        if not isinstance(data, dict):
            raise protocol.ProtocolError("INVALID_REQUEST", "terminal event invalid")
        scope = data.get("scope")
        expected = (
            frozenset({"code", "nativeCode", "scope", "socket"})
            if scope == "socket"
            else frozenset({"code", "nativeCode", "scope"})
        )
        fields = protocol.exact_object(data, expected, "terminal event")
        if scope not in {"socket", "epoch"}:
            raise protocol.ProtocolError("INVALID_REQUEST", "event scope invalid")
        if scope == "socket":
            protocol.bounded_integer(fields["socket"], "socket", 0, 2**31 - 1)
        if event_type == "closed" and scope != "socket":
            raise protocol.ProtocolError("INVALID_REQUEST", "closed scope invalid")
        _stable_code(fields["code"], "event code")
        protocol.bounded_integer(
            fields["nativeCode"], "nativeCode", -(2**31), 2**31 - 1
        )
    return MappingProxyType(event)


class WorkerIpcState:
    """Thread-safe reservation and routing state for one Worker epoch."""

    def __init__(self, worker_epoch: str) -> None:
        protocol.canonical_uuid4(worker_epoch, "workerEpoch")
        self._epoch = worker_epoch
        self._condition = threading.Condition()
        self._queue: deque[str] = deque()
        self._records: dict[str, _CommandRecord] = {}
        self._events: deque[tuple[bytes, Mapping[str, Any]]] = deque()
        self._command_count = 0
        self._command_bytes = 0
        self._route_count = 0
        self._route_bytes = 0
        self._event_count = 0
        self._event_bytes = 0
        self._control_in_use = False
        self._business_open = True
        self._alive = True

    @property
    def worker_epoch(self) -> str:
        return self._epoch

    def close_business_admission(self) -> None:
        with self._condition:
            self._business_open = False

    def _reserve_normal(self, line_length: int) -> None:
        if (
            self._command_count >= protocol.PARENT_COMMAND_QUEUE_MAX
            or self._command_bytes
            > protocol.PARENT_COMMAND_QUEUE_BYTES_MAX - line_length
            or self._route_count >= protocol.PARENT_RESPONSE_ROUTE_MAX
            or self._route_bytes
            > protocol.PARENT_RESPONSE_ROUTE_BYTES_MAX - protocol.IPC_LINE_MAX
        ):
            _fail("CAPACITY_BUSY")
        self._command_count += 1
        self._command_bytes += line_length
        self._route_count += 1
        self._route_bytes += protocol.IPC_LINE_MAX

    def admit(
        self,
        operation: str,
        args: Mapping[str, Any],
        *,
        command_id: str | None = None,
    ) -> str:
        if command_id is None:
            command_id = str(uuid.uuid4())
        line = protocol.encode_worker_command(
            operation, args, command_id=command_id
        )
        with self._condition:
            if not self._alive:
                _fail("WORKER_DIED")
            if not self._business_open:
                _fail("RUNTIME_STOPPING")
            if command_id in self._records:
                _fail("DUPLICATE_COMMAND_ID")
            self._reserve_normal(len(line))
            self._records[command_id] = _CommandRecord(
                command_id=command_id,
                operation=operation,
                args=MappingProxyType(dict(args)),
                line=line,
                worker_epoch=self._epoch,
                control=False,
            )
            self._queue.append(command_id)
            self._condition.notify()
            return command_id

    def admit_stop(self, *, command_id: str | None = None) -> str:
        if command_id is None:
            command_id = str(uuid.uuid4())
        line = protocol.encode_worker_command("stop", {}, command_id=command_id)
        with self._condition:
            if not self._alive:
                _fail("WORKER_DIED")
            if self._business_open:
                _fail("BUSINESS_ADMISSION_OPEN")
            if self._control_in_use:
                _fail("CONTROL_SLOT_BUSY")
            if command_id in self._records:
                _fail("DUPLICATE_COMMAND_ID")
            self._control_in_use = True
            self._records[command_id] = _CommandRecord(
                command_id=command_id,
                operation="stop",
                args=MappingProxyType({}),
                line=line,
                worker_epoch=self._epoch,
                control=True,
            )
            self._queue.append(command_id)
            self._condition.notify()
            return command_id

    def dequeue(self, timeout: float | None = None) -> _CommandRecord | None:
        with self._condition:
            if timeout is not None and timeout < 0:
                raise ValueError("timeout must be nonnegative")
            if not self._queue and self._alive:
                self._condition.wait(timeout)
            while self._queue:
                command_id = self._queue.popleft()
                record = self._records[command_id]
                if record.state == _CANCELED:
                    self._retire_if_done_locked(record)
                    continue
                if record.state != _QUEUED:
                    _fail("IPC_STATE_CORRUPT")
                record.state = _WRITING
                return record
            return None

    def record_write(self, command_id: str, written: int) -> None:
        with self._condition:
            record = self._records.get(command_id)
            if (
                record is None
                or record.state not in {_WRITING, _COMPLETED}
                or type(written) is not int
                or written <= 0
                or record.bytes_written > len(record.line) - written
            ):
                _fail("IPC_STATE_CORRUPT")
            record.bytes_written += written
            if record.bytes_written == len(record.line) and record.state == _WRITING:
                record.state = _WRITTEN

    def _release_command_locked(self, record: _CommandRecord) -> None:
        if not record.command_reserved:
            return
        record.command_reserved = False
        if not record.control:
            self._command_count -= 1
            self._command_bytes -= len(record.line)

    def _release_route_locked(self, record: _CommandRecord) -> None:
        if not record.route_reserved:
            return
        record.route_reserved = False
        if record.control:
            self._control_in_use = False
        else:
            self._route_count -= 1
            self._route_bytes -= protocol.IPC_LINE_MAX

    def _retire_if_done_locked(self, record: _CommandRecord) -> None:
        if (
            record.state in {_CANCELED, _COMPLETED}
            and not record.command_reserved
            and not record.route_reserved
        ):
            if self._records.get(record.command_id) is not record:
                _fail("IPC_STATE_CORRUPT")
            del self._records[record.command_id]

    def finish_write(self, command_id: str, *, success: bool) -> None:
        with self._condition:
            record = self._records.get(command_id)
            if record is None or record.state not in {
                _WRITING,
                _WRITTEN,
                _COMPLETED,
            }:
                _fail("IPC_STATE_CORRUPT")
            if success and record.bytes_written != len(record.line):
                _fail("IPC_PARTIAL_WRITE")
            self._release_command_locked(record)
            self._retire_if_done_locked(record)

    def cancel(self, command_id: str) -> str:
        with self._condition:
            record = self._records.get(command_id)
            if record is None:
                _fail("COMMAND_NOT_FOUND")
            if record.state == _QUEUED:
                record.state = _CANCELED
                record.waiter_attached = False
                record.terminal_code = "CANCELED"
                self._release_command_locked(record)
                self._release_route_locked(record)
                try:
                    self._queue.remove(command_id)
                except ValueError:
                    _fail("IPC_STATE_CORRUPT")
                self._retire_if_done_locked(record)
                return "canceled"
            if record.state in {_WRITING, _WRITTEN}:
                record.waiter_attached = False
                record.outcome_unknown = True
                return "outcomeUnknown"
            return "outcomeUnknown" if record.outcome_unknown else record.state

    def timeout(self, command_id: str) -> str:
        """Detach a timed-out caller using the same write-state fence as cancel."""

        return self.cancel(command_id)

    def cancel_queued(self) -> tuple[str, ...]:
        canceled: list[str] = []
        with self._condition:
            retained: deque[str] = deque()
            for command_id in self._queue:
                record = self._records[command_id]
                if record.state != _QUEUED or record.control:
                    retained.append(command_id)
                    continue
                record.state = _CANCELED
                record.waiter_attached = False
                record.terminal_code = "CANCELED"
                self._release_command_locked(record)
                self._release_route_locked(record)
                canceled.append(command_id)
                self._retire_if_done_locked(record)
            self._queue = retained
            self._condition.notify_all()
        return tuple(canceled)

    def _fail_epoch_locked(self) -> None:
        if not self._alive:
            return
        self._alive = False
        self._business_open = False
        for record in self._records.values():
            if record.state in {_QUEUED, _WRITING, _WRITTEN}:
                record.outcome_unknown = (
                    record.bytes_written > 0 or record.state == _WRITTEN
                )
                record.waiter_attached = False
                record.terminal_code = (
                    "outcomeUnknown" if record.outcome_unknown else "WORKER_DIED"
                )
                record.state = _EPOCH_FAILED
            self._release_command_locked(record)
            self._release_route_locked(record)
        self._queue.clear()
        self._events.clear()
        self._event_count = 0
        self._event_bytes = 0
        self._condition.notify_all()

    def fail_epoch(self) -> None:
        with self._condition:
            self._fail_epoch_locked()

    def accept_line(self, raw: bytes) -> IpcDelivery:
        try:
            value = protocol.strict_json_loads(
                raw,
                max_bytes=protocol.IPC_LINE_MAX,
                require_canonical=True,
                require_object=True,
            )
        except protocol.ProtocolError:
            self.fail_epoch()
            _fail("WORKER_PROTOCOL_ERROR")
        if "id" in value:
            try:
                command_id = protocol.canonical_uuid4(
                    value["id"], "response.id"
                )
            except protocol.ProtocolError:
                self.fail_epoch()
                _fail("WORKER_PROTOCOL_ERROR")
            with self._condition:
                record = self._records.get(command_id)
                if (
                    record is None
                    or not record.route_reserved
                    or record.state not in {_WRITING, _WRITTEN}
                ):
                    self._fail_epoch_locked()
                    _fail("WORKER_PROTOCOL_ERROR")
                try:
                    validated = _validate_response(value, record.operation)
                except protocol.ProtocolError:
                    self._fail_epoch_locked()
                    _fail("WORKER_PROTOCOL_ERROR")
                if (
                    record.operation == "hello"
                    and validated["ok"] is True
                    and validated["result"]["workerEpoch"] != self._epoch
                ):
                    self._fail_epoch_locked()
                    _fail("WORKER_PROTOCOL_ERROR")
                if validated["ok"] is True and record.operation == "send_bytes":
                    expected_sent = len(
                        protocol.decode_strict_base64(
                            record.args["data"], maximum=protocol.REMOTE_FRAME_MAX
                        )
                    )
                    if validated["result"]["sentBytes"] != expected_sent:
                        self._fail_epoch_locked()
                        _fail("WORKER_PROTOCOL_ERROR")
                if (
                    validated["ok"] is True
                    and record.operation == "snapshot_nodes"
                    and record.args
                    and validated["result"]["snapshotId"]
                    != record.args["snapshotId"]
                ):
                    self._fail_epoch_locked()
                    _fail("WORKER_PROTOCOL_ERROR")
                if (
                    validated["ok"] is True
                    and record.operation
                    in {"begin_device_bind", "get_device_bind_status"}
                    and validated["result"]["deviceIdSha256"]
                    != record.args["deviceIdSha256"]
                ):
                    self._fail_epoch_locked()
                    _fail("WORKER_PROTOCOL_ERROR")
                deliver = record.waiter_attached
                outcome_unknown = record.outcome_unknown
                record.state = _COMPLETED
                record.terminal_code = ""
                self._release_route_locked(record)
                self._retire_if_done_locked(record)
                return IpcDelivery(
                    kind="response",
                    value=validated,
                    command_id=command_id,
                    deliver_to_waiter=deliver,
                    outcome_unknown=outcome_unknown,
                )
        if "event" in value:
            try:
                validated_event = _validate_event(value)
            except protocol.ProtocolError:
                self.fail_epoch()
                _fail("WORKER_PROTOCOL_ERROR")
            if validated_event["workerEpoch"] != self._epoch:
                return IpcDelivery(kind="quarantined", value=None)
            with self._condition:
                if not self._alive:
                    return IpcDelivery(kind="quarantined", value=None)
                if validated_event["event"] in {"overflow", "fatal"}:
                    self._fail_epoch_locked()
                    return IpcDelivery(kind="event", value=validated_event)
                if (
                    self._event_count >= protocol.PARENT_EVENT_CAP
                    or self._event_bytes
                    > protocol.PARENT_EVENT_BYTES_MAX - len(raw)
                ):
                    self._fail_epoch_locked()
                    _fail("PARENT_EVENT_CAPACITY_FATAL")
                self._events.append((raw, validated_event))
                self._event_count += 1
                self._event_bytes += len(raw)
                return IpcDelivery(kind="event", value=validated_event)
        self.fail_epoch()
        _fail("WORKER_PROTOCOL_ERROR")

    def pop_event(self) -> Mapping[str, Any] | None:
        with self._condition:
            if not self._events:
                return None
            raw, value = self._events.popleft()
            self._event_count -= 1
            self._event_bytes -= len(raw)
            return value

    def record(self, command_id: str) -> Mapping[str, Any]:
        with self._condition:
            record = self._records.get(command_id)
            if record is None:
                _fail("COMMAND_NOT_FOUND")
            return MappingProxyType(
                {
                    "bytesWritten": record.bytes_written,
                    "commandReserved": record.command_reserved,
                    "control": record.control,
                    "outcomeUnknown": record.outcome_unknown,
                    "routeReserved": record.route_reserved,
                    "state": record.state,
                    "terminalCode": record.terminal_code,
                    "waiterAttached": record.waiter_attached,
                }
            )

    def health(self) -> Mapping[str, Any]:
        with self._condition:
            if min(
                self._command_count,
                self._command_bytes,
                self._route_count,
                self._route_bytes,
                self._event_count,
                self._event_bytes,
            ) < 0:
                _fail("IPC_STATE_CORRUPT")
            if (
                self._route_bytes != self._route_count * protocol.IPC_LINE_MAX
                or (
                    self._alive
                    and len(self._records)
                    > self._command_count + self._route_count + 1
                )
                or len(self._queue) != len(set(self._queue))
                or any(command_id not in self._records for command_id in self._queue)
                or self._command_count > protocol.PARENT_COMMAND_QUEUE_MAX
                or self._command_bytes > protocol.PARENT_COMMAND_QUEUE_BYTES_MAX
                or self._route_count > protocol.PARENT_RESPONSE_ROUTE_MAX
                or self._route_bytes > protocol.PARENT_RESPONSE_ROUTE_BYTES_MAX
                or self._event_count > protocol.PARENT_EVENT_CAP
                or self._event_bytes > protocol.PARENT_EVENT_BYTES_MAX
            ):
                _fail("IPC_STATE_CORRUPT")
            return MappingProxyType(
                {
                    "alive": self._alive,
                    "businessAdmissionOpen": self._business_open,
                    "controlSlotInUse": self._control_in_use,
                    "parentCommandQueueBytes": self._command_bytes,
                    "parentCommandQueueCount": self._command_count,
                    "parentEventBytes": self._event_bytes,
                    "parentEventCount": self._event_count,
                    "parentResponseRouteBytes": self._route_bytes,
                    "parentResponseRouteCount": self._route_count,
                }
            )


class ParentStdinWriter:
    """The only parent-side writer for complete Worker command lines."""

    def __init__(
        self,
        state: WorkerIpcState,
        *,
        write: Callable[[int, bytes | memoryview], int] = os.write,
    ) -> None:
        self._state = state
        self._write = write

    def write_one(self, descriptor: int, *, timeout: float | None = None) -> bool:
        record = self._state.dequeue(timeout)
        if record is None:
            return False
        view = memoryview(record.line)
        offset = 0
        try:
            while offset < len(view):
                try:
                    written = self._write(descriptor, view[offset:])
                except InterruptedError:
                    continue
                if type(written) is not int or written <= 0 or written > len(view) - offset:
                    _fail("IPC_WRITE_FAILED")
                self._state.record_write(record.command_id, written)
                offset += written
            self._state.finish_write(record.command_id, success=True)
            return True
        except Exception as error:
            try:
                self._state.finish_write(record.command_id, success=False)
            finally:
                self._state.fail_epoch()
            if isinstance(error, WorkerIpcFailure):
                raise
            raise WorkerIpcFailure("IPC_WRITE_FAILED") from error


__all__ = [
    "IpcDelivery",
    "ParentStdinWriter",
    "WorkerIpcFailure",
    "WorkerIpcState",
]
