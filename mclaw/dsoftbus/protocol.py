# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Stable constants and strict wire primitives for the DSoftBus Runtime.

This module is deliberately platform neutral.  Importing it must never load the
OpenHarmony Native shim (or ``ctypes``); the shim is owned by the isolated
worker process.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, NoReturn
import uuid


SERVICE_NAME = "mclaw.a2a.v1"
CLIENT_SERVICE_NAME = "mclaw.a2a.v1.client"
SOFTBUS_PACKAGE_NAME = "mclaw"
PROTOCOL_BINDING = "https://gitcode.com/m-robots/mclaw/specs/a2a-softbus/v1"
DEVICE_CONTEXT_EXTENSION_URI = (
    "https://gitcode.com/m-robots/mclaw/specs/device-context/v1"
)
A2A_PROTOCOL_VERSION = "1.0"
A2A_REFERENCE_SCHEMA_RELEASE = "1.0.1"
BINDING_VERSION = 1
NATIVE_ABI_VERSION = 1

REMOTE_FRAME_MAX = 32_768
MIN_NEGOTIATED_FRAME = 4_096
TERMINAL_ERROR_FRAME_MAX = 1_024
TRANSIENT_SOCKET_CAP = 2
SOFTBUS_HANDLE_TOMBSTONE_MAX = 4_096
AGENT_CARD_MAX = 8_192
DEVICE_DOCUMENT_MAX = 24_576
TOOL_RESULT_MAX = 65_536
SERVICE_PARAMETER_MAX = 8
SERVICE_PARAMETER_KEY_MAX = 64
SERVICE_PARAMETER_VALUE_MAX = 2_048
SERVICE_PARAMETERS_BYTES_MAX = 4_096
IPC_LINE_MAX = 65_536
NATIVE_EVENT_CAP = 128
NATIVE_EVENT_BYTES_MAX = 4_194_304
WORKER_EVENT_CAP = 128
WORKER_EVENT_BYTES_MAX = 4_194_304
PARENT_EVENT_CAP = 128
PARENT_EVENT_BYTES_MAX = 4_194_304
PARENT_COMMAND_QUEUE_MAX = 48
PARENT_COMMAND_QUEUE_BYTES_MAX = 2_097_152
PARENT_RESPONSE_ROUTE_MAX = 48
PARENT_RESPONSE_ROUTE_BYTES_MAX = 2_097_152
SOCKET_SEND_QUEUE_MAX = 32
SOCKET_SEND_QUEUE_BYTES_MAX = 1_048_576
GLOBAL_SEND_QUEUE_MAX = 128
GLOBAL_SEND_QUEUE_BYTES_MAX = 4_194_304
SOCKET_CONTROL_SEND_MAX = 2
SOCKET_CONTROL_SEND_BYTES_MAX = 2_048
SOCKET_BUSINESS_SEND_BURST_MAX = 8
SOCKET_CONTROL_SEND_BURST_MAX = 2
NODE_SNAPSHOT_MAX = 256
NODE_SNAPSHOT_BYTES_MAX = 262_144
NODE_SNAPSHOT_PAGE_MAX = 32
NODE_SNAPSHOT_PAGE_BYTES_MAX = 49_152
NODE_SNAPSHOT_TTL_S = 5
PEER_REGISTRY_MAX = 64
DISPATCH_QUEUE_MAX = 32
DISPATCH_QUEUE_BYTES_MAX = 1_048_576
PER_PEER_DISPATCH_PENDING_MAX = 8
PER_REQUEST_WAITER_MAX = 8
PER_PEER_IDEMPOTENCY_WAITER_MAX = 64
PER_PEER_IDEMPOTENCY_WAITER_BYTES_MAX = 524_288
IDEMPOTENCY_WAITER_MAX = 256
IDEMPOTENCY_WAITER_BYTES_MAX = 2_097_152
REMOTE_CONTEXT_MAX = 32
PER_PEER_REMOTE_CONTEXT_MAX = 8
REMOTE_CONTEXT_BYTES_MAX = 4_194_304
PER_PEER_REMOTE_CONTEXT_BYTES_MAX = 1_048_576
REMOTE_CONTEXT_MESSAGE_MAX = 64
REMOTE_CONTEXT_UTF8_MAX = 131_072
STATE_READ_PER_PEER_PER_MINUTE = 60
STATE_READ_GLOBAL_PER_MINUTE = 240
REMOTE_MODEL_MAX_OUTPUT_TOKENS = 8_192
MODEL_STREAM_QUEUE_MAX = 64
MODEL_STREAM_QUEUE_BYTES_MAX = 262_144
REMOTE_MODEL_ACCUMULATOR_BYTES_MAX = 65_536
MAX_OPEN_PEERS = 12
TASK_HISTORY_MAX = 128
TASK_ARTIFACT_MAX = 32
TASK_JSON_BYTES_MAX = 262_144
TASK_ARTIFACT_BYTES_MAX = 16_777_216
# Current Artifact updates are one SoftBus frame.  Keep model-produced JSON or
# file bytes below this smaller bound so Base64 and A2A envelope overhead still
# fit the negotiated frame; chunk/append transfer remains deliberately closed.
TASK_SINGLE_FRAME_ARTIFACT_BYTES_MAX = 16_384
TASK_STREAM_QUEUE_MAX = 64
TASK_STREAM_ITEM_BYTES_MAX = REMOTE_FRAME_MAX
CONTROL_TIMEOUT_S = 5
# Runtime startup performs several individually bounded Worker and SoftBus
# control operations.  Its outer budget must cover the sequence rather than
# reuse the five-second budget of one control request.
RUNTIME_START_TIMEOUT_S = 30
DEVICE_DISCOVERY_WINDOW_S = 5.0
DEVICE_BIND_TIMEOUT_S = 120.0
DEVICE_BIND_POLL_INTERVAL_S = 0.25
DSOFTBUS_SHUTDOWN_TIMEOUT_S = 40
DSOFTBUS_EVIDENCE_HDC_GRACE_MS = 30_000
HDC_MERGED_PREFIX_BYTES_MAX = 65_536
DSB_IDENTITY_RESULT_BYTES_MAX = 65_536
DSB_ECHO_RESULT_BYTES_MAX = 1_048_576
RESOURCE_SAMPLE_BYTES_MAX = 8_388_608
RESOURCE_DIRECT_CHILD_PIDS_MAX = 1_024
RESOURCE_MAPPED_FILES_MAX_PER_PROCESS = 4_096
RESOURCE_MAPPED_PATH_UTF8_MAX = 4_096
RESOURCE_MAPPED_FILE_BYTES_MAX = 268_435_456
RESOURCE_MAPPED_HASH_BYTES_MAX = 536_870_912
RESOURCE_RUNTIME_HEALTH_BYTES_MAX = 1_048_576
RESOURCE_ENDPOINT_LOCK_BYTES_MAX = 65_536
BIND_TIMEOUT_MS = 10_000
RESPONSE_CACHE_CAP = 256
PER_PEER_RESPONSE_CACHE_CAP = 64
RESPONSE_CACHE_BYTES_MAX = 8_388_608
PER_PEER_RESPONSE_CACHE_BYTES_MAX = 2_097_152
RESPONSE_CACHE_TTL_S = 600
WORKER_RESTART_LIMIT = 3
WORKER_RESTART_WINDOW_S = 60
RECONNECT_BASE_S = 0.5
RECONNECT_MAX_S = 30
WORKER_COMMAND_CAP = 64
WORKER_COMMAND_BYTES_MAX = 2_097_152
WORKER_RESPONSE_CAP = 64
WORKER_RESPONSE_BYTES_MAX = 2_097_152
COMMAND_BURST_MAX = 8
NATIVE_EVENT_BURST_MAX = 32
RESPONSE_BURST_MAX = 8


RPC_ERROR_CODES: Mapping[str, int] = MappingProxyType(
    {
        "PARSE_ERROR": -32700,
        "INVALID_REQUEST": -32600,
        "METHOD_NOT_FOUND": -32601,
        "INVALID_PARAMS": -32602,
        "INTERNAL_ERROR": -32603,
        "TASK_NOT_FOUND": -32001,
        "TASK_NOT_CANCELABLE": -32002,
        "PUSH_NOT_SUPPORTED": -32003,
        "UNSUPPORTED_OPERATION": -32004,
        "CONTENT_TYPE_NOT_SUPPORTED": -32005,
        "INVALID_AGENT_RESPONSE": -32006,
        "EXTENDED_AGENT_CARD_NOT_CONFIGURED": -32007,
        "EXTENSION_SUPPORT_REQUIRED": -32008,
        "VERSION_NOT_SUPPORTED": -32009,
        "PEER_NOT_READY": -32010,
        "CAPACITY_BUSY": -32011,
        "DEADLINE_EXCEEDED": -32012,
        "FRAME_TOO_LARGE": -32013,
        "STALE_GENERATION": -32014,
        "MANIFEST_CONFLICT": -32015,
        "RUNTIME_STOPPING": -32016,
        "BINDING_INCOMPATIBLE": -32017,
        "AGENT_INTERRUPTED": -32018,
        "CONTEXT_NOT_FOUND": -32020,
        "REMOTE_INFERENCE_DISABLED": -32021,
        "RATE_LIMITED": -32022,
        "REMOTE_BUDGET_EXCEEDED": -32023,
        "REMOTE_PROVIDER_UNAVAILABLE": -32024,
        "PROVIDER_ERROR": -32025,
        "AGENT_TOOLS_FORBIDDEN": -32026,
    }
)

WORKER_OPERATIONS = frozenset(
    {
        "hello",
        "start",
        "snapshot_nodes",
        "get_node_udid",
        "start_device_discovery",
        "stop_device_discovery",
        "begin_device_bind",
        "get_device_bind_status",
        "list_trusted_devices",
        "unbind_device",
        "listen",
        "connect",
        "send_bytes",
        "close_socket",
        "stop",
    }
)


class ProtocolError(ValueError):
    """A stable, non-sensitive protocol validation failure."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _invalid(detail: str, *, code: str = "INVALID_REQUEST") -> NoReturn:
    raise ProtocolError(code, detail)


def canonical_json_bytes(value: Any) -> bytes:
    """Return compact, code-point-key-sorted UTF-8 JSON plus exactly one LF."""

    try:
        rendered = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return (rendered + "\n").encode("utf-8")
    except (TypeError, UnicodeEncodeError, ValueError) as error:
        raise ProtocolError("INVALID_JSON_VALUE", "value is not canonical JSON") from error


def canonical_digest(value: Any) -> str:
    """Return the protocol-form SHA-256 digest of canonical JSON bytes."""

    return f"sha256:{hashlib.sha256(canonical_json_bytes(value)).hexdigest()}"


def _reject_constant(_: str) -> NoReturn:
    _invalid("non-finite JSON number", code="PARSE_ERROR")


def _pairs_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _invalid(f"duplicate object key {key!r}", code="PARSE_ERROR")
        result[key] = value
    return result


def strict_json_loads(
    raw: bytes,
    *,
    max_bytes: int,
    require_canonical: bool = False,
    require_object: bool = False,
) -> Any:
    """Decode bounded duplicate-key-free UTF-8 JSON.

    ``require_canonical`` additionally requires compact sorted bytes and one
    trailing LF.  This is used for on-disk locks and NDJSON frames.
    """

    if not isinstance(raw, bytes):
        _invalid("JSON input must be bytes", code="PARSE_ERROR")
    if type(max_bytes) is not int or max_bytes < 1:
        raise ValueError("max_bytes must be a positive integer")
    if not raw or len(raw) > max_bytes:
        _invalid("JSON byte length is outside the allowed range", code="PARSE_ERROR")
    if raw.startswith(b"\xef\xbb\xbf"):
        _invalid("UTF-8 BOM is forbidden", code="PARSE_ERROR")
    try:
        text = raw.decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=_pairs_object,
            parse_constant=_reject_constant,
        )
    except ProtocolError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ProtocolError("PARSE_ERROR", "invalid UTF-8 JSON") from error
    if require_object and not isinstance(value, dict):
        _invalid("root value must be an object")
    if require_canonical and canonical_json_bytes(value) != raw:
        _invalid("JSON bytes are not compact sorted canonical JSON plus LF")
    return value


def exact_object(value: Any, keys: frozenset[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _invalid(f"{label} must be an object")
    actual = frozenset(value)
    if actual != keys:
        missing = sorted(keys - actual)
        extra = sorted(actual - keys)
        _invalid(f"{label} keys mismatch missing={missing} extra={extra}")
    return value


def canonical_uuid4(value: Any, label: str) -> str:
    if not isinstance(value, str):
        _invalid(f"{label} must be a UUID4 string")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as error:
        raise ProtocolError("INVALID_REQUEST", f"{label} is not UUID4") from error
    if parsed.version != 4 or str(parsed) != value:
        _invalid(f"{label} must be lowercase canonical UUID4")
    return value


def bounded_utf8(value: Any, label: str, minimum: int, maximum: int) -> str:
    if not isinstance(value, str):
        _invalid(f"{label} must be a string")
    size = len(value.encode("utf-8"))
    if size < minimum or size > maximum or "\x00" in value:
        _invalid(f"{label} UTF-8 length is outside {minimum}..{maximum}")
    return value


def bounded_integer(
    value: Any, label: str, minimum: int, maximum: int
) -> int:
    if type(value) is not int or value < minimum or value > maximum:
        _invalid(f"{label} must be an integer in {minimum}..{maximum}")
    return value


def decode_strict_base64(value: Any, *, maximum: int = REMOTE_FRAME_MAX) -> bytes:
    text = bounded_utf8(value, "args.data", 1, ((maximum + 2) // 3) * 4)
    try:
        encoded = text.encode("ascii")
        decoded = base64.b64decode(encoded, validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError) as error:
        raise ProtocolError("INVALID_REQUEST", "args.data is not strict base64") from error
    if not decoded or len(decoded) > maximum or base64.b64encode(decoded) != encoded:
        _invalid("args.data is not canonical padded base64")
    return decoded


def _validate_command_args(op: str, value: Any) -> None:
    if op in {
        "hello",
        "start",
        "start_device_discovery",
        "stop",
        "stop_device_discovery",
    }:
        exact_object(value, frozenset(), f"{op}.args")
        return
    if op == "snapshot_nodes":
        if value == {}:
            return
        args = exact_object(
            value, frozenset({"cursor", "snapshotId"}), "snapshot_nodes.args"
        )
        canonical_uuid4(args["snapshotId"], "snapshot_nodes.args.snapshotId")
        bounded_utf8(args["cursor"], "snapshot_nodes.args.cursor", 1, 1_024)
        return
    if op == "get_node_udid":
        args = exact_object(value, frozenset({"networkId"}), "get_node_udid.args")
        bounded_utf8(args["networkId"], "get_node_udid.args.networkId", 1, 64)
        return
    if op in {"begin_device_bind", "get_device_bind_status"}:
        args = exact_object(
            value, frozenset({"deviceIdSha256"}), f"{op}.args"
        )
        digest = bounded_utf8(
            args["deviceIdSha256"], f"{op}.args.deviceIdSha256", 64, 64
        )
        if any(character not in "0123456789abcdef" for character in digest):
            _invalid(f"{op}.args.deviceIdSha256 is not lower-hex SHA-256")
        return
    if op == "list_trusted_devices":
        exact_object(value, frozenset(), "list_trusted_devices.args")
        return
    if op == "unbind_device":
        args = exact_object(value, frozenset({"networkId"}), "unbind_device.args")
        bounded_utf8(args["networkId"], "unbind_device.args.networkId", 1, 96)
        return
    if op == "listen":
        args = exact_object(value, frozenset({"serviceName"}), "listen.args")
        if args["serviceName"] != SERVICE_NAME:
            _invalid("listen.args.serviceName is not the fixed server service")
        return
    if op == "connect":
        args = exact_object(
            value,
            frozenset({"networkId", "peerServiceName", "serviceName"}),
            "connect.args",
        )
        if (
            args["serviceName"] != CLIENT_SERVICE_NAME
            or args["peerServiceName"] != SERVICE_NAME
        ):
            _invalid("connect.args service names are not the fixed pair")
        bounded_utf8(args["networkId"], "connect.args.networkId", 1, 64)
        return
    if op == "send_bytes":
        args = exact_object(value, frozenset({"data", "socket"}), "send_bytes.args")
        bounded_integer(args["socket"], "send_bytes.args.socket", 0, 2**31 - 1)
        decode_strict_base64(args["data"])
        return
    if op == "close_socket":
        args = exact_object(value, frozenset({"socket"}), "close_socket.args")
        bounded_integer(args["socket"], "close_socket.args.socket", 0, 2**31 - 1)
        return
    _invalid("unsupported worker operation")


@dataclass(frozen=True, slots=True)
class WorkerCommand:
    version: int
    command_id: str
    operation: str
    args: Mapping[str, Any]
    line_bytes: int


def parse_worker_command(line: bytes) -> WorkerCommand:
    """Parse one complete canonical worker command NDJSON line."""

    value = strict_json_loads(
        line,
        max_bytes=IPC_LINE_MAX,
        require_canonical=True,
        require_object=True,
    )
    command = exact_object(value, frozenset({"args", "id", "op", "v"}), "command")
    if type(command["v"]) is not int or command["v"] != 1:
        _invalid("command.v must be integer 1")
    command_id = canonical_uuid4(command["id"], "command.id")
    operation = command["op"]
    if not isinstance(operation, str) or operation not in WORKER_OPERATIONS:
        _invalid("command.op is not supported")
    _validate_command_args(operation, command["args"])
    return WorkerCommand(
        version=1,
        command_id=command_id,
        operation=operation,
        args=MappingProxyType(dict(command["args"])),
        line_bytes=len(line),
    )


def encode_worker_command(
    operation: str,
    args: Mapping[str, Any],
    *,
    command_id: str | None = None,
) -> bytes:
    """Build and self-validate one canonical worker command line."""

    if command_id is None:
        command_id = str(uuid.uuid4())
    line = canonical_json_bytes(
        {"args": dict(args), "id": command_id, "op": operation, "v": 1}
    )
    parse_worker_command(line)
    return line


def encode_ipc_object(value: Mapping[str, Any]) -> bytes:
    """Encode a generic response/event object under the shared IPC line cap."""

    line = canonical_json_bytes(dict(value))
    if len(line) > IPC_LINE_MAX:
        _invalid("IPC object exceeds IPC_LINE_MAX", code="FRAME_TOO_LARGE")
    return line


__all__ = [
    "A2A_PROTOCOL_VERSION",
    "A2A_REFERENCE_SCHEMA_RELEASE",
    "BINDING_VERSION",
    "CLIENT_SERVICE_NAME",
    "DEVICE_CONTEXT_EXTENSION_URI",
    "IPC_LINE_MAX",
    "NATIVE_ABI_VERSION",
    "PROTOCOL_BINDING",
    "ProtocolError",
    "REMOTE_FRAME_MAX",
    "RPC_ERROR_CODES",
    "SERVICE_NAME",
    "SOFTBUS_PACKAGE_NAME",
    "WorkerCommand",
    "WORKER_OPERATIONS",
    "canonical_digest",
    "canonical_json_bytes",
    "decode_strict_base64",
    "encode_ipc_object",
    "encode_worker_command",
    "parse_worker_command",
    "strict_json_loads",
]
